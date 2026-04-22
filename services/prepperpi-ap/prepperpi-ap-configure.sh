#!/usr/bin/env bash
# prepperpi-ap-configure.sh
#
# Runs once per boot, before hostapd and dnsmasq start. Reads the user's
# overrides from /boot/firmware/prepperpi.conf (if present), derives the
# SSID from the onboard MAC, picks a 2.4 GHz channel if not forced,
# renders hostapd and dnsmasq configs from the templates shipped with
# this service, and brings wlan0 up with 10.42.0.1/24.
#
# Idempotent. Safe to re-run by hand:
#   sudo systemctl restart prepperpi-ap-configure
#
# Exits non-zero on any step that matters so systemd can surface the
# failure and block the dependent units.

set -euo pipefail

# ---------- paths and defaults ----------

readonly PREFIX="${PREFIX:-/opt/prepperpi}"                      # where service files live
readonly RUN_DIR="${RUN_DIR:-/run/prepperpi}"                    # tmpfs, recomputed per boot
readonly LOG_DIR="${LOG_DIR:-/var/log/prepperpi}"

readonly BOOT_CONF_CANDIDATES=(
  "/boot/firmware/prepperpi.conf"
  "/boot/prepperpi.conf"
)

readonly TMPL_DIR="${TMPL_DIR:-${PREFIX}/services/prepperpi-ap}"
readonly HOSTAPD_OUT="/etc/hostapd/hostapd.conf"
readonly DNSMASQ_OUT="/etc/dnsmasq.d/prepperpi.conf"

# Defaults — overridden by /boot/firmware/prepperpi.conf if present.
INTERFACE="wlan0"
SSID=""
WIFI_PASSWORD=""
COUNTRY="US"
CHANNEL="auto"
MAX_STA=""

log() { printf '[prepperpi-ap-configure] %s\n' "$*"; }
die() { printf '[prepperpi-ap-configure] FATAL: %s\n' "$*" >&2; exit 1; }

# ---------- load overrides ----------

load_overrides() {
  local conf=""
  for candidate in "${BOOT_CONF_CANDIDATES[@]}"; do
    if [[ -r "$candidate" ]]; then conf="$candidate"; break; fi
  done
  if [[ -z "$conf" ]]; then
    log "no prepperpi.conf found; using defaults"
    return 0
  fi
  log "reading overrides from ${conf}"

  # Very small KEY=value parser. Rejects anything weird so a typo
  # can't be turned into shell injection.
  local line key val
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"                             # strip comments
    line="${line#"${line%%[![:space:]]*}"}"        # ltrim
    line="${line%"${line##*[![:space:]]}"}"        # rtrim
    [[ -z "$line" ]] && continue
    if [[ "$line" =~ ^([A-Z][A-Z0-9_]*)=(.*)$ ]]; then
      key="${BASH_REMATCH[1]}"
      val="${BASH_REMATCH[2]}"
      # Strip surrounding single or double quotes if present.
      if [[ "$val" =~ ^\"(.*)\"$ || "$val" =~ ^\'(.*)\'$ ]]; then
        val="${BASH_REMATCH[1]}"
      fi
      case "$key" in
        SSID|WIFI_PASSWORD|COUNTRY|CHANNEL|MAX_STA|INTERFACE)
          printf -v "$key" '%s' "$val"
          ;;
        *)
          log "ignoring unknown override '${key}'"
          ;;
      esac
    else
      log "skipping malformed line: ${line}"
    fi
  done < "$conf"
}

# ---------- helpers ----------

mac_of() {
  local iface="$1"
  local mac_file="/sys/class/net/${iface}/address"
  [[ -r "$mac_file" ]] || die "no MAC for ${iface} (is the radio enabled?)"
  tr -d ':\n ' < "$mac_file"
}

default_ssid_from_mac() {
  local mac="$1"
  local last4="${mac: -4}"
  local upper
  upper=$(printf '%s' "$last4" | tr 'a-f' 'A-F')
  printf 'PrepperPi-%s' "$upper"
}

pi_model_default_max_sta() {
  local model="unknown"
  if [[ -r /proc/device-tree/model ]]; then
    # device-tree/model is NUL-terminated.
    model=$(tr -d '\0' </proc/device-tree/model)
  fi
  case "$model" in
    *"Raspberry Pi 5"*) printf '20' ;;
    *"Raspberry Pi 4"*) printf '10' ;;
    *)                  printf '10' ;;  # conservative default
  esac
}

pick_channel() {
  # Pick the least-busy of the non-overlapping 2.4 GHz channels (1,6,11).
  # Requires `iw` and a radio that's up but not yet associated.
  local iface="$1"
  if ! command -v iw >/dev/null 2>&1; then
    log "iw not installed; defaulting to channel 6"
    printf '6'
    return 0
  fi
  # Make sure the radio can scan.
  ip link set "$iface" up 2>/dev/null || true

  local scan
  # A scan can transiently fail while NetworkManager/wpa_supplicant is
  # still releasing the radio. Retry a few times.
  local attempt
  for attempt in 1 2 3; do
    if scan=$(iw dev "$iface" scan 2>/dev/null); then break; fi
    sleep 1
    scan=""
  done
  if [[ -z "${scan:-}" ]]; then
    log "scan failed; defaulting to channel 6"
    printf '6'
    return 0
  fi

  local ch1=0 ch6=0 ch11=0 freq
  while read -r freq; do
    case "$freq" in
      2412) ch1=$((ch1+1)) ;;
      2437) ch6=$((ch6+1)) ;;
      2462) ch11=$((ch11+1)) ;;
    esac
  done < <(printf '%s\n' "$scan" | awk '/^[[:space:]]*freq:/ {print $2}')

  log "channel load: ch1=${ch1} ch6=${ch6} ch11=${ch11}"
  # Ties go to 6 (middle; most devices default to it).
  local best=6 best_count=$ch6
  if (( ch1 < best_count )); then best=1; best_count=$ch1; fi
  if (( ch11 < best_count )); then best=11; fi
  printf '%s' "$best"
}

render_auth_block() {
  local pass="$1"
  if [[ -z "$pass" ]]; then
    printf 'auth_algs=1\nwpa=0\n'
  else
    # Require at least 8 printable ASCII chars per WPA2 spec.
    if (( ${#pass} < 8 || ${#pass} > 63 )); then
      die "WIFI_PASSWORD must be 8..63 characters (got ${#pass})"
    fi
    printf 'auth_algs=1\nwpa=2\nwpa_key_mgmt=WPA-PSK\nrsn_pairwise=CCMP\nwpa_passphrase=%s\n' "$pass"
  fi
}

# Template fill: replace @KEY@ with the values in ${_TMPL[*]} (set by caller).
render_template() {
  local src="$1" dst="$2"
  shift 2
  local tmp
  tmp=$(mktemp)
  cp "$src" "$tmp"
  while (( $# >= 2 )); do
    local key="$1" val="$2"
    # Use a sed delimiter unlikely to appear in any of our values.
    # Escape backslashes, ampersands, and the delimiter.
    local esc
    esc=$(printf '%s' "$val" \
          | sed -e 's/[\\&|]/\\&/g' \
          | awk '{ gsub(/\n/, "\\n"); print }')
    sed -i "s|@${key}@|${esc}|g" "$tmp"
    shift 2
  done
  install -o root -g root -m 0644 "$tmp" "$dst"
  rm -f "$tmp"
}

# ---------- main ----------

main() {
  install -d -m 0755 "$RUN_DIR" "$LOG_DIR" /etc/hostapd /etc/dnsmasq.d

  load_overrides

  [[ -d "/sys/class/net/${INTERFACE}" ]] \
    || die "interface ${INTERFACE} does not exist"

  local mac
  mac=$(mac_of "$INTERFACE")
  if [[ -z "$SSID" ]]; then
    SSID=$(default_ssid_from_mac "$mac")
  fi

  if [[ -z "$MAX_STA" ]]; then
    MAX_STA=$(pi_model_default_max_sta)
  fi

  if [[ "$CHANNEL" == "auto" || -z "$CHANNEL" ]]; then
    CHANNEL=$(pick_channel "$INTERFACE")
  fi

  log "interface=${INTERFACE} ssid=${SSID} channel=${CHANNEL} country=${COUNTRY} max_sta=${MAX_STA}"

  # Stop anything that wants to manage wlan0 so hostapd can have it.
  # NetworkManager: unmanaged. wpa_supplicant: stop on this iface.
  if systemctl is-active --quiet NetworkManager 2>/dev/null; then
    nmcli dev set "$INTERFACE" managed no 2>/dev/null || true
  fi
  systemctl stop "wpa_supplicant@${INTERFACE}.service" 2>/dev/null || true

  # Force the interface into a known state and assign the AP IP.
  ip link set "$INTERFACE" down 2>/dev/null || true
  ip addr flush dev "$INTERFACE" || true
  ip link set "$INTERFACE" up
  ip addr add 10.42.0.1/24 dev "$INTERFACE"

  # Render configs.
  local auth_block
  auth_block=$(render_auth_block "$WIFI_PASSWORD")

  render_template "${TMPL_DIR}/hostapd.conf.tmpl" "$HOSTAPD_OUT" \
    INTERFACE "$INTERFACE" \
    SSID "$SSID" \
    COUNTRY "$COUNTRY" \
    CHANNEL "$CHANNEL" \
    MAX_STA "$MAX_STA" \
    AUTH_BLOCK "$auth_block"

  render_template "${TMPL_DIR}/dnsmasq.conf.tmpl" "$DNSMASQ_OUT" \
    INTERFACE "$INTERFACE"

  # hostapd on Debian needs DAEMON_CONF set or started with the -B flag.
  # We drop a small override that points it at our generated file.
  install -d -m 0755 /etc/default
  printf 'DAEMON_CONF="%s"\n' "$HOSTAPD_OUT" > /etc/default/hostapd

  # Surface the current runtime config for debugging / admin console reads.
  {
    printf 'ssid=%s\n' "$SSID"
    printf 'interface=%s\n' "$INTERFACE"
    printf 'channel=%s\n' "$CHANNEL"
    printf 'country=%s\n' "$COUNTRY"
    printf 'max_sta=%s\n' "$MAX_STA"
    printf 'auth=%s\n' "$([[ -z "$WIFI_PASSWORD" ]] && echo open || echo wpa2)"
  } > "${RUN_DIR}/ap.state"

  log "configuration rendered; hostapd and dnsmasq can now start"
}

# Only run main() when executed directly, not when sourced by tests.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
