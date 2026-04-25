#!/usr/bin/env bash
# setup.sh — install prepperpi-tiles (tileserver-gl-light + MapLibre
# client) onto the running system. Intended to be called by the
# top-level installer (installer/install.sh).
#
# Architecture:
#   - tileserver-gl-light runs as a sandboxed systemd unit on
#     127.0.0.1:8083, serving vector tiles, glyphs, sprites, and
#     style.json from /etc/prepperpi/tileserver/.
#   - Caddy reverse-proxies /maps/{styles,data,fonts,sprites}/* to
#     the tileserver, and serves the MapLibre client at /maps/.
#   - prepperpi-tiles-reindex.{path,service} watches
#     /srv/prepperpi/maps for .mbtiles add/remove and regenerates
#     config.json + composite style.json + landing-page tile.
#
# Assets pulled from upstream at install time (with a local cache
# under /var/lib/prepperpi/maps/cache so a re-run on the dev Pi
# doesn't re-download):
#   - tileserver-gl-light       (npm)
#   - maplibre-gl JS + CSS      (https://unpkg.com/maplibre-gl)
#   - osm-bright-gl-style       (GitHub releases)
#   - osm-bright sprite atlas   (GitHub Pages, openmaptiles)
#   - glyph PBFs                (bundled with tileserver-gl-styles via npm)
#
# Safe to re-run.

set -euo pipefail

readonly REPO_DIR="${REPO_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
readonly SRC_DIR="${REPO_DIR}/services/prepperpi-tiles"
readonly PREFIX="${PREFIX:-/opt/prepperpi}"
readonly DST_DIR="${PREFIX}/services/prepperpi-tiles"
readonly CONF_DIR=/etc/prepperpi/tileserver
readonly STATE_DIR=/var/lib/prepperpi/maps
readonly CACHE_DIR="${STATE_DIR}/cache"
readonly MAPS_DIR=/srv/prepperpi/maps
readonly LANDING_DIR="${PREFIX}/web/landing"
readonly SERVICE_USER="${SERVICE_USER:-prepperpi}"
readonly SERVICE_GROUP="${SERVICE_GROUP:-prepperpi}"
readonly ADMIN_USER="${ADMIN_USER:-prepperpi-admin}"

# ---------- pinned upstream versions ----------
# Bump these together with the matching SHA256 of the tarball / file.
# Re-running setup.sh after a bump fetches the new asset; the cached
# old one is left in place harmlessly.
readonly TILESERVER_GL_LIGHT_VERSION="5.0.0"
readonly MAPLIBRE_GL_VERSION="4.7.1"
readonly OSM_BRIGHT_VERSION="v1.20"
# Glyph fonts come from tileserver-gl-styles (a transitive dep of
# tileserver-gl-light), not from openmaptiles/fonts directly — see
# install_glyph_fonts() for why.

log() { printf '[prepperpi-tiles/setup] %s\n' "$*"; }
warn() { printf '[prepperpi-tiles/setup] WARN: %s\n' "$*" >&2; }
die() { printf '[prepperpi-tiles/setup] FATAL: %s\n' "$*" >&2; exit 1; }

require_root() {
  if [[ $EUID -ne 0 ]]; then
    echo "setup.sh must be run as root" >&2
    exit 1
  fi
}

install_packages() {
  log "installing apt packages (nodejs, npm, jq, ca-certificates)"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y --no-install-recommends nodejs npm jq ca-certificates curl
}

ensure_dirs() {
  log "creating directories"
  install -d -m 0755 "$DST_DIR"
  install -d -m 0755 "$CONF_DIR"
  install -d -m 0755 "${CONF_DIR}/styles"
  install -d -m 0755 "${CONF_DIR}/styles/osm-bright"
  install -d -m 0755 "${CONF_DIR}/fonts"
  install -d -m 0755 "${CONF_DIR}/sprites"
  install -d -m 0755 "${CONF_DIR}/mbtiles"
  install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$STATE_DIR"
  install -d -m 0755 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$CACHE_DIR"
  # /srv/prepperpi/maps is owned by the admin user so the admin
  # console can delete regions directly without a privileged worker.
  # The tileserver only needs read access (uid prepperpi can read a
  # 0755 dir owned by prepperpi-admin).
  install -d -m 0755 -o "$ADMIN_USER" -g "$SERVICE_GROUP" "$MAPS_DIR" 2>/dev/null \
    || install -d -m 0755 "$MAPS_DIR"
}

# Symlink /srv/prepperpi/maps under the tileserver mbtiles path so
# config.json's `mbtiles/<region>.mbtiles` paths resolve to the user's
# actual installed regions. We keep them out of /etc/prepperpi/tileserver
# so admin / fs operations on the maps dir don't churn config.
symlink_mbtiles() {
  local link="${CONF_DIR}/mbtiles"
  if [[ -L "$link" ]]; then
    return
  fi
  if [[ -d "$link" ]]; then
    rmdir "$link" 2>/dev/null || die "could not replace ${link} with a symlink to ${MAPS_DIR}"
  fi
  ln -s "$MAPS_DIR" "$link"
}

install_tileserver_gl_light() {
  log "installing tileserver-gl-light@${TILESERVER_GL_LIGHT_VERSION} via npm"
  # Local install into DST_DIR/node_modules so the dependency tree is
  # owned by us, version is pinned, and an apt-level npm upgrade can't
  # silently mutate the runtime.
  pushd "$DST_DIR" >/dev/null
  if [[ ! -f package.json ]]; then
    cat > package.json <<EOF
{
  "name": "prepperpi-tiles",
  "private": true,
  "dependencies": {
    "tileserver-gl-light": "${TILESERVER_GL_LIGHT_VERSION}"
  }
}
EOF
  fi
  # --no-fund / --no-audit silence interactive output; --omit=dev
  # avoids pulling test deps. npm respects an existing package-lock
  # if present, so re-runs are deterministic.
  npm install --no-fund --no-audit --omit=dev
  popd >/dev/null
}

# Download a URL into the cache, verifying via Content-Length only —
# we explicitly do not verify SHA256s here because (a) GitHub release
# artifacts are TLS-fetched, (b) the threat model is "user bricks Pi",
# not "supply-chain attacker compromises maptiler". Curl's --fail
# turns 4xx/5xx into a non-zero exit, and we re-fetch on size 0.
fetch_to_cache() {
  local url="$1" dest="$2"
  if [[ -s "$dest" ]]; then
    log "  cached: $(basename "$dest")"
    return 0
  fi
  log "  fetching: $(basename "$dest")"
  curl --fail --silent --show-error --location --output "$dest" "$url" \
    || die "could not download $url"
}

install_maplibre_client() {
  log "installing MapLibre GL JS @${MAPLIBRE_GL_VERSION} + static client"
  local js="${CACHE_DIR}/maplibre-gl-${MAPLIBRE_GL_VERSION}.js"
  local css="${CACHE_DIR}/maplibre-gl-${MAPLIBRE_GL_VERSION}.css"
  fetch_to_cache "https://unpkg.com/maplibre-gl@${MAPLIBRE_GL_VERSION}/dist/maplibre-gl.js"  "$js"
  fetch_to_cache "https://unpkg.com/maplibre-gl@${MAPLIBRE_GL_VERSION}/dist/maplibre-gl.css" "$css"

  local client_dst="${DST_DIR}/client"
  install -d -m 0755 "$client_dst"
  install -m 0644 "${SRC_DIR}/client/index.html" "${client_dst}/index.html"
  install -m 0644 "${SRC_DIR}/client/map.js"     "${client_dst}/map.js"
  install -m 0644 "${SRC_DIR}/client/map.css"    "${client_dst}/map.css"
  install -m 0644 "$js"                          "${client_dst}/maplibre-gl.js"
  install -m 0644 "$css"                         "${client_dst}/maplibre-gl.css"
}

install_osm_bright_style() {
  log "installing osm-bright style @${OSM_BRIGHT_VERSION}"
  local tarball="${CACHE_DIR}/osm-bright-${OSM_BRIGHT_VERSION}.tar.gz"
  local extract_dir="${CACHE_DIR}/osm-bright-${OSM_BRIGHT_VERSION}"
  fetch_to_cache \
    "https://github.com/openmaptiles/osm-bright-gl-style/archive/refs/tags/${OSM_BRIGHT_VERSION}.tar.gz" \
    "$tarball"
  rm -rf "$extract_dir"
  install -d -m 0755 "$extract_dir"
  tar -xzf "$tarball" -C "$extract_dir" --strip-components=1

  # The release ships style.json (single-source openmaptiles) and SVG
  # icon sources. We ship style.json verbatim as the *template* — the
  # reindex script transforms it into our composite style at runtime.
  install -m 0644 "${extract_dir}/style.json" "${CONF_DIR}/styles/osm-bright/style.template.json"

  # Sprites: starting with osm-bright v1.x the GitHub release no
  # longer ships pre-built sprite atlases (only SVG sources under
  # icons/). Pre-built sprites are published to GitHub Pages instead;
  # we fetch them at install time. This avoids carrying spreet or any
  # other sprite-builder dependency, and keeps the install offline-
  # capable on re-runs once the cache is warm.
  #
  # Layout: tileserver-gl-light v5 resolves sprite=<id> in style.json
  # to <paths.sprites>/<id>.json on disk (NOT a subdir). So we drop
  # the four atlases at sprites/osm-bright.{json,png} and
  # sprites/osm-bright@2x.{json,png}.
  local sprites_dst="${CONF_DIR}/sprites"
  install -d -m 0755 "$sprites_dst"
  # rm -rf so we can replace either a stale subdir (older layout) or
  # stale top-level atlas files when bumping OSM_BRIGHT_VERSION.
  rm -rf "${sprites_dst}/osm-bright" "${sprites_dst}/osm-bright."* "${sprites_dst}/osm-bright@"*
  local f local_name
  for f in sprite.png sprite.json sprite@2x.png sprite@2x.json; do
    fetch_to_cache \
      "https://openmaptiles.github.io/osm-bright-gl-style/${f}" \
      "${CACHE_DIR}/osm-bright-sprite-${OSM_BRIGHT_VERSION}-${f//[@\/]/_}"
    # Rename: sprite.json → osm-bright.json, sprite@2x.png → osm-bright@2x.png, etc.
    local_name="osm-bright${f#sprite}"
    install -m 0644 "${CACHE_DIR}/osm-bright-sprite-${OSM_BRIGHT_VERSION}-${f//[@\/]/_}" \
                    "${sprites_dst}/${local_name}"
  done
}

install_glyph_fonts() {
  log "installing glyph fontstacks (from tileserver-gl-styles bundle)"
  # tileserver-gl-light's npm package brings tileserver-gl-styles in
  # transitively; that package ships pre-built PBF glyph atlases for
  # the OSM-Bright fontstack family. Specifically Noto Sans Regular —
  # which is OSM-Bright's universal fallback. Multi-fontstack
  # references like ["Metropolis Light", "Noto Sans Regular"] fall
  # through to Noto Sans Regular when the first isn't installed, so
  # ONE fontstack on disk is sufficient for the style to render
  # cleanly without any "missing glyphs" warnings.
  #
  # We don't pull from openmaptiles/fonts — its v2.0 release pivoted
  # to TTF source files + a generation script (no pre-built PBFs),
  # which would mean carrying a full Node-based glyph builder here.
  # The bundled tileserver-gl-styles assets are the path of least
  # resistance and exactly what upstream tileserver-gl uses.
  local fonts_dst="${CONF_DIR}/fonts"
  local bundled_fonts="${DST_DIR}/node_modules/tileserver-gl-styles/fonts"
  if [[ ! -d "$bundled_fonts" ]]; then
    warn "tileserver-gl-styles fonts dir not found at ${bundled_fonts}; glyphs will 404"
    return 0
  fi
  rm -rf "${fonts_dst:?}"/*
  # Copy each fontstack dir verbatim. The names ("Noto Sans Regular")
  # contain a space, which is what the style.json's text-font field
  # references; the tileserver URL-encodes them on lookup.
  find "$bundled_fonts" -mindepth 1 -maxdepth 1 -type d -print0 \
    | while IFS= read -r -d '' d; do
        cp -r "$d" "${fonts_dst}/"
      done
}

install_scripts_and_units() {
  log "installing reindex scripts and systemd units"
  install -m 0755 "${SRC_DIR}/build-tiles-index.sh" "${DST_DIR}/build-tiles-index.sh"
  install -m 0755 "${SRC_DIR}/build-tiles-index.py" "${DST_DIR}/build-tiles-index.py"
  install -m 0644 "${SRC_DIR}/tiles_indexer.py"     "${DST_DIR}/tiles_indexer.py"

  install -m 0644 "${SRC_DIR}/prepperpi-tiles.service"           /etc/systemd/system/prepperpi-tiles.service
  install -m 0644 "${SRC_DIR}/prepperpi-tiles-reindex.service"   /etc/systemd/system/prepperpi-tiles-reindex.service
  install -m 0644 "${SRC_DIR}/prepperpi-tiles-reindex.path"      /etc/systemd/system/prepperpi-tiles-reindex.path
}

initial_index() {
  log "running initial maps index"
  "${DST_DIR}/build-tiles-index.sh" || warn "initial index failed (will retry on first MBTiles event)"
}

enable_units() {
  log "enabling prepperpi-tiles units"
  systemctl daemon-reload
  systemctl enable prepperpi-tiles.service
  systemctl enable prepperpi-tiles-reindex.path
}

restart_units() {
  log "restarting prepperpi-tiles units"
  systemctl restart prepperpi-tiles.service
  systemctl restart prepperpi-tiles-reindex.path
}

main() {
  require_root
  install_packages
  ensure_dirs
  symlink_mbtiles
  install_tileserver_gl_light
  install_maplibre_client
  install_osm_bright_style
  install_glyph_fonts
  install_scripts_and_units
  initial_index
  enable_units
  restart_units
  log "done. tileserver-gl-light is active on 127.0.0.1:8083; client at /maps/."
}

main "$@"
