// dashboard.js — progressive enhancement for the landing page.
//
// Polls /_events.json every POLL_INTERVAL_MS, surfaces any new
// events as toast notifications, and DOM-swaps the affected
// fragment regions in place so the user doesn't have to refresh.
//
// Hard requirement: with JS disabled the page must still render
// and work, with the static fragments that were inlined at request
// time. Nothing in this file is load-bearing; everything is
// progressive.

(function () {
  "use strict";

  var POLL_INTERVAL_MS = 2000;
  var TOAST_DURATION_MS = 4500;
  var TOAST_FADE_MS = 300;
  var EVENTS_URL = "/_events.json";

  // Map a server-emitted event type to the set of fragments the
  // page should refetch + DOM-swap when the event lands. Unknown
  // types fall back to `["library", "usb"]` -- conservative.
  var FRAGMENT_FOR_EVENT = {
    usb_plugged: ["usb", "library", "library_search"],
    usb_unplugged: ["usb", "library", "library_search"],
    library_changed: ["library", "library_search"],
    maps_changed: ["maps"],
  };

  var lastVersion = null;
  var seenIds = new Set();
  var pollTimer = null;

  function showToast(message) {
    var container = document.getElementById("prepperpi-toasts");
    if (!container) return;
    var toast = document.createElement("div");
    toast.className = "toast";
    toast.setAttribute("role", "status");
    toast.textContent = message;
    container.appendChild(toast);
    // Reflow before adding the visible class so the transition
    // actually animates instead of jumping straight to opacity 1.
    requestAnimationFrame(function () {
      toast.classList.add("toast--visible");
    });
    setTimeout(function () {
      toast.classList.remove("toast--visible");
      setTimeout(function () {
        if (toast.parentNode) toast.parentNode.removeChild(toast);
      }, TOAST_FADE_MS);
    }, TOAST_DURATION_MS);
  }

  function refreshFragment(name) {
    var target = document.querySelector(
      '[data-fragment="' + name + '"]'
    );
    if (!target) return Promise.resolve();
    return fetch("/_" + name + ".html", { cache: "no-store" })
      .then(function (res) {
        if (!res.ok) return null;
        return res.text();
      })
      .then(function (html) {
        if (html != null) target.innerHTML = html;
      })
      .catch(function () {
        // network blips: ignore, next poll will recover
      });
  }

  function poll() {
    return fetch(EVENTS_URL, { cache: "no-store" })
      .then(function (res) {
        if (!res.ok) return null;
        return res.json();
      })
      .then(function (data) {
        if (!data || typeof data !== "object") return;
        var events = Array.isArray(data.events) ? data.events : [];
        if (lastVersion === null) {
          // First poll: ingest the existing log silently. We only
          // want to surface events that happen AFTER the user
          // arrived on the page.
          lastVersion = data.version;
          events.forEach(function (ev) { seenIds.add(ev.id); });
          return;
        }
        if (typeof data.version === "number" && data.version < lastVersion) {
          // Server-side reset (e.g. _events.json was truncated /
          // re-seeded). Re-baseline silently.
          lastVersion = data.version;
          seenIds = new Set();
          events.forEach(function (ev) { seenIds.add(ev.id); });
          return;
        }
        if (data.version === lastVersion) return;
        lastVersion = data.version;

        var fragmentsToRefresh = new Set();
        events.forEach(function (ev) {
          if (seenIds.has(ev.id)) return;
          seenIds.add(ev.id);
          if (ev.message) showToast(ev.message);
          var frags = FRAGMENT_FOR_EVENT[ev.type] || ["library", "usb"];
          frags.forEach(function (f) { fragmentsToRefresh.add(f); });
        });
        var refreshes = [];
        fragmentsToRefresh.forEach(function (f) {
          refreshes.push(refreshFragment(f));
        });
        return Promise.all(refreshes);
      })
      .catch(function () {
        // ignore
      });
  }

  function startPolling() {
    if (pollTimer != null) return;
    poll();
    pollTimer = setInterval(poll, POLL_INTERVAL_MS);
  }

  function stopPolling() {
    if (pollTimer == null) return;
    clearInterval(pollTimer);
    pollTimer = null;
  }

  // Page Visibility: pause when the tab is hidden so we aren't
  // hammering the server (and the phone's radio) while sitting
  // on a sidebar tab.
  document.addEventListener("visibilitychange", function () {
    if (document.hidden) stopPolling();
    else startPolling();
  });

  // Kick off after DOMContentLoaded so the toast container exists.
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", startPolling);
  } else {
    startPolling();
  }
})();
