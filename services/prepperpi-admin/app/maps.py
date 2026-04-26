"""maps — admin helpers for the /admin/maps page.

Two responsibilities, separated cleanly:

  1. Installed regions. The reindex service writes a JSON summary to
     /var/lib/prepperpi/maps/regions.json; we read that for display
     and unlink files for delete.

  2. Available regions + downloader. A static catalog at
     /opt/prepperpi/services/prepperpi-tiles/regions.json lists ~200
     countries with bbox + bundle membership + estimated size. The
     downloader spawns extract-region.sh as a detached worker which
     writes status to /srv/prepperpi/maps/.status/current.json.
     One install at a time (lock file at .lock).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
from pathlib import Path

REGIONS_JSON = Path("/var/lib/prepperpi/maps/regions.json")
MAPS_DIR = Path("/srv/prepperpi/maps")
CATALOG_JSON = Path("/opt/prepperpi/services/prepperpi-tiles/regions.json")
EXTRACT_SCRIPT = Path("/opt/prepperpi/services/prepperpi-tiles/extract-region.sh")
STATUS_FILE = MAPS_DIR / ".status" / "current.json"
LOCK_FILE = MAPS_DIR / ".lock"
INSTALL_LOG = MAPS_DIR / ".status" / "last-extract.log"

# region_id is the .mbtiles basename (without extension). Any path
# separator, traversal, or odd byte is rejected. We accept the same
# character class kiwix-style ids use, which keeps a future filename-
# based content addressing scheme compatible with this validator.
REGION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def read_regions() -> list[dict]:
    """Return the cached regions list, or [] if the index hasn't run yet.

    Tolerant of partial / corrupted writes: the indexer writes the
    file atomically (tempfile + os.replace), so a torn read is rare,
    but we still ValueError-handle to keep the admin page renderable
    even if something else corrupts the file.
    """
    if not REGIONS_JSON.exists():
        return []
    try:
        data = json.loads(REGIONS_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    # Filter out any entries we wouldn't accept on the way back in.
    return [r for r in data if isinstance(r, dict) and REGION_ID_RE.match(r.get("region_id", ""))]


def delete_region(region_id: str) -> tuple[bool, str]:
    """Remove /srv/prepperpi/maps/<region_id>.mbtiles.

    Returns (ok, message). On success, the path-watcher will fire
    within ~1 second and the reindex unit regenerates the style.
    """
    if not REGION_ID_RE.match(region_id):
        return False, "Invalid region id."
    target = MAPS_DIR / f"{region_id}.mbtiles"
    # Resolve and re-check that the result is still inside MAPS_DIR.
    # Belt-and-braces against any future relaxation of the regex; with
    # the current regex no '..' or '/' can leak through.
    try:
        resolved = target.resolve(strict=False)
        if not str(resolved).startswith(str(MAPS_DIR.resolve()) + "/"):
            return False, "Refusing to delete outside the maps directory."
    except OSError as exc:
        return False, f"Could not resolve {target}: {exc}"

    if not target.exists():
        return False, f"No region named {region_id}."

    try:
        target.unlink()
    except OSError as exc:
        return False, f"Delete failed: {exc}"

    return True, f"Removed {region_id}."


def total_size_bytes(regions: list[dict]) -> int:
    return sum(int(r.get("size_bytes") or 0) for r in regions)


def human_size(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024**3:.1f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024**2:.0f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


# ---------- catalog + downloader ----------

# Free-space safety margin. pmtiles extract writes a .partial file and
# atomically renames at the end; mid-extract the temp file can equal
# the final size, so we need at least 1.2x the estimate to be safe.
FREE_SPACE_HEADROOM = 1.2

# Region IDs in the catalog are short ISO-style codes (US, GB, FR, ME).
# Bundle ids are slugged (na, latam, emea). Same charset, longer max.
CATALOG_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,24}$")


def read_catalog() -> dict:
    """Return the parsed catalog (countries + bundles + source URL).

    Returns {} if the file is missing or unparseable. The catalog is
    static, shipped with the prepperpi-tiles install — failure here
    means the install is broken, not a runtime issue.
    """
    if not CATALOG_JSON.exists():
        return {}
    try:
        data = json.loads(CATALOG_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def enrich_with_catalog_names(regions: list[dict]) -> list[dict]:
    """Overlay catalog display names onto installed-region records.

    The reindex script applies the same overlay before writing
    regions.json, so this is mostly a no-op for current installs. We
    keep it as a defensive safety net: if the catalog gets updated or
    a region is dropped in by hand, the admin page still surfaces the
    friendly name without waiting for the next reindex.

    Always prefers the catalog name when one exists for region_id; the
    metadata baked into Protomaps-derived .pmtiles files is generic
    ("Protomaps Basemap") and never the real country name, so unlike
    the older heuristic we don't try to detect "did the file name
    itself usefully" — we just always trust the catalog.
    """
    catalog = read_catalog()
    by_id: dict[str, dict] = {}
    for c in catalog.get("countries", []):
        if isinstance(c, dict) and c.get("id"):
            by_id[c["id"]] = c
    out: list[dict] = []
    for r in regions:
        rid = r.get("region_id", "")
        catalog_name = (by_id.get(rid) or {}).get("name")
        copy = dict(r)
        if catalog_name:
            copy["name"] = catalog_name
        out.append(copy)
    return out


def installed_region_ids() -> set[str]:
    """Set of region_ids currently on disk under /srv/prepperpi/maps."""
    if not MAPS_DIR.is_dir():
        return set()
    out: set[str] = set()
    for p in MAPS_DIR.iterdir():
        if p.is_file() and p.suffix in (".pmtiles", ".mbtiles"):
            out.add(p.stem)
    return out


def free_space_bytes() -> int:
    """Free bytes on the filesystem hosting /srv/prepperpi/maps."""
    try:
        return shutil.disk_usage(str(MAPS_DIR)).free
    except OSError:
        return 0


def read_install_status() -> dict | None:
    """Return the current install status JSON, or None if no install
    has ever been started.

    The wrapper writes this file atomically; we trust whatever is on
    disk. If the file is unparseable, treat as "no install."
    """
    if not STATUS_FILE.exists():
        return None
    try:
        data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    # Sanity: if status is "extracting" but the PID is dead, surface
    # that as "stalled" so the UI doesn't spin forever waiting for a
    # worker that already crashed.
    if data.get("status") == "extracting":
        pid = data.get("pid")
        if isinstance(pid, int) and pid > 0 and not _pid_alive(pid):
            data = dict(data)
            data["status"] = "stalled"
    return data


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, PermissionError):
        return False


def start_install(region_id: str) -> tuple[bool, str, dict | None]:
    """Spawn extract-region.sh for one region. Returns (ok, message, status).

    Refuses if:
      - region_id isn't in the catalog
      - region is already installed on disk (delete first to refresh)
      - another install is already running (one-at-a-time gate)
      - estimated size + 20% headroom exceeds free space
    """
    if not CATALOG_ID_RE.match(region_id):
        return False, "Invalid region id.", None

    catalog = read_catalog()
    countries = {c["id"]: c for c in catalog.get("countries", []) if isinstance(c, dict)}
    entry = countries.get(region_id)
    if not entry:
        return False, f"No catalog entry for {region_id}.", None

    if region_id in installed_region_ids():
        return False, f"{entry.get('name', region_id)} is already installed. Delete it first to refresh.", None

    current = read_install_status()
    if current and current.get("status") in ("starting", "extracting"):
        # Verify the lock is real, not a stale leftover.
        pid = current.get("pid")
        if isinstance(pid, int) and pid > 0 and _pid_alive(pid):
            return False, f"Another install is in progress: {current.get('region_id')}.", current

    estimated = int(entry.get("estimated_bytes") or 0)
    free = free_space_bytes()
    needed = int(estimated * FREE_SPACE_HEADROOM)
    if estimated > 0 and needed > free:
        return False, (
            f"Not enough free space: estimated {human_size(estimated)} (×{FREE_SPACE_HEADROOM:.1f} headroom = "
            f"{human_size(needed)}), only {human_size(free)} free."
        ), None

    if not EXTRACT_SCRIPT.exists():
        return False, "Region downloader is not installed (extract-region.sh missing).", None

    # Spawn detached so the worker survives an admin daemon restart.
    # stdout/stderr go to a log file; status JSON is the channel the
    # admin polls. close_fds + start_new_session keep us cleanly
    # decoupled from the FastAPI process.
    INSTALL_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_fh = INSTALL_LOG.open("ab")
    try:
        subprocess.Popen(
            [str(EXTRACT_SCRIPT), region_id],
            stdout=log_fh,
            stderr=log_fh,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    finally:
        log_fh.close()
    return True, f"Started extracting {entry.get('name', region_id)}.", read_install_status()


def cancel_install() -> tuple[bool, str]:
    """SIGTERM the running install worker. The wrapper's signal trap
    cleans up the partial file and writes status: cancelled.

    No-op if there's no active install.
    """
    current = read_install_status()
    if not current:
        return False, "No install is running."
    if current.get("status") not in ("starting", "extracting"):
        return False, f"Install is not active (status: {current.get('status')})."
    pid = current.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False, "Install status has no valid PID; cannot cancel."
    if not _pid_alive(pid):
        # Stale entry; clean up the lock so the next install can start.
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass
        return False, "Worker process is already gone."
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return False, f"Could not signal worker: {exc}"
    return True, f"Cancellation signal sent to PID {pid}."


def resolve_bundle(bundle_id: str) -> list[str]:
    """Return the list of country IDs in a bundle.

    Empty list if the bundle id isn't in the catalog. The UI uses this
    to populate the bundle dropdown's "members" preview without
    bouncing through the full catalog.
    """
    catalog = read_catalog()
    for b in catalog.get("bundles", []):
        if isinstance(b, dict) and b.get("id") == bundle_id:
            countries = b.get("countries") or []
            return [c for c in countries if isinstance(c, str)]
    return []
