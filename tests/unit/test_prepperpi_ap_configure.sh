#!/usr/bin/env bash
# tests/unit/test_prepperpi_ap_configure.sh
#
# Exercises the pure-function helpers in
# services/prepperpi-ap/prepperpi-ap-configure.sh. We can't call the
# top-level `main` (it touches /etc, /sys, and ip), but we can source
# the file and test the helpers directly.
#
# Run with:
#   bash tests/unit/test_prepperpi_ap_configure.sh

set -euo pipefail

REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
SCRIPT="${REPO_DIR}/services/prepperpi-ap/prepperpi-ap-configure.sh"

# ---------- tiny test harness ----------
FAIL=0
PASS=0

assert_eq() {
  local expected="$1" got="$2" msg="$3"
  if [[ "$expected" == "$got" ]]; then
    PASS=$((PASS+1))
    printf '  ok  %s\n' "$msg"
  else
    FAIL=$((FAIL+1))
    printf '  FAIL %s\n       expected: %s\n            got: %s\n' "$msg" "$expected" "$got"
  fi
}

# Source the script to get its helper functions. The script guards its
# own `main` behind a `BASH_SOURCE == $0` check, so sourcing is safe.
# shellcheck disable=SC1090
source "$SCRIPT"

# ---------- default_ssid_from_mac ----------
printf 'default_ssid_from_mac\n'
assert_eq "PrepperPi-3A7F" "$(default_ssid_from_mac dca632aa3a7f)" "colonless lowercase MAC"
assert_eq "PrepperPi-3A7F" "$(default_ssid_from_mac DCA632AA3A7F)" "colonless uppercase MAC"
assert_eq "PrepperPi-0000" "$(default_ssid_from_mac 000000000000)" "all zeros"
assert_eq "PrepperPi-FFFF" "$(default_ssid_from_mac ffffffffffff)" "all Fs"

# ---------- render_auth_block ----------
# Note: $() strips trailing newlines, so expected values omit them.
printf 'render_auth_block\n'
open_expected=$'auth_algs=1\nwpa=0'
assert_eq "$open_expected" "$(render_auth_block '')" "empty password -> open network"

wpa_expected=$'auth_algs=1\nwpa=2\nwpa_key_mgmt=WPA-PSK\nrsn_pairwise=CCMP\nwpa_passphrase=hunter2hunter'
assert_eq "$wpa_expected" "$(render_auth_block 'hunter2hunter')" "valid password -> WPA2 block"

# A 7-char password should fail (die() exits the subshell).
if out=$(render_auth_block 'short' 2>&1); then
  FAIL=$((FAIL+1))
  printf '  FAIL 7-char password should have failed, got: %s\n' "$out"
else
  PASS=$((PASS+1))
  printf '  ok  7-char password rejected\n'
fi

# ---------- render_template ----------
printf 'render_template\n'

TMPDIR_TEST=$(mktemp -d)
trap 'rm -rf "$TMPDIR_TEST"' EXIT

tmpl="${TMPDIR_TEST}/in.tmpl"
out="${TMPDIR_TEST}/out"
cat > "$tmpl" <<'EOF'
name=@SSID@
block=@AUTH_BLOCK@
trail=@INTERFACE@
EOF

# Runs as unprivileged user, so we need install to skip -o root -g root.
# Shadow install with a thin wrapper that preserves the command for tests.
install() { command install -m 0644 "${@: -2}"; }
render_template "$tmpl" "$out" \
  SSID "PrepperPi-TEST" \
  AUTH_BLOCK $'auth_algs=1\nwpa=2\nwpa_passphrase=s3cret&more' \
  INTERFACE "wlan0"
unset -f install

expected=$'name=PrepperPi-TEST\nblock=auth_algs=1\nwpa=2\nwpa_passphrase=s3cret&more\ntrail=wlan0\n'
got=$(cat "$out"; printf x); got="${got%x}"
assert_eq "$expected" "$got" "multi-line value with ampersand expands verbatim"

# ---------- summary ----------
printf '\n%s passed, %s failed\n' "$PASS" "$FAIL"
exit $(( FAIL == 0 ? 0 : 1 ))
