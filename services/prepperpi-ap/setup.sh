#!/usr/bin/env bash
# setup.sh — install prepperpi-ap onto the running system.
#
# Intended to be called by the top-level installer (installer/install.sh).
# Safe to re-run. Assumes Debian/Raspberry Pi OS Lite (Bookworm or later)
# and that we are running as root.

set -euo pipefail

readonly REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
readonly SRC_DIR="${REPO_DIR}/services/prepperpi-ap"
readonly PREFIX="${PREFIX:-/opt/prepperpi}"
readonly DST_DIR="${PREFIX}/services/prepperpi-ap"

log() { printf '[prepperpi-ap/setup] %s\n' "$*"; }

require_root() {
  if [[ $EUID -ne 0 ]]; then
    echo "setup.sh must be run as root" >&2
    exit 1
  fi
}

install_packages() {
  log "installing apt packages (hostapd, dnsmasq, iw, iproute2)"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y --no-install-recommends \
    hostapd dnsmasq iw iproute2
  # Ship the services as disabled — we control them through our own
  # oneshot configure unit, so the stock units only start after we've
  # rendered config. Unmask if the image masks them.
  systemctl unmask hostapd.service || true
  systemctl unmask dnsmasq.service || true
  # Stop them for now; they will be started by prepperpi-ap.target.
  systemctl stop hostapd.service 2>/dev/null || true
  systemctl stop dnsmasq.service 2>/dev/null || true
}

install_files() {
  log "installing configure script and templates to ${DST_DIR}"
  install -d -m 0755 "$DST_DIR"
  install -m 0755 "${SRC_DIR}/prepperpi-ap-configure.sh" "${DST_DIR}/prepperpi-ap-configure.sh"
  install -m 0644 "${SRC_DIR}/hostapd.conf.tmpl"         "${DST_DIR}/hostapd.conf.tmpl"
  install -m 0644 "${SRC_DIR}/dnsmasq.conf.tmpl"         "${DST_DIR}/dnsmasq.conf.tmpl"
  install -m 0644 "${SRC_DIR}/prepperpi.conf.example"    "${DST_DIR}/prepperpi.conf.example"

  log "installing systemd units"
  install -m 0644 "${SRC_DIR}/prepperpi-ap-configure.service" /etc/systemd/system/prepperpi-ap-configure.service
  install -m 0644 "${SRC_DIR}/prepperpi-ap.target"            /etc/systemd/system/prepperpi-ap.target
}

seed_boot_conf() {
  # If there's no user override file on the boot partition yet, drop the
  # example there so operators can see what they can change.
  local boot_dir="/boot/firmware"
  [[ -d "$boot_dir" ]] || boot_dir="/boot"
  if [[ -d "$boot_dir" && ! -e "${boot_dir}/prepperpi.conf" ]]; then
    log "seeding ${boot_dir}/prepperpi.conf from example"
    install -m 0644 "${SRC_DIR}/prepperpi.conf.example" "${boot_dir}/prepperpi.conf"
  fi
}

enable_units() {
  log "enabling prepperpi-ap units"
  systemctl daemon-reload
  systemctl enable prepperpi-ap-configure.service
  systemctl enable hostapd.service
  systemctl enable dnsmasq.service
  systemctl enable prepperpi-ap.target
}

main() {
  require_root
  install_packages
  install_files
  seed_boot_conf
  enable_units
  log "done. Reboot or run 'systemctl start prepperpi-ap.target' to activate."
}

main "$@"
