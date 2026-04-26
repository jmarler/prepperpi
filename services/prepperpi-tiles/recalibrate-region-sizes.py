#!/usr/bin/env python3
"""recalibrate-region-sizes.py — refresh estimated_bytes in regions.json.

Maintainer-side helper. NOT run at install time. Drives `pmtiles
extract` against every country in the catalog and reads the
"archive size of N B" line from the tool's stderr to update
estimated_bytes. Designed to run either on a developer's machine or
on a CI runner with enough bandwidth + disk.

Two modes:

  * Full pass (default; no --maxzoom): extracts each country at its
    full z0-15 zoom range. Captured size IS the new estimate. ~50-150 GB
    of total transfer for the whole catalog; ~30-90 min wall time on a
    decent connection at concurrency=4.

  * Sample pass (--maxzoom=N): caps the extract zoom for a faster,
    cheaper sweep. The captured size is a fraction of the real z0-15
    size; the script geometric-means the z15:zN ratio across known
    anchor countries (ANCHORS_Z15 below) and applies it to scale up.
    Useful for sanity checks but inferior to the full pass.

Resume + audit
==============

The measurements sidecar (--measurements, default
`<catalog-dir>/measurements.json`) is BOTH the resume input and the
durable audit log:

  {
    "schema_version": 1,
    "planet_source_url": "...",
    "started_at": "...",
    "completed_at": null,
    "by_region_id": {
      "VA": {"extracted_bytes": 2441541,
             "transferred_bytes": 2500000,
             "duration_seconds": 3.2,
             "extracted_at": "..."},
      ...
    },
    "failures": [{"region_id": "PT", "error": "...", "attempted_at": "..."}]
  }

A re-run reads any existing measurements file and SKIPS already-measured
countries (use --refresh to force re-measurement). Failures are
re-attempted unless --skip-failed is passed.

CI-friendly output
==================

  --json-progress   emit one JSON line per country to stdout (separate
                    from the human-readable progress on stderr) so a
                    workflow can parse the live state. Lines look like
                    `{"event": "extracted", "region_id": "VA", "bytes": 2441541}`.

Usage
=====

  # full pass on a developer Mac, defaults baked in
  python3 services/prepperpi-tiles/recalibrate-region-sizes.py

  # CI-style invocation; explicit paths, structured progress
  python3 services/prepperpi-tiles/recalibrate-region-sizes.py \\
    --catalog services/prepperpi-tiles/regions.json \\
    --measurements services/prepperpi-tiles/recalibration.json \\
    --temp-dir /var/tmp/pmtiles-recalibrate \\
    --concurrency 6 \\
    --json-progress
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import math
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from typing import Optional


# Anchor measurements: full z0-15 archive size (in bytes) for countries
# we've extracted end-to-end, kept for the --maxzoom sampling mode's
# extrapolation. The full pass doesn't need them.
ANCHORS_Z15: dict[str, int] = {
    "VA": 2_441_541,
    "LI": 10_003_928,
    "BZ": 57_226_469,
    "US": 18_000_000_000,
}

# Output line we parse from pmtiles stdout/stderr:
#   "Extract transferred 10 MB (overfetch 0.05) for an archive size of 10 MB"
ARCHIVE_RE = re.compile(
    r"Extract transferred\s+([\d.]+)\s*([kMGT]?B)[^\n]*archive size of\s+([\d.]+)\s*([kMGT]?B)",
)
UNIT = {"B": 1, "kB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3, "TB": 1000 ** 4}


# ---------- argument parsing ----------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--catalog", type=Path,
                   default=Path(__file__).resolve().parent / "regions.json")
    p.add_argument("--pmtiles-bin", default="pmtiles",
                   help="Path to the pmtiles executable. Defaults to $PATH lookup.")
    p.add_argument("--source-url", default=None,
                   help="Planet PMTiles source. Default: walk back from today's "
                        "date through Protomaps build/ until a 200 is found.")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--maxzoom", type=int, default=None,
                   help="Cap extract zoom for the sampling mode. Omit for full pass.")
    p.add_argument("--countries", default=None,
                   help="Comma-separated ISO codes to limit the run to. Default: all.")
    p.add_argument("--measurements", type=Path, default=None,
                   help="Path to the measurements sidecar (resume + audit). "
                        "Default: <catalog-dir>/measurements.json.")
    p.add_argument("--temp-dir", type=Path, default=None,
                   help="Where to write .pmtiles extracts during measurement. "
                        "Default: a tmpdir that's removed on exit.")
    p.add_argument("--keep-extracts", action="store_true",
                   help="Keep the per-country .pmtiles files after measuring "
                        "(useful for reusing as test fixtures).")
    p.add_argument("--refresh", action="store_true",
                   help="Re-measure even countries already in the measurements file.")
    p.add_argument("--skip-failed", action="store_true",
                   help="Don't retry countries previously recorded as failed.")
    p.add_argument("--retries", type=int, default=2,
                   help="Per-country retry attempts on transient failure.")
    p.add_argument("--json-progress", action="store_true",
                   help="Emit one JSON line per country to stdout, in addition "
                        "to the human-readable progress on stderr.")
    p.add_argument("--output-catalog", type=Path, default=None,
                   help="Where to write the updated catalog. Default: in-place "
                        "overwrite of --catalog.")
    return p.parse_args()


# ---------- planet URL discovery ----------

def discover_source_url() -> str:
    """Walk back from today (UTC) up to 14 days until a 200 is found.

    Protomaps' CDN 403s plain HEADs without a User-Agent, so we send a
    range-limited GET and read the first byte instead.
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


# ---------- pmtiles invocation + parsing ----------

def parse_archive_size(stream: str) -> tuple[Optional[int], Optional[int]]:
    """Pull (transferred_bytes, archive_bytes) out of pmtiles' summary line.

    Returns (None, None) if the regex doesn't match.
    """
    m = ARCHIVE_RE.search(stream)
    if not m:
        return None, None
    transferred = int(round(float(m.group(1)) * UNIT.get(m.group(2), 1)))
    archive     = int(round(float(m.group(3)) * UNIT.get(m.group(4), 1)))
    return transferred, archive


def extract_one(country: dict, *, source_url: str, pmtiles_bin: str,
                temp_dir: Path, maxzoom: Optional[int], keep: bool,
                retries: int) -> dict:
    """Run pmtiles extract for one country. Returns a measurement dict
    on success or a {"error": ...} dict on failure.
    """
    rid = country["id"]
    bbox = ",".join(str(b) for b in country["bbox"])
    out_path = temp_dir / f"{rid}.pmtiles"
    # pmtiles' kong-based flag parser treats a leading-negative value
    # ("-5.5,...") as a NEW flag rather than the previous flag's value.
    # Pass the bbox attached with `=` so it's a single token. Same
    # quirk catches --maxzoom only when its value is negative, which
    # never happens — but the joined form is harmless either way.
    cmd = [pmtiles_bin, "extract", source_url, str(out_path), f"--bbox={bbox}"]
    if maxzoom is not None:
        cmd.append(f"--maxzoom={maxzoom}")

    last_err = ""
    for attempt in range(1, retries + 2):
        # Always start from a clean output path so a partially-extracted
        # file from a prior failed attempt doesn't poison the next try.
        try: out_path.unlink()
        except FileNotFoundError: pass

        started = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3600,  # 1h per country; US takes ~10 min on a fast pipe
            )
        except subprocess.TimeoutExpired as exc:
            last_err = f"timeout after {exc.timeout}s"
            continue

        duration = time.monotonic() - started

        if proc.returncode != 0:
            last_err = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or [f"rc={proc.returncode}"]
            last_err = last_err[0] if last_err else f"rc={proc.returncode}"
            continue

        transferred, archive = parse_archive_size(proc.stderr or proc.stdout)
        if archive is None:
            # Tool succeeded but we couldn't parse the size — fall back
            # to the file size we just wrote.
            try:
                archive = out_path.stat().st_size
            except OSError:
                archive = None

        if not keep:
            try: out_path.unlink()
            except FileNotFoundError: pass

        return {
            "extracted_bytes":   archive,
            "transferred_bytes": transferred,
            "duration_seconds":  round(duration, 1),
            "extracted_at":      dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "attempts":          attempt,
        }

    return {"error": last_err}


# ---------- measurements sidecar ----------

def load_measurements(path: Path, source_url: str) -> dict:
    if not path.exists():
        return {
            "schema_version": 1,
            "planet_source_url": source_url,
            "started_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "completed_at": None,
            "by_region_id": {},
            "failures": [],
        }
    data = json.loads(path.read_text())
    if data.get("schema_version") != 1:
        raise SystemExit(f"unknown measurements schema_version: {data.get('schema_version')}")
    return data


def save_measurements(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".new")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    tmp.replace(path)


# ---------- catalog update ----------

def compute_scale_ratio(z_sizes: dict[str, int]) -> float:
    """Geometric mean of z15:znN ratios for known anchors. Used only by
    sample mode (--maxzoom)."""
    ratios = []
    for rid, z15 in ANCHORS_Z15.items():
        zn = z_sizes.get(rid)
        if zn and zn > 0:
            ratios.append(z15 / zn)
            print(f"  anchor {rid}: zN={zn / 1e6:.1f} MB, z15={z15 / 1e6:.0f} MB, "
                  f"ratio={z15 / zn:.1f}x", file=sys.stderr)
    if not ratios:
        print("  WARN: no anchor matched; defaulting to 50x", file=sys.stderr)
        return 50.0
    return math.exp(statistics.mean([math.log(r) for r in ratios]))


def round_estimate(b: int) -> int:
    """Round each estimate to a clean unit so the catalog reads tidily."""
    if b >= 5_000_000_000:
        return ((b + 999_999_999) // 1_000_000_000) * 1_000_000_000
    if b >= 500_000_000:
        return ((b + 99_999_999) // 100_000_000) * 100_000_000
    if b >= 50_000_000:
        return ((b + 9_999_999) // 10_000_000) * 10_000_000
    if b >= 5_000_000:
        return ((b + 999_999) // 1_000_000) * 1_000_000
    return max(b, 1_000_000)


def update_catalog(catalog: dict, measurements: dict, *,
                   sample_ratio: Optional[float]) -> int:
    """Write `estimated_bytes` for every country we have a measurement for.

    Returns the count of countries whose estimate changed.
    """
    by_region = measurements["by_region_id"]
    changed = 0
    for c in catalog["countries"]:
        rid = c["id"]
        m = by_region.get(rid)
        if not m or m.get("extracted_bytes") is None:
            continue
        if sample_ratio is None:
            new = round_estimate(m["extracted_bytes"])
        else:
            new = round_estimate(int(m["extracted_bytes"] * sample_ratio))
        if new != c.get("estimated_bytes"):
            changed += 1
            c["estimated_bytes"] = new
    return changed


# ---------- main loop ----------

def main() -> int:
    args = parse_args()
    catalog_path: Path = args.catalog
    measurements_path: Path = args.measurements or (catalog_path.parent / "measurements.json")
    output_path: Path = args.output_catalog or catalog_path

    catalog = json.loads(catalog_path.read_text())
    countries = catalog["countries"]
    if args.countries:
        wanted = {c.strip() for c in args.countries.split(",") if c.strip()}
        countries = [c for c in countries if c["id"] in wanted]
        if not countries:
            raise SystemExit(f"no catalog entries match --countries={args.countries}")

    src = args.source_url or discover_source_url()
    measurements = load_measurements(measurements_path, src)

    # If the source URL changed since the last run, the prior measurements
    # are still valid (the planet is daily, but country geographies don't
    # move). We just record the new URL on this run.
    measurements["planet_source_url"] = src
    measurements["started_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    # Decide which countries actually need work.
    by_region = measurements["by_region_id"]
    failed_ids = {f["region_id"] for f in measurements.get("failures", [])}
    todo = []
    for c in countries:
        rid = c["id"]
        if not args.refresh and by_region.get(rid, {}).get("extracted_bytes") is not None:
            continue
        if args.skip_failed and rid in failed_ids:
            continue
        todo.append(c)

    print(f"source: {src}", file=sys.stderr)
    print(f"catalog: {catalog_path}", file=sys.stderr)
    print(f"measurements: {measurements_path}", file=sys.stderr)
    print(f"to do: {len(todo)} of {len(countries)} countries "
          f"(skipped {len(countries) - len(todo)} already measured)",
          file=sys.stderr)
    print(f"concurrency: {args.concurrency}", file=sys.stderr)
    print(f"maxzoom: {args.maxzoom if args.maxzoom is not None else 'full'}",
          file=sys.stderr)

    if not todo:
        print("nothing to do", file=sys.stderr)
        return 0

    # Temp dir for the .pmtiles extracts.
    own_temp = args.temp_dir is None
    temp_dir = args.temp_dir or Path(tempfile.mkdtemp(prefix="pmtiles-recalibrate-"))
    temp_dir.mkdir(parents=True, exist_ok=True)
    print(f"temp dir: {temp_dir} (will be removed at exit: {own_temp})",
          file=sys.stderr)

    failures: list[dict] = list(measurements.get("failures", []))
    failures = [f for f in failures if f["region_id"] not in {c["id"] for c in todo}]

    try:
        with cf.ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            futures = {
                ex.submit(extract_one, c,
                          source_url=src,
                          pmtiles_bin=args.pmtiles_bin,
                          temp_dir=temp_dir,
                          maxzoom=args.maxzoom,
                          keep=args.keep_extracts,
                          retries=args.retries): c
                for c in todo
            }
            done = 0
            for fut in cf.as_completed(futures):
                c = futures[fut]
                rid = c["id"]
                done += 1
                result = fut.result()
                if "error" in result:
                    failures.append({
                        "region_id":  rid,
                        "error":      result["error"],
                        "attempted_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                    })
                    print(f"  [{done:>3}/{len(todo)}] {rid:<4} FAILED: {result['error']}",
                          file=sys.stderr)
                    if args.json_progress:
                        print(json.dumps({"event": "failed", "region_id": rid,
                                          "error": result["error"]}), flush=True)
                else:
                    by_region[rid] = result
                    eb = result["extracted_bytes"]
                    print(f"  [{done:>3}/{len(todo)}] {rid:<4} {eb / 1e6:>9.1f} MB "
                          f"in {result['duration_seconds']:>6.1f}s "
                          f"(attempts={result['attempts']})",
                          file=sys.stderr)
                    if args.json_progress:
                        print(json.dumps({"event": "extracted", "region_id": rid,
                                          "bytes": eb}), flush=True)
                # Save after every country so an interrupted run can resume cleanly.
                measurements["failures"] = failures
                save_measurements(measurements_path, measurements)
    finally:
        if own_temp:
            try: shutil.rmtree(temp_dir)
            except Exception as exc:
                print(f"WARN: failed to clean tempdir {temp_dir}: {exc}",
                      file=sys.stderr)

    measurements["completed_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    measurements["failures"] = failures
    save_measurements(measurements_path, measurements)

    # Compute the scaling ratio (only used in --maxzoom mode) and update
    # the catalog from the measurements.
    sample_ratio = None
    if args.maxzoom is not None:
        sample_ratio = compute_scale_ratio({
            rid: m.get("extracted_bytes") for rid, m in by_region.items()
        })
        print(f"\nz15:z{args.maxzoom} scale ratio (geomean): {sample_ratio:.1f}x",
              file=sys.stderr)

    changed = update_catalog(catalog, measurements, sample_ratio=sample_ratio)
    output_path.write_text(json.dumps(catalog, indent=2) + "\n")
    print(f"\nwrote {output_path} ({changed} estimates updated)", file=sys.stderr)
    print(f"failures: {len(failures)}", file=sys.stderr)
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
