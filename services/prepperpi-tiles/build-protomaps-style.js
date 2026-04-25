#!/usr/bin/env node
/* build-protomaps-style.js — emit a MapLibre style.json template for
 * the Protomaps "light" basemap, using the protomaps-themes-base npm
 * package's `layers()` helper.
 *
 * Output goes to stdout. setup.sh redirects it into:
 *     /etc/prepperpi/tileserver/styles/protomaps/style.template.json
 *
 * The template uses a single placeholder source named "protomaps".
 * The Python reindex script (tiles_indexer.py:build_composite_style)
 * later rewrites that source plus all per-layer source bindings into
 * one entry per installed region (region__<id>) so multiple installed
 * countries render seamlessly under a single MapLibre instance.
 *
 * Glyphs/sprite URLs in the emitted template are placeholders and are
 * also rewritten by build_composite_style at reindex time so they
 * point at our local Caddy / tileserver URLs.
 */

const path = require("path");

// Resolve from our own node_modules (setup.sh installs to DST_DIR/node_modules).
const themesBase = require(path.resolve(__dirname, "node_modules/protomaps-themes-base"));

// 4.x exports a `default(sourceName, themeName)` function that returns
// the COMPLETE basemap layer array — background + basemap shapes +
// label layers in render order. That's what we want; the alternate
// `layers()` export omits labels and the background (which would force
// us to assemble them ourselves and risks double-background bugs).
const buildLayers = themesBase.default;
if (typeof buildLayers !== "function") {
  console.error("build-protomaps-style: protomaps-themes-base default export is not a function");
  process.exit(1);
}

const themeLayers = buildLayers("protomaps", "light");

// Workaround for a recurrent upstream issue (tracked in the
// protomaps-themes-base 4.x branch): the rendered style occasionally
// contains expression sub-trees with `null` literals that
// MapLibre/tileserver-gl-light reject during style validation
// (text-font with null elements, fill-color stops with null colors).
// We walk the layer tree once and replace any literal `null` we find
// with a benign neutral so the style stays loadable. This is purely
// defensive — when the upstream cleans these up, this is a no-op.
function scrubNulls(node) {
  if (Array.isArray(node)) {
    return node.map(scrubNulls).map((v) => v === null ? "" : v);
  }
  if (node && typeof node === "object") {
    const out = {};
    for (const k of Object.keys(node)) out[k] = scrubNulls(node[k]);
    return out;
  }
  return node;
}

const style = {
  version: 8,
  name: "Protomaps Light",
  glyphs: "https://example.invalid/{fontstack}/{range}.pbf",
  sprite: "https://example.invalid/sprite",
  sources: {
    protomaps: {
      type: "vector",
      // Tileserver-gl-light replaces this URL when serving; our composite
      // builder also rewrites the sources block. Anything reasonable here.
      url: "pmtiles://{protomaps}",
    },
  },
  layers: scrubNulls(themeLayers),
};

process.stdout.write(JSON.stringify(style, null, 2) + "\n");
