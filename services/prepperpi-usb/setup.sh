#!/usr/bin/env bash
# setup.sh — install prepperpi-usb (USB hot-plug mount + landing tile
# + Markdown renderer) onto the running system. Intended to be called
# by installer/install.sh. Safe to re-run.

set -euo pipefail

readonly REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
readonly SRC_DIR="${REPO_DIR}/services/prepperpi-usb"
readonly PREFIX="${PREFIX:-/opt/prepperpi}"
readonly DST_DIR="${PREFIX}/services/prepperpi-usb"
readonly UDEV_RULE="/etc/udev/rules.d/99-prepperpi-usb.rules"

log() { printf '[prepperpi-usb/setup] %s\n' "$*"; }

require_root() {
  if [[ $EUID -ne 0 ]]; then
    echo "setup.sh must be run as root" >&2
    exit 1
  fi
}

install_packages() {
  log "installing apt packages (filesystem drivers + python markdown)"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y --no-install-recommends \
    exfatprogs \
    ntfs-3g \
    udev \
    util-linux \
    python3 \
    python3-markdown
}

install_files() {
  log "installing scripts and unit files to ${DST_DIR}"
  install -d -m 0755 "$DST_DIR"
  install -m 0755 "${SRC_DIR}/prepperpi-usb-mount.sh"   "${DST_DIR}/prepperpi-usb-mount.sh"
  install -m 0755 "${SRC_DIR}/prepperpi-usb-unmount.sh" "${DST_DIR}/prepperpi-usb-unmount.sh"
  install -m 0755 "${SRC_DIR}/build-usb-index.sh"       "${DST_DIR}/build-usb-index.sh"
  install -m 0644 "${SRC_DIR}/markdown_server.py"       "${DST_DIR}/markdown_server.py"

  install -m 0644 "${SRC_DIR}/prepperpi-usb-mount@.service"      /etc/systemd/system/prepperpi-usb-mount@.service
  install -m 0644 "${SRC_DIR}/prepperpi-usb-reindex.service"     /etc/systemd/system/prepperpi-usb-reindex.service
  install -m 0644 "${SRC_DIR}/prepperpi-usb-reindex.path"        /etc/systemd/system/prepperpi-usb-reindex.path
  install -m 0644 "${SRC_DIR}/prepperpi-usb-markdown.service"    /etc/systemd/system/prepperpi-usb-markdown.service

  log "installing udev rule at ${UDEV_RULE}"
  install -m 0644 "${SRC_DIR}/99-prepperpi-usb.rules" "$UDEV_RULE"
}

reload_udev() {
  log "reloading udev rules"
  udevadm control --reload-rules
  # No `udevadm trigger` here -- we don't want to mass-mount everything
  # already plugged in at install time. The next add event will fire
  # the rule naturally.
}

initial_index() {
  log "running initial USB index"
  "${DST_DIR}/build-usb-index.sh" || log "WARN: initial index failed"
}

enable_units() {
  log "enabling prepperpi-usb units"
  systemctl daemon-reload
  systemctl enable prepperpi-usb-reindex.path
  systemctl enable prepperpi-usb-markdown.service
  # Note: prepperpi-usb-mount@.service is a template, not enabled
  # directly. Instances are pulled in by the udev rule on demand.
}

main() {
  require_root
  install_packages
  install_files
  reload_udev
  initial_index
  enable_units
  log "done. Plug in a USB drive to test, or reboot to clean-state."
}

main "$@"
