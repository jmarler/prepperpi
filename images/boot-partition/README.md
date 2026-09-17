# Boot-partition examples

Drop these files onto the FAT32 boot partition of a freshly-flashed
SD card to customize the Pi on first boot. The image ships with
cloud-init (NoCloud datasource) preconfigured to read them.

## What the image already ships

Pi-gen's `stage2` puts three files on the FAT32 boot partition before our stage even runs. It is **safe to do nothing** — the stock files are intentionally no-ops:

| File on boot partition | Stock content | Safe to replace? |
|---|---|---|
| `user-data` | Cloud-init template — **every line is commented out**. Cloud-init reads it and does nothing. | **Yes** — our [`user-data.example`](user-data.example) is a drop-in replacement. |
| `network-config` | Netplan template — **every line is commented out**. Network falls back to DHCP-on-eth0 via systemd-networkd. | **Yes** — our [`network-config.example`](network-config.example) is a drop-in replacement. |
| `meta-data` | Sets `dsmode: local` and `instance_id: rpios-image`. Cloud-init needs both to treat this as a local NoCloud datasource. | Don't rewrite it — but **do** change `instance_id` if you need cloud-init to re-run. See [Editing after first boot](#editing-after-first-boot). |

If you replace nothing, the Pi boots with:

- Hostname `prepperpi`
- User `prepper` / password `prepperpi` (SSH disabled)
- DHCP on eth0 if cabled; the PrepperPi AP beaconing on wlan0 either way

## Files in this directory

| File | Purpose | When to use it |
|---|---|---|
| [`user-data.example`](user-data.example) | Install your SSH pubkey and enable SSH (password login stays on as a fallback until you've proven the key) | You want to SSH in with your own key instead of the shipped `prepper` / `prepperpi` |
| [`network-config.example`](network-config.example) | Static IP, or client-mode Wi-Fi on a second radio | You need something beyond DHCP-on-eth0 |

## How to use them

1. **Flash** the image (`rpi-imager --choose-os` → *Use custom* → the `.zip`, or `dd`).
2. **Mount** the boot partition. Most OSes auto-mount it after flash:
   - **macOS:** `/Volumes/bootfs`
   - **Linux:** `/media/<user>/bootfs` or similar
   - **Windows:** shows up as a drive letter (often `E:` or later)
3. **Copy** the example file you want, renaming off the `.example` suffix:
   ```bash
   # macOS example — substitute your own paths
   cp images/boot-partition/user-data.example /Volumes/bootfs/user-data
   cp images/boot-partition/network-config.example /Volumes/bootfs/network-config
   ```
4. **Edit** the copied file and replace the placeholders — in particular, paste your real SSH pubkey into `user-data`.
5. **Eject** the SD card (`diskutil eject /Volumes/bootfs` on macOS; `udisksctl unmount` on Linux; right-click → Eject on Windows) and boot the Pi.

On first boot, cloud-init reads both files, applies the changes, and signals `cloud-init` complete. Subsequent boots are no-ops — cloud-init keeps a marker in `/var/lib/cloud/` so the same files aren't re-applied. **If you're editing a card that has already booted once, read [Editing after first boot](#editing-after-first-boot) first** — otherwise your changes are silently ignored.

## Editing after first boot

**cloud-init reads `user-data` once per instance, not once per boot.** It caches the `instance_id` from `meta-data` under `/var/lib/cloud/`, and on every later boot it compares the two. Same id means "I have already configured this instance" — and `user-data` is skipped entirely, however much you changed it.

So the common sequence of *flash → boot → discover SSH is off → power down → add `user-data` → boot again* *does nothing*. The file is read, found to belong to an instance already handled, and ignored. There's no error; SSH just stays off.

To make an edit apply to a card that has already booted, change `instance_id` to any new value at the same time:

```yaml
# /Volumes/bootfs/meta-data
instance_id: prepperpi-2       # was: rpios-image
```

Both files live on the FAT32 partition, so this is doable from any Mac, Windows, or Linux machine with the card in a reader — no shell on the Pi required. That matters, because the other way to reset cloud-init (`sudo cloud-init clean --logs && sudo reboot`) needs the login you're trying to get back.

### If you're locked out

Put the card in another machine and, on the boot partition:

1. **Create an empty file named `ssh`.** This is the legacy Raspberry Pi marker, handled by `raspberrypi-sys-mods` independently of cloud-init. It enables `sshd` on the next boot no matter what cloud-init decides to do.
2. **Check your key is actually valid** before trusting it. A single mistyped character makes it silently useless:
   ```bash
   grep -o 'ssh-ed25519 [A-Za-z0-9+/=]*' /Volumes/bootfs/user-data > /tmp/k.pub
   ssh-keygen -l -f /tmp/k.pub     # prints the fingerprint, or errors
   ```
   A valid ed25519 key always begins `AAAAC3NzaC1lZDI1NTE5AAAAI`. If yours doesn't, it's corrupt — copy it again with `cat ~/.ssh/id_ed25519.pub`, never by retyping.
3. **Bump `instance_id`** in `meta-data` as above, so your corrected `user-data` is actually read.
4. Confirm `lock_passwd: false` and `ssh_pwauth: true` in `user-data`, so the console password (`prepper` / `prepperpi`) stays as a fallback.

Then eject and boot. Console login is `prepper` / `prepperpi` unless you changed it.

## Debugging

If something looks wrong after first boot:

```bash
sudo journalctl -u cloud-init         # what cloud-init did
sudo cat /var/log/cloud-init.log      # verbose detail
sudo cat /var/log/cloud-init-output.log  # stdout of the runcmd block
```

The most common issues:

- **The edit is being ignored entirely.** If the card has booted before, `instance_id` in `meta-data` must change or cloud-init skips `user-data`. See [Editing after first boot](#editing-after-first-boot). This is the most common cause of "I enabled SSH and it didn't work."
- **Pasted key has a newline in the middle.** `ssh_authorized_keys` entries must be one continuous line. Check with `wc -l` on the file.
- **Key has a typo.** Validate it with `ssh-keygen -l -f` before booting — a bad key fails silently, and if you also set `lock_passwd: true` you have no way back in.
- **YAML indentation off by one space.** cloud-init will skip the whole block silently. Run `cloud-init schema --system` to validate.
- **Wrong partition.** Pi Imager mounts the FAT32 *boot* partition (labeled `bootfs`); the rootfs partition is `rootfs` and won't accept cloud-init files.
- **The file is still named `user-data.example`.** cloud-init only reads the exact names `user-data`, `network-config`, `meta-data`. Dragging via Finder preserves the extension; use `cp source dest-without-extension` or rename in the Finder rename field.

### macOS `._user-data` metadata files

If you copy via Finder (or `cp -X` is disabled), macOS will sometimes write an AppleDouble sidecar file named `._user-data` next to `user-data`. It's harmless — cloud-init only reads files by exact name so `._user-data` is ignored — but you can delete it with `dot_clean /Volumes/bootfs` or `rm /Volumes/bootfs/._*` before ejecting if you like tidy filesystems.

## Why not Pi Imager's customization dialog?

Pi Imager 2.x greys out the *Use OS customization* button for locally-loaded image files — it can't know the image's `init_format` without a manifest, and the `--repo` path we tried for a sidecar manifest isn't reliable across Imager builds. Dropping these files by hand is the equivalent mechanism one layer down: Imager's dialog just *writes* `user-data` and `network-config` for you.
