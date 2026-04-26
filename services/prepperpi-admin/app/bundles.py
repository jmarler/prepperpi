"""bundles — content-bundle manifests for one-click installs.

A "bundle" is a YAML manifest listing ZIMs + map regions + static files.
The appliance reads bundles from a list of HTTP(S) source URLs, plus
a builtin set baked into the image so a freshly-flashed offline Pi has
something to install. End-users can add community-managed sources from
the admin console.

This module is pure-ish: parsing and validation are pure (testable from
strings), and the I/O wrappers (`fetch_index`, `fetch_manifest`) are
small adapters around `urllib.request` so the same call sites can be
mocked from tests.

Schema reference: see `docs/creating-bundles.md`.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

SCHEMA_VERSION = 1
ITEM_KINDS = frozenset({"zim", "map_region", "static"})

# `install_to` for static items must land under one of these prefixes.
# Keeps a malicious or buggy manifest from writing to /etc, /boot,
# /opt, etc.
STATIC_INSTALL_ROOTS = frozenset({"static/", "zim/static/", "user-content/"})

DEFAULT_FETCH_TIMEOUT = 30
USER_AGENT = "PrepperPi-Admin/1"


# ---------- dataclasses ----------


@dataclass
class BundleItem:
    kind: str
    # `zim` items
    book_id: Optional[str] = None
    # `map_region` items
    region_id: Optional[str] = None
    # `static` items
    url: Optional[str] = None
    sha256: Optional[str] = None
    size_bytes: Optional[int] = None
    install_to: Optional[str] = None


@dataclass
class Bundle:
    source_id: str          # local id of the source that served this bundle
    source_name: str        # display name of the source
    id: str                 # bundle id within source (e.g. "starter")
    name: str
    description: str
    license_notes: str
    items: list[BundleItem]
    # Resolved at lookup time:
    resolved_size_bytes: int = 0
    resolution_errors: list[str] = field(default_factory=list)
    resolved_items: list[dict] = field(default_factory=list)

    @property
    def qualified_id(self) -> str:
        """Source-namespaced id, e.g. `official:starter`. Stable handle
        for URLs and storage paths."""
        return f"{self.source_id}:{self.id}"


@dataclass
class Source:
    id: str
    url: str
    name: str = ""
    enabled: bool = True
    builtin: bool = False


# ---------- schema validation ----------


class ManifestError(ValueError):
    """Raised when a bundle manifest fails schema validation."""


def parse_manifest(yaml_text: str, *, source_id: str, source_name: str) -> Bundle:
    """Parse + validate a YAML manifest. Raises ManifestError with a
    specific human-readable message on any failure."""
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"YAML parse failed: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError("manifest top level must be a mapping")

    bundle_id = data.get("id")
    if not isinstance(bundle_id, str) or not bundle_id:
        raise ManifestError("`id` is required and must be a non-empty string")
    if not _looks_like_id(bundle_id):
        raise ManifestError(
            "`id` must contain only lowercase letters, digits, and hyphens"
        )
    name = data.get("name")
    if not isinstance(name, str) or not name:
        raise ManifestError("`name` is required and must be a non-empty string")
    description = data.get("description", "")
    if not isinstance(description, str):
        raise ManifestError("`description` must be a string if present")
    license_notes = data.get("license_notes", "")
    if not isinstance(license_notes, str):
        raise ManifestError("`license_notes` must be a string if present")

    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise ManifestError("`items` is required and must be a non-empty list")

    items: list[BundleItem] = []
    for idx, raw in enumerate(raw_items):
        items.append(_parse_item(raw, idx))

    return Bundle(
        source_id=source_id,
        source_name=source_name,
        id=bundle_id,
        name=name,
        description=description.strip(),
        license_notes=license_notes.strip(),
        items=items,
    )


def _parse_item(raw: object, idx: int) -> BundleItem:
    if not isinstance(raw, dict):
        raise ManifestError(f"items[{idx}]: must be a mapping")
    kind = raw.get("kind")
    if kind not in ITEM_KINDS:
        raise ManifestError(
            f"items[{idx}]: kind must be one of {sorted(ITEM_KINDS)}"
        )
    item = BundleItem(kind=kind)

    if kind == "zim":
        book_id = raw.get("book_id")
        if not isinstance(book_id, str) or not book_id:
            raise ManifestError(f"items[{idx}]: zim items require `book_id`")
        item.book_id = book_id
    elif kind == "map_region":
        region_id = raw.get("region_id")
        if not isinstance(region_id, str) or not region_id:
            raise ManifestError(
                f"items[{idx}]: map_region items require `region_id`"
            )
        item.region_id = region_id
    elif kind == "static":
        url = raw.get("url")
        sha256 = raw.get("sha256")
        size_bytes = raw.get("size_bytes")
        install_to = raw.get("install_to")
        if not isinstance(url, str) or not url:
            raise ManifestError(f"items[{idx}]: static items require `url`")
        if not _is_safe_url(url):
            raise ManifestError(
                f"items[{idx}]: url must be http(s) and well-formed"
            )
        if not isinstance(sha256, str) or len(sha256) != 64 or any(
            c not in "0123456789abcdef" for c in sha256
        ):
            raise ManifestError(
                f"items[{idx}]: sha256 must be a 64-character lowercase hex digest"
            )
        if not isinstance(size_bytes, int) or size_bytes <= 0:
            raise ManifestError(
                f"items[{idx}]: size_bytes must be a positive integer"
            )
        if not isinstance(install_to, str) or not _is_safe_install_path(install_to):
            roots = ", ".join(sorted(STATIC_INSTALL_ROOTS))
            raise ManifestError(
                f"items[{idx}]: install_to must start with one of {roots} "
                f"and contain no `..` segments"
            )
        item.url = url
        item.sha256 = sha256
        item.size_bytes = size_bytes
        item.install_to = install_to
    return item


def _looks_like_id(s: str) -> bool:
    return bool(s) and all(c.islower() or c.isdigit() or c == "-" for c in s)


def _is_safe_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _is_safe_install_path(path: str) -> bool:
    if path.startswith("/") or ".." in Path(path).parts:
        return False
    return any(path.startswith(root) for root in STATIC_INSTALL_ROOTS)


# ---------- source / index handling ----------


def parse_sources_config(text: str) -> list[Source]:
    """Parse the JSON-on-disk sources config. Bad entries are skipped
    with a logger warning rather than raising — we don't want a typo
    in one source's URL to block the whole bundles page."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    raw = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return []
    out: list[Source] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        sid = entry.get("id")
        url = entry.get("url")
        if not isinstance(sid, str) or not _looks_like_id(sid):
            continue
        if not isinstance(url, str) or not _is_safe_url(url):
            continue
        out.append(
            Source(
                id=sid,
                url=url,
                name=entry.get("name", "") if isinstance(entry.get("name"), str) else "",
                enabled=bool(entry.get("enabled", True)),
                builtin=bool(entry.get("builtin", False)),
            )
        )
    return out


def parse_index(json_text: str) -> tuple[str, list[dict]]:
    """Parse a source's index.json. Returns (display-name, [manifest stubs]).

    Each manifest stub is `{"id": ..., "url": ...}` where `url` may be
    relative — it gets resolved against the index URL by the caller."""
    try:
        data = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise ManifestError(f"index.json parse failed: {exc}") from exc
    if not isinstance(data, dict):
        raise ManifestError("index.json top level must be an object")
    if data.get("version") != SCHEMA_VERSION:
        raise ManifestError(
            f"index.json version must be {SCHEMA_VERSION}; got {data.get('version')!r}"
        )
    name = data.get("name", "")
    if not isinstance(name, str):
        name = ""
    raw_manifests = data.get("manifests")
    if not isinstance(raw_manifests, list):
        raise ManifestError("index.json `manifests` must be a list")
    out: list[dict] = []
    seen: set[str] = set()
    for entry in raw_manifests:
        if not isinstance(entry, dict):
            continue
        mid = entry.get("id")
        murl = entry.get("url")
        if not isinstance(mid, str) or not _looks_like_id(mid):
            continue
        if mid in seen:
            continue
        if not isinstance(murl, str) or not murl:
            continue
        seen.add(mid)
        out.append({"id": mid, "url": murl})
    return name, out


def resolve_manifest_url(index_url: str, manifest_url: str) -> str:
    """Resolve a (possibly relative) manifest URL against its index URL."""
    return urllib.parse.urljoin(index_url, manifest_url)


# ---------- I/O adapters (unit-tested via mock) ----------


def fetch_text(url: str, timeout: int = DEFAULT_FETCH_TIMEOUT) -> str:
    """GET a URL and return the body as text. Raises urllib.error.URLError
    on transport failure and ValueError on non-2xx responses."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status >= 400:
            raise ValueError(f"{url}: HTTP {resp.status}")
        return resp.read().decode("utf-8", errors="replace")


# ---------- ZIM resolver: book_id → catalog entry ----------


_KIWIX_FILENAME_DATE_RE = re.compile(
    r"^(?P<stem>.+?)_(?P<date>\d{4}-\d{2}(?:-\d{2})?)$"
)


def _filename_stem_no_date(filename: str) -> str:
    """`wikipedia_en_medicine_mini_2026-04.zim` -> `wikipedia_en_medicine_mini`.

    Kiwix's OPDS feed reports the `name` field WITHOUT the flavor
    suffix (mini/nopic/maxi) — three different books share
    `name=wikipedia_en_medicine`. The flavor only lives in the URL /
    filename. To let manifests pin to a specific flavor we match
    against the date-stripped filename stem too."""
    if filename.endswith(".zim"):
        filename = filename[: -len(".zim")]
    m = _KIWIX_FILENAME_DATE_RE.match(filename)
    if m:
        return m.group("stem")
    return filename


def find_kiwix_book(books: list[dict], book_id: str) -> Optional[dict]:
    """Look a bundle's `book_id` up in a Kiwix OPDS catalog entries list.

    Two-strategy match (a manifest's `book_id` may be either):

    1. **Logical name (no flavor)**, e.g. `wikipedia_en_medicine`. We
       match by `name == book_id` or `name.startswith(book_id + "_")`.
       This typically returns multiple flavors (mini / nopic / maxi).
       Whichever has the latest `updated` wins — and ties are broken
       toward the larger payload, since users picking "no flavor"
       generally want the richest variant available.

    2. **Flavor-specific filename stem**, e.g.
       `wikipedia_en_medicine_mini`. We match by the date-stripped
       filename stem; only the mini variant qualifies. Use this in
       manifests when size predictability matters.

    Returns the chosen entry, or None if nothing matched."""
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
        if _filename_stem_no_date(fn) == book_id:
            fname_matches.append(b)

    # Filename hits are preferred when present — they pin a specific
    # flavor; the broad name-prefix match only fires as a fallback.
    chosen_pool = fname_matches if fname_matches else name_matches
    if not chosen_pool:
        return None
    chosen_pool.sort(
        key=lambda b: (b.get("updated") or "", b.get("size_bytes") or 0),
        reverse=True,
    )
    return chosen_pool[0]


# ---------- bundle resolution ----------


def resolve_bundle(
    bundle: Bundle,
    *,
    catalog_books: list[dict],
    region_catalog: dict,
) -> None:
    """Mutate `bundle` in place: fill in resolved_size_bytes,
    resolved_items, and any resolution_errors for items whose backing
    source can't be looked up.

    `catalog_books` is the parsed Kiwix OPDS book list (from
    /srv/prepperpi/cache/kiwix-catalog.json's `books` field).
    `region_catalog` is the parsed region catalog from
    prepperpi-tiles/regions.json.
    """
    # The shipped catalog uses `countries` as the key; older drafts
    # used `regions`. Accept either so manifests are forward-compatible
    # if the catalog format ever changes.
    catalog_entries = (
        region_catalog.get("countries")
        or region_catalog.get("regions")
        or []
    )
    region_lookup: dict[str, dict] = {
        r.get("id", ""): r for r in catalog_entries
    }

    total = 0
    bundle.resolution_errors = []
    bundle.resolved_items = []

    for idx, item in enumerate(bundle.items):
        if item.kind == "zim":
            assert item.book_id is not None
            book = find_kiwix_book(catalog_books, item.book_id)
            if book is None:
                bundle.resolution_errors.append(
                    f"items[{idx}] zim: book_id {item.book_id!r} "
                    f"not found in the Kiwix catalog (refresh the catalog?)"
                )
                continue
            size = int(book.get("size_bytes") or 0)
            total += size
            bundle.resolved_items.append({
                "kind": "zim",
                "book_id": item.book_id,
                "name": book.get("name"),
                "title": book.get("title"),
                "size_bytes": size,
                "url": book.get("url"),
                "updated": book.get("updated"),
            })
        elif item.kind == "map_region":
            assert item.region_id is not None
            region = region_lookup.get(item.region_id)
            if region is None:
                bundle.resolution_errors.append(
                    f"items[{idx}] map_region: region_id {item.region_id!r} "
                    f"not found in the maps catalog"
                )
                continue
            size = int(region.get("estimated_bytes") or 0)
            total += size
            bundle.resolved_items.append({
                "kind": "map_region",
                "region_id": item.region_id,
                "name": region.get("name"),
                "size_bytes": size,
            })
        elif item.kind == "static":
            assert item.size_bytes is not None
            total += item.size_bytes
            bundle.resolved_items.append({
                "kind": "static",
                "url": item.url,
                "sha256": item.sha256,
                "size_bytes": item.size_bytes,
                "install_to": item.install_to,
            })

    bundle.resolved_size_bytes = total
