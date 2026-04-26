// PrepperPi — MapLibre GL JS init for the offline /maps/ client.
//
// The flow:
//   1. Fetch our composite style.json from the local tileserver. If
//      it 404s or has no sources, surface the "No regions installed"
//      message (AC-1 fail-open behavior) and never instantiate
//      MapLibre — its constructor throws on an empty source list.
//   2. Otherwise, create the map, fit it to the union bounds the
//      reindex script wrote into the style ("max_bounds" key), and
//      enable the standard pan/zoom/touch controls.
//   3. Anything that goes wrong on the way to a rendered map writes
//      a visible reason into the empty-state UI AND logs to the
//      browser console — silent fall-through to "no regions" was
//      hiding genuine setup bugs.
//
// Single file, no build step, no module loader. MapLibre GL JS is
// loaded via /maps/maplibre-gl.js by the host page; we just consume
// the global `maplibregl`.

(function () {
  "use strict";

  var STYLE_URL = "/maps/styles/protomaps/style.json";

  function showEmpty(reason, detail) {
    var canvas = document.getElementById("map");
    var empty  = document.getElementById("map-empty");
    if (canvas) canvas.hidden = true;
    if (empty) {
      empty.hidden = false;
      // Replace the canned "no regions installed" copy with a more
      // accurate message when we KNOW why we ended up here. We keep
      // the canned copy unmodified for the plain "no regions" path.
      if (reason && reason !== "no-regions") {
        var h2 = empty.querySelector("h2");
        var p  = empty.querySelector("p");
        if (h2) h2.textContent = "Map could not load";
        if (p)  p.textContent  = (detail || "Unknown error") +
          " — see browser console for details.";
      }
    }
    if (reason && reason !== "no-regions") {
      try { console.error("[prepperpi-maps]", reason, detail); } catch (_) {}
    }
  }

  function buildMap(styleSpec) {
    if (typeof maplibregl === "undefined") {
      // The maplibre-gl bundle didn't load. Setup.sh might not have
      // fetched it yet; fall back to the empty-state message.
      showEmpty("no-maplibre", "MapLibre GL JS bundle did not load.");
      return;
    }

    var center = styleSpec.center || [0, 0];
    var zoom   = (typeof styleSpec.zoom === "number") ? styleSpec.zoom : 2;

    var map;
    try {
      map = new maplibregl.Map({
        container: "map",
        style: styleSpec,
        center: center,
        zoom: zoom,
        // The tileserver-gl-light backend is local — no need to throttle.
        maxParallelImageRequests: 32,
        // Disable CJK font rendering on the worker; we ship Latin-only
        // glyphs to keep the initial install lightweight. Setup.sh can
        // add CJK ranges later without touching this code.
        localIdeographFontFamily: false,
        attributionControl: { compact: true },
        // MapLibre fetches tile data from a Web Worker that has no
        // `document` to resolve relative URLs against — Request()
        // throws "Failed to parse URL" on bare paths like
        // "/maps/data/BZ/{z}/{x}/{y}.pbf". Tileserver-gl-light's
        // --public_url flag rewrites glyphs/sprite to absolute, but
        // source.tiles entries pass through. Absolutize them here so
        // the URL works regardless of how the user reached the box
        // (prepperpi.home.arpa vs 10.42.0.1 vs 192.168.x.y).
        transformRequest: function (url) {
          if (url && url.charAt(0) === "/" && url.charAt(1) !== "/") {
            return { url: window.location.origin + url };
          }
          return { url: url };
        }
      });
    } catch (err) {
      // MapLibre throws synchronously on a fatally-malformed style.
      showEmpty("style-error", "MapLibre rejected the style: " + (err && err.message || err));
      return;
    }

    // Asynchronous errors (a tile fetch failing, a glyph 404) emit
    // through the 'error' event. We surface a one-shot warning to
    // the console so debugging is possible without dev-tools tricks,
    // but we DO NOT swap to the empty-state UI — partial render is
    // better than a blank "couldn't load" screen.
    map.on("error", function (ev) {
      try { console.warn("[prepperpi-maps] runtime", ev && ev.error || ev); } catch (_) {}
    });

    map.addControl(new maplibregl.NavigationControl({ visualizePitch: false }), "top-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");

    // Snap to the LARGEST installed region on first render. AC-1 says
    // "largest installed region, centered on the metadata-defined
    // origin" — and unioning bounds across far-apart regions zooms
    // out so far that nothing is visible (e.g. Belize + Vatican
    // spans the Atlantic; at that zoom both are sub-pixel and the
    // user sees just background-color, looking like "no map").
    // Pick the source with the largest bbox area; user can pan to
    // others.
    var biggest = pickLargestSourceBbox(styleSpec.sources);
    if (biggest) {
      map.once("load", function () {
        try {
          map.fitBounds(
            [[biggest[0], biggest[1]], [biggest[2], biggest[3]]],
            { padding: 24, animate: false, maxZoom: 10 }
          );
        } catch (_) { /* fall back to the style's own center/zoom */ }
      });
    }
  }

  function pickLargestSourceBbox(sources) {
    var best = null;
    var bestArea = 0;
    if (!sources) return null;
    for (var k in sources) {
      if (!Object.prototype.hasOwnProperty.call(sources, k)) continue;
      var b = sources[k] && sources[k].bounds;
      if (!Array.isArray(b) || b.length !== 4) continue;
      var area = (b[2] - b[0]) * (b[3] - b[1]);
      if (area > bestArea) { bestArea = area; best = b; }
    }
    return best;
  }

  function fetchAndInit() {
    fetch(STYLE_URL, { credentials: "same-origin", cache: "no-store" })
      .then(function (res) {
        if (!res.ok) throw new Error("style fetch HTTP " + res.status);
        return res.json();
      })
      .then(function (styleSpec) {
        var sources = styleSpec && styleSpec.sources;
        var hasAnySource = sources && Object.keys(sources).length > 0;
        if (!hasAnySource) {
          showEmpty("no-regions");
          return;
        }
        buildMap(styleSpec);
      })
      .catch(function (err) {
        showEmpty("style-fetch-failed",
          "Could not fetch or parse style.json (" + (err && err.message || err) + ").");
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", fetchAndInit);
  } else {
    fetchAndInit();
  }
})();
