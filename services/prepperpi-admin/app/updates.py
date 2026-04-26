"""updates — detect installed-vs-available drift for ZIMs, map regions,
bundle manifests, and bundle static files.

The pure pieces (parsers, drift comparators) live here so they can be
unit-tested from fixture data without any I/O. The thin I/O wrappers
(`http_head`, `compute_state`) live at the bottom of the file and are
exercised against the real network during a check run.

Drift sources:
  - **ZIM**: installed filename `<book_id>_<date>.zim` vs. the latest
    matching catalog entry (find_kiwix_book gives us "latest by updated"
    matching the same `<book_id>` prefix).
  - **map_region**: each `<region_id>.source.json` sidecar carries the
    ETag + Last-Modified at extract time. We HEAD the source URL and
    compare; either differing means stale.
  - **bundle**: cached manifests under /var/lib/prepperpi/bundles/. We
    sha256 each on-disk manifest body. The "current" handle is the
    cached body's sha; the "available" handle is computed by re-fetching
    the index + manifests during a check (the existing
    _refresh_remote_sources path overwrites the cache atomically — the
    check runs *before* a refresh so we see drift, then the apply path
    runs the refresh).
  - **static** (inside an installed bundle): manifest carries an
    explicit sha256. We sha256 the on-disk file at install_to and
    compare.

Pin model: any item can be pinned to a specific version handle. A
pinned item never appears in the "stale" set; the page surfaces it with
a 📌 badge so the user knows it's intentional.

Snapshot shape (written to /var/lib/prepperpi/updates/state.json):
    {
      "checked_at":   "2026-04-26T12:00:00Z",
      "uplink":       "ethernet" | "none",
      "errors":       [str, ...],
      "items": [
        {
          "kind":           "zim" | "map_region" | "bundle" | "static",
          "id":             "<stable item id>",
          "title":          "<human label>",
          "installed":      "<version handle>",
          "available":      "<version handle>",
          "size_delta_bytes": int | None,
          "available_url":  "<URL or None>",
          "status":         "stale" | "current" | "pinned" | "unknown",
        },
        ...
      ]
    }
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

USER_AGENT = "PrepperPi-Admin/1"
DEFAULT_HEAD_TIMEOUT = 12

# A ZIM filename ends in `_YYYY-MM.zim` or `_YYYY-MM-DD.zim` per Kiwix's
# convention. The book_id is the prefix before that date.
_ZIM_DATE_RE = re.compile(r"^(?P<book_id>.+?)_(?P<date>\d{4}-\d{2}(?:-\d{2})?)$")


# ---------- pure: ZIM filename parsing ----------


@dataclass(frozen=True)
class ZimFile:
    """One ZIM file living on disk."""
    filename: str        # e.g. "wikipedia_en_all_2026-03.zim"
    name_stem: str       # e.g. "wikipedia_en_all_2026-03"
    book_id: str         # e.g. "wikipedia_en_all"
    version: str         # e.g. "2026-03" — empty string if no date suffix
    size_bytes: int = 0


def parse_zim_filename(filename: str, size_bytes: int = 0) -> ZimFile:
    """Strip `.zim`, then split off a trailing `_YYYY-MM(-DD)`. If the
    file doesn't follow that pattern, book_id is the whole stem and
    version is the empty string — the catalog match will then fall back
    to a literal-name comparison."""
    stem = filename[: -len(".zim")] if filename.endswith(".zim") else filename
    m = _ZIM_DATE_RE.match(stem)
    if m:
        return ZimFile(
            filename=filename,
            name_stem=stem,
            book_id=m.group("book_id"),
            version=m.group("date"),
            size_bytes=size_bytes,
        )
    return ZimFile(
        filename=filename,
        name_stem=stem,
        book_id=stem,
        version="",
        size_bytes=size_bytes,
    )


# ---------- pure: pin store ----------


@dataclass
class PinStore:
    """In-memory representation of pins.json. All pin values are
    strings (the version handle for the kind) or `True` for boolean pins
    (used for region kinds where the handle is a compound etag/lm pair
    we just store opaquely).

    Schema:
        {
          "zims":     {"<book_id>": "<pinned_version>"},
          "regions":  {"<region_id>": {"etag": "...", "last_modified": "..."}},
          "bundles":  {"<qualified_id>": "<pinned_sha256>"},
          "statics":  {"<install_to>": "<pinned_sha256>"}
        }
    """
    zims: dict[str, str] = field(default_factory=dict)
    regions: dict[str, dict] = field(default_factory=dict)
    bundles: dict[str, str] = field(default_factory=dict)
    statics: dict[str, str] = field(default_factory=dict)


def parse_pins(text: str) -> PinStore:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return PinStore()
    if not isinstance(data, dict):
        return PinStore()
    out = PinStore()
    z = data.get("zims") or {}
    if isinstance(z, dict):
        for k, v in z.items():
            if isinstance(k, str) and isinstance(v, str):
                out.zims[k] = v
    r = data.get("regions") or {}
    if isinstance(r, dict):
        for k, v in r.items():
            if isinstance(k, str) and isinstance(v, dict):
                out.regions[k] = {
                    "etag": v.get("etag") or "",
                    "last_modified": v.get("last_modified") or "",
                }
    b = data.get("bundles") or {}
    if isinstance(b, dict):
        for k, v in b.items():
            if isinstance(k, str) and isinstance(v, str):
                out.bundles[k] = v
    s = data.get("statics") or {}
    if isinstance(s, dict):
        for k, v in s.items():
            if isinstance(k, str) and isinstance(v, str):
                out.statics[k] = v
    return out


def serialize_pins(store: PinStore) -> str:
    return json.dumps(
        {
            "zims":    store.zims,
            "regions": store.regions,
            "bundles": store.bundles,
            "statics": store.statics,
        },
        indent=2,
        sort_keys=True,
    ) + "\n"


# ---------- pure: ZIM drift ----------


def group_zims_by_book_id(installed: list[ZimFile]) -> dict[str, list[ZimFile]]:
    """Group installed ZIMs by their parsed book_id and sort each group's
    files newest first (by version date — empty version sorts last)."""
    groups: dict[str, list[ZimFile]] = {}
    for zf in installed:
        groups.setdefault(zf.book_id, []).append(zf)
    for files in groups.values():
        files.sort(key=lambda z: z.version, reverse=True)
    return groups


def detect_zim_drift(
    installed: list[ZimFile],
    catalog_books: list[dict],
    pins: PinStore,
) -> list[dict]:
    """One row per book_id (NOT per file). When multiple .zim files
    share a book_id — e.g. the user did a side-by-side update and
    hasn't deleted the old one yet — the row reports drift status for
    the newest installed copy and lists the older copies as
    `older_files` so the UI can offer per-file Delete buttons. Catalog
    entries are dicts as produced by `app.catalog.parse_feed`."""
    groups = group_zims_by_book_id(installed)
    items: list[dict] = []
    for book_id in sorted(groups.keys()):
        files = groups[book_id]
        newest = files[0]
        older = files[1:]
        older_payload = [
            {"filename": f.filename, "version": f.version,
             "size_bytes": f.size_bytes}
            for f in older
        ]

        latest = _find_latest_for_book_id(catalog_books, book_id)
        pinned = pins.zims.get(book_id)

        if latest is None:
            items.append({
                "kind": "zim",
                "id": book_id,
                "title": newest.name_stem,
                "installed": newest.version or newest.name_stem,
                "available": None,
                "size_delta_bytes": None,
                "available_url": None,
                "available_name": None,
                "older_files": older_payload,
                "installed_filename": newest.filename,
                "status": "pinned" if pinned == newest.version else "unknown",
            })
            continue

        # Compare against the date-stripped *filename stem*, not the
        # OPDS `name`. Kiwix's `name` drops the flavor (mini/nopic/maxi)
        # and date — comparing it to an installed file's stem is a
        # category error and produces spurious "stale" rows even when
        # the installed file IS the catalog's latest.
        latest_filename = latest.get("filename") or ""
        if latest_filename.endswith(".zim"):
            latest_filename_stem = latest_filename[: -len(".zim")]
        else:
            latest_filename_stem = latest_filename
        m = _ZIM_DATE_RE.match(latest_filename_stem)
        latest_version = m.group("date") if m else ""

        installed_handle = newest.name_stem
        available_handle = latest_filename_stem or (latest.get("name") or "")
        is_stale = installed_handle != available_handle and bool(available_handle)

        size_delta = None
        if is_stale:
            size_delta = int(latest.get("size_bytes") or 0) - newest.size_bytes

        if pinned == newest.version and is_stale:
            status = "pinned"
        elif is_stale:
            status = "stale"
        else:
            status = "current"

        items.append({
            "kind": "zim",
            "id": book_id,
            "title": latest.get("title") or newest.name_stem,
            "installed": newest.version or installed_handle,
            "available": latest_version or available_handle,
            "size_delta_bytes": size_delta,
            "available_url": latest.get("url") if is_stale else None,
            "available_name": latest_filename_stem if is_stale else None,
            "older_files": older_payload,
            "installed_filename": newest.filename,
            "status": status,
        })
    return items


def _find_latest_for_book_id(books: list[dict], book_id: str) -> Optional[dict]:
    """Mirrors bundles.find_kiwix_book matching rules. Looks for a name
    match first, then falls back to the date-stripped filename stem so
    manifests can pin to a specific Kiwix flavor (mini / nopic / maxi)
    where the OPDS `name` field is shared across flavors. We avoid an
    import from bundles.py here to keep updates.py free of an import
    cycle when bundles.py grows new dependencies."""
    name_matches = [
        b for b in books
        if (b.get("name") or "") == book_id
        or (b.get("name") or "").startswith(book_id + "_")
    ]
    fname_matches: list[dict] = []
    for b in books:
        fn = b.get("filename") or ""
        if not fn:
            continue
        if fn.endswith(".zim"):
            fn = fn[: -len(".zim")]
        m = _ZIM_DATE_RE.match(fn)
        stem = m.group("book_id") if m else fn
        if stem == book_id:
            fname_matches.append(b)
    pool = fname_matches if fname_matches else name_matches
    if not pool:
        return None
    pool.sort(
        key=lambda b: (b.get("updated") or "", b.get("size_bytes") or 0),
        reverse=True,
    )
    return pool[0]


# ---------- pure: map-region drift ----------


@dataclass(frozen=True)
class RegionSidecar:
    """Parsed contents of a single <region>.source.json."""
    region_id: str
    source_url: str
    etag: str
    last_modified: str
    extracted_bytes: int = 0


def parse_sidecar(text: str) -> Optional[RegionSidecar]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    rid = data.get("region_id")
    url = data.get("source_url")
    if not isinstance(rid, str) or not rid or not isinstance(url, str) or not url:
        return None
    return RegionSidecar(
        region_id=rid,
        source_url=url,
        etag=str(data.get("etag") or ""),
        last_modified=str(data.get("last_modified") or ""),
        extracted_bytes=int(data.get("extracted_bytes") or 0),
    )


def detect_region_drift(
    sidecars: list[RegionSidecar],
    head_results: dict[str, dict],
    pins: PinStore,
) -> list[dict]:
    """`head_results` is keyed by source_url and yields
    `{etag, last_modified, content_length, error}` (any field may be empty)."""
    items: list[dict] = []
    for sc in sidecars:
        head = head_results.get(sc.source_url) or {}
        err = head.get("error")
        new_etag = head.get("etag") or ""
        new_lm = head.get("last_modified") or ""

        installed_handle = sc.last_modified or sc.etag or "unknown"
        available_handle = new_lm or new_etag or installed_handle

        is_stale = False
        if not err:
            # Any change in either field means stale. Empty-on-empty
            # counts as no-change (conservative — never spurious).
            if (sc.etag and new_etag and sc.etag != new_etag) \
               or (sc.last_modified and new_lm and sc.last_modified != new_lm):
                is_stale = True

        pin = pins.regions.get(sc.region_id)
        is_pinned = bool(pin) and (
            (pin.get("etag") or "") == sc.etag
            and (pin.get("last_modified") or "") == sc.last_modified
        )

        if err:
            status = "unknown"
        elif is_pinned and is_stale:
            status = "pinned"
        elif is_stale:
            status = "stale"
        else:
            status = "current"

        items.append({
            "kind": "map_region",
            "id": sc.region_id,
            "title": sc.region_id,
            "installed": installed_handle,
            "available": available_handle,
            "size_delta_bytes": None,  # HEAD content-length isn't comparable
            "available_url": sc.source_url if is_stale else None,
            "status": status,
            "error": err,
        })
    return items


# ---------- pure: bundle manifest drift ----------


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def detect_bundle_drift(
    cached_bodies: dict[str, str],
    fresh_bodies: dict[str, str],
    pins: PinStore,
    bundle_titles: Optional[dict[str, str]] = None,
) -> list[dict]:
    """Both dicts are keyed by `<source_id>:<bundle_id>` (qualified_id).

    Only emits rows whose status is something other than `current` —
    a current bundle is just cache hygiene that doesn't need user
    attention. Bundles in `fresh_bodies` but not in `cached_bodies`
    are surfaced as new-available; bundles in `cached_bodies` but not
    in `fresh_bodies` are dropped silently (the source removed them
    upstream — not a notifiable event).

    `bundle_titles` is an optional mapping from qualified_id to the
    bundle's human-readable name (parsed from the manifest). When
    present we use it as the row title so the page can say "Update
    available for Starter bundle" instead of `official:starter`.
    """
    items: list[dict] = []
    titles = bundle_titles or {}
    qualified_ids = set(cached_bodies) | set(fresh_bodies)
    for qid in sorted(qualified_ids):
        cached = cached_bodies.get(qid)
        fresh = fresh_bodies.get(qid)

        installed = sha256_text(cached) if cached else ""
        available = sha256_text(fresh) if fresh else ""

        is_stale = bool(available) and installed != available
        pinned = pins.bundles.get(qid)
        is_pinned = pinned is not None and pinned == installed

        if not available:
            # source no longer offers it; ignore.
            continue
        if is_pinned and is_stale:
            status = "pinned"
        elif is_stale:
            status = "stale"
        else:
            # Current bundles never make it onto the dashboard; the
            # Bundles page's Refresh button is the user-level surface.
            continue

        items.append({
            "kind": "bundle",
            "id": qid,
            "title": titles.get(qid) or qid,
            "installed": installed[:7] if installed else "(new)",
            "available": available[:7],
            "size_delta_bytes": None,
            "available_url": None,
            "status": status,
        })
    return items


# ---------- pure: static-inside-bundle drift ----------


@dataclass(frozen=True)
class StaticInstalled:
    install_to: str
    on_disk_sha256: str
    size_bytes: int


@dataclass(frozen=True)
class StaticManifestEntry:
    install_to: str
    expected_sha256: str
    expected_size: int
    url: str
    bundle_qualified_id: str


def detect_static_drift(
    installed: list[StaticInstalled],
    manifest_entries: list[StaticManifestEntry],
    pins: PinStore,
) -> list[dict]:
    """Drift = on-disk sha256 doesn't match the manifest's expected
    sha256 for the same install_to. Manifest authors bump the sha256
    when they ship a new version; the appliance treats that as
    "available."""
    by_path = {e.install_to: e for e in manifest_entries}
    items: list[dict] = []
    for s in installed:
        entry = by_path.get(s.install_to)
        if entry is None:
            continue
        is_stale = s.on_disk_sha256 and s.on_disk_sha256 != entry.expected_sha256
        pinned = pins.statics.get(s.install_to)
        is_pinned = pinned is not None and pinned == s.on_disk_sha256

        if is_pinned and is_stale:
            status = "pinned"
        elif is_stale:
            status = "stale"
        else:
            status = "current"

        items.append({
            "kind": "static",
            "id": s.install_to,
            "title": s.install_to,
            "installed": (s.on_disk_sha256 or "")[:7] or "(missing-sha)",
            "available": entry.expected_sha256[:7],
            "size_delta_bytes": entry.expected_size - s.size_bytes,
            "available_url": entry.url if is_stale else None,
            "bundle_qualified_id": entry.bundle_qualified_id,
            "status": status,
        })
    return items


# ---------- pure: snapshot count helpers ----------


def count_stale(items: list[dict]) -> int:
    return sum(1 for it in items if it.get("status") == "stale")


# ---------- I/O: HTTP HEAD ----------


def http_head(url: str, timeout: int = DEFAULT_HEAD_TIMEOUT) -> dict:
    """Issue a HEAD request and return etag/last-modified/content-length.
    On any transport error returns `{"error": "..."}` so the caller can
    keep going on a partial result."""
    req = urllib.request.Request(
        url,
        method="HEAD",
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status >= 400:
                return {"error": f"HTTP {resp.status}"}
            etag = resp.headers.get("ETag") or ""
            lm = resp.headers.get("Last-Modified") or ""
            length_raw = resp.headers.get("Content-Length") or "0"
            try:
                length = int(length_raw)
            except ValueError:
                length = 0
            return {
                "etag": etag,
                "last_modified": lm,
                "content_length": length,
            }
    except urllib.error.URLError as exc:
        return {"error": f"{exc}"}
    except (OSError, ValueError) as exc:
        return {"error": f"{exc}"}


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    """Hash a file. Used by the static-drift detector at check time."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            block = fh.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()
