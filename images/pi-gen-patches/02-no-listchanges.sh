#!/bin/bash -e
# PrepperPi patch for pi-gen, installed into
# pi-gen/stage0/00-configure-apt/02-run.sh by images/build.sh. Runs
# once, early -- after pi-gen configures apt but before any stage
# installs apt-listchanges.
#
# What apt-listchanges does, and why we neutralize it:
#
# apt-listchanges hooks dpkg's invoke-post trigger and, for every
# package that gets installed, reaches out to
# metadata.ftp-master.debian.org over HTTPS to fetch the changelog
# so it can print "what changed in this version." It's purely
# decorative. On Docker Desktop for macOS the embedded DNS resolver
# (vpnkit) is flaky on that specific hostname -- works fine for
# deb.debian.org and archive.raspberrypi.com, which is why apt itself
# succeeds -- and every fetch blocks on the full resolver timeout
# (~30 s). With ~60 packages in stage0/02-firmware alone, that's
# minutes of dead time per stage.
#
# An earlier version of this patch pinned the package at APT priority
# -1 to block installation entirely. That worked, but pi-gen's stage2
# EXPLICITLY lists apt-listchanges in its install set, so apt aborted
# the build with "E: Package 'apt-listchanges' has no installation
# candidate". Preseeding debconf is the right answer: let it install,
# but tell it up front that its frontend is `none`. With that setting,
# the dpkg-trigger invocation exits immediately without fetching
# anything. No DNS, no timeout, no noise.

on_chroot <<'CHROOT'
debconf-set-selections <<'SEED'
apt-listchanges apt-listchanges/frontend select none
apt-listchanges apt-listchanges/email-address string root@localhost
apt-listchanges apt-listchanges/confirm boolean false
apt-listchanges apt-listchanges/save-seen boolean true
SEED
CHROOT
