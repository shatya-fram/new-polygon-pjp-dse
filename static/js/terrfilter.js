/* Territory filter bar and basemap toggle, above the map.

   Two families of filter that are alternatives, not a hierarchy: Indosat
   territory (area / branch / microcluster) OR administrative (kota /
   kecamatan). Choosing in one clears the other, because a combination of
   the two is almost always empty and silently returning nothing is worse
   than saying which axis you are on.

   Runs on any page that provides #terrFilter. It also honours the query
   string, which is how "Go to area" on Preview Polygon arrives here. */
(function () {
  "use strict";
  var T = window.TMAP;
  var host = document.getElementById("terrFilter");
  if (!T || !T.map || !host) return;
  var $ = function (id) { return document.getElementById(id); };

  var INDOSAT = [["area", "Area"], ["branch", "Branch / SA"], ["mc", "Microcluster"]];
  var ADMIN = [["kabkot", "Kota / Kabupaten"], ["kecamatan", "Kecamatan"]];
  var sel = {};

  host.innerHTML =
    '<div class="tf-group"><span class="tf-tag">Indosat territory</span>'
    + INDOSAT.map(function (f) {
        return '<select id="tf_' + f[0] + '" data-fam="ind"><option value="">'
          + f[1] + ": all</option></select>"; }).join("")
    + '</div><span class="tf-or">or</span>'
    + '<div class="tf-group"><span class="tf-tag">Administrative</span>'
    + ADMIN.map(function (f) {
        return '<select id="tf_' + f[0] + '" data-fam="adm"><option value="">'
          + f[1] + ": all</option></select>"; }).join("")
    + "</div>"
    + '<button type="button" id="tfApply" class="primary small-btn">Apply &amp; zoom</button>'
    + '<button type="button" id="tfClear" class="ghost small-btn">Clear</button>'
    + '<span class="tf-spacer"></span>'
    + '<button type="button" id="tfBase" class="rtab" title="Switch the basemap">Light map</button>';

  function all() { return INDOSAT.concat(ADMIN).map(function (f) { return f[0]; }); }

  function read() {
    var out = {};
    all().forEach(function (k) {
      var el = $("tf_" + k);
      if (el && el.value) out[k] = el.value;
    });
    return out;
  }

  function fill(id, values, label) {
    var el = $("tf_" + id);
    if (!el) return;
    var keep = el.value;
    el.innerHTML = '<option value="">' + label + ": all</option>";
    values.forEach(function (v) {
      var o = document.createElement("option");
      o.value = v; o.textContent = v;
      el.appendChild(o);
    });
    if (values.indexOf(keep) >= 0) el.value = keep;
  }

  // ── request tickets ───────────────────────────────────────────────────
  // fill() keeps a select's current value only if the new option list still
  // contains it. So a superseded options response does not merely repaint a
  // stale list -- it silently clears a branch or microcluster the reader
  // has just chosen. The ticket makes the loser drop its answer instead.
  var SEQ = {};
  function ticket(k) { SEQ[k] = (SEQ[k] || 0) + 1; return SEQ[k]; }
  function isCurrent(k, n) { return SEQ[k] === n; }

  var lastOpts = null;

  function refresh() {
    var q = new URLSearchParams(read()).toString();
    // Same selection, same option lists. Arriving from "Go to area" used to
    // ask for them twice, and every Sales Area drill asks again.
    if (q === lastOpts) return Promise.resolve();
    var tk = ticket("opts");
    var busy = document.getElementById("terrFilter");
    if (busy) busy.classList.add("loading");
    return fetch("/api/rollup/options?" + q)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!isCurrent("opts", tk)) return;
        if (busy) busy.classList.remove("loading");
        if (!d.ok) return;
        lastOpts = q;
        INDOSAT.concat(ADMIN).forEach(function (f) {
          fill(f[0], d.options[f[0]] || [], f[1]);
        });
      })
      .catch(function (e) {
        if (!isCurrent("opts", tk)) return;
        if (busy) busy.classList.remove("loading");
        lastOpts = null;
        if (T.status) T.status("territory options failed: " + e);
      });
  }

  // Picking in one family clears the other: they are alternatives.
  host.querySelectorAll("select").forEach(function (el) {
    el.addEventListener("change", function () {
      if (el.value) {
        var fam = el.dataset.fam;
        host.querySelectorAll("select").forEach(function (o) {
          if (o !== el && o.dataset.fam !== fam) o.value = "";
        });
      }
      refresh();
    });
  });

  function label(f) {
    return f.mc || f.kecamatan || f.branch || f.kabkot || f.area || "All areas";
  }

  // Anything that redraws when the territory changes listens for this rather
  // than polling the selects. The territory model on the Distribution page
  // is the reason it exists: before, picking an area zoomed the map but left
  // the polygons describing the territory you had just left.
  function announce() {
    window.dispatchEvent(new CustomEvent("terr:filters",
                                         { detail: read() }));
  }

  function apply(zoom) {
    var f = read();
    T.territory = f;
    T.configChips = Object.keys(f).length
      ? [T.chip("territory", label(f), "key")]
      : [];
    var p = T.loadPois ? T.loadPois() : Promise.resolve();
    announce();
    if (!zoom || !Object.keys(f).length) { T.renderConfigBar(); return p; }
    var tk = ticket("bounds");
    return fetch("/api/rollup/bounds?" + new URLSearchParams(f).toString())
      .then(function (r) { return r.json(); })
      .then(function (d) {
        // Two Applies in flight would otherwise leave the map framed on the
        // first selection with the polygons of the second.
        if (!isCurrent("bounds", tk)) return;
        if (d.ok && d.bounds) T.map.fitBounds(d.bounds, { padding: [24, 24] });
        T.renderConfigBar();
      })
      .catch(function () { if (isCurrent("bounds", tk)) T.renderConfigBar(); });
  }

  $("tfApply").addEventListener("click", function () { apply(true); });
  $("tfClear").addEventListener("click", function () {
    host.querySelectorAll("select").forEach(function (o) { o.value = ""; });
    refresh().then(function () { apply(false); });
  });

  // ── basemap ───────────────────────────────────────────────────────────
  var BASES = {
    dark: { url: "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
            label: "Light map" },
    light: { url: "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
             label: "Dark map" }
  };
  var current = null, tiles = null;
  function setBase(which) {
    if (current === which) return;
    current = which;
    if (tiles) T.map.removeLayer(tiles);
    tiles = L.tileLayer(BASES[which].url,
      { attribution: "&copy; OpenStreetMap &copy; CARTO", maxZoom: 20 });
    tiles.addTo(T.map);
    if (tiles.bringToBack) tiles.bringToBack();
    document.body.classList.toggle("lightmap", which === "light");
    $("tfBase").textContent = BASES[which].label;
    try { window.localStorage.setItem("basemap", which); } catch (e) { /* private */ }
  }
  // Drop the tile layer territory.js added, so only one basemap is painted.
  T.map.eachLayer(function (l) {
    if (l instanceof L.TileLayer) T.map.removeLayer(l);
  });
  var saved = "dark";
  try { saved = window.localStorage.getItem("basemap") || "dark"; } catch (e) { saved = "dark"; }
  setBase(saved === "light" ? "light" : "dark");
  $("tfBase").addEventListener("click", function () {
    setBase(current === "dark" ? "light" : "dark");
  });

  // ── arriving from "Go to area" ────────────────────────────────────────
  var qs = new URLSearchParams(window.location.search);
  var incoming = {};
  all().forEach(function (k) { if (qs.get(k)) incoming[k] = qs.get(k); });
  refresh().then(function () {
    if (!Object.keys(incoming).length) return;
    Object.keys(incoming).forEach(function (k) {
      var el = $("tf_" + k);
      if (el) {
        if (![].slice.call(el.options).some(function (o) { return o.value === incoming[k]; })) {
          var o = document.createElement("option");
          o.value = incoming[k]; o.textContent = incoming[k];
          el.appendChild(o);
        }
        el.value = incoming[k];
      }
    });
    return refresh().then(function () { return apply(qs.get("zoom") === "1"); });
  });

  // Set the bar from outside — the Sales Area table drills by calling this,
  // so descending a level in the table and moving the map are one action
  // rather than two the user has to keep in sync by hand. Values that are
  // not yet in a <select> are added: the table can name a territory the
  // options list has not been refreshed for, and refusing it would silently
  // do nothing.
  T.setTerritory = function (want, zoom) {
    all().forEach(function (k) {
      var el = $("tf_" + k);
      if (!el) return;
      var v = want && want[k] ? want[k] : "";
      if (v && ![].slice.call(el.options).some(function (o) {
        return o.value === v;
      })) {
        var o = document.createElement("option");
        o.value = v; o.textContent = v;
        el.appendChild(o);
      }
      el.value = v;
    });
    return refresh().then(function () { return apply(zoom !== false); });
  };

  T.applyTerritory = apply;
  // The territory model can draw straight from the hierarchy, with no layer
  // ticked, so it needs to know what the filter bar currently holds.
  T.territoryFilters = read;
})();
