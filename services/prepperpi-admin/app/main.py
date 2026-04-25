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

import json
import re
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from uplink import detect_uplink

APP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
CONF_FILE = Path("/boot/firmware/prepperpi.conf")
APPLY_CMD = "/opt/prepperpi/services/prepperpi-admin/apply-network-config"

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


def call_wrapper(payload: dict) -> tuple[bool, str]:
    """Invoke the privileged apply-network-config worker via sudo.
    Returns (ok, message). Any non-zero exit propagates the wrapper's
    stderr so the user sees what specifically failed (e.g. hostapd
    refused to start with the new country code)."""
    try:
        proc = subprocess.run(
            ["sudo", "-n", APPLY_CMD],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False, "Applying took too long; check the AP service journal."
    if proc.returncode == 0:
        return True, proc.stdout.strip() or "ok"
    return False, (proc.stderr or proc.stdout or "apply failed").strip()


# ---------- routes ----------

@app.get("/admin/healthz")
async def healthz() -> dict:
    return {"ok": True}


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
