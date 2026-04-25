"""Tiny aria2 JSON-RPC client (E2-S3).

aria2c exposes a JSON-RPC 2.0 endpoint at http://127.0.0.1:6800/jsonrpc
when started with `--enable-rpc`. We only need a handful of methods:
addUri, tellActive, tellWaiting, tellStopped, pause, unpause, remove.

Stdlib-only — no `aiohttp` or `requests` dependency to keep the apt
footprint small. Calls are blocking; uvicorn runs single-worker so
this is fine.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

RPC_URL = "http://127.0.0.1:6800/jsonrpc"
RPC_SECRET_FILE = Path("/etc/prepperpi/aria2/secret.env")
RPC_TIMEOUT = 8


class Aria2Error(Exception):
    """Raised when the RPC call returns an error or can't reach the
    daemon. The admin routes catch this and turn it into a clean
    user-facing message."""


def _secret() -> str:
    """Read ARIA2_RPC_SECRET out of the env-formatted secret file."""
    try:
        text = RPC_SECRET_FILE.read_text()
    except OSError as exc:
        raise Aria2Error(f"can't read RPC secret: {exc}")
    for line in text.splitlines():
        if line.startswith("ARIA2_RPC_SECRET="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    raise Aria2Error("RPC secret file missing ARIA2_RPC_SECRET= line")


def _call(method: str, params: list) -> dict | list:
    """Issue one JSON-RPC call. The first param is always the auth
    token in `token:<secret>` form, prepended here."""
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": "x",
        "method": method,
        "params": [f"token:{_secret()}", *params],
    }).encode()
    req = urllib.request.Request(
        RPC_URL,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=RPC_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode())
    except urllib.error.URLError as exc:
        raise Aria2Error(f"can't reach aria2 RPC: {exc}")
    except json.JSONDecodeError as exc:
        raise Aria2Error(f"aria2 RPC returned non-JSON: {exc}")
    if "error" in payload:
        err = payload["error"]
        raise Aria2Error(f"aria2 RPC error {err.get('code')}: {err.get('message')}")
    return payload.get("result")


# ---------- public API ----------

def add_uri(urls, dest_dir: str, out: Optional[str] = None) -> str:
    """Queue a download. `urls` may be a single URL or a list of mirror
    URLs that all serve the same file; aria2 will use the first
    available and fall back to the rest. Returns the GID."""
    if isinstance(urls, str):
        urls = [urls]
    options: dict[str, str] = {"dir": dest_dir}
    if out:
        options["out"] = out
    gid = _call("aria2.addUri", [list(urls), options])
    if not isinstance(gid, str):
        raise Aria2Error(f"unexpected addUri result: {gid!r}")
    return gid


def pause(gid: str) -> None:
    """Pause and wait until aria2 has actually transitioned to the
    paused state. The pause RPC initiates the pause asynchronously;
    if we return immediately the user can fire `unpause` before
    aria2 has flushed the .aria2 control file, and the resume
    restarts from byte 0 instead of continuing. Polling for up to
    ~2s eliminates that race in practice."""
    _call("aria2.pause", [gid])
    for _ in range(20):
        try:
            status = _call("aria2.tellStatus", [gid, ["status"]])
        except Aria2Error:
            return
        if isinstance(status, dict) and status.get("status") in (
            "paused", "complete", "error", "removed"
        ):
            return
        time.sleep(0.1)


def unpause(gid: str) -> None:
    _call("aria2.unpause", [gid])


def remove(gid: str) -> None:
    """Cancel an active, waiting, or paused download. Force-removes
    so a stuck download doesn't block the queue. Has no effect on
    downloads that have already transitioned to the result list
    (complete/error/removed) — use `remove_result` for those."""
    try:
        _call("aria2.forceRemove", [gid])
    except Aria2Error:
        # forceRemove fails on already-finished/errored entries; that's
        # fine, the row just transitioned to the result list and the
        # UI will offer the Clear button instead.
        pass


def remove_result(gid: str) -> None:
    """Clear a completed/errored entry from the result list so it
    stops appearing in tellStopped output. The file (if any) stays
    on disk."""
    try:
        _call("aria2.removeDownloadResult", [gid])
    except Aria2Error:
        pass


# Status keys we care about. aria2 returns lots of fields; we keep
# the response small by asking for only these.
_KEYS = [
    "gid", "status", "totalLength", "completedLength",
    "downloadSpeed", "errorCode", "errorMessage", "files", "dir",
]


def _normalize(item: dict, dest_overrides: dict[str, str] | None = None) -> dict:
    """Translate aria2's verbose dict into our compact one. `files`
    is a list with one entry per output file; we surface the first."""
    files = item.get("files") or []
    out_path = files[0].get("path") if files else ""
    total = int(item.get("totalLength", "0") or 0)
    done = int(item.get("completedLength", "0") or 0)
    speed = int(item.get("downloadSpeed", "0") or 0)
    return {
        "gid": item.get("gid", ""),
        "status": item.get("status", ""),  # active/paused/waiting/complete/error/removed
        "total_bytes": total,
        "completed_bytes": done,
        "speed_bps": speed,
        "percent": round(done / total * 100.0, 1) if total else 0.0,
        "eta_seconds": int((total - done) / speed) if speed > 0 and total > done else None,
        "filename": out_path.rsplit("/", 1)[-1] if out_path else "",
        "dest_dir": item.get("dir", ""),
        "error_code": item.get("errorCode", ""),
        "error_message": item.get("errorMessage", ""),
    }


def list_all() -> list[dict]:
    """Return active + waiting + recently-stopped downloads, normalized."""
    active = _call("aria2.tellActive", [_KEYS])
    waiting = _call("aria2.tellWaiting", [0, 100, _KEYS])
    stopped = _call("aria2.tellStopped", [0, 50, _KEYS])
    items: list[dict] = []
    for group in (active or [], waiting or [], stopped or []):
        if isinstance(group, list):
            items.extend(_normalize(it) for it in group if isinstance(it, dict))
    return items


def get_version() -> dict:
    result = _call("aria2.getVersion", [])
    if isinstance(result, dict):
        return result
    return {}
