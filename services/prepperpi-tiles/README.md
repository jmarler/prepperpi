# prepperpi-tiles

Offline vector map tile server (E3-S1). [tileserver-gl-light](https://github.com/maptiler/tileserver-gl) serves OpenMapTiles MBTiles from `/srv/prepperpi/maps/`, with a [MapLibre GL JS](https://maplibre.org) client served by Caddy at `/maps/`. Drop a `.mbtiles` into the maps directory and within a few seconds the landing page surfaces it, the live `/maps/` map renders it, and the [`prepperpi-admin`](../prepperpi-admin/) Maps panel lists it for one-click uninstall.

## How it works

```
  /srv/prepperpi/maps/{region}.mbtiles
        │  (path watcher)
        ▼
  prepperpi-tiles-reindex.path
        │  (oneshot)
        ▼
  prepperpi-tiles-reindex.service
        │  reads each MBTiles' metadata table (sqlite),
        │  writes:
        │    /etc/prepperpi/tileserver/config.json
        │    /etc/prepperpi/tileserver/styles/osm-bright/style.json   ← composite
        │    /opt/prepperpi/web/landing/_maps.html                    ← tile fragment
        │    /var/lib/prepperpi/maps/regions.json                     ← admin uses this
        │  emits maps_changed event
        │  restarts prepperpi-tiles.service
        ▼
  prepperpi-tiles.service (tileserver-gl-light, 127.0.0.1:8083)
        │
        │  Caddy: /maps/{styles,data,fonts,sprites}/* → reverse_proxy w/ uri strip_prefix /maps
        │         /maps/                              → static MapLibre client
        ▼
  client browses /maps/ → fetches /maps/styles/osm-bright/style.json
                       → renders pan/zoom map composed of all installed regions
```

The reindex script is the **single writer** of the composite style and the regions JSON; everything else (tileserver, admin, landing page) is a reader. That keeps the data flow one-way and easy to reason about.

## Composite style (AC-3)

OSM-Bright assumes one vector source named `openmaptiles`. To overlay multiple regional MBTiles seamlessly, the reindex script transforms the upstream style template:

- One vector source per region, named `openmaptiles__<region_id>` (double underscore so it can't collide with OSM-Bright's source-layer names like `transportation_name`).
- Every layer that points at `openmaptiles` is duplicated — one copy per region — with `id` suffixed `__<region_id>` and `source` rebound. Layer order is preserved with per-region copies grouped, so MapLibre draws all regions' `water`, then all regions' `landuse`, etc — z-order stays correct across regions.
- The style's `max_bounds` and `center` are unioned across regions so the initial view fits whatever's installed.

Pure `dict → dict` transform; the regression battery lives in [`tests/unit/test_tiles_indexer.py`](../../tests/unit/test_tiles_indexer.py).

## Files

| Path                                  | Role                                              |
| ------------------------------------- | ------------------------------------------------- |
| `tiles_indexer.py`                    | Pure-ish library: MBTiles metadata reader, composite-style builder, tileserver config builder, landing-fragment renderer. Unit-tested. |
| `build-tiles-index.py`                | Orchestrator. Parses CLI args, calls `tiles_indexer`, writes outputs atomically. |
| `build-tiles-index.sh`                | Shell wrapper. Invoked by the reindex unit. Runs the Python orchestrator, restarts the tileserver, emits the dashboard event. |
| `client/index.html`                   | MapLibre app shell + `<noscript>` fallback (AC-5). |
| `client/map.js`                       | MapLibre init. Fails open to "no regions installed" if the style is empty. |
| `client/map.css`                      | Full-viewport map + header styles. |
| `prepperpi-tiles.service`             | Sandboxed systemd unit running tileserver-gl-light on 127.0.0.1:8083. |
| `prepperpi-tiles-reindex.path`        | PathChanged watcher on `/srv/prepperpi/maps`. |
| `prepperpi-tiles-reindex.service`     | Oneshot that runs `build-tiles-index.sh`. |
| `setup.sh`                            | Installer: apt deps, npm install pinned tileserver, asset fetch, units, initial reindex. |

## Trust model

`tileserver-gl-light` runs as the **`prepperpi`** user, with `ProtectSystem=strict`, no namespace creation, and read-only mounts on `/srv/prepperpi/maps`, `/etc/prepperpi/tileserver`, and its own install dir. It binds only on `127.0.0.1:8083`; only Caddy reaches it.

`/srv/prepperpi/maps/` is owned by **`prepperpi-admin`** so the admin console can `unlink()` `.mbtiles` files directly (AC-4) without a privileged worker. The tileserver only needs read access. The reindex unit runs as **root** because it has to write under `/etc/prepperpi/tileserver` and restart the tileserver service — same pattern as the Kiwix reindex.

## Caddy integration

Two route families in [`prepperpi-web/Caddyfile`](../prepperpi-web/Caddyfile):

```caddyfile
@maps_assets path /maps/styles/* /maps/data/* /maps/fonts/* /maps/sprites/*
handle @maps_assets {
  uri strip_prefix /maps
  reverse_proxy 127.0.0.1:8083
}

handle_path /maps/* {
  root * /opt/prepperpi/services/prepperpi-tiles/client
  try_files {path} /index.html
  file_server
}
```

`tileserver-gl-light` is launched with `--public_url=http://prepperpi.home.arpa/maps/` so the URLs it embeds in `style.json` (font/sprite/tile references) match what the client sees through Caddy.

## Pinned upstream versions

Bumped in [`setup.sh`](setup.sh):

| Asset                       | Version | Source                                           |
| --------------------------- | ------- | ------------------------------------------------ |
| `tileserver-gl-light`       | 5.0.0   | `npm` (locally installed under `node_modules/`)  |
| `maplibre-gl` JS + CSS      | 4.7.1   | `https://unpkg.com/maplibre-gl@<v>`              |
| `osm-bright-gl-style`       | v1.10   | github.com/openmaptiles/osm-bright-gl-style/releases |
| `openmaptiles/fonts`        | v2.0    | github.com/openmaptiles/fonts/releases           |

Downloads are cached under `/var/lib/prepperpi/maps/cache/` so a re-run on the dev Pi over SSH doesn't re-download. There's no SHA256 verification — we trust GitHub's TLS and the threat model isn't a maptiler supply-chain attack.

## Routes

Behind Caddy. The tileserver itself runs at `127.0.0.1:8083` and exposes its native URL tree (`/styles/`, `/data/`, `/fonts/`, `/sprites/`); Caddy's `uri strip_prefix /maps` rewrites paths back to that tree on the way through.

| Path                                               | Purpose                                              |
| -------------------------------------------------- | ---------------------------------------------------- |
| `GET /maps/`                                       | MapLibre client app (static).                        |
| `GET /maps/maplibre-gl.{js,css}`                   | Vendored MapLibre bundle (static).                   |
| `GET /maps/map.{js,css}`                           | App init script + styles (static).                   |
| `GET /maps/styles/osm-bright/style.json`           | Composite vector style (proxied to tileserver).      |
| `GET /maps/data/{region}/{z}/{x}/{y}.pbf`          | Vector tiles (proxied to tileserver).                |
| `GET /maps/fonts/{fontstack}/{range}.pbf`          | Glyph stacks (proxied to tileserver).                |
| `GET /maps/sprites/osm-bright[.json,@2x.png,…]`    | Sprite atlas (proxied to tileserver).                |
| `GET /admin/maps`                                  | Region list + delete (E3-S1 AC-4). See [`prepperpi-admin/`](../prepperpi-admin/). |

## Manually applying a region (development)

```bash
# Drop a sample MBTiles into the maps dir; the path watcher fires
# the reindex within ~1 second. Make sure ownership is at least
# readable by the prepperpi user.
sudo install -o prepperpi-admin -g prepperpi -m 0644 \
  ~/Downloads/monaco.mbtiles \
  /srv/prepperpi/maps/monaco.mbtiles

# Force a reindex without touching files
sudo systemctl start prepperpi-tiles-reindex.service

# Watch the indexer + tileserver
journalctl -u prepperpi-tiles-reindex.service -n 30
journalctl -u prepperpi-tiles.service -f
```

## Debugging

```bash
# Hit the tileserver directly (bypassing Caddy)
curl -s 'http://127.0.0.1:8083/styles/osm-bright/style.json' | jq .name

# Hit it through Caddy
curl -sI -H 'Host: prepperpi.home.arpa' http://10.42.0.1/maps/styles/osm-bright/style.json

# What's the reindexer seeing?
sudo /opt/prepperpi/services/prepperpi-tiles/build-tiles-index.sh

# Inspect the regions JSON the admin reads
jq . /var/lib/prepperpi/maps/regions.json
```
