"""Unit tests for the admin-console uplink detector.

Pure stdlib — runs anywhere with Python 3.10+. No FastAPI required;
the parser lives in `app/uplink.py` precisely so we don't have to
install the whole FastAPI stack to test it.

Run with:
    python3 tests/unit/test_admin_uplink.py
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_DIR / "services" / "prepperpi-admin" / "app"))

from uplink import parse_uplink_routes  # noqa: E402


class ParseUplinkRoutesTests(unittest.TestCase):
    def test_no_routes_means_offline(self) -> None:
        self.assertEqual(parse_uplink_routes([]), {"ethernet": False})

    def test_eth0_default_route_means_ethernet_uplink(self) -> None:
        routes = [
            {"dst": "default", "gateway": "192.168.1.1", "dev": "eth0",
             "protocol": "dhcp", "metric": 100, "flags": []},
        ]
        result = parse_uplink_routes(routes)
        self.assertEqual(result, {
            "ethernet": True,
            "iface": "eth0",
            "gateway": "192.168.1.1",
        })

    def test_eth1_also_counts(self) -> None:
        # Multiple ethernet interfaces (e.g. USB-Ethernet) are still ethernet.
        routes = [{"dst": "default", "gateway": "10.0.0.1", "dev": "eth1"}]
        self.assertTrue(parse_uplink_routes(routes)["ethernet"])

    def test_wlan0_default_route_does_not_count(self) -> None:
        # The Pi's own AP interface should never be treated as uplink, even
        # if (somehow) a default route ends up pointing through it.
        routes = [{"dst": "default", "gateway": "10.42.0.1", "dev": "wlan0"}]
        self.assertEqual(parse_uplink_routes(routes), {"ethernet": False})

    def test_first_eth_wins_when_multiple_routes(self) -> None:
        routes = [
            {"dst": "default", "gateway": "10.42.0.1", "dev": "wlan0"},
            {"dst": "default", "gateway": "192.168.1.1", "dev": "eth0"},
        ]
        result = parse_uplink_routes(routes)
        self.assertTrue(result["ethernet"])
        self.assertEqual(result["iface"], "eth0")

    def test_missing_gateway_field_is_ok(self) -> None:
        # Direct on-link default routes (rare but legal) lack a gateway.
        routes = [{"dst": "default", "dev": "eth0"}]
        self.assertEqual(parse_uplink_routes(routes), {
            "ethernet": True,
            "iface": "eth0",
            "gateway": None,
        })

    def test_garbage_entries_are_ignored(self) -> None:
        # Defensive: malformed entries shouldn't crash the helper.
        self.assertEqual(parse_uplink_routes([{}, {"dev": ""}]), {"ethernet": False})


if __name__ == "__main__":
    unittest.main()
