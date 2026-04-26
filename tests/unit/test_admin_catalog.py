"""Unit tests for app/catalog.py — OPDS parser + filter helpers.

Pure stdlib. No network. Sample XML built inline to mimic what
library.kiwix.org/catalog/v2/entries actually returns; if the
upstream feed format changes we'll catch it here before deployment.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_DIR / "services" / "prepperpi-admin" / "app"))

from catalog import (  # noqa: E402
    _category_from_name,
    _filename_from_url,
    collect_facets,
    filter_books,
    parse_entry,
    parse_feed,
)
import xml.etree.ElementTree as ET  # noqa: E402


SAMPLE_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:dc="http://purl.org/dc/terms/"
      xmlns:opds="http://opds-spec.org/2010/catalog">
  <title>Kiwix library</title>
  <updated>2026-04-25T00:00:00Z</updated>
  <entry>
    <title>Wikipedia (en)</title>
    <id>urn:uuid:11111111-1111-1111-1111-111111111111</id>
    <name>wikipedia_en_all_nopic_2024-01</name>
    <summary>The free encyclopedia, English.</summary>
    <language>eng</language>
    <updated>2024-01-15T00:00:00Z</updated>
    <category term="wikipedia" label="Wikipedia"/>
    <category term="_pictures:no"/>
    <link rel="http://opds-spec.org/acquisition/open-access"
          href="https://download.kiwix.org/zim/wikipedia/wikipedia_en_all_nopic_2024-01.zim.meta4"
          type="application/x-zim"
          length="95000000000" />
  </entry>
  <entry>
    <title>WikiHow (es)</title>
    <id>urn:uuid:22222222-2222-2222-2222-222222222222</id>
    <name>wikihow_es_maxi_2024-03</name>
    <summary>Cómo hacer cualquier cosa.</summary>
    <language>spa</language>
    <updated>2024-03-10T00:00:00Z</updated>
    <category term="wikihow"/>
    <link rel="http://opds-spec.org/acquisition/open-access"
          href="https://download.kiwix.org/zim/wikihow/wikihow_es_maxi_2024-03.zim"
          type="application/x-zim"
          length="500000000" />
  </entry>
  <entry>
    <title>broken — no acquisition link</title>
    <id>urn:uuid:33333333-3333-3333-3333-333333333333</id>
    <name>broken</name>
  </entry>
</feed>
"""


class ParseFeedTests(unittest.TestCase):
    def test_drops_entries_without_acquisition(self) -> None:
        books = parse_feed(SAMPLE_FEED)
        self.assertEqual(len(books), 2)
        self.assertEqual(books[0]["name"], "wikipedia_en_all_nopic_2024-01")
        self.assertEqual(books[1]["name"], "wikihow_es_maxi_2024-03")

    def test_garbage_returns_empty_list(self) -> None:
        self.assertEqual(parse_feed(""), [])
        self.assertEqual(parse_feed("not xml"), [])
        self.assertEqual(parse_feed("<feed>unclosed"), [])

    def test_filename_strips_meta4_suffix(self) -> None:
        books = parse_feed(SAMPLE_FEED)
        self.assertEqual(books[0]["filename"], "wikipedia_en_all_nopic_2024-01.zim")
        self.assertEqual(books[1]["filename"], "wikihow_es_maxi_2024-03.zim")

    def test_extracts_size_and_language(self) -> None:
        books = parse_feed(SAMPLE_FEED)
        self.assertEqual(books[0]["size_bytes"], 95_000_000_000)
        self.assertEqual(books[0]["language"], "eng")
        self.assertEqual(books[1]["size_bytes"], 500_000_000)
        self.assertEqual(books[1]["language"], "spa")

    def test_drops_underscore_internal_tags(self) -> None:
        books = parse_feed(SAMPLE_FEED)
        # _pictures:no should be filtered out; only "wikipedia" remains.
        self.assertEqual(books[0]["tags"], ["wikipedia"])

    def test_category_falls_back_to_name_prefix_when_no_tags(self) -> None:
        # Synthesize an entry with NO category tags.
        xml = """<entry xmlns="http://www.w3.org/2005/Atom">
            <title>Bare</title><id>x</id><name>gutenberg_en_all_2024</name>
            <updated>2024-01-01T00:00:00Z</updated>
            <link rel="http://opds-spec.org/acquisition/open-access"
                  href="https://example/foo.zim" length="100"/>
        </entry>"""
        node = ET.fromstring(xml)
        book = parse_entry(node)
        self.assertIsNotNone(book)
        assert book is not None
        self.assertEqual(book["category"], "gutenberg")


class FilterBooksTests(unittest.TestCase):
    def setUp(self) -> None:
        self.books = parse_feed(SAMPLE_FEED)

    def test_no_filters_returns_all(self) -> None:
        self.assertEqual(len(filter_books(self.books)), 2)

    def test_language(self) -> None:
        self.assertEqual(len(filter_books(self.books, language="eng")), 1)
        self.assertEqual(len(filter_books(self.books, language="spa")), 1)
        self.assertEqual(len(filter_books(self.books, language="zzz")), 0)

    def test_category(self) -> None:
        self.assertEqual(len(filter_books(self.books, category="wikipedia")), 1)
        self.assertEqual(len(filter_books(self.books, category="wikihow")), 1)
        self.assertEqual(len(filter_books(self.books, category="missing")), 0)

    def test_size_range(self) -> None:
        # 100 MB-1 GB bucket should match wikihow (500MB), not wikipedia (95GB)
        result = filter_books(self.books, size_min=100_000_000, size_max=1_000_000_000)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "wikihow_es_maxi_2024-03")

    def test_name_substring_case_insensitive(self) -> None:
        self.assertEqual(len(filter_books(self.books, name_substring="WIKIPEDIA")), 1)
        self.assertEqual(len(filter_books(self.books, name_substring="es_maxi")), 1)
        self.assertEqual(len(filter_books(self.books, name_substring="nothing")), 0)


class CollectFacetsTests(unittest.TestCase):
    def test_counts_languages_and_categories(self) -> None:
        books = parse_feed(SAMPLE_FEED)
        facets = collect_facets(books)
        langs = {f["code"]: f["count"] for f in facets["languages"]}
        cats = {f["name"]: f["count"] for f in facets["categories"]}
        self.assertEqual(langs, {"eng": 1, "spa": 1})
        self.assertEqual(cats, {"wikipedia": 1, "wikihow": 1})


class HelperTests(unittest.TestCase):
    def test_filename_from_url(self) -> None:
        self.assertEqual(_filename_from_url("https://x/foo.zim.meta4"), "foo.zim")
        self.assertEqual(_filename_from_url("https://x/foo.zim"), "foo.zim")

    def test_category_from_name(self) -> None:
        self.assertEqual(_category_from_name("wikipedia_en_all"), "wikipedia")
        self.assertEqual(_category_from_name(""), "")
        self.assertEqual(_category_from_name("nounderscores"), "")


if __name__ == "__main__":
    unittest.main()
