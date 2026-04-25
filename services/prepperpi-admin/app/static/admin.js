// admin.js — progressive enhancement for the admin home page.
//
// Polls /admin/uplink every POLL_INTERVAL_MS and DOM-swaps the uplink
// banner in place when ethernet state changes. Hard requirement (same
// as the landing-page dashboard.js): with JS disabled the page must
// still render correctly via Jinja's request-time render. Nothing here
// is load-bearing.

(function () {
  "use strict";

  var POLL_INTERVAL_MS = 5000;
  var UPLINK_URL = "/admin/uplink";

  var banner = document.querySelector("[data-uplink-banner]");
  if (!banner) return;

  // Track last-rendered state so we only touch the DOM on real changes.
  var lastEthernet = banner.classList.contains("uplink-banner--online");
  var lastIface = banner.querySelector("code")
    ? banner.querySelector("code").textContent
    : null;

  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      }[c];
    });
  }

  function render(state) {
    var ethernet = !!state.ethernet;
    var iface = state.iface || null;
    if (ethernet === lastEthernet && iface === lastIface) return;

    if (ethernet) {
      banner.className = "uplink-banner uplink-banner--online";
      banner.innerHTML =
        "<strong>Ethernet uplink active</strong> on <code>" +
        escapeHtml(iface || "eth?") +
        "</code> — downloads possible without interrupting the AP. " +
        "Wi-Fi clients on this network cannot reach the internet " +
        "through the Pi.";
    } else {
      banner.className = "uplink-banner uplink-banner--offline";
      banner.innerHTML =
        "<strong>Offline.</strong> Plug an Ethernet cable in to enable " +
        "downloads. The AP keeps running while you're online.";
    }
    lastEthernet = ethernet;
    lastIface = iface;
  }

  function poll() {
    if (typeof document.hidden === "boolean" && document.hidden) return;
    fetch(UPLINK_URL, { cache: "no-store" })
      .then(function (res) {
        if (!res.ok) return null;
        return res.json();
      })
      .then(function (state) {
        if (state && typeof state === "object") render(state);
      })
      .catch(function () {
        // Network blip; next tick will recover.
      });
  }

  setInterval(poll, POLL_INTERVAL_MS);
})();
