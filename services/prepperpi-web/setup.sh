#!/usr/bin/env bash
# setup.sh — install prepperpi-web (Caddy + landing page) onto the
# running system. Intended to be called by installer/install.sh.
# Safe to re-run. Assumes Debian/Raspberry Pi OS Lite (Trixie or
# Bookworm) and root.

set -euo pipefail

readonly REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
readonly SRC_DIR="${REPO_DIR}/services/prepperpi-web"
readonly WEB_SRC="${REPO_DIR}/web/landing"
readonly PREFIX="${PREFIX:-/opt/prepperpi}"
readonly WEB_DST="${PREFIX}/web/landing"
readonly CONF_DIR="/etc/prepperpi"
readonly SSL_DIR="${CONF_DIR}/ssl"
readonly SSL_CERT="${SSL_DIR}/cert.pem"
readonly SSL_KEY="${SSL_DIR}/key.pem"

log() { printf '[prepperpi-web/setup] %s\n' "$*"; }

require_root() {
  if [[ $EUID -ne 0 ]]; then
    echo "setup.sh must be run as root" >&2
    exit 1
  fi
}

install_packages() {
  log "installing apt packages (caddy, openssl)"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y --no-install-recommends caddy openssl

  # The Debian caddy package auto-enables a caddy.service that binds
  # :80 with a default placeholder config. We ship our own config via
  # prepperpi-web.service, so disable and stop the stock unit to free
  # the port. Safe to re-run.
  if systemctl is-enabled --quiet caddy.service 2>/dev/null; then
    log "disabling stock caddy.service (we run Caddy via prepperpi-web.service)"
    systemctl disable --now caddy.service || true
  fi
}

generate_ssl() {
  # Caddy serves :443 with this self-signed cert. Its only job is
  # to give Android's HTTPS captive probe a completed TLS handshake
  # (Android treats a completed handshake with an untrusted cert as
  # evidence of a captive portal; a RST is treated as "no internet").
  # Regenerating on every setup run would invalidate any browser-side
  # trust, so we only mint once per SAN set.
  install -d -m 0755 "$SSL_DIR"
  # Regenerate if the cert is missing OR doesn't include our current
  # friendly name in SAN (covers in-place upgrades that change the
  # hostname layout).
  if [[ -s "$SSL_CERT" && -s "$SSL_KEY" ]] \
     && openssl x509 -in "$SSL_CERT" -noout -text 2>/dev/null \
        | grep -q "DNS:prepperpi.home.arpa"; then
    log "reusing existing self-signed cert at ${SSL_CERT}"
    return 0
  fi
  log "generating self-signed cert at ${SSL_CERT}"
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$SSL_KEY" -out "$SSL_CERT" \
    -subj "/CN=PrepperPi" \
    -addext "subjectAltName=IP:10.42.0.1,DNS:prepperpi,DNS:prepperpi.home.arpa" \
    >/dev/null 2>&1
  # caddy user needs read access.
  chown root:caddy "$SSL_KEY" "$SSL_CERT"
  chmod 0640 "$SSL_KEY" "$SSL_CERT"
}

install_files() {
  log "installing Caddyfile to ${CONF_DIR}/Caddyfile"
  install -d -m 0755 "$CONF_DIR"
  install -m 0644 "${SRC_DIR}/Caddyfile" "${CONF_DIR}/Caddyfile"

  log "installing landing page to ${WEB_DST}"
  install -d -m 0755 "$WEB_DST"
  install -m 0644 "${WEB_SRC}/index.html" "${WEB_DST}/index.html"
  install -m 0644 "${WEB_SRC}/style.css"  "${WEB_DST}/style.css"

  log "installing systemd unit"
  install -m 0644 "${SRC_DIR}/prepperpi-web.service" /etc/systemd/system/prepperpi-web.service
}

validate_caddyfile() {
  # Run Caddy's own validator so a typo caught here doesn't turn into
  # a cryptic boot failure later.
  log "validating Caddyfile"
  caddy validate --config "${CONF_DIR}/Caddyfile" --adapter caddyfile
}

enable_units() {
  log "enabling prepperpi-web.service"
  systemctl daemon-reload
  systemctl enable prepperpi-web.service
}

main() {
  require_root
  install_packages
  generate_ssl
  install_files
  validate_caddyfile
  enable_units
  log "done. Start with 'systemctl start prepperpi-web.service' or reboot."
}

main "$@"
