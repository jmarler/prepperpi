#!/usr/bin/env bash
# setup.sh — install prepperpi-events. The "service" here is purely
# the emit-event.py helper that other services call to push events
# onto the dashboard log; there is no daemon. Caddy serves the log
# as a static file under /_events.json.

set -euo pipefail

readonly REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
readonly SRC_DIR="${REPO_DIR}/services/prepperpi-events"
readonly PREFIX="${PREFIX:-/opt/prepperpi}"
readonly DST_DIR="${PREFIX}/services/prepperpi-events"
readonly EVENTS_FILE="${PREFIX}/web/landing/_events.json"

log() { printf '[prepperpi-events/setup] %s\n' "$*"; }

require_root() {
  if [[ $EUID -ne 0 ]]; then
    echo "setup.sh must be run as root" >&2
    exit 1
  fi
}

install_files() {
  log "installing emit-event.py to ${DST_DIR}"
  install -d -m 0755 "$DST_DIR"
  install -m 0755 "${SRC_DIR}/emit-event.py" "${DST_DIR}/emit-event.py"

  log "installing prepperpi-boot-event.service"
  install -m 0644 "${SRC_DIR}/prepperpi-boot-event.service" \
    /etc/systemd/system/prepperpi-boot-event.service
}

enable_units() {
  systemctl daemon-reload
  systemctl enable prepperpi-boot-event.service
}

seed_events_file() {
  # Seed an empty events.json so the dashboard's first poll gets a
  # well-formed (but empty) response instead of a 404. Don't clobber
  # an existing file -- a re-run of the installer shouldn't wipe the
  # event log.
  if [[ ! -f "$EVENTS_FILE" ]]; then
    log "seeding empty ${EVENTS_FILE}"
    install -d -m 0755 "$(dirname "$EVENTS_FILE")"
    printf '{"version":0,"events":[]}\n' >"$EVENTS_FILE"
    chmod 0644 "$EVENTS_FILE"
  fi
}

main() {
  require_root
  install_files
  seed_events_file
  enable_units
  log "done."
}

main "$@"
