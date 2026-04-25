"""Unit tests for services/prepperpi-tiles/tiles_indexer.py (E3-S1).

Covers the four pure transforms plus the I/O boundary:

  read_region_metadata    — opens a real SQLite fixture
  discover_regions        — walks a tmp dir
  build_tileserver_config — pure dict-out
  build_composite_style   — pure dict-out (layer multiplication)
  render_landing_fragment — pure HTML-out
  regions_summary         — pure list-out

Pure stdlib. No network. MBTiles fixtures are built in-process via
sqlite3 so the suite stays self-contained.
"""
from __future__ import annotations

import json
import sqlite3
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
        r = Region(region_id="ca", path=Path("/x/ca.mbtiles"), name="Canada",
                   format="pbf", minzoom=0, maxzoom=12,
                   bounds=(-141, 41, -52, 84), center=(-100, 60, 3),
                   attribution="", description="", size_bytes=1)
        cfg = build_tileserver_config([r])
        self.assertEqual(cfg["data"], {"ca": {"mbtiles": "ca.mbtiles"}})
        self.assertIn("osm-bright", cfg["styles"])

    def test_styles_omitted_when_no_regions(self):
        # tileserver-gl-light errors at startup if a registered style
        # references a `data` key that doesn't exist. Empty regions ->
        # no style entry -> daemon stays up.
        cfg = build_tileserver_config([])
        self.assertNotIn("osm-bright", cfg["styles"])


# ---------- build_composite_style ----------

class TestBuildCompositeStyle(unittest.TestCase):
    def _region(self, rid: str, *, bounds=(-180, -85, 180, 85)) -> Region:
        return Region(region_id=rid, path=Path(f"/x/{rid}.mbtiles"), name=rid.title(),
                      format="pbf", minzoom=0, maxzoom=12,
                      bounds=bounds, center=(0, 0, 2),
                      attribution=f"© {rid}", description="", size_bytes=1)

    def test_empty_regions_keeps_only_background(self):
        out = build_composite_style(TEMPLATE, [])
        self.assertEqual(out["sources"], {})
        self.assertEqual([l["id"] for l in out["layers"]], ["background"])
        # Glyphs/sprite still rebound for local serving even with no regions.
        self.assertTrue(out["glyphs"].startswith("/maps/fonts/"))
        self.assertEqual(out["sprite"], "osm-bright")

    def test_one_region_doubles_each_vector_layer(self):
        out = build_composite_style(TEMPLATE, [self._region("ca")])
        ids = [l["id"] for l in out["layers"]]
        self.assertEqual(ids, ["background", "water__ca", "road__ca"])
        self.assertEqual(out["sources"], {
            "openmaptiles__ca": {
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
                self.assertEqual(l["source"], "openmaptiles__ca")

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
                         {"openmaptiles__a", "openmaptiles__b", "openmaptiles__c"})

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
        r = Region(region_id="ca", path=Path("/x/ca.mbtiles"), name="Canada",
                   format="pbf", minzoom=0, maxzoom=12,
                   bounds=(-180, -85, 180, 85), center=(0, 0, 2),
                   attribution="", description="", size_bytes=12 * 1024 * 1024)
        html = render_landing_fragment([r])
        self.assertIn('href="/maps/"', html)
        self.assertIn("Canada", html)
        self.assertIn("1 region", html)        # singular
        self.assertIn("12M", html)

    def test_html_escaping(self):
        r = Region(region_id="x", path=Path("/x/x.mbtiles"),
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
        r = Region(region_id="ca", path=Path("/x/ca.mbtiles"), name="Canada",
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


if __name__ == "__main__":
    unittest.main()
