# Update notifier

PrepperPi tracks installed content (ZIMs, map regions, bundle manifests, and
bundle static files) and surfaces drift against upstream on the admin
console's **Updates** page (`/admin/updates`).

## What's checked

| Kind | Drift signal |
|---|---|
| ZIM | Installed filename's date suffix vs. the latest matching catalog entry's `name` (Kiwix's date-suffixed convention; we keep the same `book_id` prefix). |
| Map region | The `<region_id>.source.json` sidecar (written by `extract-region.sh`) holds the source URL + `ETag` + `Last-Modified` at extract time. The check HEADs the source URL and compares. |
| Bundle manifest | sha256 of the cached manifest body vs. sha256 of a freshly-fetched body from the source's `index.json`. |
| Static (inside a bundle) | sha256 of the on-disk file at `install_to` vs. the manifest's `sha256` field. |

## When the check runs

Three triggers, all hitting the same code path:

1. **Boot + every 6 h** — `prepperpi-updates-check.timer`.
2. **Uplink-up** — `/etc/NetworkManager/dispatcher.d/90-prepperpi-updates`
   fires `systemctl start --no-block prepperpi-updates-check.service` when
   any interface comes `up`. The check internally bails fast with a "no
   uplink" snapshot if the Pi has no Ethernet route.
3. **Manual** — the **Check now** button on the Updates page runs the check
   in-process inside the FastAPI worker.

## Snapshot file

The check writes `/var/lib/prepperpi/updates/state.json`:

```json
{
  "checked_at": "2026-04-26T12:00:00Z",
  "uplink": "ethernet",
  "errors": [],
  "stale_count": 3,
  "items": [
    {
      "kind": "zim",
      "id": "wikipedia_en_all",
      "title": "Wikipedia (en)",
      "installed": "2026-03",
      "available": "2026-04",
      "available_url": "https://download.kiwix.org/.../wikipedia_en_all_2026-04.zim.meta4",
      "available_name": "wikipedia_en_all_2026-04",
      "size_delta_bytes": 12345,
      "status": "stale"
    }
  ]
}
```

The page renders from this file; it does no live fetching at render time.
That keeps the page fast and the badge accurate even when the Pi is
offline.

## Apply paths

| Kind | Behavior |
|---|---|
| ZIM (default) | **Side-by-side**: pre-flight HEAD the new URL, check free space against advertised content-length, queue the new ZIM via aria2 alongside the old one. The Content / Catalog page surfaces both; user removes the old one when ready. |
| ZIM (low-disk) | **Delete-then-update**: when free < new size, the page exposes a "Delete old, then update" button that unlinks the old `.zim`, then queues the new one. If the download fails after the delete, the old version is gone — there is no automatic rollback. The confirm dialog states this explicitly. |
| Map region | Re-runs `extract-region.sh` via the existing bundle queue + drainer. The extract writes `<region>.pmtiles.tmp` and atomic-renames; the source-sidecar JSON is rewritten on success. |
| Bundle manifest | Re-fetches the source's `index.json` + manifests via `_refresh_remote_sources`, which writes the new bodies atomically. |
| Static | Streams to `<install_to>.new`, computes sha256 on the fly, atomic-renames onto the live path on hash match. Failed downloads delete the partial and leave the old file untouched. |

## Pinning

A pinned item never appears in the "stale" set; the page shows it with a 📌
badge. Pin handles per kind:

- **ZIM**: the installed version date (e.g. `2026-03`).
- **Map region**: the sidecar's `etag` + `last_modified` pair.
- **Bundle**: sha256 of the cached manifest body.
- **Static**: sha256 of the on-disk file.

The **Pin** button always pins to the *currently installed* state — there
is no UI for picking an arbitrary version. Unpin restores normal drift
detection.

Pins live at `/var/lib/prepperpi/updates/pins.json`.

## Layout

```
/var/lib/prepperpi/updates/
├── state.json    # latest detection snapshot (atomic write+rename)
└── pins.json     # per-item pin handles (atomic write+rename)
```

Both files are owned `prepperpi-admin:prepperpi-admin` mode 0644. The
`prepperpi-admin` systemd unit grants the directory via `ReadWritePaths`.

## Files at a glance

| Path | Role |
|---|---|
| `services/prepperpi-admin/app/updates.py` | Pure detectors + HTTP HEAD wrapper. |
| `services/prepperpi-admin/app/updates_state.py` | Collectors (installed-state from disk + caches), snapshot writer. |
| `services/prepperpi-admin/app/updates_apply.py` | Per-kind apply implementations. |
| `services/prepperpi-admin/prepperpi-updates-check` | Python entry point invoked by the timer + dispatcher. |
| `services/prepperpi-admin/prepperpi-updates-check.{service,timer}` | systemd units. |
| `services/prepperpi-admin/dispatcher.d-prepperpi-updates` | NetworkManager dispatcher hook installed at `/etc/NetworkManager/dispatcher.d/90-prepperpi-updates`. |
| `services/prepperpi-admin/app/templates/updates.html` | Dashboard template. |
