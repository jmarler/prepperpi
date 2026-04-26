#!/usr/bin/env python3
"""build-tiles-index.py — orchestrator for prepperpi-tiles-reindex.

Walks /srv/prepperpi/maps, builds the tileserver config + composite style
+ landing-page fragment + admin regions JSON, writes them atomically,
and exits 0. Any subsequent step (event emit, service restart) is the
shell wrapper's job.

The pure work lives in tiles_indexer.py and has its own unit tests.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

# Resolve the sibling indexer module relative to this script.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tiles_indexer import (    # noqa: E402
    apply_name_overrides,
    build_composite_style,
    build_tileserver_config,
    discover_regions,
    load_catalog_names,
    regions_summary,
    render_landing_fragment,
)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--maps-dir", required=True, type=Path)
    p.add_argument("--style-template", required=True, type=Path)
    p.add_argument("--style-out", required=True, type=Path)
    p.add_argument("--config-out", required=True, type=Path)
    p.add_argument("--fragment-out", required=True, type=Path)
    p.add_argument("--regions-json", required=True, type=Path)
    p.add_argument(
        "--catalog",
        type=Path,
        default=Path(__file__).resolve().parent / "regions.json",
        help="Path to the static country catalog (used to overlay friendly "
             "names onto regions whose .pmtiles metadata says only "
             "'Protomaps Basemap')",
    )
    return p.parse_args()


def write_atomic(path: Path, content: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main() -> int:
    args = parse_args()

    regions = discover_regions(args.maps_dir)
    print(f"[prepperpi-tiles/index] discovered {len(regions)} region(s)", file=sys.stderr)

    name_overrides = load_catalog_names(args.catalog)
    apply_name_overrides(regions, name_overrides)

    config = build_tileserver_config(regions)
    write_atomic(args.config_out, (json.dumps(config, indent=2) + "\n").encode("utf-8"))

    if args.style_template.exists():
        try:
            template = json.loads(args.style_template.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"[prepperpi-tiles/index] WARN: style template unreadable ({exc}); skipping", file=sys.stderr)
            template = None
    else:
        template = None

    if template is not None:
        composite = build_composite_style(template, regions)
        write_atomic(args.style_out, (json.dumps(composite, indent=2) + "\n").encode("utf-8"))
    else:
        # First boot before the asset fetch finished, or template was
        # purged. Don't fail — the tileserver will still come up.
        print("[prepperpi-tiles/index] no style template found; skipping style.json", file=sys.stderr)

    fragment_html = render_landing_fragment(regions)
    write_atomic(args.fragment_out, fragment_html.encode("utf-8"))

    summary = regions_summary(regions)
    write_atomic(args.regions_json, (json.dumps(summary, indent=2) + "\n").encode("utf-8"))

    return 0


if __name__ == "__main__":
    sys.exit(main())
