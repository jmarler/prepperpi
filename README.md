<div align="center">

# PrepperPi

**An open-source, DIY offline "survival computer" for the Raspberry Pi.**

Self-hosted Wi-Fi. Offline Wikipedia, maps, medical references, repair guides, and more — no internet, no subscriptions, no tracking.

[![License: MIT-0](https://img.shields.io/badge/license-MIT--0-blue)](LICENSE)
[![Status: Alpha](https://img.shields.io/badge/status-alpha-yellow)](#status)
[![Platform: Raspberry Pi 4B / 5](https://img.shields.io/badge/platform-Raspberry%20Pi%204B%20%2F%205-c51a4a)](#hardware)

</div>

---

## Status

PrepperPi is **alpha**. The base appliance (Epic 1), most of the content layer (Epic 2), and the network and online-mode pieces of the admin console (Epic 4) are end-to-end verified on a Pi 4B. Maps, the rest of the admin console, and release engineering are still ahead.

**What works today:**

- ✅ Flashable SD card image OR install-on-existing-Pi-OS path
- ✅ Wi-Fi access point (`PrepperPi-XXXX`), open or WPA2 via boot-partition config
- ✅ Captive portal — iPhone auto-pops, Samsung requires typing any URL in a browser (documented quirk)
- ✅ Live landing page with dynamic content tiles
- ✅ **Kiwix library** — drop a `.zim` into `/srv/prepperpi/zim/` and it appears as a tile within seconds; full search across all books; click a tile to open the Kiwix reader
- ✅ **USB content hosting** — auto-mount FAT32/exFAT/NTFS/ext drives at `/srv/prepperpi/user-usb/<label>/`; in-browser file viewer with directory listing, PDF/image/video/audio playback, and Markdown rendering; ZIMs on USB auto-import into the kiwix library while plugged in (local-disk wins on duplicates); clean tear-down on unplug
- ✅ **Live dashboard** — toast notifications and in-place tile refreshes when USB drives plug/unplug or library content changes; pure progressive enhancement (no-JS users see the same content with a manual refresh)
- ✅ **Admin console — Network panel** — browser-based admin at `/admin/` reachable only from the AP subnet; change SSID, Wi-Fi password, channel, country, with one-click reset to factory defaults. Form-based with no-JS fallback; an unprivileged FastAPI process shells out via sudo to a privileged worker that's the only thing allowed to rewrite `/boot/firmware/prepperpi.conf` and bounce hostapd
- ✅ **Online mode (Ethernet uplink)** — plug an Ethernet cable in and the admin home page surfaces an "Ethernet uplink active" banner; the Pi can reach the internet for downloads, but the AP keeps running and AP clients are firewalled off the upstream (the Pi is not a hotspot)
- ✅ **Storage and health panel** — live CPU / RAM / SoC temperature / disk-free / connected-client count at 1 Hz; per-USB write toggle (session-only — re-plug resets to read-only); recent event log + downloadable JSON of the last 500 events; one-click diagnostics tarball

**Not yet shipped (planned):**

- Catalog selector for downloading ZIMs over an online uplink (Epic 2-S3)
- Vector tile server for offline maps (Epic 3)
- One-click content bundles and update engine (Epic 5)
- Config export, backup to USB (Epic 6)
- Signed release images, auto-generated release notes (Epic 7)

Star the repo if you want to be notified as each phase lands, or dive into the [roadmap](#roadmap) if you'd like to help build it.

## What is PrepperPi?

PrepperPi turns a Raspberry Pi 4B or Pi 5 into a plug-and-play offline reference library. Plug it into power, and the Pi broadcasts its own Wi-Fi network. Join the network from any phone, tablet, or laptop, and browse private, local copies of Wikipedia, OpenStreetMap, iFixit repair guides, medical references, survival literature, and any files you've loaded onto a USB drive. No cell signal. No Wi-Fi uplink. No accounts.

It is a free, open-source, clean-room equivalent of commercial offline-library devices. The code is MIT-0 licensed; PrepperPi is the glue, not the content — you install content from its original sources ([Kiwix](https://kiwix.org), [OpenMapTiles](https://openmaptiles.org), [Project Gutenberg](https://www.gutenberg.org), FEMA, and others) directly from the admin console.

## Features

- Broadcasts its own Wi-Fi access point, `PrepperPi-XXXX`, out of the box.
- Captive portal — join the Wi-Fi and your device opens the home page automatically.
- Offline Wikipedia (every language Kiwix publishes), iFixit, WikiHow, Project Gutenberg, Khan Academy Lite, Stack Exchange, and the Kiwix medical pack.
- Offline street maps for North America, Europe, and Oceania, with optional place-name search and turn-by-turn routing.
- Plug-in USB storage is auto-mounted and exposed in the landing page.
- Browser-based admin console for network settings, content bundles, updates, and live health.
- "Online mode" for downloading updates over a temporary uplink, then back to fully offline.
- No telemetry, no cloud accounts, no phone-home. Ever.

## How it works

```
                   ┌───────────────────────────────┐
 [Phone/laptop]───▶│   Wi-Fi AP (hostapd)          │
 [Phone/laptop]───▶│   DHCP/DNS (dnsmasq)          │
 [Phone/laptop]───▶│   Captive portal redirect     │
                   └───────────────┬───────────────┘
                                   │
                   ┌───────────────▼───────────────┐
                   │   Reverse proxy (Caddy)       │
                   └─┬─────────┬────────┬──────────┘
                     │         │        │
             ┌───────▼──┐  ┌──▼──────┐  ┌▼────────────────┐
             │ Landing  │  │ Kiwix   │  │ TileServer GL   │
             │ page     │  │ serve   │  │ (OpenMapTiles)  │
             └──────────┘  └─────────┘  └─────────────────┘
             ┌──────────┐  ┌─────────┐  ┌─────────────────┐
             │ Admin    │  │ USB     │  │ Updater         │
             │ console  │  │ share   │  │ (online mode)   │
             └──────────┘  └─────────┘  └─────────────────┘

                    systemd orchestrates everything.
                    OS: Raspberry Pi OS Lite (64-bit, Trixie).
```

Content lives on a separate SSD or USB 3 drive at `/srv/prepperpi`, so your SD card stays read-mostly and survives longer.

When the dashboard tab is open, a tiny client-side script polls a small JSON event log at `/_events.json` (written by the same systemd reindex services that maintain the tile fragments), surfaces toast notifications for state changes, and DOM-swaps the affected tiles in place. Polling pauses when the tab is hidden. With JavaScript disabled, the page renders identically with whatever state existed at request time — toasts and auto-refresh are progressive enhancements, not load-bearing.

## Hardware

PrepperPi targets the Raspberry Pi 4B and Pi 5. Older Pis are not supported in v1.

| Board | RAM | Good for | Notes |
|---|---|---|---|
| Raspberry Pi 4B | 4 GB | Solo or small-household use | Default build skips optional routing/geocoding modules. |
| Raspberry Pi 4B | 8 GB | Most households | Sweet spot for cost and performance. |
| Raspberry Pi 5 | 4 GB | Field / NGO use | Active cooling recommended. NVMe HAT unlocks fast content storage. |
| Raspberry Pi 5 | 8 GB+ | Heavier deployments | Needed if you want to experiment with the future LLM module. |

You'll also want:

- **Storage for content:** USB 3.0 SSD (Pi 4B) or NVMe via a HAT (Pi 5). 512 GB is a comfortable default for a Premium-equivalent library.
- **SD card for the OS:** a 16 GB+ A2-rated card. Cheap, replaceable.
- **Power:** the official Pi USB-C supply (5 V / 3 A for Pi 4B, 5 V / 5 A for Pi 5) for bench use; any decent 20 000 mAh USB-C PD battery bank for field use.

## Quick start

### Path A — install on an existing Raspberry Pi OS Lite (maker, works today)

Start with a fresh **Raspberry Pi OS Lite (64-bit, Bookworm or Trixie)** install on a Pi 4B or Pi 5. Then:

```bash
git clone https://github.com/jmarler/prepperpi.git
cd prepperpi
sudo installer/install.sh
```

The installer runs its preflight checks, asks you once to confirm that it's OK to reboot at the end, then proceeds unattended. It installs apt dependencies, writes systemd units, provisions a `prepperpi` system user, lays down `/srv/prepperpi/`, and reboots into AP mode. Re-running is idempotent.

After reboot: join the `PrepperPi-XXXX` Wi-Fi from any phone → the portal opens (iPhone), or open any URL in Chrome (Samsung). See [`installer/README.md`](installer/README.md) for flags (`--yes` for `curl | bash`, `--no-reboot` to stay in place).

The curl-bash one-liner (`curl -fsSL get.prepperpi.org | bash`) is planned for Epic 7 once there's a CDN-hosted release.

### Path B — flash a prebuilt image (non-technical, works today if you build it yourself)

A flashable `.zip` image is produced by `images/build.sh`. On an ARM64 host (Apple Silicon Mac, a Pi itself, GitHub's `ubuntu-24.04-arm` runners) the build takes ~5 minutes in Docker and produces an ~810 MB artifact. On an x86_64 host with qemu it's ~45–90 minutes.

```bash
images/build.sh      # needs Docker Desktop / docker + git; output at images/out/
```

Flash the resulting `.zip` with [Raspberry Pi Imager](https://www.raspberrypi.com/software/) ("Choose OS" → "Use custom"). On first boot the Pi comes up as `PrepperPi-<mac4>` within ~90 seconds.

GitHub Releases with prebuilt images, SHA-256 sums, and GPG signatures are planned for Epic 7. Until then, build from source.

### Pre-flash configuration

Boot partition files work the same on both paths. Drop them on the FAT32 `bootfs` volume before first boot:

| File | Purpose |
|---|---|
| `prepperpi.conf` | Override Wi-Fi SSID, password, channel, country code — see [`services/prepperpi-ap/prepperpi.conf.example`](services/prepperpi-ap/prepperpi.conf.example). |
| `user-data` | Cloud-init user config: install your SSH pubkey, set the hostname, lock the default password. Starting template: [`images/boot-partition/user-data.example`](images/boot-partition/user-data.example). |
| `network-config` | Cloud-init netplan: static IP, or client-mode Wi-Fi on a second radio. Starting template: [`images/boot-partition/network-config.example`](images/boot-partition/network-config.example). |
| `ssh` *(empty file)* | Older Pi OS marker — still honored by `sshswitch.service`, so `touch /Volumes/bootfs/ssh` enables SSH at boot if you don't want to write a full `user-data`. |

Full walk-through (mount, copy, edit, eject) is in [`images/boot-partition/README.md`](images/boot-partition/README.md).

**The prebuilt image ships with default login `prepper` / `prepperpi`** to keep headless first-boot working. **Change it before putting the device on any shared network.**

## Content

PrepperPi ships the *downloader*, never the content itself. Here's what you can install through the admin console:

| Category | What you get | Source | Content license |
|---|---|---|---|
| Encyclopedias | Wikipedia (all Kiwix languages), Wikiversity, Wiktionary | Kiwix | CC BY-SA |
| Repair | iFixit | Kiwix | CC BY-NC-SA |
| How-to | WikiHow | Kiwix | CC BY-NC-SA |
| Medical | WikiMed, MedlinePlus, US military medicine field manuals | Kiwix, NIH, public-domain archives | Public domain / CC |
| Education | Khan Academy Lite, Stack Exchange | Kiwix | CC BY-NC-SA / CC BY-SA |
| Literature | Project Gutenberg (60 000+ books) | Kiwix / Gutenberg | Public domain |
| Maps | OpenMapTiles MBTiles for North America, Europe, Oceania | OpenMapTiles / Geofabrik | ODbL |
| Emergency | Ready.gov (FEMA), Nuclear War Survival Skills, US Army FM 21-76 survival manual | ready.gov, OISM, public-domain military archives | Public domain |
| Video | TED talks (what Kiwix publishes) | Kiwix | CC BY-NC-ND |
| Your own | Anything you drop on a USB drive | You | You |

Content bundles (`Starter`, `Premium`, `Medical-only`, `Education-only`) install the curated sets in one click.

## Roadmap

**Phase 1 — Bootable base appliance.** ✅ **Shipped (2026-04).** Installer + prebuilt SD image, Wi-Fi access point, captive portal landing page. All four stories merged.

**Phase 2 — Content and maps.** ⏳ **In progress.** Kiwix library serving (E2-S1) ✅, USB content hosting (E2-S2) ✅, live dashboard with event toasts (E2-S4) ✅. Still ahead: ZIM catalog selector for downloading over an uplink (E2-S3), and the entire offline vector-maps stack (E3).

**Phase 3 — Admin console and updates.** ⏳ **In progress.** Network settings (E4-S1) ✅, Ethernet online mode (E4-S3) ✅, Storage and health (E4-S2) ✅. Still ahead: one-click content bundles, update notifier.

**Phase 4 — Polish and release.** Backup and restore, signed release images, auto-generated release notes, documentation, community channels.

Possible futures (not committed): non-Pi SBC support, an optional offline LLM assistant over your library, mesh between multiple PrepperPis, APRS and Winlink ham-radio integrations.

## Known limitations (alpha)

- **Samsung Galaxy devices (One UI 5 / Android 13)** don't auto-open the captive portal on Wi-Fi attach — a documented vendor quirk that every captive portal hits. Workaround: after connecting, open a browser and type any URL; the portal will load. Stock Android (Pixel etc.) is expected to auto-pop but hasn't been tested on hardware.
- **Pi 5 is not yet verified end-to-end.** All development and testing so far has been on a Pi 4B 8 GB. Pi 5 support is in the code (`pi_model_default_max_sta` differentiates them, raspi-firmware installs for both) but a fresh flash-and-boot test on a Pi 5 is still pending hardware availability.
- **Maps aren't built yet.** The landing-page Maps tile renders as a placeholder "Not installed" card. TileServer GL (Phase 2 / E3) replaces it.
- **Admin console is partial.** Network, Ethernet online-mode, and Storage/health work end-to-end; the Content panel is scoped to E2-S3 and currently renders as a "(soon)" card on the admin overview.
- **Online mode is Ethernet-only by design.** No Wi-Fi role-swap on the onboard radio (avoids dropping the AP). A USB Wi-Fi dongle would let the Pi be a Wi-Fi client *and* keep the AP up — that's a stretch story, not shipped.
- **No content downloader UI.** Today you supply ZIMs by `cp` over SSH or by dropping them onto a USB. The in-browser catalog selector arrives in E2-S3.
- **No Release artifacts on GitHub.** For now, you build images locally or use the installer path. Tag-triggered GitHub Releases with GPG-signed artifacts are Phase 4.

## Licensing

**Code.** PrepperPi's own source code is released under the [MIT No Attribution (MIT-0)](LICENSE) license. In practice: do whatever you want with the code, no attribution required, no warranty.

**Content.** The content you install is **not** covered by PrepperPi's code license — each source sets its own terms. A few that matter in practice:

- Wikipedia is **CC BY-SA**: free to redistribute with attribution under the same license.
- WikiHow, Khan Academy, and iFixit are **CC BY-NC-SA**: free for personal and educational use, but **not for commercial redistribution**. If you're ever tempted to sell a preloaded PrepperPi, these have to come off first, or you need a separate arrangement with the content publisher.
- OpenStreetMap data is **ODbL**: redistribute with attribution and keep derivatives open.
- US Government material (FEMA, military manuals, NIH) and Project Gutenberg are **public domain**.

The downloader surfaces each item's license in the admin console. If you're doing anything beyond personal use, read the license on every bundle before you hit install.

## Clean-room policy

PrepperPi is built from publicly-available descriptions only. Contributors **must not** reference the shipped image of any commercial offline-library product, copy from or dump an SD card from such a product, or include content that was redistributed under an exclusive or licensed-only arrangement. Marketing pages, FAQs, comparison charts, public reviews, and the documentation of upstream open-source projects are fair game. If you're unsure, open an issue before you write the code.

## Contributing

Contributions are welcome. Because PrepperPi is MIT-0 licensed, contributors are not required to sign a CLA; by opening a pull request you are releasing your contribution under MIT-0.

A proper `CONTRIBUTING.md` is on the way. In the meantime:

1. Open an issue describing what you want to build or fix.
2. Fork and branch from `main`.
3. Keep commits focused, with `conventional-commit` prefixes (`feat:`, `fix:`, `docs:`, `refactor:`, `chore:`).
4. Respect the [clean-room policy](#clean-room-policy).
5. Run the test suite (once it exists) before opening the PR.

## Acknowledgments

PrepperPi is mostly a careful arrangement of other people's work. Standing on the shoulders of:

- [Kiwix](https://kiwix.org) — the offline-content ecosystem and the ZIM format.
- [OpenMapTiles](https://openmaptiles.org) and the wider [OpenStreetMap](https://www.openstreetmap.org) community.
- The [Raspberry Pi Foundation](https://www.raspberrypi.org) and the Raspberry Pi OS maintainers.
- [Caddy](https://caddyserver.com), [TileServer GL](https://github.com/maptiler/tileserver-gl), [MapLibre GL JS](https://maplibre.org), [OSRM](https://project-osrm.org), [Nominatim](https://nominatim.org), [FastAPI](https://fastapi.tiangolo.com), [aria2](https://aria2.github.io), [hostapd](https://w1.fi/hostapd), [dnsmasq](https://thekelleys.org.uk/dnsmasq/), and the many smaller tools that make this possible.
- [Internet-in-a-Box](https://internet-in-a-box.org) for pioneering this category of device.
- Every volunteer who writes a Wikipedia article, fixes an iFixit guide, answers a Stack Exchange question, or traces a road in OSM.

## Disclaimer

PrepperPi is an independent open-source project. It is **not** affiliated with, endorsed by, or connected to any commercial offline-library product or company. Any resemblance in category, function, or aesthetic is a function of the underlying open-source building blocks (Kiwix, OpenMapTiles, Raspberry Pi) that any such device is built from.
