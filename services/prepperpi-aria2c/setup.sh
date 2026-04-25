#!/usr/bin/env bash
# setup.sh — install prepperpi-aria2c. Runs aria2c as a long-lived
# JSON-RPC daemon under the `prepperpi` system user; the admin
# console talks to it over 127.0.0.1:6800 to queue, pause, resume,
# and cancel Kiwix ZIM downloads (E2-S3).
#
# Safe to re-run.

set -euo pipefail

readonly REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
readonly SRC_DIR="${REPO_DIR}/services/prepperpi-aria2c"
readonly PREFIX="${PREFIX:-/opt/prepperpi}"
readonly DST_DIR="${PREFIX}/services/prepperpi-aria2c"
readonly CONF_DIR=/etc/prepperpi/aria2
readonly STATE_DIR=/var/lib/prepperpi/aria2
readonly SERVICE_USER=prepperpi
readonly SERVICE_GROUP=prepperpi
readonly ADMIN_GROUP=prepperpi-admin

log() { printf '[prepperpi-aria2c/setup] %s\n' "$*"; }

require_root() {
  if [[ $EUID -ne 0 ]]; then
    echo "setup.sh must be run as root" >&2
    exit 1
  fi
}

install_packages() {
  log "installing apt packages (aria2)"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y --no-install-recommends aria2
}

ensure_state_dirs() {
  log "creating state directories"
  install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$STATE_DIR"
  # The downloading staging dirs. Per-USB ones are created on demand by
  # the admin daemon when the user picks a USB destination.
  install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_GROUP" /srv/prepperpi/zim/.downloading
  # session.txt has to exist (aria2 will read it via input-file), even
  # if empty.
  if [[ ! -f "${STATE_DIR}/session.txt" ]]; then
    install -m 0644 -o "$SERVICE_USER" -g "$SERVICE_GROUP" /dev/null "${STATE_DIR}/session.txt"
  fi
}

ensure_secret() {
  # Generate the RPC secret once. The file is owned prepperpi:prepperpi-admin
  # mode 0640 so both the aria2c daemon (running as prepperpi) and the
  # admin uvicorn (running as prepperpi-admin) can read it; nobody else
  # can. systemd reads it via EnvironmentFile= because aria2c only
  # supports passing the secret on the command line.
  local secret_file="${CONF_DIR}/secret.env"
  install -d -m 0755 /etc/prepperpi
  install -d -m 0755 "$CONF_DIR"

  if [[ ! -s "$secret_file" ]]; then
    log "generating new aria2 RPC secret"
    local hex
    hex=$(head -c 24 /dev/urandom | base64 | tr -d '+/=' | head -c 32)
    printf 'ARIA2_RPC_SECRET=%s\n' "$hex" > "$secret_file"
  fi

  # Best-effort group ownership: the prepperpi-admin group is created
  # by the admin service's setup.sh which runs *before* us in
  # SERVICE_ORDER, so this should always succeed. If somehow it
  # doesn't, fall back to root:root and warn — the admin daemon
  # won't be able to read the secret until install ordering is fixed.
  if getent group "$ADMIN_GROUP" >/dev/null; then
    chown "${SERVICE_USER}:${ADMIN_GROUP}" "$secret_file"
    chmod 0640 "$secret_file"
  else
    log "WARNING: ${ADMIN_GROUP} group missing; aria2 RPC secret won't be readable by the admin"
    chown root:root "$secret_file"
    chmod 0600 "$secret_file"
  fi
}

install_files() {
  log "installing scripts and config to ${DST_DIR} and ${CONF_DIR}"
  install -d -m 0755 "$DST_DIR"
  install -m 0755 -o root -g root "${SRC_DIR}/on-complete.sh" "${DST_DIR}/on-complete.sh"
  install -m 0755 -o root -g root "${SRC_DIR}/on-error.sh"    "${DST_DIR}/on-error.sh"
  install -m 0644 "${SRC_DIR}/aria2.conf.tmpl"                "${CONF_DIR}/aria2.conf"

  install -m 0644 "${SRC_DIR}/prepperpi-aria2c.service" \
    /etc/systemd/system/prepperpi-aria2c.service
}

enable_units() {
  log "enabling prepperpi-aria2c.service"
  systemctl daemon-reload
  systemctl enable prepperpi-aria2c.service
}

restart_units() {
  log "restarting prepperpi-aria2c.service"
  systemctl restart prepperpi-aria2c.service
}

main() {
  require_root
  install_packages
  ensure_state_dirs
  ensure_secret
  install_files
  enable_units
  restart_units
  log "done. aria2 RPC daemon is active on 127.0.0.1:6800."
}

main "$@"
