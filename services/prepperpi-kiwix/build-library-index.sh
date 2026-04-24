#!/usr/bin/env bash
# build-library-index.sh — scan /srv/prepperpi/zim, rebuild the kiwix
# library XML, rebuild the landing-page tile fragment, and restart
# kiwix-serve so it sees the new library.
#
# Invoked by prepperpi-kiwix-reindex.service (oneshot, triggered by
# prepperpi-kiwix-reindex.path). Also invoked once during install so
# the landing page and library.xml exist before first boot.
#
# Safe to re-run.

set -euo pipefail

readonly ZIM_DIR="${ZIM_DIR:-/srv/prepperpi/zim}"
readonly LIB_XML="${LIB_XML:-/var/lib/prepperpi/library.xml}"
readonly FRAGMENT="${FRAGMENT:-/opt/prepperpi/web/landing/_library.html}"
readonly SERVICE_USER="${SERVICE_USER:-prepperpi}"
readonly SERVICE_GROUP="${SERVICE_GROUP:-prepperpi}"

log() { printf '[prepperpi-kiwix/reindex] %s\n' "$*"; }

# Human-readable file size. 1.2G / 340M / 12K. POSIX-friendly.
human_size() {
  local bytes="$1"
  if (( bytes >= 1073741824 )); then
    awk -v b="$bytes" 'BEGIN{printf "%.1fG", b/1073741824}'
  elif (( bytes >= 1048576 )); then
    awk -v b="$bytes" 'BEGIN{printf "%.0fM", b/1048576}'
  elif (( bytes >= 1024 )); then
    awk -v b="$bytes" 'BEGIN{printf "%.0fK", b/1024}'
  else
    printf '%dB' "$bytes"
  fi
}

# `zimdump info <file>` emits one key/value per line. The exact labels
# have drifted across zim-tools releases; we check a few likely forms
# and fall back to empty so the tile still renders if parsing fails.
# Yields key=value pairs on stdout for the caller to consume.
zim_info() {
  zimdump info "$1" 2>/dev/null || true
}

zim_field() {
  # $1 = info output, $2 = regex (matching at the start of a line, up to ':')
  awk -v re="$2" '
    {
      i = index($0, ":");
      if (i == 0) next;
      k = substr($0, 1, i-1);
      v = substr($0, i+1);
      gsub(/^[ \t]+|[ \t]+$/, "", k);
      gsub(/^[ \t]+|[ \t]+$/, "", v);
      if (tolower(k) ~ re) { print v; exit }
    }
  ' <<<"$1"
}

# HTML escape helper for values we inline into the fragment.
html_escape() {
  local s="$1"
  s=${s//&/&amp;}
  s=${s//</&lt;}
  s=${s//>/&gt;}
  s=${s//\"/&quot;}
  s=${s//\'/&#39;}
  printf '%s' "$s"
}

rebuild_library_xml() {
  install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$(dirname "$LIB_XML")"
  # Start fresh every run. kiwix-manage has no "sync with directory"
  # mode, and incremental add/remove would drift on rename.
  rm -f "$LIB_XML"

  shopt -s nullglob
  local zim
  local added=0
  for zim in "$ZIM_DIR"/*.zim; do
    if kiwix-manage "$LIB_XML" add "$zim" >/dev/null 2>&1; then
      added=$((added + 1))
    else
      log "WARN: kiwix-manage failed to add ${zim}; skipping"
    fi
  done
  shopt -u nullglob

  if [[ -f "$LIB_XML" ]]; then
    chown "$SERVICE_USER:$SERVICE_GROUP" "$LIB_XML"
    chmod 0644 "$LIB_XML"
  else
    # Empty library still needs a valid file for kiwix-serve to start.
    printf '<?xml version="1.0" encoding="UTF-8"?>\n<library version="20110515"/>\n' >"$LIB_XML"
    chown "$SERVICE_USER:$SERVICE_GROUP" "$LIB_XML"
    chmod 0644 "$LIB_XML"
  fi

  log "library.xml rebuilt; ${added} ZIM(s) indexed"
}

rebuild_fragment() {
  install -d -m 0755 "$(dirname "$FRAGMENT")"
  local tmp
  tmp=$(mktemp)
  # shellcheck disable=SC2064
  trap "rm -f '$tmp'" RETURN

  shopt -s nullglob
  local count=0 zim
  for zim in "$ZIM_DIR"/*.zim; do
    local name info title articles size_bytes size_h articles_txt
    name=$(basename "${zim%.zim}")
    info=$(zim_info "$zim")
    title=$(zim_field "$info" '^(title)$')
    [[ -z "$title" ]] && title="$name"
    articles=$(zim_field "$info" '^(count-articles|article-count|articles|article count)$')
    size_bytes=$(stat -c%s "$zim" 2>/dev/null || echo 0)
    size_h=$(human_size "$size_bytes")
    if [[ -n "$articles" && "$articles" =~ ^[0-9]+$ ]]; then
      articles_txt="$(printf "%'d" "$articles" 2>/dev/null || printf '%s' "$articles") articles"
    else
      articles_txt="ZIM library"
    fi
    count=$((count + 1))
    {
      printf '<article class="tile tile--library" aria-labelledby="tile-zim-%s-title">\n' "$(html_escape "$name")"
      printf '  <div class="tile__icon" aria-hidden="true">📖</div>\n'
      printf '  <h2 id="tile-zim-%s-title" class="tile__title">\n' "$(html_escape "$name")"
      printf '    <a href="/library/viewer#%s">%s</a>\n' "$(html_escape "$name")" "$(html_escape "$title")"
      printf '  </h2>\n'
      printf '  <p class="tile__desc">%s</p>\n' "$articles_txt"
      printf '  <p class="tile__status">%s on disk</p>\n' "$(html_escape "$size_h")"
      printf '</article>\n'
    } >>"$tmp"
  done
  shopt -u nullglob

  if (( count == 0 )); then
    {
      printf '<article class="tile tile--unavailable" aria-labelledby="tile-library-empty-title">\n'
      printf '  <div class="tile__icon" aria-hidden="true">📚</div>\n'
      printf '  <h2 id="tile-library-empty-title" class="tile__title">Library</h2>\n'
      printf '  <p class="tile__desc">No ZIM files installed yet.</p>\n'
      printf '  <p class="tile__status">Drop a <code>.zim</code> file into <code>/srv/prepperpi/zim/</code>.</p>\n'
      printf '</article>\n'
    } >>"$tmp"
  fi

  install -m 0644 "$tmp" "$FRAGMENT"
  log "fragment rebuilt; ${count} tile(s) rendered"
}

reload_kiwix() {
  # Only try to restart if the unit is already active -- during image
  # build we run before systemd is PID 1 in any meaningful sense.
  if systemctl is-active --quiet prepperpi-kiwix.service 2>/dev/null; then
    log "restarting prepperpi-kiwix.service"
    systemctl restart prepperpi-kiwix.service || log "WARN: restart failed"
  fi
}

main() {
  if [[ $EUID -ne 0 ]]; then
    echo "build-library-index.sh must be run as root" >&2
    exit 1
  fi
  install -d -m 0755 "$ZIM_DIR" 2>/dev/null || true
  rebuild_library_xml
  rebuild_fragment
  reload_kiwix
}

main "$@"
