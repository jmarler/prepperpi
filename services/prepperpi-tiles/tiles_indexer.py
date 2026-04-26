"""tiles_indexer — pure helpers for the MBTiles/PMTiles → composite-style pipeline.

The reindex flow is:

  /srv/prepperpi/maps/{region}.{mbtiles,pmtiles}
        │
        │  read_region_metadata()  ← MBTiles: SQLite metadata table
        │                            PMTiles: 127-byte header + JSON metadata blob
        ▼
  list[Region]
        │
        │  build_tileserver_config(...)   build_composite_style(...)
        ▼                                 ▼
  config.json                       styles/protomaps/style.json
  (tells tileserver-gl-light        (composite style with one source +
   which sources to load)            duplicated layers per region)

Plus:
  render_landing_fragment(regions) -> HTML for the Maps tile.

Everything in this module is a pure function except `read_region_metadata`,
which has been kept narrow (one open + one read) so the orchestrator can
mock or substitute at the I/O boundary. The functions that actually
generate config + style + HTML are pure dict/string transforms — they are
the surface unit-tested in tests/unit/test_tiles_indexer.py.
"""
from __future__ import annotations

import gzip
import json
import sqlite3
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional


# ---------- data model ----------

@dataclass
class Region:
    """One installed map region (MBTiles or PMTiles).

    `region_id` is the basename without the extension (e.g. "north-america").
    It's used both as the tileserver `data` key and as a suffix on every
    style layer + source so a composite of N regions has 1 source and L
    layers per region (L ~= 50 for OSM-Bright).

    `kind` is "mbtiles" or "pmtiles" — drives the data entry shape in
    tileserver-gl-light's config.json.
    """
    region_id: str
    path: Path
    kind: str                                   # "mbtiles" | "pmtiles"
    name: str
    format: str
    minzoom: int
    maxzoom: int
    bounds: tuple[float, float, float, float]   # west, south, east, north
    center: tuple[float, float, float]          # lon, lat, zoom
    attribution: str
    description: str
    size_bytes: int
    vector_layers: list[dict[str, Any]] = field(default_factory=list)


# ---------- I/O boundary (kept narrow on purpose) ----------

def read_region_metadata(path: Path) -> Optional[Region]:
    """Dispatch on file extension to the right reader."""
    suffix = path.suffix.lower()
    if suffix == ".mbtiles":
        return _read_mbtiles_metadata(path)
    if suffix == ".pmtiles":
        return _read_pmtiles_metadata(path)
    return None


def _read_mbtiles_metadata(path: Path) -> Optional[Region]:
    """Open one .mbtiles, read its metadata table, return a Region.

    Returns None if the file isn't a valid MBTiles (no metadata table,
    not SQLite, missing required keys). Callers log + skip in that case.
    """
    try:
        size_bytes = path.stat().st_size
    except OSError:
        return None

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    except sqlite3.Error:
        return None

    try:
        cur = conn.cursor()
        try:
            rows = cur.execute("SELECT name, value FROM metadata").fetchall()
        except sqlite3.Error:
            return None
        meta = {k: v for k, v in rows if k}
    finally:
        conn.close()

    region_id = path.stem
    fmt = meta.get("format", "")
    if fmt not in ("pbf", "png", "jpg", "webp"):
        return None

    bounds = _parse_csv_floats(meta.get("bounds", ""), 4) or (-180.0, -85.0511, 180.0, 85.0511)
    center = _parse_csv_floats(meta.get("center", ""), 3) or (
        (bounds[0] + bounds[2]) / 2.0,
        (bounds[1] + bounds[3]) / 2.0,
        max(0, _to_int(meta.get("minzoom"), 0)),
    )

    vector_layers: list[dict[str, Any]] = []
    json_field = meta.get("json")
    if json_field:
        try:
            parsed = json.loads(json_field)
            vl = parsed.get("vector_layers")
            if isinstance(vl, list):
                vector_layers = vl
        except (ValueError, AttributeError):
            pass

    return Region(
        region_id=region_id,
        path=path,
        kind="mbtiles",
        name=meta.get("name") or region_id,
        format=fmt,
        minzoom=_to_int(meta.get("minzoom"), 0),
        maxzoom=_to_int(meta.get("maxzoom"), 14),
        bounds=bounds,                                  # type: ignore[arg-type]
        center=center,                                  # type: ignore[arg-type]
        attribution=meta.get("attribution") or "",
        description=meta.get("description") or "",
        size_bytes=size_bytes,
        vector_layers=vector_layers,
    )


# PMTiles v3 spec: https://github.com/protomaps/PMTiles/blob/main/spec/v3/spec.md
# The first 127 bytes are a fixed-layout header. After that the file has
# a root directory, optional leaf directories, a JSON metadata blob, and
# the tile data. We only need the header + JSON blob for our purposes.
_PMTILES_MAGIC = b"PMTiles\x03"
_PMTILES_HEADER_LEN = 127

# tile_type enum
_PMTILES_TYPE_TO_FORMAT = {
    1: "pbf",       # MVT (Mapbox Vector Tiles, the OSM-Bright/OpenMapTiles format)
    2: "png",
    3: "jpg",
    4: "webp",
    5: "avif",
}

# internal_compression / tile_compression enum
_PMTILES_NONE = 1
_PMTILES_GZIP = 2
_PMTILES_BROTLI = 3
_PMTILES_ZSTD = 4


def _read_pmtiles_metadata(path: Path) -> Optional[Region]:
    """Parse a .pmtiles file's fixed header + JSON metadata blob.

    Returns None if the magic doesn't match or required fields are
    unreadable. We never read tile data — just the header and the
    metadata blob (typically <10 KB), so this is cheap even for
    multi-GB regional extracts.
    """
    try:
        size_bytes = path.stat().st_size
    except OSError:
        return None

    try:
        with path.open("rb") as f:
            header_bytes = f.read(_PMTILES_HEADER_LEN)
            if len(header_bytes) != _PMTILES_HEADER_LEN:
                return None
            if header_bytes[:8] != _PMTILES_MAGIC:
                return None
            (
                _root_off, _root_len,
                json_meta_off, json_meta_len,
                _leaf_off, _leaf_len,
                _tile_data_off, _tile_data_len,
                _addr_count, _entry_count, _content_count,
                _clustered, internal_compression, _tile_compression, tile_type,
                min_zoom, max_zoom,
                min_lon_e7, min_lat_e7, max_lon_e7, max_lat_e7,
                center_zoom, center_lon_e7, center_lat_e7,
            ) = struct.unpack_from(
                "<QQQQQQQQQQQBBBBBBiiiiBii",
                header_bytes,
                offset=8,
            )

            f.seek(json_meta_off)
            blob = f.read(json_meta_len)
    except (OSError, struct.error):
        return None

    raw = _pmtiles_decompress(blob, internal_compression)
    if raw is None:
        return None
    try:
        meta = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        meta = {}

    fmt = _PMTILES_TYPE_TO_FORMAT.get(tile_type)
    if fmt is None:
        return None

    bounds = (min_lon_e7 / 1e7, min_lat_e7 / 1e7, max_lon_e7 / 1e7, max_lat_e7 / 1e7)
    center = (center_lon_e7 / 1e7, center_lat_e7 / 1e7, float(center_zoom))

    vector_layers = meta.get("vector_layers")
    if not isinstance(vector_layers, list):
        vector_layers = []

    return Region(
        region_id=path.stem,
        path=path,
        kind="pmtiles",
        name=meta.get("name") or path.stem,
        format=fmt,
        minzoom=min_zoom,
        maxzoom=max_zoom,
        bounds=bounds,
        center=center,
        attribution=meta.get("attribution") or "",
        description=meta.get("description") or "",
        size_bytes=size_bytes,
        vector_layers=vector_layers,
    )


def _pmtiles_decompress(data: bytes, compression: int) -> Optional[bytes]:
    """Decompress a PMTiles internal blob per the file's compression enum.

    Brotli/zstd require third-party libraries; we don't pull those into
    the install dependency tree. Almost all real-world PMTiles use gzip
    (or none) for the metadata blob, so unknown variants degrade to
    "skip" rather than crash — the caller treats that as "no metadata"
    and we fall back to header-only fields.
    """
    if compression == _PMTILES_NONE:
        return data
    if compression == _PMTILES_GZIP:
        try:
            return gzip.decompress(data)
        except (OSError, zlib.error):
            return None
    return None


def _parse_csv_floats(raw: str, n: int) -> Optional[tuple[float, ...]]:
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) != n:
        return None
    try:
        return tuple(float(p) for p in parts)
    except ValueError:
        return None


def _to_int(raw: Any, default: int) -> int:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


# ---------- region discovery (filesystem only) ----------

def discover_regions(maps_dir: Path) -> list[Region]:
    """Return all valid MBTiles/PMTiles regions in `maps_dir`, sorted by region_id.

    Invalid or unreadable files are silently skipped — the orchestrator
    is responsible for surfacing those (it walks the same dir and logs
    misses).

    If a region is present in BOTH formats (e.g. `us.mbtiles` and
    `us.pmtiles`), the PMTiles wins — that's the format the region
    downloader produces, so we treat it as the canonical install. Same
    region_id, second copy ignored.
    """
    if not maps_dir.is_dir():
        return []
    by_id: dict[str, Region] = {}
    # Pass 1 — MBTiles. Pass 2 — PMTiles overrides if same region_id.
    for entry in sorted(maps_dir.iterdir()):
        if entry.is_file() and entry.suffix == ".mbtiles":
            r = read_region_metadata(entry)
            if r is not None:
                by_id[r.region_id] = r
    for entry in sorted(maps_dir.iterdir()):
        if entry.is_file() and entry.suffix == ".pmtiles":
            r = read_region_metadata(entry)
            if r is not None:
                by_id[r.region_id] = r
    return sorted(by_id.values(), key=lambda r: r.region_id)


# ---------- tileserver config.json (PURE) ----------

def build_tileserver_config(regions: Iterable[Region], style_id: str = "protomaps") -> dict[str, Any]:
    """Build the config.json that tileserver-gl-light loads at startup.

    `data` keys mirror region_id, with mbtiles paths relative to the
    tileserver's working directory (we'll symlink /srv/prepperpi/maps
    into the tileserver root at install time so the daemon can resolve
    these relative paths under its sandbox).

    Even with zero regions installed, we still emit a valid (empty)
    config so the daemon stays up. The Caddy /maps/ route then renders
    the static "no regions installed" UI.
    """
    region_list = list(regions)
    data: dict[str, dict[str, str]] = {}
    for r in region_list:
        # tileserver-gl-light v5 resolves data values relative to
        # options.paths.mbtiles. We symlink /srv/prepperpi/maps in there at
        # install time, so a bare filename is enough. The key (`mbtiles`
        # vs `pmtiles`) tells the daemon which adapter to load.
        if r.kind == "pmtiles":
            data[r.region_id] = {"pmtiles": f"{r.region_id}.pmtiles"}
        else:
            data[r.region_id] = {"mbtiles": f"{r.region_id}.mbtiles"}

    config: dict[str, Any] = {
        "options": {
            "paths": {
                "root": ".",
                "fonts": "fonts",
                "sprites": "sprites",
                "styles": "styles",
                # Both formats live in the same dir (symlinked to
                # /srv/prepperpi/maps at install time). tileserver-gl-light
                # routes data values keyed `mbtiles:` through paths.mbtiles
                # and `pmtiles:` through paths.pmtiles, so we point both
                # at the same place.
                "mbtiles": "mbtiles",
                "pmtiles": "mbtiles",
            },
        },
        "styles": {},
        "data": data,
    }

    # Only register the style when we have at least one region that can
    # back it. tileserver-gl-light errors out at startup if a style
    # references a `data` source that doesn't exist.
    if region_list:
        config["styles"][style_id] = {
            "style": f"{style_id}/style.json",
        }

    return config


# ---------- composite style.json (PURE) ----------

def build_composite_style(
    template: dict[str, Any],
    regions: Iterable[Region],
    *,
    public_url_prefix: str = "/maps/",
    sprite_id: str = "protomaps",
) -> dict[str, Any]:
    """Build a composite MapLibre style that overlays all installed regions.

    `template` is the upstream OSM-Bright style.json. It assumes a single
    vector source (we don't care what the template called it). We rewrite it to:

      sources:   one per region, keyed `region__<region_id>`
                 (note the double-underscore — single underscores show up
                 in some schemas' source-layer names like `transportation_name`,
                 so we need a separator that won't collide)
      layers:    each original vector-tile-backed layer is duplicated
                 N times, one per region, with `id` suffixed and `source`
                 rebound. Background / non-vector layers are emitted once.
      glyphs:    `<prefix>fonts/{fontstack}/{range}.pbf`  (served by tileserver)
      sprite:    `<prefix>sprites/<sprite_id>`            (served by tileserver)

    Layer ORDER preserves the original list, with each layer's per-region
    copies grouped together. MapLibre draws in array order, so all regions'
    `water` are rendered before any region's `landuse`, etc — the right
    sequence for seamless overlap.

    `bounds` and `center` on the style itself are unioned across all
    regions, so MapLibre's initial view fits everything installed.
    """
    region_list = list(regions)
    out: dict[str, Any] = json.loads(json.dumps(template))   # deep copy

    out["glyphs"] = f"{public_url_prefix}fonts/{{fontstack}}/{{range}}.pbf"
    # tileserver-gl-light v5 resolves the sprite by appending
    # `<style.sprite>.json` to paths.sprites. So a bare id like
    # "osm-bright" maps to <paths.sprites>/osm-bright.json on disk,
    # and the served style's `sprite` URL gets rewritten to
    # <public_url>/styles/<style_id>/sprite. Setup.sh installs the
    # sprite atlas at sprites/<sprite_id>.{json,png} (top-level, not
    # in a subdir) to match.
    out["sprite"] = sprite_id

    if not region_list:
        out["sources"] = {}
        out["layers"] = [l for l in out.get("layers", []) if l.get("type") == "background"]
        return out

    # Sources — one per region.
    new_sources: dict[str, Any] = {}
    for r in region_list:
        src_id = _source_key(r.region_id)
        new_sources[src_id] = {
            "type": "vector",
            "tiles": [
                f"{public_url_prefix}data/{r.region_id}/{{z}}/{{x}}/{{y}}.{r.format}"
            ],
            "minzoom": r.minzoom,
            "maxzoom": r.maxzoom,
            "bounds": list(r.bounds),
            "attribution": r.attribution,
        }
    out["sources"] = new_sources

    # Layers — duplicate each vector-backed layer once per region.
    new_layers: list[dict[str, Any]] = []
    for layer in out.get("layers", []):
        src = layer.get("source")
        ltype = layer.get("type")
        if not src or ltype == "background":
            new_layers.append(layer)
            continue
        for r in region_list:
            copy = json.loads(json.dumps(layer))
            copy["id"] = f"{layer['id']}__{r.region_id}"
            copy["source"] = _source_key(r.region_id)
            new_layers.append(copy)
    out["layers"] = new_layers

    # Union bounds + center across regions.
    minlon = min(r.bounds[0] for r in region_list)
    minlat = min(r.bounds[1] for r in region_list)
    maxlon = max(r.bounds[2] for r in region_list)
    maxlat = max(r.bounds[3] for r in region_list)
    out["center"] = [(minlon + maxlon) / 2.0, (minlat + maxlat) / 2.0]
    out["zoom"] = 2
    # `bounds` on the root style is non-standard but a few clients honor
    # it as the constraint envelope. MapLibre uses `max_bounds`; keep
    # both for compatibility with future tweaks.
    out["max_bounds"] = [minlon, minlat, maxlon, maxlat]

    return out


def _source_key(region_id: str) -> str:
    # OSM-Bright source-layer names use single underscores; use double to
    # keep our region suffix from looking like part of one.
    return f"region__{region_id}"


# ---------- landing-page Maps tile fragment (PURE) ----------

def render_landing_fragment(regions: Iterable[Region]) -> str:
    """Render the `_maps.html` fragment for the captive-portal landing page.

    Returns one of two shapes:
      - "Maps" tile linking to /maps/, with N regions listed (count, total size)
      - "Maps" tile in `tile--unavailable` state when nothing is installed
    """
    region_list = list(regions)
    if not region_list:
        return (
            '<article class="tile tile--unavailable" aria-labelledby="tile-maps-title">\n'
            '  <div class="tile__icon" aria-hidden="true">🗺️</div>\n'
            '  <h2 id="tile-maps-title" class="tile__title">Maps</h2>\n'
            '  <p class="tile__desc">Offline street maps you can pan and zoom.</p>\n'
            '  <p class="tile__status">No regions installed &mdash; open <strong>Admin</strong> to install one.</p>\n'
            '</article>\n'
        )

    total_bytes = sum(r.size_bytes for r in region_list)
    region_count = len(region_list)
    region_word = "region" if region_count == 1 else "regions"
    parts = [
        '<article class="tile tile--maps" aria-labelledby="tile-maps-title">\n',
        '  <div class="tile__icon" aria-hidden="true">🗺️</div>\n',
        '  <h2 id="tile-maps-title" class="tile__title">\n',
        '    <a href="/maps/">Maps</a>\n',
        '  </h2>\n',
        f'  <p class="tile__desc">{region_count} {region_word} &middot; {_human_size(total_bytes)}</p>\n',
        '  <p class="tile__status">',
    ]
    parts.append(", ".join(_html_escape(r.name) for r in region_list))
    parts.append("</p>\n</article>\n")
    return "".join(parts)


def _human_size(n: int) -> str:
    if n >= 1024 ** 3:
        return f"{n / 1024**3:.1f}G"
    if n >= 1024 ** 2:
        return f"{n / 1024**2:.0f}M"
    if n >= 1024:
        return f"{n / 1024:.0f}K"
    return f"{n}B"


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


# ---------- catalog-driven name override (PURE) ----------

def load_catalog_names(catalog_path: Path) -> dict[str, str]:
    """Return {region_id: friendly_name} from the static catalog.

    Missing or unparseable catalog → empty dict; caller treats as
    "no overrides," which keeps the indexer working on systems that
    were installed before the catalog shipped.
    """
    if not catalog_path.exists():
        return {}
    try:
        data = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, str] = {}
    for c in data.get("countries", []):
        if isinstance(c, dict) and c.get("id") and c.get("name"):
            out[c["id"]] = c["name"]
    return out


def apply_name_overrides(regions: list[Region], overrides: dict[str, str]) -> list[Region]:
    """In-place rename regions whose .pmtiles/.mbtiles metadata is generic.

    Heuristic: if the catalog has a friendly name for region.region_id,
    use it. We always prefer the catalog name when available — a
    Protomaps-derived extract embeds the generic name "Protomaps
    Basemap" regardless of what country was extracted, so the file's
    own name field is never useful for an admin-installed region.
    Manual drop-ins (a user copying an OpenMapTiles MBTiles into the
    maps dir) keep their file-embedded name when their region_id
    isn't in the catalog.
    """
    for r in regions:
        friendly = overrides.get(r.region_id)
        if friendly:
            r.name = friendly
    return regions


# ---------- summary for the admin page (PURE) ----------

def regions_summary(regions: Iterable[Region]) -> list[dict[str, Any]]:
    """JSON-serializable list of region info for /admin/maps."""
    return [
        {
            "region_id": r.region_id,
            "name": r.name,
            "kind": r.kind,
            "size_bytes": r.size_bytes,
            "size_human": _human_size(r.size_bytes),
            "minzoom": r.minzoom,
            "maxzoom": r.maxzoom,
            "bounds": list(r.bounds),
            "center": list(r.center),
            "attribution": r.attribution,
            "description": r.description,
        }
        for r in regions
    ]
