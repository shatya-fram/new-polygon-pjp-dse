/* POLYGON SAMPLE EXERCISE — the base map a sample is read against.

   WHY THIS IS ITS OWN PAGE AND NOT A PANEL ON PREVIEW POLYGON
   Preview Polygon answers "what does our territory look like": levels,
   roll-ups, a model built from the outlets we hold. This page answers a
   different question -- "does this proposal fit" -- and the two want
   different defaults. Here the four base layers are all on from the start,
   because the territory is the thing being compared against rather than
   optional context, and there is no model to build, no reach to tune and
   no roll-up to drill.

   It publishes the same window.PVMAP handle the preview page does, so the
   sample overlay in sample_layer.js runs here unchanged and knows nothing
   about which page it is on. */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  if (typeof L === "undefined" || !$("pvMap")) return;

  var T = window.TERRCFG || { lat: -6.25, lon: 106.95 };

  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;",
               '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function num(v) { return Number(v || 0).toLocaleString(); }

  // The four base layers, in draw order: the coarsest underneath so the
  // finest grain is the one you can still read when all four are on.
  var BASE = [
    { box: "bsKabkot", key: "kabkot",     label: "Kota / Kabupaten",
      colour: "#F2994A", weight: 2.2, dash: null,  z: 402 },
    { box: "bsMc",     key: "indosat_mc", label: "Indosat territory",
      colour: "#B388FF", weight: 2.2, dash: "7 5", z: 404 },
    { box: "bsKec",    key: "kecamatan",  label: "Kecamatan",
      colour: "#35D0E0", weight: 1.4, dash: null,  z: 406 },
    { box: "bsDesa",   key: "kelurahan",  label: "Desa / Kelurahan",
      colour: "#6EC46E", weight: 0.8, dash: null,  z: 408 }
  ];

  /* ── ONE CANVAS, NOT SEVEN ─────────────────────────────────────────────
     Each layer used to draw into its own pane, which meant its own
     <canvas>, stacked by z-index. Canvases are opaque to the mouse: the
     topmost one swallowed every hover and click before the layers under it
     saw them, so as soon as a sample's outlet dots were drawn no boundary
     could be hovered at all.

     One renderer for everything, and depth from draw order inside it.
     Leaflet hit-tests every drawn layer and takes the last match, which is
     the rule we want: a dot beats the desa under it, a desa beats the
     kecamatan under that. pvReorder() re-asserts that order after every
     add, because a layer joins at the top whenever its fetch lands. */
  var PVRENDER = null;
  var PVSTACK = ["kabkot", "indosat_mc", "kecamatan", "kelurahan", "sites"];

  /* ══════════════════════════════════════════════════════════════════════
     WHAT A BOUNDARY IS WORTH, UNDER THE CURSOR

     A hover has to answer immediately or it answers nothing: by the time a
     request comes back the cursor has moved on. So the whole desa table --
     size, masts and people for all 7,761 of them -- is fetched once and
     every hover after that is a dictionary lookup in this page. Kecamatan,
     microcluster and kabupaten totals are added up here from that same
     table, so the coarse figures can never disagree with the fine ones.

     WHERE THE NAMES ACTUALLY LIVE
     The territory endpoint lower-cases every source column, so a desa
     arrives carrying kec, kab_kot, kel_des and mc281 -- not the tidy
     `kecamatan` this file used to read, which was undefined on every desa
     in the layer. Everything goes through these accessors now.
     ══════════════════════════════════════════════════════════════════════ */
  function pvNorm(v) {
    return String(v == null ? "" : v).toUpperCase().replace(/[^A-Z0-9]/g, "");
  }
  function pvKey(d, k) { return pvNorm(d) + "|" + pvNorm(k); }
  function featName2(p) {
    return p.name || p.kel_des || p.KEL_DES || p.kec || p.Kec
        || p.kab_kot || p.KAB_KOT || p["MC IOH"] || p.MC36 || p.mc || "";
  }
  function featKec(p) { return p.kecamatan || p.kec || p.KEC || p.Kec || ""; }
  function featKab(p) { return p.kabupaten || p.kab_kot || p.KAB_KOT || ""; }
  function featMc(p) {
    return p.mc || p.mc281 || p.mc260 || p["MC IOH"] || p.MC36 || "";
  }
  function featPop(p) {
    var v = p.population != null ? p.population : p.jumlah_pen;
    if (v == null || v === "") return null;
    var n = parseFloat(v);
    return isFinite(n) ? Math.round(n) : null;
  }

  function plural(n, one, many) {
    return num(n) + " " + (n === 1 ? one : (many || one + "s"));
  }

  var PVSTATS = null, pvStatsBusy = false;
  var pvDesa = {}, pvKec = {}, pvMc = {}, pvKab = {};
  function pvAdd(into, k, r) {
    if (!k) return;
    var t = into[k] || (into[k] = { km2: 0, sites: 0, pop: 0, desa: 0 });
    t.km2 += r[4]; t.sites += r[5]; t.pop += r[6]; t.desa += 1;
  }
  function pvEnsureStats(then) {
    if (PVSTATS) { if (then) then(); return; }
    if (pvStatsBusy) return;
    pvStatsBusy = true;
    fetch("/api/samples/area-stats")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        pvStatsBusy = false;
        if (d.ok === false) return;
        PVSTATS = d.rows || [];
        PVSTATS.forEach(function (r) {
          pvDesa[pvKey(r[0], r[1])] = { km2: r[4], sites: r[5], pop: r[6],
                                        desa: 1 };
          pvAdd(pvKec, pvNorm(r[1]), r);
          
          pvAdd(pvKab, pvNorm(r[2]), r);
        });
        var mt = d.mc_totals || {};
        Object.keys(mt).forEach(function (name) {
          var v = mt[name];
          pvMc[pvNorm(name)] = { km2: v[0], sites: v[1], pop: v[2], desa: v[3] };
        });
        if (then) then();
      })
      .catch(function () { pvStatsBusy = false; });
  }
  function pvStatFor(feat, key) {
    if (!PVSTATS) return null;
    var p = feat.properties || {}, nm = featName2(p);
    if (key === "kelurahan") return pvDesa[pvKey(nm, featKec(p))] || null;
    if (key === "kecamatan") return pvKec[pvNorm(nm)] || null;
    if (key === "indosat_mc") return pvMc[pvNorm(nm)] || null;
    if (key === "kabkot") return pvKab[pvNorm(nm)] || null;
    return null;
  }

  var map = null, tiles = null;
  var loaded = {};          // key -> L.GeoJSON, fetched at most once
  var busy = {};            // key -> true while in flight
  var home = null;

  function ensureMap() {
    if (map) return map;
    map = L.map("pvMap", { zoomControl: true, preferCanvas: true })
      .setView([T.lat, T.lon], 10);
    // The ground is chosen in basemap.js, not fixed here: the CARTO default
    // this used to carry runs out of anonymous allowance and then serves
    // "API KEY REQUIRED" as a picture, which no error handler can catch.
    tiles = window.PVBASE ? window.PVBASE.attach(map) : null;
    if (tiles) tiles.mount($("pvBase"));
    BASE.forEach(function (b) {
      // The pane is still made -- other code reads its z-index -- but no
      // layer draws into it any more. See PVRENDER below.
      var p = map.createPane("bs_" + b.key);
      p.style.zIndex = b.z;
    });
    if (!PVRENDER) PVRENDER = L.canvas({ padding: .3 }).addTo(map);
    return map;
  }
  // The ground no longer follows the theme. It used to swap CARTO light for
  // CARTO dark, but the provider is now the reader's own choice and
  // overriding it on a theme change would undo that silently.

  function note(html, bad) {
    var el = $("bsNote");
    if (!el) return;
    el.innerHTML = html;
    el.style.color = bad ? "#ef8073" : "";
  }

  function summarise() {
    var on = BASE.filter(function (b) { return $(b.box).checked && loaded[b.key]; });
    if (!on.length) { note("No base layer shown."); return; }
    note(on.map(function (b) {
      return '<span class="lg"><i class="ln" style="border-top:3px '
        + (b.dash ? "dashed" : "solid") + " " + b.colour + '"></i>'
        + esc(b.label) + " <b>" + num(loaded[b.key]._count) + "</b></span>";
    }).join(" &nbsp; "));
  }

  // Boundaries come from the prebuilt cache, so all four together are a
  // handful of megabytes already simplified and gzipped rather than a
  // rebuild per request.
  function show(b, on) {
    ensureMap();
    if (!on) {
      if (loaded[b.key]) map.removeLayer(loaded[b.key]);
      summarise();
      return;
    }
    if (loaded[b.key]) { loaded[b.key].addTo(map); pvReorder(); summarise(); return; }
    if (busy[b.key]) return;
    busy[b.key] = true;
    $(b.box).disabled = true;
    note("loading " + esc(b.label) + "…");
    fetch("/api/territory/" + b.key + ".geojson")
      .then(function (r) { return r.json(); })
      .then(function (fc) {
        busy[b.key] = false;
        $(b.box).disabled = false;
        if (fc.ok === false) { note(esc(fc.error), true); return; }
        // Unticked while it was loading: honour that rather than adding a
        // layer the reader has already said they do not want.
        var g = L.geoJSON(fc, {
          renderer: PVRENDER,
          style: { color: b.colour, weight: b.weight, opacity: .85,
                   dashArray: b.dash, fillColor: b.colour, fillOpacity: 0 }
        });
        g._count = (fc.features || []).length;
        // One delegated tooltip for the layer, not one per polygon: the desa
        // layer is 7,761 shapes and a tooltip each is what stalls a tab.
        // Hover is rebuilt every time rather than bound once, because the
        // sample's own figures for this area change when a different
        // workbook is loaded and a tooltip bound at first hover would go on
        // reporting the old file's numbers.
        function tipFor(feat) {
          var p = feat.properties || {};
          var bits = ["<strong>" + esc(featName2(p)) + "</strong>",
                      "<span class='muted'>" + esc(b.label)
                      + (featKec(p) ? " · " + esc(featKec(p)) : "")
                      + "</span>"];
          var st = pvStatFor(feat, b.key);
          if (st) {
            var line = [plural(st.sites, "site"), st.km2.toFixed(2) + " km²"];
            if (b.key !== "kelurahan") line.unshift(num(st.desa) + " desa");
            bits.push(line.join(" · "));
            if (st.pop) bits.push(num(st.pop) + " people");
          } else {
            bits.push("<span class='muted'>reading area and sites…</span>");
          }
          return bits;
        }
        g.on("mouseover", function (e) {
          if (pvTarget !== "auto") return;    // the map handler owns it
          var bits = tipFor(e.layer.feature);
          var p = e.layer.feature.properties || {};
          // What the loaded workbook says about this area, if anything. It
          // is computed in the browser from rows already in memory, so it
          // costs nothing and there is no request behind a hover.
          var hint = window.PVMAP && window.PVMAP.areaHint
            ? window.PVMAP.areaHint(e.layer.feature, b.key) : null;
          if (hint) bits.push(hint);
          bits.push('<span class="muted">click for the full profile</span>');
          if (e.layer.getTooltip()) e.layer.setTooltipContent(bits.join("<br>"));
          else e.layer.bindTooltip(bits.join("<br>"),
                                   { sticky: true, className: "terr-tip" });
          e.layer.openTooltip();
          // First hover of the session pays for the table; the tooltip fills
          // itself in a moment later rather than making anybody hover twice.
          var lyr = e.layer;
          if (!PVSTATS) pvEnsureStats(function () {
            if (lyr.getTooltip() && lyr.isTooltipOpen && lyr.isTooltipOpen()) {
              var again = tipFor(lyr.feature);
              var hint2 = window.PVMAP && window.PVMAP.areaHint
                ? window.PVMAP.areaHint(lyr.feature, b.key) : null;
              if (hint2) again.push(hint2);
              again.push('<span class="muted">click for the full profile</span>');
              lyr.setTooltipContent(again.join("<br>"));
            }
          });
        });
        // The detail panel under the map listens for this. A custom event
        // rather than a direct call, so this file still knows nothing about
        // the sample overlay drawn on top of it.
        g.on("click", function (e) {
          if (pvTarget !== "auto") return;
          window.dispatchEvent(new CustomEvent("pv:areaclick", {
            detail: { feature: e.layer.feature, key: b.key, label: b.label }
          }));
        });
        loaded[b.key] = g;
        if (!$(b.box).checked) { summarise(); return; }
        g.addTo(map);
        pvReorder();
        if (!home) {
          try {
            var bb = g.getBounds();
            if (bb.isValid()) { home = bb.pad(.03); map.fitBounds(home); }
          } catch (e) { /* empty layer */ }
        }
        summarise();
      })
      .catch(function (e) {
        busy[b.key] = false;
        $(b.box).disabled = false;
        $(b.box).checked = false;
        note(esc(b.label) + " failed to load: " + esc(String(e))
             + " — tick the box to try again", true);
      });
  }

  BASE.forEach(function (b) {
    var el = $(b.box);
    if (!el) return;
    el.addEventListener("change", function () { show(b, el.checked); });
  });

  // ── site locations ─────────────────────────────────────────────────────
  // Points, not an outline, so it takes its own path: one canvas layer with
  // a delegated tooltip. 15,919 masts with a tooltip each is what stalls a
  // tab, and this page already has a sample's worth of dots on it.
  var sites = null, sitesBusy = false;
  function showSites(on) {
    ensureMap();
    if (!on) { if (sites) map.removeLayer(sites); return; }
    if (sites) { sites.addTo(map); pvReorder(); return; }
    if (sitesBusy) return;
    sitesBusy = true;
    $("bsSites").disabled = true;
    note("loading site locations…");
    fetch("/api/layer/site_locations/points.geojson")
      .then(function (r) { return r.json(); })
      .then(function (fc) {
        sitesBusy = false;
        $("bsSites").disabled = false;
        var pane = map.getPane("bs_sites")
          || (function () { var p = map.createPane("bs_sites");
                            p.style.zIndex = 410; return p; })();
        var renderer = PVRENDER;
        sites = L.geoJSON(fc, {
          pointToLayer: function (f, ll) {
            return L.circleMarker(ll, { renderer: renderer, radius: 4,
              color: "#0b0d13", weight: 1, fillColor: "#F2C94C",
              fillOpacity: .95 });
          }
        });
        sites.on("mouseover", function (e) {
          var p = e.layer.feature.properties || {};
          if (!e.layer.getTooltip()) {
            e.layer.bindTooltip("<strong>"
              + esc(p["New Site Name"] || p.name || p["New Site ID"] || "")
              + "</strong><br>" + esc(p["New Site ID"] || "")
              + (p["Site Type"] ? "<br>" + esc(p["Site Type"]) : "")
              + '<br><span class="muted">site location</span>',
              { className: "terr-tip" });
          }
          e.layer.openTooltip();
        });
        sites._count = (fc.features || []).length;
        if (!$("bsSites").checked) { summarise(); return; }
        sites.addTo(map);
        pvReorder();
        note(num(sites._count) + " site locations shown");
      })
      .catch(function (e) {
        sitesBusy = false;
        $("bsSites").disabled = false;
        $("bsSites").checked = false;
        note("site locations failed to load: " + esc(String(e)), true);
      });
  }
  if ($("bsSites")) {
    $("bsSites").addEventListener("change", function () {
      showSites($("bsSites").checked);
    });
  }

  if ($("bsHome")) {
    $("bsHome").addEventListener("click", function () {
      ensureMap();
      if (home) map.fitBounds(home);
      else map.setView([T.lat, T.lon], 10);
    });
  }

  // The sample overlay reaches the map through this and nothing else, the
  // same handle the preview page publishes -- so sample_layer.js does not
  // need to know which page it is running on.

  /* ══════════════════════════════════════════════════════════════════════
     WHICH POLYGON IS THE CLICK ASKING ABOUT?

     Everything draws into one canvas and Leaflet hands a click to the
     last-drawn layer containing the point. Right by default -- the finest
     grain wins -- but once a territory model is built its polygons sit on
     top of every boundary, and a click could only ever profile the rep. A
     reader who wanted the desa underneath got a territory instead.

     Naming the target settles it before the click. On "auto" nothing
     changes. Name a level and this takes over hover and click both: the
     point is tested against that layer's own geometry, whatever is drawn
     over it. Leaflet cannot answer that -- it only reports what it drew on
     top -- so the test is a ray-cast here over features already in memory,
     bounding-box rejected first so a hover stays instant.
     ══════════════════════════════════════════════════════════════════════ */
  var pvTarget = "auto";

  function pvRingHas(ring, x, y) {
    var inside = false;
    for (var i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      var xi = ring[i][0], yi = ring[i][1], xj = ring[j][0], yj = ring[j][1];
      if (((yi > y) !== (yj > y))
          && (x < (xj - xi) * (y - yi) / (yj - yi) + xi)) inside = !inside;
    }
    return inside;
  }
  function pvPolyHas(rings, x, y) {
    if (!rings.length || !pvRingHas(rings[0], x, y)) return false;
    for (var i = 1; i < rings.length; i++)
      if (pvRingHas(rings[i], x, y)) return false;   // a hole is outside
    return true;
  }
  function pvFeatHas(f, x, y) {
    var g = f.geometry;
    if (!g) return false;
    if (g.type === "Polygon") return pvPolyHas(g.coordinates, x, y);
    if (g.type === "MultiPolygon") {
      for (var i = 0; i < g.coordinates.length; i++)
        if (pvPolyHas(g.coordinates[i], x, y)) return true;
    }
    return false;
  }
  function pvBb(f) {
    if (f._bb !== undefined) return f._bb;
    var g = f.geometry;
    if (!g) return (f._bb = null);
    var x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    function scan(rings) {
      for (var a = 0; a < rings.length; a++)
        for (var b = 0; b < rings[a].length; b++) {
          var c = rings[a][b];
          if (c[0] < x0) x0 = c[0];
          if (c[0] > x1) x1 = c[0];
          if (c[1] < y0) y0 = c[1];
          if (c[1] > y1) y1 = c[1];
        }
    }
    if (g.type === "Polygon") scan(g.coordinates);
    else if (g.type === "MultiPolygon") g.coordinates.forEach(scan);
    else return (f._bb = null);
    return (f._bb = [x0, y0, x1, y1]);
  }

  function pvFeatures(k) {
    var out = [];
    if (k === "model") {
      var m = window.PVMAP && window.PVMAP._model;
      if (m) m.eachLayer(function (l) { if (l.feature) out.push(l.feature); });
      return out;
    }
    var g = loaded[k];
    if (g) g.eachLayer(function (l) { if (l.feature) out.push(l.feature); });
    return out;
  }
  function pvFindAt(k, ll) {
    var fs = pvFeatures(k), x = ll.lng, y = ll.lat;
    for (var i = 0; i < fs.length; i++) {
      var b = pvBb(fs[i]);
      if (!b || x < b[0] || x > b[2] || y < b[1] || y > b[3]) continue;
      if (pvFeatHas(fs[i], x, y)) return fs[i];
    }
    return null;
  }
  function pvTargetDef(k) {
    for (var i = 0; i < BASE.length; i++) if (BASE[i].key === k) return BASE[i];
    return { key: k, label: k === "model" ? "DSE territory" : k };
  }

  var pvTip = null, pvTipOn = false, pvLast = 0;
  function pvHover(e) {
    if (pvTarget === "auto") return;
    var now = Date.now();
    if (now - pvLast < 60) return;
    pvLast = now;
    var f = pvFindAt(pvTarget, e.latlng);
    if (!f) {
      if (pvTipOn) { map.closeTooltip(pvTip); pvTipOn = false; }
      return;
    }
    var html;
    if (pvTarget === "model") {
      var p = f.properties;
      html = "<strong>" + esc(p.dse) + "</strong><br>" + num(p.outlets)
        + " outlets · " + num(p.desa) + " desa · " + num(p.sites)
        + " sites<br>" + p.area_km2 + " km²<br>"
        + "<span class='muted'>click for the full profile</span>";
    } else {
      var b = pvTargetDef(pvTarget), pp = f.properties || {};
      var bits = ["<strong>" + esc(featName2(pp)) + "</strong>",
                  "<span class='muted'>" + esc(b.label)
                  + (featKec(pp) ? " · " + esc(featKec(pp)) : "") + "</span>"];
      var st = pvStatFor(f, pvTarget);
      if (st) {
        var line = [plural(st.sites, "site"), st.km2.toFixed(2) + " km²"];
        if (pvTarget !== "kelurahan") line.unshift(num(st.desa) + " desa");
        bits.push(line.join(" · "));
        if (st.pop) bits.push(num(st.pop) + " people");
      }
      var hint = window.PVMAP && window.PVMAP.areaHint
        ? window.PVMAP.areaHint(f, pvTarget) : null;
      if (hint) bits.push(hint);
      bits.push('<span class="muted">click for the full profile</span>');
      html = bits.join("<br>");
    }
    if (!pvTip) pvTip = L.tooltip({ className: "terr-tip", sticky: true });
    pvTip.setContent(html).setLatLng(e.latlng);
    if (!pvTipOn) { map.openTooltip(pvTip); pvTipOn = true; }
  }

  function pvBindTarget() {
    var m = ensureMap();
    m.on("mousemove", pvHover);
    m.on("mouseout", function () {
      if (pvTipOn) { m.closeTooltip(pvTip); pvTipOn = false; }
    });
    m.on("click", function (e) {
      if (pvTarget === "auto") return;
      var f = pvFindAt(pvTarget, e.latlng);
      if (pvTarget === "model") {
        window.dispatchEvent(new CustomEvent("pv:modelclick",
          { detail: { feature: f } }));
        return;
      }
      var b = pvTargetDef(pvTarget);
      if (!loaded[pvTarget] && !busy[pvTarget]) {
        // Asked to profile a layer that is not on: tick it and fetch it,
        // rather than answering nothing and leaving the reader wondering.
        var box = $(b.box);
        if (box) { box.checked = true; }
        show(b, true);
        return;
      }
      // A MISS IS USUALLY A GAP IN THE LAYER, NOT A MIS-CLICK
      // The kecamatan layer carries 719 names where the desa rows carry
      // 754: 36 kecamatan have desa but no boundary of their own, Tanah
      // Abang among them. Naming what does sit there turns a dead end into
      // a finding, instead of leaving somebody re-clicking a place that is
      // never going to answer.
      var under = (!f && loaded.kelurahan) ? pvFindAt("kelurahan", e.latlng) : null;
      window.dispatchEvent(new CustomEvent("pv:areaclick",
        { detail: { feature: f, key: pvTarget, label: b.label,
                    miss: !f,
                    under: under ? {
                      desa: featName2(under.properties || {}),
                      kec: featKec(under.properties || {}),
                      kab: featKab(under.properties || {}),
                      mc: featMc(under.properties || {})
                    } : null } }));
    });
  }

  if ($("pvTarget")) {
    $("pvTarget").addEventListener("change", function () {
      pvTarget = $("pvTarget").value || "auto";
      if (pvTipOn) { map.closeTooltip(pvTip); pvTipOn = false; }
    });
  }

  function pvFront(layer) {
    if (!layer) return;
    if (layer.eachLayer) layer.eachLayer(function (l) {
      if (l.bringToFront) l.bringToFront(); });
    else if (layer.bringToFront) layer.bringToFront();
  }
  function pvReorder() {
    PVSTACK.forEach(function (k) { pvFront(loaded[k]); });
    // Anything the sample overlay drew belongs on top of all of it.
    (window.PVMAP && window.PVMAP._tops || []).forEach(pvFront);
  }

  // Worth having before the first hover asks for it.
  setTimeout(pvEnsureStats, 900);
  setTimeout(pvBindTarget, 0);

  window.PVMAP = {
    ensureMap: ensureMap,
    get map() { return map; },
    esc: esc, num: num,
    fitHome: function (b) { if (b && b.isValid()) ensureMap().fitBounds(b); },
    hold: false,
    // The sample overlay draws into this same canvas rather than making its
    // own, and registers what it drew so pvReorder can keep it on top.
    get renderer() { return PVRENDER || (ensureMap(), PVRENDER); },
    _tops: [],
    setModel: function (layer) { window.PVMAP._model = layer; },
    _model: null,
    get target() { return pvTarget; },
    top: function (layer) {
      if (!layer) return;
      var t = window.PVMAP._tops;
      if (t.indexOf(layer) < 0) t.push(layer);
      pvReorder();
    },
    untop: function (layer) {
      var t = window.PVMAP._tops, i = t.indexOf(layer);
      if (i >= 0) t.splice(i, 1);
    },
    reorder: function () { pvReorder(); }
  };

  // Kecamatan and the microcluster layer first: they frame the working area
  // in about a fifth of a second each, so the map is usable before the desa
  // layer -- 7,761 polygons and the heaviest of the four -- has landed.
  ensureMap();
  var order = ["kecamatan", "indosat_mc", "kabkot", "kelurahan"];
  order.forEach(function (k, i) {
    var b = BASE.filter(function (x) { return x.key === k; })[0];
    setTimeout(function () { if ($(b.box).checked) show(b, true); }, i * 120);
  });
})();
