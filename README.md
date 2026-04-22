<div align="center">

# PrepperPi

**An open-source, DIY offline "survival computer" for the Raspberry Pi.**

Self-hosted Wi-Fi. Offline Wikipedia, maps, medical references, repair guides, and more — no internet, no subscriptions, no tracking.

[![License: MIT-0](https://img.shields.io/badge/license-MIT--0-blue)](LICENSE)
[![Status: Pre-Alpha](https://img.shields.io/badge/status-pre--alpha-orange)](#status)
[![Platform: Raspberry Pi 4B / 5](https://img.shields.io/badge/platform-Raspberry%20Pi%204B%20%2F%205-c51a4a)](#hardware)

</div>

---

## Status

PrepperPi is **pre-v1**. The plan is complete and the repository is being stood up; there is nothing here to install yet. Star the repo if you want to be notified when the first image drops, or dive into the [roadmap](#roadmap) if you'd like to help build it.

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
                    OS: Raspberry Pi OS Lite (64-bit, Bookworm+).
```

Content lives on a separate SSD or USB 3 drive at `/srv/prepperpi`, so your SD card stays read-mostly and survives longer.

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

> Neither path works yet — this is what the quick start will look like once v1 ships.

### Path A — flash a prebuilt image (non-technical)

1. Download the latest `prepperpi-<version>-<pi4|pi5>.img.xz` from [GitHub Releases](https://github.com/jmarler/prepperpi/releases).
2. Verify the SHA-256 against the published `SHA256SUMS`.
3. Flash with [Raspberry Pi Imager](https://www.raspberrypi.com/software/) onto a 16 GB+ SD card.
4. Optionally edit `prepperpi.conf` on the boot partition to set your SSID, password, locale, and timezone.
5. Insert the card, plug the Pi into power, and wait about five minutes.
6. Join the `PrepperPi-XXXX` Wi-Fi from any phone. The home page opens automatically.

### Path B — install on an existing Raspberry Pi OS Lite (maker)

```bash
curl -fsSL https://get.prepperpi.org | bash
```

The installer detects the Pi model, installs dependencies, writes systemd units, creates `/srv/prepperpi`, and reboots into AP mode. Re-running the installer is idempotent.

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

**Phase 1 — Bootable base appliance.** Installer + prebuilt SD image, Wi-Fi access point, captive portal landing page.

**Phase 2 — Content and maps.** Kiwix library serving, USB content hosting, offline vector maps with multi-region support.

**Phase 3 — Admin console and updates.** Browser-based settings, live health, one-click content bundles, online mode for updates.

**Phase 4 — Polish and release.** Backup and restore, signed release images, documentation, community channels.

Possible futures (not committed): non-Pi SBC support, an optional offline LLM assistant over your library, mesh between multiple PrepperPis, APRS and Winlink ham-radio integrations.

## Licensing

**Code.** PrepperPi's own source code is released under the [MIT No Attribution (MIT-0)](LICENSE) license. In practice: do whatever you want with the code, no attribution required, no warranty.

**Content.** The content you install is **not** covered by PrepperPi's code license — each source sets its own terms. A few that matter in practice:

- Wikipedia is **CC BY-SA**: free to redistribute with attribution under the same license.
- WikiHow, Khan Academy, and iFixit are **CC BY-NC-SA**: free for personal and educational use, but **not for commercial redistribution**. If you're ever tempted to sell a preloaded PrepperPi, these have to come off first, or you need a separate arrangement with the content publisher.
- OpenStreetMap data is **ODbL**: redistribute with attribution and keep derivatives open.
- US Government material (FEMA, military manuals, NIH) and Project Gutenberg are **public domain**.

The downloader surfaces each item's license in the admin console. If you're doing anything beyond personal use, read the license on every bundle before you hit install.

## Clean-room policy

PrepperPi is built from publicly-available descriptions only. Contributors **must not** reference the shipped image of any commercial offline-library product, photograph or dump an SD card from such a product, or include content that was redistributed under an exclusive or licensed-only arrangement. Marketing pages, FAQs, comparison charts, public reviews, and the documentation of upstream open-source projects are fair game. If you're unsure, open an issue before you write the code.

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
