"""Kiwix OPDS catalog parser + filter helpers (E2-S3).

`library.kiwix.org/catalog/v2/entries?count=N` returns Atom/OPDS XML.
We normalize it into a small, JSON-friendly dict so the admin UI can
filter client-side without parsing XML in the browser.

Pure functions: no I/O. The fetch + cache lives in main.py so this
module is fully unit-testable from a string of XML.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Optional

ATOM = "http://www.w3.org/2005/Atom"
OPDS = "http://opds-spec.org/2010/catalog"
DC = "http://purl.org/dc/terms/"
KIWIX_NS = "http://kiwix.org/opds/2020"

# Atom namespaces used in Kiwix's feed. We accept several variants
# defensively so a small upstream change doesn't break the parser.
_NS = {
    "atom": ATOM,
    "opds": OPDS,
    "dc": DC,
    "k": KIWIX_NS,
}

ACQUISITION_REL = "http://opds-spec.org/acquisition/open-access"


def _findtext(node: ET.Element, tag: str) -> str:
    """Find one of {tag}, {atom}tag, {dc}tag — return text or ''."""
    for prefix in ("", "atom:", "dc:"):
        found = node.find(f"{prefix}{tag}", _NS)
        if found is not None and found.text:
            return found.text.strip()
    return ""


def parse_entry(entry: ET.Element) -> Optional[dict]:
    """Convert one <entry> into our canonical dict, or None if it's
    missing the must-have fields (download URL or size).

    Output shape:
        {
          "id":        "urn:uuid:...",
          "name":      "wikipedia_en_all_nopic_2024-01",
          "title":     "Wikipedia (en)",
          "summary":   "...",
          "language":  "eng",
          "tags":      ["wikipedia", ...],
          "size_bytes": 1234567890,
          "url":       "https://download.kiwix.org/.../foo.zim.meta4",
          "updated":   "2024-01-01T00:00:00Z",
          "filename":  "foo.zim",     (derived from URL)
          "category":  "wikipedia",   (best-guess primary tag for filter UI)
        }
    """
    title = _findtext(entry, "title")
    book_id = _findtext(entry, "id")
    name = _findtext(entry, "name")
    summary = _findtext(entry, "summary")
    language = _findtext(entry, "language") or "und"
    updated = _findtext(entry, "updated")

    # Acquisition link: there are typically several <link> elements;
    # we want the one whose `rel` is the OPDS open-access acquisition.
    url: Optional[str] = None
    size_bytes: Optional[int] = None
    for link in entry.findall("atom:link", _NS):
        rel = link.get("rel", "")
        if rel != ACQUISITION_REL:
            continue
        url = link.get("href")
        length = link.get("length")
        if length and length.isdigit():
            size_bytes = int(length)
        break
    if not url or size_bytes is None:
        return None

    # Tags / categories. Kiwix uses both <category term="..."/> and
    # custom tags. Drop kiwix-internal flag tags (`_pictures:no` etc.)
    # and keep the human-meaningful ones.
    tags: list[str] = []
    for cat in entry.findall("atom:category", _NS):
        term = cat.get("term", "").strip()
        if term and not term.startswith("_"):
            tags.append(term)

    # Best-guess primary "topic" for the filter dropdown — first tag,
    # or fall back to the leading underscore of `name`.
    category = tags[0] if tags else _category_from_name(name)

    filename = _filename_from_url(url)

    return {
        "id": book_id,
        "name": name,
        "title": title,
        "summary": summary,
        "language": language,
        "tags": tags,
        "size_bytes": size_bytes,
        "url": url,
        "updated": updated,
        "filename": filename,
        "category": category,
    }


def parse_feed(xml_text: str) -> list[dict]:
    """Parse a full OPDS feed and return the list of normalized books.

    Entries that fail to parse (missing URL or size) are silently
    dropped — the catalog is best-effort, partial is fine."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    books: list[dict] = []
    # Entries are direct children of the <feed> root.
    for entry in root.findall("atom:entry", _NS):
        book = parse_entry(entry)
        if book is not None:
            books.append(book)
    return books


def _filename_from_url(url: str) -> str:
    """Strip any .meta4 suffix so the on-complete hook sees a .zim."""
    name = url.rstrip("/").split("/")[-1]
    if name.endswith(".meta4"):
        name = name[: -len(".meta4")]
    return name


_NAME_PREFIX_RE = re.compile(r"^([a-z]+)_")


def _category_from_name(name: str) -> str:
    """Fallback: 'wikipedia_en_all_2024-01' -> 'wikipedia'. Matches
    Kiwix's content-prefix convention."""
    match = _NAME_PREFIX_RE.match(name or "")
    return match.group(1) if match else ""


# ---------- filter helpers (also pure, also unit-tested) ----------

def filter_books(
    books: list[dict],
    language: Optional[str] = None,
    category: Optional[str] = None,
    size_min: Optional[int] = None,
    size_max: Optional[int] = None,
    name_substring: Optional[str] = None,
) -> list[dict]:
    """Apply the catalog filters server-side. The catalog page lives
    client-side at 1500 entries, but we keep this here for diagnostic
    JSON queries and for unit tests that exercise the filter logic."""
    needle = (name_substring or "").lower().strip()
    out: list[dict] = []
    for book in books:
        if language and book.get("language") != language:
            continue
        if category and book.get("category") != category and category not in book.get("tags", []):
            continue
        size = book.get("size_bytes", 0)
        if size_min is not None and size < size_min:
            continue
        if size_max is not None and size > size_max:
            continue
        if needle:
            haystack = (book.get("title", "") + " " + book.get("name", "")).lower()
            if needle not in haystack:
                continue
        out.append(book)
    return out


def collect_facets(books: list[dict]) -> dict:
    """Build the dropdown options for the filter UI: unique sorted
    languages and categories with counts."""
    languages: dict[str, int] = {}
    categories: dict[str, int] = {}
    for book in books:
        lang = book.get("language") or "und"
        languages[lang] = languages.get(lang, 0) + 1
        cat = book.get("category") or "(other)"
        categories[cat] = categories.get(cat, 0) + 1
    return {
        "languages": sorted(
            ({"code": k, "count": v} for k, v in languages.items()),
            key=lambda x: (-x["count"], x["code"]),
        ),
        "categories": sorted(
            ({"name": k, "count": v} for k, v in categories.items()),
            key=lambda x: (-x["count"], x["name"]),
        ),
    }
