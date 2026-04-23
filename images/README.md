# images/

Build the PrepperPi SD card image with [pi-gen](https://github.com/RPi-Distro/pi-gen) in Docker. Produces `.img.xz` artifacts that flash with Raspberry Pi Imager and boot straight into a configured PrepperPi appliance.

## Quick start (local build on ARM64 host)

```bash
images/build.sh
```

Output lands in `images/out/`:

- `prepperpi-<version>-<date>-prepperpi.img.xz` — flash with Raspberry Pi Imager or `dd`
- `prepperpi-<version>-<date>-prepperpi.img.xz.sha256` — integrity check

Typical build time:

| Host | Expected time |
|---|---|
| Apple Silicon Mac (M-series), GitHub `ubuntu-24.04-arm`, a Pi | **~15-25 min** (native ARM64, no emulation) |
| x86_64 Linux / macOS | ~45-90 min (qemu-user-static) |

First run clones pi-gen to `images/.work/pi-gen/` (a few hundred MB); subsequent runs reuse it. Delete `images/.work/` for a fresh pi-gen checkout.

## What the image contains

Built on top of **Raspberry Pi OS Lite (64-bit, Bookworm)** stage2 from pi-gen, with a custom `stage-prepperpi` stage that:

1. Copies the full PrepperPi repo into `/opt/prepperpi-src/`.
2. Runs `installer/install.sh --image-build` inside pi-gen's chroot, which apt-installs all packages (caddy, hostapd, dnsmasq, iw, rfkill, openssl), provisions the `prepperpi` system user + `/srv/prepperpi/` tree, mints the self-signed TLS cert, and enables every PrepperPi systemd unit.
3. Writes `/etc/prepperpi/image.version` with the git commit and build timestamp so a flashed Pi can report which image it came from.

First boot takes ~60–90 s to fully come up:

- Kernel + systemd init (~30 s)
- `prepperpi-ap-configure.service` oneshot renders hostapd/dnsmasq config from the template and brings up `wlan0` (~5 s)
- `hostapd.service` starts beaconing the AP (~5 s)
- `prepperpi-web.service` starts Caddy for the captive portal (~5 s)

By the time a phone scans Wi-Fi, `PrepperPi-<mac4>` is visible.

## Pre-flash configuration

The boot partition of a freshly-flashed card is FAT32, readable from Windows / macOS / Linux. Mount it and drop these files before first boot:

| File | Purpose |
|---|---|
| `prepperpi.conf` | Override Wi-Fi SSID, password, channel, country code. See `services/prepperpi-ap/prepperpi.conf.example` for the full list. Seeded with defaults at image-build time. |
| `ssh` *(empty file)* | Create this file to enable SSH on first boot. |
| `userconf.txt` | Pi OS's encrypted-user-auth format: `username:hashed-password` produced by `openssl passwd -6`. Overrides the baked-in `prepper` account. |
| `custom.toml` | Full Pi Imager advanced-options format. Raspberry Pi Imager writes this for you when you use the "Advanced options" gear icon at flash time. |

### Default login

To keep the headless first-boot path working (AC-3: AP up within 5 min), the image ships with a baked-in login account:

- **Username:** `prepper`
- **Password:** `prepperpi`

**Change this before putting the device on any shared network.** The easiest way is to set your own credentials in Raspberry Pi Imager's Advanced options (ctrl+shift+x in Imager) before flashing — that produces a `custom.toml` on the boot partition that overrides our defaults.

## How it works

```
 images/
   ├── build.sh              # you run this
   ├── config                # pi-gen env
   └── stage-prepperpi/      # our pi-gen stage
       ├── EXPORT_IMAGE
       ├── prerun.sh
       └── 00-install-prepperpi/
           ├── 00-run.sh         # host-side: copy repo into rootfs
           └── 01-run-chroot.sh  # chroot: run installer/install.sh

         ↓ build.sh orchestrates ↓

 images/.work/pi-gen/        # upstream pi-gen clone
   ├── stage0..2/            # standard "lite" base
   ├── stage-prepperpi/      # rsync'd from us
   ├── prepperpi-src/        # the PrepperPi repo, rsync'd
   └── deploy/*.img.xz       # → copied to images/out/
```

`build.sh`:

1. Verifies Docker is reachable (required — pi-gen runs inside a Debian container).
2. Clones pi-gen (ref = `master` by default; override with `PI_GEN_REF=...`).
3. Copies `images/stage-prepperpi/` into the pi-gen checkout.
4. Copies the PrepperPi repo into `pi-gen/prepperpi-src/` so the stage scripts reach it at `/pi-gen/prepperpi-src/` inside the container.
5. Generates pi-gen's `config` from `images/config` plus a few per-build env vars (`PREPPERPI_VERSION`, `PREPPERPI_COMMIT`, `PREPPERPI_REPO`).
6. Creates `SKIP` and `SKIP_IMAGES` flag files in `stage3..5` so pi-gen doesn't build the desktop environments we don't need.
7. Runs `./build-docker.sh`. On Apple Silicon or other ARM64 hosts, Docker pulls the arm64 Debian base image and the build runs natively. On x86_64, Docker pulls via qemu-user-static (slow).
8. Copies `*.img.xz` out of `pi-gen/deploy/` into `images/out/`, generates a SHA-256, and reports the paths.

## Environment overrides

| Env var | Default | Purpose |
|---|---|---|
| `PI_GEN_REF` | `arm64` | git ref of RPi-Distro/pi-gen to check out. The `arm64` branch produces 64-bit Pi OS Lite images; `master` is 32-bit armhf, which we don't ship. |
| `PREPPERPI_WORK` | `images/.work` | local build scratch dir |
| `PREPPERPI_OUT` | `images/out` | artifact output dir |

## Flashing

Raspberry Pi Imager → "Choose OS" → "Use custom" → select the `.img.xz`. Or from the command line:

```bash
# macOS / Linux
xz -dc images/out/*.img.xz | sudo dd of=/dev/sdX bs=4M status=progress
```

Verify before flashing:

```bash
cd images/out
sha256sum -c *.sha256
```

## CI builds

`.github/workflows/build-image.yml` does the same thing on GitHub's `ubuntu-24.04-arm` runners (manual trigger via the Actions tab). Produces identical artifacts. Tag-triggered release + GPG signing arrives with E7-S2.
