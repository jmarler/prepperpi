# prepperpi-events

A tiny shared utility for pushing dashboard events. There is no
daemon: just a Python helper that other services call when something
the user might want to see has happened.

## How it works

```
  reindex / mount script
        │
        ▼  emit-event.py <type> <message>
  /opt/prepperpi/web/landing/_events.json   ← ring buffer, last 500 events
        │
        ▼  Caddy file_server
  GET /_events.json   ← dashboard JS polls this every ~2 s
```

Each event is `{id, ts, type, message}`. The file also carries a
monotonic `version` field equal to the most-recent id; the
dashboard uses that as a "has anything changed since I last polled"
signal so it can short-circuit cheaply when no events have occurred.

## Emitting an event

```bash
/opt/prepperpi/services/prepperpi-events/emit-event.py \
    usb_plugged "USB drive 'BackupDrive' connected"
```

Type strings are free-form, but the dashboard's event-to-fragment
map (`web/landing/dashboard.js`) maps known types to which fragment
regions to refresh after a toast fires. Today:

| type            | refreshed fragments                  |
| --------------- | ------------------------------------ |
| `usb_plugged`   | `usb`, `library`, `library_search`   |
| `usb_unplugged` | `usb`, `library`, `library_search`   |
| `library_changed` | `library`, `library_search`        |
| `boot_complete` | (no fragments — surfaced in admin event log only) |

Unknown event types still surface a toast and trigger a
conservative `library` + `usb` refresh.

## Boot event

`prepperpi-boot-event.service` is a oneshot that fires after `multi-user.target` and emits a `boot_complete` event. Surfaces in the admin Storage page's event log so operators can see "yes, this Pi rebooted at X" without `journalctl`.

## Atomicity

emit-event.py serializes concurrent calls via an `fcntl` advisory
lock on `/run/prepperpi/events.lock`, then writes to a temp file
and `rename(2)`s onto the final path. Concurrent reindex services
emitting at the same moment never corrupt the JSON.

## Why a static file vs a daemon

- No new process to babysit.
- Caddy already serves the landing-page directory; one more file is free.
- 2 s polling × 1 LAN client ≈ trivial load.
- If the load assumption changes, swapping in a long-poll endpoint
  later only affects `dashboard.js` and a tiny new daemon — the
  emitter API stays the same.
