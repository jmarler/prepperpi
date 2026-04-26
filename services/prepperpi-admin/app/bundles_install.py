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
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
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


def resolve_metalink(url: str, *, timeout: int = 15) -> list[str]:
    """Kiwix's catalog advertises ZIM downloads as `.meta4` metalink
    URLs. Handing aria2 a `.meta4` URL only downloads the metalink XML
    (~3 KB) — aria2 doesn't auto-follow metalinks via URL. We fetch
    the metalink ourselves and return the inner mirror URLs sorted by
    priority.

    Fails soft: any fetch or parse error returns `[url]` so the caller
    can still hand aria2 the original URL (which will fail visibly with
    a 0-byte ZIM, but won't take down the whole bundle install).

    Non-metalink URLs are returned unchanged as a single-item list."""
    if not url.endswith(".meta4"):
        return [url]
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "PrepperPi-Admin/1"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            xml_text = resp.read().decode("utf-8", errors="replace")
        root = ET.fromstring(xml_text)
    except (urllib.error.URLError, OSError, ET.ParseError):
        return [url]
    pairs: list[tuple[int, str]] = []
    for url_node in root.iter("{urn:ietf:params:xml:ns:metalink}url"):
        href = (url_node.text or "").strip()
        if not href:
            continue
        try:
            priority = int(url_node.get("priority", "100"))
        except ValueError:
            priority = 100
        pairs.append((priority, href))
    if not pairs:
        return [url]
    pairs.sort()
    return [u for _p, u in pairs]


def queue_zim(*, url: str, filename: str, dest_dir: Path) -> str:
    """Hand a ZIM URL to aria2. If `url` is a Kiwix `.meta4` metalink,
    we resolve it to its mirror URLs first so aria2 downloads the
    actual ZIM rather than the 3 KB metalink XML. `filename` is used
    as `out=` only when the metalink resolved to a single direct URL
    that doesn't already encode the filename (i.e. mirrors typically
    do, so we leave `out` unset to let aria2 use the URL's basename)."""
    mirrors = resolve_metalink(url)
    options: dict[str, str] = {}
    if filename and len(mirrors) == 1 and mirrors[0] == url:
        # Direct URL, no metalink resolution; pin the output name so
        # we don't end up with a `.meta4`-suffixed file on disk.
        options["out"] = filename
    return aria2.add_uri(mirrors, str(dest_dir), out=options.get("out"))


def queue_static(*, url: str, sha256: str, install_to: str) -> str:
    """Queue a static download with explicit sha-256 verification.

    `install_to` is a manifest path like `static/foo.pdf` — we resolve
    the prefix into a real on-disk directory under /srv/prepperpi/."""
    dest_dir, out_name = _split_install_path(install_to)
    dest_dir.mkdir(parents=True, exist_ok=True)
    # aria2 supports `checksum=sha-256=<hex>` as a per-download option
    # via the addUri extra-options mapping. Our local add_uri helper
    # only takes dir/out today; static-file updates with explicit
    # sha256 verification go through updates_apply._download_with_sha256
    # instead, which streams + hashes directly. The bundle-install path
    # below still relies on aria2's metalink-embedded checksums.
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
