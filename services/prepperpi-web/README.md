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
- **Landing page** — `web/landing/index.html` served from `/opt/prepperpi/web/landing/`. Four tiles (Library / Maps / Admin / USB), static placeholders for now. No JavaScript.
- **`/healthz`** — plain `200 ok` for curl-from-Pi smoke checks.

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

Edit `web/landing/index.html` and `web/landing/style.css`, rerun `setup.sh`, and `systemctl restart prepperpi-web`. The page is intentionally plain HTML + CSS — no build step, no JavaScript dependency — so the Pi doesn't need Node/npm at runtime.
