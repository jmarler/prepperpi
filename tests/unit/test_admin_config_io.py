"""Unit tests for app/config_io.py — pure manifest build/parse for the
config export tarball.

Pure-stdlib. Run with:
    python3 tests/unit/test_admin_config_io.py
"""
from __future__ import annotations

import io
import json
import sys
import tarfile
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_DIR / "services" / "prepperpi-admin" / "app"))

from config_io import (  # noqa: E402
    CURRENT_SCHEMA_VERSION,
    MANIFEST_NAME,
    ConfigIOError,
    build_manifest,
    manifest_to_tarball_bytes,
    parse_tarball,
)


# ---------- build_manifest ----------


class BuildManifestTests(unittest.TestCase):
    def test_includes_required_fields(self):
        m = build_manifest(
            network={"ssid": "PrepperPi", "wifi_password": "hunter2hunter2",
                     "channel": "auto", "country": "US"},
            bundles=["official:starter"],
            host="prepperpi-1",
            now="2026-04-27T00:00:00Z",
        )
        self.assertEqual(m["schema_version"], CURRENT_SCHEMA_VERSION)
        self.assertEqual(m["created_at"], "2026-04-27T00:00:00Z")
        self.assertEqual(m["host"], "prepperpi-1")
        self.assertEqual(m["network"]["ssid"], "PrepperPi")
        self.assertEqual(m["bundles"], ["official:starter"])

    def test_normalizes_network_string_fields(self):
        # Defensive: wrapper coerces to str so ints from a malformed
        # caller don't slip through to the apply path.
        m = build_manifest(
            network={"ssid": 123, "wifi_password": None, "channel": 6, "country": 7},
            bundles=[],
            host="p",
            now="2026-04-27T00:00:00Z",
        )
        self.assertEqual(m["network"]["ssid"], "123")
        self.assertEqual(m["network"]["wifi_password"], "None")
        self.assertEqual(m["network"]["channel"], 6)  # passed through (int OR "auto" both legal)
        self.assertEqual(m["network"]["country"], "7")


# ---------- manifest_to_tarball_bytes ----------


class TarballRoundtripTests(unittest.TestCase):
    def _round_trip(self, manifest: dict) -> dict:
        blob = manifest_to_tarball_bytes(manifest, mtime=1700000000)
        return parse_tarball(blob)

    def test_simple_round_trip(self):
        m = build_manifest(
            network={"ssid": "X", "wifi_password": "abcdefgh",
                     "channel": "auto", "country": "US"},
            bundles=["official:starter", "community:medical"],
            host="prepperpi-abcd",
            now="2026-04-27T12:00:00Z",
        )
        decoded = self._round_trip(m)
        self.assertEqual(decoded, m)

    def test_tarball_only_contains_manifest(self):
        m = build_manifest(
            network={"ssid": "X", "wifi_password": "", "channel": "auto", "country": "US"},
            bundles=[], host="h", now="t",
        )
        blob = manifest_to_tarball_bytes(m, mtime=1700000000)
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            names = tf.getnames()
        self.assertEqual(names, [MANIFEST_NAME])

    def test_export_well_under_1mb(self):
        # AC-2: export is < 1 MB. Even with many bundles + a long
        # password, we should be tens of KB at the outside.
        m = build_manifest(
            network={"ssid": "X" * 32, "wifi_password": "p" * 63,
                     "channel": 11, "country": "US"},
            bundles=[f"src{i}:bundle{i}" for i in range(500)],
            host="prepperpi-abcd", now="2026-04-27T00:00:00Z",
        )
        blob = manifest_to_tarball_bytes(m)
        self.assertLess(len(blob), 100 * 1024)  # 100 KB ceiling for v1


# ---------- parse_tarball error paths ----------


def _make_blob(payload_bytes: bytes, name: str = MANIFEST_NAME,
               mode: int = 0o644, *, regular: bool = True) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(payload_bytes) if regular else 0
        info.mode = mode
        if regular:
            tar.addfile(info, io.BytesIO(payload_bytes))
        else:
            info.type = tarfile.DIRTYPE
            tar.addfile(info)
    return buf.getvalue()


class ParseTarballErrorPathTests(unittest.TestCase):
    def test_rejects_non_tarball_input(self):
        with self.assertRaises(ConfigIOError) as cm:
            parse_tarball(b"not a tarball at all")
        self.assertIn("Not a valid", str(cm.exception))

    def test_rejects_missing_manifest(self):
        # Tarball with a different filename inside.
        blob = _make_blob(b"{}", name="something_else.json")
        with self.assertRaises(ConfigIOError) as cm:
            parse_tarball(blob)
        self.assertIn("missing manifest.json", str(cm.exception))

    def test_rejects_invalid_json(self):
        blob = _make_blob(b"this is not json")
        with self.assertRaises(ConfigIOError) as cm:
            parse_tarball(blob)
        self.assertIn("not valid JSON", str(cm.exception))

    def test_rejects_non_object_root(self):
        blob = _make_blob(b'["just", "an", "array"]')
        with self.assertRaises(ConfigIOError) as cm:
            parse_tarball(blob)
        self.assertIn("must be a JSON object", str(cm.exception))

    def test_rejects_missing_schema_version(self):
        blob = _make_blob(json.dumps({"network": {}, "bundles": []}).encode())
        with self.assertRaises(ConfigIOError) as cm:
            parse_tarball(blob)
        self.assertIn("schema_version", str(cm.exception))

    def test_rejects_newer_schema_version(self):
        blob = _make_blob(json.dumps({
            "schema_version": CURRENT_SCHEMA_VERSION + 1,
            "network": {"ssid": "x"},
            "bundles": [],
        }).encode())
        with self.assertRaises(ConfigIOError) as cm:
            parse_tarball(blob)
        self.assertIn("Upgrade PrepperPi", str(cm.exception))

    def test_rejects_missing_network(self):
        blob = _make_blob(json.dumps({
            "schema_version": CURRENT_SCHEMA_VERSION,
            "bundles": [],
        }).encode())
        with self.assertRaises(ConfigIOError) as cm:
            parse_tarball(blob)
        self.assertIn("'network'", str(cm.exception))

    def test_rejects_non_string_bundles(self):
        blob = _make_blob(json.dumps({
            "schema_version": CURRENT_SCHEMA_VERSION,
            "network": {"ssid": "x"},
            "bundles": [{"qualified_id": "official:starter"}],
        }).encode())
        with self.assertRaises(ConfigIOError) as cm:
            parse_tarball(blob)
        self.assertIn("list of strings", str(cm.exception))

    def test_accepts_current_schema(self):
        blob = _make_blob(json.dumps({
            "schema_version": CURRENT_SCHEMA_VERSION,
            "network": {"ssid": "x"},
            "bundles": ["official:starter"],
        }).encode())
        decoded = parse_tarball(blob)
        self.assertEqual(decoded["bundles"], ["official:starter"])


if __name__ == "__main__":
    unittest.main()
