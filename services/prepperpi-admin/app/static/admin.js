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

  function fmtBytes(n) {
    var units = ["B", "kB", "MB", "GB", "TB"];
    var v = Number(n) || 0;
    var i;
    for (i = 0; i < units.length - 1; i++) {
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

  // ---------- Block 3: catalog page (browse + queue) ----------
  (function () {
    var POLL_INTERVAL_MS = 1000;
    var CATALOG_DATA_URL = "/admin/catalog/data";
    var DOWNLOADS_URL = "/admin/downloads";
    var PAGE_SIZE = 50;

    var listContainer = document.querySelector("[data-catalog-list]");
    var queueContainer = document.querySelector("[data-catalog-queue]");
    var destPicker = document.querySelector("[data-destination-picker]");
    if (!listContainer || !queueContainer) return; // not on catalog page

    var searchInput = document.getElementById("catalog-search");
    var languageSelect = document.getElementById("catalog-language");
    var categorySelect = document.getElementById("catalog-category");
    var sizeSelect = document.getElementById("catalog-size");
    var countLabel = document.getElementById("catalog-count");
    var pagerNav = document.getElementById("catalog-pager");

    var allBooks = [];
    var filteredBooks = [];
    var currentPage = 0;
    var lastQueueSig = null;

    function bookMatchesFilters(b, opts) {
      if (opts.lang && b.language !== opts.lang) return false;
      if (opts.cat && b.category !== opts.cat && (b.tags || []).indexOf(opts.cat) === -1) return false;
      if (opts.minSize !== null && b.size_bytes < opts.minSize) return false;
      if (opts.maxSize !== null && b.size_bytes > opts.maxSize) return false;
      if (opts.q) {
        var hay = ((b.title || "") + " " + (b.name || "")).toLowerCase();
        if (hay.indexOf(opts.q) === -1) return false;
      }
      return true;
    }

    function readFilters() {
      var sizeRange = (sizeSelect && sizeSelect.value) || "";
      var minSize = null, maxSize = null;
      if (sizeRange) {
        var parts = sizeRange.split(":");
        minSize = parts[0] ? Number(parts[0]) : null;
        maxSize = parts[1] ? Number(parts[1]) : null;
      }
      return {
        q: ((searchInput && searchInput.value) || "").toLowerCase().trim(),
        lang: (languageSelect && languageSelect.value) || "",
        cat: (categorySelect && categorySelect.value) || "",
        minSize: minSize,
        maxSize: maxSize,
      };
    }

    function applyFilters() {
      var opts = readFilters();
      filteredBooks = allBooks.filter(function (b) { return bookMatchesFilters(b, opts); });
      currentPage = 0;
      renderList();
      renderPager();
      if (countLabel) {
        countLabel.textContent = filteredBooks.length === allBooks.length
          ? "(" + allBooks.length + ")"
          : "(" + filteredBooks.length + " of " + allBooks.length + ")";
      }
    }

    function bookRow(b) {
      var dest = destPicker ? destPicker.options[destPicker.selectedIndex] : null;
      var destFree = dest ? Number(dest.getAttribute("data-free-bytes") || "0") : 0;
      var willFit = b.size_bytes <= destFree;
      var fitNote = willFit
        ? ""
        : '<span class="catalog-warn">Won\'t fit in selected destination</span>';
      return (
        '<li class="storage-row catalog-book">' +
          '<div class="storage-row__head">' +
            '<span class="storage-row__label">' + escapeHtml(b.title || b.name) + ' ' +
              '<span class="storage-row__sub">' +
                '<span class="badge">' + escapeHtml(b.language || "?") + '</span> ' +
                '<span class="badge">' + escapeHtml(b.category || "—") + '</span>' +
              '</span>' +
            '</span>' +
            '<span class="storage-row__usage">' + fmtBytes(b.size_bytes) + ' · ' + escapeHtml((b.updated || "").slice(0, 10)) + '</span>' +
          '</div>' +
          (b.summary ? '<p class="catalog-book__summary">' + escapeHtml(b.summary) + '</p>' : '') +
          '<form class="storage-row__form" data-queue-form data-book-id="' + escapeHtml(b.id) + '">' +
            '<button type="submit" class="btn btn--primary"' + (willFit ? "" : " disabled") + '>Download</button>' +
            fitNote +
          '</form>' +
        '</li>'
      );
    }

    function renderList() {
      if (!filteredBooks.length) {
        listContainer.innerHTML = '<p class="storage-row storage-row--empty">No books match your filters.</p>';
        return;
      }
      var start = currentPage * PAGE_SIZE;
      var slice = filteredBooks.slice(start, start + PAGE_SIZE);
      listContainer.innerHTML = '<ul class="storage-list">' + slice.map(bookRow).join("") + "</ul>";
      // Wire up the per-book queue forms.
      var forms = listContainer.querySelectorAll("[data-queue-form]");
      for (var i = 0; i < forms.length; i++) {
        forms[i].addEventListener("submit", onQueueSubmit);
      }
    }

    function renderPager() {
      if (!pagerNav) return;
      var pages = Math.ceil(filteredBooks.length / PAGE_SIZE);
      if (pages <= 1) { pagerNav.innerHTML = ""; return; }
      var html = "";
      for (var p = 0; p < pages; p++) {
        var cls = (p === currentPage) ? "catalog-pager__page catalog-pager__page--active" : "catalog-pager__page";
        html += '<button type="button" class="' + cls + '" data-page="' + p + '">' + (p + 1) + '</button>';
      }
      pagerNav.innerHTML = html;
      var btns = pagerNav.querySelectorAll("button");
      for (var i = 0; i < btns.length; i++) {
        btns[i].addEventListener("click", function (e) {
          currentPage = Number(e.currentTarget.getAttribute("data-page")) || 0;
          renderList();
          renderPager();
        });
      }
    }

    function populateFacets(facets) {
      if (languageSelect && facets.languages) {
        var existing = languageSelect.value;
        var opts = ['<option value="">All languages</option>'];
        for (var i = 0; i < facets.languages.length; i++) {
          var f = facets.languages[i];
          opts.push('<option value="' + escapeHtml(f.code) + '">' + escapeHtml(f.code) + ' (' + f.count + ')</option>');
        }
        languageSelect.innerHTML = opts.join("");
        languageSelect.value = existing;
      }
      if (categorySelect && facets.categories) {
        var existing = categorySelect.value;
        var opts = ['<option value="">All categories</option>'];
        for (var i = 0; i < facets.categories.length; i++) {
          var f = facets.categories[i];
          opts.push('<option value="' + escapeHtml(f.name) + '">' + escapeHtml(f.name) + ' (' + f.count + ')</option>');
        }
        categorySelect.innerHTML = opts.join("");
        categorySelect.value = existing;
      }
    }

    function onQueueSubmit(e) {
      e.preventDefault();
      var form = e.currentTarget;
      var bookId = form.getAttribute("data-book-id");
      var destId = destPicker ? destPicker.value : "sd";
      var btn = form.querySelector("button");
      if (btn) { btn.disabled = true; btn.textContent = "Queueing…"; }
      var body = "book_id=" + encodeURIComponent(bookId) +
                 "&destination_id=" + encodeURIComponent(destId);
      fetch("/admin/downloads/queue", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: body,
      })
        .then(function (res) {
          if (res.ok) {
            if (btn) { btn.textContent = "Queued"; }
            return;
          }
          // Tolerate plain-text error bodies (e.g. uvicorn's default
          // "Internal Server Error" when something escapes our
          // HTTPException handlers). res.json() throws on those and
          // the user gets a JSON-parse error in their face — not
          // useful. Read as text, try to parse, fall back to text.
          return res.text().then(function (text) {
            var detail = text || ("HTTP " + res.status);
            try { detail = JSON.parse(text).detail || detail; } catch (e) {}
            throw new Error(detail);
          });
        })
        .catch(function (err) {
          if (btn) { btn.disabled = false; btn.textContent = "Download"; }
          alert("Couldn't queue: " + err.message);
        });
    }

    function queueRow(d) {
      var statusBadge =
        d.status === "active" ? '<span class="badge badge--writable">active</span>' :
        d.status === "paused" ? '<span class="badge badge--readonly">paused</span>' :
        d.status === "complete" ? '<span class="badge badge--writable">complete</span>' :
        d.status === "error" ? '<span class="badge badge--readonly">error</span>' :
        '<span class="badge">' + escapeHtml(d.status) + '</span>';

      var actions = "";
      if (d.status === "active") {
        actions =
          '<button type="button" class="btn" data-pause data-gid="' + escapeHtml(d.gid) + '">Pause</button> ' +
          '<button type="button" class="btn btn--danger" data-cancel data-gid="' + escapeHtml(d.gid) + '">Cancel</button>';
      } else if (d.status === "paused") {
        actions =
          '<button type="button" class="btn btn--primary" data-resume data-gid="' + escapeHtml(d.gid) + '">Resume</button> ' +
          '<button type="button" class="btn btn--danger" data-cancel data-gid="' + escapeHtml(d.gid) + '">Cancel</button>';
      } else if (d.status === "waiting") {
        actions =
          '<button type="button" class="btn btn--danger" data-cancel data-gid="' + escapeHtml(d.gid) + '">Cancel</button>';
      } else if (d.status === "complete" || d.status === "error" || d.status === "removed") {
        // Already finished — `Clear` removes the bookkeeping row only;
        // any downloaded file stays in place.
        actions =
          '<button type="button" class="btn" data-clear data-gid="' + escapeHtml(d.gid) + '">Clear</button>';
      }

      var meta = d.error_message
        ? '<p class="storage-row__warn">Error: ' + escapeHtml(d.error_message) + '</p>'
        : "";
      var speed = d.speed_bps > 0 ? " · " + fmtBytes(d.speed_bps) + "/s" : "";
      var eta = (d.eta_seconds && d.eta_seconds > 0) ? " · ETA " + fmtUptime(d.eta_seconds) : "";

      return (
        '<li class="storage-row">' +
          '<div class="storage-row__head">' +
            '<span class="storage-row__label">' + escapeHtml(d.filename || d.gid) + ' <span class="storage-row__sub">' + statusBadge + '</span></span>' +
            '<span class="storage-row__usage">' + fmtBytes(d.completed_bytes) + ' / ' + fmtBytes(d.total_bytes) + ' (' + d.percent + '%)' + speed + eta + '</span>' +
          '</div>' +
          '<div class="storage-bar"><div class="storage-bar__fill" style="width: ' + Math.min(100, Math.max(0, Number(d.percent) || 0)) + '%"></div></div>' +
          (actions ? '<div class="storage-row__form">' + actions + '</div>' : '') +
          meta +
        '</li>'
      );
    }

    function renderQueue(items) {
      var sig = items.map(function (d) {
        return d.gid + ":" + d.status + ":" + d.completed_bytes + ":" + d.total_bytes;
      }).join("|");
      if (sig === lastQueueSig) return;
      lastQueueSig = sig;

      if (!items.length) {
        queueContainer.innerHTML = '<li class="storage-row storage-row--empty">No downloads yet.</li>';
        return;
      }
      queueContainer.innerHTML = items.map(queueRow).join("");
      // Wire up pause/resume/cancel buttons.
      queueContainer.querySelectorAll("[data-pause]").forEach(function (btn) {
        btn.addEventListener("click", function () {
          // Optimistic UI: pause/unpause RPC may take a few seconds
          // while aria2 closes peer connections, so show feedback
          // immediately rather than waiting for the next 1Hz poll.
          btn.disabled = true;
          btn.textContent = "Pausing…";
          dispatch(btn.getAttribute("data-gid"), "pause");
        });
      });
      queueContainer.querySelectorAll("[data-resume]").forEach(function (btn) {
        btn.addEventListener("click", function () {
          btn.disabled = true;
          btn.textContent = "Resuming…";
          dispatch(btn.getAttribute("data-gid"), "resume");
        });
      });
      queueContainer.querySelectorAll("[data-cancel]").forEach(function (btn) {
        btn.addEventListener("click", function () {
          if (!confirm("Cancel this download?")) return;
          btn.disabled = true;
          btn.textContent = "Cancelling…";
          dispatch(btn.getAttribute("data-gid"), "cancel");
        });
      });
      queueContainer.querySelectorAll("[data-clear]").forEach(function (btn) {
        btn.addEventListener("click", function () {
          btn.disabled = true;
          btn.textContent = "Clearing…";
          dispatch(btn.getAttribute("data-gid"), "clear");
        });
      });
    }

    function dispatch(gid, action) {
      fetch("/admin/downloads/" + encodeURIComponent(gid) + "/" + action, { method: "POST" })
        .then(function () {
          // Force the next poll to re-render even if the queue
          // signature hasn't changed yet — speeds up the visual
          // confirmation that the action took effect.
          lastQueueSig = null;
        })
        .catch(function () { /* next tick recovers */ });
    }

    function poll() {
      if (isHidden()) return;
      fetch(DOWNLOADS_URL, { cache: "no-store" })
        .then(function (res) { return res.ok ? res.json() : null; })
        .then(function (state) {
          if (state && Array.isArray(state.items)) renderQueue(state.items);
        })
        .catch(function () { /* tick */ });
    }

    // Initial load: fetch the catalog dataset once, then user-triggered
    // filter changes are pure JS (client-side filtering).
    fetch(CATALOG_DATA_URL, { cache: "no-store" })
      .then(function (res) { return res.ok ? res.json() : null; })
      .then(function (cache) {
        if (!cache) return;
        allBooks = cache.books || [];
        populateFacets(cache.facets || {});
        applyFilters();
      })
      .catch(function () {
        listContainer.innerHTML = '<p class="storage-row storage-row--empty">Couldn\'t load catalog. Refresh while online to fetch.</p>';
      });

    // Filter change triggers a refilter (cheap; ~1500 books).
    [searchInput, languageSelect, categorySelect, sizeSelect].forEach(function (el) {
      if (el) el.addEventListener("input", applyFilters);
      if (el) el.addEventListener("change", applyFilters);
    });
    if (destPicker) destPicker.addEventListener("change", function () {
      // Re-render the book list so the "won't fit" warning updates.
      renderList();
    });

    poll();
    setInterval(poll, POLL_INTERVAL_MS);
  })();
})();
