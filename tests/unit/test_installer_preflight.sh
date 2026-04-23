#!/usr/bin/env bash
# tests/unit/test_installer_preflight.sh
#
# Exercises the pure-function preflight helpers in installer/install.sh.
# The script's top-level main() is guarded behind a BASH_SOURCE == $0
# check, so sourcing is safe.
#
# Run with:
#   bash tests/unit/test_installer_preflight.sh

set -euo pipefail

REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
SCRIPT="${REPO_DIR}/installer/install.sh"

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

assert_rc() {
  local expected="$1" got="$2" msg="$3"
  if [[ "$expected" == "$got" ]]; then
    PASS=$((PASS+1))
    printf '  ok  %s\n' "$msg"
  else
    FAIL=$((FAIL+1))
    printf '  FAIL %s (expected rc %s, got %s)\n' "$msg" "$expected" "$got"
  fi
}

# shellcheck disable=SC1090
source "$SCRIPT"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

# ---------- detect_pi_model ----------
printf 'detect_pi_model\n'

printf '%s\0' "Raspberry Pi 4 Model B Rev 1.5" >"$TMP/model_pi4"
assert_eq "4" "$(detect_pi_model "$TMP/model_pi4")" "Pi 4B Rev 1.5"

printf '%s\0' "Raspberry Pi 5 Model B Rev 1.0" >"$TMP/model_pi5"
assert_eq "5" "$(detect_pi_model "$TMP/model_pi5")" "Pi 5 Rev 1.0"

printf '%s\0' "Raspberry Pi 3 Model B Plus Rev 1.3" >"$TMP/model_pi3"
assert_eq "" "$(detect_pi_model "$TMP/model_pi3")" "Pi 3B+ unsupported"

printf '%s\0' "Some Orange Pi 3 LTS" >"$TMP/model_orange"
assert_eq "" "$(detect_pi_model "$TMP/model_orange")" "Orange Pi unsupported"

assert_eq "" "$(detect_pi_model "$TMP/does_not_exist")" "missing model file → empty"

# ---------- detect_os_codename ----------
printf 'detect_os_codename\n'

cat >"$TMP/os_bookworm" <<'EOF'
PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"
NAME="Debian GNU/Linux"
VERSION_ID="12"
VERSION_CODENAME=bookworm
ID=debian
EOF
assert_eq "bookworm" "$(detect_os_codename "$TMP/os_bookworm")" "bookworm unquoted"

cat >"$TMP/os_trixie" <<'EOF'
VERSION_ID="13"
VERSION="13 (trixie)"
VERSION_CODENAME=trixie
ID=debian
EOF
assert_eq "trixie" "$(detect_os_codename "$TMP/os_trixie")" "trixie"

cat >"$TMP/os_quoted" <<'EOF'
VERSION_CODENAME="bookworm"
ID=debian
EOF
assert_eq "bookworm" "$(detect_os_codename "$TMP/os_quoted")" "quoted value stripped"

cat >"$TMP/os_bullseye" <<'EOF'
VERSION_CODENAME=bullseye
ID=debian
EOF
assert_eq "bullseye" "$(detect_os_codename "$TMP/os_bullseye")" "bullseye parsed (separate check)"

assert_eq "" "$(detect_os_codename "$TMP/does_not_exist")" "missing os-release → empty"

# ---------- is_supported_os_codename ----------
printf 'is_supported_os_codename\n'

is_supported_os_codename "bookworm" && rc=0 || rc=$?
assert_rc 0 "$rc" "bookworm supported"

is_supported_os_codename "trixie" && rc=0 || rc=$?
assert_rc 0 "$rc" "trixie supported"

is_supported_os_codename "forky" && rc=0 || rc=$?
assert_rc 0 "$rc" "forky supported (Debian 14 preview)"

is_supported_os_codename "bullseye" && rc=0 || rc=$?
assert_rc 1 "$rc" "bullseye (Debian 11) rejected"

is_supported_os_codename "" && rc=0 || rc=$?
assert_rc 1 "$rc" "empty codename rejected"

is_supported_os_codename "ubuntu" && rc=0 || rc=$?
assert_rc 1 "$rc" "ubuntu codename rejected"

# ---------- summary ----------
printf '\n%s passed, %s failed\n' "$PASS" "$FAIL"
exit $(( FAIL == 0 ? 0 : 1 ))
