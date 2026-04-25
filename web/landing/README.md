# web/landing

PrepperPi's captive-portal landing page. Mostly HTML + CSS, with one small enhancement script (`dashboard.js`) that delivers live toast notifications and in-place tile refresh when content changes. The page works without JavaScript — every tile region is rendered server-side at request time via Caddy's `templates` directive, so a no-JS browser sees the same content with a manual refresh.

## Files

| File | Owner | Purpose |
|---|---|---|
| `index.html` | this service | Outer page. Contains `{{include "/_<frag>.html"}}` calls wrapped in `<div data-fragment="<name>">` so the dashboard script has stable DOM targets. |
| `style.css` | this service | Site styles, including `.toast-container` / `.toast` and `[data-fragment] { display: contents }` so wrappers don't disturb the CSS grid. |
| `dashboard.js` | this service | Polls `/_events.json` every 2 s, surfaces new events as top-center toasts, DOM-swaps the affected fragment regions in place. Pauses polling via the Page Visibility API. |
| `_library.html` | `prepperpi-kiwix` | One tile per ZIM. |
| `_library_search.html` | `prepperpi-kiwix` | Cross-library search form (or empty when no books). |
| `_usb.html` | `prepperpi-usb` | One tile per mounted USB volume, or empty-state. |
| `_events.json` | `prepperpi-events` | Event log polled by `dashboard.js`. |

The static parts (`index.html`, `style.css`, `dashboard.js`) are deployed by `services/prepperpi-web/setup.sh`. The `_*.html` / `_events.json` fragments are written by the services that own each surface; this directory just hosts them so Caddy can serve them with a single `root *` directive.

## Design constraints

- **Works without JavaScript** (E1-S4 AC-3). Progressive enhancement only — `dashboard.js` is loaded with `defer`, the toast container is empty until JS adds children, and every tile is server-side-rendered through Caddy's templates engine.
- **Renders at 320 px width** (E1-S4 AC-4). Mobile-first CSS: single-column on narrow screens, 2-column at 32 rem, 4-column at 60 rem.
- **No build step.** Plain HTML, plain CSS, vanilla ES2017 JS. The Pi doesn't need Node/npm/yarn/anything at runtime.

## Live updates

```
  reindex / mount / unmount script
              │
              ▼  emit-event.py <type> <message>
  /opt/prepperpi/web/landing/_events.json     ← ring buffer
              │
              ▼  Caddy file_server
  GET /_events.json   (every 2 s, paused when tab hidden)
              │
  ┌───────────▼─────────────────────────────┐
  │ dashboard.js                            │
  │  ─ shows toast for each new event       │
  │  ─ for each event type, fetches the     │
  │    matching /_<frag>.html and DOM-swaps │
  │    the [data-fragment="<name>"] region  │
  └─────────────────────────────────────────┘
```

`FRAGMENT_FOR_EVENT` in `dashboard.js` is the event-to-fragment map — extending it for a new event type is one line plus a new fragment.

## Editing

Edit any of the static files, rerun `sudo services/prepperpi-web/setup.sh`, and `sudo systemctl restart prepperpi-web`. To test the live-update path locally without plugging anything in:

```bash
sudo /opt/prepperpi/services/prepperpi-events/emit-event.py library_changed "Test toast"
```

A toast should appear on any open landing-page tab within 2 s.
