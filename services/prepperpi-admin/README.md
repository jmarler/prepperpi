# prepperpi-admin

Browser-based admin console for PrepperPi. FastAPI + Jinja2 + uvicorn behind Caddy at `/admin/*`. Today it ships:

- **Network** panel — change SSID, Wi-Fi password, channel, country, with a one-click reset to factory defaults.
- **Online-mode banner** on the home page — read-only Ethernet-uplink indicator. The Pi is the only thing that goes online; AP clients are firewalled off the upstream by [`prepperpi-ap`](../prepperpi-ap/) so we never become an accidental hotspot.
- **Storage and health panel** — live CPU / RAM / SoC temperature / disk-free / connected-client count at 1 Hz. Per-USB write toggle (session-only — re-plug resets to read-only). Recent event log + downloadable JSON of the last 500 events. Diagnostics tarball download.
- **Content catalog** — browse the Kiwix library, filter by language/topic/size/name, queue downloads via [`prepperpi-aria2c`](../prepperpi-aria2c/). Pause / resume / cancel / clear in place. Downloads land in `/srv/prepperpi/zim/` (SD card). The metalink is parsed admin-side and the direct mirror URLs are handed to aria2 — keeps each download to one GID for clean pause/resume semantics.
- **Offline maps** — list installed map regions and delete one with a single button. **Pick a country (or one-click bundle: NA / LATAM / EU / EMEA / APAC / Oceania / Russia / Antarctica) from the catalog and the admin spawns a [`pmtiles extract`](https://github.com/protomaps/go-pmtiles) job that streams just that region's tiles directly out of the [mapterhorn.com daily planet PMTiles](https://download.mapterhorn.com/) over HTTP range requests.** One install at a time (lock file at `/srv/prepperpi/maps/.lock`). Browser-side queue runs bundle members one after the other. Region metadata (name, bounds, zoom range, total size) comes from the reindex service in [`prepperpi-tiles`](../prepperpi-tiles/). Delete is plain `unlink()` — `/srv/prepperpi/maps/` is owned by the admin user.
- **Content bundles** — curated YAML manifests (Kiwix ZIMs + map regions + optional static files) installable in one click. The image ships builtin copies of the [official bundles](https://github.com/jmarler/prepperpi-bundles); when online, the admin can refresh from configured source URLs (default: official) or add community-managed sources. ZIMs go through aria2's existing queue; map regions append to a server-side queue drained by [`bundle-region-installer.py`](bundle-region-installer.py) which calls `extract-region.sh` sequentially. Schema and contributor guide: [`docs/creating-bundles.md`](../../docs/creating-bundles.md).
- **Power** panel — clean shutdown and restart from the browser, so an operator can power the appliance down without a keyboard, a screen, or SSH (SSH is off by default in the prebuilt image). Also configures an optional **physical power button** on the GPIO header. The halt is deferred a few seconds so the confirmation page — which tells the operator to wait for the green LED before unplugging — renders before the Pi goes away.

## Trust model

The admin console is **deliberately split** into an unprivileged FastAPI process and a tiny privileged Python worker:

```
            (untrusted)                          (trust boundary)        (privileged)
  Caddy ──► prepperpi-admin.service ──► sudo -n /opt/.../apply-network-config
            uvicorn, user=prepperpi-admin       JSON-on-stdin, runs as root
            ├─ no FS writes anywhere                 ├─ validates the JSON again
            ├─ render forms, parse POSTs             ├─ writes /boot/firmware/prepperpi.conf
            └─ shell out via sudo                    └─ systemctl restart {prepperpi-ap-configure,
                                                                          hostapd, dnsmasq}
```

The unprivileged side runs as the `prepperpi-admin` system user with `ProtectSystem=strict`, no write access to any path outside its own working directory, and read-only mounts on `/boot/firmware`. **Even an RCE in FastAPI cannot rewrite prepperpi.conf or restart hostapd directly** — both flow through the wrapper, which re-validates the payload independently. The wrapper is the trust boundary; the FastAPI process should be treated as untrusted.

The sudoers exception is a single rule for one specific script:

```
prepperpi-admin ALL=(root) NOPASSWD: /opt/prepperpi/services/prepperpi-admin/apply-network-config
```

The wrapper script itself is owned `root:root` mode `0755` so the admin user cannot replace or modify it. (`sudo` refuses to execute any script writable by non-root.)

### Pen-test pass

A pen-test pass was completed in April 2026; full report at
[`docs/security-pentest-2026-04.md`](../../docs/security-pentest-2026-04.md).
Summary of the threat-model boundary as it stands now:

- **Privileged-worker boundary** (AC-1): `apply-network-config` and
  `apply-storage-action` re-validate every JSON-stdin field independently of
  FastAPI; both refuse TTY stdin and cap the payload at 1 MiB so a confused
  deputy cannot wedge them.
- **CSRF defense** (AC-2): a FastAPI middleware (`csrf_origin_guard`) rejects
  cross-origin POST/PUT/PATCH/DELETE on `/admin/*` by comparing `Origin` /
  `Referer` against `Host`. With no auth or sessions, this is the load-bearing
  defense against a victim's AP-subnet browser being used as a proxy by an
  off-network attacker.
- **Output encoding** (AC-3): Jinja autoescape on by default; the one
  JS-string-context interpolation (`onsubmit="confirm('… {{ r.name|e }} …')"`
  in `maps.html`) was replaced with a `data-confirm` attribute + global JS
  handler so `r.name` lives in HTML-attribute context and is no longer
  evaluated as JS.
- **Brick resistance** (AC-4): three-layer recovery — (1) FCC reg-domain
  matrix (`US`/`CA`/`MX` → channels 1-11) blocks the known brick combos at
  validation; (2) on `restart_ap()` failure, the worker rolls
  `prepperpi.conf` back to the prior snapshot and retries; (3) if the
  rollback's restart also fails, the conf is deleted entirely so
  `prepperpi-ap-configure` boots on its hard-coded factory defaults.
- **Headers** (AC-5): Caddy emits `X-Frame-Options: DENY`,
  `X-Content-Type-Options: nosniff`, and `Referrer-Policy: same-origin` on
  every `/admin/*` response. JS `confirm()` is documented as UX-only — the
  server requires no JS-issued state to mutate, and the security boundary is
  the network ACL + the CSRF middleware.
- **Information disclosure** (AC-6): the diagnostics tarball excludes
  `prepperpi.conf`; greps for the aria2 RPC secret, `/etc/shadow` hashes, and
  `wpa_passphrase` over the full bundle return zero hits; 4xx/5xx responses
  return generic detail with no traceback.

## Network access guard

The admin console is reachable from private (RFC 1918 / RFC 4193 / loopback / link-local) addresses only. That's the AP subnet, the device itself, and whatever LAN the Ethernet port is plugged into. The check lives in Caddy, not Python:

```caddyfile
@admin path /admin /admin/*
handle @admin {
  @admin_allowed remote_ip 10.0.0.0/8 172.16.0.0/12 \
    192.168.0.0/16 127.0.0.1/32 ::1/128 fc00::/7 fe80::/10
  handle @admin_allowed {
    reverse_proxy 127.0.0.1:8090
  }
  respond "Forbidden — …" 403
}
```

Because Caddy strips disallowed requests *before* they reach uvicorn, FastAPI doesn't need to re-check the source address (and we don't want the CIDR encoded in two places that can drift).

**The console has no authentication.** Anyone who can reach it can rewrite the Wi-Fi config, delete content, restore a backup, or power the Pi off. The address guard is the whole access-control story, and since the Power panel landed it covers the wired LAN too — a deliberate trade for being able to shut the appliance down from a wired workstation. Treat any network you plug the Ethernet port into as trusted, or leave it unplugged.

The ranges are spelled out rather than using Caddy's `private_ranges` shorthand. Both work (checked against Caddy 2.6.2, the Bookworm package, and 2.8.4), but the shorthand's membership is Caddy's to redefine between releases, and this list is the entire access-control boundary for an unauthenticated console — what it covers should be reviewable in the file, not in upstream release notes.

## Files

| Path                                     | Role                                             |
| ---------------------------------------- | ------------------------------------------------ |
| `app/main.py`                            | FastAPI app: routes, validation, sudo dispatch.  |
| `app/uplink.py`                          | Pure helpers for the Ethernet-uplink banner.     |
| `app/health.py`                          | Pure parsers + I/O wrappers for the Storage page (`/proc`, `/sys`, `dnsmasq.leases`, `os.statvfs`). |
| `app/catalog.py`                         | OPDS catalog parser + filter helpers. |
| `app/aria2.py`                           | JSON-RPC client for the [`prepperpi-aria2c`](../prepperpi-aria2c/) daemon. |
| `app/maps.py`                            | Reads `regions.json`, deletes region files, spawns `extract-region.sh` for the catalog/install flow, reads/cancels install status. |
| `app/templates/maps.html`                | Server-side render of the Offline maps page. Includes catalog + active-install card; populated by `admin.js` Block 4. |
| `app/templates/storage.html`             | Server-side render of the Storage and health page. |
| `app/templates/catalog.html`             | Server-side render of the Content catalog page. |
| `app/templates/base.html`                | Layout + nav + theme.                            |
| `app/templates/home.html`                | `/admin/` overview page with section cards.      |
| `app/templates/network.html`             | `/admin/network` form + reset button.            |
| `app/static/admin.css`                   | Admin-specific styling (loads `/style.css` too). |
| `app/static/admin.js`                    | Polls `/admin/uplink` every 5 s; live-swaps the home banner. Progressive enhancement — no-JS users still see the request-time render. |
| `apply-network-config`                   | Privileged worker for the Network panel. JSON on stdin. |
| `apply-storage-action`                   | Privileged worker for the USB write toggle. JSON on stdin. |
| `apply-power-action`                     | Privileged worker for shutdown / restart / GPIO-button config. JSON on stdin. |
| `app/power.py`                           | Read-only parse of the managed `config.txt` block (physical-button state). Mirrors the worker's constants; drift is caught by `tests/unit/test_admin_power.py`. |
| `app/templates/power.html`               | `/admin/power` — shutdown / restart buttons + GPIO-button form. |
| `app/templates/power_pending.html`       | Terminal page shown while the deferred halt counts down. |
| `sudoers.d-prepperpi-admin`              | Sudoers exception, dropped at `/etc/sudoers.d/`. |
| `prepperpi-admin.service`                | uvicorn unit, sandboxed.                         |
| `_admin.html`                            | Landing-page tile fragment.                      |

## Routes

| Method  | Path                       | Purpose                                           |
| ------- | -------------------------- | ------------------------------------------------- |
| GET     | `/admin/`                  | Overview with section cards.                      |
| GET     | `/admin/network`           | Render the network-settings form.                 |
| POST    | `/admin/network`           | Validate, dispatch to wrapper, redirect 303.      |
| POST    | `/admin/network/reset`     | Factory-reset via wrapper, redirect 303.          |
| GET     | `/admin/healthz`           | Plain `{"ok": true}` for smoke tests.             |
| GET     | `/admin/uplink`            | JSON snapshot of uplink state. Polled by `admin.js`. |
| GET     | `/admin/storage`           | Render the Storage and health page.              |
| GET     | `/admin/health`            | JSON snapshot of system health (CPU, RAM, temp, disks, USB, events). Polled at 1 Hz from the storage page. |
| POST    | `/admin/storage/usb/{name}/writable` | Toggle a USB drive read-only ↔ writable. Dispatches to `apply-storage-action`. |
| GET     | `/admin/diagnostics`       | Stream a `tar.gz` diagnostics bundle (logs, snapshots). Wi-Fi password excluded. |
| GET     | `/admin/catalog`           | Render the Content catalog page.                 |
| POST    | `/admin/catalog/refresh`   | Fetch + cache `library.kiwix.org/catalog/v2/entries`. Requires Ethernet uplink. |
| GET     | `/admin/catalog/data`      | Cached catalog JSON (books + facets). Polled once per page-load. |
| GET     | `/admin/downloads`         | 1 Hz JSON snapshot of aria2's queue (active + waiting + recent). |
| POST    | `/admin/downloads/queue`   | Add one ZIM to the queue. Body: `book_id`, `destination_id`. |
| POST    | `/admin/downloads/{gid}/{pause,resume,cancel}` | Mutate one queued/active download. |
| GET     | `/admin/maps`              | List installed map regions + browse/install catalog. |
| GET     | `/admin/maps/data`         | JSON snapshot of installed regions.                              |
| GET     | `/admin/maps/catalog`      | Static catalog of ~200 ISO countries + bundles, enriched with `installed: true/false` and `free_space_human`. Polled once per page-load. |
| POST    | `/admin/maps/install`      | Body: `region_id`. Spawns `extract-region.sh` detached. Returns 202 / 409 (already running or installed) / 507 (insufficient disk). |
| GET     | `/admin/maps/install/status` | Snapshot of the currently-running (or last-completed) extract. Polled at 1Hz when an install is in flight. |
| POST    | `/admin/maps/install/cancel` | SIGTERM the worker. Worker's signal trap discards the partial file. |
| POST    | `/admin/maps/{region_id}/delete` | Unlink one `.mbtiles`/`.pmtiles` and redirect 303 with a flash message. |
| GET     | `/admin/power`             | Render the Power page (shutdown / restart / button config). |
| POST    | `/admin/power/poweroff`    | Schedule a deferred clean shutdown, redirect 303.  |
| POST    | `/admin/power/reboot`      | Schedule a deferred restart, redirect 303.         |
| GET     | `/admin/power/{poweroffing,rebooting}` | Terminal "what to expect" page. Rendered before the Pi halts. |
| POST    | `/admin/power/button`      | Enable/disable the GPIO shutdown button and pick its pin. Takes effect next boot. |
| GET     | `/admin/static/admin.css`  | Static stylesheet.                                |

Behind Caddy's `/admin/*` reverse-proxy. Static files for the landing page (`/style.css`, etc.) come from Caddy's file_server; they're under a different prefix and served directly without going through uvicorn.

## Manually applying or rolling back

```
# What's currently saved
cat /boot/firmware/prepperpi.conf

# Apply by hand (skipping the FastAPI side)
echo '{"action":"set","ssid":"PrepperPi-FIELD","wifi_password":"hunter2hunter2","channel":"auto","country":"US"}' \
  | sudo -u prepperpi-admin sudo -n /opt/prepperpi/services/prepperpi-admin/apply-network-config

# Roll back to factory defaults
echo '{"action":"reset"}' \
  | sudo -u prepperpi-admin sudo -n /opt/prepperpi/services/prepperpi-admin/apply-network-config
```

## Power

Three ways to shut the appliance down cleanly, in order of how little equipment they need:

1. **Admin console** — `/admin/power` → **Shut down**. Works from a phone on the PrepperPi Wi-Fi, or from a workstation on the wired LAN.
2. **Physical button** — a momentary push-button across a GPIO pin and ground. Enable it on the Power page first.
3. **Shell** — `sudo systemctl poweroff`. SSH is off by default in the prebuilt image, so this assumes you enabled it at flash time or have a keyboard attached.

Wait for the green activity LED to stop blinking before unplugging.

### The physical button

Enabling the button writes a delimited block to `/boot/firmware/config.txt`:

```
# >>> prepperpi power button (managed) >>>
dtoverlay=gpio-shutdown,gpio_pin=3,active_low=1,gpio_pull=up,debounce=1000
# <<< prepperpi power button (managed) <<<
```

`gpio-shutdown` is a stock Raspberry Pi overlay. The firmware raises `KEY_POWER` on a falling edge and `systemd-logind` handles it — **nothing of ours is in that path**, so the button still works if every PrepperPi service has crashed. It also means the setting only takes effect after a reboot, because `config.txt` is read by the firmware at boot.

Only the block is managed. Anything the operator put in `config.txt` outside it survives, and a hand-written `dtoverlay=gpio-shutdown` line outside the block is deliberately left alone rather than adopted.

**GPIO3 (header pin 5) is the default** because it is the only pin that also *wakes* a halted Pi — the same button turns the appliance back on. Wire it to any ground pin (6, 9, 14, 20, 25, 30, 34, 39); pin 6 sits right beside it.

`debounce=1000` means the press has to be held for a second or so. Verified on a Pi 4B with a momentary button: a brief tap does nothing, a deliberate hold halts the Pi. That is the intended failsafe — an accidental brush against a cased device shouldn't take the library down.

**The catch with GPIO3:** it doubles as I²C1 SCL. On a build with an RTC or any other I²C board on header pins 3/5, normal bus traffic reads as button presses and shuts the Pi down at random. Pick a different pin in that case — the dropdown offers 13 alternatives, none of which can wake the Pi, but all of which shut it down.

The button ships **disabled**. Enabling by default would have broken every I²C build with a symptom almost impossible for a non-technical owner to diagnose.

```
# Enable by hand (skipping the FastAPI side)
echo '{"action":"set-button","enabled":true,"gpio":3}' \
  | sudo -n /opt/prepperpi/services/prepperpi-admin/apply-power-action

# Check what's scheduled after clicking Shut down
systemctl list-timers prepperpi-poweroff
```

## Debugging

```
journalctl -u prepperpi-admin.service -f
journalctl -u prepperpi-ap-configure.service -n 50    # what hostapd thought of our changes
curl -sI -H 'Host: prepperpi.home.arpa' http://10.42.0.1/admin/healthz
```
