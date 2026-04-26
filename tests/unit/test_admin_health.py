"""Unit tests for app/health.py — the pure parsers used by the admin
Storage panel. I/O wrappers (cpu_percent, disks, etc.) are
tested implicitly during the deploy verification on a live Pi.

Run with:
    python3 tests/unit/test_admin_health.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_DIR / "services" / "prepperpi-admin" / "app"))

from health import (  # noqa: E402
    cpu_percent_from_samples,
    format_bytes,
    format_uptime,
    parse_cpu_total,
    parse_dnsmasq_leases,
    parse_meminfo,
    parse_thermal_millideg,
    parse_uptime,
)


class ParseMeminfoTests(unittest.TestCase):
    SAMPLE = """\
MemTotal:        8000000 kB
MemFree:          500000 kB
MemAvailable:    6000000 kB
Buffers:          200000 kB
Cached:          1500000 kB
"""

    def test_uses_memavailable_for_used_calc(self) -> None:
        result = parse_meminfo(self.SAMPLE)
        # 8 GB total, 6 GB available -> 2 GB "real" used -> 25%.
        self.assertEqual(result["total_bytes"], 8000000 * 1024)
        self.assertEqual(result["available_bytes"], 6000000 * 1024)
        self.assertEqual(result["used_bytes"], 2000000 * 1024)
        self.assertEqual(result["percent"], 25.0)

    def test_falls_back_to_memfree_when_memavailable_missing(self) -> None:
        sample = "MemTotal: 8000000 kB\nMemFree: 1000000 kB\n"
        result = parse_meminfo(sample)
        self.assertEqual(result["available_bytes"], 1000000 * 1024)

    def test_handles_empty_input(self) -> None:
        result = parse_meminfo("")
        self.assertEqual(result["total_bytes"], 0)
        self.assertEqual(result["percent"], 0.0)


class ParseCpuTotalTests(unittest.TestCase):
    def test_parses_aggregate_line_only(self) -> None:
        text = (
            "cpu  100 0 50 800 10 0 5 0 0 0\n"
            "cpu0 25 0 12 200 2 0 1 0 0 0\n"
        )
        idle, total = parse_cpu_total(text)
        # idle = 800 + 10 = 810; total = sum of all 10 fields
        self.assertEqual(idle, 810)
        self.assertEqual(total, 100 + 0 + 50 + 800 + 10 + 0 + 5 + 0 + 0 + 0)

    def test_returns_none_on_garbage(self) -> None:
        self.assertIsNone(parse_cpu_total("not a /proc/stat\n"))
        self.assertIsNone(parse_cpu_total("cpu  not numbers\n"))

    def test_handles_short_line(self) -> None:
        # Some kernels report fewer fields than 10; we need at least 4.
        self.assertIsNone(parse_cpu_total("cpu  10 20\n"))


class CpuPercentFromSamplesTests(unittest.TestCase):
    def test_50_percent_busy(self) -> None:
        # total moved 100, idle moved 50 -> 50% busy
        self.assertEqual(cpu_percent_from_samples((100, 200), (150, 300)), 50.0)

    def test_idle_only(self) -> None:
        # total moved 100, idle also moved 100 -> 0%
        self.assertEqual(cpu_percent_from_samples((100, 200), (200, 300)), 0.0)

    def test_no_movement(self) -> None:
        # both samples identical -> divide-by-zero guard
        self.assertEqual(cpu_percent_from_samples((100, 200), (100, 200)), 0.0)

    def test_counter_wrap(self) -> None:
        # Going backwards (shouldn't happen, but be defensive)
        self.assertEqual(cpu_percent_from_samples((200, 300), (100, 200)), 0.0)


class ParseUptimeTests(unittest.TestCase):
    def test_parses_first_float(self) -> None:
        self.assertEqual(parse_uptime("123456.78 100000.00\n"), 123456)

    def test_garbage_returns_zero(self) -> None:
        self.assertEqual(parse_uptime(""), 0)
        self.assertEqual(parse_uptime("nope"), 0)


class ParseThermalTests(unittest.TestCase):
    def test_basic(self) -> None:
        # 50°C = 50000 millideg
        self.assertEqual(parse_thermal_millideg("50000\n"), 50.0)

    def test_garbage_returns_none(self) -> None:
        self.assertIsNone(parse_thermal_millideg("warm"))


class ParseDnsmasqLeasesTests(unittest.TestCase):
    SAMPLE = (
        "1735689600 aa:bb:cc:dd:ee:01 10.42.0.50 phone *\n"
        "1735689700 aa:bb:cc:dd:ee:02 10.42.0.51 laptop *\n"
        "1735689800 aa:bb:cc:dd:ee:03 192.168.1.99 lan-thing *\n"
    )

    def test_filters_to_ap_subnet(self) -> None:
        # Two AP-subnet entries, one off-subnet entry that shouldn't count.
        self.assertEqual(parse_dnsmasq_leases(self.SAMPLE), 2)

    def test_empty(self) -> None:
        self.assertEqual(parse_dnsmasq_leases(""), 0)

    def test_custom_subnet_prefix(self) -> None:
        self.assertEqual(parse_dnsmasq_leases(self.SAMPLE, "192.168.1."), 1)


class FormatHelperTests(unittest.TestCase):
    def test_format_uptime_minutes(self) -> None:
        self.assertEqual(format_uptime(45), "45s")
        self.assertEqual(format_uptime(125), "2m")  # 125 // 60 = 2 mins
        self.assertEqual(format_uptime(3600 + 5 * 60), "1h 5m")
        self.assertEqual(format_uptime(86400 * 3 + 7200 + 60 * 5), "3d 2h 5m")

    def test_format_bytes(self) -> None:
        self.assertEqual(format_bytes(0), "0 B")
        self.assertEqual(format_bytes(999), "999 B")
        self.assertEqual(format_bytes(1500), "1.5 kB")
        self.assertEqual(format_bytes(2_500_000_000), "2.5 GB")


if __name__ == "__main__":
    unittest.main()
