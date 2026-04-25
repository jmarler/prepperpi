#!/usr/bin/env python3
"""prepperpi-events emit — append a single event to the dashboard
event log served at /opt/prepperpi/web/landing/_events.json.

Usage:
    emit-event.py <type> <message>

The events file is a JSON object:

    {
      "version": <monotonic counter>,
      "events": [
        {"id": <int>, "ts": "<iso>", "type": "<type>", "message": "<msg>"},
        ...
      ]
    }

The ring buffer keeps the last MAX_EVENTS entries. `version` is the
id of the most-recent event; the dashboard uses it to detect any
change since its last poll.

Atomic: serialized via fcntl flock on a /run lockfile, then written
to a temp file and renamed onto the final path.
"""
from __future__ import annotations

import datetime as _dt
import fcntl
import json
import os
import sys
from pathlib import Path

EVENTS_FILE = Path(os.environ.get(
    "PREPPERPI_EVENTS_FILE",
    "/opt/prepperpi/web/landing/_events.json",
))
LOCK_FILE = Path(os.environ.get(
    "PREPPERPI_EVENTS_LOCK",
    "/run/prepperpi/events.lock",
))
MAX_EVENTS = int(os.environ.get("PREPPERPI_EVENTS_MAX", "50"))


def _read_events() -> dict:
    if not EVENTS_FILE.exists():
        return {"version": 0, "events": []}
    try:
        data = json.loads(EVENTS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"version": 0, "events": []}
    if not isinstance(data, dict):
        return {"version": 0, "events": []}
    data.setdefault("version", 0)
    data.setdefault("events", [])
    if not isinstance(data["events"], list):
        data["events"] = []
    return data


def _write_events(data: dict) -> None:
    EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = EVENTS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")))
    tmp.chmod(0o644)
    os.replace(tmp, EVENTS_FILE)


def emit(event_type: str, message: str) -> int:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_FILE, "w") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        data = _read_events()
        new_id = int(data.get("version", 0)) + 1
        event = {
            "id": new_id,
            "ts": _dt.datetime.now(_dt.timezone.utc)
                              .strftime("%Y-%m-%dT%H:%M:%SZ"),
            "type": event_type,
            "message": message,
        }
        events = list(data.get("events", []))
        events.append(event)
        events = events[-MAX_EVENTS:]
        _write_events({"version": new_id, "events": events})
        return new_id


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        sys.stderr.write("usage: emit-event.py <type> <message>\n")
        return 2
    emit(argv[1], argv[2])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
