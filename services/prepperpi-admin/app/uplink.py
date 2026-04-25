"""Uplink detection for the admin home page (E4-S3).

Pulled out of `main.py` so the pure parsing logic can be unit-tested
without pulling FastAPI into the test environment.
"""
from __future__ import annotations

import json
import subprocess


def parse_uplink_routes(routes: list[dict]) -> dict:
    """Given parsed `ip -j route show default` output, return uplink state.

    Each route entry looks like:
        {"dst": "default", "gateway": "...", "dev": "eth0", ...}

    A default route via any interface whose name starts with "eth" counts
    as Ethernet uplink for E4-S3. wlan/USB-dongle uplinks are a future
    story; this detector intentionally ignores them.
    """
    for route in routes:
        dev = route.get("dev", "")
        if dev.startswith("eth"):
            return {
                "ethernet": True,
                "iface": dev,
                "gateway": route.get("gateway"),
            }
    return {"ethernet": False}


def detect_uplink() -> dict:
    """Read-only check. Cheap enough to call on every page render — no
    privileges required (we only read routing-table state)."""
    try:
        proc = subprocess.run(
            ["ip", "-j", "route", "show", "default"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (subprocess.TimeoutExpired, OSError):
        return {"ethernet": False}
    if proc.returncode != 0:
        return {"ethernet": False}
    try:
        routes = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        return {"ethernet": False}
    return parse_uplink_routes(routes)
