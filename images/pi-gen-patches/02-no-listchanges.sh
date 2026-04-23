#!/bin/bash -e
# PrepperPi patch for pi-gen, installed into
# pi-gen/stage0/00-configure-apt/02-no-listchanges.sh by
# images/build.sh. Runs once, early -- after pi-gen configures apt
# but before any stage installs heavy package sets (stage0/02-firmware
# brings in gcc + kernel; stage1 / stage2 add more on top).
#
# Why this exists:
#
# apt-listchanges is a package that hooks into dpkg's invoke-post and,
# for every package being installed, reaches out to
# metadata.ftp-master.debian.org over HTTPS to fetch the changelog so
# it can print "what changed in this version." It's purely decorative.
# On Docker Desktop for macOS the embedded DNS resolver (vpnkit) flakes
# out on that specific hostname -- works fine for deb.debian.org and
# archive.raspberrypi.com, which is why apt itself succeeds -- and
# every fetch blocks on the full resolver timeout (~30 s). With ~60
# packages in stage0/02-firmware alone, that's minutes of dead time
# per stage. The build eventually completes but the CPU sits idle.
#
# We pin apt-listchanges at priority -1 so apt will refuse to install
# it even if some other package lists it as a Recommends dependency,
# and we purge any already-installed copy. The pin file sits at a
# deterministic path so a follow-up stage can verify the setup.

install -d "${ROOTFS_DIR}/etc/apt/preferences.d"
cat > "${ROOTFS_DIR}/etc/apt/preferences.d/prepperpi-no-apt-listchanges" <<'EOF'
Package: apt-listchanges
Pin: release *
Pin-Priority: -1
EOF

# Belt and suspenders: if a previous stage already pulled the package
# in, remove it now. Ignore the "not installed" case so this step stays
# idempotent across re-runs.
on_chroot <<EOF
if dpkg -l apt-listchanges 2>/dev/null | grep -q '^ii'; then
  apt-get -y purge apt-listchanges
fi
EOF
