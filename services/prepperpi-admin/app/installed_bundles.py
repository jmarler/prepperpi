"""installed_bundles — tiny registry of bundles the user has installed.

Bundle install was historically stateless: the route resolved a bundle
manifest, queued items into aria2 / the maps queue, and forgot the
intent. Config export needs to know "which bundles did the user pick?",
which can't be reconstructed from on-disk content (partial installs,
manual `rm`, content shared across bundles). So we keep an explicit
registry, written when the install route succeeds.

Schema is intentionally minimal — qualified_id only. Bundle names,
versions, and item lists get re-resolved from the manifest cache at
read time, so an old export still imports cleanly even if a bundle's
manifest body changed in the interim.

Storage lives next to the bundle manifest cache:
  /var/lib/prepperpi/bundles/installed.json
"""
from __future__ import annotations

import json
import os
from pathlib import Path

INSTALLED_FILE = Path("/var/lib/prepperpi/bundles/installed.json")


def read_installed() -> list[str]:
    """Return the list of qualified_ids the user has installed. Empty
    list if the file doesn't exist or is malformed — both are fine,
    "no recorded installs" is a valid state."""
    if not INSTALLED_FILE.exists():
        return []
    try:
        data = json.loads(INSTALLED_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict):
        return []
    bundles = data.get("bundles")
    if not isinstance(bundles, list):
        return []
    return [b for b in bundles if isinstance(b, str)]


def _write(qids: list[str]) -> None:
    INSTALLED_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = INSTALLED_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"bundles": qids}, indent=2) + "\n")
    os.replace(tmp, INSTALLED_FILE)


def record_installed(qualified_id: str) -> None:
    """Mark `qualified_id` as installed. Idempotent — re-recording the
    same id is a no-op."""
    cur = read_installed()
    if qualified_id in cur:
        return
    cur.append(qualified_id)
    _write(cur)


def replace_all(qualified_ids: list[str]) -> None:
    """Authoritative replace — used by config import.

    Dedupes while preserving first-seen order so the imported list is
    canonicalized but the user's intended ordering survives."""
    seen: set[str] = set()
    out: list[str] = []
    for q in qualified_ids:
        if not isinstance(q, str) or q in seen:
            continue
        seen.add(q)
        out.append(q)
    _write(out)
