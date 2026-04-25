"""Unit tests for services/prepperpi-tiles/tiles_indexer.py.

Covers the I/O boundary + pure transforms for both MBTiles (E3-S1) and
PMTiles (E3-S2):

  read_region_metadata    — dispatches on extension; opens SQLite or PMTiles
  discover_regions        — walks a tmp dir; PMTiles wins on dup region_id
  build_tileserver_config — emits {mbtiles: ...} or {pmtiles: ...} per kind
  build_composite_style   — pure dict-out (layer multiplication)
  render_landing_fragment — pure HTML-out
  regions_summary         — pure list-out, includes kind

Pure stdlib. No network. Both file fixtures are built in-process so the
suite stays self-contained.
"""
from __future__ import annotations

import gzip
import json
import sqlite3
import struct
import sys
import tempfile
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_DIR / "services" / "prepperpi-tiles"))

from tiles_indexer import (  # noqa: E402
    Region,
    build_composite_style,
    build_tileserver_config,
    discover_regions,
    read_region_metadata,
    regions_summary,
    render_landing_fragment,
)


# ---------- helpers ----------

def make_pmtiles(path: Path, *, name: str = "PMTiles fixture",
                 minzoom: int = 0, maxzoom: int = 14,
                 bounds_e7: tuple[int, int, int, int] = (-1800000000, -850511287, 1800000000, 850511287),
                 center: tuple[int, int, int] = (0, 0, 2),
                 tile_type: int = 1,
                 internal_compression: int = 2,    # gzip
                 attribution: str = "© test",
                 description: str = "",
                 vector_layers: list[dict] | None = None) -> None:
    """Synthesize a minimal PMTiles v3 file: magic + 127-byte header + JSON metadata.

    No tile data — we only test header/metadata reading. The default
    compression is gzip to exercise the inflate path; pass compression=1
    (none) to test the uncompressed branch.
    """
    if path.exists():
        path.unlink()

    meta = {
        "name": name,
        "attribution": attribution,
        "description": description,
        "vector_layers": vector_layers or [],
    }
    meta_bytes = json.dumps(meta).encode("utf-8")
    if internal_compression == 2:        # gzip
        meta_blob = gzip.compress(meta_bytes)
    else:
        meta_blob = meta_bytes

    json_meta_offset = 127               # write JSON immediately after header
    json_meta_length = len(meta_blob)

    header = bytearray(127)
    header[0:8] = b"PMTiles\x03"
    struct.pack_into(
        "<QQQQQQQQQQQBBBBBBiiiiBii",
        header,
        8,
        0, 0,                          # root dir off, len
        json_meta_offset, json_meta_length,
        0, 0,                          # leaf dirs off, len
        json_meta_offset + json_meta_length, 0,
        0, 0, 0,                       # addressed/entry/content counts
        1,                             # clustered
        internal_compression,          # internal_compression
        internal_compression,          # tile_compression (irrelevant for our reader)
        tile_type,
        minzoom, maxzoom,
        bounds_e7[0], bounds_e7[1], bounds_e7[2], bounds_e7[3],
        center[2],
        center[0], center[1],
    )
    with path.open("wb") as f:
        f.write(bytes(header))
        f.write(meta_blob)


def make_mbtiles(path: Path, *, name: str = "Test region",
                 fmt: str = "pbf",
                 minzoom: int = 0, maxzoom: int = 14,
                 bounds: str = "-180,-85.0511,180,85.0511",
                 center: str = "0,0,2",
                 attribution: str = "© Test contributors",
                 description: str = "",
                 vector_layers: list[dict] | None = None) -> None:
    """Create a minimal MBTiles SQLite file with a metadata table."""
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
        meta = {
            "name": name,
            "format": fmt,
            "minzoom": str(minzoom),
            "maxzoom": str(maxzoom),
            "bounds": bounds,
            "center": center,
            "attribution": attribution,
            "description": description,
        }
        if vector_layers is not None:
            meta["json"] = json.dumps({"vector_layers": vector_layers})
        for k, v in meta.items():
            conn.execute("INSERT INTO metadata (name, value) VALUES (?, ?)", (k, v))
        conn.commit()
    finally:
        conn.close()


# Minimal OSM-Bright-style template: background + two vector-backed
# layers, all referencing the upstream `openmaptiles` source.
TEMPLATE = {
    "version": 8,
    "name": "OSM Bright",
    "sources": {
        "openmaptiles": {"type": "vector", "url": "mbtiles://{openmaptiles}"}
    },
    "glyphs": "https://example.com/{fontstack}/{range}.pbf",
    "sprite": "https://example.com/sprite",
    "layers": [
        {"id": "background", "type": "background", "paint": {"background-color": "#fff"}},
        {"id": "water",      "type": "fill",       "source": "openmaptiles", "source-layer": "water"},
        {"id": "road",       "type": "line",       "source": "openmaptiles", "source-layer": "transportation"},
    ],
}


# ---------- read_region_metadata ----------

class TestReadRegionMetadata(unittest.TestCase):
    def test_happy_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "north-america.mbtiles"
            make_mbtiles(path,
                         name="North America",
                         minzoom=0, maxzoom=12,
                         bounds="-170,15,-50,72",
                         center="-100,40,3",
                         vector_layers=[{"id": "water", "fields": {}}])
            r = read_region_metadata(path)

            self.assertIsNotNone(r)
            assert r is not None
            self.assertEqual(r.region_id, "north-america")
            self.assertEqual(r.name, "North America")
            self.assertEqual(r.format, "pbf")
            self.assertEqual(r.minzoom, 0)
            self.assertEqual(r.maxzoom, 12)
            self.assertEqual(r.bounds, (-170.0, 15.0, -50.0, 72.0))
            self.assertEqual(r.center, (-100.0, 40.0, 3.0))
            self.assertEqual(r.vector_layers, [{"id": "water", "fields": {}}])

    def test_returns_none_on_missing_file(self):
        self.assertIsNone(read_region_metadata(Path("/nonexistent/foo.mbtiles")))

    def test_returns_none_on_non_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "junk.mbtiles"
            path.write_text("not sqlite")
            self.assertIsNone(read_region_metadata(path))

    def test_returns_none_when_format_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.mbtiles"
            conn = sqlite3.connect(str(path))
            conn.execute("CREATE TABLE metadata (name TEXT, value TEXT)")
            conn.execute("INSERT INTO metadata VALUES ('name', 'No format')")
            conn.commit(); conn.close()
            self.assertIsNone(read_region_metadata(path))

    def test_falls_back_when_bounds_malformed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "weird.mbtiles"
            make_mbtiles(path, bounds="not,real,coordinates")
            r = read_region_metadata(path)
            self.assertIsNotNone(r)
            assert r is not None
            # Default world envelope, not a crash.
            self.assertEqual(r.bounds[0], -180.0)
            self.assertEqual(r.bounds[2], 180.0)


# ---------- discover_regions ----------

class TestDiscoverRegions(unittest.TestCase):
    def test_empty_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(discover_regions(Path(tmp)), [])

    def test_nonexistent_dir(self):
        self.assertEqual(discover_regions(Path("/nonexistent/whatever")), [])

    def test_skips_non_mbtiles_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            make_mbtiles(base / "a.mbtiles", name="A")
            (base / "ignored.txt").write_text("hello")
            (base / "ignored.zim").write_text("not a zim either")
            r = discover_regions(base)
            self.assertEqual([x.region_id for x in r], ["a"])

    def test_sorted_by_region_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for rid in ("z", "a", "m"):
                make_mbtiles(base / f"{rid}.mbtiles", name=rid.upper())
            self.assertEqual([r.region_id for r in discover_regions(base)], ["a", "m", "z"])


# ---------- build_tileserver_config ----------

class TestBuildTileserverConfig(unittest.TestCase):
    def test_empty_regions(self):
        cfg = build_tileserver_config([])
        self.assertEqual(cfg["data"], {})
        self.assertEqual(cfg["styles"], {})
        # Options block always present so the daemon has a stable config.
        self.assertIn("options", cfg)
        self.assertIn("paths", cfg["options"])

    def test_one_region(self):
        r = Region(region_id="ca", path=Path("/x/ca.mbtiles"), kind="mbtiles", name="Canada",
                   format="pbf", minzoom=0, maxzoom=12,
                   bounds=(-141, 41, -52, 84), center=(-100, 60, 3),
                   attribution="", description="", size_bytes=1)
        cfg = build_tileserver_config([r])
        self.assertEqual(cfg["data"], {"ca": {"mbtiles": "ca.mbtiles"}})
        self.assertIn("protomaps", cfg["styles"])

    def test_styles_omitted_when_no_regions(self):
        # tileserver-gl-light errors at startup if a registered style
        # references a `data` key that doesn't exist. Empty regions ->
        # no style entry -> daemon stays up.
        cfg = build_tileserver_config([])
        self.assertNotIn("protomaps", cfg["styles"])


# ---------- build_composite_style ----------

class TestBuildCompositeStyle(unittest.TestCase):
    def _region(self, rid: str, *, bounds=(-180, -85, 180, 85)) -> Region:
        return Region(region_id=rid, path=Path(f"/x/{rid}.mbtiles"), kind="mbtiles", name=rid.title(),
                      format="pbf", minzoom=0, maxzoom=12,
                      bounds=bounds, center=(0, 0, 2),
                      attribution=f"© {rid}", description="", size_bytes=1)

    def test_empty_regions_keeps_only_background(self):
        out = build_composite_style(TEMPLATE, [])
        self.assertEqual(out["sources"], {})
        self.assertEqual([l["id"] for l in out["layers"]], ["background"])
        # Glyphs/sprite still rebound for local serving even with no regions.
        self.assertTrue(out["glyphs"].startswith("/maps/fonts/"))
        self.assertEqual(out["sprite"], "protomaps")

    def test_one_region_doubles_each_vector_layer(self):
        out = build_composite_style(TEMPLATE, [self._region("ca")])
        ids = [l["id"] for l in out["layers"]]
        self.assertEqual(ids, ["background", "water__ca", "road__ca"])
        self.assertEqual(out["sources"], {
            "region__ca": {
                "type": "vector",
                "tiles": ["/maps/data/ca/{z}/{x}/{y}.pbf"],
                "minzoom": 0, "maxzoom": 12,
                "bounds": [-180, -85, 180, 85],
                "attribution": "© ca",
            }
        })
        # Each layer's source points at the right region's source.
        for l in out["layers"]:
            if l.get("type") != "background":
                self.assertEqual(l["source"], "region__ca")

    def test_multi_region_groups_per_layer(self):
        # Three regions, two vector layers each -> 6 vector layers
        # total, grouped by original layer order so rendering preserves
        # z-ordering across regions (water-r1, water-r2, water-r3, then
        # road-r1, road-r2, road-r3).
        regions = [self._region(rid) for rid in ("a", "b", "c")]
        out = build_composite_style(TEMPLATE, regions)
        ids = [l["id"] for l in out["layers"]]
        self.assertEqual(ids, [
            "background",
            "water__a", "water__b", "water__c",
            "road__a",  "road__b",  "road__c",
        ])
        # All three sources registered.
        self.assertEqual(set(out["sources"].keys()),
                         {"region__a", "region__b", "region__c"})

    def test_unioned_bounds(self):
        regions = [
            self._region("east", bounds=(-90, 25, -60, 50)),
            self._region("west", bounds=(-130, 30, -100, 49)),
        ]
        out = build_composite_style(TEMPLATE, regions)
        self.assertEqual(out["max_bounds"], [-130, 25, -60, 50])
        # Center is the midpoint of the union.
        self.assertEqual(out["center"], [(-130 + -60) / 2, (25 + 50) / 2])

    def test_template_is_not_mutated(self):
        # Pure: caller-supplied template untouched.
        before = json.dumps(TEMPLATE, sort_keys=True)
        build_composite_style(TEMPLATE, [self._region("ca")])
        after = json.dumps(TEMPLATE, sort_keys=True)
        self.assertEqual(before, after)


# ---------- render_landing_fragment ----------

class TestRenderLandingFragment(unittest.TestCase):
    def test_empty(self):
        html = render_landing_fragment([])
        self.assertIn("tile--unavailable", html)
        self.assertIn("No regions installed", html)
        self.assertNotIn("href=\"/maps/\"", html)

    def test_one_region(self):
        r = Region(region_id="ca", path=Path("/x/ca.mbtiles"), kind="mbtiles", name="Canada",
                   format="pbf", minzoom=0, maxzoom=12,
                   bounds=(-180, -85, 180, 85), center=(0, 0, 2),
                   attribution="", description="", size_bytes=12 * 1024 * 1024)
        html = render_landing_fragment([r])
        self.assertIn('href="/maps/"', html)
        self.assertIn("Canada", html)
        self.assertIn("1 region", html)        # singular
        self.assertIn("12M", html)

    def test_html_escaping(self):
        r = Region(region_id="x", path=Path("/x/x.mbtiles"), kind="mbtiles",
                   name="A & B <script>alert(1)</script>",
                   format="pbf", minzoom=0, maxzoom=12,
                   bounds=(-180, -85, 180, 85), center=(0, 0, 2),
                   attribution="", description="", size_bytes=1)
        html = render_landing_fragment([r])
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)


# ---------- regions_summary ----------

class TestRegionsSummary(unittest.TestCase):
    def test_shape(self):
        r = Region(region_id="ca", path=Path("/x/ca.mbtiles"), kind="mbtiles", name="Canada",
                   format="pbf", minzoom=0, maxzoom=12,
                   bounds=(-141, 41, -52, 84), center=(-100, 60, 3),
                   attribution="© Canada", description="Streets",
                   size_bytes=2 * 1024 * 1024 * 1024)
        out = regions_summary([r])
        self.assertEqual(len(out), 1)
        e = out[0]
        self.assertEqual(e["region_id"], "ca")
        self.assertEqual(e["name"], "Canada")
        self.assertEqual(e["bounds"], [-141, 41, -52, 84])
        self.assertEqual(e["minzoom"], 0)
        self.assertEqual(e["maxzoom"], 12)
        self.assertEqual(e["size_bytes"], 2 * 1024 * 1024 * 1024)
        self.assertEqual(e["size_human"], "2.0G")
        self.assertEqual(e["attribution"], "© Canada")


# ---------- PMTiles support (E3-S2) ----------

class TestPMTilesReader(unittest.TestCase):
    def test_happy_path_gzip_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "us.pmtiles"
            make_pmtiles(path,
                         name="United States",
                         minzoom=0, maxzoom=12,
                         bounds_e7=(-1250000000, 244000000, -669000000, 494000000),
                         center=(-981000000, 392000000, 4),
                         attribution="© OpenStreetMap",
                         vector_layers=[{"id": "water"}])
            r = read_region_metadata(path)
            self.assertIsNotNone(r)
            assert r is not None
            self.assertEqual(r.kind, "pmtiles")
            self.assertEqual(r.region_id, "us")
            self.assertEqual(r.name, "United States")
            self.assertEqual(r.format, "pbf")
            self.assertEqual(r.minzoom, 0)
            self.assertEqual(r.maxzoom, 12)
            self.assertAlmostEqual(r.bounds[0], -125.0, places=4)
            self.assertAlmostEqual(r.bounds[3], 49.4, places=4)
            self.assertAlmostEqual(r.center[0], -98.1, places=4)
            self.assertEqual(r.center[2], 4.0)
            self.assertEqual(r.attribution, "© OpenStreetMap")
            self.assertEqual(r.vector_layers, [{"id": "water"}])

    def test_uncompressed_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ca.pmtiles"
            make_pmtiles(path, name="Canada", internal_compression=1)  # 1 = none
            r = read_region_metadata(path)
            self.assertIsNotNone(r)
            assert r is not None
            self.assertEqual(r.name, "Canada")
            self.assertEqual(r.kind, "pmtiles")

    def test_returns_none_on_bad_magic(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.pmtiles"
            path.write_bytes(b"not pmtiles" + b"\x00" * 200)
            self.assertIsNone(read_region_metadata(path))

    def test_returns_none_on_truncated_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "short.pmtiles"
            path.write_bytes(b"PMTiles\x03" + b"\x00" * 8)   # only 16 bytes
            self.assertIsNone(read_region_metadata(path))

    def test_unknown_tile_type_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "weird.pmtiles"
            make_pmtiles(path, tile_type=99)
            self.assertIsNone(read_region_metadata(path))


class TestDiscoverRegionsBothFormats(unittest.TestCase):
    def test_pmtiles_wins_over_mbtiles_for_same_region_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            make_mbtiles(base / "us.mbtiles", name="US (mbtiles)")
            make_pmtiles(base / "us.pmtiles", name="US (pmtiles)")
            regions = discover_regions(base)
            self.assertEqual(len(regions), 1)
            self.assertEqual(regions[0].kind, "pmtiles")
            self.assertEqual(regions[0].name, "US (pmtiles)")

    def test_mixed_formats_kept_when_distinct_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            make_mbtiles(base / "monaco.mbtiles", name="Monaco")
            make_pmtiles(base / "us.pmtiles", name="United States")
            regions = discover_regions(base)
            self.assertEqual([r.region_id for r in regions], ["monaco", "us"])
            kinds = {r.region_id: r.kind for r in regions}
            self.assertEqual(kinds["monaco"], "mbtiles")
            self.assertEqual(kinds["us"], "pmtiles")


class TestBuildTileserverConfigPmtilesKey(unittest.TestCase):
    def test_pmtiles_key_emitted_for_pmtiles_region(self):
        r = Region(region_id="ca", path=Path("/x/ca.pmtiles"), kind="pmtiles", name="Canada",
                   format="pbf", minzoom=0, maxzoom=12,
                   bounds=(-141, 41, -52, 84), center=(-100, 60, 3),
                   attribution="", description="", size_bytes=1)
        cfg = build_tileserver_config([r])
        self.assertEqual(cfg["data"]["ca"], {"pmtiles": "ca.pmtiles"})

    def test_mbtiles_key_emitted_for_mbtiles_region(self):
        r = Region(region_id="ca", path=Path("/x/ca.mbtiles"), kind="mbtiles", name="Canada",
                   format="pbf", minzoom=0, maxzoom=12,
                   bounds=(-141, 41, -52, 84), center=(-100, 60, 3),
                   attribution="", description="", size_bytes=1)
        cfg = build_tileserver_config([r])
        self.assertEqual(cfg["data"]["ca"], {"mbtiles": "ca.mbtiles"})


class TestRegionsCatalog(unittest.TestCase):
    """Sanity checks against the static regions.json shipped with prepperpi-tiles."""

    @classmethod
    def setUpClass(cls):
        cat_path = REPO_DIR / "services" / "prepperpi-tiles" / "regions.json"
        cls.catalog = json.loads(cat_path.read_text(encoding="utf-8"))

    def test_source_url_is_https_pmtiles(self):
        url = self.catalog["source_url"]
        self.assertTrue(url.startswith("https://"))
        self.assertTrue(url.endswith(".pmtiles"))

    def test_every_country_has_required_fields(self):
        for c in self.catalog["countries"]:
            self.assertIn("id", c)
            self.assertIn("name", c)
            self.assertIn("bbox", c)
            self.assertEqual(len(c["bbox"]), 4)
            w, s, e, n = c["bbox"]
            self.assertGreaterEqual(w, -180.001, c["id"])
            self.assertLessEqual(e, 180.001, c["id"])
            self.assertGreaterEqual(s, -90.001, c["id"])
            self.assertLessEqual(n, 90.001, c["id"])
            self.assertLess(s, n, c["id"])
            self.assertIn("estimated_bytes", c)
            self.assertGreater(c["estimated_bytes"], 0, c["id"])

    def test_every_bundle_member_resolves(self):
        country_ids = {c["id"] for c in self.catalog["countries"]}
        for b in self.catalog["bundles"]:
            for cid in b["countries"]:
                self.assertIn(cid, country_ids,
                              f"bundle {b['id']} references unknown country {cid}")

    def test_required_bundles_exist(self):
        bundle_ids = {b["id"] for b in self.catalog["bundles"]}
        self.assertEqual(
            bundle_ids,
            {"na", "latam", "eu", "emea", "apac", "oceania", "russia", "antarctica"},
        )

    def test_iso_codes_are_uppercase_two_or_three_letters(self):
        # ISO 3166-1 alpha-2 (most) plus a few non-standard slugs we
        # accept (XK = Kosovo by common convention).
        import re
        pat = re.compile(r"^[A-Z]{2,3}$")
        for c in self.catalog["countries"]:
            self.assertRegex(c["id"], pat, c["id"])


if __name__ == "__main__":
    unittest.main()
