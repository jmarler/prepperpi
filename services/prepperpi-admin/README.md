# prepperpi-admin

Browser-based admin console for PrepperPi. FastAPI + Jinja2 + uvicorn behind Caddy at `/admin/*`. Today it ships:

- **Network** panel (E4-S1) — change SSID, Wi-Fi password, channel, country, with a one-click reset to factory defaults.
- **Online-mode banner** on the home page (E4-S3) — read-only Ethernet-uplink indicator. The Pi is the only thing that goes online; AP clients are firewalled off the upstream by [`prepperpi-ap`](../prepperpi-ap/) so we never become an accidental hotspot.
- **Storage and health panel** (E4-S2) — live CPU / RAM / SoC temperature / disk-free / connected-client count at 1 Hz. Per-USB write toggle (closes E2-S2 AC-5; session-only — re-plug resets to read-only). Recent event log + downloadable JSON of the last 500 events. Diagnostics tarball download.

The Content panel lands in E2-S3.

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

> **TODO — pen-test before declaring E4 done.** Walk through:
> stdin parser fuzzing, sudoers-rule confused-deputy probes, CSRF on the form
> POSTs (no token today — relying on AC-5's network ACL), template-injection
> attempts, an attacker on the AP subnet trying to brick hostapd via specific
> country / channel combinations, and the JS `confirm()` dialog as an
> opt-out for clickjack.

## AC-5 access guard

E4-S1 requires the admin console be reachable only from clients on the AP subnet (`10.42.0.0/24`) or the device itself (`127.0.0.1`, `::1`). The check lives in Caddy, not Python:

```caddyfile
@admin path /admin /admin/*
handle @admin {
  @admin_allowed remote_ip 10.42.0.0/24 127.0.0.1/32 ::1/128
  handle @admin_allowed {
    reverse_proxy 127.0.0.1:8090
  }
  respond "Forbidden — admin console is AP-only" 403
}
```

Because Caddy strips off-subnet requests *before* they reach uvicorn, FastAPI doesn't need to re-check the source address (and we don't want the CIDR encoded in two places that can drift).

## Files

| Path                                     | Role                                             |
| ---------------------------------------- | ------------------------------------------------ |
| `app/main.py`                            | FastAPI app: routes, validation, sudo dispatch.  |
| `app/uplink.py`                          | Pure helpers for the Ethernet-uplink banner.     |
| `app/health.py`                          | Pure parsers + I/O wrappers for the Storage page (`/proc`, `/sys`, `dnsmasq.leases`, `os.statvfs`). |
| `app/templates/storage.html`             | Server-side render of the Storage and health page. |
| `app/templates/base.html`                | Layout + nav + theme.                            |
| `app/templates/home.html`                | `/admin/` overview page with section cards.      |
| `app/templates/network.html`             | `/admin/network` form + reset button.            |
| `app/static/admin.css`                   | Admin-specific styling (loads `/style.css` too). |
| `app/static/admin.js`                    | Polls `/admin/uplink` every 5 s; live-swaps the home banner. Progressive enhancement — no-JS users still see the request-time render. |
| `apply-network-config`                   | Privileged worker for the Network panel. JSON on stdin. |
| `apply-storage-action`                   | Privileged worker for the USB write toggle. JSON on stdin. |
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
| GET     | `/admin/uplink`            | JSON snapshot of uplink state (E4-S3). Polled by `admin.js`. |
| GET     | `/admin/storage`           | Render the Storage and health page (E4-S2).      |
| GET     | `/admin/health`            | JSON snapshot of system health (CPU, RAM, temp, disks, USB, events). Polled at 1 Hz from the storage page. |
| POST    | `/admin/storage/usb/{name}/writable` | Toggle a USB drive read-only ↔ writable. Dispatches to `apply-storage-action`. |
| GET     | `/admin/diagnostics`       | Stream a `tar.gz` diagnostics bundle (logs, snapshots). Wi-Fi password excluded. |
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

## Debugging

```
journalctl -u prepperpi-admin.service -f
journalctl -u prepperpi-ap-configure.service -n 50    # what hostapd thought of our changes
curl -sI -H 'Host: prepperpi.home.arpa' http://10.42.0.1/admin/healthz
```
