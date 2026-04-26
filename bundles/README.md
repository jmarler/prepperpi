# bundles/

Content-bundle definitions for the admin console's one-click install flow.

```
.
├── builtin/                  # Baked into the SD image; offline-capable.
│   ├── index.json
│   └── manifests/
│       ├── starter.yaml
│       ├── complete.yaml
│       ├── medical.yaml
│       └── education.yaml
└── sources.json              # Default source list — installer copies
                              # this to /etc/prepperpi/bundles/.
```

## Layout

- **`builtin/`** is the offline fallback. The image installer copies the
  current contents of [`prepperpi-bundles`](https://github.com/jmarler/prepperpi-bundles)
  here at build time so a freshly-flashed Pi can install the official
  bundles without reaching the network. The admin console's "Refresh
  bundle sources" action overlays the latest from each configured source
  URL on top of these baked copies.
- **`sources.json`** is the default sources list. The installer copies
  it to `/etc/prepperpi/bundles/sources.json` on first install only;
  subsequent installs leave the on-disk copy alone so admin-added
  community sources persist.

## Schema

See [`docs/creating-bundles.md`](../docs/creating-bundles.md) for the
manifest schema and the howto for hosting your own bundles. The
authoritative copy of the spec also lives in the
[`prepperpi-bundles` README](https://github.com/jmarler/prepperpi-bundles#readme);
the two are kept in sync deliberately so contributors landing in either
repo find the full picture.

## What's in this directory is metadata, never content

A manifest is a list of pointers (Kiwix book IDs, map region IDs, plus
URL+SHA-256 for any extra static files). The actual bytes live on Kiwix
mirrors, the planet PMTiles host, and elsewhere. Nothing under `bundles/`
is the downloaded payload.
