"""Unit tests for app/updates.py — pure detection logic for ZIM /
map-region / bundle / static drift, plus pin handling.

Pure-stdlib. Run with:
    python3 tests/unit/test_admin_updates.py
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_DIR / "services" / "prepperpi-admin" / "app"))

from updates import (  # noqa: E402
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
    serialize_pins,
    sha256_text,
)


# ---------- ZIM filename parsing ----------


class ParseZimFilenameTests(unittest.TestCase):
    def test_yyyy_mm_suffix(self) -> None:
        zf = parse_zim_filename("wikipedia_en_all_2026-03.zim", size_bytes=10)
        self.assertEqual(zf.book_id, "wikipedia_en_all")
        self.assertEqual(zf.version, "2026-03")
        self.assertEqual(zf.name_stem, "wikipedia_en_all_2026-03")
        self.assertEqual(zf.size_bytes, 10)

    def test_yyyy_mm_dd_suffix(self) -> None:
        zf = parse_zim_filename("wikem_en_all_2026-03-15.zim")
        self.assertEqual(zf.book_id, "wikem_en_all")
        self.assertEqual(zf.version, "2026-03-15")

    def test_maxi_variant(self) -> None:
        zf = parse_zim_filename("wikipedia_en_all_maxi_2026-03.zim")
        self.assertEqual(zf.book_id, "wikipedia_en_all_maxi")
        self.assertEqual(zf.version, "2026-03")

    def test_no_date_suffix_falls_back_to_stem(self) -> None:
        zf = parse_zim_filename("custom-content.zim")
        self.assertEqual(zf.book_id, "custom-content")
        self.assertEqual(zf.version, "")
        self.assertEqual(zf.name_stem, "custom-content")

    def test_filename_without_zim_ext(self) -> None:
        # Defensive — caller may pass us a stem.
        zf = parse_zim_filename("ifixit_en_all_2026-02")
        self.assertEqual(zf.book_id, "ifixit_en_all")
        self.assertEqual(zf.version, "2026-02")


# ---------- Pin parsing / serialization ----------


class PinStoreTests(unittest.TestCase):
    def test_empty_input_yields_empty_store(self) -> None:
        self.assertEqual(parse_pins(""), PinStore())
        self.assertEqual(parse_pins("not json"), PinStore())
        self.assertEqual(parse_pins("[]"), PinStore())

    def test_round_trip(self) -> None:
        store = PinStore(
            zims={"wikipedia_en_all": "2026-03"},
            regions={"US": {"etag": "abc", "last_modified": "Mon"}},
            bundles={"official:starter": "deadbeef"},
            statics={"static/foo.pdf": "facade01"},
        )
        text = serialize_pins(store)
        parsed = parse_pins(text)
        self.assertEqual(parsed, store)

    def test_drops_malformed_entries(self) -> None:
        body = json.dumps({
            "zims": {"good": "2026-01", "bad": 123},
            "regions": {"US": "should-be-dict"},
            "bundles": {"x:y": ["nope"]},
        })
        store = parse_pins(body)
        self.assertEqual(store.zims, {"good": "2026-01"})
        self.assertEqual(store.regions, {})
        self.assertEqual(store.bundles, {})


# ---------- ZIM drift ----------


class DetectZimDriftTests(unittest.TestCase):
    def _catalog(self, *names_with_dates: tuple[str, str]) -> list[dict]:
        # (filename_stem, updated). The catalog `name` is shared
        # across flavors, so we set it to the prefix-without-date and
        # the filename to the full date-suffixed form (matches Kiwix).
        out = []
        for stem, updated in names_with_dates:
            # Derive the OPDS "name" by stripping a trailing _YYYY-MM(-DD).
            import re as _re
            m = _re.match(r"^(.+?)_(\d{4}-\d{2}(?:-\d{2})?)$", stem)
            opds_name = m.group(1) if m else stem
            out.append({
                "name": opds_name,
                "updated": updated,
                "title": stem,
                "size_bytes": 1_000_000,
                "url": f"https://download.kiwix.org/zim/{stem}.zim.meta4",
                "filename": f"{stem}.zim",
            })
        return out

    def test_stale_when_catalog_has_newer(self) -> None:
        installed = [parse_zim_filename("wikipedia_en_all_2026-03.zim", size_bytes=900_000)]
        catalog = self._catalog(
            ("wikipedia_en_all_2026-04", "2026-04-01T00:00:00Z"),
            ("wikipedia_en_all_2026-03", "2026-03-01T00:00:00Z"),
        )
        items = detect_zim_drift(installed, catalog, PinStore())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "stale")
        self.assertEqual(items[0]["installed"], "2026-03")
        self.assertEqual(items[0]["available"], "2026-04")
        self.assertEqual(items[0]["available_name"], "wikipedia_en_all_2026-04")
        self.assertEqual(items[0]["size_delta_bytes"], 100_000)

    def test_current_when_catalog_matches(self) -> None:
        installed = [parse_zim_filename("wikipedia_en_all_2026-03.zim")]
        catalog = self._catalog(
            ("wikipedia_en_all_2026-03", "2026-03-01T00:00:00Z"),
        )
        items = detect_zim_drift(installed, catalog, PinStore())
        self.assertEqual(items[0]["status"], "current")
        self.assertIsNone(items[0]["available_url"])

    def test_pin_to_installed_version_hides_stale(self) -> None:
        installed = [parse_zim_filename("wikipedia_en_all_2026-03.zim")]
        catalog = self._catalog(
            ("wikipedia_en_all_2026-04", "2026-04-01T00:00:00Z"),
        )
        pins = PinStore(zims={"wikipedia_en_all": "2026-03"})
        items = detect_zim_drift(installed, catalog, pins)
        self.assertEqual(items[0]["status"], "pinned")

    def test_unknown_when_catalog_has_no_match(self) -> None:
        installed = [parse_zim_filename("custom-content.zim")]
        items = detect_zim_drift(installed, [], PinStore())
        self.assertEqual(items[0]["status"], "unknown")
        self.assertIsNone(items[0]["available"])

    def test_two_files_same_book_id_yield_one_row_with_older(self) -> None:
        # User did a side-by-side update; old + new both on disk.
        installed = [
            parse_zim_filename("wikipedia_en_all_2026-03.zim", size_bytes=900_000),
            parse_zim_filename("wikipedia_en_all_2026-04.zim", size_bytes=950_000),
        ]
        catalog = self._catalog(
            ("wikipedia_en_all_2026-04", "2026-04-01T00:00:00Z"),
        )
        items = detect_zim_drift(installed, catalog, PinStore())
        self.assertEqual(len(items), 1)
        # Row reports on the newest copy (2026-04, current).
        self.assertEqual(items[0]["status"], "current")
        self.assertEqual(items[0]["installed"], "2026-04")
        self.assertEqual(items[0]["installed_filename"], "wikipedia_en_all_2026-04.zim")
        # Older file surfaces in older_files so the UI can offer Delete.
        self.assertEqual(len(items[0]["older_files"]), 1)
        self.assertEqual(
            items[0]["older_files"][0]["filename"],
            "wikipedia_en_all_2026-03.zim",
        )

    def test_exact_filename_stem_match_beats_prefix(self) -> None:
        # If the catalog has an entry whose date-stripped filename
        # stem matches the book_id exactly, that wins over a broader
        # prefix match — pinning to a specific flavor should never get
        # upgraded to a different family without the user asking.
        installed = [parse_zim_filename("wikipedia_en_2026-01.zim")]
        catalog = self._catalog(
            ("wikipedia_en_all_2026-04",      "2026-04-01T00:00:00Z"),
            ("wikipedia_en_all_maxi_2026-04", "2026-04-02T00:00:00Z"),
            ("wikipedia_en_2026-04",          "2026-04-01T00:00:00Z"),
        )
        items = detect_zim_drift(installed, catalog, PinStore())
        self.assertEqual(items[0]["available_name"], "wikipedia_en_2026-04")
        self.assertEqual(items[0]["status"], "stale")


# ---------- Region drift ----------


class DetectRegionDriftTests(unittest.TestCase):
    def _sidecar(self, **kw) -> RegionSidecar:
        defaults = {
            "region_id": "US",
            "source_url": "https://example.com/planet.pmtiles",
            "etag": "\"abc\"",
            "last_modified": "Mon, 01 Mar 2026 00:00:00 GMT",
            "extracted_bytes": 1_000_000_000,
        }
        defaults.update(kw)
        return RegionSidecar(**defaults)

    def test_stale_on_changed_etag(self) -> None:
        sc = self._sidecar()
        head = {sc.source_url: {"etag": "\"xyz\"", "last_modified": sc.last_modified}}
        items = detect_region_drift([sc], head, PinStore())
        self.assertEqual(items[0]["status"], "stale")
        self.assertEqual(items[0]["available_url"], sc.source_url)

    def test_stale_on_changed_last_modified(self) -> None:
        sc = self._sidecar()
        head = {sc.source_url: {
            "etag": sc.etag,
            "last_modified": "Mon, 15 Apr 2026 00:00:00 GMT",
        }}
        items = detect_region_drift([sc], head, PinStore())
        self.assertEqual(items[0]["status"], "stale")

    def test_current_on_matching_headers(self) -> None:
        sc = self._sidecar()
        head = {sc.source_url: {
            "etag": sc.etag, "last_modified": sc.last_modified,
        }}
        items = detect_region_drift([sc], head, PinStore())
        self.assertEqual(items[0]["status"], "current")

    def test_unknown_on_head_error(self) -> None:
        sc = self._sidecar()
        head = {sc.source_url: {"error": "timeout"}}
        items = detect_region_drift([sc], head, PinStore())
        self.assertEqual(items[0]["status"], "unknown")
        self.assertEqual(items[0]["error"], "timeout")

    def test_pinned_region_hides_stale(self) -> None:
        sc = self._sidecar()
        pins = PinStore(regions={
            "US": {"etag": sc.etag, "last_modified": sc.last_modified},
        })
        head = {sc.source_url: {"etag": "\"new\"", "last_modified": sc.last_modified}}
        items = detect_region_drift([sc], head, pins)
        self.assertEqual(items[0]["status"], "pinned")

    def test_empty_etag_is_not_a_diff(self) -> None:
        # Servers that don't ship ETag at all shouldn't make the region
        # look perpetually stale. Empty-on-empty = no change.
        sc = self._sidecar(etag="")
        head = {sc.source_url: {"etag": "", "last_modified": sc.last_modified}}
        items = detect_region_drift([sc], head, PinStore())
        self.assertEqual(items[0]["status"], "current")


class ParseSidecarTests(unittest.TestCase):
    def test_parses_full_payload(self) -> None:
        text = json.dumps({
            "region_id": "US",
            "source_url": "https://example.com/x.pmtiles",
            "etag": "\"abc\"",
            "last_modified": "Mon, 01 Mar 2026 00:00:00 GMT",
            "extracted_at": "2026-03-01T00:00:00Z",
            "extracted_bytes": 12345,
        })
        sc = parse_sidecar(text)
        self.assertIsNotNone(sc)
        assert sc is not None
        self.assertEqual(sc.region_id, "US")
        self.assertEqual(sc.extracted_bytes, 12345)

    def test_rejects_missing_required_fields(self) -> None:
        self.assertIsNone(parse_sidecar("{}"))
        self.assertIsNone(parse_sidecar(json.dumps({"region_id": "US"})))
        self.assertIsNone(parse_sidecar("not json"))


# ---------- Bundle drift ----------


class DetectBundleDriftTests(unittest.TestCase):
    def test_stale_when_body_changed(self) -> None:
        cached = {"official:starter": "old body"}
        fresh = {"official:starter": "new body"}
        items = detect_bundle_drift(cached, fresh, PinStore())
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["status"], "stale")
        self.assertEqual(items[0]["available"], sha256_text("new body")[:7])

    def test_current_emits_no_row(self) -> None:
        # The dashboard hides current bundles — only stale/pinned/new
        # rows surface so users see actionable items only.
        cached = {"official:starter": "same"}
        fresh = {"official:starter": "same"}
        items = detect_bundle_drift(cached, fresh, PinStore())
        self.assertEqual(items, [])

    def test_uses_bundle_title_when_provided(self) -> None:
        cached = {"official:starter": "old"}
        fresh = {"official:starter": "new"}
        items = detect_bundle_drift(
            cached, fresh, PinStore(),
            bundle_titles={"official:starter": "Starter"},
        )
        self.assertEqual(items[0]["title"], "Starter")

    def test_pinned_bundle_hides_stale(self) -> None:
        cached = {"official:starter": "old"}
        fresh = {"official:starter": "new"}
        pins = PinStore(bundles={"official:starter": sha256_text("old")})
        items = detect_bundle_drift(cached, fresh, pins)
        self.assertEqual(items[0]["status"], "pinned")

    def test_dropped_upstream_yields_no_row(self) -> None:
        cached = {"official:retired": "x"}
        fresh: dict[str, str] = {}
        items = detect_bundle_drift(cached, fresh, PinStore())
        self.assertEqual(items, [])

    def test_brand_new_bundle_appears_as_stale(self) -> None:
        cached: dict[str, str] = {}
        fresh = {"community:new": "body"}
        items = detect_bundle_drift(cached, fresh, PinStore())
        self.assertEqual(items[0]["status"], "stale")
        self.assertEqual(items[0]["installed"], "(new)")


# ---------- Static drift ----------


class DetectStaticDriftTests(unittest.TestCase):
    def test_stale_when_sha_differs(self) -> None:
        installed = [StaticInstalled(
            install_to="static/foo.pdf",
            on_disk_sha256="aa" * 32,
            size_bytes=1000,
        )]
        manifest = [StaticManifestEntry(
            install_to="static/foo.pdf",
            expected_sha256="bb" * 32,
            expected_size=1100,
            url="https://example.com/foo.pdf",
            bundle_qualified_id="official:complete",
        )]
        items = detect_static_drift(installed, manifest, PinStore())
        self.assertEqual(items[0]["status"], "stale")
        self.assertEqual(items[0]["size_delta_bytes"], 100)
        self.assertEqual(items[0]["available_url"], "https://example.com/foo.pdf")
        self.assertEqual(items[0]["bundle_qualified_id"], "official:complete")

    def test_current_when_sha_matches(self) -> None:
        installed = [StaticInstalled(
            install_to="static/foo.pdf",
            on_disk_sha256="aa" * 32,
            size_bytes=1000,
        )]
        manifest = [StaticManifestEntry(
            install_to="static/foo.pdf",
            expected_sha256="aa" * 32,
            expected_size=1000,
            url="https://example.com/foo.pdf",
            bundle_qualified_id="official:complete",
        )]
        items = detect_static_drift(installed, manifest, PinStore())
        self.assertEqual(items[0]["status"], "current")

    def test_no_row_when_install_to_not_in_any_manifest(self) -> None:
        installed = [StaticInstalled(
            install_to="static/orphan.pdf",
            on_disk_sha256="aa" * 32,
            size_bytes=10,
        )]
        items = detect_static_drift(installed, [], PinStore())
        self.assertEqual(items, [])

    def test_pinned_static_hides_stale(self) -> None:
        installed = [StaticInstalled(
            install_to="static/foo.pdf",
            on_disk_sha256="aa" * 32,
            size_bytes=1000,
        )]
        manifest = [StaticManifestEntry(
            install_to="static/foo.pdf",
            expected_sha256="bb" * 32,
            expected_size=1000,
            url="https://example.com/foo.pdf",
            bundle_qualified_id="official:complete",
        )]
        pins = PinStore(statics={"static/foo.pdf": "aa" * 32})
        items = detect_static_drift(installed, manifest, pins)
        self.assertEqual(items[0]["status"], "pinned")


# ---------- count_stale ----------


class CountStaleTests(unittest.TestCase):
    def test_counts_only_stale(self) -> None:
        items = [
            {"status": "stale"},
            {"status": "current"},
            {"status": "pinned"},
            {"status": "stale"},
            {"status": "unknown"},
        ]
        self.assertEqual(count_stale(items), 2)


if __name__ == "__main__":
    unittest.main()
