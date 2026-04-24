# images/

Build the PrepperPi SD card image with [pi-gen](https://github.com/RPi-Distro/pi-gen) in Docker. Produces a `.zip`-packaged raw `.img` that flashes with Raspberry Pi Imager and boots straight into a configured PrepperPi appliance.

## Quick start (local build on ARM64 host)

```bash
images/build.sh
```

Output lands in `images/out/`:

- `image_<date>-prepperpi-prepperpi.zip` — flash with Raspberry Pi Imager or `dd`
- `image_<date>-prepperpi-prepperpi.zip.sha256` — integrity check
- `image_<date>-prepperpi-prepperpi.rpi-imager.json` — manifest sidecar (see [Flashing](#flashing))

Typical build time:

| Host | Expected time |
|---|---|
| Apple Silicon Mac (M-series), GitHub `ubuntu-24.04-arm`, a Pi | **~15-25 min** (native ARM64, no emulation) |
| x86_64 Linux / macOS | ~45-90 min (qemu-user-static) |

First run clones pi-gen to `images/.work/pi-gen/` (a few hundred MB); subsequent runs reuse it. Delete `images/.work/` for a fresh pi-gen checkout.

## What the image contains

Built on top of **Raspberry Pi OS Lite (64-bit, Trixie)** stage2 from pi-gen, with a custom `stage-prepperpi` stage that:

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

The boot partition of a freshly-flashed card is FAT32, readable from Windows / macOS / Linux. Drop any of these files on it before first boot:

| File | Purpose |
|---|---|
| `user-data` | cloud-init user config (SSH pubkey, hostname, password policy). Starting template: [`boot-partition/user-data.example`](boot-partition/user-data.example). |
| `network-config` | cloud-init netplan v2 (static IP, upstream Wi-Fi). Starting template: [`boot-partition/network-config.example`](boot-partition/network-config.example). |
| `prepperpi.conf` | Override Wi-Fi AP SSID, password, channel, country code. See [`services/prepperpi-ap/prepperpi.conf.example`](../services/prepperpi-ap/prepperpi.conf.example). Seeded at image-build time. |

Details and the full workflow: [`boot-partition/README.md`](boot-partition/README.md).

### Default login

To keep the headless first-boot path working (AC-3: AP up within 5 min), the image ships with a baked-in login account:

- **Username:** `prepper`
- **Password:** `prepperpi`

**Change this before putting the device on any shared network.** Drop a `user-data` on the boot partition with your own SSH pubkey + `lock_passwd: true` — the starter template already wires this up.

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

### 1. Write the image

```bash
# Verify integrity first
cd images/out && sha256sum -c *.sha256
```

Then either:

- **Raspberry Pi Imager** → *Choose OS* → *Use custom* → select the `.zip`. Imager will grey out the *Use OS customization* button (it does that for every locally-loaded image in 2.x — see [boot-partition/README.md](boot-partition/README.md#why-not-pi-imagers-customization-dialog)). Customization happens in step 2 instead.
- **`dd`**:
  ```bash
  unzip -p image_*-prepperpi-prepperpi.zip | sudo dd of=/dev/sdX bs=4M status=progress
  ```

### 2. Customize via the boot partition (optional)

After flashing, the FAT32 boot partition is auto-mounted (`/Volumes/bootfs` on macOS, `/media/<user>/bootfs` on Linux, a drive letter on Windows). Drop a `user-data` and/or `network-config` file onto it before first boot and cloud-init picks them up via its NoCloud datasource.

Starting templates live in [`images/boot-partition/`](boot-partition/):

```bash
# Typical: install your SSH pubkey, enable SSH, lock the default password
cp images/boot-partition/user-data.example /Volumes/bootfs/user-data
# edit /Volumes/bootfs/user-data, paste your ssh-ed25519 line, save
diskutil eject /Volumes/bootfs
```

Without any files dropped on the boot partition, the image boots with `prepper` / `prepperpi` login, SSH off, DHCP on eth0 — usable for console, not ideal for network-attached use. Read the partition README for the full list of knobs.

### Experimental: `rpi-imager --repo` with the generated manifest

`build.sh` also emits a `*.rpi-imager.json` sidecar next to the `.zip` (manifest declaring `init_format: cloudinit-rpi`). In theory `rpi-imager --repo file://...json` re-enables the customization dialog by treating the image as a first-class OS list entry. In practice `--repo` with a `file://` URL is flaky on the macOS Imager build (2.x rejects the path). The manifest is still useful when hosted over HTTP or served out of a GitHub Release; deferred to E7-S2. For now, use the boot-partition path above.

## pi-gen patches

We drop one small script into pi-gen's own stages at build time. It lives in this repo under `images/pi-gen-patches/` and is copied into place by `build.sh`:

| Patch | Where it lands | What it does |
|---|---|---|
| `02-no-listchanges.sh` | `pi-gen/stage0/00-configure-apt/` | Pin + purge `apt-listchanges`. Prevents every subsequent package install from blocking ~30 s on a Docker Desktop DNS timeout for `metadata.ftp-master.debian.org`. |

If pi-gen's `arm64` branch upstream restructures stage0, the destination path in `build.sh` may need a touch-up.

## CI builds

`.github/workflows/build-image.yml` does the same thing on GitHub's `ubuntu-24.04-arm` runners (manual trigger via the Actions tab). Produces identical artifacts. Tag-triggered release + GPG signing arrives with E7-S2.
