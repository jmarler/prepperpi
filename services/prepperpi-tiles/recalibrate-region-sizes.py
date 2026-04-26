#!/usr/bin/env python3
"""recalibrate-region-sizes.py — refresh estimated_bytes in regions.json.

Maintainer-side helper. NOT run at install time. Re-runs every catalog
country at --maxzoom=8 against the live Protomaps planet, captures the
"archive size of N MB" line from pmtiles' own output, then extrapolates
to a z0-15 estimate via a calibration ratio derived from known anchors.

Why z0-8 and not full? A full z0-15 pass for 195 countries pushes
~40 GB through Protomaps' free CDN. z0-8 is roughly 1-2 GB total —
cheap enough to be polite, dense enough that the directory walk
drives a meaningful fraction of the total bytes.

Anchors are measured separately (currently inline below; bump as we
collect more data points). The geometric mean ratio across anchors
becomes the scale factor applied to every country's z0-8 size.

Usage:
    python3 recalibrate-region-sizes.py \\
        --catalog services/prepperpi-tiles/regions.json \\
        --pmtiles-bin /opt/prepperpi/services/prepperpi-tiles/bin/pmtiles \\
        --concurrency 4 \\
        [--source-url https://build.protomaps.com/<DATE>.pmtiles]   # auto-discovered if omitted
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

# Anchor measurements: full z0-15 archive size (in bytes) for countries
# we've extracted end-to-end. These are the source of truth for the
# z0-15:z0-8 scaling ratio. Add more rows as you do real installs.
ANCHORS_Z15: dict[str, int] = {
    "VA": 2_441_541,
    "LI": 10_003_928,
    "BZ": 57_226_469,
    "US": 18_000_000_000,
}

# Output line we parse from pmtiles stdout/stderr:
#   "Extract transferred 10 MB (overfetch 0.05) for an archive size of 10 MB"
# The unit can be B / kB / MB / GB.
ARCHIVE_RE = re.compile(
    r"archive size of\s+([\d.]+)\s*([kMGT]?B)\s*$",
    re.MULTILINE,
)
UNIT = {"B": 1, "kB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3, "TB": 1000 ** 4}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--catalog", required=True, type=Path)
    p.add_argument("--pmtiles-bin", required=True, type=Path)
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--source-url", default=None,
                   help="Planet PMTiles source. Defaults to walking back from "
                        "today's date until a 200 is found.")
    p.add_argument("--maxzoom", type=int, default=8,
                   help="Cap extract zoom for the sampling pass. Default 8 "
                        "keeps each sample to a few MB.")
    p.add_argument("--out", type=Path, default=None,
                   help="Where to write the updated catalog. Defaults to "
                        "overwriting --catalog in place.")
    return p.parse_args()


def discover_source_url() -> str:
    """Walk back from today (UTC) up to 14 days until a 200 is found.

    Protomaps' CDN 403s plain HEADs without a User-Agent, so we send a
    Range-limited GET and read just the first byte instead. Same shape
    as the curl-based check inside extract-region.sh.
    """
    today = dt.datetime.now(dt.timezone.utc).date()
    for offset in range(15):
        d = today - dt.timedelta(days=offset)
        url = f"https://build.protomaps.com/{d:%Y%m%d}.pmtiles"
        req = urllib.request.Request(url, headers={
            "User-Agent": "prepperpi-recalibrate/1",
            "Range": "bytes=0-0",
        })
        try:
            with urllib.request.urlopen(req, timeout=8) as resp:
                if resp.status in (200, 206):
                    return url
        except Exception:
            continue
    raise RuntimeError("no recent Protomaps planet PMTiles found")


def parse_archive_size(stderr: str) -> int | None:
    m = ARCHIVE_RE.search(stderr)
    if not m:
        return None
    return int(round(float(m.group(1)) * UNIT.get(m.group(2), 1)))


def sample_country(country: dict, *, source_url: str, pmtiles_bin: Path,
                   maxzoom: int) -> tuple[str, int | None]:
    """Run pmtiles extract --maxzoom=N for one country, return its z0-N size."""
    rid = country["id"]
    bbox = ",".join(str(b) for b in country["bbox"])
    with tempfile.NamedTemporaryFile(suffix=".pmtiles", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        proc = subprocess.run(
            [str(pmtiles_bin), "extract", source_url, tmp_path,
             "--bbox", bbox, "--maxzoom", str(maxzoom)],
            capture_output=True,
            text=True,
            timeout=600,
        )
    finally:
        try: os.unlink(tmp_path)
        except OSError: pass
    if proc.returncode != 0:
        return rid, None
    size = parse_archive_size(proc.stderr) or parse_archive_size(proc.stdout)
    return rid, size


def compute_scale_ratio(z8_sizes: dict[str, int]) -> float:
    """Geometric mean of z15:z8 ratios for known anchors.

    Geometric (not arithmetic) mean because the ratio varies log-scaled
    across country sizes. Falls back to 50.0 if no anchors landed in
    the sampled set (shouldn't happen unless our small anchors were
    skipped or failed).
    """
    ratios: list[float] = []
    for rid, z15 in ANCHORS_Z15.items():
        z8 = z8_sizes.get(rid)
        if z8 and z8 > 0:
            ratios.append(z15 / z8)
            print(f"  anchor {rid}: z8={z8/1e6:.1f} MB, z15={z15/1e6:.0f} MB, "
                  f"ratio={z15/z8:.1f}x", file=sys.stderr)
    if not ratios:
        print("  WARN: no anchor matched; defaulting to 50x", file=sys.stderr)
        return 50.0
    log_mean = statistics.mean([statistics.log(r) for r in ratios] if False
                                else [__import__("math").log(r) for r in ratios])
    import math
    return math.exp(log_mean)


def main() -> int:
    args = parse_args()
    catalog = json.loads(args.catalog.read_text(encoding="utf-8"))
    countries = catalog.get("countries", [])

    src = args.source_url or discover_source_url()
    print(f"source: {src}", file=sys.stderr)
    print(f"sampling {len(countries)} countries at z0-{args.maxzoom} "
          f"with concurrency={args.concurrency}", file=sys.stderr)

    z8_sizes: dict[str, int | None] = {}
    started = 0
    with cf.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = {
            ex.submit(sample_country, c,
                      source_url=src,
                      pmtiles_bin=args.pmtiles_bin,
                      maxzoom=args.maxzoom): c["id"]
            for c in countries
        }
        for fut in cf.as_completed(futures):
            rid, size = fut.result()
            z8_sizes[rid] = size
            started += 1
            if size is None:
                print(f"  [{started:>3}/{len(countries)}] {rid:<4} FAILED",
                      file=sys.stderr)
            else:
                print(f"  [{started:>3}/{len(countries)}] {rid:<4} z8={size/1e6:.1f} MB",
                      file=sys.stderr)

    ratio = compute_scale_ratio({k: v for k, v in z8_sizes.items() if v})
    print(f"\nz15:z8 scale ratio (geomean across anchors): {ratio:.1f}x",
          file=sys.stderr)

    # Apply ratio. Round up to next 100 MB / 1 GB for tidiness, AND
    # to bias estimates toward overestimation (better UX than
    # under-estimating and failing mid-extract).
    def round_up(b: int) -> int:
        if b >= 5_000_000_000:
            return ((b + 999_999_999) // 1_000_000_000) * 1_000_000_000
        if b >= 500_000_000:
            return ((b + 99_999_999) // 100_000_000) * 100_000_000
        if b >= 50_000_000:
            return ((b + 9_999_999) // 10_000_000) * 10_000_000
        if b >= 5_000_000:
            return ((b + 999_999) // 1_000_000) * 1_000_000
        return max(b, 1_000_000)

    refreshed = 0
    for c in countries:
        rid = c["id"]
        if rid in ANCHORS_Z15:
            new = ANCHORS_Z15[rid]                          # exact measurement
        else:
            z8 = z8_sizes.get(rid)
            if z8 is None:
                continue                                     # leave existing estimate
            new = round_up(int(z8 * ratio))
        if new != c.get("estimated_bytes"):
            refreshed += 1
            c["estimated_bytes"] = new

    out = args.out or args.catalog
    out.write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")
    print(f"\nwrote {out} ({refreshed} estimates updated)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
