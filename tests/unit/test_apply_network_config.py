"""Unit tests for the apply-network-config privileged worker.

Covers:
  - Input validation (incl. FCC channel/country matrix from AC-4).
  - Snapshot / restore / factory-reset helpers used by the rollback chain.
  - Top-level main() dispatch on action types and recovery paths
    (mocking subprocess.run to simulate restart_ap success/failure).

The worker is a CLI script with no .py extension (it's exec'd via sudo
from FastAPI), so we load it via importlib.

Run with:
    python3 tests/unit/test_apply_network_config.py
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_DIR = Path(__file__).resolve().parents[2]
WORKER_PATH = REPO_DIR / "services" / "prepperpi-admin" / "apply-network-config"

# The worker has no .py extension (it's exec'd via sudo), so we have to
# point importlib at a SourceFileLoader explicitly.
_loader = importlib.machinery.SourceFileLoader("apply_network_config", str(WORKER_PATH))
_spec = importlib.util.spec_from_loader("apply_network_config", _loader)
worker = importlib.util.module_from_spec(_spec)
_loader.exec_module(worker)


class ValidateSetTests(unittest.TestCase):
    def _spec(self, **overrides):
        base = {
            "ssid": "PrepperPi",
            "wifi_password": "password123",
            "channel": "auto",
            "country": "US",
        }
        base.update(overrides)
        return base

    def test_happy_path(self) -> None:
        self.assertEqual(worker.validate_set(self._spec()), [])

    def test_ssid_too_long(self) -> None:
        errors = worker.validate_set(self._spec(ssid="A" * 33))
        self.assertTrue(any("32 bytes" in e for e in errors))

    def test_ssid_with_metacharacters(self) -> None:
        errors = worker.validate_set(self._spec(ssid="bad ssid;rm -rf"))
        self.assertTrue(any("ssid:" in e for e in errors))

    def test_password_too_short(self) -> None:
        errors = worker.validate_set(self._spec(wifi_password="short"))
        self.assertTrue(any("8-63" in e for e in errors))

    def test_country_not_in_allowlist(self) -> None:
        errors = worker.validate_set(self._spec(country="XX"))
        self.assertTrue(any("country:" in e for e in errors))

    def test_fcc_country_with_channel_12(self) -> None:
        # AC-4 layer 1: FCC reg-domain countries forbid channel >= 12.
        errors = worker.validate_set(self._spec(country="US", channel=12))
        self.assertTrue(any("not allowed in US" in e for e in errors))

    def test_fcc_country_with_channel_13(self) -> None:
        errors = worker.validate_set(self._spec(country="CA", channel=13))
        self.assertTrue(any("not allowed in CA" in e for e in errors))

    def test_non_fcc_country_with_channel_13_ok(self) -> None:
        self.assertEqual(
            worker.validate_set(self._spec(country="JP", channel=13)),
            [],
        )

    def test_fcc_country_with_channel_11_ok(self) -> None:
        self.assertEqual(
            worker.validate_set(self._spec(country="US", channel=11)),
            [],
        )

    def test_fcc_country_with_auto_channel_ok(self) -> None:
        self.assertEqual(
            worker.validate_set(self._spec(country="US", channel="auto")),
            [],
        )


class SnapshotRestoreFactoryResetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.conf = Path(self.tmpdir.name) / "prepperpi.conf"
        self._patch = mock.patch.object(worker, "CONF_FILE", self.conf)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self.tmpdir.cleanup()

    def test_snapshot_returns_none_when_missing(self) -> None:
        self.assertIsNone(worker.snapshot_config())

    def test_snapshot_round_trip(self) -> None:
        original = b"SSID=Foo\nCHANNEL=6\nCOUNTRY=DE\n"
        self.conf.write_bytes(original)
        snap = worker.snapshot_config()
        self.assertEqual(snap, original)

        # Overwrite with junk, then restore.
        self.conf.write_bytes(b"GARBAGE")
        worker.restore_config(snap)
        self.assertEqual(self.conf.read_bytes(), original)

    def test_restore_none_deletes_file(self) -> None:
        self.conf.write_bytes(b"SSID=Foo\n")
        worker.restore_config(None)
        self.assertFalse(self.conf.exists())

    def test_factory_reset_removes_file(self) -> None:
        self.conf.write_bytes(b"SSID=Foo\n")
        worker.factory_reset_config()
        self.assertFalse(self.conf.exists())

    def test_factory_reset_when_missing_is_noop(self) -> None:
        # No file present; should not raise.
        worker.factory_reset_config()
        self.assertFalse(self.conf.exists())


class MainRollbackChainTests(unittest.TestCase):
    """Exercise the full layered recovery in main() by mocking restart_ap.

    The interesting paths are:
      A. restart_ap succeeds first try -> "ok", exit 0, conf has new content.
      B. apply restart fails, rollback restart succeeds -> exit 1,
         conf has prior content.
      C. apply + rollback restart both fail, factory-reset succeeds ->
         exit 1, conf removed.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.conf = Path(self.tmpdir.name) / "prepperpi.conf"
        self._patches = [
            mock.patch.object(worker, "CONF_FILE", self.conf),
            mock.patch.object(worker.sys, "stdin", io.StringIO()),
            mock.patch.object(worker.sys, "stdout", io.StringIO()),
            mock.patch.object(worker.sys, "stderr", io.StringIO()),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self.tmpdir.cleanup()

    def _run_set(self, **overrides):
        spec = {
            "action": "set",
            "ssid": "PrepperPi",
            "wifi_password": "password123",
            "channel": "auto",
            "country": "US",
        }
        spec.update(overrides)
        worker.sys.stdin = io.StringIO(__import__("json").dumps(spec))
        # isatty() is unavailable on StringIO; patch it.
        worker.sys.stdin.isatty = lambda: False
        return worker.main()

    def test_apply_succeeds_first_try(self) -> None:
        self.conf.write_bytes(b"SSID=Original\n")
        with mock.patch.object(worker.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0)
            rc = self._run_set(ssid="NewName")
        self.assertEqual(rc, 0)
        self.assertIn("SSID=NewName", self.conf.read_text())

    def test_rollback_succeeds_after_apply_failure(self) -> None:
        self.conf.write_bytes(b"SSID=Original\nCOUNTRY=US\n")
        # First call (the apply restart) fails; second (rollback restart) succeeds.
        outcomes = [
            worker.subprocess.CalledProcessError(1, "systemctl"),  # apply
            mock.Mock(returncode=0),                                # rollback restart x3
            mock.Mock(returncode=0),
            mock.Mock(returncode=0),
        ]
        def fake_run(*a, **kw):
            o = outcomes.pop(0)
            if isinstance(o, Exception):
                raise o
            return o
        with mock.patch.object(worker.subprocess, "run", side_effect=fake_run):
            rc = self._run_set(ssid="NewName")
        self.assertEqual(rc, 1)
        # Rolled-back conf == original snapshot.
        self.assertIn("SSID=Original", self.conf.read_text())

    def test_factory_reset_after_apply_and_rollback_fail(self) -> None:
        self.conf.write_bytes(b"SSID=Original\n")
        outcomes = [
            worker.subprocess.CalledProcessError(1, "systemctl"),  # apply
            worker.subprocess.CalledProcessError(1, "systemctl"),  # rollback
            mock.Mock(returncode=0),                                # factory-reset x3
            mock.Mock(returncode=0),
            mock.Mock(returncode=0),
        ]
        def fake_run(*a, **kw):
            o = outcomes.pop(0)
            if isinstance(o, Exception):
                raise o
            return o
        with mock.patch.object(worker.subprocess, "run", side_effect=fake_run):
            rc = self._run_set(ssid="NewName")
        self.assertEqual(rc, 1)
        self.assertFalse(self.conf.exists())

    def test_all_three_layers_fail(self) -> None:
        self.conf.write_bytes(b"SSID=Original\n")
        with mock.patch.object(
            worker.subprocess,
            "run",
            side_effect=worker.subprocess.CalledProcessError(1, "systemctl"),
        ):
            rc = self._run_set(ssid="NewName")
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
