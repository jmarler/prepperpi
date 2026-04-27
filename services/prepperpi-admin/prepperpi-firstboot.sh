#!/usr/bin/env bash
# prepperpi-firstboot.sh — oneshot first-boot tasks for a freshly-flashed
# PrepperPi clone. Run by prepperpi-firstboot.service with
# ConditionFirstBoot=yes (i.e., when /etc/machine-id was empty at boot).
#
# Idempotent — every step is a no-op when there's nothing to do, so this
# is safe to run on a system that's already in its target state.
#
# Tasks:
#   1. growpart: expand the rootfs partition to fill the SD card.
#   2. resize2fs: expand the rootfs filesystem to fill the partition.
#   3. ssh-keygen -A: generate any missing SSH host keys.
#
# The system was almost certainly imaged via backup-image.sh, which
# zeroes /etc/machine-id and strips /etc/ssh/ssh_host_*. That's what
# triggers this service.

set -euo pipefail

log() { printf '[prepperpi-firstboot] %s\n' "$*"; }

# ---------- find the rootfs disk + partition ----------

ROOT_DEV=$(findmnt -nT / -o SOURCE)
[ -n "$ROOT_DEV" ] || { log "could not determine rootfs device"; exit 1; }

# Strip the partition number to get the parent disk. Handles both
# /dev/mmcblk0p2 -> /dev/mmcblk0 and /dev/sda2 -> /dev/sda. lsblk's
# PKNAME column gives us this without parsing.
PARENT_NAME=$(lsblk -no PKNAME "$ROOT_DEV" | head -1)
PARENT_DEV="/dev/$PARENT_NAME"
PART_NUM=$(echo "$ROOT_DEV" | grep -oE '[0-9]+$')

log "rootfs:    $ROOT_DEV"
log "parent:    $PARENT_DEV"
log "part num:  $PART_NUM"

# ---------- 1. grow partition ----------

# growpart is idempotent — exits 1 with "NOCHANGE" on stderr if there's
# nothing to grow. Treat that as success.
log "growpart $PARENT_DEV $PART_NUM"
if growpart_out=$(growpart "$PARENT_DEV" "$PART_NUM" 2>&1); then
    log "$growpart_out"
elif echo "$growpart_out" | grep -qi "NOCHANGE"; then
    log "(partition already at full size)"
else
    log "growpart failed: $growpart_out"
    # Don't fail the whole service — resize2fs and ssh-keygen still
    # matter even if the partition couldn't grow.
fi

# Force the kernel to re-read the partition table so resize2fs sees
# the new size. partprobe is best-effort; on failure, resize2fs may
# still work because we're operating on a live mount.
partprobe "$PARENT_DEV" 2>/dev/null || true
sleep 0.5

# ---------- 2. grow filesystem ----------

# resize2fs grows an online ext4 filesystem to the partition's full
# size. No-op when fs already fills partition.
log "resize2fs $ROOT_DEV"
if r2fs_out=$(resize2fs "$ROOT_DEV" 2>&1); then
    log "$r2fs_out"
else
    log "resize2fs failed: $r2fs_out"
    # Continue anyway.
fi

# ---------- 3. regenerate SSH host keys ----------

# ssh-keygen -A only generates keys that don't already exist. So this
# is a no-op on a system whose host keys are already in place.
log "ssh-keygen -A (regenerate any missing host keys)"
ssh-keygen -A

# ---------- 4. scrub stale USB mountpoint dirs from the rsync ----------

# The backup-image rsync's --exclude=/srv/prepperpi/user-usb/* should
# already keep these out of new images, but reap any leftovers on the
# off chance the exclude was missed (older image, hand-rolled rsync,
# etc.). Idempotent: rmdir only succeeds for empty non-mountpoint dirs.
USB_BASE="/srv/prepperpi/user-usb"
if [ -d "$USB_BASE" ]; then
    log "scrubbing dangling USB mountpoint dirs under $USB_BASE"
    for d in "$USB_BASE"/*/; do
        [ -d "$d" ] || continue
        d="${d%/}"
        if mountpoint -q "$d" 2>/dev/null; then
            continue
        fi
        if rmdir "$d" 2>/dev/null; then
            log "  scrubbed: $d"
        fi
    done
fi

log "done"
