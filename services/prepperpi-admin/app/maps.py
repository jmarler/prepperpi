"""maps — admin helpers for the /admin/maps page (E3-S1).

The reindex service (`prepperpi-tiles-reindex.service`, fired by a path
watcher on /srv/prepperpi/maps) writes a JSON summary of installed
regions to /var/lib/prepperpi/maps/regions.json. The admin console
reads that file for display and never has to open the .mbtiles
directly.

Uninstall (AC-4) is plain unlink: /srv/prepperpi/maps/ is owned by the
admin user, so no privileged worker is needed. The path-watch
regenerates the composite style + landing fragment automatically and
the tileserver restarts on its own.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REGIONS_JSON = Path("/var/lib/prepperpi/maps/regions.json")
MAPS_DIR = Path("/srv/prepperpi/maps")

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
