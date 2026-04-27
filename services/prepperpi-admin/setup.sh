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
    python3-python-multipart \
    python3-yaml
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

  # Add to systemd-journal so the diagnostics tarball endpoint can
  # call `journalctl -u prepperpi-*` without privilege escalation.
  if getent group systemd-journal >/dev/null; then
    if ! id -nG "$ADMIN_USER" | tr ' ' '\n' | grep -qx systemd-journal; then
      log "adding '${ADMIN_USER}' to systemd-journal"
      usermod -aG systemd-journal "$ADMIN_USER"
    fi
  fi

  # Add to the `prepperpi` group so admin can unlink ZIMs in the
  # group-writable /srv/prepperpi/zim/ dir (set up by the prepperpi
  # base service). aria2c still creates files as `prepperpi`; admin
  # only needs delete.
  if getent group prepperpi >/dev/null; then
    if ! id -nG "$ADMIN_USER" | tr ' ' '\n' | grep -qx prepperpi; then
      log "adding '${ADMIN_USER}' to prepperpi"
      usermod -aG prepperpi "$ADMIN_USER"
    fi
  fi
}

ensure_zim_dir_groupwrite() {
  # /srv/prepperpi/zim is owned prepperpi:prepperpi 0755 by the base
  # service. Re-mode it 0775 so the prepperpi group (which now
  # includes prepperpi-admin) can unlink ZIMs.
  if [[ -d /srv/prepperpi/zim ]]; then
    log "ensuring /srv/prepperpi/zim is group-writable for the prepperpi group"
    chmod g+w /srv/prepperpi/zim
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
  install -m 0644 "${SRC_DIR}/app/bundles.py"     "${APP_DST}/bundles.py"
  install -m 0644 "${SRC_DIR}/app/bundles_install.py" "${APP_DST}/bundles_install.py"
  install -m 0644 "${SRC_DIR}/app/updates.py"     "${APP_DST}/updates.py"
  install -m 0644 "${SRC_DIR}/app/updates_state.py" "${APP_DST}/updates_state.py"
  install -m 0644 "${SRC_DIR}/app/updates_apply.py" "${APP_DST}/updates_apply.py"
  install -m 0644 "${SRC_DIR}/app/templates/base.html"    "${APP_DST}/templates/base.html"
  install -m 0644 "${SRC_DIR}/app/templates/home.html"    "${APP_DST}/templates/home.html"
  install -m 0644 "${SRC_DIR}/app/templates/network.html" "${APP_DST}/templates/network.html"
  install -m 0644 "${SRC_DIR}/app/templates/storage.html" "${APP_DST}/templates/storage.html"
  install -m 0644 "${SRC_DIR}/app/templates/catalog.html" "${APP_DST}/templates/catalog.html"
  install -m 0644 "${SRC_DIR}/app/templates/maps.html"    "${APP_DST}/templates/maps.html"
  install -m 0644 "${SRC_DIR}/app/templates/bundles.html" "${APP_DST}/templates/bundles.html"
  install -m 0644 "${SRC_DIR}/app/templates/updates.html" "${APP_DST}/templates/updates.html"
  install -m 0644 "${SRC_DIR}/app/templates/backup.html"  "${APP_DST}/templates/backup.html"
  install -m 0644 "${SRC_DIR}/app/static/admin.css"       "${APP_DST}/static/admin.css"
  install -m 0644 "${SRC_DIR}/app/static/admin.js"        "${APP_DST}/static/admin.js"

  # The privileged workers. Owned root:root, mode 0755 so the admin
  # user can execute via sudo but not modify in place. sudo refuses
  # to run scripts that are writable by anyone other than root.
  install -m 0755 -o root -g root "${SRC_DIR}/apply-network-config" \
                  "${DST_DIR}/apply-network-config"
  install -m 0755 -o root -g root "${SRC_DIR}/apply-storage-action" \
                  "${DST_DIR}/apply-storage-action"
  install -m 0755 -o root -g root "${SRC_DIR}/manage-backup" \
                  "${DST_DIR}/manage-backup"

  # Backup helper scripts: the disaster-recovery image creator and the
  # content-tarball restore worker. Both invoked by manage-backup.
  install -m 0755 -o root -g root "${SRC_DIR}/backup-image.sh" \
                  "${DST_DIR}/backup-image.sh"
  install -m 0755 -o root -g root "${SRC_DIR}/restore-content.sh" \
                  "${DST_DIR}/restore-content.sh"

  # First-boot worker that runs ONCE on a freshly-flashed clone:
  # grows the rootfs partition + filesystem to fill the new SD, and
  # regenerates SSH host keys. Idempotent — safe no-op on a system
  # that's already in its target state.
  install -m 0755 -o root -g root "${SRC_DIR}/prepperpi-firstboot.sh" \
                  "${DST_DIR}/prepperpi-firstboot.sh"
  install -m 0644 "${SRC_DIR}/prepperpi-firstboot.service" \
                  /etc/systemd/system/prepperpi-firstboot.service

  # Bundle region drainer — runs as the admin user (no sudo); owned
  # root:root mode 0755 so the admin user can exec but not modify.
  install -m 0755 -o root -g root "${SRC_DIR}/bundle-region-installer.py" \
                  "${DST_DIR}/bundle-region-installer.py"

  # Update-availability checker — invoked by the timer, the
  # NetworkManager dispatcher hook, and the in-process "Check now"
  # button. Owned root:root mode 0755 so admin user can exec.
  install -m 0755 -o root -g root "${SRC_DIR}/prepperpi-updates-check" \
                  "${DST_DIR}/prepperpi-updates-check"

  install -m 0644 "${SRC_DIR}/prepperpi-admin.service" \
                  /etc/systemd/system/prepperpi-admin.service
  install -m 0644 "${SRC_DIR}/prepperpi-updates-check.service" \
                  /etc/systemd/system/prepperpi-updates-check.service
  install -m 0644 "${SRC_DIR}/prepperpi-updates-check.timer" \
                  /etc/systemd/system/prepperpi-updates-check.timer
  install -m 0644 "${SRC_DIR}/prepperpi-updates-check.path" \
                  /etc/systemd/system/prepperpi-updates-check.path

  # NetworkManager dispatcher hook. Fires the update-check service
  # when an interface comes up. Must be owned root:root and executable.
  install -d -m 0755 /etc/NetworkManager/dispatcher.d
  install -m 0755 -o root -g root \
                  "${SRC_DIR}/dispatcher.d-prepperpi-updates" \
                  /etc/NetworkManager/dispatcher.d/90-prepperpi-updates
}

ensure_updates_state_dir() {
  log "ensuring updates state dir /var/lib/prepperpi/updates/"
  install -d -m 0755 -o "$ADMIN_USER" -g "$ADMIN_GROUP" \
                  /var/lib/prepperpi/updates
}

ensure_backup_state_dir() {
  # manage-backup runs as root via sudo; status.json + last-run.log
  # land here. Owned root so non-privileged readers see status but
  # only the worker mutates it.
  log "ensuring backup state dir /var/lib/prepperpi/backup/"
  install -d -m 0755 -o root -g root /var/lib/prepperpi/backup
}

install_bundles() {
  # Builtin bundle manifests baked into the image. The admin daemon
  # always lists these as a fallback when remote sources are
  # unreachable. The admin user owns the staging dir under
  # /var/lib/prepperpi/bundles/ for cached remote-source snapshots.
  log "installing builtin bundle manifests + sources.json"
  install -d -m 0755 "${PREFIX}/bundles" "${PREFIX}/bundles/builtin" \
                     "${PREFIX}/bundles/builtin/manifests"
  install -m 0644 "${REPO_DIR}/bundles/builtin/index.json" \
                  "${PREFIX}/bundles/builtin/index.json"
  install -m 0644 -t "${PREFIX}/bundles/builtin/manifests" \
                  "${REPO_DIR}"/bundles/builtin/manifests/*.yaml

  install -d -m 0755 /etc/prepperpi/bundles
  # Don't clobber an admin-edited sources.json with the shipped default.
  if [[ ! -f /etc/prepperpi/bundles/sources.json ]]; then
    install -m 0644 "${REPO_DIR}/bundles/sources.json" \
                    /etc/prepperpi/bundles/sources.json
  else
    log "/etc/prepperpi/bundles/sources.json already present; preserving local edits"
  fi

  # Cache dir for fetched remote manifests + bundle install state.
  install -d -m 0755 -o "$ADMIN_USER" -g "$ADMIN_GROUP" \
                  /var/lib/prepperpi/bundles
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
  # daemon writes to it — its systemd unit grants
  # ReadWritePaths=/srv/prepperpi/cache. Owned by the admin user so
  # writes don't need root; mode 0755 so other PrepperPi services
  # can read the cache if they ever need to.
  log "ensuring catalog cache dir /srv/prepperpi/cache/"
  install -d -m 0755 -o "$ADMIN_USER" -g "$ADMIN_GROUP" /srv/prepperpi/cache
}

enable_units() {
  log "enabling prepperpi-admin.service + updates timer/path + firstboot service"
  systemctl daemon-reload
  systemctl enable prepperpi-admin.service
  systemctl enable prepperpi-updates-check.timer
  systemctl enable prepperpi-updates-check.path
  # prepperpi-firstboot.service is gated by ConditionFirstBoot=yes, so
  # enabling here on the running appliance is a no-op for this boot. It
  # only matters for clones flashed from images this appliance later
  # produces via backup-image.sh.
  systemctl enable prepperpi-firstboot.service
}

restart_units() {
  # Re-running the installer should leave the service active with the
  # new code, without requiring a reboot or a manual `systemctl restart`.
  log "restarting prepperpi-admin.service"
  systemctl restart prepperpi-admin.service
  log "starting prepperpi-updates-check.timer + .path"
  systemctl start prepperpi-updates-check.timer
  systemctl start prepperpi-updates-check.path
}

main() {
  require_root
  install_packages
  ensure_user
  install_files
  install_sudoers
  install_landing_tile
  ensure_catalog_cache_dir
  ensure_zim_dir_groupwrite
  install_bundles
  ensure_updates_state_dir
  ensure_backup_state_dir
  enable_units
  restart_units
  log "done. Admin console is active at /admin/."
}

main "$@"
