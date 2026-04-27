#!/usr/bin/env bash
# restore-content.sh — extract a backup content tarball into /srv/prepperpi/.
# Run as root.
#
#     sudo restore-content.sh --tar PATH [options]
#
# Options:
#   --skip-sha256       skip verifying the tarball against its sidecar sha256
#                       (default: verify when sidecar exists)
#   --force             overwrite a non-empty /srv/prepperpi/ without prompting
#                       (default: refuse unless empty)
#
# The script verifies the tar against its sidecar (`<tar>.json` next to it,
# carries the sha256 emitted by backup-image.sh), then extracts in place
# preserving permissions / ownership / xattrs.

set -euo pipefail

TAR=""
SKIP_SHA=0
FORCE=0

usage() {
    sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-2}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --tar)         TAR="$2"; shift 2 ;;
        --skip-sha256) SKIP_SHA=1; shift ;;
        --force)       FORCE=1; shift ;;
        -h|--help)     usage 0 ;;
        *) echo "unknown arg: $1" >&2; usage ;;
    esac
done

[ -n "$TAR" ] && [ -f "$TAR" ] || { echo "error: --tar PATH must be an existing file" >&2; usage; }
[ "$(id -u)" -eq 0 ] || { echo "error: must run as root" >&2; exit 2; }

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }

DEST="/srv/prepperpi"
SIDECAR="$TAR.json"

# ---------- pre-flight ----------

# /srv/prepperpi must exist (it always does on a PrepperPi appliance).
[ -d "$DEST" ] || { echo "error: $DEST does not exist on this device" >&2; exit 2; }

# Refuse to clobber a populated /srv/prepperpi without --force.
if [ -z "$(find "$DEST" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
    log "$DEST is empty — proceeding"
else
    if [ "$FORCE" -eq 0 ]; then
        echo "error: $DEST is non-empty — pass --force to overwrite" >&2
        exit 2
    fi
    log "WARNING: $DEST is non-empty — proceeding because --force was passed"
fi

# Verify against sidecar if present.
if [ "$SKIP_SHA" -eq 0 ] && [ -f "$SIDECAR" ]; then
    EXPECTED=$(grep -E '"tar_sha256"' "$SIDECAR" | sed -E 's/.*"tar_sha256"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/' | head -1)
    if [ -n "$EXPECTED" ] && [ "$EXPECTED" != "" ]; then
        log "verifying tar sha256 against sidecar..."
        ACTUAL=$(sha256sum "$TAR" | awk '{ print $1 }')
        if [ "$ACTUAL" != "$EXPECTED" ]; then
            echo "error: sha256 mismatch" >&2
            echo "  expected: $EXPECTED" >&2
            echo "  got:      $ACTUAL"   >&2
            exit 1
        fi
        log "sha256 OK: $ACTUAL"
    else
        log "(sidecar has no tar_sha256 — skipping verification)"
    fi
elif [ "$SKIP_SHA" -eq 0 ]; then
    log "(no sidecar at $SIDECAR — skipping verification)"
fi

# ---------- extract ----------

log "extracting into $DEST"
T0=$(date +%s)
# -p preserves perms; --xattrs / --acls capture extended attrs.
# -C /srv extracts the top-level "prepperpi" dir relative to /srv,
# matching how backup-image.sh produced the archive.
tar -xpf "$TAR" --xattrs --acls -C /srv
T1=$(date +%s)
log "extracted in $(( T1 - T0 ))s"

# ---------- post-extract sanity ----------

# Match ownership the live system expects: prepperpi:prepperpi for content.
PREPPERPI_UID=$(id -u prepperpi 2>/dev/null || echo 999)
PREPPERPI_GID=$(getent group prepperpi 2>/dev/null | cut -d: -f3 || echo 985)
log "fixing ownership (uid=$PREPPERPI_UID gid=$PREPPERPI_GID)"
chown -R "$PREPPERPI_UID:$PREPPERPI_GID" "$DEST"

log "DONE"
