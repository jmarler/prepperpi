#!/bin/bash -e
# Runs inside the chroot on the target rootfs. Invokes the PrepperPi
# installer in image-build mode: no preflight (we're on the build
# host, not a Pi), no interactive prompt, no reboot.
#
# The installer is responsible for provisioning the `prepperpi`
# system user, the /srv/prepperpi state tree, and running each
# services/*/setup.sh (which apt-installs packages, drops systemd
# units, enables them). By the time this returns, the image is a
# fully-provisioned PrepperPi appliance; first boot just starts the
# already-enabled units.

cd /opt/prepperpi-src
bash installer/install.sh --image-build
