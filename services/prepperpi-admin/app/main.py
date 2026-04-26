"""prepperpi-admin — FastAPI app served behind Caddy at /admin/*.

This is the unprivileged side of the admin console: it renders the
forms and reads /boot/firmware/prepperpi.conf for display, but every
write goes through `sudo -n /opt/.../apply-network-config`. That
wrapper is the trust boundary; this process must be assumed
compromisable.

Caddy enforces network-level access (10.42.0.0/24 + localhost only)
before any request reaches us. We don't re-check
remote_addr here -- Caddy strips it before reverse-proxy and we
don't want to encode the AP-subnet CIDR in two places.
"""
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import aria2
import bundles as bundles_mod
import bundles_install
import catalog
import health
import maps
import updates as updates_mod
import updates_apply
import updates_state
from uplink import detect_uplink

APP_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"
CONF_FILE = Path("/boot/firmware/prepperpi.conf")
APPLY_CMD = "/opt/prepperpi/services/prepperpi-admin/apply-network-config"
STORAGE_CMD = "/opt/prepperpi/services/prepperpi-admin/apply-storage-action"
EVENTS_FILE = Path("/opt/prepperpi/web/landing/_events.json")
USB_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,255}$")

# Catalog cache. The admin daemon owns this directory because the
# prepperpi-admin systemd unit grants it ReadWritePaths=/srv/prepperpi/cache.
CATALOG_CACHE = Path("/srv/prepperpi/cache/kiwix-catalog.json")
# count=-1 returns the entire catalog in one response. The OPDS feed
# is small (~3MB for the full ~3000-book catalog as of writing), and a
# single fetch keeps the refresh path simple.
CATALOG_URL = "https://library.kiwix.org/catalog/v2/entries?count=-1"
CATALOG_FETCH_TIMEOUT = 60
CATALOG_USER_AGENT = "PrepperPi-Admin/1"
ZIM_BASE = Path("/srv/prepperpi/zim")
USB_BASE = Path("/srv/prepperpi/user-usb")

ALLOWED_COUNTRIES = sorted([
    "AT", "AU", "BE", "BR", "CA", "CH", "CL", "CN", "CO", "CZ",
    "DE", "DK", "ES", "FI", "FR", "GB", "GR", "HK", "HU", "IE",
    "IL", "IN", "IS", "IT", "JP", "KR", "MX", "MY", "NL", "NO",
    "NZ", "PH", "PL", "PT", "RO", "RU", "SE", "SG", "SK", "TH",
    "TR", "TW", "UA", "US", "VN", "ZA",
])

# Mirrored from apply-network-config. Keep in sync; the wrapper is canonical.
FCC_COUNTRIES = frozenset({"US", "CA", "MX"})

# Mirrored from the wrapper. Keep in sync; the wrapper is canonical.
SSID_RE = re.compile(r"^[A-Za-z0-9 \-_.()\[\]]{1,32}$")
WIFI_PASSWORD_RE = re.compile(r"^[\x20-\x7e]{0,63}$")


app = FastAPI(title="PrepperPi Admin", docs_url=None, redoc_url=None)
app.mount("/admin/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _stale_count_context(request: Request) -> dict:
    """Make `stale_count` available to every template so the nav-badge
    renders without each route having to thread it through. A missing
    or unreadable snapshot reports 0 — better than showing a stale badge
    forever if the file gets corrupted."""
    try:
        snap = updates_state.read_snapshot()
        return {"stale_count": int(snap.get("stale_count") or 0)}
    except Exception:
        return {"stale_count": 0}


templates = Jinja2Templates(
    directory=str(TEMPLATES_DIR),
    context_processors=[_stale_count_context],
)


_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


@app.middleware("http")
async def csrf_origin_guard(request: Request, call_next):
    # The admin console has no authentication; the Caddy network ACL
    # is the only access gate. That gate passes any request whose
    # client IP is on the AP subnet -- including a request that a
    # *victim's* browser auto-submitted because it was lured to an
    # off-network attacker's page. Compare Origin (or Referer if
    # Origin is absent) against this request's own Host so that
    # browser-driven cross-origin mutations are rejected. Non-browser
    # callers (curl, scripts) typically omit both headers; we let
    # those through, since the AP-subnet attacker that could send
    # them already has direct ACL-passing access and gains nothing
    # from CSRF.
    if request.method in _MUTATING_METHODS:
        host = request.headers.get("host", "")
        source = request.headers.get("origin") or request.headers.get("referer")
        if source:
            parsed = urlparse(source)
            if parsed.netloc != host:
                return PlainTextResponse(
                    "cross-origin request blocked",
                    status_code=403,
                )
    return await call_next(request)


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
    ch_int: Optional[int] = None
    if channel != "auto":
        try:
            ch_int = int(channel)
            if not (1 <= ch_int <= 13):
                errors.append("Channel must be Auto or 1-13.")
                ch_int = None
        except (TypeError, ValueError):
            errors.append("Channel must be Auto or 1-13.")

    country = spec.get("country")
    if country not in ALLOWED_COUNTRIES:
        errors.append("Country must be a supported ISO code.")

    if ch_int is not None and country in FCC_COUNTRIES and ch_int > 11:
        errors.append(
            f"Channel {ch_int} is not allowed in {country}; "
            f"that regulatory domain restricts 2.4 GHz to channels 1-11."
        )

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


# ---------- catalog cache + destination helpers ----------

def read_catalog_cache() -> dict:
    """Return {fetched_at, books, facets} or an empty placeholder if
    no refresh has been done yet."""
    if not CATALOG_CACHE.exists():
        return {"fetched_at": None, "books": [], "facets": {"languages": [], "categories": []}}
    try:
        data = json.loads(CATALOG_CACHE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"fetched_at": None, "books": [], "facets": {"languages": [], "categories": []}}
    if not isinstance(data, dict):
        return {"fetched_at": None, "books": [], "facets": {"languages": [], "categories": []}}
    data.setdefault("fetched_at", None)
    data.setdefault("books", [])
    data.setdefault("facets", {"languages": [], "categories": []})
    return data


def fetch_catalog() -> tuple[bool, str, int]:
    """Fetch + parse + cache the Kiwix catalog. Returns (ok, message,
    book_count). Requires Ethernet uplink — fails fast with a friendly
    error otherwise."""
    if not detect_uplink().get("ethernet"):
        return False, "No Ethernet uplink. Plug in a cable and try again.", 0

    req = urllib.request.Request(CATALOG_URL, headers={"User-Agent": CATALOG_USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=CATALOG_FETCH_TIMEOUT) as resp:
            xml_text = resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as exc:
        return False, f"Couldn't reach library.kiwix.org: {exc}", 0
    except (OSError, ValueError) as exc:
        return False, f"Catalog fetch failed: {exc}", 0

    books = catalog.parse_feed(xml_text)
    if not books:
        return False, "Catalog parse returned no books — upstream may have changed.", 0

    facets = catalog.collect_facets(books)
    payload = {
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "books": books,
        "facets": facets,
    }
    CATALOG_CACHE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CATALOG_CACHE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")))
    os.replace(tmp, CATALOG_CACHE)
    return True, "ok", len(books)


def destinations() -> list[dict]:
    """Available download destinations. MVP: internal storage only.

    USB destinations were tried during catalog development and pulled
    because the experience was unreliable — the combination of slow
    vfat-on-USB writes, aria2's metalink resume semantics, and the
    mount-namespace gymnastics needed to make aria2c's writes hit the
    right thing produced too many edge cases. Users wanting content
    on a USB drive can copy it from the SD after download, or pre-load
    the drive from a laptop. The USB write toggle on the Storage page
    is independent and stays in place for other manual write workflows."""
    out: list[dict] = []
    free = _free_bytes(ZIM_BASE)
    out.append({
        "id": "sd",
        "label": "Internal storage (SD card)",
        "path": str(ZIM_BASE),
        "free_bytes": free,
        "writable": True,
    })
    return out


def resolve_destination(dest_id: str) -> Optional[dict]:
    """Look up one destination by id. None if not found / not currently
    available (covers race where USB was just unplugged)."""
    for d in destinations():
        if d["id"] == dest_id:
            return d
    return None


def _free_bytes(path: Path) -> int:
    try:
        stats = os.statvfs(path)
    except OSError:
        return 0
    return stats.f_bavail * stats.f_frsize


def fetch_mirror_urls(meta4_url: str) -> list[str]:
    """Fetch + parse a Kiwix-style Metalink 4 file. Return the list of
    direct mirror URLs (already sorted by priority).

    Handing aria2 the mirror URLs directly sidesteps MirrorBrain's
    `Link: rel=describedby` redirect, which otherwise tricks aria2
    into downloading the .meta4 *as* the file (~3 KB of XML, where
    we wanted the .zim). Aria2's `follow-metalink=false` setting
    only affects metalinks given by URL, not ones discovered via
    HTTP response headers, so we have to bypass the discovery path
    entirely.
    """
    import xml.etree.ElementTree as ET
    try:
        req = urllib.request.Request(meta4_url, headers={"User-Agent": CATALOG_USER_AGENT})
        with urllib.request.urlopen(req, timeout=15) as resp:
            xml_text = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError) as exc:
        raise HTTPException(status_code=502, detail=f"can't fetch metalink: {exc}")

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise HTTPException(status_code=502, detail=f"metalink parse failed: {exc}")

    ns = {"m": "urn:ietf:params:xml:ns:metalink"}
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
    pairs.sort()
    return [u for _p, u in pairs]


def queue_summary() -> dict:
    """Build the JSON the catalog page polls at 1 Hz. Wraps aria2 so
    a daemon outage doesn't break the page — we just return an empty
    queue with an `error` field for the UI to surface."""
    try:
        items = aria2.list_all()
    except aria2.Aria2Error as exc:
        return {"items": [], "error": str(exc)}
    return {"items": items, "error": None}


# ---------- routes ----------

@app.get("/admin/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.get("/admin/uplink")
def uplink_state() -> dict:
    """JSON endpoint polled by admin.js to live-update the home banner.
    Same shape as the dict passed into the home template."""
    return detect_uplink()


@app.get("/admin", response_class=HTMLResponse)
@app.get("/admin/", response_class=HTMLResponse)
def admin_home(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        "home.html",
        {"request": request, "active": "home", "uplink": detect_uplink()},
    )


@app.get("/admin/network", response_class=HTMLResponse)
def network_get(
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
def network_post(
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
def health_state() -> dict:
    """Live snapshot polled by the storage page (1 Hz)."""
    return health_snapshot()


@app.get("/admin/storage", response_class=HTMLResponse)
def storage_get(request: Request) -> HTMLResponse:
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
def storage_usb_toggle(name: str, writable: bool = Form(...)):
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
def diagnostics_tarball():
    """Produce a downloadable diagnostics tarball.

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


# ---------- catalog page ----------

@app.get("/admin/catalog", response_class=HTMLResponse)
def catalog_get(
    request: Request,
    refreshed: Optional[str] = None,
    refresh_error: Optional[str] = None,
) -> HTMLResponse:
    cache = read_catalog_cache()
    return templates.TemplateResponse(
        "catalog.html",
        {
            "request": request,
            "active": "catalog",
            "fetched_at": cache.get("fetched_at"),
            "book_count": len(cache.get("books") or []),
            "destinations": destinations(),
            "refreshed": refreshed == "1",
            "refresh_error": refresh_error,
            "format_bytes": health.format_bytes,
        },
    )


@app.post("/admin/catalog/refresh")
def catalog_refresh():
    ok, msg, count = fetch_catalog()
    if ok:
        return RedirectResponse(f"/admin/catalog?refreshed=1", status_code=303)
    # Bounce back with the error in the query string so the user sees
    # a flash banner instead of a raw 5xx.
    from urllib.parse import quote
    return RedirectResponse(
        f"/admin/catalog?refresh_error={quote(msg)}",
        status_code=303,
    )


@app.get("/admin/catalog/data")
def catalog_data() -> dict:
    """Full catalog payload — books + facets + last-refresh timestamp.
    Polled once per page-load by catalog.js (the dataset is too big to
    inline into the HTML, ~1500 books)."""
    return read_catalog_cache()


@app.get("/admin/downloads")
def downloads_get() -> dict:
    """1 Hz queue snapshot for the catalog page's progress section."""
    return queue_summary()


@app.post("/admin/downloads/queue")
def downloads_queue(
    book_id: str = Form(...),
    destination_id: str = Form(...),
):
    """Queue one ZIM for download. Validates against the catalog cache
    AND the live destination list (drive may have been unplugged)."""
    cache = read_catalog_cache()
    book = next((b for b in cache.get("books") or [] if b.get("id") == book_id), None)
    if book is None:
        raise HTTPException(status_code=404, detail="book not in current catalog")

    dest = resolve_destination(destination_id)
    if dest is None:
        raise HTTPException(status_code=400, detail="destination unavailable")

    # Refuse duplicates of an in-flight download. If we let aria2 take
    # the same URL twice it tries to write the same staging file from
    # two sources; one aborts and cleans up the partial, which kills
    # the OTHER download's resume state too. Cleared rows (status =
    # complete / error / removed) don't block — the user can re-queue
    # them after deleting the result.
    try:
        active = aria2.list_all()
    except aria2.Aria2Error:
        active = []
    for it in active:
        if it.get("status") in ("active", "waiting", "paused"):
            if it.get("filename") == book["filename"]:
                raise HTTPException(
                    status_code=409,
                    detail=f"'{book['filename']}' is already in the queue.",
                )

    # Refuse if the file already exists at the destination (already
    # downloaded). User can delete it from the destination first if
    # they really want to redownload.
    final_path = Path(dest["path"]) / book["filename"]
    if final_path.exists():
        raise HTTPException(
            status_code=409,
            detail=(
                f"'{book['filename']}' is already on {dest['label']}. "
                f"Delete it from the destination first if you want to redownload."
            ),
        )

    # Pre-queue space check: warn the user before they start a download
    # that won't fit.
    if book["size_bytes"] > dest["free_bytes"]:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{health.format_bytes(book['size_bytes'])} won't fit in "
                f"{dest['label']} ({health.format_bytes(dest['free_bytes'])} free)."
            ),
        )

    # aria2c auto-creates the destination dir at download time
    # (default --auto-create-dir=true), running as the `prepperpi`
    # user which owns /srv/prepperpi/zim. No mkdir needed here.
    staging = f"{dest['path']}/.downloading"

    # Resolve the metalink to direct mirror URLs ourselves. The OPDS
    # feed advertises a `.zim.meta4` acquisition link, and aria2's
    # default behaviour with metalinks (or even Link: rel=describedby
    # discovered ones — `follow-metalink=false` doesn't suppress that
    # path) is to split the download into a parent + child GID dance
    # that breaks our pause/resume tracking. By fetching the metalink
    # and handing aria2 the raw mirror URLs, the download is a single
    # GID that pause/resume target correctly.
    if book["url"].endswith(".meta4"):
        mirror_urls = fetch_mirror_urls(book["url"])
        if not mirror_urls:
            raise HTTPException(
                status_code=502,
                detail="metalink had no mirror URLs",
            )
    else:
        mirror_urls = [book["url"]]

    try:
        gid = aria2.add_uri(mirror_urls, dest_dir=staging, out=book["filename"])
    except aria2.Aria2Error as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return {"gid": gid}


def _aria2_action_response(request: Request, gid_action) -> dict | RedirectResponse:
    """Run an aria2 mutation with shared error handling. Negotiates
    response format: callers asking for JSON (the live-update JS) get
    `{"ok": True}`; plain form posts get a 303 redirect back to the
    Catalog page so a no-JS click never hands the user a raw JSON blob."""
    try:
        gid_action()
    except aria2.Aria2Error as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    accept = request.headers.get("accept") or ""
    if "application/json" in accept:
        return {"ok": True}
    return RedirectResponse("/admin/catalog", status_code=303)


@app.post("/admin/downloads/{gid}/pause")
def downloads_pause(request: Request, gid: str):
    if not re.match(r"^[A-Za-z0-9]{1,32}$", gid):
        raise HTTPException(status_code=400, detail="invalid gid")
    return _aria2_action_response(request, lambda: aria2.pause(gid))


@app.post("/admin/downloads/{gid}/resume")
def downloads_resume(request: Request, gid: str):
    if not re.match(r"^[A-Za-z0-9]{1,32}$", gid):
        raise HTTPException(status_code=400, detail="invalid gid")
    return _aria2_action_response(request, lambda: aria2.unpause(gid))


@app.post("/admin/downloads/{gid}/cancel")
def downloads_cancel(request: Request, gid: str):
    if not re.match(r"^[A-Za-z0-9]{1,32}$", gid):
        raise HTTPException(status_code=400, detail="invalid gid")
    return _aria2_action_response(request, lambda: aria2.remove(gid))


@app.post("/admin/downloads/{gid}/clear")
def downloads_clear(request: Request, gid: str):
    """Clear a finished (complete or error) download from the queue
    history. Different from `cancel` which targets in-flight downloads;
    this just removes the bookkeeping entry. The actual file (if any)
    stays at the destination."""
    if not re.match(r"^[A-Za-z0-9]{1,32}$", gid):
        raise HTTPException(status_code=400, detail="invalid gid")
    return _aria2_action_response(request, lambda: aria2.remove_result(gid))


@app.get("/admin/maps", response_class=HTMLResponse)
def maps_get(request: Request, ok: Optional[str] = None, err: Optional[str] = None) -> HTMLResponse:
    """List installed map regions and offer per-region delete.

    Reads the regions JSON the reindex service maintains; never opens
    .mbtiles files itself. The reindex script is the single writer of
    that JSON, so the admin process can stay narrow.
    """
    regions = maps.enrich_with_catalog_names(maps.read_regions())
    flash = None
    if ok:
        flash = {"kind": "ok", "message": ok}
    elif err:
        flash = {"kind": "err", "message": err}
    return templates.TemplateResponse(
        "maps.html",
        {
            "request": request,
            "active": "maps",
            "regions": regions,
            "total_size_human": maps.human_size(maps.total_size_bytes(regions)),
            "flash": flash,
        },
    )


@app.get("/admin/maps/data")
def maps_data() -> dict:
    """Cheap JSON snapshot for any future polled UI. Polled-once today."""
    regions = maps.read_regions()
    return {
        "regions": regions,
        "count": len(regions),
        "total_size_bytes": maps.total_size_bytes(regions),
    }


@app.post("/admin/maps/{region_id}/delete")
def maps_delete(region_id: str):
    """Remove one region's .mbtiles or .pmtiles. The path-watcher fires
    the reindex asynchronously (~1s); the user sees the new state on
    the redirect target, which re-reads the regions JSON."""
    ok, msg = maps.delete_region(region_id)
    qs = ("ok=" + msg) if ok else ("err=" + msg)
    return RedirectResponse(url=f"/admin/maps?{qs}", status_code=303)


@app.get("/admin/maps/catalog")
def maps_catalog() -> dict:
    """Return the catalog of available regions for the install UI.

    Enriches each country with `installed: true/false` so the UI can
    render the right state without a second call. Also includes free
    disk space so the UI can flag oversized picks.
    """
    catalog = maps.read_catalog()
    installed = maps.installed_region_ids()
    countries = []
    for c in catalog.get("countries", []):
        if not isinstance(c, dict):
            continue
        cid = c.get("id", "")
        countries.append({
            **c,
            "installed": cid in installed,
            "estimated_human": maps.human_size(int(c.get("estimated_bytes") or 0)),
        })
    return {
        "version": catalog.get("version"),
        "source_url": catalog.get("source_url"),
        "source_attribution": catalog.get("source_attribution"),
        "bundles": catalog.get("bundles", []),
        "countries": countries,
        "free_space_bytes": maps.free_space_bytes(),
        "free_space_human": maps.human_size(maps.free_space_bytes()),
    }


@app.get("/admin/maps/install/status")
def maps_install_status() -> dict:
    """Snapshot of the active or last-completed install. Polled at 1Hz
    by the maps page when an install is running."""
    status = maps.read_install_status()
    if status is None:
        return {"status": "idle"}
    return status


@app.post("/admin/maps/install")
def maps_install_start(region_id: str = Form(...)):
    """Spawn extract-region.sh for one country. Returns 202 on success,
    409 if another install is running, 400 on validation, 507 if disk
    space insufficient."""
    ok, msg, status = maps.start_install(region_id)
    if ok:
        return {"status": "starting", "message": msg, "snapshot": status}
    # Map common failure modes to status codes the UI can branch on.
    code = 400
    if "already installed" in msg.lower():
        code = 409
    elif "already in progress" in msg.lower() or "another install" in msg.lower():
        code = 409
    elif "not enough free space" in msg.lower():
        code = 507
    raise HTTPException(status_code=code, detail=msg)


@app.post("/admin/maps/install/cancel")
def maps_install_cancel():
    """SIGTERM the running install. Idempotent — returns 200 with a
    descriptive message even if there's nothing to cancel, so the UI
    doesn't have to handle 4xx for an idle state."""
    ok, msg = maps.cancel_install()
    return {"ok": ok, "message": msg}


@app.post("/admin/network/reset", response_class=HTMLResponse)
def network_reset(request: Request) -> HTMLResponse:
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


# ---------- bundles page ----------

BUNDLE_SOURCES_FILE = Path("/etc/prepperpi/bundles/sources.json")
BUNDLE_BUILTIN_INDEX = Path("/opt/prepperpi/bundles/builtin/index.json")
BUNDLE_BUILTIN_DIR = Path("/opt/prepperpi/bundles/builtin")
BUNDLE_CACHE_DIR = Path("/var/lib/prepperpi/bundles")
REGION_CATALOG_FILE = Path("/opt/prepperpi/services/prepperpi-tiles/regions.json")


def _read_region_catalog() -> dict:
    """Load the static maps catalog the appliance ships."""
    try:
        return json.loads(REGION_CATALOG_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"regions": []}


def _internal_free_bytes() -> int:
    free = _free_bytes(ZIM_BASE)
    return int(free or 0)


def _load_builtin_bundles() -> tuple[list[bundles_mod.Bundle], list[str]]:
    """Load the bundles baked into the image. Returns (bundles, errors)."""
    out: list[bundles_mod.Bundle] = []
    errs: list[str] = []
    try:
        idx_text = BUNDLE_BUILTIN_INDEX.read_text()
    except OSError as exc:
        errs.append(f"builtin index missing: {exc}")
        return out, errs
    try:
        _, manifest_stubs = bundles_mod.parse_index(idx_text)
    except bundles_mod.ManifestError as exc:
        errs.append(f"builtin index parse failed: {exc}")
        return out, errs
    for stub in manifest_stubs:
        path = BUNDLE_BUILTIN_DIR / stub["url"]
        try:
            text = path.read_text()
        except OSError as exc:
            errs.append(f"builtin manifest {stub['id']}: {exc}")
            continue
        try:
            b = bundles_mod.parse_manifest(
                text, source_id="official", source_name="Official (builtin)"
            )
            out.append(b)
        except bundles_mod.ManifestError as exc:
            errs.append(f"builtin manifest {stub['id']}: {exc}")
    return out, errs


def _load_remote_bundles() -> tuple[list[bundles_mod.Bundle], list[str]]:
    """Walk every enabled source from sources.json and load its
    manifests. On a fresh Pi (no refresh done) the cache dir is empty;
    we fall back to builtin only and the user can hit Refresh to fetch.

    Cache layout:
        /var/lib/prepperpi/bundles/<source-id>/index.json
        /var/lib/prepperpi/bundles/<source-id>/manifests/*.yaml
    """
    out: list[bundles_mod.Bundle] = []
    errs: list[str] = []
    sources = _read_sources()
    for src in sources:
        if not src.enabled:
            continue
        if src.builtin and src.id == "official":
            # Already loaded from BUNDLE_BUILTIN_DIR; remote refresh
            # overlays into a per-source cache dir.
            continue
        cache_idx = BUNDLE_CACHE_DIR / src.id / "index.json"
        if not cache_idx.exists():
            continue
        try:
            idx_text = cache_idx.read_text()
            src_name, stubs = bundles_mod.parse_index(idx_text)
        except (OSError, bundles_mod.ManifestError) as exc:
            errs.append(f"{src.id}: {exc}")
            continue
        for stub in stubs:
            path = BUNDLE_CACHE_DIR / src.id / stub["url"]
            try:
                text = path.read_text()
                b = bundles_mod.parse_manifest(
                    text,
                    source_id=src.id,
                    source_name=src_name or src.name or src.id,
                )
                out.append(b)
            except (OSError, bundles_mod.ManifestError) as exc:
                errs.append(f"{src.id}/{stub['id']}: {exc}")
    return out, errs


def _read_sources() -> list[bundles_mod.Source]:
    try:
        return bundles_mod.parse_sources_config(BUNDLE_SOURCES_FILE.read_text())
    except OSError:
        return []


def _refresh_remote_sources() -> list[str]:
    """Fetch index.json + manifests for every enabled non-builtin
    source and write them to the cache. Returns a list of error
    strings; empty list = full success."""
    errs: list[str] = []
    BUNDLE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    for src in _read_sources():
        if not src.enabled:
            continue
        if src.builtin:
            # Refresh the builtin source IF its url is reachable; the
            # baked /opt/.../builtin/ copy is never overwritten — we
            # cache the latest under a per-source dir and merge at
            # render time so a future S2 can compare versions.
            pass
        try:
            idx_text = bundles_mod.fetch_text(src.url)
        except (urllib.error.URLError, ValueError, OSError) as exc:
            errs.append(f"{src.id}: fetch index — {exc}")
            continue
        try:
            _, stubs = bundles_mod.parse_index(idx_text)
        except bundles_mod.ManifestError as exc:
            errs.append(f"{src.id}: index parse — {exc}")
            continue
        src_dir = BUNDLE_CACHE_DIR / src.id
        manifests_dir = src_dir / "manifests"
        manifests_dir.mkdir(parents=True, exist_ok=True)
        # Write index first so a partial failure mid-loop doesn't
        # leave the source unreadable.
        (src_dir / "index.json").write_text(idx_text)
        for stub in stubs:
            url = bundles_mod.resolve_manifest_url(src.url, stub["url"])
            try:
                manifest_text = bundles_mod.fetch_text(url)
            except (urllib.error.URLError, ValueError, OSError) as exc:
                errs.append(f"{src.id}/{stub['id']}: fetch — {exc}")
                continue
            try:
                bundles_mod.parse_manifest(
                    manifest_text, source_id=src.id, source_name=src.name or src.id
                )
            except bundles_mod.ManifestError as exc:
                errs.append(f"{src.id}/{stub['id']}: parse — {exc}")
                continue
            (manifests_dir / Path(stub["url"]).name).write_text(manifest_text)
    # Stamp last-refresh time for the UI.
    BUNDLE_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (BUNDLE_CACHE_DIR / ".last-refresh").write_text(
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    )
    (BUNDLE_CACHE_DIR / ".last-refresh-errors.json").write_text(
        json.dumps(errs)
    )
    return errs


def _read_last_refresh() -> tuple[Optional[str], list[str]]:
    ts: Optional[str] = None
    errs: list[str] = []
    p_ts = BUNDLE_CACHE_DIR / ".last-refresh"
    if p_ts.exists():
        try:
            ts = p_ts.read_text().strip()
        except OSError:
            pass
    p_errs = BUNDLE_CACHE_DIR / ".last-refresh-errors.json"
    if p_errs.exists():
        try:
            data = json.loads(p_errs.read_text())
            if isinstance(data, list):
                errs = [str(x) for x in data]
        except (OSError, json.JSONDecodeError):
            pass
    return ts, errs


def _all_bundles() -> tuple[list[bundles_mod.Bundle], list[str]]:
    """Built-in + remote-cached. Resolved against the live catalogs.
    Order: builtin first (deterministic), then remote sources in the
    order listed in sources.json."""
    builtin, errs1 = _load_builtin_bundles()
    remote, errs2 = _load_remote_bundles()
    catalog_books = read_catalog_cache().get("books") or []
    region_catalog = _read_region_catalog()
    for b in builtin + remote:
        bundles_mod.resolve_bundle(
            b, catalog_books=catalog_books, region_catalog=region_catalog
        )
    return builtin + remote, errs1 + errs2


@app.get("/admin/bundles", response_class=HTMLResponse)
def bundles_get(request: Request, ok: Optional[str] = None, err: Optional[str] = None) -> HTMLResponse:
    bs, load_errs = _all_bundles()
    last_refresh, refresh_errs = _read_last_refresh()
    flash = None
    if ok:
        flash = {"kind": "ok", "message": ok}
    elif err:
        flash = {"kind": "error", "message": err}
    return templates.TemplateResponse(
        "bundles.html",
        {
            "request": request,
            "active": "bundles",
            "bundles": bs,
            "internal_free_bytes": _internal_free_bytes(),
            "format_bytes": health.format_bytes,
            "last_refresh": last_refresh,
            "refresh_errors": load_errs + refresh_errs,
            "catalog_empty": not (read_catalog_cache().get("books") or []),
            "flash": flash,
        },
    )


@app.post("/admin/bundles/refresh")
def bundles_refresh():
    errs = _refresh_remote_sources()
    if errs:
        return RedirectResponse(
            f"/admin/bundles?err=Refresh+completed+with+{len(errs)}+error(s)",
            status_code=303,
        )
    return RedirectResponse("/admin/bundles?ok=Sources+refreshed", status_code=303)


@app.post("/admin/bundles/{qualified_id}/install")
def bundles_install_endpoint(qualified_id: str):
    if ":" not in qualified_id:
        raise HTTPException(status_code=400, detail="bundle id must be source:id")
    source_id, _, bundle_id = qualified_id.partition(":")

    bs, _ = _all_bundles()
    bundle = next(
        (b for b in bs if b.source_id == source_id and b.id == bundle_id),
        None,
    )
    if bundle is None:
        raise HTTPException(status_code=404, detail="bundle not found")
    if bundle.resolved_size_bytes == 0 and not bundle.resolved_items:
        raise HTTPException(
            status_code=409, detail="bundle has no resolvable items"
        )
    if bundle.resolved_size_bytes > _internal_free_bytes():
        return RedirectResponse(
            "/admin/bundles?err=Bundle+won%27t+fit+in+internal+storage",
            status_code=303,
        )

    queued_zims = 0
    queued_statics = 0
    queued_regions: list[str] = []
    in_flight = bundles_install.aria2_in_flight_filenames()

    for it in bundle.resolved_items:
        kind = it.get("kind")
        if kind == "zim":
            url = it.get("url")
            if not url:
                continue
            # Skip duplicates already in aria2.
            existing = read_catalog_cache().get("books") or []
            book = next(
                (b for b in existing if b.get("name") == it.get("name")), None
            )
            filename = book.get("filename") if book else None
            if filename and filename in in_flight:
                continue
            try:
                bundles_install.queue_zim(
                    url=url, filename=filename or "", dest_dir=ZIM_BASE
                )
                queued_zims += 1
            except aria2.Aria2Error:
                continue
        elif kind == "static":
            try:
                bundles_install.queue_static(
                    url=it["url"],
                    sha256=it["sha256"],
                    install_to=it["install_to"],
                )
                queued_statics += 1
            except aria2.Aria2Error:
                continue
        elif kind == "map_region":
            queued_regions.append(it["region_id"])

    if queued_regions:
        bundles_install.append_to_queue(queued_regions)
        bundles_install.kick_drainer(
            BUNDLE_CACHE_DIR / "last-drainer.log"
        )

    summary_parts = []
    if queued_zims:
        summary_parts.append(f"{queued_zims} ZIM(s)")
    if queued_statics:
        summary_parts.append(f"{queued_statics} static(s)")
    if queued_regions:
        summary_parts.append(f"{len(queued_regions)} map region(s)")
    if not summary_parts:
        return RedirectResponse(
            "/admin/bundles?err=Nothing+to+queue+(items+already+in+flight%3F)",
            status_code=303,
        )
    summary = ", ".join(summary_parts)
    return RedirectResponse(
        f"/admin/bundles?ok=Queued+{summary}",
        status_code=303,
    )


# ---------- updates dashboard ----------


@app.get("/admin/updates", response_class=HTMLResponse)
def updates_get(
    request: Request,
    ok: Optional[str] = None,
    err: Optional[str] = None,
) -> HTMLResponse:
    snap = updates_state.read_snapshot()
    flash = None
    if ok:
        flash = {"kind": "ok", "message": ok}
    elif err:
        flash = {"kind": "error", "message": err}
    return templates.TemplateResponse(
        "updates.html",
        {
            "request": request,
            "active": "updates",
            "snapshot": snap,
            "flash": flash,
        },
    )


@app.post("/admin/updates/check")
def updates_check_now():
    """In-process check trigger for the "Check now" button."""
    uplink_ok = bool(detect_uplink().get("ethernet"))
    snap = updates_state.compute_snapshot(uplink_ok=uplink_ok)
    updates_state.write_snapshot(snap)
    if not uplink_ok:
        return RedirectResponse(
            "/admin/updates?err=No+Ethernet+uplink+%E2%80%94+plug+in+a+cable+and+retry",
            status_code=303,
        )
    msg = (
        f"Check complete: {snap['stale_count']} update(s) available, "
        f"{len(snap.get('errors') or [])} source(s) had errors."
    ).replace(" ", "+")
    return RedirectResponse(f"/admin/updates?ok={msg}", status_code=303)


@app.post("/admin/updates/apply")
def updates_apply_endpoint(
    kind: str = Form(...),
    item_id: str = Form(...),
    delete_old: Optional[str] = Form(None),
):
    """Apply one update. The route looks up the item in the latest
    snapshot — we don't trust user-supplied URLs."""
    snap = updates_state.read_snapshot()
    item = next(
        (it for it in snap.get("items") or []
         if it.get("kind") == kind and it.get("id") == item_id),
        None,
    )
    if item is None:
        raise HTTPException(status_code=404, detail="update not found in snapshot")
    if item.get("status") != "stale":
        raise HTTPException(
            status_code=409,
            detail=f"item is {item.get('status')!r}, not stale",
        )

    try:
        if kind == "zim":
            # Prefer the filename the detector identified as the newest
            # installed copy for this book_id; that's what we'd swap or
            # remove, not whatever the directory listing happens to put
            # first. Fall back to a fresh disk lookup for older snapshots
            # that didn't carry the field.
            current = item.get("installed_filename") or _installed_zim_filename(item_id)
            msg = updates_apply.apply_zim_update(
                book_id=item_id,
                current_filename=current,
                new_url=item.get("available_url") or "",
                new_filename=(item.get("available_name") or "") + ".zim"
                    if item.get("available_name") else "",
                delete_old=bool(delete_old),
            )
        elif kind == "map_region":
            msg = updates_apply.apply_region_update(region_id=item_id)
        elif kind == "bundle":
            msg = updates_apply.apply_bundle_update(
                qualified_id=item_id,
                refresh_callback=_refresh_remote_sources,
            )
        elif kind == "static":
            entry = _static_manifest_entry(item_id)
            if entry is None:
                raise updates_apply.UpdateError(
                    f"No manifest knows about {item_id!r} anymore."
                )
            msg = updates_apply.apply_static_update(
                install_to=entry.install_to,
                url=entry.url,
                expected_sha256=entry.expected_sha256,
                expected_size=entry.expected_size,
            )
        else:
            raise HTTPException(status_code=400, detail=f"unknown kind {kind!r}")
    except updates_apply.UpdateError as exc:
        return RedirectResponse(
            f"/admin/updates?err={_quote_msg(str(exc))}", status_code=303
        )

    return RedirectResponse(
        f"/admin/updates?ok={_quote_msg(msg)}", status_code=303
    )


@app.post("/admin/updates/pin")
def updates_pin(
    kind: str = Form(...),
    item_id: str = Form(...),
):
    """Pin an item to its currently installed version. The route reads
    fresh on-disk state to derive the pin handle — that way a
    drift-since-last-check doesn't get baked in."""
    store = updates_state.read_pins()
    if kind == "zim":
        zf = next(
            (z for z in updates_state.collect_installed_zims()
             if z.book_id == item_id),
            None,
        )
        if zf is None:
            raise HTTPException(status_code=404, detail="installed ZIM not found")
        store.zims[zf.book_id] = zf.version
    elif kind == "map_region":
        sc = next(
            (s for s in updates_state.collect_region_sidecars()
             if s.region_id == item_id),
            None,
        )
        if sc is None:
            raise HTTPException(status_code=404, detail="region sidecar not found")
        store.regions[sc.region_id] = {
            "etag": sc.etag,
            "last_modified": sc.last_modified,
        }
    elif kind == "bundle":
        cached = updates_state.collect_cached_bundle_bodies()
        body = cached.get(item_id)
        if body is None:
            raise HTTPException(status_code=404, detail="cached manifest not found")
        store.bundles[item_id] = updates_mod.sha256_text(body)
    elif kind == "static":
        installed_path = updates_state._resolve_static_install_path(item_id)
        if installed_path is None or not installed_path.is_file():
            raise HTTPException(status_code=404, detail="static file not on disk")
        try:
            sha = updates_mod.sha256_file(installed_path)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"hash failed: {exc}")
        store.statics[item_id] = sha
    else:
        raise HTTPException(status_code=400, detail=f"unknown kind {kind!r}")
    updates_state.write_pins(store)
    return RedirectResponse(
        "/admin/updates?ok=Pinned+to+current+version", status_code=303
    )


# Filenames we permit deletion of — Kiwix ZIM convention plus a
# safety regex that prevents path traversal or hidden-file shenanigans.
_ZIM_FILENAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}\.zim$")


@app.post("/admin/zim/{filename}/delete")
def zim_delete(filename: str):
    """Unlink a ZIM from /srv/prepperpi/zim/. Filename must be a plain
    `*.zim` basename (no path separators, no `..`). The kiwix-reindex
    `.path` unit picks up the directory change and re-indexes."""
    if not _ZIM_FILENAME_RE.match(filename) or filename.startswith("."):
        raise HTTPException(status_code=400, detail="invalid filename")
    path = ZIM_BASE / filename
    try:
        path.unlink()
    except FileNotFoundError:
        return RedirectResponse(
            f"/admin/updates?err={_quote_msg(f'{filename} was already gone')}",
            status_code=303,
        )
    except OSError as exc:
        return RedirectResponse(
            f"/admin/updates?err={_quote_msg(f'Could not delete: {exc}')}",
            status_code=303,
        )
    return RedirectResponse(
        f"/admin/updates?ok={_quote_msg(f'Deleted {filename}')}",
        status_code=303,
    )


@app.post("/admin/updates/unpin")
def updates_unpin(
    kind: str = Form(...),
    item_id: str = Form(...),
):
    store = updates_state.read_pins()
    if kind == "zim":
        store.zims.pop(item_id, None)
    elif kind == "map_region":
        store.regions.pop(item_id, None)
    elif kind == "bundle":
        store.bundles.pop(item_id, None)
    elif kind == "static":
        store.statics.pop(item_id, None)
    else:
        raise HTTPException(status_code=400, detail=f"unknown kind {kind!r}")
    updates_state.write_pins(store)
    return RedirectResponse("/admin/updates?ok=Unpinned", status_code=303)


def _installed_zim_filename(book_id: str) -> Optional[str]:
    for z in updates_state.collect_installed_zims():
        if z.book_id == book_id:
            return z.filename
    return None


def _static_manifest_entry(install_to: str):
    cached = updates_state.collect_cached_bundle_bodies()
    for entry in updates_state.collect_static_manifest_entries(cached):
        if entry.install_to == install_to:
            return entry
    return None


def _quote_msg(msg: str) -> str:
    """URL-quote the flash message into a redirect query string."""
    from urllib.parse import quote
    return quote(msg, safe="")
