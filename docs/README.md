# PrepperPi documentation

Everything written down about PrepperPi, in one list. Most of it lives next to
the code it describes rather than in this directory — service documentation sits
in each service's own folder, so it gets updated in the same pull request as the
change it explains.

Start with the [project README](../README.md) if you haven't already; it covers
what PrepperPi is, the hardware it runs on, and how to flash it.

## If you own one

| Document | What it covers |
|---|---|
| [Project README](../README.md) | What it is, hardware, quick start, shutting down, reaching it over Ethernet, known limitations. |
| [Boot-partition configuration](../images/boot-partition/README.md) | Setting the Wi-Fi name and password, enabling SSH, static IPs — all by dropping files on the SD card before first boot. Also the recovery procedure if you lock yourself out. |
| [Backup and recovery](backup-and-recovery.md) | Making a flashable disaster-recovery image, exporting settings to move to a replacement Pi, and growing the filesystem. |
| [Update notifier](update-notifier.md) | How PrepperPi decides your content is out of date, and what the Updates page does with that. |
| [Content licenses](../CONTENT-LICENSES.md) | The terms attached to the content PrepperPi downloads. These are **not** covered by the project's own MIT-0 license. |

## If you're adding content

| Document | What it covers |
|---|---|
| [Creating bundles](creating-bundles.md) | The bundle manifest schema and how to author one. Mirrors the [prepperpi-bundles](https://github.com/jmarler/prepperpi-bundles) README so the full spec is available from either repo. |

## If you're contributing code

| Document | What it covers |
|---|---|
| [Contributing](../CONTRIBUTING.md) | How to propose a change, branch naming, commit style, and what CI checks. |
| [Code of conduct](../CODE-OF-CONDUCT.md) | Expected behaviour, and what happens if it isn't met. |
| [Clean-room policy](../README.md#clean-room-policy) | **Read before writing code.** PrepperPi is built from publicly-available descriptions only. |
| [Installer](../installer/README.md) | What `install.sh` does, in order, and the flags it accepts. |
| [Release engineering](release-engineering.md) | Cutting a release: tagging, the signed build pipeline, and verifying the artifacts. |
| [CI workflows](../.github/workflows/README.md) | What each GitHub Actions workflow does and when it fires. |
| [Security pen-test, April 2026](security-pentest-2026-04.md) | Per-finding report on the admin console: what was tested, what changed, and the residual risks. The trust model it describes is still current. |

## Service reference

One directory per service under [`services/`](../services/), each with its own
README covering what it does, the files it installs, and how to debug it.

| Service | Role |
|---|---|
| [prepperpi-ap](../services/prepperpi-ap/) | The Wi-Fi access point: hostapd, dnsmasq, DHCP, and the captive-portal DNS hijack. |
| [prepperpi-web](../services/prepperpi-web/) | Caddy out front. Captive-portal probes, the landing page, and the routing that puts every other service behind one address. |
| [prepperpi-admin](../services/prepperpi-admin/) | The admin console at `/admin/`. Network, storage, content catalog, maps, bundles, updates, backup, and power. Also documents the privileged-worker trust boundary. |
| [prepperpi-kiwix](../services/prepperpi-kiwix/) | Serves ZIM files — Wikipedia and the rest of the Kiwix library — at `/library/`. |
| [prepperpi-tiles](../services/prepperpi-tiles/) | Offline vector maps: the tile server and the MapLibre client at `/maps/`. |
| [prepperpi-usb](../services/prepperpi-usb/) | Auto-mounts USB drives and serves them at `/usb/`, with in-browser preview. |
| [prepperpi-aria2c](../services/prepperpi-aria2c/) | The download daemon behind the content catalog. Pause, resume and cancel survive a reboot. |
| [prepperpi-events](../services/prepperpi-events/) | Small shared helper other services use to push events to the dashboard. |

## Conventions

- **Documentation ships with the code it describes.** A pull request that changes
  behaviour updates the affected README in the same commit. There is no separate
  docs backlog, and no follow-up "docs:" commit.
- **Service documentation lives with the service**, not here. This directory is
  for material that spans services or doesn't belong to one.
- **Keep user-facing material approachable.** Someone who has never used a
  terminal should be able to follow the project README and the boot-partition
  guide. Internals can assume more.
