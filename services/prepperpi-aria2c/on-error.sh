#!/usr/bin/env bash
# on-error.sh — aria2c hook fired when a download fails.
#
# We don't try to clean up partial files here; aria2c's session file
# tracks them for resume, and the .downloading/ staging dir keeps
# them out of the kiwix indexer's path. Just log to journal — the
# admin Catalog page surfaces the failure directly via the aria2
# RPC's tellStopped output, so we don't need to fan it out as a
# dashboard event from this hook.

set -euo pipefail

readonly GID="${1:-?}"
readonly NUM_FILES="${2:-?}"
readonly PATH_ARG="${3:-}"

echo "on-error: gid=${GID} files=${NUM_FILES} path=${PATH_ARG}"
