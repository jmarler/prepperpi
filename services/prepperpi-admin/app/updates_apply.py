"""updates_apply — apply a single available update.

Per kind:
  - **zim**: pre-flight HEAD the new URL; check free space against the
    advertised content-length. If free >= new_size, queue side-by-side
    via aria2 (old file stays). Else require the user to confirm
    `delete_old=True`, in which case we unlink the old .zim first then
    queue the new one. Failure mid-transfer leaves the user without the
    old file — that trade-off is surfaced in the confirm dialog.
  - **map_region**: re-trigger the existing region-extract via the
    bundle queue + drainer pattern. extract-region.sh writes
    <region>.pmtiles atomically and rewrites the source-sidecar JSON.
  - **bundle**: refresh the source's manifests in-cache. The cached
    body becomes "current" and any future check shows it as not stale.
  - **static**: download to <install_to>.new with explicit sha256
    verification, then atomic-rename onto the live path.

Errors raise `UpdateError` with a user-facing message; the route
catches it and surfaces via flash redirect.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Optional

import aria2
import bundles_install
import updates
import updates_state

ZIM_DIR = bundles_install.ZIM_BASE
MAPS_DIR = bundles_install.MAPS_DIR


class UpdateError(Exception):
    """User-facing failure during an apply. Message is shown in the
    flash banner verbatim."""


# ---------- ZIM apply ----------


def apply_zim_update(
    *,
    book_id: str,
    current_filename: Optional[str],
    new_url: str,
    new_filename: str,
    delete_old: bool = False,
) -> str:
    """Queue a new ZIM via aria2. Returns a short summary message.

    `current_filename` may be None if the user-deleted the old file
    behind our back (we still allow the install).

    `delete_old=True` is the low-disk path — the caller has already
    shown a confirm dialog explaining the trade-off."""
    if not new_url:
        raise UpdateError("New ZIM URL is missing — refresh the catalog and retry.")
    if not new_filename:
        raise UpdateError("New ZIM filename is missing.")

    # Pre-flight HEAD: confirms reachability + gets size for the
    # disk-space check.
    head = updates.http_head(new_url)
    if head.get("error"):
        raise UpdateError(f"Couldn't reach the new ZIM URL: {head['error']}")
    advertised_size = int(head.get("content_length") or 0)

    # Filename collision: aria2 would error or rename the file. If the
    # new name equals the old name, do nothing — already current.
    if current_filename and current_filename == new_filename:
        return "Already up to date."

    free = _free_bytes(ZIM_DIR)

    if delete_old:
        if not current_filename:
            # Nothing to delete; treat as side-by-side.
            delete_old = False
        else:
            old_path = ZIM_DIR / current_filename
            if not old_path.is_file():
                # Already gone.
                delete_old = False

    if not delete_old and advertised_size and advertised_size > free:
        raise UpdateError(
            f"Not enough free space for side-by-side install "
            f"(need {_human(advertised_size)}, have {_human(free)}). "
            f"Re-submit with delete_old=1 to remove the old version first."
        )

    if delete_old and current_filename:
        old_path = ZIM_DIR / current_filename
        try:
            old_path.unlink()
        except OSError as exc:
            raise UpdateError(f"Couldn't remove old ZIM: {exc}") from exc

    # Resolve `.meta4` metalinks before handing to aria2 — feeding
    # aria2 the metalink URL alone just downloads the 3 KB XML.
    mirrors = bundles_install.resolve_metalink(new_url)
    aria2_out = new_filename if (len(mirrors) == 1 and mirrors[0] == new_url) else None
    try:
        aria2.add_uri(mirrors, str(ZIM_DIR), out=aria2_out)
    except aria2.Aria2Error as exc:
        raise UpdateError(f"aria2 refused the new ZIM: {exc}") from exc

    if delete_old:
        return f"Old ZIM removed; queued {new_filename} ({_human(advertised_size)})."
    return f"Queued {new_filename} side-by-side ({_human(advertised_size)})."


# ---------- Map-region apply ----------


def apply_region_update(*, region_id: str) -> str:
    """Re-extract the region. Reuses the bundle drainer queue so we
    never collide with an in-flight install on the same region."""
    if not region_id:
        raise UpdateError("Missing region_id.")
    bundles_install.append_to_queue([region_id])
    bundles_install.kick_drainer(
        Path("/var/lib/prepperpi/bundles/last-drainer.log")
    )
    return f"Queued re-extract for region {region_id}."


# ---------- Bundle manifest apply ----------


def apply_bundle_update(*, qualified_id: str, refresh_callback) -> str:
    """The "apply" for a bundle revision IS just refreshing the cache.
    `refresh_callback` is `_refresh_remote_sources` from main.py — we
    accept it as a parameter to keep this module free of FastAPI
    imports (so the unit tests don't have to load the whole app)."""
    if ":" not in qualified_id:
        raise UpdateError("Bundle id must be `<source>:<id>`.")
    errs = refresh_callback() or []
    if errs:
        raise UpdateError(
            f"Refresh completed with {len(errs)} error(s) — "
            f"see the Bundles page for details."
        )
    return "Bundle manifests refreshed."


# ---------- Static apply ----------


def apply_static_update(
    *,
    install_to: str,
    url: str,
    expected_sha256: str,
    expected_size: int,
) -> str:
    """Download to <install_to>.new, verify sha256, atomic-rename onto
    the live path. Failure leaves the old file untouched."""
    if not (install_to and url and expected_sha256):
        raise UpdateError("Static-update payload is incomplete.")
    dest_path = updates_state._resolve_static_install_path(install_to)
    if dest_path is None:
        raise UpdateError(f"install_to {install_to!r} is not under an allowed root.")
    dest_path.parent.mkdir(parents=True, exist_ok=True)

    # Pre-flight HEAD just to confirm reachability before we start
    # streaming. If it fails we keep the old file intact.
    head = updates.http_head(url)
    if head.get("error"):
        raise UpdateError(f"Couldn't reach {url}: {head['error']}")

    new_path = dest_path.with_suffix(dest_path.suffix + ".new")
    try:
        _download_with_sha256(url, new_path, expected_sha256, expected_size)
    except UpdateError:
        # Clean up the partial.
        try:
            new_path.unlink()
        except OSError:
            pass
        raise

    os.replace(new_path, dest_path)
    return f"Updated {install_to}."


def _download_with_sha256(
    url: str,
    dest: Path,
    expected_sha256: str,
    expected_size: int,
) -> None:
    """Stream URL → dest, computing sha256 on the fly. We don't go
    through aria2 here because aria2's metalink workflow doesn't fit
    this single-URL-with-one-known-hash pattern, and the static files
    in bundles are typically small enough that a urllib stream is
    fine."""
    import urllib.request
    h = hashlib.sha256()
    written = 0
    req = urllib.request.Request(url, headers={"User-Agent": "PrepperPi-Admin/1"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp, dest.open("wb") as fh:
            while True:
                block = resp.read(1024 * 1024)
                if not block:
                    break
                fh.write(block)
                h.update(block)
                written += len(block)
    except Exception as exc:  # noqa: BLE001 — surface any transport issue
        raise UpdateError(f"Download failed: {exc}") from exc

    actual = h.hexdigest()
    if actual != expected_sha256:
        raise UpdateError(
            f"Checksum mismatch (got {actual[:7]}, expected {expected_sha256[:7]})."
        )
    if expected_size and written != expected_size:
        # Don't fail outright — some upstreams gzip-on-the-wire — but
        # only if the hash also passed. We trust the hash.
        pass


# ---------- helpers ----------


def _free_bytes(path: Path) -> int:
    try:
        stats = os.statvfs(path)
    except OSError:
        return 0
    return stats.f_bavail * stats.f_frsize


def _human(n: int) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    f = float(n)
    for u in units:
        if f < 1024 or u == units[-1]:
            return f"{f:.1f} {u}"
        f /= 1024
    return f"{n} B"
