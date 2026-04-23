#!/bin/bash -e
# Runs outside the chroot. Drops the PrepperPi source tree into the
# rootfs at /opt/prepperpi-src so the in-chroot step below can run
# `installer/install.sh --image-build` against it.
#
# PREPPERPI_REPO is an absolute path on the build host (set by
# images/build.sh or by the GitHub Actions workflow) pointing at the
# repo root. We bind-copy the checkout, intentionally excluding .git
# (image size) and tests (not needed at runtime).

: "${PREPPERPI_REPO:?PREPPERPI_REPO must be set to the repo root on the build host}"
: "${ROOTFS_DIR:?ROOTFS_DIR must be set by pi-gen}"

install -d -m 0755 "${ROOTFS_DIR}/opt/prepperpi-src"
rsync -a \
	--exclude='.git' \
	--exclude='tests' \
	--exclude='images' \
	"${PREPPERPI_REPO}/" "${ROOTFS_DIR}/opt/prepperpi-src/"

# Mark the source of this image so operators can trace back to a
# specific commit from a flashed Pi (`/etc/prepperpi/image.version`).
install -d -m 0755 "${ROOTFS_DIR}/etc/prepperpi"
{
	printf 'image_version=%s\n' "${PREPPERPI_VERSION:-unknown}"
	printf 'git_commit=%s\n'    "${PREPPERPI_COMMIT:-unknown}"
	printf 'built_at=%s\n'      "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
} > "${ROOTFS_DIR}/etc/prepperpi/image.version"
chmod 0644 "${ROOTFS_DIR}/etc/prepperpi/image.version"
