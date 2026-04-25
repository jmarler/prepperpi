#!/usr/bin/env bash
# prepperpi-usb-unmount.sh — tear down the mount put in place by
# prepperpi-usb-mount.sh. Tolerant of yanked-out devices: the block
# device may already be gone by the time systemd runs ExecStop=,
# so we use lazy unmount and don't fail when nothing is left to do.

set -euo pipefail

readonly DEV_NAME="${1:?usage: $0 <kernel-name>}"
readonly DEV_PATH="/dev/${DEV_NAME}"
readonly MOUNT_BASE="${MOUNT_BASE:-/srv/prepperpi/user-usb}"

log() { printf '[prepperpi-usb-unmount] %s\n' "$*"; }

# Find any mountpoints that point at this device. findmnt's --source
# is exact-match against the kernel mount table.
mapfile -t mps < <(findmnt -nr -o TARGET --source "$DEV_PATH" 2>/dev/null || true)

# If the device disappeared before we got here, fall back to scanning
# everything mounted under MOUNT_BASE. Stale mounts left behind by an
# unclean shutdown will get reaped this way too.
if (( ${#mps[@]} == 0 )); then
  mapfile -t mps < <(findmnt -nr -o TARGET --types vfat,exfat,ntfs,ntfs3,fuseblk,ext2,ext3,ext4 \
                     | awk -v base="$MOUNT_BASE/" '$1 ~ "^"base {print $1}' || true)
fi

if (( ${#mps[@]} == 0 )); then
  log "no mountpoints found for $DEV_PATH; nothing to do"
  exit 0
fi

for mp in "${mps[@]}"; do
  [[ -z "$mp" ]] && continue
  log "unmounting $mp"
  # Lazy unmount: detach now, let in-flight reads drain. Safer when
  # the device has been physically removed.
  umount -l "$mp" 2>/dev/null || log "umount -l $mp failed (already gone?)"
  if [[ "$mp" == "$MOUNT_BASE"/* && -d "$mp" ]]; then
    rmdir "$mp" 2>/dev/null || log "rmdir $mp skipped (not empty?)"
  fi
done
