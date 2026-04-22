# prepperpi-ap

Wi-Fi access point service. Implements [E1-S3 — Wi-Fi access point](../../#roadmap).

On every boot, `prepperpi-ap-configure.service` runs once, reads optional
overrides from `/boot/firmware/prepperpi.conf`, derives the SSID from the
onboard Wi-Fi MAC, picks the least-busy 2.4 GHz channel (1, 6, or 11), and
renders configs for `hostapd` and `dnsmasq` from the templates in this
directory. `prepperpi-ap.target` ties the three units together so the
admin console can start or stop the AP as a single thing.

## Files

| File | Role |
|---|---|
| `prepperpi-ap-configure.sh` | Main logic. Parses `prepperpi.conf`, computes SSID/channel, renders configs, brings up `wlan0` with `10.42.0.1/24`. Idempotent. |
| `prepperpi-ap-configure.service` | Oneshot unit that invokes the script before hostapd/dnsmasq start. |
| `prepperpi-ap.target` | Orchestration target bundling configure + hostapd + dnsmasq. |
| `hostapd.conf.tmpl` | Template with `@SSID@`, `@CHANNEL@`, `@COUNTRY@`, `@MAX_STA@`, `@AUTH_BLOCK@` placeholders. |
| `dnsmasq.conf.tmpl` | DHCP range `10.42.0.50–250/24` and captive DNS (everything resolves to `10.42.0.1`). |
| `prepperpi.conf.example` | User-facing overrides file dropped at `/boot/firmware/prepperpi.conf`. |
| `setup.sh` | Called by the top-level installer. Installs apt packages, copies files, enables units. |

## How it meets E1-S3 acceptance criteria

- **AC-1 (≤60 s to visible SSID)** — `prepperpi-ap-configure.service` is a oneshot that finishes in well under five seconds on a Pi 5; `hostapd` typically announces within another few seconds. Pi 4B with the onboard Broadcom chip is also comfortably under the 60 s budget. The configure unit orders itself `Before=hostapd.service dnsmasq.service` so the timing is deterministic.
- **AC-2 (`PrepperPi-<mac4>`)** — `default_ssid_from_mac()` reads `/sys/class/net/wlan0/address`, strips colons, takes the last four hex digits, and uppercases them. Example: a MAC of `dc:a6:32:aa:3a:7f` becomes `PrepperPi-3A7F`.
- **AC-3 (open by default, optional WPA2)** — `render_auth_block()` emits `wpa=0` when `WIFI_PASSWORD` is empty or absent, and a `wpa=2 + CCMP + passphrase` block otherwise. A passphrase outside the WPA2-mandated 8..63 characters fails the unit fast instead of silently falling back to open.
- **AC-4 (≥10 clients on 4B, ≥20 on 5)** — `pi_model_default_max_sta()` reads `/proc/device-tree/model` and emits `max_num_sta=20` on the Pi 5, `10` on the Pi 4B. Operators can raise this via `MAX_STA=` in `prepperpi.conf`; real capacity is RF-environment-dependent and documented here.
- **AC-5 (least-busy channel)** — `pick_channel()` runs `iw dev wlan0 scan`, tallies beacons observed on 2412/2437/2462 MHz (channels 1/6/11), and picks the least-loaded. Falls back to channel 6 when `iw` is missing or the scan fails (e.g. driver transient).

## Installing

From the repo root:

```bash
sudo services/prepperpi-ap/setup.sh
```

This is wrapped by `installer/install.sh` in the normal user flow. The
setup script is safe to re-run.

## Testing locally (without a real AP)

Unit tests for the pure-function helpers live at `../../tests/unit/test_prepperpi_ap_configure.sh`:

```bash
bash tests/unit/test_prepperpi_ap_configure.sh
```

They cover SSID derivation, auth-block rendering, and the
`prepperpi.conf` parser.

Full integration testing — actually running hostapd and seeing a client
associate — requires either real Pi hardware or a Linux VM with a
passthrough Wi-Fi adapter that supports AP mode. That's the scope of
the follow-up integration test in `tests/integration/`; not shipped in
this story.

## Known limitations

- **2.4 GHz only.** The Pi's onboard Broadcom/Cypress chipset can do 5 GHz client-mode reliably but AP-mode support is regulatory-domain-dependent and flaky. We ship 2.4 GHz for best range and compatibility. A future story can add a `BAND=5` mode for operators with known-good firmware.
- **No band steering.** Single-band, single-SSID.
- **No MAC randomization.** The SSID contains four hex digits of the onboard MAC; operators who want privacy can override `SSID=` in `prepperpi.conf`.
