"""updates_state — orchestrate a check run and write the snapshot.

Collects installed-state from disk + the cached Kiwix catalog + the
sources cache, runs the pure detectors in `updates.py`, and writes
`/var/lib/prepperpi/updates/state.json` atomically.

Two callers:
  - `prepperpi-updates-check` CLI (run from the systemd service / NM
    dispatcher / timer).
  - The `/admin/updates/check` route (in-process for the "Check now"
    button).

We split the "fetch" pieces from `updates.py` so detection stays pure
and unit-testable.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
from pathlib import Path
from typing import Optional

import bundles as bundles_mod
import updates
from updates import (
    PinStore,
    RegionSidecar,
    StaticInstalled,
    StaticManifestEntry,
    ZimFile,
    count_stale,
    detect_bundle_drift,
    detect_region_drift,
    detect_static_drift,
    detect_zim_drift,
    parse_pins,
    parse_sidecar,
    parse_zim_filename,
)

ZIM_DIR = Path("/srv/prepperpi/zim")
MAPS_DIR = Path("/srv/prepperpi/maps")
STATIC_BASES = {
    "static/":        Path("/srv/prepperpi/static"),
    "zim/static/":    Path("/srv/prepperpi/zim/static"),
    "user-content/":  Path("/srv/prepperpi/user-content"),
}

CATALOG_CACHE = Path("/srv/prepperpi/cache/kiwix-catalog.json")
BUNDLE_CACHE_DIR = Path("/var/lib/prepperpi/bundles")
BUNDLE_BUILTIN_DIR = Path("/opt/prepperpi/bundles/builtin")
BUNDLE_BUILTIN_INDEX = BUNDLE_BUILTIN_DIR / "index.json"
BUNDLE_SOURCES_FILE = Path("/etc/prepperpi/bundles/sources.json")

UPDATES_DIR = Path("/var/lib/prepperpi/updates")
STATE_FILE = UPDATES_DIR / "state.json"
PINS_FILE = UPDATES_DIR / "pins.json"


# ---------- pin I/O ----------


def read_pins() -> PinStore:
    try:
        return parse_pins(PINS_FILE.read_text())
    except OSError:
        return PinStore()


def write_pins(store: PinStore) -> None:
    UPDATES_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PINS_FILE.with_suffix(".json.tmp")
    tmp.write_text(updates.serialize_pins(store))
    os.replace(tmp, PINS_FILE)


# ---------- collectors: what's installed ----------


def collect_installed_zims() -> list[ZimFile]:
    if not ZIM_DIR.exists():
        return []
    out: list[ZimFile] = []
    for path in sorted(ZIM_DIR.glob("*.zim")):
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        out.append(parse_zim_filename(path.name, size_bytes=size))
    return out


def collect_region_sidecars() -> list[RegionSidecar]:
    if not MAPS_DIR.exists():
        return []
    out: list[RegionSidecar] = []
    for path in sorted(MAPS_DIR.glob("*.source.json")):
        try:
            sc = parse_sidecar(path.read_text())
        except OSError:
            sc = None
        if sc is not None:
            out.append(sc)
    return out


def collect_cached_bundle_bodies() -> dict[str, str]:
    """Walk built-in + remote-cache and read every manifest body. Keyed
    by qualified_id (`<source_id>:<bundle_id>`). Built-in manifests are
    returned with `source_id="official"` to align with the resolver."""
    out: dict[str, str] = {}
    # Built-in
    if BUNDLE_BUILTIN_INDEX.exists():
        try:
            _, stubs = bundles_mod.parse_index(BUNDLE_BUILTIN_INDEX.read_text())
            for stub in stubs:
                manifest_path = BUNDLE_BUILTIN_DIR / stub["url"]
                try:
                    body = manifest_path.read_text()
                    out[f"official:{stub['id']}"] = body
                except OSError:
                    continue
        except (bundles_mod.ManifestError, OSError):
            pass
    # Remote per-source caches
    if BUNDLE_CACHE_DIR.exists():
        for src_dir in sorted(BUNDLE_CACHE_DIR.iterdir()):
            if not src_dir.is_dir():
                continue
            idx = src_dir / "index.json"
            if not idx.exists():
                continue
            try:
                _, stubs = bundles_mod.parse_index(idx.read_text())
            except (bundles_mod.ManifestError, OSError):
                continue
            for stub in stubs:
                manifest_path = src_dir / stub["url"]
                try:
                    body = manifest_path.read_text()
                    out[f"{src_dir.name}:{stub['id']}"] = body
                except OSError:
                    continue
    return out


def fetch_remote_bundle_bodies(errors: list[str]) -> dict[str, str]:
    """Re-fetch every enabled source's index + manifests over HTTP. We
    DON'T overwrite the on-disk cache here — the apply path
    (`_refresh_remote_sources`) does that. Detection only needs the
    fresh body in memory to compute hashes."""
    try:
        sources = bundles_mod.parse_sources_config(BUNDLE_SOURCES_FILE.read_text())
    except OSError as exc:
        errors.append(f"sources.json unreadable: {exc}")
        return {}
    out: dict[str, str] = {}
    for src in sources:
        if not src.enabled:
            continue
        try:
            idx_text = bundles_mod.fetch_text(src.url)
        except (urllib.error.URLError, ValueError, OSError) as exc:
            errors.append(f"{src.id}: fetch index — {exc}")
            continue
        try:
            _, stubs = bundles_mod.parse_index(idx_text)
        except bundles_mod.ManifestError as exc:
            errors.append(f"{src.id}: index parse — {exc}")
            continue
        for stub in stubs:
            mu = bundles_mod.resolve_manifest_url(src.url, stub["url"])
            try:
                body = bundles_mod.fetch_text(mu)
            except (urllib.error.URLError, ValueError, OSError) as exc:
                errors.append(f"{src.id}/{stub['id']}: fetch — {exc}")
                continue
            out[f"{src.id}:{stub['id']}"] = body
    return out


def collect_static_manifest_entries(
    bundle_bodies: dict[str, str],
) -> list[StaticManifestEntry]:
    """Walk every cached bundle body and pull out static items."""
    entries: list[StaticManifestEntry] = []
    for qid, body in bundle_bodies.items():
        source_id, _, bundle_id = qid.partition(":")
        try:
            b = bundles_mod.parse_manifest(
                body, source_id=source_id, source_name=source_id
            )
        except bundles_mod.ManifestError:
            continue
        for item in b.items:
            if item.kind != "static":
                continue
            if not (item.install_to and item.url and item.sha256
                    and item.size_bytes is not None):
                continue
            entries.append(StaticManifestEntry(
                install_to=item.install_to,
                expected_sha256=item.sha256,
                expected_size=item.size_bytes,
                url=item.url,
                bundle_qualified_id=qid,
            ))
    return entries


def collect_installed_statics(
    manifest_entries: list[StaticManifestEntry],
) -> list[StaticInstalled]:
    """Hash every static file referenced by *some* manifest. We only
    hash files we'd report on — saves wall-time on the check run."""
    out: list[StaticInstalled] = []
    for entry in manifest_entries:
        path = _resolve_static_install_path(entry.install_to)
        if path is None or not path.is_file():
            continue
        try:
            sha = updates.sha256_file(path)
            size = path.stat().st_size
        except OSError:
            continue
        out.append(StaticInstalled(
            install_to=entry.install_to,
            on_disk_sha256=sha,
            size_bytes=size,
        ))
    return out


def _bundle_titles_from_bodies(bodies: dict[str, str]) -> dict[str, str]:
    """Best-effort extract `name:` from each manifest body so the
    Updates page can label rows with the human-readable bundle name."""
    out: dict[str, str] = {}
    for qid, body in bodies.items():
        source_id, _, _ = qid.partition(":")
        try:
            b = bundles_mod.parse_manifest(
                body, source_id=source_id, source_name=source_id
            )
            out[qid] = b.name
        except bundles_mod.ManifestError:
            continue
    return out


def _resolve_static_install_path(install_to: str) -> Optional[Path]:
    """Map a manifest install_to (`static/foo.pdf`) onto a real on-disk
    path. Mirrors bundles_install._split_install_path but returns the
    full file path."""
    for prefix, base in STATIC_BASES.items():
        if install_to.startswith(prefix):
            rel = install_to[len(prefix):]
            return base / rel
    return None


# ---------- catalog cache reader ----------


def read_catalog_books() -> list[dict]:
    if not CATALOG_CACHE.exists():
        return []
    try:
        data = json.loads(CATALOG_CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return data.get("books") or []


# ---------- snapshot orchestration ----------


def compute_snapshot(*, uplink_ok: bool) -> dict:
    """Run all detectors and return the snapshot dict. Caller writes it
    to disk via `write_snapshot`. Splitting these makes the orchestration
    easier to test from a fixture filesystem layout."""
    errors: list[str] = []
    pins = read_pins()

    # --- ZIMs ---
    installed_zims = collect_installed_zims()
    catalog_books = read_catalog_books()
    if not catalog_books and installed_zims:
        errors.append(
            "Kiwix catalog cache is empty — refresh from the Content "
            "page to enable ZIM update detection."
        )
    zim_items = detect_zim_drift(installed_zims, catalog_books, pins)

    # --- Map regions ---
    sidecars = collect_region_sidecars()
    head_results: dict[str, dict] = {}
    if uplink_ok:
        for sc in sidecars:
            head_results[sc.source_url] = updates.http_head(sc.source_url)
    else:
        # No uplink — mark every region "unknown" by emitting an error.
        for sc in sidecars:
            head_results[sc.source_url] = {"error": "no uplink"}
    region_items = detect_region_drift(sidecars, head_results, pins)

    # --- Bundle definitions (the "manifest" word never reaches users) ---
    cached = collect_cached_bundle_bodies()
    fresh: dict[str, str]
    if uplink_ok:
        fresh = fetch_remote_bundle_bodies(errors)
        # Built-in source: also re-fetch from GitHub if its source URL
        # is the canonical one. fetch_remote_bundle_bodies already walks
        # every enabled source including the built-in entry.
    else:
        fresh = dict(cached)  # no uplink → "available == installed"
    bundle_titles = _bundle_titles_from_bodies({**cached, **fresh})
    bundle_items = detect_bundle_drift(cached, fresh, pins, bundle_titles)

    # --- Static items ---
    static_manifest = collect_static_manifest_entries(cached)
    installed_statics = collect_installed_statics(static_manifest)
    static_items = detect_static_drift(installed_statics, static_manifest, pins)

    items = zim_items + region_items + bundle_items + static_items
    snapshot = {
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "uplink": "ethernet" if uplink_ok else "none",
        "errors": errors,
        "items": items,
        "stale_count": count_stale(items),
    }
    return snapshot


def write_snapshot(snapshot: dict) -> None:
    UPDATES_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(snapshot, indent=2) + "\n")
    os.replace(tmp, STATE_FILE)


def read_snapshot() -> dict:
    """Return the most recent snapshot, or an empty placeholder if none
    has been written yet."""
    if not STATE_FILE.exists():
        return {
            "checked_at": None,
            "uplink": "unknown",
            "errors": [],
            "items": [],
            "stale_count": 0,
        }
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {
            "checked_at": None,
            "uplink": "unknown",
            "errors": [],
            "items": [],
            "stale_count": 0,
        }
