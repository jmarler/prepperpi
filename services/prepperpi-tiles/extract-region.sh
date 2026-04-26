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

# write_status STATUS [bytes_so_far] [bytes_total] [phase] [eta_seconds]
# Atomic-write the current.json file. Optional fields are filled with
# defaults; bytes_total falls back to the catalog estimate so the UI
# always has something to render the bar against.
write_status() {
  local status="$1"
  local bytes_so_far="${2:-0}"
  local bytes_total="${3:-0}"
  local phase="${4:-}"
  local eta_seconds="${5:-0}"
  python3 - "$status" "$bytes_so_far" "$bytes_total" "$phase" "$eta_seconds" <<PY
import json, os, sys, time
status      = sys.argv[1]
bytes_done  = int(sys.argv[2])
bytes_total = int(sys.argv[3])
phase       = sys.argv[4]
eta_seconds = int(sys.argv[5])
out = {
    "region_id": "${REGION_ID}",
    "name": "${REGION_NAME:-${REGION_ID}}",
    "status": status,
    "estimated_bytes": ${EST_BYTES:-0},
    "bytes_so_far": bytes_done,
    "bytes_total": bytes_total or ${EST_BYTES:-0},
    "phase": phase,
    "eta_seconds": eta_seconds,
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

# Parse pmtiles' own progress output from the log file. Returns
# `bytes_so_far|bytes_total|phase|eta_seconds` (pipe-separated) on
# stdout. Empty fields when not available.
#
# pmtiles emits to stderr (we tee to LOG_FILE):
#   "fetching N dirs, ..."                          ← directory phase
#   "Region tiles X, result tile entries Y"        ← end of planning
#   "fetching N tiles, N chunks, N requests"       ← about to download
#   "fetching chunks 29% |...| (5.3/18 GB, 36 MB/s) [2m34s:5m40s]"  ← live
#   "Completed in Xs ..."                          ← done
#
# Progress lines use carriage returns (in-place updates), so we
# normalize CR → LF before grep.
parse_pmtiles_progress() {
  [[ -s "$LOG_FILE" ]] || { echo "0|0||0"; return; }
  python3 - "$LOG_FILE" <<'PY'
import re, sys
log = open(sys.argv[1], "r", errors="replace").read().replace("\r", "\n")

unit = {"B": 1, "kB": 1000, "MB": 1000**2, "GB": 1000**3, "TB": 1000**4}
def b(num, u): return int(round(float(num) * unit.get(u, 1)))

bytes_so_far = bytes_total = eta = 0
phase = ""

# Find the LAST "fetching chunks ..." line — that's the live progress.
# pmtiles formats the in-flight numbers two ways depending on units:
#   "(1020 MB/18 GB, ...)"   when done and total have different units
#   "(9.6/18 GB, ...)"       when both share the same unit (compact)
# So the first unit is optional; when missing it's implicitly the
# second unit's value.
m = None
for hit in re.finditer(
    r"fetching chunks\s+(\d+)%\s*\|[^|]*\|\s*\(([\d.]+)(?:\s*([kMGT]?B))?\s*/\s*([\d.]+)\s*([kMGT]?B)[^)]*\)\s*\[(?:[^:]+):([^\]]+)\]",
    log,
):
    m = hit
if m:
    pct = int(m.group(1))
    first_unit = m.group(3) or m.group(5)   # fallback to total's unit
    bytes_so_far = b(m.group(2), first_unit)
    bytes_total  = b(m.group(4), m.group(5))
    eta_str = m.group(6).strip()
    # ETA is e.g. "5m40s", "32s", "1h2m". Convert to seconds.
    secs = 0
    for n, u in re.findall(r"(\d+)([hms])", eta_str):
        secs += int(n) * {"h": 3600, "m": 60, "s": 1}[u]
    eta = secs
    phase = "downloading"
elif re.search(r"Completed in", log):
    phase = "verifying"
elif re.search(r"fetching \d+ tiles, \d+ chunks", log):
    phase = "downloading"
elif re.search(r"Region tiles \d+", log):
    phase = "planning"
elif re.search(r"fetching \d+ dirs", log):
    phase = "planning"

print(f"{bytes_so_far}|{bytes_total}|{phase}|{eta}")
PY
}

cleanup() {
  local exit_code=$?
  # Only clean up resources WE acquired. If we exited because the lock
  # was already held by another install, removing $LOCK_FILE here would
  # silently free the OTHER install's lock and let a third install
  # race in — exactly the bug that bit the in-flight US extract earlier.
  if [[ "${LOCK_ACQUIRED:-no}" == "yes" ]]; then
    rm -f "$TMP_FILE"
    rm -f "$LOCK_FILE"
    if [[ "$exit_code" -ne 0 && "${WROTE_TERMINAL_STATUS:-no}" != "yes" ]]; then
      write_status "failed" 0 0 "" 0
    fi
  fi
}

handle_signal() {
  WROTE_TERMINAL_STATUS=yes
  IFS='|' read -r bytes_so_far bytes_total _ _ < <(parse_pmtiles_progress)
  write_status "cancelled" "${bytes_so_far:-0}" "${bytes_total:-0}" "" 0
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
LOCK_ACQUIRED=yes  # cleanup() only frees the lock when we set this

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

# Poll loop: every second, parse pmtiles' own progress from the log
# (NOT the partial file size — pmtiles preallocates the full target
# size on disk, so stat -c%s is constant throughout the download).
# Falls back to a "planning" phase status while pmtiles is still
# walking the directory tree before any chunk fetch starts.
while kill -0 "$EXTRACT_PID" 2>/dev/null; do
  sleep 1
  IFS='|' read -r bytes_so_far bytes_total phase eta_seconds < <(parse_pmtiles_progress)
  write_status "extracting" "${bytes_so_far:-0}" "${bytes_total:-0}" "${phase:-}" "${eta_seconds:-0}"
done

# Reap the extract process and capture its exit code.
wait "$EXTRACT_PID"
EXTRACT_RC=$?

if [[ "$EXTRACT_RC" -ne 0 ]]; then
  echo "[extract-region] pmtiles exited with code ${EXTRACT_RC}; see ${LOG_FILE}" >&2
  WROTE_TERMINAL_STATUS=yes
  IFS='|' read -r bytes_so_far bytes_total _ _ < <(parse_pmtiles_progress)
  write_status "failed" "${bytes_so_far:-0}" "${bytes_total:-0}" "" "0"
  exit "$EXTRACT_RC"
fi

# Verify the partial file is at least 128 bytes (PMTiles header) and
# starts with the magic. The pmtiles tool refuses to write malformed
# output, but a check here means we never publish a broken file.
if [[ ! -s "$TMP_FILE" ]] || ! head -c 8 "$TMP_FILE" | grep -q '^PMTiles'; then
  echo "[extract-region] extracted file failed PMTiles magic check" >&2
  WROTE_TERMINAL_STATUS=yes
  write_status "failed" 0 0 "" 0
  exit 5
fi

# Atomic publish — the path watcher fires on this rename, the reindex
# regenerates style.json + landing fragment + admin regions.json, and
# the tileserver restarts. ~5s end to end.
mv -f "$TMP_FILE" "$FINAL_FILE"

final_size=$(stat -c%s "$FINAL_FILE" 2>/dev/null || echo 0)
WROTE_TERMINAL_STATUS=yes
write_status "complete" "$final_size" "$final_size" "" 0
exit 0
