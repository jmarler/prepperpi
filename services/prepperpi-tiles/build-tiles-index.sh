#!/usr/bin/env bash
# build-tiles-index.sh — scan /srv/prepperpi/maps, regenerate tileserver
# config + composite style + landing-page fragment, restart the
# tileserver, and emit a maps_changed event when the set of installed
# regions has actually changed.
#
# Invoked by prepperpi-tiles-reindex.service (oneshot, triggered by the
# .path watcher). Also invoked once during install so the landing tile
# and the tileserver config exist before the unit first starts.
#
# All non-trivial work happens in tiles_indexer.py (pure-ish, unit
# tested). This shell wrapper is just I/O glue + restart.
#
# Safe to re-run.

set -euo pipefail

readonly MAPS_DIR="${MAPS_DIR:-/srv/prepperpi/maps}"
readonly TS_CONF_DIR="${TS_CONF_DIR:-/etc/prepperpi/tileserver}"
readonly STYLE_TEMPLATE="${STYLE_TEMPLATE:-${TS_CONF_DIR}/styles/osm-bright/style.template.json}"
readonly STYLE_OUT="${STYLE_OUT:-${TS_CONF_DIR}/styles/osm-bright/style.json}"
readonly CONFIG_OUT="${CONFIG_OUT:-${TS_CONF_DIR}/config.json}"
readonly FRAGMENT="${FRAGMENT:-/opt/prepperpi/web/landing/_maps.html}"
readonly REGIONS_JSON="${REGIONS_JSON:-/var/lib/prepperpi/maps/regions.json}"
readonly STATE_FILE="${STATE_FILE:-/var/lib/prepperpi/maps/last-regions.txt}"
readonly EVENT_EMITTER="${EVENT_EMITTER:-/opt/prepperpi/services/prepperpi-events/emit-event.py}"
readonly INDEXER="${INDEXER:-/opt/prepperpi/services/prepperpi-tiles/build-tiles-index.py}"
readonly SERVICE_USER="${SERVICE_USER:-prepperpi}"
readonly SERVICE_GROUP="${SERVICE_GROUP:-prepperpi}"

log() { printf '[prepperpi-tiles/reindex] %s\n' "$*"; }

main() {
  if [[ $EUID -ne 0 ]]; then
    echo "build-tiles-index.sh must be run as root" >&2
    exit 1
  fi

  install -d -m 0755 "$(dirname "$REGIONS_JSON")"
  install -d -m 0755 "$(dirname "$FRAGMENT")"
  install -d -m 0755 "$(dirname "$STYLE_OUT")"

  log "running indexer"
  if ! python3 "$INDEXER" \
        --maps-dir "$MAPS_DIR" \
        --style-template "$STYLE_TEMPLATE" \
        --style-out "$STYLE_OUT" \
        --config-out "$CONFIG_OUT" \
        --fragment-out "$FRAGMENT" \
        --regions-json "$REGIONS_JSON"; then
    log "WARN: indexer failed; leaving previous outputs in place"
    return 0
  fi

  # Make outputs readable by the prepperpi user (the tileserver runs
  # as that user) and by everyone else for the public landing fragment.
  chown -R "${SERVICE_USER}:${SERVICE_GROUP}" "$TS_CONF_DIR" 2>/dev/null || true
  chmod 0644 "$FRAGMENT"

  reload_tiles
  emit_change_event
}

reload_tiles() {
  if systemctl is-active --quiet prepperpi-tiles.service 2>/dev/null; then
    log "restarting prepperpi-tiles.service"
    systemctl restart prepperpi-tiles.service || log "WARN: restart failed"
  fi
}

emit_change_event() {
  [[ -x "$EVENT_EMITTER" ]] || return 0
  install -d -m 0755 "$(dirname "$STATE_FILE")"

  local current previous
  current=$(jq -r '.[].region_id' <"$REGIONS_JSON" 2>/dev/null | sort -u || true)
  previous=$(cat "$STATE_FILE" 2>/dev/null || true)

  if [[ "$current" == "$previous" ]]; then
    return 0
  fi

  printf '%s\n' "$current" >"$STATE_FILE"

  local cur_count
  cur_count=$(printf '%s\n' "$current" | grep -cE '^[a-zA-Z0-9_.-]+$' || true)

  local msg
  if (( cur_count == 0 )); then
    msg="Maps cleared"
  elif (( cur_count == 1 )); then
    msg="Maps updated · 1 region"
  else
    msg="Maps updated · ${cur_count} regions"
  fi
  "$EVENT_EMITTER" maps_changed "$msg" || true
}

main "$@"
