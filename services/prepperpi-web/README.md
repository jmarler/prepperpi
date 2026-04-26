# prepperpi-web

Caddy-based web front for PrepperPi. Serves the captive-portal probe responses, the off-host redirect, the landing page, and a small RFC 8908 captive-portal API. Listens on `:80` (primary) and `:443` (self-signed cert, for Android's HTTPS probe).

## What it does

- **Captive-portal probes** — `/hotspot-detect.html` (iOS), `/generate_204`, `/gen_204` (Android), `/ncsi.txt`, `/connecttest.txt`, `/redirect` (Windows), `/success.txt` (ChromeOS), `/kindle-wifi/wifistub.html` (Amazon), and the bare-host probes against `captive.apple.com` / `netcts.cdn-apple.com`. Each returns:
  ```
  HTTP/1.1 307 Temporary Redirect
  Cache-Control: no-store
  Connection: close
  Location: http://prepperpi.home.arpa/
  ```
  This is the nodogsplash pattern — the same HTTP response that widely-deployed OpenWrt captive portals use and that reliably triggers the OS captive handler on iOS and stock Android.
- **Off-host redirect** — any request whose `Host` header isn't `prepperpi.home.arpa` gets a `302` bounce to `http://prepperpi.home.arpa{uri}`. Pairs with the dnsmasq catch-all DNS hijack from `prepperpi-ap` so that typing any URL in a browser lands users on the portal.
- **RFC 8908 captive-portal API** at `/captive-api` — returns `{"captive":true,"user-portal-url":"http://prepperpi.home.arpa/"}`. Advertised to clients via DHCP option 114 (RFC 8910) so modern iOS / Android / NetworkManager can discover the portal without relying on OS-specific probe URLs.
- **Landing page** at `/` — `web/landing/index.html` served from `/opt/prepperpi/web/landing/`, processed through Caddy's `templates` directive so each `{{include "/_<frag>.html"}}` call inlines the latest tile fragment maintained by another service.
- **`/admin/*`** — reverse-proxied to the FastAPI admin app on `127.0.0.1:8090`, **but only when the request's source IP is in `10.42.0.0/24` (the AP subnet) or `127.0.0.1` / `::1`**. Off-subnet requests get a `403` at the Caddy layer before hitting uvicorn. Allowed responses also carry `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, and `Referrer-Policy: same-origin`. Maintained by [`prepperpi-admin`](../prepperpi-admin/).
- **`/library/*`** — reverse-proxied to `kiwix-serve` on `127.0.0.1:8088`. Maintained by [`prepperpi-kiwix`](../prepperpi-kiwix/).
- **`/maps/{styles,data,fonts,sprites}/*`** — reverse-proxied to `tileserver-gl-light` on `127.0.0.1:8083` after `uri strip_prefix /maps`. Vector tiles, glyphs, sprites, and the composite style live here. Maintained by [`prepperpi-tiles`](../prepperpi-tiles/).
- **`/maps/*`** (everything else) — static MapLibre GL JS client served from `/opt/prepperpi/services/prepperpi-tiles/client/`. Maintained by [`prepperpi-tiles`](../prepperpi-tiles/).
- **`/usb/*.md`** — reverse-proxied to the Python markdown daemon on `127.0.0.1:8089`. Maintained by [`prepperpi-usb`](../prepperpi-usb/).
- **`/usb/*`** (everything else) — `file_server browse` rooted at `/srv/prepperpi/user-usb/`. Directory listings, range requests, native MIME for PDFs/images/video/audio.
- **`/_events.json`** — a small JSON ring buffer (last 50 events) written by reindex services and polled by `dashboard.js` for the live toast notifications. Maintained by [`prepperpi-events`](../prepperpi-events/).
- **`/healthz`** — plain `200 ok` for curl-from-Pi smoke checks.

## Landing-page templates

The landing-page `index.html` is processed by Caddy's `templates` directive. Several other services own a fragment file under the landing root and update it on their own schedule; Caddy re-includes the latest version on every page load:

| Fragment file (under `/opt/prepperpi/web/landing/`) | Owner | Contents |
|---|---|---|
| `_library.html` | [`prepperpi-kiwix`](../prepperpi-kiwix/) | One tile per ZIM in the library, plus the empty-state tile. |
| `_library_search.html` | [`prepperpi-kiwix`](../prepperpi-kiwix/) | Cross-library search form (omitted entirely when the library is empty). |
| `_usb.html` | [`prepperpi-usb`](../prepperpi-usb/) | One tile per mounted USB volume, or the empty-state tile. |
| `_maps.html` | [`prepperpi-tiles`](../prepperpi-tiles/) | Maps tile — links to `/maps/` when one or more regions are installed, otherwise the empty-state tile. |
| `_admin.html` | [`prepperpi-admin`](../prepperpi-admin/) | Admin tile shipped with the landing page itself; the admin service doesn't currently rewrite it. |
| `_events.json` | [`prepperpi-events`](../prepperpi-events/) | Static-served event log. |

Each `{{include}}` call in `index.html` is wrapped in a `<div data-fragment="<name>">` so [`dashboard.js`](../../web/landing/dashboard.js) can DOM-swap the fragment in place without reloading the page when an event fires. The wrappers use `display: contents` so they don't disturb the CSS grid.

## Captive-portal UX by platform

End-to-end tested on the dev Pi 4B:

| Platform | Behavior |
|---|---|
| **iOS 17 / 18** (iPhone 12, iPhone 16 Pro Max) | Joining the AP auto-opens the Captive Network Assistant (CNA), which loads the PrepperPi landing page directly. Tapping the `X` / "Cancel" offers "Use Without Internet," which dismisses CNA and leaves the network connected for manual browsing. |
| **Stock Android 11+** (Pixel, most Android) | Expected: a "Sign in to Wi-Fi network" notification appears; tapping it opens the captive webview pointed at the landing page. *(Untested on hardware; behavior inferred from AOSP's `CaptivePortalLoginActivity` code path.)* |
| **Samsung One UI 5 (Android 13)** (Galaxy Note20) | **The auto-pop captive UI does not appear** — Samsung's One UI 5 captive handler silently marks the network as "connected, no internet" without surfacing a notification. The fallback path works reliably: connect to the AP, then open Chrome or Samsung Internet and type any URL (e.g. `bbc.com`). The DNS hijack + Caddy's off-host redirect deliver the PrepperPi landing page. This is a documented Samsung quirk that every captive portal hits; it's not a bug in our config. |

## Install

```
sudo services/prepperpi-web/setup.sh
```

Installs the Caddy and OpenSSL apt packages, disables the stock `caddy.service` (so our unit owns port 80), mints a self-signed TLS cert at `/etc/prepperpi/ssl/`, drops the Caddyfile at `/etc/prepperpi/Caddyfile`, the landing page at `/opt/prepperpi/web/landing/`, and the unit at `/etc/systemd/system/prepperpi-web.service`, then enables it. Safe to re-run — the self-signed cert is regenerated only if its SAN doesn't match the current friendly name.

## Files shipped

| Path | What |
|---|---|
| `Caddyfile` | Caddy v2 config: catch-all `:80` + `:443` sharing one handler snippet via `import app`. |
| `prepperpi-web.service` | Systemd unit. Runs Caddy as the `caddy` user with `CAP_NET_BIND_SERVICE`. |
| `setup.sh` | Idempotent installer. |

## Deploy layout

| Installed path | Source |
|---|---|
| `/etc/prepperpi/Caddyfile` | `services/prepperpi-web/Caddyfile` |
| `/etc/prepperpi/ssl/{cert,key}.pem` | Generated at install time by `setup.sh`. |
| `/opt/prepperpi/web/landing/` | `web/landing/` |
| `/etc/systemd/system/prepperpi-web.service` | `services/prepperpi-web/prepperpi-web.service` |

## Customizing the landing page

Edit `web/landing/index.html`, `web/landing/style.css`, or `web/landing/dashboard.js`, rerun `setup.sh`, and `systemctl restart prepperpi-web`. No build step — Caddy serves the files as-is. The dashboard script is plain ES2017 vanilla JS (no transpiler, no bundler, no Node on the device).

To add a new dynamic tile region, add a `{{include "/_<name>.html"}}` call inside a `<div data-fragment="<name>">` in `index.html`, have whatever service owns that surface write a fragment file at `/opt/prepperpi/web/landing/_<name>.html`, and (optionally) add a row to `FRAGMENT_FOR_EVENT` in `dashboard.js` so an event of that type triggers the right fragment refresh.
