"""Unit tests for app/version_info.py — the parser for
/etc/prepperpi/image.version that the admin home footer reads.

Run with:
    python3 tests/unit/test_admin_version_info.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_DIR / "services" / "prepperpi-admin" / "app"))

from version_info import parse_image_version, read_image_version  # noqa: E402


class ParseImageVersionTests(unittest.TestCase):
    def test_parses_full_record(self) -> None:
        text = (
            "image_version=v1.2.3\n"
            "git_commit=abcdef0123456789abcdef0123456789abcdef01\n"
            "pigen_rev=4ad56cc850fa60adcc7f07dc15879bc95cc1d281\n"
            "built_at=2026-04-29T10:11:12Z\n"
        )
        result = parse_image_version(text)
        self.assertEqual(result["image_version"], "v1.2.3")
        self.assertEqual(result["git_commit"], "abcdef0123456789abcdef0123456789abcdef01")
        self.assertEqual(result["pigen_rev"], "4ad56cc850fa60adcc7f07dc15879bc95cc1d281")
        self.assertEqual(result["built_at"], "2026-04-29T10:11:12Z")

    def test_skips_blank_and_comment_lines(self) -> None:
        text = "\nimage_version=v1.0.0\n\ncomment without an equals\n"
        self.assertEqual(parse_image_version(text), {"image_version": "v1.0.0"})

    def test_preserves_value_whitespace_verbatim(self) -> None:
        # An operator hand-edit shouldn't get silently stripped.
        text = "built_at=  2026-04-29  \n"
        self.assertEqual(parse_image_version(text), {"built_at": "  2026-04-29  "})

    def test_empty_input_returns_empty_dict(self) -> None:
        self.assertEqual(parse_image_version(""), {})

    def test_forward_compat_extra_keys_pass_through(self) -> None:
        # Future fields shouldn't break the parser.
        text = "image_version=v2\nfuture_key=anything\n"
        result = parse_image_version(text)
        self.assertEqual(result["image_version"], "v2")
        self.assertEqual(result["future_key"], "anything")


class ReadImageVersionTests(unittest.TestCase):
    def test_missing_file_returns_empty_dict(self) -> None:
        # Host-side dev runs without the file present; this must not
        # raise. Using a path under /nonexistent so the OS reports
        # FileNotFoundError, which the reader is expected to swallow.
        result = read_image_version(Path("/nonexistent/prepperpi/image.version"))
        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
