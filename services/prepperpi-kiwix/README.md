# prepperpi-kiwix

Serves ZIM files (Kiwix/Wikipedia archives) as tiles on the landing page and as a browsable library behind `/library/`. Discovers ZIMs from two locations:

- `/srv/prepperpi/zim/` — the local-disk library, persistent across reboots.
- `/srv/prepperpi/user-usb/<volume>/...` — USB drives mounted by [`prepperpi-usb`](../prepperpi-usb/), scanned to depth 5. Auto-imported while the drive is plugged in, dropped on yank.

## Moving parts

| Unit                              | Type     | Role                                                            |
| --------------------------------- | -------- | --------------------------------------------------------------- |
| `prepperpi-kiwix.service`         | simple   | Runs `kiwix-serve` on `127.0.0.1:8088` with urlRoot `/library`. |
| `prepperpi-kiwix-reindex.path`    | path     | Watches `/srv/prepperpi/zim` AND `/srv/prepperpi/user-usb` for ctime changes. |
| `prepperpi-kiwix-reindex.service` | oneshot  | Rebuilds `library.xml` + the tile and search fragments, restarts kiwix-serve, emits a `library_changed` event when the set of indexed books changes. |

Caddy (from `prepperpi-web`) reverse-proxies `/library/*` to the local kiwix-serve, and processes the landing page as a Go template that includes `_library.html` and `_library_search.html` — so tiles update as soon as the oneshot finishes.

## Data flow

```
  ZIM dropped in /srv/prepperpi/zim/             USB mounted at /srv/prepperpi/user-usb/<vol>/
            │                                                       │
            └────────────── ctime change on the watched dir ────────┘
                                       │
                                       ▼
  prepperpi-kiwix-reindex.service (oneshot, root)
                                       │
            ├─► kiwix-manage add (USB first, local second)  → /var/lib/prepperpi/library.xml
            ├─► render tile + search fragments              → /opt/prepperpi/web/landing/_library.html
            │                                               → /opt/prepperpi/web/landing/_library_search.html
            ├─► systemctl restart prepperpi-kiwix.service
            └─► emit-event.py library_changed "Library updated · N books"
                                                            (only when the sorted set of book UUIDs differs from the previous run)
```

## Three identifiers per book

`kiwix-tools 3.7` exposes a book under three different names depending on which API surface you're calling. The reindex script and Caddy config both have to honor this, and getting it wrong is a silent 404 in the wrong place. Documented here so the next person who touches this doesn't relearn it the hard way:

| Selector  | Where it appears                                | Format                                      |
| --------- | ----------------------------------------------- | ------------------------------------------- |
| **`id`**  | `library.xml` `id=` attribute; `books.id=<uuid>` on `/library/search` | UUID. The only selector that works reliably for `books.*` filters in 3.7 — `books.name=<name>` returns *No such book* even when the name matches. |
| **`name`** | OPDS catalog metadata; `library.xml` `name=` attribute | Internal slug derived from the ZIM's metadata, e.g. `wikipedia_ab_all`. |
| **slug**   | `/library/content/<slug>/`, `/library/viewer#<slug>` | The `.zim` filename minus the extension, e.g. `wikipedia_ab_all_nopic_2026-04`. **Never surfaced as an XML attribute** — the script derives it from the basename of the `path=` attribute. |

## Duplicate-UUID dedup

`kiwix-manage add` with a duplicate UUID is **last-write-wins** — the existing `<book>` entry's `path=` is rewritten to the second source. The reindex therefore scans **USB first, local-disk second**, so when the same ZIM lives in both places the final `library.xml` points at the local copy. Local doesn't disappear on a USB yank, so the tile keeps working.

The `library_changed` toast fires only when the sorted set of book UUIDs differs from the previous run (state stashed in `/var/lib/prepperpi/last-library-state.txt`) — a USB plug-in that contains no ZIMs doesn't fire a misleading "Library updated" toast.

## Tile storage indicator

Each Library tile says where the ZIM physically lives on the third line:

```
Wikipedia (Abkhazian)
8,276 articles
10M on internal disk          ← from /srv/prepperpi/zim/
```

```
Wikipedia (Abkhazian)
8,276 articles
10M on external USB (32GB)    ← from /srv/prepperpi/user-usb/32GB/
```

Computed at fragment-render time from the resolved abs_path.

## Yank safety

When the USB is unplugged, `prepperpi-usb`'s mount unit's `BindsTo`-cascade lazy-unmounts and `rmdir`s the per-volume mountpoint. That ctime change fires `prepperpi-kiwix-reindex.path` (in addition to `prepperpi-usb-reindex.path`), the kiwix reindex re-scans, drops any USB-resident ZIMs from `library.xml`, and restarts kiwix-serve. The restart is the key step: kiwix-serve `mmap`s ZIM files, and on a yanked device that mmap is the last thing keeping the kernel filesystem alive. Restart releases the fd, the kernel completes the tear-down, and `library.xml` is consistent with reality. Total recovery: ~1 s on the dev Pi 4B.

## Paths

| Path | Purpose |
|---|---|
| `/srv/prepperpi/zim/` | Drop ZIMs here for the local-disk library. |
| `/srv/prepperpi/user-usb/<vol>/...` | USB-resident ZIMs, auto-imported (max depth 5 inside each volume). |
| `/var/lib/prepperpi/library.xml` | kiwix-serve's library file. Rebuilt from scratch each reindex. |
| `/var/lib/prepperpi/last-library-state.txt` | UUID set from previous reindex; powers the change-detection that gates `library_changed` events. |
| `/opt/prepperpi/web/landing/_library.html` | Caddy template fragment with the tile list. |
| `/opt/prepperpi/web/landing/_library_search.html` | Caddy template fragment with the cross-library search form (zero or more hidden `books.id` inputs). |
| `/opt/prepperpi/services/prepperpi-kiwix/build-library-index.sh` | The reindex script. |

## Manual reindex

```
sudo systemctl start prepperpi-kiwix-reindex.service
```

## Debugging

```
journalctl -u prepperpi-kiwix.service -f
journalctl -u prepperpi-kiwix-reindex.service -n 100
curl -s http://127.0.0.1:8088/library/ | head
kiwix-manage /var/lib/prepperpi/library.xml show
```
