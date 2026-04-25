#!/usr/bin/env bash
# on-complete.sh — aria2c hook fired when a download finishes.
#
# Args from aria2c: $1=GID, $2=#files, $3=path-to-file (absolute).
#
# We always download to a `.downloading/` subdirectory of the user's
# chosen destination (`/srv/prepperpi/zim/.downloading/` or
# `/srv/prepperpi/user-usb/<vol>/prepperpi-zim/.downloading/`). On
# completion this script moves the file up one directory so the
# existing `prepperpi-kiwix-reindex.path` watcher sees a new ZIM in
# the indexed locations.
#
# Idempotent and safe under partial failures: if the move fails we
# log and exit non-zero so aria2c logs the failure; the file stays
# in `.downloading/` for manual recovery instead of being lost.

set -euo pipefail

readonly SRC="${3:-}"
[[ -n "$SRC" ]] || { echo "on-complete: missing path arg" >&2; exit 1; }
[[ -f "$SRC" ]] || { echo "on-complete: not a file: $SRC" >&2; exit 1; }

readonly STAGING_DIR="$(dirname -- "$SRC")"
readonly STAGING_NAME="$(basename -- "$STAGING_DIR")"

# Only act on files we actually staged. Anything else (a manual aria2c
# invocation that didn't use our staging convention) gets left alone.
if [[ "$STAGING_NAME" != ".downloading" ]]; then
  echo "on-complete: $SRC is not under a .downloading/ dir; leaving in place"
  exit 0
fi

readonly DEST_DIR="$(dirname -- "$STAGING_DIR")"
readonly DEST="${DEST_DIR}/$(basename -- "$SRC")"

# Cross-FS safe: same FS by construction (staging is a child of dest).
mv -f -- "$SRC" "$DEST"

# Drop aria2's control file alongside, if any.
if [[ -f "${SRC}.aria2" ]]; then
  rm -f -- "${SRC}.aria2"
fi

echo "on-complete: moved ${SRC} -> ${DEST}"
