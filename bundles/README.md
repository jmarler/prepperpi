# bundles/

Content-bundle manifests in YAML. Each manifest lists the source URL, checksum, license, on-disk size, and install target for one item, grouped into named bundles (`starter.yaml`, `premium.yaml`, `medical-only.yaml`, `education-only.yaml`).

The updater consumes these; nothing in this directory is content itself.
