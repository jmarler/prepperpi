"""Unit tests for app/bundles.py — schema validation, source/index parsing,
ZIM book lookup against a cached Kiwix catalog, and resolution that joins
manifests to the live catalog data.

Pure-stdlib + PyYAML. Run with:
    python3 tests/unit/test_admin_bundles.py
"""
from __future__ import annotations

import sys
import textwrap
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_DIR / "services" / "prepperpi-admin" / "app"))

from bundles import (  # noqa: E402
    Bundle,
    BundleItem,
    ManifestError,
    Source,
    find_kiwix_book,
    parse_index,
    parse_manifest,
    parse_sources_config,
    resolve_bundle,
    resolve_manifest_url,
)


# ---------- manifest parsing ----------


class ParseManifestTests(unittest.TestCase):
    def _parse(self, body: str) -> Bundle:
        return parse_manifest(
            textwrap.dedent(body),
            source_id="official",
            source_name="Official",
        )

    def test_minimal_zim_only(self) -> None:
        b = self._parse("""
            id: starter
            name: Starter
            description: Curated kit.
            items:
              - kind: zim
                book_id: wikipedia_en_medicine_maxi
        """)
        self.assertEqual(b.id, "starter")
        self.assertEqual(b.qualified_id, "official:starter")
        self.assertEqual(len(b.items), 1)
        self.assertEqual(b.items[0].kind, "zim")
        self.assertEqual(b.items[0].book_id, "wikipedia_en_medicine_maxi")

    def test_all_three_kinds(self) -> None:
        b = self._parse("""
            id: mixed
            name: Mixed
            description: A bit of everything.
            items:
              - kind: zim
                book_id: wikipedia_en_all_maxi
              - kind: map_region
                region_id: US
              - kind: static
                url: https://example.com/file.pdf
                sha256: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
                size_bytes: 1024
                install_to: static/file.pdf
        """)
        kinds = [i.kind for i in b.items]
        self.assertEqual(kinds, ["zim", "map_region", "static"])
        self.assertEqual(b.items[2].install_to, "static/file.pdf")

    # --- top-level errors ---

    def test_rejects_non_yaml(self) -> None:
        with self.assertRaises(ManifestError):
            self._parse(":\n  - bad: yaml: [")

    def test_rejects_top_level_list(self) -> None:
        with self.assertRaises(ManifestError):
            parse_manifest("[]", source_id="s", source_name="S")

    def test_rejects_missing_id(self) -> None:
        with self.assertRaises(ManifestError) as cx:
            self._parse("""
                name: Foo
                items:
                  - kind: zim
                    book_id: x
            """)
        self.assertIn("`id`", str(cx.exception))

    def test_rejects_uppercase_id(self) -> None:
        with self.assertRaises(ManifestError):
            self._parse("""
                id: Starter
                name: x
                items: [{kind: zim, book_id: x}]
            """)

    def test_rejects_empty_items(self) -> None:
        with self.assertRaises(ManifestError):
            self._parse("""
                id: empty
                name: x
                items: []
            """)

    # --- per-item errors ---

    def test_rejects_unknown_kind(self) -> None:
        with self.assertRaises(ManifestError) as cx:
            self._parse("""
                id: x
                name: x
                items: [{kind: weird}]
            """)
        self.assertIn("kind", str(cx.exception))

    def test_zim_requires_book_id(self) -> None:
        with self.assertRaises(ManifestError):
            self._parse("""
                id: x
                name: x
                items: [{kind: zim}]
            """)

    def test_map_region_requires_region_id(self) -> None:
        with self.assertRaises(ManifestError):
            self._parse("""
                id: x
                name: x
                items: [{kind: map_region}]
            """)

    def test_static_requires_all_fields(self) -> None:
        # Missing sha256.
        with self.assertRaises(ManifestError):
            self._parse("""
                id: x
                name: x
                items:
                  - kind: static
                    url: https://example.com/f.pdf
                    size_bytes: 100
                    install_to: static/f.pdf
            """)

    def test_static_rejects_short_sha256(self) -> None:
        with self.assertRaises(ManifestError) as cx:
            self._parse("""
                id: x
                name: x
                items:
                  - kind: static
                    url: https://example.com/f.pdf
                    sha256: abc
                    size_bytes: 100
                    install_to: static/f.pdf
            """)
        self.assertIn("sha256", str(cx.exception))

    def test_static_rejects_non_https_for_safety(self) -> None:
        with self.assertRaises(ManifestError):
            self._parse("""
                id: x
                name: x
                items:
                  - kind: static
                    url: ftp://example.com/f.pdf
                    sha256: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
                    size_bytes: 100
                    install_to: static/f.pdf
            """)

    def test_static_rejects_path_traversal(self) -> None:
        with self.assertRaises(ManifestError):
            self._parse("""
                id: x
                name: x
                items:
                  - kind: static
                    url: https://example.com/f.pdf
                    sha256: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
                    size_bytes: 100
                    install_to: static/../etc/passwd
            """)

    def test_static_rejects_absolute_install_path(self) -> None:
        with self.assertRaises(ManifestError):
            self._parse("""
                id: x
                name: x
                items:
                  - kind: static
                    url: https://example.com/f.pdf
                    sha256: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
                    size_bytes: 100
                    install_to: /etc/passwd
            """)

    def test_static_rejects_non_allowlist_root(self) -> None:
        with self.assertRaises(ManifestError):
            self._parse("""
                id: x
                name: x
                items:
                  - kind: static
                    url: https://example.com/f.pdf
                    sha256: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
                    size_bytes: 100
                    install_to: somewhere-else/f.pdf
            """)


# ---------- index.json + sources config ----------


class ParseIndexTests(unittest.TestCase):
    def test_happy_path(self) -> None:
        text = '{"version":1,"name":"Official","manifests":[{"id":"starter","url":"manifests/starter.yaml"}]}'
        name, manifests = parse_index(text)
        self.assertEqual(name, "Official")
        self.assertEqual(manifests, [{"id": "starter", "url": "manifests/starter.yaml"}])

    def test_rejects_wrong_version(self) -> None:
        with self.assertRaises(ManifestError):
            parse_index('{"version":2,"manifests":[]}')

    def test_silently_drops_bad_manifest_entries(self) -> None:
        # Bad ids / missing urls don't blow up the whole index — they get skipped.
        text = (
            '{"version":1,"manifests":['
            '{"id":"good","url":"a.yaml"},'
            '{"id":"BAD"},'                 # uppercase fails id check
            '{"id":"good","url":"dup.yaml"}'  # dup id
            ']}'
        )
        _, manifests = parse_index(text)
        self.assertEqual(len(manifests), 1)
        self.assertEqual(manifests[0]["id"], "good")


class ParseSourcesConfigTests(unittest.TestCase):
    def test_happy_path(self) -> None:
        text = '{"sources":[{"id":"official","url":"https://example.com/index.json","builtin":true}]}'
        srcs = parse_sources_config(text)
        self.assertEqual(len(srcs), 1)
        self.assertEqual(srcs[0].id, "official")
        self.assertTrue(srcs[0].builtin)
        self.assertTrue(srcs[0].enabled)

    def test_skips_bad_url(self) -> None:
        text = '{"sources":[{"id":"a","url":"not-a-url"},{"id":"b","url":"https://x.example/i.json"}]}'
        srcs = parse_sources_config(text)
        self.assertEqual([s.id for s in srcs], ["b"])

    def test_returns_empty_on_garbage(self) -> None:
        self.assertEqual(parse_sources_config("not json"), [])
        self.assertEqual(parse_sources_config("[]"), [])


class ResolveManifestUrlTests(unittest.TestCase):
    def test_relative(self) -> None:
        self.assertEqual(
            resolve_manifest_url(
                "https://raw.githubusercontent.com/u/r/main/index.json",
                "manifests/starter.yaml",
            ),
            "https://raw.githubusercontent.com/u/r/main/manifests/starter.yaml",
        )

    def test_absolute_passes_through(self) -> None:
        self.assertEqual(
            resolve_manifest_url(
                "https://x/index.json",
                "https://other/foo.yaml",
            ),
            "https://other/foo.yaml",
        )


# ---------- Kiwix book lookup ----------


class FindKiwixBookTests(unittest.TestCase):
    BOOKS = [
        {"name": "wikipedia_en_all_maxi_2024-01", "size_bytes": 100, "updated": "2024-01-01"},
        {"name": "wikipedia_en_all_maxi_2024-04", "size_bytes": 110, "updated": "2024-04-01"},
        {"name": "wikipedia_en_all_maxi_2024-03", "size_bytes": 105, "updated": "2024-03-01"},
        {"name": "wikipedia_en_medicine_maxi_2024-04", "size_bytes": 50, "updated": "2024-04-01"},
        {"name": "ifixit_en_all_2024-02", "size_bytes": 20, "updated": "2024-02-01"},
    ]

    def test_picks_latest_by_updated(self) -> None:
        b = find_kiwix_book(self.BOOKS, "wikipedia_en_all_maxi")
        self.assertIsNotNone(b)
        self.assertEqual(b["name"], "wikipedia_en_all_maxi_2024-04")

    def test_short_prefix_picks_latest_across_variants(self) -> None:
        # `wikipedia_en` matches every Wikipedia variant; we pick the
        # newest by `updated`. This is intentional: more-specific
        # prefixes give more-specific picks. Manifests that need a
        # particular edition should use the full Kiwix name (e.g.
        # `wikipedia_en_all_maxi`).
        b = find_kiwix_book(self.BOOKS, "wikipedia_en")
        self.assertIsNotNone(b)
        # Tied at 2024-04-01: medicine and all_maxi. Sort is stable,
        # so insertion order from BOOKS wins for ties — assert one of
        # the two valid newest candidates.
        self.assertIn(b["name"], {
            "wikipedia_en_all_maxi_2024-04",
            "wikipedia_en_medicine_maxi_2024-04",
        })

    def test_exact_match_works_too(self) -> None:
        b = find_kiwix_book(self.BOOKS, "ifixit_en_all_2024-02")
        self.assertIsNotNone(b)
        self.assertEqual(b["name"], "ifixit_en_all_2024-02")

    def test_no_match_returns_none(self) -> None:
        self.assertIsNone(find_kiwix_book(self.BOOKS, "nonexistent"))

    def test_distinguishes_medicine_from_all(self) -> None:
        # `wikipedia_en_medicine_maxi` must not match
        # `wikipedia_en_all_maxi`. Test the underscore-prefix discipline.
        b = find_kiwix_book(self.BOOKS, "wikipedia_en_medicine_maxi")
        self.assertEqual(b["name"], "wikipedia_en_medicine_maxi_2024-04")


# ---------- bundle resolution ----------


class ResolveBundleTests(unittest.TestCase):
    BOOKS = [
        {
            "name": "wikipedia_en_medicine_maxi_2024-04",
            "title": "Wikipedia (medicine)",
            "size_bytes": 4_500_000_000,
            "url": "https://download.kiwix.org/.../wikipedia_en_medicine_maxi_2024-04.zim.meta4",
            "updated": "2024-04-01",
        },
    ]
    REGIONS = {
        "regions": [
            {"id": "US", "name": "United States", "estimated_bytes": 18_000_000_000},
        ],
    }

    def _bundle(self, items: list[BundleItem]) -> Bundle:
        return Bundle(
            source_id="official",
            source_name="Official",
            id="t",
            name="Test",
            description="",
            license_notes="",
            items=items,
        )

    def test_resolves_zim_against_catalog(self) -> None:
        b = self._bundle([BundleItem(kind="zim", book_id="wikipedia_en_medicine_maxi")])
        resolve_bundle(b, catalog_books=self.BOOKS, region_catalog=self.REGIONS)
        self.assertEqual(b.resolution_errors, [])
        self.assertEqual(b.resolved_size_bytes, 4_500_000_000)
        self.assertEqual(len(b.resolved_items), 1)
        self.assertEqual(b.resolved_items[0]["kind"], "zim")

    def test_resolves_map_region(self) -> None:
        b = self._bundle([BundleItem(kind="map_region", region_id="US")])
        resolve_bundle(b, catalog_books=self.BOOKS, region_catalog=self.REGIONS)
        self.assertEqual(b.resolution_errors, [])
        self.assertEqual(b.resolved_size_bytes, 18_000_000_000)

    def test_resolves_static_from_manifest(self) -> None:
        b = self._bundle([
            BundleItem(
                kind="static",
                url="https://example.com/f.pdf",
                sha256="0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
                size_bytes=12345,
                install_to="static/f.pdf",
            ),
        ])
        resolve_bundle(b, catalog_books=self.BOOKS, region_catalog=self.REGIONS)
        self.assertEqual(b.resolution_errors, [])
        self.assertEqual(b.resolved_size_bytes, 12345)

    def test_unknown_zim_book_id_is_an_error_not_a_throw(self) -> None:
        b = self._bundle([BundleItem(kind="zim", book_id="not_in_catalog")])
        resolve_bundle(b, catalog_books=self.BOOKS, region_catalog=self.REGIONS)
        self.assertEqual(len(b.resolution_errors), 1)
        self.assertIn("not_in_catalog", b.resolution_errors[0])
        self.assertEqual(b.resolved_size_bytes, 0)

    def test_unknown_region_id_is_an_error_not_a_throw(self) -> None:
        b = self._bundle([BundleItem(kind="map_region", region_id="ZZ")])
        resolve_bundle(b, catalog_books=self.BOOKS, region_catalog=self.REGIONS)
        self.assertEqual(len(b.resolution_errors), 1)

    def test_size_aggregates_only_resolved_items(self) -> None:
        b = self._bundle([
            BundleItem(kind="zim", book_id="wikipedia_en_medicine_maxi"),
            BundleItem(kind="zim", book_id="not_in_catalog"),
            BundleItem(kind="map_region", region_id="US"),
        ])
        resolve_bundle(b, catalog_books=self.BOOKS, region_catalog=self.REGIONS)
        # Two of three items resolved.
        self.assertEqual(b.resolved_size_bytes, 4_500_000_000 + 18_000_000_000)
        self.assertEqual(len(b.resolution_errors), 1)


if __name__ == "__main__":
    unittest.main()
