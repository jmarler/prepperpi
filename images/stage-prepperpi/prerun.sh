#!/bin/bash -e
# Standard pi-gen prerun: inherit the rootfs from the previous stage
# (stage2 -- "lite" base) into this stage's WORK_DIR so our
# customization layers on top of a working headless Pi OS.

if [ ! -d "${ROOTFS_DIR}" ]; then
	copy_previous
fi
