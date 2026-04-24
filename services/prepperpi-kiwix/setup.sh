#!/usr/bin/env bash
# setup.sh — install prepperpi-kiwix (kiwix-serve + library indexer)
# onto the running system. Intended to be called by the top-level
# installer (installer/install.sh). Safe to re-run. Assumes Debian /
# Raspberry Pi OS Lite (Bookworm or later) and root.

set -euo pipefail

readonly REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
readonly SRC_DIR="${REPO_DIR}/services/prepperpi-kiwix"
readonly PREFIX="${PREFIX:-/opt/prepperpi}"
readonly DST_DIR="${PREFIX}/services/prepperpi-kiwix"
readonly LIB_STATE_DIR="/var/lib/prepperpi"
readonly SERVICE_USER="${SERVICE_USER:-prepperpi}"
readonly SERVICE_GROUP="${SERVICE_GROUP:-prepperpi}"

log() { printf '[prepperpi-kiwix/setup] %s\n' "$*"; }

require_root() {
  if [[ $EUID -ne 0 ]]; then
    echo "setup.sh must be run as root" >&2
    exit 1
  fi
}

install_packages() {
  log "installing apt packages (kiwix-tools, zim-tools)"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y --no-install-recommends kiwix-tools zim-tools
}

install_files() {
  log "installing reindex script and unit files to ${DST_DIR}"
  install -d -m 0755 "$DST_DIR"
  install -m 0755 "${SRC_DIR}/build-library-index.sh" "${DST_DIR}/build-library-index.sh"

  install -m 0644 "${SRC_DIR}/prepperpi-kiwix.service"          /etc/systemd/system/prepperpi-kiwix.service
  install -m 0644 "${SRC_DIR}/prepperpi-kiwix-reindex.service"  /etc/systemd/system/prepperpi-kiwix-reindex.service
  install -m 0644 "${SRC_DIR}/prepperpi-kiwix-reindex.path"     /etc/systemd/system/prepperpi-kiwix-reindex.path
}

ensure_state() {
  # library.xml lives here; prepperpi owns it so reindex (running as
  # root) can rewrite and kiwix-serve (running as prepperpi) can read.
  install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$LIB_STATE_DIR"

  # Seed an empty library.xml so the kiwix-serve unit can start before
  # any ZIMs are dropped in -- otherwise it would refuse to bind.
  if [[ ! -f "${LIB_STATE_DIR}/library.xml" ]]; then
    log "seeding empty library.xml"
    printf '<?xml version="1.0" encoding="UTF-8"?>\n<library version="20110515"/>\n' \
      >"${LIB_STATE_DIR}/library.xml"
    chown "$SERVICE_USER:$SERVICE_GROUP" "${LIB_STATE_DIR}/library.xml"
    chmod 0644 "${LIB_STATE_DIR}/library.xml"
  fi
}

initial_index() {
  # Run the reindex once now so the landing page has a fragment and
  # the library.xml reflects whatever ZIMs are already on disk.
  log "running initial library index"
  "${DST_DIR}/build-library-index.sh" || log "WARN: initial index failed (will retry on first ZIM event)"
}

enable_units() {
  log "enabling prepperpi-kiwix units"
  systemctl daemon-reload
  systemctl enable prepperpi-kiwix.service
  systemctl enable prepperpi-kiwix-reindex.path
}

main() {
  require_root
  install_packages
  install_files
  ensure_state
  initial_index
  enable_units
  log "done. Start with 'systemctl start prepperpi-kiwix.service prepperpi-kiwix-reindex.path' or reboot."
}

main "$@"
