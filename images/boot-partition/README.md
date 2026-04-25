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
| `meta-data` | Sets `dsmode: local` and `instance_id: rpios-image`. Cloud-init needs both to treat this as a local NoCloud datasource. | **No** — leave it alone. |

If you replace nothing, the Pi boots with:

- Hostname `prepperpi`
- User `prepper` / password `prepperpi` (SSH disabled)
- DHCP on eth0 if cabled; the PrepperPi AP beaconing on wlan0 either way

## Files in this directory

| File | Purpose | When to use it |
|---|---|---|
| [`user-data.example`](user-data.example) | Install your SSH pubkey, lock the default password, enable SSH | You want to SSH in with your own key instead of the shipped `prepper` / `prepperpi` |
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

On first boot, cloud-init reads both files, applies the changes, and signals `cloud-init` complete. Subsequent boots are no-ops — cloud-init keeps a marker in `/var/lib/cloud/` so the same files aren't re-applied. To re-run, `sudo cloud-init clean --logs` and reboot.

## Debugging

If something looks wrong after first boot:

```bash
sudo journalctl -u cloud-init         # what cloud-init did
sudo cat /var/log/cloud-init.log      # verbose detail
sudo cat /var/log/cloud-init-output.log  # stdout of the runcmd block
```

The four most common issues:

- **Pasted key has a newline in the middle.** `ssh_authorized_keys` entries must be one continuous line. Check with `wc -l` on the file.
- **YAML indentation off by one space.** cloud-init will skip the whole block silently. Run `cloud-init schema --system` to validate.
- **Wrong partition.** Pi Imager mounts the FAT32 *boot* partition (labeled `bootfs`); the rootfs partition is `rootfs` and won't accept cloud-init files.
- **The file is still named `user-data.example`.** cloud-init only reads the exact names `user-data`, `network-config`, `meta-data`. Dragging via Finder preserves the extension; use `cp source dest-without-extension` or rename in the Finder rename field.

### macOS `._user-data` metadata files

If you copy via Finder (or `cp -X` is disabled), macOS will sometimes write an AppleDouble sidecar file named `._user-data` next to `user-data`. It's harmless — cloud-init only reads files by exact name so `._user-data` is ignored — but you can delete it with `dot_clean /Volumes/bootfs` or `rm /Volumes/bootfs/._*` before ejecting if you like tidy filesystems.

## Why not Pi Imager's customization dialog?

Pi Imager 2.x greys out the *Use OS customization* button for locally-loaded image files — it can't know the image's `init_format` without a manifest, and the `--repo` path we tried for a sidecar manifest isn't reliable across Imager builds. Dropping these files by hand is the equivalent mechanism one layer down: Imager's dialog just *writes* `user-data` and `network-config` for you.
