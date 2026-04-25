#!/usr/bin/env bash
# build-usb-index.sh — render /opt/prepperpi/web/landing/_usb.html
# from the current set of subdirectories under /srv/prepperpi/user-usb
# that are actual mountpoints. Invoked by prepperpi-usb-reindex.service
# (oneshot, triggered by prepperpi-usb-reindex.path).

set -euo pipefail

readonly USB_DIR="${USB_DIR:-/srv/prepperpi/user-usb}"
readonly FRAGMENT="${FRAGMENT:-/opt/prepperpi/web/landing/_usb.html}"

log() { printf '[prepperpi-usb/reindex] %s\n' "$*"; }

# Render HTML-safe text. Mirrors the helper in prepperpi-kiwix's
# build script (small enough that copy-paste beats sharing a lib).
html_escape() {
  local s="$1"
  s=${s//&/&amp;}
  s=${s//</&lt;}
  s=${s//>/&gt;}
  s=${s//\"/&quot;}
  s=${s//\'/&#39;}
  printf '%s' "$s"
}

# Pull "<used>/<total>" via df. Falls back to "USB drive" if df
# can't see the mount (race during teardown).
mount_size() {
  local mp="$1"
  df -h --output=used,size "$mp" 2>/dev/null \
    | awk 'NR==2 {gsub(/^[ \t]+/, "", $1); printf "%s of %s used", $1, $2}' \
    || true
}

rebuild_fragment() {
  install -d -m 0755 "$(dirname "$FRAGMENT")"
  local tmp
  tmp=$(mktemp)
  # shellcheck disable=SC2064
  trap "rm -f '$tmp'" RETURN

  shopt -s nullglob
  local count=0 mp name size_txt
  for mp in "$USB_DIR"/*/; do
    # Trim trailing slash so basename is clean.
    mp="${mp%/}"
    # Only render tiles for live mountpoints; stale empty dirs from
    # an unclean unmount shouldn't show up.
    mountpoint -q "$mp" || continue
    name=$(basename "$mp")
    size_txt=$(mount_size "$mp")
    [[ -z "$size_txt" ]] && size_txt="USB drive"
    count=$((count + 1))
    {
      printf '<article class="tile tile--usb" aria-labelledby="tile-usb-%s-title">\n' "$(html_escape "$name")"
      printf '  <div class="tile__icon" aria-hidden="true">💾</div>\n'
      printf '  <h2 id="tile-usb-%s-title" class="tile__title">\n' "$(html_escape "$name")"
      printf '    <a href="/usb/%s/">%s</a>\n' "$(html_escape "$name")" "$(html_escape "$name")"
      printf '  </h2>\n'
      printf '  <p class="tile__desc">USB drive</p>\n'
      printf '  <p class="tile__status">%s</p>\n' "$(html_escape "$size_txt")"
      printf '</article>\n'
    } >>"$tmp"
  done
  shopt -u nullglob

  if (( count == 0 )); then
    {
      printf '<article class="tile tile--unavailable" aria-labelledby="tile-usb-empty-title">\n'
      printf '  <div class="tile__icon" aria-hidden="true">💾</div>\n'
      printf '  <h2 id="tile-usb-empty-title" class="tile__title">USB</h2>\n'
      printf '  <p class="tile__desc">No USB drives plugged in.</p>\n'
      printf '  <p class="tile__status">Insert a USB stick to add files.</p>\n'
      printf '</article>\n'
    } >>"$tmp"
  fi

  install -m 0644 "$tmp" "$FRAGMENT"
  log "fragment rebuilt; ${count} tile(s) rendered"
}

main() {
  rebuild_fragment
}

main "$@"
