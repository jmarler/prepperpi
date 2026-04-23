#!/usr/bin/env bash
# installer/install.sh — top-level PrepperPi installer.
#
# Run as root on a fresh Raspberry Pi OS Lite (Bookworm or later)
# install. Orchestrates each services/*/setup.sh in order, provisions
# the `prepperpi` service account and /srv/prepperpi storage tree,
# and reboots into AP mode.
#
# Usage:
#   sudo installer/install.sh             # interactive
#   sudo installer/install.sh --yes       # non-interactive (e.g. curl|bash)
#   sudo installer/install.sh --no-reboot # skip the final reboot
#
# Safe to re-run. Each service's setup.sh is already idempotent;
# we no-op on existing state (user, storage tree, enabled units).

set -euo pipefail

readonly REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
readonly SERVICES_DIR="${REPO_DIR}/services"
readonly LOG_DIR="/var/log/prepperpi"
readonly LOG_FILE="${LOG_DIR}/install.log"
readonly SERVICE_USER="prepperpi"
readonly SERVICE_GROUP="prepperpi"
readonly STATE_DIR="/srv/prepperpi"
readonly STATE_SUBDIRS=(zim maps media user-usb config cache backups)

# Services are invoked in this order. Any services/* directory with a
# setup.sh that isn't in this list is skipped with a notice -- additions
# to this array are intentional, not automatic, so the install order is
# always explicit.
readonly SERVICE_ORDER=(
  prepperpi-ap
  prepperpi-web
)

# ---------- flags ----------

ASSUME_YES="no"
SKIP_REBOOT="no"
IMAGE_BUILD="no"

parse_args() {
  while (( $# > 0 )); do
    case "$1" in
      -y|--yes)        ASSUME_YES="yes"; shift ;;
      --no-reboot)     SKIP_REBOOT="yes"; shift ;;
      --image-build)
        # Building a pi-gen SD image: we're in a chroot on a non-Pi
        # host, so the hardware/OS preflight doesn't apply. Implies
        # --yes and --no-reboot.
        IMAGE_BUILD="yes"
        ASSUME_YES="yes"
        SKIP_REBOOT="yes"
        shift ;;
      -h|--help)
        sed -n '3,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
      *)
        printf 'unknown option: %s\n' "$1" >&2
        exit 2
        ;;
    esac
  done
}

# ---------- logging ----------

log()  { printf '[prepperpi/install] %s\n' "$*"; }
warn() { printf '[prepperpi/install] WARN: %s\n' "$*" >&2; }
die()  { printf '[prepperpi/install] FATAL: %s\n' "$*" >&2; exit 1; }

# ---------- preflight (pure helpers are unit-tested) ----------

require_root() {
  if [[ $EUID -ne 0 ]]; then
    die "installer must be run as root (try 'sudo $0')"
  fi
}

# Parse /proc/device-tree/model and return the major model number:
#   "Raspberry Pi 4 Model B Rev 1.5"  -> 4
#   "Raspberry Pi 5 Model B Rev 1.0"  -> 5
#   anything else                      -> empty string (unsupported)
detect_pi_model() {
  local src="${1:-/proc/device-tree/model}"
  [[ -r "$src" ]] || { printf ''; return; }
  local model
  model=$(tr -d '\0' <"$src")
  case "$model" in
    *"Raspberry Pi 5"*) printf '5' ;;
    *"Raspberry Pi 4"*) printf '4' ;;
    *)                  printf '' ;;
  esac
}

# Parse /etc/os-release and return VERSION_CODENAME.
detect_os_codename() {
  local src="${1:-/etc/os-release}"
  [[ -r "$src" ]] || { printf ''; return; }
  awk -F= '$1=="VERSION_CODENAME"{gsub(/"/,"",$2); print $2; exit}' "$src"
}

# We support Debian Bookworm (12) and later. Keeping this as a whitelist
# rather than a >= comparison so a surprise new Debian release doesn't
# break the installer silently.
is_supported_os_codename() {
  case "${1:-}" in
    bookworm|trixie|forky) return 0 ;;
    *) return 1 ;;
  esac
}

preflight() {
  log "preflight checks"
  if [[ "$IMAGE_BUILD" == "yes" ]]; then
    log "  image-build mode; skipping hardware + OS checks"
    return 0
  fi
  local model codename
  model=$(detect_pi_model)
  if [[ -z "$model" ]]; then
    die "unsupported hardware (expected Raspberry Pi 4 or 5; see /proc/device-tree/model)"
  fi
  log "  hardware:  Raspberry Pi ${model}"

  codename=$(detect_os_codename)
  if ! is_supported_os_codename "$codename"; then
    die "unsupported OS (expected Debian Bookworm or later; got '${codename:-unknown}')"
  fi
  log "  OS:        Debian ${codename}"

  local missing=""
  for cmd in apt-get systemctl install ip useradd getent; do
    command -v "$cmd" >/dev/null 2>&1 || missing+=" $cmd"
  done
  if [[ -n "$missing" ]]; then
    die "missing required commands:${missing}"
  fi
  log "  all required commands present"
}

# ---------- confirmation ----------

confirm_reboot_permission() {
  if [[ "$ASSUME_YES" == "yes" ]]; then
    log "running non-interactively (--yes); will reboot when install completes"
    return 0
  fi
  if [[ "$SKIP_REBOOT" == "yes" ]]; then
    log "--no-reboot set; no confirmation needed"
    return 0
  fi

  # No tty? We can't prompt. Refuse to proceed rather than silently
  # surprise the operator with a reboot.
  if [[ ! -t 0 ]]; then
    die "stdin is not a terminal; pass --yes to allow unattended reboot or --no-reboot to skip it"
  fi

  printf 'PrepperPi will install system packages, create the service account,\n'
  printf 'write systemd units, and REBOOT the Pi when it is done.\n'
  printf '\n'
  printf 'Proceed and reboot when finished? [y/N] '
  local answer=""
  read -r answer
  case "${answer,,}" in
    y|yes) log "confirmed; proceeding" ;;
    *)     log "aborted by user. Re-run this script when you're ready."
           exit 0 ;;
  esac
}

# ---------- log redirect ----------

setup_log() {
  install -d -m 0755 "$LOG_DIR"
  # Append a separator then redirect all further stdout/stderr through
  # tee so both the terminal and the log file see the same output.
  {
    printf '\n========== %s ==========\n' "$(date -Is)"
  } >>"$LOG_FILE"
  exec > >(tee -a "$LOG_FILE") 2>&1
  log "logging to ${LOG_FILE}"
}

# ---------- system state ----------

ensure_service_account() {
  if getent passwd "$SERVICE_USER" >/dev/null; then
    log "service account '${SERVICE_USER}' already exists"
    return 0
  fi
  log "creating system user '${SERVICE_USER}'"
  useradd --system \
          --home-dir "$STATE_DIR" \
          --no-create-home \
          --shell /usr/sbin/nologin \
          --user-group \
          "$SERVICE_USER"
}

ensure_state_tree() {
  log "ensuring state tree under ${STATE_DIR}"
  install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$STATE_DIR"
  local sub
  for sub in "${STATE_SUBDIRS[@]}"; do
    install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "${STATE_DIR}/${sub}"
  done
}

run_service_setups() {
  # Clear any stale "failed" state from previous dev runs so each
  # service's setup.sh starts from a clean slate. On a fresh install
  # this is a no-op; during repeated iteration on a test Pi it keeps
  # systemd from remembering long-past failures.
  systemctl reset-failed 'prepperpi-*' hostapd.service dnsmasq.service 2>/dev/null || true

  local name setup
  for name in "${SERVICE_ORDER[@]}"; do
    setup="${SERVICES_DIR}/${name}/setup.sh"
    if [[ ! -x "$setup" ]]; then
      warn "skipping ${name}: ${setup} is missing or not executable"
      continue
    fi
    log "running ${name}/setup.sh"
    # Each service script logs with its own prefix; our tee captures it.
    "$setup"
  done

  # Inform about any unexpected setup.sh scripts that exist in the
  # repo but aren't in SERVICE_ORDER yet -- catches "I added a new
  # service and forgot to wire it up."
  local dir skipped=""
  for dir in "$SERVICES_DIR"/*/; do
    name=$(basename "$dir")
    [[ -x "${dir}setup.sh" ]] || continue
    case " ${SERVICE_ORDER[*]} " in
      *" $name "*) ;;
      *) skipped+=" $name" ;;
    esac
  done
  if [[ -n "$skipped" ]]; then
    warn "found setup.sh for service(s) not in SERVICE_ORDER:${skipped}"
    warn "  add them to installer/install.sh SERVICE_ORDER if they should be installed"
  fi
}

do_reboot() {
  if [[ "$SKIP_REBOOT" == "yes" ]]; then
    log "--no-reboot set; installation complete. Reboot manually to activate."
    return 0
  fi
  log "installation complete; rebooting in 5 seconds (Ctrl-C to abort)"
  sleep 5
  systemctl reboot
}

# ---------- main ----------

main() {
  parse_args "$@"
  require_root
  preflight
  confirm_reboot_permission
  setup_log

  log "PrepperPi installer starting"
  ensure_service_account
  ensure_state_tree
  run_service_setups
  log "all services installed"

  do_reboot
}

# Only run main() when executed directly. Sourcing is supported so unit
# tests can exercise the pure helpers without triggering side effects.
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
