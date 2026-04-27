# Backup and recovery

PrepperPi can produce a **disaster-recovery image** of itself onto a USB
drive: a flashable `.img` you write to a fresh microSD card with
[Etcher](https://etcher.balena.io/) or
[Raspberry Pi Imager](https://www.raspberrypi.com/software/), boot, and
end up with an equivalent device. When your content lives on a separate
SSD/M.2, the system image stays small and content is saved as a
companion `.tar` next to the image.

## What you need

- A USB drive with enough free space for the backup. Sizes:
  - System image: roughly the used portion of your SD-card rootfs +
    ~10% overhead (typically a few GB).
  - Content archive (only when `/srv/prepperpi/` is on a separate
    volume): roughly the size of `/srv/prepperpi/`. This can be tens
    or hundreds of GB.
- The USB needs to allow large files. **FAT32 won't work** for the
  system image (>4 GB). **exFAT or ext4 are fine.**
- A blank or wipeable microSD card to flash later, ≥ the size of the
  backup image's rootfs partition.

## Creating a backup

1. Plug your destination USB into PrepperPi. The Storage page lists it
   under "User USB drives." If it's read-only, flip its **write
   toggle** in the admin console so PrepperPi can write to it.
2. Go to **Admin → Backup → Create disaster-recovery backup**.
3. PrepperPi detects whether `/srv/prepperpi/` is on the SD-card rootfs
   or on a separate drive (M.2 / SSD / external USB) and adapts:

| Source layout | "Include content" | What gets produced |
|---|---|---|
| `/srv` on rootfs | OFF | `prepperpi-system-<host>-<ts>.img` only — small, flashable, no content. |
| `/srv` on rootfs | ON | `prepperpi-system-<host>-<ts>.img` with content baked in — flashable, sized to fit a card ≥ used SD. |
| `/srv` on a separate volume | OFF | `prepperpi-system-<host>-<ts>.img` only. |
| `/srv` on a separate volume | ON | Two files on the destination USB: `prepperpi-system-<host>-<ts>.img` (small, flashable) + `prepperpi-content-<host>-<ts>.tar` (large, contains `/srv/prepperpi/`). |

4. Two opt-in toggles:
   - **Include content** — defaulted ON. When `/srv` lives on a
     separate volume, this just produces the companion `.tar` (so the
     `.img` stays small enough to flash to any reasonable microSD).
   - **Include user-configured network secrets** — defaulted OFF and
     flagged "unsafe" (anyone who flashes the resulting image rejoins
     your uplink). Covers `/etc/NetworkManager/system-connections/*`
     and `/etc/wpa_supplicant/*`.
5. PrepperPi shows the planned output filenames + sizes before you
   click go.
6. Click **Create**. Progress is reported live. You can navigate away
   and come back; the backup runs in the background.

Each output file gets a sidecar `<filename>.json` recording the
snapshot's metadata (PrepperPi version, source layout, included
toggles, sha256, etc.).

## What's always stripped (regenerated on first boot)

The backup script always strips ephemeral / per-device state from the
image so a flashed clone gets fresh values rather than colliding with
the source device:

- SSH host keys (`/etc/ssh/ssh_host_*`)
- `/etc/machine-id`
- DHCP leases, journald logs, `/var/log/*.gz` rotated logs
- aria2 in-flight session state
- The update-notifier's snapshot file

Your AP credentials in `/boot/firmware/prepperpi.conf` are kept (they
were already plaintext on the SD's FAT32 boot partition that any host
can mount).

## First-boot tasks on a flashed clone

When the clone boots for the first time, `prepperpi-firstboot.service`
fires (gated by `ConditionFirstBoot=yes`, which is true because we
zeroed `/etc/machine-id` in the image). It runs once and:

- **Grows the rootfs partition** to fill the destination microSD via
  `growpart`. The image's rootfs partition is intentionally sized to
  the source's used bytes + headroom, so the spare space on a larger
  destination card is left unallocated until this step.
- **Grows the ext4 filesystem** to fill the expanded partition via
  `resize2fs` (online — no reboot needed).
- **Generates SSH host keys** via `ssh-keygen -A`. Pi OS Lite doesn't
  ship its own first-boot key-regen mechanism (Raspberry Pi Imager's
  custom-config UI plants one, but a raw `.img` flashed bare gets
  nothing), so we provide our own.

The service is idempotent — every step is a no-op when there's nothing
to do, so it's safe even if it fires more than once.

If something goes wrong and you find yourself with a clone where the
rootfs didn't expand or sshd is failing on missing host keys, you can
re-run the worker by hand:

```bash
sudo /opt/prepperpi/services/prepperpi-admin/prepperpi-firstboot.sh
```

## Restoring

### Case 1 — System image only (or content baked into the image)

1. Flash `prepperpi-system-*.img` to a new microSD with Etcher /
   Raspberry Pi Imager.
2. Pop the SD into a Pi and power on.
3. The AP comes up, the captive portal lands, the admin console works.
   First boot regenerates SSH host keys + `machine-id` so the new
   device is genuinely distinct from the source.

### Case 2 — System image + content tarball

1. Flash `prepperpi-system-*.img` to a new microSD.
2. Boot the new Pi. Plug in your destination drive (the one that will
   hold `/srv/prepperpi/`) — the same M.2 / SSD shape as on the source.
3. Plug in the backup USB containing `prepperpi-content-*.tar` (this
   can be the same USB that held the .img while flashing, or a
   different one).
4. Go to **Admin → Backup → Restore content from USB**. PrepperPi
   detects the `prepperpi-content-*.tar` and offers to extract it into
   `/srv/prepperpi/` (verifying its sha256 against the sidecar first).
5. After extraction, the restored Pi has the same content the source
   did.

**Power-user shortcut (skipping the in-Pi restore step):** if you have
a host machine with a way to write to the destination drive (e.g.,
M.2-in-NVMe-enclosure on USB), you can `tar -xpf
prepperpi-content-*.tar -C /srv` directly onto a freshly-formatted ext4
volume from your laptop. Plug it into the Pi after flashing the
system image, and the appliance will mount it as `/srv/prepperpi`
without the in-UI restore step.

## What's *not* in the backup

- Anything outside `/` that isn't `/boot/firmware` or (when included)
  `/srv/prepperpi/` — namely, attached USB drives mounted under
  `/srv/prepperpi/user-usb/`. Those are user data on removable media;
  the backup deliberately doesn't try to capture them.
- WiFi uplink credentials by default (toggle to include).
- Compressed archives — the script doesn't `.xz` or `.gz` either
  output. Compression is too slow on a Pi 4B and ZIM files don't
  benefit from re-compression. If you want to compress for cold
  storage, do it on a host machine after the fact.

## Frequently asked

**Why isn't the content tar formatted as ext4 in a loopback file like
the system image is?** Because then you'd need a way to flash that
loopback to your destination M.2 drive, which most people can't do
from a microSD-only environment. A `.tar` is just a single file; you
can copy it to any filesystem (exFAT, NTFS, ext4) and extract it
later.

**What if my microSD is bigger than the backup image?** Etcher /
Raspberry Pi Imager only write the image's bytes; the rest of the SD
is unallocated until first boot, when Pi OS's `init_resize.service` (or
equivalent) auto-expands the rootfs partition to fill the card. No
manual resize needed.

**My USB drive is read-only after auto-mount — how do I make it
writable?** That's PrepperPi's per-USB safety toggle. From **Admin →
Storage**, find your USB and click the write-toggle. The choice is
session-only (re-plug resets to read-only).

**Can I encrypt the backup?** The "Include user-configured network
secrets" toggle is off by default for exactly this reason — the
typical secrets-in-an-image risk is your WiFi PSK leaking. Full-disk
encryption of the produced image isn't supported here; if you need
it, encrypt the resulting `.img` and `.tar` files yourself with `gpg`
or `age` after the backup completes. (The image won't be directly
flashable in encrypted form; you'd decrypt before flashing.)
