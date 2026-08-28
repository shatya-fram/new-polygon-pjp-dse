/* Preview Polygon — DSE territory polygons for whatever the page is filtered
   to, with no layer selection step.

   The Distribution page asks you to tick a layer because there the layer IS
   the subject. Here the subject is the territory, so the source is every
   uploaded outlet export at once and the filter comes from the hierarchy the
   table is already showing. Picking a level, or drilling into a row, redraws
   this map — there is nothing to upload and nothing to tick.

   "Existing" is not offered here. It means "the polygons the uploaded file
   carries", and this view has no single uploaded file to speak for: it reads
   across all of them. */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  if (!$("pvMap") || !window.L) return;

  var map = null, layer = null, dots = null, bounds = null, mode = "exclusive";
  var timer = null, on = true;
  var last = null;

  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/[&<>"']/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
      });
  }
  function num(v) {
    var n = Number(v);
    return isNaN(n) ? String(v) : Math.round(n).toLocaleString();
  }

  // A pale page wrapped around near-black tiles reads as a bug, so the
  // basemap is part of the theme rather than a separate setting.
  var tiles = null;
  function tileUrl() {
    var light = document.body.getAttribute("data-theme") === "light";
    return "https://{s}.basemaps.cartocdn.com/"
      + (light ? "light_all" : "dark_all") + "/{z}/{x}/{y}{r}.png";
  }

  // The Indosat territory and kabupaten layers are national on purpose, so
  // an outlet or a site outside the patch can be seen rather than silently
  // dropped. But this is a Jakarta Raya + West Java map: fitting to
  // everything loaded would frame the whole archipelago. Every automatic
  // fit is intersected with HOME, so the default view stays on the three
  // provinces and a national polygon is reached by panning, not by force.
  var HOME = null;                        // set from the API response
  function homeBounds() {
    return HOME ? L.latLngBounds([[HOME.minlat, HOME.minlon],
                                  [HOME.maxlat, HOME.maxlon]]) : null;
  }
  function fitHome(b, pad) {
    var h = homeBounds();
    if (!b || !b.isValid()) { if (h) map.fitBounds(h); return; }
    if (!h) { map.fitBounds(b.pad(pad == null ? .04 : pad)); return; }
    // Intersect rather than clamp each edge separately: a selection that
    // lies wholly outside the home box still gets framed on itself, which
    // is what someone who filtered to it is asking for.
    var s = b.getSouthWest(), n = b.getNorthEast();
    var lo = [Math.max(s.lat, HOME.minlat), Math.max(s.lng, HOME.minlon)];
    var hi = [Math.min(n.lat, HOME.maxlat), Math.min(n.lng, HOME.maxlon)];
    var use = (lo[0] < hi[0] && lo[1] < hi[1])
      ? L.latLngBounds(lo, hi) : b;
    map.fitBounds(use.pad(pad == null ? .04 : pad));
  }

  function ensureMap() {
    if (map) return map;
    map = L.map("pvMap", { zoomControl: true, attributionControl: true })
      .setView([-6.25, 106.95], 10);
    tiles = L.tileLayer(tileUrl(),
      { maxZoom: 19, attribution: "© OpenStreetMap © CARTO" }).addTo(map);
    return map;
  }

  // setUrl rather than a new layer: swapping layers drops the tile cache and
  // makes the map blink white before it redraws.
  window.addEventListener("theme:changed", function () {
    if (tiles) tiles.setUrl(tileUrl());
  });

  // ── request tickets ───────────────────────────────────────────────────
  // Every fetch here paints the map or the page when it lands, and the
  // requests are not fast. Click Exclusive then Coverage and both are in
  // flight; whichever the server finishes LAST wins, which is not what was
  // clicked. So a handler takes a ticket before it fetches and checks it
  // when the response arrives: a superseded answer is dropped rather than
  // painted over the newer one.
  var SEQ = {};
  function ticket(k) { SEQ[k] = (SEQ[k] || 0) + 1; return SEQ[k]; }
  function isCurrent(k, n) { return SEQ[k] === n; }

  function msg(t, bad) {
    var el = $("pvTerrMsg");
    if (!el) return;
    el.innerHTML = t;
    el.style.color = bad ? "#ef8073" : "#8b93a7";
  }

  function readout(d) {
    $("pvPolys").textContent = d ? d.drawn : "—";
    $("pvOutlets").textContent = d ? num(d.points) : "—";
    $("pvArea").textContent = d ? d.area_km2 : "—";
    $("pvOverlap").textContent = d && d.overlap_pct != null
      ? d.overlap_pct + "%" : "—";
  }

  // Only one shape is ever highlighted. Per-layer mouseout is not enough:
  // crossing a shared border can fire the neighbour's mouseover first, and
  // the shape you left keeps its highlight.
  var hot = null;
  function base(p) {
    return { color: p.colour, weight: 1.6, opacity: .95,
             fillColor: p.colour, fillOpacity: .08 };
  }
  function cool() {
    if (hot) { hot.setStyle(base(hot.feature.properties)); hot = null; }
  }

  function where() {
    var f = (window.PVSTATE && window.PVSTATE.filters) || {};
    var order = ["kecamatan", "kabkot", "mc", "branch", "area"];
    for (var i = 0; i < order.length; i++) {
      if (f[order[i]]) return { key: order[i], value: f[order[i]], all: f };
    }
    return { key: null, value: null, all: f };
  }

  function draw() {
    if (!on) return;
    var w = where();
    var q = ["mode=" + mode, "reach=" + $("pvReach").value,
             "cell=" + Math.max(100, $("pvReach").value / 2)];
    Object.keys(w.all).forEach(function (k) {
      if (w.all[k]) q.push(k + "=" + encodeURIComponent(w.all[k]));
    });
    var url = "/api/preview/territory.geojson?" + q.join("&");
    if (url === last) return;
    last = url;
    var tk = ticket("terr");

    $("pvTerrWhere").textContent = w.value
      ? "— " + w.value : "— whole region";
    msg("Building…");
    ensureMap();
    fetch(url).then(function (r) { return r.json(); }).then(function (fc) {
      // Superseded, or the reader pressed Hide while this was in flight.
      if (!isCurrent("terr", tk) || !on) return;
      if (layer) { map.removeLayer(layer); layer = null; }
      hot = null;
      if (fc.ok === false) { readout(null); msg(esc(fc.error), !fc.empty); return; }
      layer = L.geoJSON(fc, {
        style: function (f) { return base(f.properties); },
        onEachFeature: function (f, lyr) {
          var p = f.properties;
          lyr.bindTooltip("<strong>" + esc(p.dse) + "</strong><br>"
            + num(p.outlets) + " outlets · " + p.area_km2 + " km²"
            + (p.parts > 1 ? "<br>" + p.parts + " separate areas" : ""),
            { sticky: true, className: "terr-tip" });
          lyr.on("mouseover", function () {
            cool();
            hot = lyr;
            lyr.setStyle({ weight: 3.4, fillOpacity: .22 });
          });
          lyr.on("mouseout", cool);
        }
      }).addTo(map);
      map.on("mouseout", cool);
      try {
        // Not when the reader has asked to look somewhere. A focused desa,
        // an open microcluster and a picked rep are all deliberate answers
        // to "show me THIS"; the territory fetch takes a few seconds, and
        // refitting to the whole selection when it lands would pull the map
        // back out from under whoever had just zoomed in.
        var b = layer.getBounds();
        if (!focus && !mcOpen && !dsePick
            && !(window.PVMAP && window.PVMAP.hold)) fitHome(b);
      } catch (e) { /* empty selection */ }
      readout(fc);
      // The points do not depend on mode, reach or cell -- only on the
      // filters and the kind checkboxes. drawDots dedupes on its own URL,
      // so calling it here is now free when nothing about it changed,
      // instead of refetching fifteen thousand points per slider nudge.
      drawDots();
      drawBounds();
      var extra = [];
      if (fc.forcefit) {
        var ff = fc.forcefit;
        extra.push("<b>" + ff.moved + "</b> desa moved of " + ff.total_desa);
        extra.push(ff.in_norm + " of " + ff.dse + " reps inside the norms");
      }
      if (fc.seam) extra.push("<b>" + num(fc.seam) + "</b> outlets on a seam");
      // Specks folded into whoever surrounds them, so the shapes on screen
      // are the ones worth reading. Stated, not tidied away.
      if (fc.islands_absorbed)
        extra.push("<b>" + num(fc.islands_absorbed) + "</b> slivers merged"
          + " into the surrounding rep");
      if (fc.islands_kept)
        extra.push(num(fc.islands_kept) + " left standing alone");
      if (fc.unplaced) extra.push(num(fc.unplaced) + " could not be placed");
      msg("Read from <b>" + (fc.layers_used || []).length + " of "
        + fc.layers_available + "</b> uploaded outlet exports — no layer "
        + "selection needed. " + fc.drawn + " of " + fc.groups + " "
        + esc(fc.field) + " drawn at " + (fc.reach != null ? fc.reach
            : $("pvReach").value) + " m reach."
        + (extra.length ? " " + extra.join(" · ") + "." : ""));
    }).catch(function (e) { msg("Failed: " + esc(e), true); });
  }

  // Points. Outlets, sites and third-party POI each have their own toggle,
  // and each class its own colour, named and counted in the legend below the
  // map — colour alone never has to carry the meaning.
  var canvasRenderer = null;

  function kinds() {
    var k = [];
    if ($("pvKindOutlet").checked) k.push("outlet");
    if ($("pvKindSite").checked) k.push("site");
    if ($("pvKindPoi").checked) k.push("poi");
    return k;
  }

  // ── boundary outlines ──────────────────────────────────────────────────
  // Context, not subject: they go in their own Leaflet pane BELOW the DSE
  // polygons, so ticking Desa never buries the territory you came to read.
  var BND = { pvBndDesa: "kelurahan", pvBndKec: "kecamatan",
              pvBndMc: "indosat_mc" };
  var bndPane = null, lastBnd = null;

  function bndWanted() {
    return Object.keys(BND).filter(function (id) {
      var el = $(id);
      return el && el.checked;
    }).map(function (id) { return BND[id]; });
  }

  function drawBounds() {
    var want = bndWanted();
    var w = where();
    var q = ["layers=" + want.join(",")];
    Object.keys(w.all).forEach(function (k) {
      if (w.all[k]) q.push(k + "=" + encodeURIComponent(w.all[k]));
    });
    var url = "/api/preview/boundaries.geojson?" + q.join("&");

    if (!want.length) {
      if (bounds) { map.removeLayer(bounds); bounds = null; }
      lastBnd = null;
      bndNote("");
      return;
    }
    if (url === lastBnd && bounds) return;   // same request, same picture
    lastBnd = url;
    var tk = ticket("bnd");
    ensureMap();
    if (!bndPane) {
      bndPane = map.createPane("pvBounds");
      // Leaflet's overlayPane is 400; anything lower draws underneath it.
      bndPane.style.zIndex = 380;
    }
    bndNote("loading outlines…");
    fetch(url).then(function (r) { return r.json(); }).then(function (fc) {
      if (!isCurrent("bnd", tk)) return;
      if (bounds) { map.removeLayer(bounds); bounds = null; }
      // The boxes are re-read HERE, not trusted from when the request was
      // sent. Ticking Desa and unticking it again before the outlines
      // arrived used to put them on a map with every box clear, and there
      // was no way to take them off again.
      if (!bndWanted().length) { lastBnd = null; bndNote(""); return; }
      if (fc.ok === false) { lastBnd = null; bndNote(esc(fc.error)); return; }
      if (fc.home) HOME = fc.home;
      var st = fc.layers || {};
      bounds = L.geoJSON(fc, {
        pane: "pvBounds",
        style: function (f) {
          var s = st[f.properties.layer] || {};
          // A polygon whose kecamatan the hierarchy cannot place is still
          // inside the three regions -- it is a join that failed, not
          // foreign territory -- so it draws muted rather than differently
          // coloured. Anything genuinely outside the regions never reaches
          // the browser: it is stopped at import and again in the cache.
          if (f.properties.in_territory === false) {
            return { color: s.colour || "#8b93a7",
                     weight: Math.min(s.weight || 1, 1),
                     opacity: .4, dashArray: "3 4",
                     fillColor: s.colour || "#8b93a7", fillOpacity: 0 };
          }
          return { color: s.colour || "#8b93a7", weight: s.weight || 1,
                   opacity: .85, dashArray: s.dash || null,
                   fillColor: s.colour || "#8b93a7",
                   fillOpacity: 0 };     // outline only: fill would hide the map
        },
        onEachFeature: function (f, lyr) {
          var p = f.properties, s = st[p.layer] || {};
          // A microcluster outline is the handle on its roster. Only this
          // layer takes a click: a desa or a kecamatan has no DSE of its
          // own to list, and making them clickable would promise something
          // the panel could not deliver.
          if (p.layer === "indosat_mc" && p.mc) {
            lyr.on("click", function (e) {
              if (e.originalEvent) L.DomEvent.stop(e.originalEvent);
              openMc(p.mc);
            });
          }
          var bits = ["<strong>" + esc(p.name || "") + "</strong>",
                      esc(s.label || p.layer)];
          if (p.kecamatan && p.layer === "kelurahan")
            bits.push("in " + esc(p.kecamatan));
          if (p.population != null)
            bits.push(num(p.population) + " people");
          if (p.mc) bits.push(esc(p.mc));
          if (p.layer === "indosat_mc" && p.mc)
            bits.push('<span class="muted">click for the DSE roster</span>');
          if (p.region) bits.push(esc(p.region));
          if (p.in_territory === false)
            bits.push('<span style="color:' + OUTSIDE
                      + '">not matched to the territory model</span>');
          lyr.bindTooltip(bits.join("<br>"),
                          { sticky: true, className: "terr-tip" });
        }
      }).addTo(map);
      var parts = Object.keys(st).map(function (k) {
        return '<i style="border-top-color:' + (st[k].colour || "#8b93a7")
          + '"></i>' + esc(st[k].label) + " <b>" + num(st[k].count) + "</b>";
      });
      // A thinned outline is never a silent surprise.
      if (fc.simplify_m > 0)
        parts.push("simplified to " + fc.simplify_m + " m for this zoom");
      var out = (fc.features || []).filter(function (f) {
        return f.properties.in_territory === false; }).length;
      if (out)
        parts.push('<i style="border-top-color:' + OUTSIDE + '"></i>'
                   + num(out) + " not matched to the territory model");
      if (fc.home_name) parts.push("within " + esc(fc.home_name));
      bndNote(parts.join(" · "));
    }).catch(function (e) {
      if (!isCurrent("bnd", tk)) return;
      // Clear the dedupe, or the failure is permanent: every later call
      // with the same URL would be waved through as "already showing" and
      // the outlines would never be tried again.
      lastBnd = null;
      bndNote("outlines failed: " + esc(e) + " — tick the box again to retry");
    });
  }

  // Muted grey marks a row the hierarchy could not place -- a name that
  // did not join, inside the three regions. Territory outside the regions
  // has no colour here because it never arrives.
  var OUTSIDE = "#7c8598";

  function bndNote(html) {
    var el = $("pvBndNote");
    if (el) el.innerHTML = html;
  }

  // ── focus one desa ─────────────────────────────────────────────────────
  // The Desa table has 887 rows and no level below it. Clicking one used to
  // do nothing; now it brings the map to that polygon. The highlight sits in
  // its own pane ABOVE everything, because unlike the boundary layers it is
  // an answer to a question just asked, not background.
  var focus = null, focusPane = null;

  function clearFocus() {
    if (focus && map) { map.removeLayer(focus); }
    focus = null;
    var el = $("pvFocus");
    if (el) { el.hidden = true; el.innerHTML = ""; }
  }

  window.addEventListener("pv:focus", function (e) {
    var d = e.detail || {};
    if (!d.key) return;
    ensureMap();
    on = true;
    $("pvTerr").classList.remove("off");
    if (!focusPane) {
      focusPane = map.createPane("pvFocus");
      focusPane.style.zIndex = 650;        // above markers (600)
      focusPane.style.pointerEvents = "none";
    }
    var el = $("pvFocus");
    if (el) {
      el.hidden = false;
      el.innerHTML = "Showing <b>" + esc(d.label) + "</b>"
        + (d.sub ? " · " + esc(d.sub) : "")
        + ' <button type="button" id="pvFocusOff">clear</button>';
      var off = $("pvFocusOff");
      if (off) off.addEventListener("click", clearFocus);
    }
    var tk = ticket("focus");
    fetch("/api/preview/feature.geojson?layer=" + encodeURIComponent(d.layer)
          + "&key=" + encodeURIComponent(d.key))
      .then(function (r) { return r.json(); })
      .then(function (ft) {
        // Click desa A then desa B and A may still land last. Without this
        // the table highlights B while the map flies to A.
        if (!isCurrent("focus", tk)) return;
        if (ft.ok === false) {
          if (el) el.innerHTML = esc(ft.error || "could not find that polygon");
          return;
        }
        clearFocusLayerOnly();
        focus = L.geoJSON(ft, {
          pane: "pvFocus",
          style: { color: "#ffffff", weight: 3, opacity: 1,
                   fillColor: "#35d0e0", fillOpacity: .18 }
        }).addTo(map);
        if (ft.bounds) {
          // A single desa is small; without a floor the map would jump to
          // street level and lose every neighbour worth comparing it to.
          map.fitBounds(ft.bounds, { padding: [40, 40], maxZoom: 14 });
        }
        // The map is below the table on this page, so scrolling it into
        // view is part of "go and look at it".
        var m = $("pvMap");
        if (m && m.scrollIntoView)
          m.scrollIntoView({ behavior: "smooth", block: "center" });
      })
      .catch(function (err) {
        if (!isCurrent("focus", tk)) return;
        if (el) el.innerHTML = "could not load that polygon: " + esc(err);
      });
  });

  function clearFocusLayerOnly() {
    if (focus && map) { map.removeLayer(focus); focus = null; }
  }

  var lastPts = null;

  function drawDots() {
    var want = kinds();
    var leg = $("pvLegend");
    if (!want.length) {
      if (dots) { map.removeLayer(dots); dots = null; }
      lastPts = null;
      leg.hidden = true; leg.innerHTML = "";
      return;
    }
    var w = where();
    var q = ["kinds=" + want.join(","), "limit=15000"];
    Object.keys(w.all).forEach(function (k) {
      if (w.all[k]) q.push(k + "=" + encodeURIComponent(w.all[k]));
    });
    var url = "/api/preview/points.geojson?" + q.join("&");
    // Same points as last time: keep them. draw() calls this on every
    // territory rebuild, and mode, reach and cell change nothing here --
    // without this guard every nudge of the reach slider refetched fifteen
    // thousand points to draw them in exactly the same places.
    if (url === lastPts && dots) return;
    var tk = ticket("pts");
    fetch(url)
      .then(function (r) { return r.json(); })
      .then(function (fc) {
        // Dropped if superseded. The old layer is removed HERE rather than
        // at call time: removing it up front and adding the new one on
        // arrival meant two overlapping calls left the first-to-arrive
        // layer on the map with nothing referencing it, and no toggle
        // could ever take it off again.
        if (!isCurrent("pts", tk)) return;
        if (dots) { map.removeLayer(dots); dots = null; }
        if (fc.ok === false) { lastPts = null; leg.hidden = true; return; }
        lastPts = url;
        // Canvas, not SVG: ten thousand SVG circles is a slideshow.
        canvasRenderer = canvasRenderer || L.canvas({ padding: .3 });
        if (fc.home) HOME = fc.home;
        var by = {}, label = {};
        (fc.legend || []).forEach(function (l) {
          by[l.cls] = l.colour; label[l.cls] = l.label;
        });
        // One geoJSON pass and ONE tooltip for the whole layer. Building a
        // tooltip per dot meant fifteen thousand of them before the first
        // paint, which is what made this map take its time.
        dots = L.geoJSON(fc, {
          pointToLayer: function (f, ll) {
            var p = f.properties, site = p.cls === "site";
            var out = p.in_territory === false;
            var col = by[p.cls] || "#7c8598";
            return L.circleMarker(ll, {
              renderer: canvasRenderer,
              radius: site ? 5 : (p.cls === "outlet" ? 2.6 : 2.2),
              color: col, weight: site ? 1.6 : 0,
              fillColor: col, fillOpacity: out ? .45 : (site ? .95 : .8)
            });
          }
        });
        dots.on("mouseover", function (e) {
          var lyr = e.layer, p = lyr.feature.properties;
          if (!lyr.getTooltip()) {
            lyr.bindTooltip("<strong>" + esc(p.name || "") + "</strong><br>"
              + esc(label[p.cls] || p.cls)
              + (p.sub ? " · " + esc(p.sub) : "")
              + (p.dse ? "<br>" + esc(p.dse) : "")
              + (p.in_territory === false
                 ? '<br><span style="color:' + OUTSIDE
                   + '">not matched to the territory model</span>' : ""),
              { className: "terr-tip" });
          }
          lyr.openTooltip();
        });
        dots.addTo(map);

        leg.hidden = false;
        leg.innerHTML = (fc.legend || []).map(function (l) {
          return '<span class="lg"><i class="' + (l.cls === "site" ? "big" : "")
            + '" style="background:' + l.colour + '"></i>'
            + esc(l.label) + " <b>" + num(l.count) + "</b></span>";
        }).join("")
          + (fc.sampled
             ? '<span class="note">showing 1 in ' + fc.stride + " of "
               + num(fc.total) + " — narrow the selection to plot them all"
               + "</span>"
             : '<span class="note">' + num(fc.total) + " plotted, all of them</span>")
          + (function () {
              var n = (fc.features || []).filter(function (f) {
                return f.properties.in_territory === false; }).length;
              return n ? '<span class="note">' + num(n)
                + " not matched to the territory model</span>" : "";
            })();
      })
      .catch(function (e) {
        if (!isCurrent("pts", tk)) return;
        lastPts = null;            // so a retry is not deduped away
        leg.hidden = false;
        leg.innerHTML = '<span class="note">points failed to load: '
          + esc(e) + "</span>";
      });
  }

  function schedule() { clearTimeout(timer); timer = setTimeout(draw, 250); }

  $("pvModes").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-mode]");
    if (!b || b.dataset.mode === mode) return;
    mode = b.dataset.mode;
    $("pvModes").querySelectorAll("button").forEach(function (x) {
      var isOn = x.dataset.mode === mode;
      x.classList.toggle("on", isOn);
      x.setAttribute("aria-checked", isOn ? "true" : "false");
    });
    last = null;
    draw();
  });
  $("pvReach").addEventListener("input", function (e) {
    $("pvReachVal").textContent = e.target.value + " m";
    last = null;
    schedule();
  });
  // Ticking three boxes in a row is one decision, not three. Without a
  // coalescing window it asked for kelurahan, then kelurahan+kecamatan,
  // then all three -- and desa is the heaviest layer of the lot, so two of
  // those three requests were pure waste. 140 ms is invisible on a single
  // click and removes the burst entirely.
  var bndTimer = null, dotTimer = null;
  Object.keys(BND).forEach(function (id) {
    var el = $(id);
    if (el) el.addEventListener("change", function () {
      clearTimeout(bndTimer);
      bndTimer = setTimeout(drawBounds, 140);
    });
  });

  ["pvKindOutlet", "pvKindSite", "pvKindPoi"].forEach(function (id) {
    $(id).addEventListener("change", function () {
      clearTimeout(dotTimer);
      dotTimer = setTimeout(function () { if (map) drawDots(); }, 140);
    });
  });
  $("pvTerrDraw").addEventListener("click", function () {
    on = true;
    $("pvTerr").classList.remove("off");
    last = null;
    setTimeout(function () { ensureMap().invalidateSize(); draw(); }, 30);
  });
  $("pvTerrHide").addEventListener("click", function () {
    on = false;
    $("pvTerr").classList.add("off");
    // Cancel what is pending and disown what is already in flight, so a
    // response landing after Hide cannot put layers back on a hidden map.
    clearTimeout(timer);
    ticket("terr"); ticket("pts"); ticket("bnd");
  });

  // preview.js owns the filter state; it announces a change rather than this
  // file polling for one.
  window.addEventListener("pv:filters", function () {
    last = null;
    lastBnd = null;          // a new selection means different outlines
    clearFocus();            // and the focused desa may not be in it
    schedule();
  });

  // Clear resets the controls this file owns. preview.js resets the level and
  // the filters and then fires this, so one click puts the whole page back to
  // how it opened rather than leaving the map on settings from an analysis
  // that has just been abandoned.
  window.addEventListener("pv:clear", function () {
    mode = "exclusive";
    $("pvModes").querySelectorAll("button").forEach(function (x) {
      var isOn = x.dataset.mode === mode;
      x.classList.toggle("on", isOn);
      x.setAttribute("aria-checked", isOn ? "true" : "false");
    });
    $("pvReach").value = 400;
    $("pvReachVal").textContent = "400 m";
    $("pvKindOutlet").checked = true;
    $("pvKindSite").checked = true;
    $("pvKindPoi").checked = false;      // POI is opt-in: 4,397 of them
    on = true;
    $("pvTerr").classList.remove("off");
    ["pvBndDesa", "pvBndKec", "pvBndMc"].forEach(function (id) {
      if ($(id)) $(id).checked = false;
    });
    cool();
    if (dots) { map.removeLayer(dots); dots = null; }
    if (bounds && map) { map.removeLayer(bounds); bounds = null; }
    lastBnd = null;
    bndNote("");
    clearFocus();
    if (layer && map) { map.removeLayer(layer); layer = null; }
    closeMc();
    // Anything already asked for belongs to the view being cleared.
    clearTimeout(timer);
    ticket("terr"); ticket("pts"); ticket("bnd"); ticket("focus");
    lastPts = null;
    readout(null);
    // The pv:filters that preview.js fires next would be deduped against the
    // URL we last drew, and after a clear that URL is often the same one.
    last = null;
  });


  // ══════════════════════════════════════════════════════════════════════
  // THE MICROCLUSTER ROSTER
  //
  // Two zoom levels, because the question has two levels. Opening a
  // microcluster frames the microcluster -- read from its own polygon, not
  // from where its outlets happen to fall, so a rep working one corner does
  // not make the whole MC look like that corner. Picking a rep from the
  // roster frames that rep's territory polygon, the one already drawn on
  // the map, so what you zoom to is the shape the model actually produced
  // rather than a second opinion computed for the panel.
  // ══════════════════════════════════════════════════════════════════════
  var mcOpen = null, mcData = null, dsePick = null, dseRing = null, dsePane = null;

  function mcEl() { return $("pvMc"); }

  // 1,151,687 people is "1.15M", not "1152k". A microcluster header is read
  // at a glance and the exact figure is not the point at that size.
  function pop(n) {
    if (n == null) return "—";
    if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
    if (n >= 1e4) return Math.round(n / 1e3) + "k";
    return num(n);
  }

  function closeMc() {
    mcOpen = null; mcData = null;
    clearDsePick();
    var el = mcEl();
    if (el) { el.hidden = true; el.innerHTML = ""; }
    if (map) map.invalidateSize();
  }

  function clearDsePick() {
    dsePick = null;
    if (dseRing && map) { map.removeLayer(dseRing); dseRing = null; }
    var el = mcEl();
    if (el) el.querySelectorAll(".pv-dse.on").forEach(function (b) {
      b.classList.remove("on"); });
  }

  function openMc(mc) {
    if (!mc) return;
    ensureMap();
    var el = mcEl();
    if (!el) return;
    // Already showing this one: do not refetch, but do take the map back
    // there. Clicking a microcluster you have panned away from and getting
    // nothing at all reads as a broken control.
    if (mcOpen === mc) {
      if (mcData && mcData.bounds) {
        clearDsePick();
        fitHome(L.latLngBounds(mcData.bounds), .06);
      }
      return;
    }
    mcOpen = mc;
    // Show the outline of the thing being read about. Reading a roster for
    // a microcluster you cannot see the edge of is half an answer, and the
    // tick stays on afterwards so the reader can turn it off if they want.
    // Tick the box, but let the redraw that the filter change is about to
    // trigger pick it up. Calling drawBounds() here as well fetched the
    // outlines twice for one click -- once with the old filter set and
    // once with the new -- and the two raced into the same layer.
    var box = $("pvBndMc");
    if (box && !box.checked) {
      box.checked = true;
      setTimeout(function () { if ($("pvBndMc").checked) drawBounds(); }, 60);
    }
    el.hidden = false;
    el.innerHTML = '<div class="pv-mc-empty">reading ' + esc(mc) + '…</div>';
    map.invalidateSize();
    fetch("/api/preview/mc.json?mc=" + encodeURIComponent(mc))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (mcOpen !== mc) return;        // another one was clicked meanwhile
        if (!d.ok) {
          el.innerHTML = '<div class="pv-mc-empty">' + esc(d.error || "not found")
            + '</div>';
          return;
        }
        mcData = d;
        renderMc(d);
        if (d.bounds) fitHome(L.latLngBounds(d.bounds), .06);
      })
      .catch(function (e) {
        el.innerHTML = '<div class="pv-mc-empty">could not read it: '
          + esc(e) + "</div>";
      });
  }

  function renderMc(d) {
    var t = d.totals || {};
    var path = [d.branch, d.area, d.region].filter(Boolean).map(esc).join(" › ");
    var h = ['<div class="pv-mc-head">',
             '<button type="button" class="x" id="pvMcClose">close</button>',
             "<h3>", esc(d.mc), "</h3>",
             '<div class="path">', path, "</div>",
             "</div>",
             '<div class="pv-mc-stats">',
             "<div><b>", num(t.dse), "</b><span>DSE</span></div>",
             "<div><b>", num(t.outlets), "</b><span>outlets</span></div>",
             "<div><b>", num(t.desa), "</b><span>desa</span></div>",
             "<div><b>", num(t.kecamatan), "</b><span>kecamatan</span></div>",
             "<div><b>", pop(t.population), "</b><span>people</span></div>",
             "<div><b>", (t.dse ? (t.outlets / t.dse).toFixed(0) : "—"),
             "</b><span>outlets/DSE</span></div>",
             "</div>"];
    if (t.unassigned)
      h.push('<div class="pv-mc-note">', num(t.unassigned),
             " outlets here carry no DSE code.</div>");
    h.push('<div class="pv-mc-note">Pick a rep to zoom to their territory.</div>');

    var top = (d.dse[0] && d.dse[0].outlets) || 1;
    d.dse.forEach(function (r, i) {
      var brands = (r.brands || []).map(function (b) { return b.value; }).join(", ");
      var meta = [brands, r.partner, r.kecamatan + " kec"].filter(Boolean);
      h.push('<button type="button" class="pv-dse" data-i="', i, '">',
             '<div class="code">', esc(r.dse), "</div>",
             '<div class="figs"><span><b>', num(r.outlets), "</b> outlets</span>",
             "<span><b>", num(r.desa), "</b> desa</span>",
             (r.sites != null ? "<span><b>" + num(r.sites) + "</b> sites</span>" : ""),
             "<span>", r.share, "%</span></div>",
             '<div class="meta">', esc(meta.join(" · ")), "</div>",
             '<div class="bar"><i style="width:',
             Math.max(2, Math.round(r.outlets * 100 / top)), '%"></i></div>',
             "</button>");
    });
    if (!d.dse.length)
      h.push('<div class="pv-mc-empty">No outlets in this microcluster carry '
             + "a DSE code.</div>");

    var el = mcEl();
    el.innerHTML = h.join("");
    el.querySelector("#pvMcClose").addEventListener("click", closeMc);
    el.querySelectorAll(".pv-dse").forEach(function (b) {
      b.addEventListener("click", function () { pickDse(+b.dataset.i, b); });
    });
  }

  // Zoom to the rep's own territory polygon. The polygons on screen are
  // keyed by DSE code, so the shape is already here -- no second request,
  // and no chance of framing a shape different from the one drawn. If this
  // rep has no polygon in the current model (too few outlets to build one,
  // or the selection excludes them) the outlet envelope is the honest
  // fallback, and it says so.
  function dseShapes(code) {
    var hits = [];
    if (layer) layer.eachLayer(function (l) {
      if (l.feature && l.feature.properties
          && String(l.feature.properties.dse) === String(code)) hits.push(l);
    });
    return hits;
  }

  function pickDse(i, btn) {
    if (!mcData || !mcData.dse[i]) return;
    var r = mcData.dse[i];
    if (dsePick === r.dse) { clearDsePick(); return; }   // click again to drop
    clearDsePick();
    dsePick = r.dse;
    if (btn) btn.classList.add("on");
    ensureMap();
    if (!dsePane) {
      dsePane = map.createPane("pvDse");
      dsePane.style.zIndex = 640;
      dsePane.style.pointerEvents = "none";
    }
    var shapes = dseShapes(r.dse), b = null;
    if (shapes.length) {
      shapes.forEach(function (l) {
        b = b ? b.extend(l.getBounds()) : L.latLngBounds(l.getBounds().getSouthWest(),
                                                         l.getBounds().getNorthEast());
      });
      var fc = { type: "FeatureCollection",
                 features: shapes.map(function (l) { return l.feature; }) };
      // Two strokes, not one. A single white outline vanishes on the light
      // basemap and a single dark one vanishes on the dark; a dark casing
      // under a white core reads on both, and against the twelve hundred
      // other coloured polygons it is the only shape wearing either.
      dseRing = L.layerGroup([
        L.geoJSON(fc, { pane: "pvDse",
          style: { color: "#0b0d13", weight: 7, opacity: .45,
                   fill: false, lineJoin: "round" } }),
        L.geoJSON(fc, { pane: "pvDse",
          style: { color: "#ffffff", weight: 2.5, opacity: 1,
                   fillColor: "#35d0e0", fillOpacity: .22 } })
      ]).addTo(map);
    } else if (r.bounds) {
      b = L.latLngBounds(r.bounds);
      dseRing = L.layerGroup([
        L.rectangle(b, { pane: "pvDse", color: "#0b0d13", weight: 5,
                         opacity: .4, fill: false }),
        L.rectangle(b, { pane: "pvDse", color: "#ffffff", weight: 2,
                         dashArray: "5 4", fillColor: "#35d0e0",
                         fillOpacity: .08 })
      ]).addTo(map);
    }
    if (b && b.isValid())
      map.fitBounds(b.pad(.25), { maxZoom: 15 });
    msg("<b>" + esc(r.dse) + "</b> — " + num(r.outlets) + " outlets across "
        + num(r.desa) + " desa"
        + (shapes.length ? "" : " (no territory polygon in this model — "
                                + "showing where the outlets are)"));
  }

  // The roster is about a microcluster, and a new selection is a new
  // microcluster or none. Leaving it open would leave a roster on screen
  // that the map no longer shows.
  window.addEventListener("pv:filters", function () {
    var f = (window.PVSTATE && window.PVSTATE.filters) || {};
    if (mcOpen && f.mc !== mcOpen) closeMc();
  });
  window.addEventListener("pv:mc", function (e) {
    openMc(e.detail && e.detail.mc);
  });

  // preview.js loads the roll-up and then fires pv:filters, which schedules
  // the first draw with the filters already known. This timer is only the
  // fallback for a roll-up that fails or is slow -- if the real draw has
  // already happened it does nothing, rather than racing a second copy of
  // every request with an empty filter set.
  // The map on this page lives in this closure, which is right for
  // everything that draws territory -- but the sample overlay is a separate
  // concern in a separate file and it needs somewhere to draw. This is the
  // whole of what it is allowed to reach: the map, and the two formatters,
  // so a sample cannot touch the layers it is drawn over.
  // `hold` is how another file says "the reader is looking at something I
  // put there". A sample overlay sets it, and the territory fetch -- which
  // takes seconds and refits the map when it lands -- leaves the view alone
  // while it is set, the same way it does for a focused desa.
  window.PVMAP = {
    ensureMap: ensureMap,
    get map() { return map; },
    esc: esc, num: num, fitHome: fitHome,
    hold: false
  };
  window.dispatchEvent(new CustomEvent("pv:mapready"));

  function boot() { if (!last) draw(); }
  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", function () { setTimeout(boot, 900); });
  else setTimeout(boot, 900);
})();
