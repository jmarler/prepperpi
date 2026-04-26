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
readonly USB_DIR="${USB_DIR:-/srv/prepperpi/user-usb}"
readonly LIB_XML="${LIB_XML:-/var/lib/prepperpi/library.xml}"
readonly FRAGMENT="${FRAGMENT:-/opt/prepperpi/web/landing/_library.html}"
readonly SEARCH_FRAGMENT="${SEARCH_FRAGMENT:-/opt/prepperpi/web/landing/_library_search.html}"
readonly STATE_FILE="${STATE_FILE:-/var/lib/prepperpi/last-library-state.txt}"
readonly EVENT_EMITTER="${EVENT_EMITTER:-/opt/prepperpi/services/prepperpi-events/emit-event.py}"
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

# Read per-book metadata from the just-built library.xml. kiwix-manage
# emits one self-closing <book> element per entry, all attributes on
# the same logical line. We pull the fields we need (id, name, title,
# articleCount) and reconstruct an absolute path from the basename so
# the caller doesn't have to care whether kiwix-manage stored it
# relative to library.xml (it does).
#
# Two identifiers for each book live in library.xml: an internal
# `name` (e.g. wikipedia_ab_all) used for OPDS metadata and as the
# only reliable selector for `books.id`-style API calls; and a
# file-basename URL slug (e.g. wikipedia_ab_all_nopic_2026-04) that
# kiwix-serve uses in its public URL routes (/content/<slug> and the
# viewer's #<slug> fragment). The slug is the .zim filename minus
# the extension -- not surfaced as an XML attribute, so we derive
# it from the `path=` attribute.
#
# Caveats from kiwix-tools 3.7:
#  - books.name=<name> returns "No such book" even for an exact
#    match. Use books.id=<UUID> instead.
#  - /library/<name>/ redirects to /library/content/<name>/, which
#    404s; only /library/content/<slug>/ works.
#
# Path handling: kiwix-manage stores `path=` relative to the
# library.xml directory. Local-disk ZIMs and USB ZIMs both end up
# relative (e.g. ../../../srv/prepperpi/zim/foo.zim and
# ../../../srv/prepperpi/user-usb/MyDrive/foo.zim respectively).
# We emit the path as-is and let the bash caller resolve it against
# the library.xml directory.
#
# Emits one TSV line per book:
#   id<TAB>name<TAB>slug<TAB>title<TAB>raw_path<TAB>articleCount
library_entries() {
  [[ -f "$LIB_XML" ]] || return 0
  awk '
    /<book / {
      id=""; name=""; title=""; path=""; count="";
      if (match($0, /[[:space:]]id="[^"]*"/))           id=substr($0, RSTART+5, RLENGTH-6);
      if (match($0, /[[:space:]]name="[^"]*"/))         name=substr($0, RSTART+7, RLENGTH-8);
      if (match($0, /[[:space:]]title="[^"]*"/))        title=substr($0, RSTART+8, RLENGTH-9);
      if (match($0, /[[:space:]]path="[^"]*"/))         path=substr($0, RSTART+7, RLENGTH-8);
      if (match($0, /[[:space:]]articleCount="[^"]*"/)) count=substr($0, RSTART+15, RLENGTH-16);
      if (id == "" || name == "" || path == "") next;
      n = split(path, parts, "/");
      filename = parts[n];
      slug = filename;
      sub(/\.zim$/, "", slug);
      if (title == "") title = name;
      print id "\t" name "\t" slug "\t" title "\t" path "\t" count;
    }
  ' "$LIB_XML"
}

# Resolve a kiwix-manage path attribute (relative to the library.xml
# directory) to an absolute filesystem path. Absolute inputs are
# returned unchanged.
resolve_book_path() {
  local raw="$1"
  if [[ "$raw" = /* ]]; then
    printf '%s' "$raw"
  else
    realpath -m -- "$(dirname "$LIB_XML")/$raw"
  fi
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

  # Discover ZIMs:
  #  - USB:   anywhere up to 4 levels deep under each mounted USB
  #           volume. Bounding the depth keeps the scan fast on a
  #           large drive while still finding ZIMs in nested
  #           "wikipedia/2026/" style folders.
  #  - Local: top-level of /srv/prepperpi/zim (single-level glob).
  #
  # USB is scanned FIRST because kiwix-manage's `add` is last-write-
  # wins for duplicate UUIDs: if the same ZIM exists on both a USB
  # drive and the SD card, the second `add` rewrites the existing
  # `<book>` entry's `path=` to point at the second source. By
  # adding USB first and local second, the final library.xml points
  # at the local copy when both are present -- which is what we
  # want, since local storage doesn't disappear on a USB yank.
  #
  # If a ZIM is unreadable (USB yanked mid-scan), kiwix-manage add
  # fails with EIO and we WARN+skip; the partial library.xml is
  # still valid and kiwix-serve picks up exactly the ZIMs we managed
  # to read.
  local -a sources=()
  if [[ -d "$USB_DIR" ]]; then
    while IFS= read -r -d '' z; do
      sources+=("$z")
    done < <(find "$USB_DIR" -mindepth 2 -maxdepth 5 -name '*.zim' -type f -print0 2>/dev/null)
  fi
  if [[ -d "$ZIM_DIR" ]]; then
    while IFS= read -r -d '' z; do
      sources+=("$z")
    done < <(find "$ZIM_DIR" -mindepth 1 -maxdepth 1 -name '*.zim' -type f -print0 2>/dev/null)
  fi

  local zim before after added=0 replaced=0
  for zim in "${sources[@]}"; do
    before=$(grep -c '<book ' "$LIB_XML" 2>/dev/null || true)
    if kiwix-manage "$LIB_XML" add "$zim" >/dev/null 2>&1; then
      after=$(grep -c '<book ' "$LIB_XML" 2>/dev/null || true)
      if (( before == after )); then
        # Same book count -> kiwix-manage replaced an existing
        # entry with this one (duplicate UUID). Useful to see in
        # journals when a USB is shadowing/being shadowed by a
        # local copy.
        log "  duplicate-UUID replace: ${zim}"
        replaced=$((replaced + 1))
      else
        added=$((added + 1))
      fi
    else
      log "WARN: kiwix-manage failed to add ${zim}; skipping"
    fi
  done
  if (( replaced > 0 )); then
    log "  ${replaced} duplicate-UUID entr$([[ $replaced -eq 1 ]] && echo y || echo ies) collapsed"
  fi

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
  local tiles_tmp search_tmp
  tiles_tmp=$(mktemp)
  search_tmp=$(mktemp)
  # shellcheck disable=SC2064
  trap "rm -f '$tiles_tmp' '$search_tmp'" RETURN

  local count=0 ids=()
  while IFS=$'\t' read -r id name slug title raw_path articles; do
    [[ -z "$name" ]] && continue
    local abs_path size_bytes size_h articles_txt
    abs_path=$(resolve_book_path "$raw_path")
    size_bytes=$(stat -c%s "$abs_path" 2>/dev/null || echo 0)
    size_h=$(human_size "$size_bytes")
    if [[ -n "$articles" && "$articles" =~ ^[0-9]+$ ]]; then
      articles_txt="$(printf "%'d" "$articles" 2>/dev/null || printf '%s' "$articles") articles"
    else
      articles_txt="ZIM library"
    fi
    count=$((count + 1))
    ids+=("$id")
    # Storage location indicator: tells the user whether the ZIM
    # lives on the SD card or on a removable USB. Helpful for
    # capacity planning ("can I yank this USB?") and trust
    # decisions ("is this content going to disappear?").
    local storage_txt
    case "$abs_path" in
      "$USB_DIR"/*)
        local volume
        volume="${abs_path#$USB_DIR/}"
        volume="${volume%%/*}"
        storage_txt="$size_h on external USB ($(html_escape "$volume"))"
        ;;
      *)
        storage_txt="$size_h on internal disk"
        ;;
    esac
    {
      printf '<article class="tile tile--library" aria-labelledby="tile-zim-%s-title">\n' "$(html_escape "$slug")"
      printf '  <div class="tile__icon" aria-hidden="true">📖</div>\n'
      printf '  <h2 id="tile-zim-%s-title" class="tile__title">\n' "$(html_escape "$slug")"
      printf '    <a href="/library/viewer#%s">%s</a>\n' "$(html_escape "$slug")" "$(html_escape "$title")"
      printf '  </h2>\n'
      printf '  <p class="tile__desc">%s</p>\n' "$articles_txt"
      printf '  <p class="tile__status">%s</p>\n' "$storage_txt"
      printf '</article>\n'
    } >>"$tiles_tmp"
  done < <(library_entries)

  if (( count == 0 )); then
    {
      printf '<article class="tile tile--unavailable" aria-labelledby="tile-library-empty-title">\n'
      printf '  <div class="tile__icon" aria-hidden="true">📚</div>\n'
      printf '  <h2 id="tile-library-empty-title" class="tile__title">Library</h2>\n'
      printf '  <p class="tile__desc">No ZIM files installed yet.</p>\n'
      printf '  <p class="tile__status">Drop a <code>.zim</code> file into <code>/srv/prepperpi/zim/</code>.</p>\n'
      printf '</article>\n'
    } >>"$tiles_tmp"
    # Empty library -> empty search fragment -> landing page renders
    # no form at all (it would only return "No book matches" on submit).
    : >"$search_tmp"
  else
    # kiwix-serve's /search requires at least one books.id / books.name
    # / books.filter.* to scope the query; a bare pattern returns a 400
    # "No book matches selection criteria". Enumerate every UUID here
    # so "search across all loaded books" is the effective default
    # with no client-side JS.
    {
      printf '<form class="library-search" role="search" action="/library/search" method="get">\n'
      printf '  <label class="library-search__label" for="library-search-input">Search the library</label>\n'
      printf '  <div class="library-search__row">\n'
      printf '    <input id="library-search-input" class="library-search__input" type="search" name="pattern" placeholder="Wikipedia, first aid, solar oven&hellip;" autocomplete="off" required>\n'
      printf '    <button class="library-search__submit" type="submit">Search</button>\n'
      printf '  </div>\n'
      local book_id
      for book_id in "${ids[@]}"; do
        printf '  <input type="hidden" name="books.id" value="%s">\n' "$(html_escape "$book_id")"
      done
      printf '</form>\n'
    } >"$search_tmp"
  fi

  install -m 0644 "$tiles_tmp" "$FRAGMENT"
  install -m 0644 "$search_tmp" "$SEARCH_FRAGMENT"
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

# Emit a `library_changed` event if the set of indexed ZIMs differs
# from the previous run. We compare the sorted list of book IDs
# (UUIDs) snapshot-to-snapshot. Used so the dashboard can surface
# a single "Library updated" toast when something actually changed,
# without firing on every ctime tick (each USB plug-in fires the
# path watcher even for drives that contain no ZIMs).
emit_library_change_event() {
  [[ -x "$EVENT_EMITTER" ]] || return 0
  install -d -m 0755 "$(dirname "$STATE_FILE")"

  local current previous
  current=$(grep -oE 'id="[^"]+"' "$LIB_XML" 2>/dev/null | sort -u || true)
  previous=$(cat "$STATE_FILE" 2>/dev/null || true)

  if [[ "$current" == "$previous" ]]; then
    return 0
  fi

  printf '%s\n' "$current" >"$STATE_FILE"

  local cur_count prev_count
  cur_count=$(printf '%s\n' "$current" | grep -c '^id=' || true)
  prev_count=$(printf '%s\n' "$previous" | grep -c '^id=' || true)

  local msg
  if (( cur_count == 0 )); then
    msg="Library cleared"
  elif (( cur_count > prev_count )); then
    msg="Library updated · ${cur_count} book$([[ $cur_count -eq 1 ]] && echo '' || echo 's')"
  elif (( cur_count < prev_count )); then
    msg="Library updated · ${cur_count} book$([[ $cur_count -eq 1 ]] && echo '' || echo 's') remaining"
  else
    msg="Library updated"
  fi
  "$EVENT_EMITTER" library_changed "$msg" || true
}

main() {
  if [[ $EUID -ne 0 ]]; then
    echo "build-library-index.sh must be run as root" >&2
    exit 1
  fi
  # 0775 (not 0755) so the prepperpi-admin user — which is in the
  # `prepperpi` group — can unlink ZIMs from the directory. Aria2c
  # writes here as `prepperpi`; admin only deletes.
  install -d -m 0775 "$ZIM_DIR" 2>/dev/null || true
  rebuild_library_xml
  rebuild_fragment
  reload_kiwix
  emit_library_change_event
}

main "$@"
