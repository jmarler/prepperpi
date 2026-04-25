#!/usr/bin/env bash
# prepperpi-usb-mount.sh — mount a USB partition read-only at
# /srv/prepperpi/user-usb/<sanitized-label>. Invoked by
# prepperpi-usb-mount@<kernel>.service which is triggered by
# /etc/udev/rules.d/99-prepperpi-usb.rules on USB partition add.
#
# Read-only is the only mode E2-S2 supports. Per-mount writes land
# in E4 with the admin console.

set -euo pipefail

readonly DEV_NAME="${1:?usage: $0 <kernel-name like sda1>}"
readonly DEV_PATH="/dev/${DEV_NAME}"
readonly MOUNT_BASE="${MOUNT_BASE:-/srv/prepperpi/user-usb}"
readonly SERVICE_USER="${SERVICE_USER:-prepperpi}"
readonly SERVICE_GROUP="${SERVICE_GROUP:-prepperpi}"

log()  { printf '[prepperpi-usb-mount] %s\n' "$*"; }
warn() { printf '[prepperpi-usb-mount] WARN: %s\n' "$*" >&2; }
die()  { printf '[prepperpi-usb-mount] FATAL: %s\n' "$*" >&2; exit 1; }

[[ -b "$DEV_PATH" ]] || die "no such block device: $DEV_PATH"

FSTYPE=$(blkid -o value -s TYPE  "$DEV_PATH" 2>/dev/null || true)
LABEL=$(blkid  -o value -s LABEL "$DEV_PATH" 2>/dev/null || true)
UUID=$(blkid   -o value -s UUID  "$DEV_PATH" 2>/dev/null || true)

if [[ -z "$FSTYPE" ]]; then
  log "no recognized filesystem on $DEV_PATH; ignoring"
  exit 0
fi

case "$FSTYPE" in
  vfat|exfat|ntfs|ext2|ext3|ext4) ;;
  *) log "unsupported filesystem '$FSTYPE' on $DEV_PATH; ignoring"; exit 0 ;;
esac

# Sanitize the volume label into a URL-safe slug. Allow alnum, dot,
# dash, underscore. Replace runs of other chars with a single dash;
# trim leading/trailing dashes.
sanitize() {
  local s="${1:-}"
  s=$(printf '%s' "$s" | tr -c 'a-zA-Z0-9._-' '-' | tr -s '-')
  s="${s#-}"; s="${s%-}"
  printf '%s' "$s"
}

NAME=""
[[ -n "$LABEL" ]] && NAME=$(sanitize "$LABEL")
[[ -z "$NAME" && -n "$UUID" ]] && NAME="usb-${UUID:0:8}"
[[ -z "$NAME" ]] && NAME="$DEV_NAME"

# Resolve naming collisions (two volumes labeled "USB"): suffix -2, -3..
MOUNT_PATH="${MOUNT_BASE}/${NAME}"
i=2
while mountpoint -q "$MOUNT_PATH"; do
  if [[ "$(findmnt -nr -o SOURCE "$MOUNT_PATH" 2>/dev/null)" == "$DEV_PATH" ]]; then
    log "$DEV_PATH already mounted at $MOUNT_PATH; nothing to do"
    exit 0
  fi
  MOUNT_PATH="${MOUNT_BASE}/${NAME}-${i}"
  i=$((i + 1))
  (( i > 20 )) && die "too many mount-name collisions for '$NAME'"
done

install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$MOUNT_PATH"

# Common safety flags: read-only, no device nodes, no setuid bits,
# no exec. Stops a malicious USB from being able to drop a binary
# we'd accidentally run.
common_opts="ro,nodev,nosuid,noexec"

# uid/gid/umask only meaningful for filesystems that don't store
# unix ownership (vfat, exfat, ntfs); ext* uses on-disk ownership.
ownership_opts="uid=$(id -u "$SERVICE_USER"),gid=$(id -g "$SERVICE_GROUP"),umask=0022"

case "$FSTYPE" in
  vfat|exfat)
    mount -t "$FSTYPE" -o "${common_opts},${ownership_opts}" "$DEV_PATH" "$MOUNT_PATH"
    ;;
  ntfs)
    # ntfs-3g is the userspace driver Debian ships; the kernel `ntfs3`
    # is also available in Trixie but ntfs-3g handles permissions
    # mapping more predictably for cross-OS USB sticks.
    mount -t ntfs-3g -o "${common_opts},${ownership_opts}" "$DEV_PATH" "$MOUNT_PATH"
    ;;
  ext2|ext3|ext4)
    mount -t "$FSTYPE" -o "${common_opts}" "$DEV_PATH" "$MOUNT_PATH"
    ;;
esac

log "mounted $DEV_PATH at $MOUNT_PATH (type=$FSTYPE, label=${LABEL:-<none>})"
