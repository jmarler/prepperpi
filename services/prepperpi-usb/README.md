# prepperpi-usb

Auto-mounts USB drives plugged into the Pi at `/srv/prepperpi/user-usb/<label>/`, exposes them on the landing page as a tile each, and serves them through the Caddy front door at `/usb/<label>/` with directory listing, inline preview of common formats, and Markdown rendering.

Read-only by default. The per-mount write toggle defers to the admin console (E4-S2).

## Moving parts

| Unit / file                              | Type     | Role                                                              |
| ---------------------------------------- | -------- | ----------------------------------------------------------------- |
| `99-prepperpi-usb.rules`                 | udev     | Match USB block devices that carry a filesystem; pull in the mount instance via `SYSTEMD_WANTS`. |
| `prepperpi-usb-mount@<kernel>.service`   | template | Per-partition mount instance. `BindsTo` the device unit so unplug auto-stops it. |
| `prepperpi-usb-mount.sh`                 | script   | Reads `blkid` for FS type + label, sanitizes the label into a slug, mounts read-only at `/srv/prepperpi/user-usb/<slug>/`. |
| `prepperpi-usb-unmount.sh`               | script   | Lazy-unmount + `rmdir` of the mountpoint on stop. |
| `prepperpi-usb-reindex.path`             | path     | Watch `/srv/prepperpi/user-usb` for subdir add/remove. |
| `prepperpi-usb-reindex.service`          | oneshot  | Rebuild `_usb.html` (one tile per live mountpoint). |
| `build-usb-index.sh`                     | script   | The fragment generator. |
| `prepperpi-usb-markdown.service`         | simple   | Tiny Python HTTP daemon on `127.0.0.1:8089` that renders `*.md` to styled HTML. |
| `markdown_server.py`                     | daemon   | The renderer. |

Caddy (in `prepperpi-web`) reverse-proxies `/usb/*.md` to the markdown daemon and serves everything else under `/usb/*` via `file_server browse` rooted at `/srv/prepperpi/user-usb`.

## Supported filesystems

| FS | Mount opts |
|---|---|
| `vfat` (FAT32) | `ro,nodev,nosuid,noexec,uid=prepperpi,gid=prepperpi,umask=0022` |
| `exfat` | `ro,nodev,nosuid,noexec,uid=prepperpi,gid=prepperpi,umask=0022` |
| `ntfs` | via `ntfs-3g`, same opts |
| `ext2/3/4` | `ro,nodev,nosuid,noexec` (on-disk ownership preserved) |

`uid`/`gid`/`umask` only apply to filesystems that don't store unix ownership. For ext\*, files keep their on-disk owner — Caddy will get a 403 reading any file that isn't world-readable. (Most ext\* USB drives in the wild were `mkfs`'d with default umask and look 0644 anyway.)

## Markdown rendering

`/usb/<path>.md` requests are proxied to `markdown_server.py`, which:

1. Maps the URL back to a file under `/srv/prepperpi/user-usb/`, refusing path traversal.
2. Reads the file as UTF-8 (replace-mode for invalid bytes).
3. Renders via `python3-markdown` with `extra`, `sane_lists`, `toc` extensions enabled.
4. Wraps in an HTML shell that inherits the landing-page stylesheet so the look is consistent.

A breadcrumb trail at the top links back up the directory tree.

## Debugging

```
# What's mounted right now
findmnt -nr -t vfat,exfat,ntfs,ntfs3,fuseblk,ext2,ext3,ext4 | grep /srv/prepperpi/user-usb

# Per-instance mount logs
journalctl -u 'prepperpi-usb-mount@*' -n 50

# Path watcher / reindex
journalctl -u prepperpi-usb-reindex.service -n 50

# Markdown daemon
journalctl -u prepperpi-usb-markdown.service -f

# Force a manual reindex
sudo systemctl start prepperpi-usb-reindex.service
```
