/* NEW POLYGON PJP DSE — reads the local DB via /api/*. No framework. */
(function () {
  "use strict";
  var A = window.APP;
  var state = { source: "unified", rows: [], sortKey: "name", sortAsc: true };
  // One place per source, so adding a fourth connection is a two-line change
  // rather than a hunt through every ternary in the file.
  var TABLE_OF = { overture: "poi_overture", google: "poi_google",
                   osm: "poi_osm", unified: "poi_unified" };
  var COLOUR_OF = { overture: "#7c5cff", google: "#4b96f3", osm: "#6ec46e",
                    unified: "#f0d04b" };
  var BADGE_OF = { overture: "OVT", google: "GGL", osm: "OSM",
                   reconcile: "RCN", unified: "UNI" };
  var $ = function (id) { return document.getElementById(id); };

  // ── map (optional: page must still work if Leaflet's CDN is blocked) ──
  var hasMap = false, map, layer;
  if (typeof L !== "undefined") {
    try {
      map = L.map("map").setView([A.lat, A.lon], 12);
      L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
        { attribution: "&copy; OpenStreetMap &copy; CARTO", maxZoom: 20 }).addTo(map);
      layer = L.layerGroup().addTo(map);
      hasMap = true;
      // Exposed so the Expand-map control at the foot of this file can call
      // invalidateSize after the panels fold away.
      A.map = map;
    } catch (e) { console.warn("map init failed:", e); }
  }
  if (!hasMap) {
    var el = $("map");
    el.classList.add("map-off");
    el.innerHTML = '<div class="map-fallback">Map unavailable — Leaflet could not load.'
      + '<br><span class="muted small">Filters and the table work normally.</span></div>';
  }

  function busy(on, text) {
    $("busy").hidden = !on;
    if (text) $("busyText").textContent = text;
  }

  function params() {
    var p = new URLSearchParams({ source: state.source });
    ["fCategory", "fBrand", "fName"].forEach(function (id, i) {
      var v = $(id).value.trim();
      if (v) p.set(["category", "brand", "name"][i], v);
    });
    return p;
  }

  function loadFacets() {
    return fetch("/api/facets?source=" + state.source)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        fill($("fCategory"), d.categories || []);
        fill($("fBrand"), d.brands || []);
      });
  }

  function fill(sel, values) {
    var keep = sel.value;
    sel.innerHTML = '<option value="">All</option>';
    values.forEach(function (v) {
      var o = document.createElement("option");
      o.value = v; o.textContent = v;
      sel.appendChild(o);
    });
    if (values.indexOf(keep) !== -1) sel.value = keep;
  }

  function load() {
    busy(true, "Reading " + state.source + " …");
    fetch("/api/rows?" + params().toString())
      .then(function (r) { return r.json(); })
      .then(function (d) {
        busy(false);
        if (!d.ok) { $("summary").textContent = d.error; state.rows = []; render(); return; }
        state.rows = d.rows;
        $("summary").textContent = d.count.toLocaleString() + " rows from "
          + TABLE_OF[state.source];
        render(); plot(); configBar(d.count);
      })
      .catch(function (e) { busy(false); $("summary").textContent = "Failed: " + e; });
  }

  // The filter that produced what you are looking at, stated above the map,
  // so a screenshot still says what it is showing.
  function chip(k, v, cls) {
    var e = document.createElement("span");
    e.className = "cfgchip" + (cls ? " " + cls : "");
    e.innerHTML = '<span class="k"></span><b></b>';
    e.firstChild.textContent = k;
    e.lastChild.textContent = v;
    return e;
  }
  function configBar(count) {
    var el = $("cfgbar");
    if (!el) return;
    el.innerHTML = '<span class="cfgtitle">Applied</span>';
    el.appendChild(chip("source", TABLE_OF[state.source]));
    el.appendChild(chip("category", ($("fCategory") || {}).value || "all"));
    el.appendChild(chip("brand", ($("fBrand") || {}).value || "all"));
    var nm = ($("fName") || {}).value;
    if (nm) el.appendChild(chip("name contains", nm));
    el.appendChild(chip("rows", (count || 0).toLocaleString(),
                        count ? "key" : "warn"));
    if (count > 4000)
      el.appendChild(chip("plotted", "first 4,000", "warn"));
  }

  function loadRuns() {
    fetch("/api/runs").then(function (r) { return r.json(); }).then(function (d) {
      if (!d.ok || !d.runs.length) { $("runs").innerHTML = '<span class="muted small">No pulls yet.</span>'; return; }
      $("runs").innerHTML = d.runs.slice(0, 6).map(function (r) {
        var cost = r.est_cost_usd ? " · $" + Number(r.est_cost_usd).toFixed(2) : " · free";
        return '<div class="run"><span class="src-badge src-' + r.source + '">'
          + (BADGE_OF[r.source] || r.source.toUpperCase()) + "</span> "
          + '<span class="mono">' + (r.rows_written || 0).toLocaleString() + " rows</span>"
          + '<span class="muted small">' + cost + " · " + (r.status || "") + "</span>"
          + '<div class="muted small">' + (r.started_utc || "").replace("T", " ") + "</div></div>";
      }).join("");
    }).catch(function () { });
  }

  function fmt(k, v) {
    if (v === null || v === undefined || v === "") return "";
    if (k === "confidence" || k === "rating") return Number(v).toFixed(2);
    if (k === "lat" || k === "lon") return Number(v).toFixed(6);
    return String(v);
  }

  function render() {
    var tb = $("resultsTable").querySelector("tbody");
    var f = $("tableFilter").value.toLowerCase();
    var rows = state.rows.filter(function (r) {
      if (!f) return true;
      return A.columns.some(function (c) {
        return String(r[c[0]] || "").toLowerCase().indexOf(f) !== -1;
      });
    });
    var k = state.sortKey, dir = state.sortAsc ? 1 : -1;
    rows.sort(function (a, b) {
      var x = a[k], y = b[k];
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      if (typeof x === "number" && typeof y === "number") return (x - y) * dir;
      return String(x).localeCompare(String(y)) * dir;
    });

    tb.innerHTML = "";
    if (!rows.length) {
      var tr = document.createElement("tr"), td = document.createElement("td");
      td.colSpan = A.columns.length; td.className = "empty";
      td.textContent = state.rows.length ? "No rows match the filter."
        : (A.dbReady ? "No rows — run a pull." : "No database yet.");
      tr.appendChild(td); tb.appendChild(tr); return;
    }
    rows.slice(0, 2000).forEach(function (r) {
      var tr = document.createElement("tr");
      A.columns.forEach(function (c) {
        var td = document.createElement("td");
        td.textContent = fmt(c[0], r[c[0]]);
        td.title = td.textContent;
        tr.appendChild(td);
      });
      tr.addEventListener("click", function () {
        if (hasMap && r.lat && r.lon) map.setView([r.lat, r.lon], 18);
      });
      tb.appendChild(tr);
    });
  }

  function plot() {
    if (!hasMap) return;
    layer.clearLayers();
    var pts = [];
    state.rows.slice(0, 4000).forEach(function (r) {
      if (!r.lat || !r.lon) return;
      var col = COLOUR_OF[state.source] || "#8b93a7";
      L.circleMarker([r.lat, r.lon], {
        radius: 4, color: col, weight: 1.2, fillColor: col, fillOpacity: .6
      }).bindPopup("<strong>" + esc(r.name || "(no name)") + "</strong><br>"
        + (r.brand_resolved ? esc(r.brand_resolved) + "<br>" : "")
        + esc(r.category || "")).addTo(layer);
      pts.push([r.lat, r.lon]);
    });
    if (pts.length) map.fitBounds(L.latLngBounds(pts).pad(0.1));
  }

  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  document.querySelectorAll(".tab").forEach(function (t) {
    t.addEventListener("click", function () {
      document.querySelectorAll(".tab").forEach(function (x) { x.classList.remove("active"); });
      t.classList.add("active");
      state.source = t.dataset.source;
      loadFacets().then(load);
    });
  });
  // Bind only what is on the page. This block used to dereference #geoBtn
  // unguarded while the template had no such button, so the TypeError it
  // threw killed every line after it -- including the initial load. The
  // Overview page has been sitting on "Loading..." because of it.
  function on(id, ev, fn) {
    var el = $(id);
    if (el) el.addEventListener(ev, fn);
  }
  on("applyBtn", "click", load);
  on("tableFilter", "input", render);
  on("csvBtn", "click", function () {
    window.location = "/api/export?" + params().toString();
  });
  on("geoBtn", "click", function () {
    window.location = "/api/export.geojson?" + params().toString();
  });
  document.querySelectorAll("#resultsTable thead th").forEach(function (th) {
    th.addEventListener("click", function () {
      var k = th.dataset.key;
      state.sortAsc = state.sortKey === k ? !state.sortAsc : true;
      state.sortKey = k;
      document.querySelectorAll("#resultsTable thead th").forEach(function (x) {
        x.classList.remove("sorted", "desc");
      });
      th.classList.add("sorted");
      if (!state.sortAsc) th.classList.add("desc");
      render();
    });
  });

  loadFacets().then(load);
  loadRuns();
})();

/* Full-screen map, same control as the other menus. Declared here rather
   than shared because this page builds its own Leaflet instance. */
(function () {
  var btn = document.getElementById("mapMax");
  if (!btn) return;
  function apply(on) {
    document.body.classList.toggle("mapmax", on);
    btn.classList.toggle("on", on);
    btn.textContent = on ? "Show panels" : "Expand map";
    try { window.localStorage.setItem("mapmax", on ? "1" : "0"); } catch (e) { /* private mode */ }
    var m = window.APP && window.APP.map;
    if (m) window.setTimeout(function () { m.invalidateSize(); }, 60);
  }
  var saved = "0";
  try { saved = window.localStorage.getItem("mapmax") || "0"; } catch (e) { saved = "0"; }
  apply(saved === "1");
  btn.addEventListener("click", function () {
    apply(!document.body.classList.contains("mapmax"));
  });

  // Same container watch as the other menus: any height change repaints the
  // map, rather than each caller having to remember to ask.
  var el = document.getElementById("map");
  var m = window.APP && window.APP.map;
  if (el && m && window.ResizeObserver) {
    var last = { w: 0, h: 0 }, pending = 0;
    new window.ResizeObserver(function () {
      var w = el.clientWidth, h = el.clientHeight;
      if (!w || !h || (w === last.w && h === last.h)) return;
      last.w = w; last.h = h;
      if (pending) window.clearTimeout(pending);
      pending = window.setTimeout(function () {
        pending = 0;
        m.invalidateSize({ animate: false });
      }, 80);
    }).observe(el);
  }
})();
