#!/usr/bin/env bash
# extract-region.sh — invoked by the admin daemon to extract one region
# from the planet PMTiles source (E3-S2). Long-running; runs detached
# (admin spawns with `setsid` / `start_new_session=True` so the worker
# survives an admin restart).
#
# Lifecycle:
#   1. Acquire the install lock (one extract at a time).
#   2. Look up region's bbox + size estimate from catalog (regions.json).
#   3. Write status JSON {status: "starting", ...} so the admin can poll.
#   4. Run `pmtiles extract <source> <tmp> --bbox=<bbox>`. Tail file size
#      to surface progress to the status JSON every second.
#   5. On success: atomic rename .tmp/region.pmtiles.partial →
#      /srv/prepperpi/maps/region.pmtiles. Path watcher fires the
#      reindex; tileserver picks up the new region within ~5s.
#   6. On failure or cancel: remove partial, write status: failed/cancelled.
#
# Status file is at /srv/prepperpi/maps/.status/current.json. It always
# reflects the LAST started job (even after success); the admin's
# polling endpoint reads it directly.
#
# Cancel: send SIGTERM to the wrapper PID. The trap below cleans up the
# partial file and writes status: cancelled before exiting.

set -u

REGION_ID="${1:?region_id required as first arg}"
PMTILES_BIN="${PMTILES_BIN:-/opt/prepperpi/services/prepperpi-tiles/bin/pmtiles}"
CATALOG="${CATALOG:-/opt/prepperpi/services/prepperpi-tiles/regions.json}"
MAPS_DIR="${MAPS_DIR:-/srv/prepperpi/maps}"
STATUS_DIR="${STATUS_DIR:-${MAPS_DIR}/.status}"
TMP_DIR="${TMP_DIR:-${MAPS_DIR}/.tmp}"
LOCK_FILE="${LOCK_FILE:-${MAPS_DIR}/.lock}"
STATUS_FILE="${STATUS_FILE:-${STATUS_DIR}/current.json}"
LOG_FILE="${LOG_FILE:-${STATUS_DIR}/last-extract.log}"

mkdir -p "$STATUS_DIR" "$TMP_DIR"

TMP_FILE="${TMP_DIR}/${REGION_ID}.pmtiles.partial"
FINAL_FILE="${MAPS_DIR}/${REGION_ID}.pmtiles"

# write_status STATUS [progress_bytes]
# Atomic-write the current.json file.
write_status() {
  local status="$1" bytes_so_far="${2:-0}"
  python3 - "$status" "$bytes_so_far" <<PY
import json, os, sys, time
status, bytes_so_far = sys.argv[1], int(sys.argv[2])
out = {
    "region_id": "${REGION_ID}",
    "name": "${REGION_NAME:-${REGION_ID}}",
    "status": status,
    "estimated_bytes": ${EST_BYTES:-0},
    "bytes_so_far": bytes_so_far,
    "pid": ${WORKER_PID:-0},
    "started_at": ${STARTED_AT:-0},
    "updated_at": int(time.time()),
}
tmp = "${STATUS_FILE}.new"
with open(tmp, "w") as f:
    json.dump(out, f, indent=2)
os.replace(tmp, "${STATUS_FILE}")
PY
}

cleanup() {
  local exit_code=$?
  rm -f "$TMP_FILE"
  rm -f "$LOCK_FILE"
  if [[ "$exit_code" -ne 0 && "${WROTE_TERMINAL_STATUS:-no}" != "yes" ]]; then
    write_status "failed" "$(stat -c%s "$TMP_FILE" 2>/dev/null || echo 0)"
  fi
}

handle_signal() {
  WROTE_TERMINAL_STATUS=yes
  write_status "cancelled" "$(stat -c%s "$TMP_FILE" 2>/dev/null || echo 0)"
  rm -f "$TMP_FILE"
  rm -f "$LOCK_FILE"
  exit 130
}

trap cleanup EXIT
trap handle_signal INT TERM

# Acquire the lock. We rely on noclobber + redirect to make it atomic:
# `set -C; > file` fails if file already exists.
set -C
if ! { echo $$ > "$LOCK_FILE"; } 2>/dev/null; then
  echo "[extract-region] another install is in progress (lock at ${LOCK_FILE})" >&2
  exit 2
fi
set +C

# Resolve catalog entry: source URL template + bbox + estimated size + display name.
mapfile -t CATALOG_FIELDS < <(python3 - "$REGION_ID" <<PY
import json, sys
rid = sys.argv[1]
catalog = json.load(open("${CATALOG}"))
for c in catalog["countries"]:
    if c["id"] == rid:
        print(catalog["source_url"])
        print(",".join(str(b) for b in c["bbox"]))
        print(c.get("estimated_bytes", 0))
        print(c.get("name", rid))
        sys.exit(0)
sys.exit(3)
PY
)
if [[ "${#CATALOG_FIELDS[@]}" -lt 4 ]]; then
  echo "[extract-region] no catalog entry for region: ${REGION_ID}" >&2
  exit 3
fi
SOURCE_URL_TEMPLATE="${CATALOG_FIELDS[0]}"
BBOX="${CATALOG_FIELDS[1]}"
EST_BYTES="${CATALOG_FIELDS[2]}"
REGION_NAME="${CATALOG_FIELDS[3]}"

# Resolve the source URL. If the template contains "{date}", we walk
# back from today (UTC) up to 14 days, HEADing each candidate URL until
# one returns 200. Protomaps' build/ retains roughly the last week of
# daily builds; 14 days gives us slack for slow uploads or site outages.
SOURCE_URL=""
if [[ "$SOURCE_URL_TEMPLATE" == *"{date}"* ]]; then
  for offset in 0 1 2 3 4 5 6 7 8 9 10 11 12 13 14; do
    candidate_date=$(date -u -d "${offset} days ago" +%Y%m%d 2>/dev/null \
                  || date -u -v"-${offset}d" +%Y%m%d 2>/dev/null)
    [[ -z "$candidate_date" ]] && continue
    candidate_url="${SOURCE_URL_TEMPLATE//\{date\}/$candidate_date}"
    if curl -sIL --max-time 8 -o /dev/null -w '%{http_code}' "$candidate_url" | grep -q '^200$'; then
      SOURCE_URL="$candidate_url"
      break
    fi
  done
  if [[ -z "$SOURCE_URL" ]]; then
    echo "[extract-region] no recent planet PMTiles build found in the last 14 days" >&2
    exit 6
  fi
else
  SOURCE_URL="$SOURCE_URL_TEMPLATE"
fi

# If the region is already installed, refuse — the caller should delete
# first if they want to refresh. This avoids accidental disk thrash.
if [[ -f "$FINAL_FILE" ]]; then
  echo "[extract-region] ${REGION_ID} already installed at ${FINAL_FILE}" >&2
  WROTE_TERMINAL_STATUS=yes
  write_status "exists"
  exit 4
fi

STARTED_AT=$(date +%s)
WORKER_PID=$$
write_status "starting"

# Run pmtiles extract in the background so we can monitor TMP_FILE size.
# The tool's own progress goes to stderr; we tee it for debugging.
write_status "extracting"
"$PMTILES_BIN" extract "$SOURCE_URL" "$TMP_FILE" --bbox="$BBOX" \
    > "$LOG_FILE" 2>&1 &
EXTRACT_PID=$!

# Poll loop: every second, read partial-file size and update status.
# The tool may overshoot or undershoot the estimate by a fair margin —
# the UI uses bytes_so_far as the truthful "progress so far" value.
while kill -0 "$EXTRACT_PID" 2>/dev/null; do
  sleep 1
  bytes_so_far=$(stat -c%s "$TMP_FILE" 2>/dev/null || echo 0)
  write_status "extracting" "$bytes_so_far"
done

# Reap the extract process and capture its exit code.
wait "$EXTRACT_PID"
EXTRACT_RC=$?

if [[ "$EXTRACT_RC" -ne 0 ]]; then
  echo "[extract-region] pmtiles exited with code ${EXTRACT_RC}; see ${LOG_FILE}" >&2
  WROTE_TERMINAL_STATUS=yes
  write_status "failed" "$(stat -c%s "$TMP_FILE" 2>/dev/null || echo 0)"
  exit "$EXTRACT_RC"
fi

# Verify the partial file is at least 128 bytes (PMTiles header) and
# starts with the magic. The pmtiles tool refuses to write malformed
# output, but a check here means we never publish a broken file.
if [[ ! -s "$TMP_FILE" ]] || ! head -c 8 "$TMP_FILE" | grep -q '^PMTiles'; then
  echo "[extract-region] extracted file failed PMTiles magic check" >&2
  WROTE_TERMINAL_STATUS=yes
  write_status "failed" "$(stat -c%s "$TMP_FILE" 2>/dev/null || echo 0)"
  exit 5
fi

# Atomic publish — the path watcher fires on this rename, the reindex
# regenerates style.json + landing fragment + admin regions.json, and
# the tileserver restarts. ~5s end to end.
mv -f "$TMP_FILE" "$FINAL_FILE"

WROTE_TERMINAL_STATUS=yes
write_status "complete" "$(stat -c%s "$FINAL_FILE" 2>/dev/null || echo 0)"
exit 0
