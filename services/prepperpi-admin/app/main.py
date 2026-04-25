"""prepperpi-admin — FastAPI app served behind Caddy at /admin/*.

This is the unprivileged side of the admin console: it renders the
forms and reads /boot/firmware/prepperpi.conf for display, but every
write goes through `sudo -n /opt/.../apply-network-config`. That
wrapper is the trust boundary; this process must be assumed
compromisable.

Caddy enforces network-level access (10.42.0.0/24 + localhost only,
per E4-S1 AC-5) before any request reaches us. We don't re-check
remote_addr here -- Caddy strips it before reverse-proxy and we
don't want to encode the AP-subnet CIDR in two places.
"""
from __future__ import annotations

import io
import json
import re
import subprocess
import tarfile
import time
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import health
from uplink import detect_uplink

APP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
CONF_FILE = Path("/boot/firmware/prepperpi.conf")
APPLY_CMD = "/opt/prepperpi/services/prepperpi-admin/apply-network-config"
STORAGE_CMD = "/opt/prepperpi/services/prepperpi-admin/apply-storage-action"
EVENTS_FILE = Path("/opt/prepperpi/web/landing/_events.json")
USB_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,255}$")

ALLOWED_COUNTRIES = sorted([
    "AT", "AU", "BE", "BR", "CA", "CH", "CL", "CN", "CO", "CZ",
    "DE", "DK", "ES", "FI", "FR", "GB", "GR", "HK", "HU", "IE",
    "IL", "IN", "IS", "IT", "JP", "KR", "MX", "MY", "NL", "NO",
    "NZ", "PH", "PL", "PT", "RO", "RU", "SE", "SG", "SK", "TH",
    "TR", "TW", "UA", "US", "VN", "ZA",
])

# Mirrored from the wrapper. Keep in sync; the wrapper is canonical.
SSID_RE = re.compile(r"^[A-Za-z0-9 \-_.()\[\]]{1,32}$")
WIFI_PASSWORD_RE = re.compile(r"^[\x20-\x7e]{0,63}$")


app = FastAPI(title="PrepperPi Admin", docs_url=None, redoc_url=None)
app.mount("/admin/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def read_config() -> dict[str, str]:
    """Parse the four keys we manage out of prepperpi.conf. Anything
    else in the file (MAX_STA, INTERFACE, future keys) is preserved
    by the wrapper but not surfaced in the form."""
    config = {"ssid": "", "wifi_password": "", "channel": "auto", "country": "US"}
    if not CONF_FILE.exists():
        return config
    try:
        for line in CONF_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key == "SSID":
                config["ssid"] = value
            elif key == "WIFI_PASSWORD":
                config["wifi_password"] = value
            elif key == "CHANNEL":
                config["channel"] = value or "auto"
            elif key == "COUNTRY":
                config["country"] = value or "US"
    except OSError:
        pass
    return config


def validate_locally(spec: dict) -> list[str]:
    """Mirror of the wrapper's validation. We re-check here so the
    user gets per-field feedback in the form without paying the
    sudo round-trip on obviously-invalid input."""
    errors: list[str] = []

    ssid = spec.get("ssid", "")
    if not ssid:
        errors.append("SSID is required.")
    elif len(ssid.encode("utf-8")) > 32:
        errors.append("SSID must be 32 bytes or fewer.")
    elif not SSID_RE.match(ssid):
        errors.append("SSID may only contain letters, digits, space, and -_.()[].")

    wifi_password = spec.get("wifi_password", "")
    if wifi_password and not (8 <= len(wifi_password) <= 63):
        errors.append("Wi-Fi password must be empty (open network) or 8-63 characters.")
    elif not WIFI_PASSWORD_RE.match(wifi_password):
        errors.append("Wi-Fi password contains characters that aren't allowed.")

    channel = spec.get("channel", "auto")
    if channel != "auto":
        try:
            ch = int(channel)
            if not (1 <= ch <= 13):
                errors.append("Channel must be Auto or 1-13.")
        except (TypeError, ValueError):
            errors.append("Channel must be Auto or 1-13.")

    if spec.get("country") not in ALLOWED_COUNTRIES:
        errors.append("Country must be a supported ISO code.")

    return errors


def call_wrapper(payload: dict, cmd: str = APPLY_CMD, timeout: int = 30) -> tuple[bool, str]:
    """Invoke a privileged worker via sudo. Returns (ok, message).
    Any non-zero exit propagates the wrapper's stderr so the user
    sees what specifically failed (e.g. hostapd refused to start with
    the new country code)."""
    try:
        proc = subprocess.run(
            ["sudo", "-n", cmd],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, "Applying took too long; check the journal."
    if proc.returncode == 0:
        return True, proc.stdout.strip() or "ok"
    return False, (proc.stderr or proc.stdout or "apply failed").strip()


def read_events_tail(limit: int = 20) -> dict:
    """Best-effort read of the shared events ring. Returns {version, events}.
    Empty payload is fine; the dashboard renders that as 'no events yet'."""
    try:
        data = json.loads(EVENTS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"version": 0, "events": []}
    if not isinstance(data, dict):
        return {"version": 0, "events": []}
    events = data.get("events") or []
    if not isinstance(events, list):
        events = []
    return {
        "version": int(data.get("version", 0)) if isinstance(data.get("version"), int) else 0,
        "events": events[-limit:],
    }


def health_snapshot() -> dict:
    """The dict served by /admin/health and passed into storage.html.
    Includes the most recent 20 events so the storage page only needs
    to poll one endpoint."""
    snap = health.snapshot()
    snap["events"] = read_events_tail(20)
    return snap


# ---------- routes ----------

@app.get("/admin/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.get("/admin/uplink")
async def uplink_state() -> dict:
    """JSON endpoint polled by admin.js to live-update the home banner.
    Same shape as the dict passed into the home template."""
    return detect_uplink()


@app.get("/admin", response_class=HTMLResponse)
@app.get("/admin/", response_class=HTMLResponse)
async def admin_home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "home.html",
        {"request": request, "active": "home", "uplink": detect_uplink()},
    )


@app.get("/admin/network", response_class=HTMLResponse)
async def network_get(
    request: Request,
    saved: Optional[str] = None,
    reset: Optional[str] = None,
) -> HTMLResponse:
    config = read_config()
    return templates.TemplateResponse(
        "network.html",
        {
            "request": request,
            "active": "network",
            "config": config,
            "channels": [str(n) for n in range(1, 14)],
            "countries": ALLOWED_COUNTRIES,
            "errors": [],
            "saved": saved == "1",
            "reset_done": reset == "1",
        },
    )


@app.post("/admin/network", response_class=HTMLResponse)
async def network_post(
    request: Request,
    ssid: str = Form(...),
    wifi_password: str = Form(""),
    channel: str = Form("auto"),
    country: str = Form("US"),
) -> HTMLResponse:
    spec = {
        "ssid": ssid,
        "wifi_password": wifi_password,
        "channel": channel,
        "country": country,
    }
    errors = validate_locally(spec)
    if errors:
        return templates.TemplateResponse(
            "network.html",
            {
                "request": request,
                "active": "network",
                "config": spec,
                "channels": [str(n) for n in range(1, 14)],
                "countries": ALLOWED_COUNTRIES,
                "errors": errors,
                "saved": False,
                "reset_done": False,
            },
            status_code=400,
        )
    payload = {"action": "set", **spec}
    ok, msg = call_wrapper(payload)
    if not ok:
        return templates.TemplateResponse(
            "network.html",
            {
                "request": request,
                "active": "network",
                "config": spec,
                "channels": [str(n) for n in range(1, 14)],
                "countries": ALLOWED_COUNTRIES,
                "errors": [f"Couldn't apply: {msg}"],
                "saved": False,
                "reset_done": False,
            },
            status_code=500,
        )
    # Post-Redirect-Get so a refresh doesn't re-submit.
    return RedirectResponse("/admin/network?saved=1", status_code=303)


@app.get("/admin/health")
async def health_state() -> dict:
    """Live snapshot polled by the storage page (E4-S2 AC-1, 1 Hz)."""
    return health_snapshot()


@app.get("/admin/storage", response_class=HTMLResponse)
async def storage_get(request: Request) -> HTMLResponse:
    """Server-side renders the page so no-JS clients still see all
    the stats. The JS poll then keeps the values fresh."""
    snap = health_snapshot()
    return templates.TemplateResponse(
        "storage.html",
        {
            "request": request,
            "active": "storage",
            "snap": snap,
            "format_uptime": health.format_uptime,
            "format_bytes": health.format_bytes,
        },
    )


@app.post("/admin/storage/usb/{name}/writable")
async def storage_usb_toggle(name: str, writable: bool = Form(...)):
    if not USB_NAME_RE.match(name):
        raise HTTPException(status_code=400, detail="invalid usb name")
    payload = {"action": "remount", "name": name, "writable": writable}
    ok, msg = call_wrapper(payload, cmd=STORAGE_CMD, timeout=15)
    if not ok:
        raise HTTPException(status_code=500, detail=msg)
    # Send the user back to the storage page; the JS poll will
    # pick up the new writable state on the next tick anyway.
    return RedirectResponse("/admin/storage", status_code=303)


@app.get("/admin/diagnostics")
async def diagnostics_tarball():
    """Produce a downloadable diagnostics tarball (E4-S2 AC-5).

    Streams a tar.gz containing the install log, recent journalctl
    output for our services, /proc/cpuinfo, /proc/meminfo, lsblk, df,
    ip route, and the current events ring. Wi-Fi password
    (/boot/firmware/prepperpi.conf) is intentionally NOT included.

    Read-only operation — no privileged escalation. The admin user
    is in the systemd-journal group so it can read journalctl for
    its own services.
    """
    sections: list[tuple[str, bytes]] = []

    def collect_file(member: str, path: Path) -> None:
        try:
            data = path.read_bytes()
        except OSError as exc:
            data = f"<unreadable: {exc}>\n".encode()
        sections.append((member, data))

    def collect_cmd(member: str, argv: list[str]) -> None:
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=False, timeout=10,
            )
            data = proc.stdout + (b"\n--- stderr ---\n" + proc.stderr if proc.stderr else b"")
        except (subprocess.TimeoutExpired, OSError) as exc:
            data = f"<command failed: {exc}>\n".encode()
        sections.append((member, data))

    collect_file("install.log", Path("/var/log/prepperpi/install.log"))
    collect_file("cpuinfo", Path("/proc/cpuinfo"))
    collect_file("meminfo", Path("/proc/meminfo"))
    collect_file("uptime", Path("/proc/uptime"))
    collect_file("os-release", Path("/etc/os-release"))
    collect_file("events.json", EVENTS_FILE)
    collect_cmd("df.txt", ["df", "-h"])
    collect_cmd("lsblk.txt", ["lsblk", "-f"])
    collect_cmd("ip-route.txt", ["ip", "-4", "route"])
    collect_cmd("ip-addr.txt", ["ip", "-4", "addr"])
    collect_cmd("journal-prepperpi.txt", [
        "journalctl", "-u", "prepperpi-*", "--since=24 hours ago",
        "--no-pager", "--output=short-iso",
    ])

    # README pointing operators at what's in here / what's deliberately not.
    readme = (
        "PrepperPi diagnostics bundle\n"
        f"Generated: {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}\n"
        "\n"
        "Includes:\n"
        "  - install.log (top-level installer log)\n"
        "  - cpuinfo, meminfo, uptime, os-release\n"
        "  - df / lsblk / ip route / ip addr snapshots\n"
        "  - journalctl output for prepperpi-* units (last 24h)\n"
        "  - events.json (the dashboard event ring)\n"
        "\n"
        "Excluded for privacy:\n"
        "  - /boot/firmware/prepperpi.conf (Wi-Fi password lives there)\n"
        "  - SSH host keys, authorized_keys, user data\n"
        "\n"
        "If you share this bundle for debugging, scan it first.\n"
    ).encode()
    sections.append(("README.txt", readme))

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        ts = int(time.time())
        for member, data in sections:
            info = tarfile.TarInfo(name=f"prepperpi-diag/{member}")
            info.size = len(data)
            info.mtime = ts
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
    buffer.seek(0)

    filename = f"prepperpi-diag-{time.strftime('%Y%m%dT%H%M%S')}.tar.gz"
    return StreamingResponse(
        buffer,
        media_type="application/gzip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/admin/network/reset", response_class=HTMLResponse)
async def network_reset(request: Request) -> HTMLResponse:
    ok, msg = call_wrapper({"action": "reset"})
    if not ok:
        config = read_config()
        return templates.TemplateResponse(
            "network.html",
            {
                "request": request,
                "active": "network",
                "config": config,
                "channels": [str(n) for n in range(1, 14)],
                "countries": ALLOWED_COUNTRIES,
                "errors": [f"Couldn't reset: {msg}"],
                "saved": False,
                "reset_done": False,
            },
            status_code=500,
        )
    return RedirectResponse("/admin/network?reset=1", status_code=303)
