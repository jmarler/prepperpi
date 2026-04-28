<div align="center">

# PrepperPi

**An open-source, DIY offline "survival computer" for the Raspberry Pi.**

Self-hosted Wi-Fi. Offline Wikipedia, maps, medical references, repair guides, and more — no internet, no subscriptions, no tracking.

[![License: MIT-0](https://img.shields.io/badge/license-MIT--0-blue)](LICENSE)
[![Status: Beta](https://img.shields.io/badge/status-beta-blue)](#status)
[![Platform: Raspberry Pi 4B / 5](https://img.shields.io/badge/platform-Raspberry%20Pi%204B%20%2F%205-c51a4a)](#hardware)

</div>

---

## Status

PrepperPi is **beta**. Every feature in the box has been used by the maintainer on real Pi 4B hardware. You should expect rough edges and report them. Release engineering — signed images on GitHub Releases via tag-triggered CI — is the last item between beta and 1.0.

**What's in the box**

- **Self-hosted Wi-Fi AP** with a captive portal that auto-pops on iPhone (Samsung needs one URL typed). Open or WPA2 via boot-partition config.
- **Kiwix library** — drop a `.zim` into `/srv/prepperpi/zim/` and it serves immediately with full search.
- **Offline maps** — vector tile server with a 200-country region downloader that extracts country-sized PMTiles directly out of the daily planet via HTTP range requests.
- **Admin console** at `/admin/` (AP-subnet only) — Wi-Fi settings, storage and health dashboard, content catalog (~3500 ZIMs filterable), one-click bundles, update notifier with side-by-side ZIM updates.
- **USB content hosting** — auto-mount, in-browser file viewer with PDF / image / video / audio playback, ZIMs on USB auto-import into the library while plugged in.
- **Backup and recovery** — flashable disaster-recovery image to USB, plus a small config-export `.tar.gz` for moving settings to a replacement Pi without lugging content.

**Not yet shipped**

- Tag-triggered GitHub Releases with signed `.img.xz` artifacts (in progress).
- Optional offline place-name search and turn-by-turn routing.

Star the repo to be notified as 1.0 lands, or jump to the [roadmap](#roadmap).

## What is PrepperPi?

PrepperPi turns a Raspberry Pi 4B or Pi 5 into a plug-and-play offline reference library. Plug in power, the Pi broadcasts its own Wi-Fi network, and any phone, tablet, or laptop that joins gets local copies of Wikipedia, OpenStreetMap, iFixit repair guides, medical references, survival literature, and any files you've loaded onto a USB drive. No cell signal. No Wi-Fi uplink. No accounts.

It's a free, MIT-0 licensed, clean-room equivalent of commercial offline-library devices. PrepperPi ships the *appliance* — Wi-Fi, web stack, admin console, downloader. Content comes directly from its original sources ([Kiwix](https://kiwix.org), [OpenMapTiles](https://openmaptiles.org), [Project Gutenberg](https://www.gutenberg.org), FEMA, and others) through the admin console.

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
             │ Landing  │  │ Kiwix   │  │ tileserver-gl   │
             │ page     │  │ serve   │  │ -light (vector) │
             └──────────┘  └─────────┘  └─────────────────┘
             ┌──────────┐  ┌─────────┐  ┌─────────────────┐
             │ Admin    │  │ USB     │  │ Updater         │
             │ console  │  │ share   │  │ (online mode)   │
             └──────────┘  └─────────┘  └─────────────────┘

                    systemd orchestrates everything.
                    OS: Raspberry Pi OS Lite (64-bit, Trixie).
```

Content lives at `/srv/prepperpi`, ideally on an external SSD or NVMe so the SD card stays read-mostly and survives longer.

## Hardware

PrepperPi targets the Raspberry Pi 4B and Pi 5. Older Pis aren't supported in v1 (the AP-grade radio and 64-bit-only assumption rule them out).

Rough USD ranges from US retailers (Adafruit, Pimoroni) as of 2026-04. DRAM costs have been volatile and stock is spotty for several SKUs — check current prices and availability before ordering.

| Board | RAM | Price | Good for | Notes |
|---|---|---|---|---|
| Raspberry Pi 5 | 4 GB | ~$115–135 | Solo or small-household | Active cooling recommended. NVMe HAT unlocks fast content storage. |
| Raspberry Pi 5 | 8 GB | ~$185–205 | Most households | Best balance of cost and headroom for new buyers. |
| Raspberry Pi 5 | 16 GB | ~$300–340 *(stock spotty)* | Heavier deployments | Headroom for the future LLM module. |
| Raspberry Pi 4B | 4 GB | ~$105–125 | If you already own one | Default build skips optional routing/geocoding modules. |
| Raspberry Pi 4B | 8 GB | ~$175–195 | If you already own one | Same ballpark as Pi 5 8 GB now; pick the 5 if you're buying new. |

**For new buyers**, the Pi 5 8 GB is the practical default — it's now within ~$10 of the Pi 4B 8 GB but ~3× faster. The Pi 4B is still fully supported; if you already have one, it works great. Skip the 1 GB and 2 GB tiers — they don't have enough RAM for the full stack under concurrent load.

**Storage for content** — content gets large fast and you don't want to grind your SD card.

| Tier | Capacity | Price | Notes |
|---|---|---|---|
| USB 3 SSD (Pi 4B) | 512 GB | ~$50 | Fits Wikipedia + maps + medical comfortably. |
| USB 3 SSD (Pi 4B) | 1 TB | ~$80 | Room for the Complete bundle. |
| NVMe + Pi 5 HAT | 1 TB | ~$70 + ~$25 | Faster than USB 3, ships in the same form factor. |

**Other parts**

- **microSD card for the OS** — 16 GB+ A2-rated, ~$10–15. Cheap, replaceable.
- **Power supply (bench use)** — official Pi USB-C, ~$10 for 4B (5 V / 3 A) or ~$15 for Pi 5 (5 V / 5 A). Don't skimp; brownouts corrupt SD cards.
- **Power bank (field use)** — 20 000 mAh USB-C PD bank, ~$50–80. Anker, INIU, or similar.

## Quick start

### Path A — flash a prebuilt image (non-technical)

> 1.0 release with signed `.img.xz` artifacts is in progress. Until that ships, see Path B or build the image yourself with `images/build.sh` (Docker required, ~5 min on an ARM64 host).

Once 1.0 ships, this becomes: download from [Releases](https://github.com/jmarler/prepperpi/releases/latest) → flash with [Raspberry Pi Imager](https://www.raspberrypi.com/software/) → boot → join `PrepperPi-XXXX`.

### Path B — install on existing Raspberry Pi OS Lite (maker, works today)

Start with a fresh **Raspberry Pi OS Lite (64-bit, Bookworm or Trixie)** install on a Pi 4B or Pi 5, then:

```bash
git clone https://github.com/jmarler/prepperpi.git
cd prepperpi
sudo installer/install.sh
```

The installer runs preflight checks, asks you once to confirm the final reboot, then proceeds unattended: apt deps, systemd units, `prepperpi` system user, `/srv/prepperpi/`, reboot into AP mode. Idempotent — safe to re-run.

After reboot: join the `PrepperPi-XXXX` Wi-Fi → portal opens (iPhone) or open any URL in Chrome (Samsung). See [`installer/README.md`](installer/README.md) for flags (`--yes` for `curl | bash`, `--no-reboot` to stay in place).

### Pre-flash configuration

Boot-partition files work on either path. Drop them on the FAT32 `bootfs` volume before first boot:

| File | Purpose |
|---|---|
| `prepperpi.conf` | Override Wi-Fi SSID, password, channel, country code — see [`services/prepperpi-ap/prepperpi.conf.example`](services/prepperpi-ap/prepperpi.conf.example). |
| `user-data` | Cloud-init: install your SSH pubkey, set the hostname, lock the default password. Template: [`images/boot-partition/user-data.example`](images/boot-partition/user-data.example). |
| `network-config` | Cloud-init netplan: static IP, or client-mode Wi-Fi on a second radio. Template: [`images/boot-partition/network-config.example`](images/boot-partition/network-config.example). |
| `ssh` *(empty file)* | Older Pi OS marker — `touch /Volumes/bootfs/ssh` enables SSH at boot if you don't want a full `user-data`. |

Full walk-through (mount, copy, edit, eject) is in [`images/boot-partition/README.md`](images/boot-partition/README.md).

**The prebuilt image ships with default login `prepper` / `prepperpi`** for headless first-boot. **Change it before putting the device on any shared network.**

## Content

PrepperPi ships the *downloader*, never the content itself. Install through the admin console:

| Category | What you get | Source | Content license |
|---|---|---|---|
| Encyclopedias | Wikipedia (all Kiwix languages), Wikiversity, Wiktionary | Kiwix | CC BY-SA |
| Repair | iFixit | Kiwix | CC BY-NC-SA |
| Medical | WikiMed, MedlinePlus, US military medicine field manuals | Kiwix, NIH, public-domain archives | Public domain / CC |
| Education | Khan Academy Lite, Stack Exchange | Kiwix | CC BY-NC-SA / CC BY-SA |
| Literature | Project Gutenberg (60 000+ books) | Kiwix / Gutenberg | Public domain |
| Maps | Daily-built OpenStreetMap PMTiles, ~200 country extracts | Protomaps planet PMTiles + OSM | ODbL |
| Emergency | Ready.gov (FEMA), Nuclear War Survival Skills, US Army FM 21-76 survival manual | ready.gov, OISM, public-domain military archives | Public domain |
| Your own | Anything you drop on a USB drive | You | You |

> **Notes on Kiwix's library churn.** Kiwix periodically retires ZIMs whose upstream license terms change. As of 2026-04, **WikiHow** and the general **TED talks** ZIMs are no longer published by Kiwix and have been removed from PrepperPi's bundles. Themed TED collections like `ted_mul_sustainability` are still available for individual install via the Content page.

### Bundles

One-click curated sets installable from the admin console's Bundles page:

- **Starter** — compact preparedness kit (~5 GB; fits on a 32 GB SD card alongside the OS).
- **Complete** — full English Wikipedia + medical + repair + literature + Stack Exchanges (~130 GB; plan for an SSD ≥ 256 GB).
- **Medical** — focused medical reference: WikEM full + Wikipedia's medical subset (~1 GB).
- **Education** — Wikipedia mini, Wikibooks, medical subset, Appropedia (~16 GB).

The four official bundles are baked into the SD image so they're available offline; when online, the admin console refreshes from [`prepperpi-bundles`](https://github.com/jmarler/prepperpi-bundles) for the latest. Anyone can host their own bundle source — see [`docs/creating-bundles.md`](docs/creating-bundles.md).

## Roadmap

**Phase 1 — Bootable base appliance.** ✅ **Shipped.** Installer + prebuilt SD image, Wi-Fi AP, captive portal landing page.

**Phase 2 — Content and maps.** ⏳ **Mostly shipped.** Kiwix ✅, USB hosting ✅, live dashboard ✅, ZIM catalog ✅, offline tile server ✅, region downloader ✅. Still ahead: optional offline place-name search and routing.

**Phase 3 — Admin console and updates.** ✅ **Shipped.** Network ✅, online mode ✅, storage and health ✅, maps panel ✅, bundles ✅, update notifier ✅.

**Phase 4 — Polish and release.** ⏳ **In progress.** Disaster-recovery image ✅, config export/import ✅. Still ahead: signed release pipeline, auto-generated release notes, community channels.

Possible futures (not committed): non-Pi SBC support, an optional offline LLM assistant over your library, mesh between multiple PrepperPis, APRS and Winlink ham-radio integrations.

## Known limitations (beta)

- **Samsung Galaxy devices** don't auto-open the captive portal. Workaround: type any URL in a browser after connecting.
- **Pi 5 isn't yet end-to-end verified.** All testing has been on a Pi 4B 8 GB. Pi 5 support is in the code but a fresh flash-and-boot test on real Pi 5 hardware is pending.
- **Maps downloader is one-shot, not resumable.** Most countries are 50–500 MB; the giant ones (US ≈ 1.5 GB, Russia ≈ 1.2 GB) hurt to interrupt.
- **Online mode is Ethernet-only.** No Wi-Fi role-swap on the onboard radio (would drop the AP). USB Wi-Fi dongle as a client *and* keep the AP up is a stretch story, not shipped.
- **No GitHub Release artifacts yet.** Build images locally or use the installer path until tag-triggered releases ship.

More detail in [`docs/`](docs/).

## Licensing

**Code.** PrepperPi's source is [MIT No Attribution (MIT-0)](LICENSE). Do whatever you want with the code, no attribution required, no warranty.

**Content.** Each source sets its own terms — they are **not** covered by PrepperPi's code license. The ones that matter in practice:

- **Wikipedia** is CC BY-SA: redistribute with attribution under the same license.
- **Khan Academy** and **iFixit** are CC BY-NC-SA: free for personal and educational use, **not for commercial redistribution**. Selling a preloaded PrepperPi means these have to come off, or you need a separate arrangement with the publisher.
- **OpenStreetMap** data is ODbL: redistribute with attribution and keep derivatives open.
- **US Government** material (FEMA, military manuals, NIH) and **Project Gutenberg** are public domain.

The downloader surfaces each item's license in the admin console. If you're doing anything beyond personal use, read the license on every bundle before you hit install.

## Clean-room policy

PrepperPi is built from publicly-available descriptions only. Contributors **must not** reference the shipped image of any commercial offline-library product, copy from or dump an SD card from such a product, or include content redistributed under an exclusive or licensed-only arrangement. Marketing pages, FAQs, comparison charts, public reviews, and the documentation of upstream open-source projects are fair game. If you're unsure, open an issue before you write the code.

## Contributing

Contributions are welcome. Because PrepperPi is MIT-0 licensed, contributors are not required to sign a CLA; by opening a pull request you are releasing your contribution under MIT-0.

In the meantime (a proper `CONTRIBUTING.md` is coming):

1. Open an issue describing what you want to build or fix.
2. Fork and branch from `main`.
3. Keep commits focused, with `conventional-commit` prefixes (`feat:`, `fix:`, `docs:`, `refactor:`, `chore:`).
4. Respect the [clean-room policy](#clean-room-policy).
5. PR CI runs shellcheck + the admin app's unit tests + bundle YAML validation on every PR (~under 2 minutes). It must be green before merge.

## Community and support

- **GitHub Discussions** — questions, ideas, show-and-tell: [github.com/jmarler/prepperpi/discussions](https://github.com/jmarler/prepperpi/discussions).
- **GitHub Issues** — bug reports and feature requests: [github.com/jmarler/prepperpi/issues](https://github.com/jmarler/prepperpi/issues).
- **Discord / Matrix** — TBD; will be linked here once a room exists.

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
