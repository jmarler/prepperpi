#!/usr/bin/env bash
# setup.sh — install prepperpi-admin (FastAPI/uvicorn admin console
# behind Caddy at /admin/). Idempotent. Assumes Debian Trixie or
# later and root.

set -euo pipefail

readonly REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
readonly SRC_DIR="${REPO_DIR}/services/prepperpi-admin"
readonly PREFIX="${PREFIX:-/opt/prepperpi}"
readonly DST_DIR="${PREFIX}/services/prepperpi-admin"
readonly APP_DST="${DST_DIR}/app"
readonly LANDING_DST="${PREFIX}/web/landing"
readonly SUDOERS_DST="/etc/sudoers.d/prepperpi-admin"
readonly ADMIN_USER="prepperpi-admin"
readonly ADMIN_GROUP="prepperpi-admin"

log() { printf '[prepperpi-admin/setup] %s\n' "$*"; }

require_root() {
  if [[ $EUID -ne 0 ]]; then
    echo "setup.sh must be run as root" >&2
    exit 1
  fi
}

install_packages() {
  log "installing apt packages (python3, fastapi, uvicorn, jinja2, python-multipart)"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  # Note: python3-python-multipart is the package FastAPI's Form()
  # depends on (the andrew-d/python-multipart library on PyPI).
  # python3-multipart (no second 'python-') is a DIFFERENT library
  # that happens to share a top-level module name; FastAPI rejects
  # it with "It seems you installed 'multipart' instead".
  apt-get install -y --no-install-recommends \
    python3 \
    python3-fastapi \
    python3-uvicorn \
    python3-jinja2 \
    python3-python-multipart
}

ensure_user() {
  if getent passwd "$ADMIN_USER" >/dev/null; then
    log "user '${ADMIN_USER}' already exists"
  else
    log "creating system user '${ADMIN_USER}'"
    useradd --system \
            --home-dir "$DST_DIR" \
            --no-create-home \
            --shell /usr/sbin/nologin \
            --user-group \
            "$ADMIN_USER"
  fi

  # Add to systemd-journal so the diagnostics tarball endpoint (E4-S2 AC-5)
  # can call `journalctl -u prepperpi-*` without privilege escalation.
  if getent group systemd-journal >/dev/null; then
    if ! id -nG "$ADMIN_USER" | tr ' ' '\n' | grep -qx systemd-journal; then
      log "adding '${ADMIN_USER}' to systemd-journal"
      usermod -aG systemd-journal "$ADMIN_USER"
    fi
  fi
}

install_files() {
  log "installing app + worker to ${DST_DIR}"
  install -d -m 0755 "$DST_DIR" "$APP_DST" "${APP_DST}/templates" "${APP_DST}/static"

  install -m 0644 "${SRC_DIR}/app/main.py"        "${APP_DST}/main.py"
  install -m 0644 "${SRC_DIR}/app/uplink.py"      "${APP_DST}/uplink.py"
  install -m 0644 "${SRC_DIR}/app/health.py"      "${APP_DST}/health.py"
  install -m 0644 "${SRC_DIR}/app/aria2.py"       "${APP_DST}/aria2.py"
  install -m 0644 "${SRC_DIR}/app/catalog.py"     "${APP_DST}/catalog.py"
  install -m 0644 "${SRC_DIR}/app/maps.py"        "${APP_DST}/maps.py"
  install -m 0644 "${SRC_DIR}/app/templates/base.html"    "${APP_DST}/templates/base.html"
  install -m 0644 "${SRC_DIR}/app/templates/home.html"    "${APP_DST}/templates/home.html"
  install -m 0644 "${SRC_DIR}/app/templates/network.html" "${APP_DST}/templates/network.html"
  install -m 0644 "${SRC_DIR}/app/templates/storage.html" "${APP_DST}/templates/storage.html"
  install -m 0644 "${SRC_DIR}/app/templates/catalog.html" "${APP_DST}/templates/catalog.html"
  install -m 0644 "${SRC_DIR}/app/templates/maps.html"    "${APP_DST}/templates/maps.html"
  install -m 0644 "${SRC_DIR}/app/static/admin.css"       "${APP_DST}/static/admin.css"
  install -m 0644 "${SRC_DIR}/app/static/admin.js"        "${APP_DST}/static/admin.js"

  # The privileged workers. Owned root:root, mode 0755 so the admin
  # user can execute via sudo but not modify in place. sudo refuses
  # to run scripts that are writable by anyone other than root.
  install -m 0755 -o root -g root "${SRC_DIR}/apply-network-config" \
                  "${DST_DIR}/apply-network-config"
  install -m 0755 -o root -g root "${SRC_DIR}/apply-storage-action" \
                  "${DST_DIR}/apply-storage-action"

  install -m 0644 "${SRC_DIR}/prepperpi-admin.service" \
                  /etc/systemd/system/prepperpi-admin.service
}

install_sudoers() {
  log "installing sudoers fragment at ${SUDOERS_DST}"
  install -m 0440 -o root -g root "${SRC_DIR}/sudoers.d-prepperpi-admin" "$SUDOERS_DST"
  # Validate syntax. visudo -c on a single fragment exits non-zero
  # if it can't parse, so we catch typos before they break sudo
  # entirely.
  if ! visudo -cf "$SUDOERS_DST" >/dev/null; then
    log "FATAL: sudoers fragment failed visudo -cf; removing"
    rm -f "$SUDOERS_DST"
    exit 1
  fi
}

install_landing_tile() {
  log "publishing admin tile fragment to ${LANDING_DST}/_admin.html"
  install -d -m 0755 "$LANDING_DST"
  install -m 0644 "${SRC_DIR}/_admin.html" "${LANDING_DST}/_admin.html"
}

ensure_catalog_cache_dir() {
  # The catalog cache lives under /srv/prepperpi/cache/. The admin
  # daemon writes to it (E2-S3) — its systemd unit grants
  # ReadWritePaths=/srv/prepperpi/cache. Owned by the admin user so
  # writes don't need root; mode 0755 so other PrepperPi services
  # can read the cache if they ever need to.
  log "ensuring catalog cache dir /srv/prepperpi/cache/"
  install -d -m 0755 -o "$ADMIN_USER" -g "$ADMIN_GROUP" /srv/prepperpi/cache
}

enable_units() {
  log "enabling prepperpi-admin.service"
  systemctl daemon-reload
  systemctl enable prepperpi-admin.service
}

restart_units() {
  # Re-running the installer should leave the service active with the
  # new code, without requiring a reboot or a manual `systemctl restart`.
  log "restarting prepperpi-admin.service"
  systemctl restart prepperpi-admin.service
}

main() {
  require_root
  install_packages
  ensure_user
  install_files
  install_sudoers
  install_landing_tile
  ensure_catalog_cache_dir
  enable_units
  restart_units
  log "done. Admin console is active at /admin/."
}

main "$@"
