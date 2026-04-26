"""bundle install orchestration.

Given a resolved Bundle, queue its items into the appropriate workers:
  - ZIM and static items go straight into aria2's queue (parallel,
    resumable, hash-verified by aria2 when checksums are known).
  - Map-region items append to /srv/prepperpi/maps/.queue.json. A
    detached `bundle-region-installer` worker drains that queue,
    spawning extract-region.sh for one region at a time so we don't
    fight the existing per-region install lock.

This module is the I/O wrapper. The pure pieces (queue mutation,
duplicate detection) are factored out so they can be unit-tested.
"""
from __future__ import annotations

import fcntl
import json
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import aria2

ZIM_BASE = Path("/srv/prepperpi/zim")
MAPS_DIR = Path("/srv/prepperpi/maps")
USER_CONTENT_BASE = Path("/srv/prepperpi/user-content")
STATIC_BASE = Path("/srv/prepperpi/static")
SERVICES_DIR = Path("/opt/prepperpi/services")

QUEUE_FILE = MAPS_DIR / ".queue.json"
QUEUE_WRITE_LOCK = MAPS_DIR / ".queue.write.lock"
DRAINER_SCRIPT = SERVICES_DIR / "prepperpi-admin" / "bundle-region-installer.py"


# ---------- pure queue helpers ----------


def queue_after_append(current: list[str], to_add: list[str]) -> list[str]:
    """Pure: dedupe-aware append. Items already in `current` are skipped;
    `to_add`'s relative order is preserved otherwise."""
    seen = set(current)
    out = list(current)
    for r in to_add:
        if r in seen:
            continue
        seen.add(r)
        out.append(r)
    return out


def queue_after_pop(current: list[str], head: str) -> list[str]:
    """Pure: pop head if it matches; otherwise return unchanged.
    Defensive against the queue being mutated between drainer's read
    and pop — only remove what we actually processed."""
    if current and current[0] == head:
        return current[1:]
    return list(current)


# ---------- file I/O with locking ----------


@contextmanager
def _queue_lock() -> Iterator[None]:
    """Brief flock around queue mutations. Prevents drainer + admin
    from clobbering each other's writes."""
    QUEUE_WRITE_LOCK.parent.mkdir(parents=True, exist_ok=True)
    fh = open(QUEUE_WRITE_LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    finally:
        fh.close()  # implicitly releases the flock


def read_queue() -> list[str]:
    try:
        return json.loads(QUEUE_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def write_queue(items: list[str]) -> None:
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items))
    tmp.replace(QUEUE_FILE)


def append_to_queue(region_ids: list[str]) -> list[str]:
    """Append region_ids to the queue (deduped). Returns the new queue."""
    if not region_ids:
        return read_queue()
    with _queue_lock():
        cur = read_queue()
        new = queue_after_append(cur, region_ids)
        write_queue(new)
        return new


def pop_queue_head(expected_head: str) -> list[str]:
    """Drainer call: pop the head if it matches `expected_head`. Returns
    the new queue."""
    with _queue_lock():
        cur = read_queue()
        new = queue_after_pop(cur, expected_head)
        write_queue(new)
        return new


# ---------- spawning the drainer ----------


def kick_drainer(log_path: Path) -> None:
    """Start a detached drainer if one isn't already running. The
    drainer takes its own singleton lock; if a drainer is already
    running, the new process exits cleanly."""
    if not DRAINER_SCRIPT.exists():
        return
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = log_path.open("ab")
    try:
        subprocess.Popen(
            ["/usr/bin/python3", str(DRAINER_SCRIPT)],
            stdout=fh,
            stderr=fh,
            stdin=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    finally:
        fh.close()


# ---------- aria2 queue helpers ----------


def aria2_in_flight_filenames() -> set[str]:
    """Filenames currently active/waiting/paused in aria2. Used to
    avoid double-queueing the same ZIM."""
    out: set[str] = set()
    try:
        rows = aria2.list_all()
    except aria2.Aria2Error:
        return out
    for r in rows:
        if r.get("status") in ("active", "waiting", "paused"):
            fn = r.get("filename")
            if fn:
                out.add(fn)
    return out


def queue_zim(*, url: str, filename: str, dest_dir: Path) -> str:
    """Hand a ZIM metalink (or direct URL) to aria2. The aria2 client
    serializes the metalink and verifies any embedded checksums."""
    return aria2.add_uri(url, str(dest_dir))


def queue_static(*, url: str, sha256: str, install_to: str) -> str:
    """Queue a static download with explicit sha-256 verification.

    `install_to` is a manifest path like `static/foo.pdf` — we resolve
    the prefix into a real on-disk directory under /srv/prepperpi/."""
    dest_dir, out_name = _split_install_path(install_to)
    dest_dir.mkdir(parents=True, exist_ok=True)
    # aria2 supports `checksum=sha-256=<hex>` as a per-download option
    # via the addUri extra-options mapping. Our local add_uri helper
    # only takes dir/out today; for v1 we still pass the URL through
    # and post-verify after download in the bundle-status checker.
    # TODO(E5-S2): teach aria2.add_uri about checksum options so the
    # daemon refuses to mark complete on hash mismatch.
    return aria2.add_uri(url, str(dest_dir), out=out_name)


def _split_install_path(install_to: str) -> tuple[Path, str]:
    """Map a manifest install_to like `static/foo.pdf` into
    (`/srv/prepperpi/static/`, `foo.pdf`). Roots are restricted by
    the schema validator so anything we see here is one of the
    allowed prefixes."""
    if install_to.startswith("static/"):
        rel = install_to[len("static/"):]
        return STATIC_BASE / Path(rel).parent, Path(rel).name
    if install_to.startswith("zim/static/"):
        rel = install_to[len("zim/static/"):]
        return ZIM_BASE / "static" / Path(rel).parent, Path(rel).name
    if install_to.startswith("user-content/"):
        rel = install_to[len("user-content/"):]
        return USER_CONTENT_BASE / Path(rel).parent, Path(rel).name
    # Schema validator should have rejected anything else; defensive
    # fallback that lands under STATIC_BASE.
    return STATIC_BASE, Path(install_to).name
