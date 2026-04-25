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
#   - go-pmtiles binary         (GitHub releases — used by E3-S2 downloader)
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
readonly PROTOMAPS_THEMES_BASE_VERSION="4.5.0"
readonly PROTOMAPS_SPRITE_VERSION="v4"
readonly GO_PMTILES_VERSION="1.30.2"
# Glyph fonts come from tileserver-gl-styles (a transitive dep of
# tileserver-gl-light), not from openmaptiles/fonts directly — see
# install_glyph_fonts() for why.
#
# We use Protomaps' basemap style (vector tiles, MIT-licensed) instead
# of OSM-Bright. The matching planet PMTiles source is
# build.protomaps.com — mapterhorn.com (initially scoped) ships WebP
# raster tiles which can't render against any vector style. See
# extract-region.sh for the date-walk-back logic that picks the latest
# available daily build at extract time.

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
  log "installing tileserver-gl-light@${TILESERVER_GL_LIGHT_VERSION} + protomaps-themes-base@${PROTOMAPS_THEMES_BASE_VERSION} via npm"
  # Local install into DST_DIR/node_modules so the dependency tree is
  # owned by us, versions are pinned, and an apt-level npm upgrade
  # can't silently mutate the runtime. We always (re)write package.json
  # so dependency bumps in this setup.sh take effect on re-run.
  pushd "$DST_DIR" >/dev/null
  cat > package.json <<EOF
{
  "name": "prepperpi-tiles",
  "private": true,
  "dependencies": {
    "tileserver-gl-light": "${TILESERVER_GL_LIGHT_VERSION}",
    "protomaps-themes-base": "${PROTOMAPS_THEMES_BASE_VERSION}"
  }
}
EOF
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

install_protomaps_style() {
  log "generating protomaps style template + fetching sprite atlas"
  install -d -m 0755 "${CONF_DIR}/styles/protomaps"
  install -d -m 0755 "${CONF_DIR}/sprites"

  # Render the style.template.json from protomaps-themes-base. The
  # resulting template uses a single placeholder source named "protomaps"
  # plus all the basemap layers (light theme). The reindex script's
  # composite builder later rewrites sources/layers per installed region.
  install -m 0755 "${SRC_DIR}/build-protomaps-style.js" "${DST_DIR}/build-protomaps-style.js"
  node "${DST_DIR}/build-protomaps-style.js" \
       > "${CONF_DIR}/styles/protomaps/style.template.json"

  # Sprite atlas. Protomaps publishes pre-built sprites for each theme
  # at protomaps.github.io/basemaps-assets/sprites/<v>/. Same layout as
  # OSM-Bright's GitHub Pages publish; we fetch the four files we need.
  rm -rf "${CONF_DIR}/sprites/protomaps" "${CONF_DIR}/sprites/protomaps."* "${CONF_DIR}/sprites/protomaps@"*
  local f local_name
  for f in light.png light.json light@2x.png light@2x.json; do
    fetch_to_cache \
      "https://protomaps.github.io/basemaps-assets/sprites/${PROTOMAPS_SPRITE_VERSION}/${f}" \
      "${CACHE_DIR}/protomaps-sprite-${PROTOMAPS_SPRITE_VERSION}-${f//[@\/]/_}"
    # Rename: light.json → protomaps.json, light@2x.png → protomaps@2x.png, etc.
    local_name="protomaps${f#light}"
    install -m 0644 "${CACHE_DIR}/protomaps-sprite-${PROTOMAPS_SPRITE_VERSION}-${f//[@\/]/_}" \
                    "${CONF_DIR}/sprites/${local_name}"
  done

  # Drop the old osm-bright assets if present from a prior install.
  rm -rf "${CONF_DIR}/styles/osm-bright" \
         "${CONF_DIR}/sprites/osm-bright" \
         "${CONF_DIR}/sprites/osm-bright."* "${CONF_DIR}/sprites/osm-bright@"*
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

install_pmtiles_binary() {
  log "installing go-pmtiles@${GO_PMTILES_VERSION} (used by E3-S2 region downloader)"
  # Pi 4B / Pi 5 are aarch64. The host arch detection is intentionally
  # narrow — we only ship official Pi targets. arm32 / x86_64 dev
  # machines aren't supported.
  local arch
  arch=$(uname -m)
  if [[ "$arch" != "aarch64" && "$arch" != "arm64" ]]; then
    warn "unsupported host arch ${arch}; pmtiles binary will not be installed"
    return 0
  fi

  local tarball="${CACHE_DIR}/go-pmtiles-${GO_PMTILES_VERSION}.tar.gz"
  fetch_to_cache \
    "https://github.com/protomaps/go-pmtiles/releases/download/v${GO_PMTILES_VERSION}/go-pmtiles_${GO_PMTILES_VERSION}_Linux_arm64.tar.gz" \
    "$tarball"

  install -d -m 0755 "${DST_DIR}/bin"
  # The tarball contains the `pmtiles` binary at the top level. We
  # extract just that entry to keep the deploy tree tidy.
  tar -xzf "$tarball" -C "${DST_DIR}/bin" pmtiles
  chmod 0755 "${DST_DIR}/bin/pmtiles"
}

install_scripts_and_units() {
  log "installing reindex + worker scripts and systemd units"
  install -m 0755 "${SRC_DIR}/build-tiles-index.sh"   "${DST_DIR}/build-tiles-index.sh"
  install -m 0755 "${SRC_DIR}/build-tiles-index.py"   "${DST_DIR}/build-tiles-index.py"
  install -m 0644 "${SRC_DIR}/tiles_indexer.py"       "${DST_DIR}/tiles_indexer.py"
  install -m 0755 "${SRC_DIR}/extract-region.sh"      "${DST_DIR}/extract-region.sh"
  install -m 0644 "${SRC_DIR}/regions.json"           "${DST_DIR}/regions.json"

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
  install_protomaps_style
  install_glyph_fonts
  install_pmtiles_binary
  install_scripts_and_units
  initial_index
  enable_units
  restart_units
  log "done. tileserver-gl-light is active on 127.0.0.1:8083; client at /maps/."
}

main "$@"
