#!/usr/bin/env python3
"""bundle-region-installer — drains the maps install queue.

Runs detached from the admin daemon. The admin handler appends
region_ids to /srv/prepperpi/maps/.queue.json when a bundle install
fires, then spawns this script. Exactly one drainer runs at a time
(enforced via flock); a second invocation while one is running exits
immediately, and the existing drainer picks up newly-queued regions
on its next loop iteration.

Per region:
  1. Pop the head of the queue if it's still our `head` (defensive
     against the queue being reordered between read and pop).
  2. Run extract-region.sh and wait. extract-region.sh's own lock
     serializes any concurrent manual installs from the maps page.
  3. Whether success or failure, pop the head and continue. Failures
     are logged via the drainer's own stderr; the per-region status
     JSON has the detailed failure reason.

Status: this script's stdout/stderr go to
/srv/prepperpi/maps/.status/last-drainer.log via systemd's stdout
redirection in the admin process that spawned us.
"""
from __future__ import annotations

import fcntl
import json
import subprocess
import sys
import time
from pathlib import Path

MAPS_DIR = Path("/srv/prepperpi/maps")
QUEUE_FILE = MAPS_DIR / ".queue.json"
SINGLETON_LOCK = MAPS_DIR / ".queue.singleton.lock"
WRITE_LOCK = MAPS_DIR / ".queue.write.lock"
EXTRACT_SCRIPT = "/opt/prepperpi/services/prepperpi-tiles/extract-region.sh"


def _read_queue() -> list[str]:
    try:
        return json.loads(QUEUE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _write_queue(items: list[str]) -> None:
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items))
    tmp.replace(QUEUE_FILE)


def _with_write_lock(fn):
    """Run `fn` inside the brief write lock used by both this script
    and the admin daemon when mutating the queue file."""
    WRITE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with open(WRITE_LOCK, "w") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        return fn()


def _peek_head() -> str | None:
    q = _with_write_lock(_read_queue)
    return q[0] if q else None


def _pop_if_head_matches(expected: str) -> None:
    def mutate() -> None:
        q = _read_queue()
        if q and q[0] == expected:
            _write_queue(q[1:])
    _with_write_lock(mutate)


def main() -> int:
    SINGLETON_LOCK.parent.mkdir(parents=True, exist_ok=True)
    singleton_fh = open(SINGLETON_LOCK, "w")
    try:
        fcntl.flock(singleton_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        # Another drainer is running. It will see our enqueued items.
        return 0

    while True:
        head = _peek_head()
        if head is None:
            return 0
        # Run extract-region.sh and wait. We don't propagate its exit
        # code: failure of one region shouldn't keep the whole queue
        # wedged. The per-region status JSON carries the failure
        # reason for the UI.
        try:
            subprocess.run(
                [EXTRACT_SCRIPT, head],
                stdin=subprocess.DEVNULL,
                check=False,
            )
        except FileNotFoundError:
            sys.stderr.write(
                f"bundle-region-installer: extract-region.sh missing at {EXTRACT_SCRIPT}\n"
            )
            return 1
        _pop_if_head_matches(head)
        # Tiny sleep so a runaway loop on a degenerate queue doesn't
        # hot-spin if extract-region.sh fails synchronously.
        time.sleep(0.5)


if __name__ == "__main__":
    sys.exit(main())
