# prepperpi-kiwix

Serves ZIM files (Kiwix/Wikipedia archives) dropped into
`/srv/prepperpi/zim/` as tiles on the landing page and as a browsable
library behind `/library/`.

## Moving parts

| Unit                              | Type     | Role                                                            |
| --------------------------------- | -------- | --------------------------------------------------------------- |
| `prepperpi-kiwix.service`         | simple   | Runs `kiwix-serve` on `127.0.0.1:8088` with urlRoot `/library`. |
| `prepperpi-kiwix-reindex.path`    | path     | Watches `/srv/prepperpi/zim` for changes (ctime on the dir).    |
| `prepperpi-kiwix-reindex.service` | oneshot  | Rebuilds `library.xml` + `_library.html`, restarts kiwix-serve. |

Caddy (from `prepperpi-web`) reverse-proxies `/library/*` to the local
kiwix-serve, and processes the landing page as a Go template that
includes `_library.html` — so the tiles update as soon as the oneshot
finishes.

## Data flow

```
  ZIM dropped in /srv/prepperpi/zim/
            │
            ▼  (systemd.path: ctime change)
  prepperpi-kiwix-reindex.service (oneshot, root)
            │
            ├─► kiwix-manage add → /var/lib/prepperpi/library.xml
            ├─► render tiles    → /opt/prepperpi/web/landing/_library.html
            └─► systemctl restart prepperpi-kiwix.service
```

## Paths

- `/srv/prepperpi/zim/` — drop ZIMs here.
- `/var/lib/prepperpi/library.xml` — kiwix-serve's library file.
- `/opt/prepperpi/web/landing/_library.html` — Caddy template fragment
  included by `index.html`.
- `/opt/prepperpi/services/prepperpi-kiwix/build-library-index.sh` —
  the reindex script.

## Manual reindex

```
sudo systemctl start prepperpi-kiwix-reindex.service
```

## Debugging

```
journalctl -u prepperpi-kiwix.service -f
journalctl -u prepperpi-kiwix-reindex.service -n 100
curl -s http://127.0.0.1:8088/library/ | head
```
