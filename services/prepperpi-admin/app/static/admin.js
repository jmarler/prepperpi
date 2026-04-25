// admin.js — progressive enhancement for the admin pages.
//
// Two independent feature blocks, both polling-based:
//   1. Home page: live-update the Ethernet uplink banner.
//   2. Storage page: live-update CPU/RAM/temp/disk/USB/events at 1 Hz.
//
// Hard requirement (same as the landing-page dashboard.js): with JS
// disabled the page must still render correctly via Jinja's request-time
// render. Nothing here is load-bearing.

(function () {
  "use strict";

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

  function isHidden() {
    return typeof document.hidden === "boolean" && document.hidden;
  }

  // ---------- Block 1: home-page uplink banner ----------
  (function () {
    var POLL_INTERVAL_MS = 5000;
    var UPLINK_URL = "/admin/uplink";

    var banner = document.querySelector("[data-uplink-banner]");
    if (!banner) return;

    var lastEthernet = banner.classList.contains("uplink-banner--online");
    var lastIface = banner.querySelector("code")
      ? banner.querySelector("code").textContent
      : null;

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
      if (isHidden()) return;
      fetch(UPLINK_URL, { cache: "no-store" })
        .then(function (res) { return res.ok ? res.json() : null; })
        .then(function (state) {
          if (state && typeof state === "object") render(state);
        })
        .catch(function () { /* next tick recovers */ });
    }
    setInterval(poll, POLL_INTERVAL_MS);
  })();

  // ---------- Block 2: storage page live stats ----------
  (function () {
    var POLL_INTERVAL_MS = 1000;
    var HEALTH_URL = "/admin/health";

    var disksContainer = document.querySelector("[data-storage-disks]");
    var usbContainer = document.querySelector("[data-storage-usb]");
    var eventsContainer = document.querySelector("[data-storage-events]");
    if (!disksContainer && !usbContainer && !eventsContainer) return; // not on storage page

    var statNodes = {};
    ["cpu", "memory", "temp", "uptime", "clients"].forEach(function (key) {
      var node = document.querySelector('[data-stat="' + key + '"]');
      if (node) statNodes[key] = node;
    });

    var tempBanner = document.querySelector("[data-temp-banner]");
    var lastEventVersion = null;
    var lastUsbState = null;

    function fmtBytes(n) {
      var units = ["B", "kB", "MB", "GB", "TB"];
      var v = Number(n) || 0;
      for (var i = 0; i < units.length - 1; i++) {
        if (v < 1000) break;
        v /= 1000;
      }
      var unit = units[Math.min(i, units.length - 1)];
      if (unit === "B") return Math.round(v) + " B";
      return v.toFixed(1) + " " + unit;
    }

    function fmtUptime(s) {
      s = Math.max(0, Number(s) || 0);
      if (s < 60) return s + "s";
      var m = Math.floor(s / 60);
      var h = Math.floor(m / 60); m = m % 60;
      var d = Math.floor(h / 24); h = h % 24;
      if (d) return d + "d " + h + "h " + m + "m";
      if (h) return h + "h " + m + "m";
      return m + "m";
    }

    function setText(key, text) {
      var node = statNodes[key];
      if (node && node.textContent !== text) node.textContent = text;
    }

    function diskRow(d) {
      var warnClass = d.low_space ? " storage-row--warn" : "";
      var warnP = d.low_space ? '<p class="storage-row__warn">Less than 5% free.</p>' : '';
      return (
        '<li class="storage-row' + warnClass + '">' +
          '<div class="storage-row__head">' +
            '<span class="storage-row__label">' + escapeHtml(d.mount) + '</span>' +
            '<span class="storage-row__usage">' + fmtBytes(d.used_bytes) + ' / ' + fmtBytes(d.total_bytes) + ' (' + d.percent + '%)</span>' +
          '</div>' +
          '<div class="storage-bar"><div class="storage-bar__fill" style="width: ' + Math.min(100, Math.max(0, Number(d.percent) || 0)) + '%"></div></div>' +
          warnP +
        '</li>'
      );
    }

    function usbRow(u) {
      var badge = u.writable
        ? '<span class="badge badge--writable">writable</span>'
        : '<span class="badge badge--readonly">read-only</span>';
      var btnClass = u.writable ? "btn btn--danger" : "btn btn--primary";
      var btnLabel = u.writable ? "Make read-only" : "Make writable";
      var nextWritable = u.writable ? "false" : "true";
      return (
        '<li class="storage-row">' +
          '<div class="storage-row__head">' +
            '<span class="storage-row__label">' + escapeHtml(u.name) + ' <span class="storage-row__sub">' + badge + '</span></span>' +
            '<span class="storage-row__usage">' + fmtBytes(u.used_bytes) + ' / ' + fmtBytes(u.total_bytes) + ' (' + u.percent + '%)</span>' +
          '</div>' +
          '<div class="storage-bar"><div class="storage-bar__fill" style="width: ' + Math.min(100, Math.max(0, Number(u.percent) || 0)) + '%"></div></div>' +
          '<form method="post" action="/admin/storage/usb/' + encodeURIComponent(u.name) + '/writable" class="storage-row__form">' +
            '<input type="hidden" name="writable" value="' + nextWritable + '">' +
            '<button type="submit" class="' + btnClass + '">' + btnLabel + '</button>' +
          '</form>' +
        '</li>'
      );
    }

    function eventLine(e) {
      return (
        '<li class="storage-event">' +
          '<time datetime="' + escapeHtml(e.ts || "") + '">' + escapeHtml(e.ts || "") + '</time> ' +
          '<span class="storage-event__type">' + escapeHtml(e.type || "") + '</span> ' +
          '<span class="storage-event__msg">' + escapeHtml(e.message || "") + '</span>' +
        '</li>'
      );
    }

    function renderUsb(drives) {
      // Cheap signature so we don't churn the DOM (and reset focus on the
      // toggle button) when nothing relevant changed.
      var sig = drives.map(function (u) {
        return u.name + ":" + u.writable + ":" + u.percent;
      }).join("|");
      if (sig === lastUsbState) return;
      lastUsbState = sig;

      if (!drives.length) {
        usbContainer.innerHTML = '<li class="storage-row storage-row--empty">No USB drives mounted.</li>';
        return;
      }
      usbContainer.innerHTML = drives.map(usbRow).join("");
    }

    function renderEvents(events) {
      // Server-side reverses for first render; we reverse here for parity.
      var reversed = events.slice().reverse();
      eventsContainer.innerHTML = reversed.length
        ? reversed.map(eventLine).join("")
        : '<li class="storage-event storage-event--empty">No events yet.</li>';
    }

    function render(state) {
      // Stats
      setText("cpu", state.cpu_percent + "%");
      if (state.memory) {
        setText("memory",
          fmtBytes(state.memory.used_bytes) + " / " +
          fmtBytes(state.memory.total_bytes) + " (" + state.memory.percent + "%)");
      }
      setText("temp", state.temp_celsius == null
        ? "n/a"
        : state.temp_celsius.toFixed(1) + " °C");
      setText("uptime", fmtUptime(state.uptime_seconds));
      setText("clients", String(state.clients || 0));

      // Temperature warning banner
      if (tempBanner) {
        if (state.temp_warn) tempBanner.classList.remove("hidden");
        else tempBanner.classList.add("hidden");
      }

      // Disks (always re-render; cheap, and percentages move)
      if (disksContainer && Array.isArray(state.disks)) {
        if (!state.disks.length) {
          disksContainer.innerHTML = '<li class="storage-row storage-row--empty">No disks reported.</li>';
        } else {
          disksContainer.innerHTML = state.disks.map(diskRow).join("");
        }
      }

      // USB drives — version-skip via signature so we don't reset the form button focus
      if (usbContainer && Array.isArray(state.usb_drives)) {
        renderUsb(state.usb_drives);
      }

      // Events — only re-render when the version actually changed
      if (eventsContainer && state.events) {
        var v = state.events.version;
        if (v !== lastEventVersion) {
          renderEvents(state.events.events || []);
          lastEventVersion = v;
        }
      }
    }

    function poll() {
      if (isHidden()) return;
      fetch(HEALTH_URL, { cache: "no-store" })
        .then(function (res) { return res.ok ? res.json() : null; })
        .then(function (state) {
          if (state && typeof state === "object") render(state);
        })
        .catch(function () { /* next tick recovers */ });
    }
    setInterval(poll, POLL_INTERVAL_MS);
  })();
})();
