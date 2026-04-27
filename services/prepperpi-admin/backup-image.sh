#!/usr/bin/env bash
# backup-image.sh — produce a disaster-recovery system .img (and, when
# /srv/prepperpi/ is on a separate volume, a content .tar) of the running
# PrepperPi appliance. Run as root.
#
#     sudo backup-image.sh --output-dir DIR [options]
#
# Options:
#   --no-content              exclude /srv/prepperpi/ entirely
#   --include-secrets         include /etc/NetworkManager/system-connections/
#                             and /etc/wpa_supplicant/ in the image
#   --rootfs-headroom-mib N   extra space on the new rootfs partition
#                             (default 512)
#   --sha256 yes|no           compute sha256 of each output for the sidecar
#                             (default yes; "no" useful for prototyping)
#   --no-defer-journal        opt out of the default deferred-journal mode
#                             (write the journal during mkfs.ext4 instead of
#                             adding it via tune2fs -j after rsync)
#
# Produces under DIR:
#   prepperpi-system-<host>-<ts>.img       always
#   prepperpi-system-<host>-<ts>.img.json  sidecar
#   prepperpi-content-<host>-<ts>.tar      only when /srv on a separate
#                                          volume AND --no-content not set
#   prepperpi-content-<host>-<ts>.tar.json sidecar (same condition)
#
# Exits non-zero on error. Cleans up loop devices + mounts on any exit.

set -euo pipefail

# ---------- args ----------

OUTPUT_DIR=""
INCLUDE_CONTENT=1
INCLUDE_SECRETS=0
ROOTFS_HEADROOM_MIB=512
WANT_SHA256=1
ESTIMATE_ONLY=0
# Default: defer journal creation. mkfs.ext4 with `-O ^has_journal`
# combined with no-journal rsync, then tune2fs -j after rsync, runs the
# rsync phase noticeably faster on slow exfat-backed loopback (no
# transaction log overhead per metadata op). Validated end-to-end with
# 17/17 structural checks identical to the with-journal output.
DEFER_JOURNAL=1

usage() {
    sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-2}"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --output-dir)          OUTPUT_DIR="$2"; shift 2 ;;
        --no-content)          INCLUDE_CONTENT=0; shift ;;
        --include-secrets)     INCLUDE_SECRETS=1; shift ;;
        --rootfs-headroom-mib) ROOTFS_HEADROOM_MIB="$2"; shift 2 ;;
        --sha256)
            case "$2" in yes|true|1) WANT_SHA256=1 ;; no|false|0) WANT_SHA256=0 ;;
                *) echo "error: --sha256 takes yes/no" >&2; exit 2 ;;
            esac
            shift 2 ;;
        --estimate)            ESTIMATE_ONLY=1; shift ;;
        --defer-journal)       DEFER_JOURNAL=1; shift ;;   # accepted as a no-op now (default)
        --no-defer-journal)    DEFER_JOURNAL=0; shift ;;   # opt out: write journal during mkfs
        -h|--help) usage 0 ;;
        *) echo "unknown arg: $1" >&2; usage ;;
    esac
done

[ -n "$OUTPUT_DIR" ] || { echo "error: --output-dir DIR is required" >&2; usage; }
[ -d "$OUTPUT_DIR" ] || { echo "error: dir does not exist: $OUTPUT_DIR" >&2; exit 2; }
[ "$(id -u)" -eq 0 ] || { echo "error: must run as root" >&2; exit 2; }

# ---------- helpers ----------

log()  { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
fmt()  { numfmt --to=iec --suffix=B "$1"; }

CLEANUP_STEPS=()
push_cleanup() { CLEANUP_STEPS+=("$1"); }
cleanup() {
    set +e
    local i
    for (( i=${#CLEANUP_STEPS[@]}-1; i>=0; i-- )); do
        eval "${CLEANUP_STEPS[i]}" >/dev/null 2>&1
    done
}
trap cleanup EXIT

require_tool() {
    command -v "$1" >/dev/null 2>&1 \
        || { echo "error: missing tool: $1" >&2; exit 2; }
}
for t in losetup sfdisk mkfs.vfat mkfs.ext4 rsync truncate findmnt lsblk \
         blkid awk sed sha256sum tar; do
    require_tool "$t"
done

# ---------- filenames + layout detection ----------

HOSTNAME_SHORT=$(hostname -s)
TS=$(date +%Y%m%d-%H%M%S)
IMG_PATH="$OUTPUT_DIR/prepperpi-system-${HOSTNAME_SHORT}-${TS}.img"
IMG_SIDECAR="$IMG_PATH.json"
TAR_PATH="$OUTPUT_DIR/prepperpi-content-${HOSTNAME_SHORT}-${TS}.tar"
TAR_SIDECAR="$TAR_PATH.json"

# Identify the live rootfs + boot devices.
ROOTFS_DEV=$(findmnt -nT / -o SOURCE)
BOOT_DEV=$(findmnt -nT /boot/firmware -o SOURCE)
[ -n "$ROOTFS_DEV" ] && [ -n "$BOOT_DEV" ] || {
    echo "error: could not determine rootfs/boot devices" >&2; exit 2;
}

# Detect whether /srv/prepperpi is its own filesystem.
SRV_DEV=$(findmnt -nT /srv/prepperpi -o SOURCE 2>/dev/null || true)
if [ -n "$SRV_DEV" ] && [ "$SRV_DEV" != "$ROOTFS_DEV" ]; then
    SRV_LAYOUT="separate"
else
    SRV_LAYOUT="rootfs"
fi

# Decide what we'll produce.
PRODUCE_TAR=0
if [ "$INCLUDE_CONTENT" -eq 1 ] && [ "$SRV_LAYOUT" = "separate" ]; then
    PRODUCE_TAR=1
fi

log "live rootfs: $ROOTFS_DEV"
log "live boot:   $BOOT_DEV"
log "/srv layout: $SRV_LAYOUT"
log "outputs:"
log "  image:    $IMG_PATH"
[ "$PRODUCE_TAR" -eq 1 ] && log "  content:  $TAR_PATH" || log "  content:  (none — content is on rootfs or excluded)"

# ---------- size estimates ----------

# Boot partition size in sectors (mirror exactly). Read via /sys
# rather than `blockdev` so estimate mode works even when called from a
# context where /dev/mmcblk0* are filtered (e.g. inside the
# prepperpi-admin daemon's PrivateDevices=true sandbox).
BOOT_SYSFS_NAME=$(basename "$BOOT_DEV")
BOOT_SECTORS=$(cat "/sys/class/block/${BOOT_SYSFS_NAME}/size")
BOOT_BYTES=$(( BOOT_SECTORS * 512 ))

# Rootfs used: stat -f is instant where du -x is slow on a Pi 4.
# stat -f -c gives block_size * (blocks - free_blocks) = used bytes.
fs_used_bytes() {
    local mountpoint="$1"
    local s f b
    read -r s b f < <(stat -f -c '%S %b %f' "$mountpoint")
    echo $(( s * (b - f) ))
}
ROOTFS_USED_BYTES=$(fs_used_bytes /)
log "rootfs used: $(fmt "$ROOTFS_USED_BYTES")"

# Subtract /srv/prepperpi if it's on rootfs and we're excluding content.
if [ "$SRV_LAYOUT" = "rootfs" ] && [ "$INCLUDE_CONTENT" -eq 0 ]; then
    SRV_BYTES=$(du -sxB1 /srv/prepperpi 2>/dev/null | awk '{ print $1 }')
    SRV_BYTES=${SRV_BYTES:-0}
    log "subtracting /srv/prepperpi: $(fmt "$SRV_BYTES")"
    ROOTFS_USED_BYTES=$(( ROOTFS_USED_BYTES - SRV_BYTES ))
fi

# Rootfs partition size: used + headroom + 5% slack, MiB-aligned.
HEADROOM_BYTES=$(( ROOTFS_HEADROOM_MIB * 1024 * 1024 ))
SLACK_BYTES=$(( ROOTFS_USED_BYTES / 20 ))
ROOTFS_BYTES=$(( ROOTFS_USED_BYTES + HEADROOM_BYTES + SLACK_BYTES ))
ROOTFS_BYTES=$(( (ROOTFS_BYTES + 1048575) / 1048576 * 1048576 ))
ROOTFS_SECTORS=$(( ROOTFS_BYTES / 512 ))

# Image layout: Pi-OS convention starts boot at sector 8192.
BOOT_START=8192
ROOT_START=$(( BOOT_START + BOOT_SECTORS ))
ROOT_START=$(( (ROOT_START + 2047) / 2048 * 2048 ))
TOTAL_SECTORS=$(( ROOT_START + ROOTFS_SECTORS + 2048 ))
TOTAL_BYTES=$(( TOTAL_SECTORS * 512 ))

# Tar size estimate (only if we'll make one).
TAR_BYTES_EST=0
if [ "$PRODUCE_TAR" -eq 1 ]; then
    TAR_BYTES_EST=$(du -sxB1 /srv/prepperpi 2>/dev/null | awk '{ print $1 }')
    TAR_BYTES_EST=${TAR_BYTES_EST:-0}
fi

log "image plan:"
log "  boot partition: $(fmt "$BOOT_BYTES") (mirrored)"
log "  root partition: $(fmt "$ROOTFS_BYTES")"
log "  total .img:     $(fmt "$TOTAL_BYTES")"
[ "$PRODUCE_TAR" -eq 1 ] && log "  content .tar:   ~$(fmt "$TAR_BYTES_EST") (estimated)"

# Pre-flight free space.
NEED_BYTES=$(( TOTAL_BYTES + TAR_BYTES_EST ))
DEST_FREE=$(df --output=avail -B1 "$OUTPUT_DIR" | tail -1)
log "destination free: $(fmt "$DEST_FREE") (need $(fmt "$NEED_BYTES"))"
[ "$DEST_FREE" -ge "$NEED_BYTES" ] || {
    echo "error: insufficient free space at $OUTPUT_DIR" >&2; exit 2;
}

if [ "$ESTIMATE_ONLY" -eq 1 ]; then
    PRODUCE_TAR_JSON=$([ $PRODUCE_TAR -eq 1 ] && echo true || echo false)
    cat <<EOF
{
    "schema_version": 1,
    "kind": "estimate",
    "source_layout": "$SRV_LAYOUT",
    "include_content": $([ $INCLUDE_CONTENT -eq 1 ] && echo true || echo false),
    "include_secrets": $([ $INCLUDE_SECRETS -eq 1 ] && echo true || echo false),
    "image_path": "$(basename "$IMG_PATH")",
    "image_size_bytes": $TOTAL_BYTES,
    "content_tar_present": $PRODUCE_TAR_JSON,
    "content_tar_path": $([ $PRODUCE_TAR -eq 1 ] && printf '"%s"' "$(basename "$TAR_PATH")" || echo null),
    "content_tar_size_bytes_estimate": $TAR_BYTES_EST,
    "total_required_bytes": $NEED_BYTES,
    "destination_free_bytes": $DEST_FREE,
    "destination_dir": "$OUTPUT_DIR"
}
EOF
    exit 0
fi

# ---------- create + partition the image ----------

log "creating sparse image"
rm -f -- "$IMG_PATH" "$IMG_PATH.partial"
truncate -s "$TOTAL_BYTES" "$IMG_PATH.partial"
push_cleanup "rm -f -- '$IMG_PATH.partial'"

DISK_ID=$(printf '0x%08x' $(( (RANDOM << 16) ^ RANDOM ^ $$ )))
log "new disk signature: $DISK_ID"

sfdisk "$IMG_PATH.partial" <<EOF >/dev/null
label: dos
label-id: $DISK_ID
unit: sectors
sector-size: 512
start=$BOOT_START, size=$BOOT_SECTORS, type=c, bootable
start=$ROOT_START, size=$ROOTFS_SECTORS, type=83
EOF

# ---------- losetup + mkfs ----------

LOOP=$(losetup -fP --show "$IMG_PATH.partial")
push_cleanup "losetup -d '$LOOP'"
log "loop device: $LOOP"

for _ in 1 2 3 4 5; do
    [ -b "${LOOP}p2" ] && break
    sleep 0.2
    partprobe "$LOOP" 2>/dev/null || true
done
[ -b "${LOOP}p1" ] && [ -b "${LOOP}p2" ] || {
    echo "error: loop partitions did not appear" >&2; exit 1;
}

log "mkfs boot (FAT32)"
mkfs.vfat -F32 -n bootfs "${LOOP}p1" >/dev/null

# -m 0: no reserved-blocks (this is a backup; root never needs to claw back space).
# lazy_itable_init=1 is the e2fsprogs default since 1.42; setting it
# explicitly is just documentation.
# Default path skips the journal during mkfs and adds it via tune2fs -j
# after rsync; the no-journal rsync runs faster on slow exfat-backed
# loopback because it doesn't write a transaction log per metadata op.
# Pass --no-defer-journal to write the journal during mkfs instead.
MKFS_T0=$(date +%s)
if [ "$DEFER_JOURNAL" -eq 1 ]; then
    log "mkfs root (ext4, no reserved blocks, journal deferred)"
    mkfs.ext4 -F -L rootfs -m 0 -O ^has_journal -E lazy_itable_init=1 "${LOOP}p2" >/dev/null
else
    log "mkfs root (ext4, no reserved blocks, journal during mkfs)"
    mkfs.ext4 -F -L rootfs -m 0 -E lazy_itable_init=1 "${LOOP}p2" >/dev/null
fi
MKFS_T1=$(date +%s)
log "mkfs root done in $(( MKFS_T1 - MKFS_T0 ))s"

NEW_BOOT_PARTUUID=$(blkid -s PARTUUID -o value "${LOOP}p1")
NEW_ROOT_PARTUUID=$(blkid -s PARTUUID -o value "${LOOP}p2")
log "new boot PARTUUID: $NEW_BOOT_PARTUUID"
log "new root PARTUUID: $NEW_ROOT_PARTUUID"

# ---------- mount ----------

MNT=$(mktemp -d -t prepperpi-backup.XXXXXX)
push_cleanup "rmdir '$MNT'"

mount "${LOOP}p2" "$MNT"
push_cleanup "umount '$MNT'"

mkdir -p "$MNT/boot/firmware"
mount "${LOOP}p1" "$MNT/boot/firmware"
push_cleanup "umount '$MNT/boot/firmware'"

# ---------- rsync rootfs ----------

# Excludes that always apply.
EXCLUDES=(
    --exclude=/proc/
    --exclude=/sys/
    --exclude=/dev/
    --exclude=/run/
    --exclude=/tmp/
    --exclude=/var/tmp/
    --exclude=/var/cache/apt/archives/*.deb
    --exclude=/var/cache/man/
    --exclude=/var/log/journal/
    --exclude=/var/log/*.gz
    --exclude=/var/log/*.[0-9]
    --exclude=/var/lib/dhcp/
    --exclude=/var/lib/systemd/random-seed
    --exclude=/var/swap
    --exclude=/swapfile
    --exclude=/lost+found
    # Stripped so first-boot regenerates them.
    --exclude=/etc/ssh/ssh_host_*
    --exclude=/etc/machine-id
    --exclude=/var/lib/dbus/machine-id
    # PrepperPi runtime state that should reset on a fresh device.
    --exclude=/var/lib/prepperpi/updates/state.json
    --exclude=/var/lib/aria2/aria2.session
    # Stale mountpoint dirs left under /srv/prepperpi/user-usb/ from
    # whatever USBs were plugged in at backup time. --one-file-system
    # already skips traversing INTO the USB mounts (so no content gets
    # copied), but the directory entries themselves live on the rootfs
    # and would otherwise propagate to clones as empty dirs that
    # confuse later admin enumeration. Keep the parent /srv/prepperpi/
    # user-usb/ dir itself (mkdir'd back below) so prepperpi-usb-mount
    # has somewhere to put new mounts.
    --exclude=/srv/prepperpi/user-usb/*
)

# When /srv is on rootfs, the toggle controls inclusion.
# When /srv is on a separate volume, --one-file-system already skips it
# (and we'll handle the tar separately below).
if [ "$SRV_LAYOUT" = "rootfs" ] && [ "$INCLUDE_CONTENT" -eq 0 ]; then
    EXCLUDES+=(--exclude=/srv/prepperpi/)
fi

if [ "$INCLUDE_SECRETS" -eq 0 ]; then
    EXCLUDES+=(
        --exclude=/etc/NetworkManager/system-connections/
        --exclude=/etc/wpa_supplicant/wpa_supplicant.conf
        --exclude=/etc/wpa_supplicant/wpa_supplicant-*.conf
    )
fi

log "rsyncing rootfs..."
RSYNC_T0=$(date +%s)
rsync -aHAX --one-file-system --no-inc-recursive "${EXCLUDES[@]}" / "$MNT/" >/dev/null
RSYNC_T1=$(date +%s)
log "rsync rootfs done in $(( RSYNC_T1 - RSYNC_T0 ))s"

# Recreate stripped mountpoint dirs so mount targets exist on first boot.
mkdir -p "$MNT/proc" "$MNT/sys" "$MNT/dev" "$MNT/run" "$MNT/tmp" \
         "$MNT/var/tmp" "$MNT/var/log/journal" "$MNT/var/lib/dhcp"

# /srv/prepperpi must exist as a mountpoint regardless of content toggle:
# when content was excluded the rsync exclude wipes it; when /srv is on a
# separate volume on the source, --one-file-system already skipped it.
mkdir -p "$MNT/srv/prepperpi"

# Mark machine-id "uninitialized" so the first boot of a flashed clone
# triggers ConditionFirstBoot=yes — both for our prepperpi-firstboot
# service AND for systemd's own first-boot units (regenerate_ssh_host_keys,
# systemd-firstboot, first-boot-complete.target). An EMPTY file does not
# satisfy ConditionFirstBoot — only "missing" or this exact sentinel
# string does. See systemd.unit(5) and machine-id(5).
printf 'uninitialized\n' > "$MNT/etc/machine-id"

# ---------- rsync boot ----------

log "rsyncing boot partition..."
rsync -aHAX --no-inc-recursive /boot/firmware/ "$MNT/boot/firmware/" >/dev/null

# ---------- rewrite cmdline.txt + fstab ----------

CMDLINE_FILE="$MNT/boot/firmware/cmdline.txt"
sed -i "s|root=PARTUUID=[A-Za-z0-9-]*|root=PARTUUID=$NEW_ROOT_PARTUUID|" "$CMDLINE_FILE"
grep -q "PARTUUID=$NEW_ROOT_PARTUUID" "$CMDLINE_FILE" || {
    echo "error: cmdline.txt did not get the new root PARTUUID" >&2
    cat "$CMDLINE_FILE" >&2; exit 1;
}

FSTAB_FILE="$MNT/etc/fstab"
OLD_BOOT_PARTUUID=$(blkid -s PARTUUID -o value "$BOOT_DEV")
OLD_ROOT_PARTUUID=$(blkid -s PARTUUID -o value "$ROOTFS_DEV")
sed -i \
    -e "s|PARTUUID=$OLD_BOOT_PARTUUID|PARTUUID=$NEW_BOOT_PARTUUID|" \
    -e "s|PARTUUID=$OLD_ROOT_PARTUUID|PARTUUID=$NEW_ROOT_PARTUUID|" \
    "$FSTAB_FILE"
grep -q "PARTUUID=$NEW_ROOT_PARTUUID" "$FSTAB_FILE" || {
    echo "error: fstab did not get the new root PARTUUID" >&2
    cat "$FSTAB_FILE" >&2; exit 1;
}

log "rewrote cmdline.txt + fstab with new PARTUUIDs"

# ---------- finalize image ----------

sync

# Capture the in-image rootfs usage for the sidecar before unmounting.
ROOT_USED_IN_IMG=$(fs_used_bytes "$MNT")

# When the journal was deferred, add it back now via tune2fs. The
# filesystem MUST be unmounted for tune2fs -j to allocate a proper
# hidden journal in inode 8 (rather than creating a regular .journal
# file at the root). Unmount, run tune2fs, then drop the cleanup steps
# that would have done the same unmounts.
if [ "$DEFER_JOURNAL" -eq 1 ]; then
    log "unmounting + adding journal via tune2fs..."
    TUNE_T0=$(date +%s)
    umount "$MNT/boot/firmware"
    umount "$MNT"
    rmdir "$MNT"
    # Drop the cleanup steps for the now-released mounts.
    new_steps=()
    for step in "${CLEANUP_STEPS[@]}"; do
        case "$step" in
            *"umount '$MNT/boot/firmware'"*) ;;
            *"umount '$MNT'"*)               ;;
            *"rmdir '$MNT'"*)                ;;
            *) new_steps+=("$step") ;;
        esac
    done
    CLEANUP_STEPS=("${new_steps[@]}")
    e2fsck -fp "${LOOP}p2" >/dev/null
    tune2fs -j "${LOOP}p2" >/dev/null
    TUNE_T1=$(date +%s)
    log "tune2fs -j done in $(( TUNE_T1 - TUNE_T0 ))s"
fi

# Atomic rename so partial files are never left around with the final name.
mv "$IMG_PATH.partial" "$IMG_PATH"
# Drop the partial-cleanup; the file is now the final output.
new_steps=()
for step in "${CLEANUP_STEPS[@]}"; do
    case "$step" in *"'$IMG_PATH.partial'"*) ;; *) new_steps+=("$step") ;; esac
done
CLEANUP_STEPS=("${new_steps[@]}")

# ---------- create content tar (only when /srv is separate) ----------

TAR_SHA256=""
TAR_SIZE_BYTES=0
if [ "$PRODUCE_TAR" -eq 1 ]; then
    log "creating content tarball: $TAR_PATH"
    rm -f -- "$TAR_PATH" "$TAR_PATH.partial"
    push_cleanup "rm -f -- '$TAR_PATH.partial'"

    TAR_T0=$(date +%s)
    # -p preserves perms; --xattrs / --acls capture extended attrs.
    # --one-file-system stays on /srv/prepperpi's filesystem.
    tar -cpf "$TAR_PATH.partial" \
        --xattrs --acls --one-file-system \
        -C /srv prepperpi
    TAR_T1=$(date +%s)
    log "tar done in $(( TAR_T1 - TAR_T0 ))s"

    TAR_SIZE_BYTES=$(stat -c '%s' "$TAR_PATH.partial")

    if [ "$WANT_SHA256" -eq 1 ]; then
        log "sha256 of content tarball..."
        TAR_SHA256=$(sha256sum "$TAR_PATH.partial" | awk '{ print $1 }')
        log "tar sha256: $TAR_SHA256"
    fi

    mv "$TAR_PATH.partial" "$TAR_PATH"
    new_steps=()
    for step in "${CLEANUP_STEPS[@]}"; do
        case "$step" in *"'$TAR_PATH.partial'"*) ;; *) new_steps+=("$step") ;; esac
    done
    CLEANUP_STEPS=("${new_steps[@]}")
fi

# ---------- compute sha256 of the .img ----------

IMG_SHA256=""
if [ "$WANT_SHA256" -eq 1 ]; then
    log "sha256 of image (this can take a while)..."
    SHA_T0=$(date +%s)
    IMG_SHA256=$(sha256sum "$IMG_PATH" | awk '{ print $1 }')
    SHA_T1=$(date +%s)
    log "image sha256 in $(( SHA_T1 - SHA_T0 ))s: $IMG_SHA256"
fi

# ---------- write sidecars ----------

write_sidecar() {
    local sidecar="$1" json_body="$2"
    printf '%s\n' "$json_body" > "$sidecar"
    log "wrote sidecar: $sidecar"
}

# Image sidecar.
INCLUDE_CONTENT_JSON=$([ $INCLUDE_CONTENT -eq 1 ] && echo true || echo false)
INCLUDE_SECRETS_JSON=$([ $INCLUDE_SECRETS -eq 1 ] && echo true || echo false)
PRODUCE_TAR_JSON=$([ $PRODUCE_TAR -eq 1 ] && echo true || echo false)

cat > "$IMG_SIDECAR" <<EOF
{
    "schema_version": 1,
    "kind": "system_image",
    "image_path": "$(basename "$IMG_PATH")",
    "image_size_bytes": $TOTAL_BYTES,
    "image_sha256": "$IMG_SHA256",
    "rootfs_used_in_image_bytes": $ROOT_USED_IN_IMG,
    "source_hostname": "$HOSTNAME_SHORT",
    "source_layout": "$SRV_LAYOUT",
    "include_content": $INCLUDE_CONTENT_JSON,
    "include_secrets": $INCLUDE_SECRETS_JSON,
    "content_tar_present": $PRODUCE_TAR_JSON,
    "content_tar_path": $([ $PRODUCE_TAR -eq 1 ] && printf '"%s"' "$(basename "$TAR_PATH")" || echo null),
    "rootfs_partition_size_bytes": $ROOTFS_BYTES,
    "boot_partition_size_bytes": $BOOT_BYTES,
    "new_root_partuuid": "$NEW_ROOT_PARTUUID",
    "new_boot_partuuid": "$NEW_BOOT_PARTUUID",
    "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
log "wrote sidecar: $IMG_SIDECAR"

# Tar sidecar (only when produced).
if [ "$PRODUCE_TAR" -eq 1 ]; then
    cat > "$TAR_SIDECAR" <<EOF
{
    "schema_version": 1,
    "kind": "content_tar",
    "tar_path": "$(basename "$TAR_PATH")",
    "tar_size_bytes": $TAR_SIZE_BYTES,
    "tar_sha256": "$TAR_SHA256",
    "system_image": "$(basename "$IMG_PATH")",
    "source_hostname": "$HOSTNAME_SHORT",
    "created_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF
    log "wrote sidecar: $TAR_SIDECAR"
fi

log "DONE"
log "  image:    $IMG_PATH"
if [ "$PRODUCE_TAR" -eq 1 ]; then
    log "  content:  $TAR_PATH"
fi
