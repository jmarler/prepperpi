# Creating a PrepperPi bundle

A "bundle" is a YAML manifest listing curated content (Kiwix ZIMs + map
regions + optional static files) that the admin console can install in
one click. PrepperPi reads bundles from a list of HTTP(S) source URLs
plus a baked-in offline fallback.

You don't need to fork PrepperPi to ship a bundle. Host your own
`index.json` + manifest files anywhere reachable over HTTPS, and your
users can point their PrepperPi at it from the admin console.

> **Authoritative spec.** This doc is kept in sync with the
> [`prepperpi-bundles` README](https://github.com/jmarler/prepperpi-bundles#readme).
> Either copy is up-to-date; pick whichever is closer to where you're
> reading.

## Minimal setup

Layout your bundle source like this:

```
your-bundles/
├── index.json                 # entry point
└── manifests/
    ├── starter.yaml
    └── advanced.yaml
```

### `index.json`

```json
{
  "version": 1,
  "name": "Alice's Emergency Bundles",
  "manifests": [
    {"id": "starter",  "url": "manifests/starter.yaml"},
    {"id": "advanced", "url": "manifests/advanced.yaml"}
  ]
}
```

`url` is resolved relative to the index URL, so the appliance fetches
`https://your-host/your-bundles/manifests/starter.yaml` if your index is
at `https://your-host/your-bundles/index.json`.

`id` must be unique within a single source. Across sources, the
appliance namespaces with `<source-id>:<bundle-id>` so the same id can
appear in multiple sources without conflict.

### Manifest YAML

```yaml
id: starter                              # required, [a-z0-9-]+
name: Starter                            # required, user-facing
description: |
  A curated kit covering medical, repair, and survival fundamentals.
  Around 28 GB on disk after install.
license_notes: |                         # optional but encouraged
  Includes CC BY-NC-SA content (iFixit). Personal and educational use
  only — commercial redistribution is not permitted by the upstream.

items:
  - kind: zim
    book_id: wikipedia_en_medicine_maxi

  - kind: map_region
    region_id: US

  - kind: static
    url: https://example.com/survival.pdf
    sha256: 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
    size_bytes: 18000000
    install_to: static/survival.pdf
```

## Item kinds

### `zim`

```yaml
- kind: zim
  book_id: wikipedia_en_medicine_maxi
```

`book_id` is matched as a prefix against Kiwix book names, picking the
newest by `updated`. So `wikipedia_en_medicine_maxi` matches
`wikipedia_en_medicine_maxi_2024-04`, `wikipedia_en_medicine_maxi_2024-05`,
etc., and resolves to the freshest one in the appliance's cached Kiwix
catalog at install time. **Sizes and SHA-256 are NOT in the manifest** —
they're resolved from the live catalog. This keeps manifests stable
across upstream Kiwix updates.

The appliance prompts the user to refresh its Kiwix catalog if no
matching book is found. `Refresh catalog` is on the
[Content page](/admin/catalog).

### `map_region`

```yaml
- kind: map_region
  region_id: US
```

`region_id` is an ISO 3166-1 alpha-2 country code from the appliance's
shipped map catalog (`prepperpi-tiles/regions.json`). The downloader
runs the existing `pmtiles extract` worker, which streams just that
region's tiles from a planet PMTiles host. After extraction, a sidecar
JSON (`<region>.source.json`) records the source URL + ETag /
Last-Modified — the update notifier later HEADs the source and
compares to detect staleness.

### `static`

```yaml
- kind: static
  url: https://archive.org/download/.../file.pdf
  sha256: 0123...
  size_bytes: 18000000
  install_to: static/file.pdf
```

All four fields required.

- **`url`** must be HTTP(S). HTTPS is strongly recommended; some hosts
  (archive.org) serve HTTP-only mirrors that are still safe via the
  SHA-256 verification.
- **`sha256`** is a 64-character lowercase hex digest of the file's
  bytes. Compute with `sha256sum file.pdf` or
  `curl -fsSL <url> | sha256sum`.
- **`size_bytes`** is advisory but used for the pre-install size check.
- **`install_to`** is a path relative to `/srv/prepperpi/`. Allowed
  prefixes: `static/`, `zim/static/`, `user-content/`. Path traversal
  (`..`) is rejected by the schema validator.

## Submitting to the official bundles

PRs against [`prepperpi-bundles`](https://github.com/jmarler/prepperpi-bundles)
are welcome — open an issue first to discuss scope. The official
bundles aim to be:

- Freely redistributable (license-checked).
- Narrow in theme; not heavily overlapping with an existing bundle.
- Reasonably-sized (low-tens of GB for themed bundles, up to a few
  hundred GB for a full-comprehensive bundle).

If your bundle is niche or has license caveats that don't fit the
"official" criteria, host it yourself — that's the whole point of the
multi-source design.

## License caveats

If your bundle includes content under a non-permissive license, surface
it in `license_notes` so the appliance can show it to the user before
they install. Quick reference:

| Content | License | Notes |
|---|---|---|
| Wikipedia, WikiMed, StackExchange | CC BY-SA | Permissive; share-alike. |
| WikiHow, Khan Academy, iFixit | CC BY-NC-SA | **Non-commercial only.** |
| TED Talks | CC BY-NC-ND | NC + no derivatives. |
| Project Gutenberg, FEMA, US gov | Public domain | Anything goes. |

A hobbyist Pi running CC-BY-NC content is fine; selling preloaded SD
cards with that content is **not** permitted by the upstream license.
Surface this clearly so users don't accidentally violate it.

## Validating a manifest

The appliance JSON-schema-validates manifests at install time and
shows specific errors for each problem. To pre-flight locally, use the
standalone validator that ships in the
[`prepperpi-bundles`](https://github.com/jmarler/prepperpi-bundles)
repo under `tools/bundles-validate`:

```bash
git clone https://github.com/jmarler/prepperpi-bundles
cd prepperpi-bundles
pip install pyyaml      # only stdlib + pyyaml required

python3 tools/bundles-validate path/to/your/manifests/
python3 tools/bundles-validate \
    --catalog kiwix-catalog.json \
    --regions regions.json \
    path/to/your/manifests/
```

`--catalog` and `--regions` take JSON snapshots of the Kiwix catalog
and `regions.json` and additionally check that every `book_id` and
`region_id` actually resolves.

The same script is wrapped by a ready-to-copy GitHub Action workflow at
`.github/workflows/validate.yml` in that repo — fork it (or copy the
two files) into your own bundle repository and PRs against `manifests/`
will be schema-checked automatically.
