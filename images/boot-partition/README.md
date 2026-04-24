# Boot-partition examples

Drop these files onto the FAT32 boot partition of a freshly-flashed
SD card to customize the Pi on first boot. The image ships with
cloud-init (NoCloud datasource) preconfigured to read them.

## Files

| File | Purpose | Required? |
|---|---|---|
| [`user-data.example`](user-data.example) | Hostname + SSH key + password policy | Only if you want SSH / your own key / a non-default hostname |
| [`network-config.example`](network-config.example) | Ethernet / Wi-Fi client | Only if you want a static IP or upstream Wi-Fi |

Without either file, the Pi boots with:

- Hostname `prepperpi`
- User `prepper` / password `prepperpi`
- SSH **off**
- Ethernet via DHCP (if plugged in)

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

The three most common issues:

- **Pasted key has a newline in the middle.** `ssh_authorized_keys` entries must be one continuous line. Check with `wc -l` on the file.
- **YAML indentation off by one space.** cloud-init will skip the whole block silently. Run `cloud-init schema --system` to validate.
- **Wrong partition.** Pi Imager mounts the FAT32 *boot* partition (labeled `bootfs`); the rootfs partition is `rootfs` and won't accept cloud-init files.

## Why not Pi Imager's customization dialog?

Pi Imager 2.x greys out the *Use OS customization* button for locally-loaded image files — it can't know the image's `init_format` without a manifest, and the `--repo` path we tried for a sidecar manifest isn't reliable across Imager builds. Dropping these files by hand is the equivalent mechanism one layer down: Imager's dialog just *writes* `user-data` and `network-config` for you.
