"""Unit tests for the Power page: the apply-power-action privileged
worker and the read-only app/power.py helper.

Covers:
  - set-button input validation (pin allowlist, bool/int confusion).
  - config.txt block round-trip: add, update in place, remove, and the
    idempotent no-op on an unchanged file.
  - Recovery from a truncated managed block (power loss mid-write).
  - Drift guard: app/power.py duplicates the worker's constants by
    design (trust boundary), so assert the two copies agree.
  - main() dispatch, including that an unknown action is rejected
    before anything privileged happens.

The worker is a CLI script with no .py extension (it's exec'd via sudo
from FastAPI), so we load it via importlib.

Run with:
    python3 tests/unit/test_admin_power.py
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_DIR = Path(__file__).resolve().parents[2]
ADMIN_DIR = REPO_DIR / "services" / "prepperpi-admin"
WORKER_PATH = ADMIN_DIR / "apply-power-action"

# The worker has no .py extension (it's exec'd via sudo), so we have to
# point importlib at a SourceFileLoader explicitly.
_loader = importlib.machinery.SourceFileLoader("apply_power_action", str(WORKER_PATH))
_spec = importlib.util.spec_from_loader("apply_power_action", _loader)
worker = importlib.util.module_from_spec(_spec)
_loader.exec_module(worker)

# The app-side helper is a plain module on the app path.
sys.path.insert(0, str(ADMIN_DIR / "app"))
import power  # noqa: E402


SAMPLE_CONFIG = """\
# For more options and information see rpi-config docs
[all]
dtparam=audio=on
camera_auto_detect=1
arm_64bit=1
"""


class ValidateSetButtonTests(unittest.TestCase):
    def test_default_pin_when_omitted(self) -> None:
        errors, enabled, gpio = worker.validate_set_button({"enabled": True})
        self.assertEqual(errors, [])
        self.assertTrue(enabled)
        self.assertEqual(gpio, worker.DEFAULT_GPIO)

    def test_allowed_pin_accepted(self) -> None:
        errors, enabled, gpio = worker.validate_set_button(
            {"enabled": True, "gpio": 17}
        )
        self.assertEqual(errors, [])
        self.assertTrue(enabled)
        self.assertEqual(gpio, 17)

    def test_disallowed_pin_rejected(self) -> None:
        # GPIO14 is the UART console; handing it to gpio-shutdown would
        # make serial traffic halt the Pi.
        errors, _, _ = worker.validate_set_button({"enabled": True, "gpio": 14})
        self.assertTrue(errors)
        self.assertIn("gpio", errors[0])

    def test_bool_is_not_an_acceptable_pin(self) -> None:
        # `True` is an int in Python; without an explicit isinstance
        # check it would be read as GPIO 1.
        errors, _, _ = worker.validate_set_button(
            {"enabled": True, "gpio": True}
        )
        self.assertTrue(errors)

    def test_string_pin_rejected(self) -> None:
        errors, _, _ = worker.validate_set_button(
            {"enabled": True, "gpio": "3"}
        )
        self.assertTrue(errors)

    def test_non_bool_enabled_rejected(self) -> None:
        errors, _, _ = worker.validate_set_button({"enabled": "yes"})
        self.assertTrue(errors)

    def test_disable_ignores_pin(self) -> None:
        errors, enabled, _ = worker.validate_set_button(
            {"enabled": False, "gpio": 999}
        )
        self.assertEqual(errors, [])
        self.assertFalse(enabled)


class ConfigTxtTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.path = Path(self.tmpdir.name) / "config.txt"
        self.path.write_text(SAMPLE_CONFIG)
        # `sync -f` is irrelevant on a tmpfs test file and not always
        # present on a dev Mac.
        patcher = mock.patch.object(worker.subprocess, "run")
        self.mock_run = patcher.start()
        self.addCleanup(patcher.stop)

    def test_enable_appends_block(self) -> None:
        worker.write_config_txt(self.path, True, 3)
        text = self.path.read_text()
        self.assertIn(worker.BLOCK_BEGIN, text)
        self.assertIn(worker.BLOCK_END, text)
        self.assertIn("dtoverlay=gpio-shutdown,gpio_pin=3", text)
        # The operator's pre-existing settings survive untouched.
        self.assertIn("arm_64bit=1", text)

    def test_enable_is_idempotent(self) -> None:
        worker.write_config_txt(self.path, True, 3)
        first = self.path.read_text()
        worker.write_config_txt(self.path, True, 3)
        self.assertEqual(self.path.read_text(), first)
        self.assertEqual(first.count(worker.BLOCK_BEGIN), 1)

    def test_changing_pin_replaces_rather_than_stacks(self) -> None:
        worker.write_config_txt(self.path, True, 3)
        worker.write_config_txt(self.path, True, 17)
        text = self.path.read_text()
        self.assertEqual(text.count(worker.BLOCK_BEGIN), 1)
        self.assertIn("gpio_pin=17", text)
        self.assertNotIn("gpio_pin=3", text)

    def test_disable_removes_block_and_restores_original(self) -> None:
        worker.write_config_txt(self.path, True, 3)
        worker.write_config_txt(self.path, False, 3)
        self.assertEqual(self.path.read_text(), SAMPLE_CONFIG)

    def test_disable_on_clean_file_is_a_noop(self) -> None:
        worker.write_config_txt(self.path, False, 3)
        self.assertEqual(self.path.read_text(), SAMPLE_CONFIG)

    def test_truncated_block_is_cleaned_up(self) -> None:
        # Power loss between writing the begin marker and the end
        # marker. The next write must not leave two half-blocks.
        self.path.write_text(
            SAMPLE_CONFIG + worker.BLOCK_BEGIN + "\ndtoverlay=gpio-shut"
        )
        worker.write_config_txt(self.path, True, 3)
        text = self.path.read_text()
        self.assertEqual(text.count(worker.BLOCK_BEGIN), 1)
        self.assertEqual(text.count(worker.BLOCK_END), 1)
        self.assertIn("gpio_pin=3", text)

    def test_chmod_refusal_does_not_fail_the_write(self) -> None:
        # /boot/firmware is vfat; chmod is refused there. Failing the
        # write over a mode we can't set anyway would mean the button
        # could never be enabled on a stock image.
        with mock.patch.object(Path, "chmod", side_effect=OSError("vfat")):
            worker.write_config_txt(self.path, True, 3)
        self.assertIn("gpio_pin=3", self.path.read_text())

    def test_failed_write_leaves_no_temp_file(self) -> None:
        with mock.patch.object(Path, "replace", side_effect=OSError("ENOSPC")):
            with self.assertRaises(OSError):
                worker.write_config_txt(self.path, True, 3)
        leftovers = list(self.path.parent.glob("*.prepperpi-tmp"))
        self.assertEqual(leftovers, [], "temp file left on the boot partition")
        # And the original is untouched.
        self.assertEqual(self.path.read_text(), SAMPLE_CONFIG)

    def test_file_without_trailing_newline(self) -> None:
        self.path.write_text("arm_64bit=1")
        worker.write_config_txt(self.path, True, 3)
        text = self.path.read_text()
        self.assertIn("arm_64bit=1\n", text)
        self.assertIn(worker.BLOCK_BEGIN, text)


class PowerModuleParseTests(unittest.TestCase):
    """app/power.py reads back what the worker writes."""

    def test_round_trip(self) -> None:
        text = SAMPLE_CONFIG + worker.render_block(17)
        state = power.parse_block(text)
        self.assertTrue(state["enabled"])
        self.assertEqual(state["gpio"], 17)

    def test_absent_block_reads_disabled(self) -> None:
        state = power.parse_block(SAMPLE_CONFIG)
        self.assertFalse(state["enabled"])
        self.assertEqual(state["gpio"], power.DEFAULT_GPIO)

    def test_hand_rolled_overlay_outside_block_is_not_claimed(self) -> None:
        # An operator's own gpio-shutdown line must not read as "we
        # manage this" -- otherwise the console would overwrite it.
        text = SAMPLE_CONFIG + "dtoverlay=gpio-shutdown,gpio_pin=27\n"
        state = power.parse_block(text)
        self.assertFalse(state["enabled"])

    def test_unparseable_pin_falls_back_to_default(self) -> None:
        text = (
            f"{power.BLOCK_BEGIN}\n"
            "dtoverlay=gpio-shutdown,gpio_pin=banana\n"
            f"{power.BLOCK_END}\n"
        )
        state = power.parse_block(text)
        self.assertTrue(state["enabled"])
        self.assertEqual(state["gpio"], power.DEFAULT_GPIO)


class ConstantDriftTests(unittest.TestCase):
    """app/power.py deliberately duplicates the worker's constants so
    the root-run worker imports nothing from the app tree. Duplication
    is only safe if it can't drift silently."""

    def test_markers_match(self) -> None:
        self.assertEqual(power.BLOCK_BEGIN, worker.BLOCK_BEGIN)
        self.assertEqual(power.BLOCK_END, worker.BLOCK_END)

    def test_gpio_allowlist_matches(self) -> None:
        self.assertEqual(set(power.ALLOWED_GPIOS), set(worker.ALLOWED_GPIOS))

    def test_default_gpio_matches(self) -> None:
        self.assertEqual(power.DEFAULT_GPIO, worker.DEFAULT_GPIO)

    def test_every_offered_pin_has_a_header_mapping(self) -> None:
        # The UI promises a physical pin number for each choice; a
        # missing entry would render "header pin None".
        for gpio in power.ALLOWED_GPIOS:
            self.assertIn(gpio, power.GPIO_TO_HEADER_PIN)

    def test_config_txt_candidates_match(self) -> None:
        self.assertEqual(
            list(power.CONFIG_TXT_CANDIDATES),
            list(worker.CONFIG_TXT_CANDIDATES),
        )


class MainDispatchTests(unittest.TestCase):
    def _run_main(self, payload: str) -> tuple[int, str, str]:
        stdin = io.StringIO(payload)
        stdin.isatty = lambda: False  # type: ignore[method-assign]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), \
             mock.patch.object(sys, "stdout", out), \
             mock.patch.object(sys, "stderr", err):
            rc = worker.main()
        return rc, out.getvalue(), err.getvalue()

    def test_unknown_action_rejected(self) -> None:
        rc, _, err = self._run_main('{"action": "format-everything"}')
        self.assertEqual(rc, 2)
        self.assertIn("unknown action", err)

    def test_non_object_payload_rejected(self) -> None:
        rc, _, err = self._run_main('["poweroff"]')
        self.assertEqual(rc, 2)
        self.assertIn("must be a JSON object", err)

    def test_invalid_json_rejected(self) -> None:
        rc, _, err = self._run_main("{not json")
        self.assertEqual(rc, 2)
        self.assertIn("invalid JSON", err)

    def test_tty_stdin_refused(self) -> None:
        stdin = io.StringIO("")
        stdin.isatty = lambda: True  # type: ignore[method-assign]
        err = io.StringIO()
        with mock.patch.object(sys, "stdin", stdin), \
             mock.patch.object(sys, "stderr", err):
            rc = worker.main()
        self.assertEqual(rc, 2)
        self.assertIn("TTY", err.getvalue())

    def test_poweroff_schedules_deferred_halt(self) -> None:
        with mock.patch.object(worker, "schedule_halt") as sched, \
             mock.patch.object(worker, "emit_event"):
            rc, out, _ = self._run_main('{"action": "poweroff"}')
        self.assertEqual(rc, 0)
        sched.assert_called_once_with("poweroff")
        self.assertIn("poweroff scheduled", out)

    def test_reboot_schedules_deferred_halt(self) -> None:
        with mock.patch.object(worker, "schedule_halt") as sched, \
             mock.patch.object(worker, "emit_event"):
            rc, _, _ = self._run_main('{"action": "reboot"}')
        self.assertEqual(rc, 0)
        sched.assert_called_once_with("reboot")

    def test_halt_deferred_not_immediate(self) -> None:
        # The deferral is what lets the confirmation page render. If
        # someone "simplifies" this to a direct systemctl call, the
        # browser gets a connection reset instead of instructions.
        self.assertGreater(worker.DEFER_SECONDS, 0)

    def test_double_click_reports_already_scheduled(self) -> None:
        # systemd's wording for a unit-name collision is "was already
        # loaded or has a fragment file". Matching the wrong substring
        # meant the operator saw a raw systemd error instead of a
        # sentence; caught on real hardware, so pin the real text.
        real_systemd_msg = (
            b"Failed to start transient timer unit: Unit "
            b"prepperpi-poweroff.timer was already loaded or has a "
            b"fragment file."
        )
        exc = subprocess.CalledProcessError(1, "systemd-run")
        exc.stderr = real_systemd_msg
        with mock.patch.object(worker, "schedule_halt", side_effect=exc), \
             mock.patch.object(worker, "emit_event"):
            rc, _, err = self._run_main('{"action": "poweroff"}')
        self.assertEqual(rc, 1)
        self.assertIn("already scheduled", err)
        self.assertNotIn("fragment file", err)

    def test_set_button_bad_pin_exits_two(self) -> None:
        rc, _, err = self._run_main('{"action": "set-button", '
                                    '"enabled": true, "gpio": 14}')
        self.assertEqual(rc, 2)
        self.assertIn("gpio", err)


if __name__ == "__main__":
    unittest.main()
