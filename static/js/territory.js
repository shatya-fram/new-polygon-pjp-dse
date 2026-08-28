/* Shared map core for Retail Gapura and Distribution Polygon.
   Boundaries, service points, the catalogue, POI styling and the legend.
   Page differences are driven by window.TERR.mode, not by a second copy. */
window.TMAP = (function () {
  "use strict";
  var T = window.TERR;
  var MODE = T.mode || "retail";
  var $ = function (id) { return document.getElementById(id); };
  var map, boundary = {}, spLayer, poiLayer, highlight;
  var meta = { layers: [], operators: [], sources: T.sources || {} };
  var opOn = {}, spFeatures = [];
  var api = {};

  // Marker style is user-chosen now, so it lives in one place and both the
  // map and the legend read from it — they cannot disagree about what a
  // given layer looks like.
  var style = { color: "#f0d04b", shape: "circle", size: 7 };

  function status(t) { if ($("status")) $("status").textContent = t; }
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

  // ── shapes ────────────────────────────────────────────────────────────
  function shapeSvg(shape, color, s) {
    var h = s / 2, st = 'stroke="#0b0d13" stroke-width="1"';
    if (shape === "square")
      return '<rect x="1" y="1" width="' + (s - 2) + '" height="' + (s - 2) +
             '" fill="' + color + '" ' + st + '/>';
    if (shape === "triangle")
      return '<polygon points="' + h + ',0.5 ' + (s - 0.5) + ',' + (s - 0.5) +
             ' 0.5,' + (s - 0.5) + '" fill="' + color + '" ' + st + '/>';
    if (shape === "diamond")
      return '<polygon points="' + h + ',0.5 ' + (s - 0.5) + ',' + h + ' ' + h +
             ',' + (s - 0.5) + ' 0.5,' + h + '" fill="' + color + '" ' + st + '/>';
    return '<circle cx="' + h + '" cy="' + h + '" r="' + (h - 1) +
           '" fill="' + color + '" ' + st + '/>';
  }
  function swatch(shape, color, s) {
    s = s || 12;
    return '<svg class="swtch" width="' + s + '" height="' + s + '" viewBox="0 0 ' +
           s + ' ' + s + '">' + shapeSvg(shape, color, s) + "</svg>";
  }
  function logoIcon(url, color, s) {
    s = s || 22;
    return L.divIcon({
      html: '<span class="oplogo" style="width:' + s + "px;height:" + s
            + "px;border-color:" + color + '"><img src="' + url
            + '" alt="" loading="lazy"></span>',
      className: "poi-mk", iconSize: [s, s], iconAnchor: [s / 2, s / 2]
    });
  }
  api.logoIcon = logoIcon;

  function opMark(code) {
    // One place decides how an operator is drawn, so the map and the legend
    // cannot disagree about whether a logo or a dot is in use.
    var m = (T.operators || {})[code] || {};
    return { url: m.icon_url || null, color: m.color || "#8b93a7",
             label: m.label || code };
  }
  api.opMark = opMark;

  function icon(shape, color, s) {
    return L.divIcon({
      html: '<svg width="' + s + '" height="' + s + '">' +
            shapeSvg(shape, color, s) + "</svg>",
      className: "poi-mk", iconSize: [s, s], iconAnchor: [s / 2, s / 2]
    });
  }
  // circleMarker is markedly cheaper than a divIcon, so circles keep the
  // fast path and only the other shapes pay for SVG.
  function place(latlng, shp, color, s, tip) {
    var m = (shp === "circle")
      ? L.circleMarker(latlng, { radius: s / 2, color: "#0b0d13", weight: .6,
                                 fillColor: color, fillOpacity: .9 })
      : L.marker(latlng, { icon: icon(shp, color, s) });
    if (tip) m.bindTooltip(tip, { className: "terr-tip" });
    return m;
  }

  // Bulk point rendering. A layer of 70k outlets cannot afford a marker
  // object plus a tooltip plus a rendered popup each; this builds one
  // canvas circleMarker per point and defers the HTML until the cursor
  // or a click actually asks for it.
  function placeMany(features, shp, color, s, tipFn, popFn) {
    var g = L.geoJSON({ type: "FeatureCollection", features: features || [] }, {
      pointToLayer: function (f, ll) {
        return (shp === "circle" || (features || []).length > 2000)
          ? L.circleMarker(ll, { radius: s / 2, color: "#0b0d13", weight: .6,
                                 fillColor: color, fillOpacity: .9 })
          : L.marker(ll, { icon: icon(shp, color, s) });
      }
    });
    if (tipFn) {
      g.on("mouseover", function (e) {
        var lyr = e.layer;
        if (!lyr.getTooltip()) {
          lyr.bindTooltip(tipFn(lyr.feature.properties), { className: "terr-tip" });
        }
        lyr.openTooltip();
      });
    }
    if (popFn) {
      g.on("click", function (e) {
        var lyr = e.layer;
        if (!lyr.getPopup()) lyr.bindPopup(popFn(lyr.feature.properties, lyr));
        lyr.openPopup();
      });
    }
    return g;
  }

  // ── map ───────────────────────────────────────────────────────────────
  if (typeof L === "undefined") {
    $("map").innerHTML = '<div class="map-fallback">Leaflet could not load — '
      + "the map is unavailable. The /api/territory endpoints still work.</div>";
    status("map unavailable");
    return {};
  }
  map = L.map("map", { preferCanvas: true }).setView([T.lat, T.lon], 11);
  L.tileLayer("https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
    { attribution: "&copy; OpenStreetMap &copy; CARTO", maxZoom: 20 }).addTo(map);
  highlight = L.layerGroup().addTo(map);
  api.map = map;
  api.esc = esc;
  api.num = num;
  api.swatch = swatch;
  api.place = place;
  api.placeMany = placeMany;
  api.status = status;
  // Future GAPURA repaints the kecamatan polygons by score, so it needs the
  // layers themselves rather than a second copy of the same GeoJSON.
  api.boundary = boundary;

  // ── boundaries ────────────────────────────────────────────────────────
  function bstyle(st) {
    return { color: st.color, weight: st.weight, opacity: .95,
             dashArray: st.dash || null, fillColor: st.color,
             fillOpacity: st.fill, interactive: true };
  }
  api.bstyle = bstyle;
  function tip(p) {
    var b = ["<strong>" + esc(p.name) + "</strong>"];
    if (p.layer === "kelurahan") {
      b.push(esc([p.kecamatan, p.kabkot].filter(Boolean).join(" · ")));
      if (p.population != null) b.push("Pop " + num(p.population)
        + (p.pop_density ? " · " + num(p.pop_density) + "/km²" : ""));
      if (p.geo_type) b.push(esc(p.geo_type));
      if (p.mc) b.push(esc(p.mc) + (p.branch ? " · " + esc(p.branch) : ""));
      return b.join("<br>");
    }
    if (p.kabkot) b.push(esc(p.kabkot));
    if (p.branch) b.push(esc(p.branch));
    if (p.kec_count) b.push(p.kec_count + " kecamatan");
    if (p.vlr != null) b.push("VLR " + num(p.vlr));
    if (p.rev != null) b.push("Rev " + (Number(p.rev) / 1e9).toFixed(2) + " Bn");
    return b.join("<br>");
  }
  function loadBoundary(key, st) {
    return fetch("/api/territory/" + key + ".geojson")
      .then(function (r) { return r.json(); })
      .then(function (fc) {
        // One tooltip and one pair of hover handlers for the whole layer.
        // Binding them per polygon costs ~4 objects x N features, which is
        // what made a 7,700-desa layer freeze the tab before it drew.
        var base = bstyle(st);
        var layer = L.geoJSON(fc, { style: function () { return bstyle(st); } });
        layer.on("mouseover", function (e) {
          var lyr = e.layer, b = lyr._baseStyle || base;
          lyr.setStyle({ weight: (b.weight || st.weight) + 2,
                         fillOpacity: (b.fillOpacity == null
                                       ? st.fill : b.fillOpacity) + .12 });
          if (!lyr.getTooltip()) {
            lyr.bindTooltip(tip(lyr.feature.properties),
                            { sticky: true, className: "terr-tip" });
          }
          lyr.openTooltip(e.latlng);
        });
        layer.on("mouseout", function (e) {
          var lyr = e.layer;
          lyr.setStyle(lyr._baseStyle || base);
          lyr.closeTooltip();
        });
        boundary[key] = { layer: layer, style: st, count: fc.features.length };
        return fc.features.length;
      });
  }

  map.on("click", function (e) {
    if (!$("panel")) return;
    highlight.clearLayers();
    L.circleMarker(e.latlng, { radius: 6, color: "#fff", weight: 2,
                               fillColor: "#fff", fillOpacity: .25 }).addTo(highlight);
    if (api.showPane && document.getElementById("paneSel")) showPane("paneSel");
    $("panel").innerHTML = '<div class="panel-empty muted">reading…</div>';
    var tk = ticket("at");
    fetch("/api/territory/at?lat=" + e.latlng.lat + "&lon=" + e.latlng.lng)
      .then(function (r) { return r.json(); })
      .then(function (d) { if (isCurrent("at", tk)) renderPanel(d); })
      .catch(function (err) {
        if (!isCurrent("at", tk)) return;
        $("panel").innerHTML = '<div class="panel-empty muted">Failed: ' + esc(err) + "</div>";
      });
  });

  function renderPanel(d) {
    if (!d.ok) { $("panel").innerHTML = '<div class="panel-empty muted">' + esc(d.error) + "</div>"; return; }
    if (!d.hits.length) {
      $("panel").innerHTML = '<div class="panel-empty muted">No desa, '
        + "kecamatan or microcluster polygon covers that point — it is outside "
        + "the boundary files you uploaded.</div>";
      return;
    }
    var html = '<div class="hit-grid">';
    d.hits.forEach(function (h) {
      html += '<div class="hit" style="border-left-color:' + h.style.color + '">';
      html += "<h3>" + esc(h.title) + "</h3>";
      html += '<div class="sub">' + esc(h.style.label)
        + (h.subtitle ? " · " + esc(h.subtitle) : "") + "</div>";
      if (h.profile_missing) {
        html += '<div class="muted small">No profile figures for this area.</div>';
      } else {
        html += "<table>";
        h.rows.forEach(function (r) {
          var v = r.value, txt;
          if (v == null) txt = "—";
          else if (r.fmt === "idr") txt = (Number(v) / 1e9).toFixed(2) + " Bn";
          else if (r.fmt === "pct") txt = (Number(v) * 100).toFixed(1) + "%";
          else if (r.fmt === "km2") txt = Number(v).toFixed(2) + " km²";
          else txt = num(v);
          html += "<tr><td>" + esc(r.label) + '</td><td class="v">' + txt
            + '</td><td class="p">' + esc(r.period || "") + "</td></tr>";
        });
        html += "</table>";
        if (h.shares.length) {
          var tot = h.shares.reduce(function (a, s) {
            return a + (s.operator ? Number(s.value) || 0 : 0); }, 0) || 1;
          html += '<div class="shares"><div class="share-bar">';
          h.shares.forEach(function (s) {
            if (!s.operator) return;
            html += '<i style="width:' + ((Number(s.value) || 0) / tot * 100).toFixed(2)
              + "%;background:" + s.color + '" title="' + esc(s.label) + '"></i>';
          });
          html += "</div>";
          h.shares.forEach(function (s) {
            html += '<div class="share-row"><span class="dot" style="background:'
              + (s.color || "#c8cde0") + '"></span><span class="k">' + esc(s.label)
              + '</span><span class="v">' + (Number(s.value) * 100).toFixed(1)
              + "%</span></div>";
          });
          html += "</div>";
        }
      }
      if (h.note) {
        html += '<div class="derived">' + esc(h.note) + "</div>";
      }
      if (h.derived_from) {
        html += '<div class="derived">Derived from ' + h.derived_from
          + " kecamatan — counts summed, shares weighted by VLR subs.</div>";
      }
      html += "</div>";
    });
    if (d.service_points && d.service_points.length && MODE === "retail") {
      html += '<div class="hit" style="border-left-color:#8b93a7"><h3>Service points here</h3>'
        + '<div class="sub">inside the polygon you clicked</div><div class="sp-tally">';
      d.service_points.forEach(function (s) {
        html += '<span><i class="dot" style="background:' + s.color + '"></i>'
          + esc(s.label) + " <b>" + s.count + "</b></span>";
      });
      html += "</div></div>";
    }
    $("panel").innerHTML = html + "</div>";
  }

  // ── service points (Retail only) ──────────────────────────────────────
  function drawServicePoints() {
    if (MODE !== "retail") return 0;
    if (spLayer) map.removeLayer(spLayer);
    spLayer = L.layerGroup();
    var n = 0;
    spFeatures.forEach(function (f) {
      var op = f.properties.operator || "OTHER";
      if (!opOn[op]) return;
      var mk = opMark(op);
      var ll = [f.geometry.coordinates[1], f.geometry.coordinates[0]];
      var tipHtml = "<strong>" + esc(f.properties.name) + "</strong><br>"
        + esc(mk.label)
        + (f.properties.sp_type ? "<br>" + esc(f.properties.sp_type) : "");
      var m = mk.url
        ? L.marker(ll, { icon: logoIcon(mk.url, mk.color, 22) })
            .bindTooltip(tipHtml, { className: "terr-tip" })
        : place(ll, "circle", mk.color, 10, tipHtml);
      m.addTo(spLayer);
      n++;
    });
    spLayer.addTo(map);
    return n;
  }

  // ── catalogue → POI layer ─────────────────────────────────────────────
  function fill(sel, values, allLabel) {
    if (!sel) return;
    var keep = sel.value;
    sel.innerHTML = '<option value="">' + allLabel + "</option>";
    values.forEach(function (v) {
      var o = document.createElement("option");
      o.value = v.value;
      o.textContent = v.value + " (" + v.count.toLocaleString() + ")";
      sel.appendChild(o);
    });
    if (values.some(function (v) { return v.value === keep; })) sel.value = keep;
  }

  function loadCatalog() {
    var src = $("poiSource") ? $("poiSource").value : "unified";
    var tk = ticket("catalog");
    return fetch("/api/catalog?source=" + src)
      .then(function (r) { return r.json(); })
      .then(function (d) {
        // fill() keeps the current selection only if the new list still
        // contains it, so a stale catalogue silently clears a category the
        // reader has just picked.
        if (!isCurrent("catalog", tk)) return d;
        if ($("sourceNote") && meta.sources[src])
          $("sourceNote").textContent = meta.sources[src].note || "";
        fill($("poiCategory"), d.categories || [], "All categories");
        fill($("poiBrand"), d.brands || [], "All brands");
        fill($("poiMc"), d.mc || [], "All microclusters");
        return d;
      })
      .catch(function (e) {
        if (isCurrent("catalog", tk)) status("catalogue failed: " + esc(e));
        return {};
      });
  }
  api.loadCatalog = loadCatalog;

  function poiQuery() {
    var p = new URLSearchParams({ source: $("poiSource").value });
    [["poiCategory", "category"], ["poiBrand", "brand"], ["poiMc", "mc"]]
      .forEach(function (pair) {
        var el = $(pair[0]);
        if (el && el.value) p.set(pair[1], el.value);
      });
    // The filter bar above the map, when the page has one.
    Object.keys(api.territory || {}).forEach(function (k) {
      if (api.territory[k]) p.set(k, api.territory[k]);
    });
    return p;
  }
  api.territory = {};
  api.poiQuery = poiQuery;

  // ── request tickets ───────────────────────────────────────────────────
  // A handler takes a ticket before it fetches and checks it when the
  // response lands. Without this, two overlapping requests are resolved by
  // whichever the server finishes last, not by whichever was asked for
  // last -- and the loser's Leaflet layer is left on the map with nothing
  // referencing it, so no toggle can ever remove it.
  var SEQ = {};
  function ticket(k) { SEQ[k] = (SEQ[k] || 0) + 1; return SEQ[k]; }
  function isCurrent(k, n) { return SEQ[k] === n; }

  // The last collection fetched, kept so a change of colour, shape or size
  // can redraw from it instead of asking the server for the same six
  // thousand POIs again. Nothing about the marker style is in the query.
  var poiFc = null;

  function paintPois(fc) {
    if (poiLayer) { map.removeLayer(poiLayer); poiLayer = null; }
    poiLayer = placeMany(fc.features, style.shape, style.color, style.size,
      function (props) {
        var extra = props.n_sources > 1
          ? '<br><span class="muted">' + props.n_sources + " sources · "
            + esc(props.sources_seen || "") + "</span>" : "";
        return "<strong>" + esc(props.name) + "</strong><br>"
          + esc(props.brand || props.category || "") + extra;
      });
    poiLayer.addTo(map);
    api.poiCount = fc.features.length;
    renderLegend();
  }

  // Restyle from what is already here. Nine colours used to mean nine
  // identical six-thousand-feature fetches and up to eight stranded layers.
  function restylePois() {
    if (poiFc) { paintPois(poiFc); status(
      api.poiCount.toLocaleString() + " POIs shown"); }
    else renderLegend();
  }
  api.restylePois = restylePois;

  function loadPois() {
    var p = poiQuery();
    status("loading POIs…");
    var tk = ticket("poi");
    return fetch("/api/territory/poi.geojson?" + p.toString() + "&limit=6000")
      .then(function (r) { return r.json(); })
      .then(function (fc) {
        if (!isCurrent("poi", tk)) return;
        if (fc.ok === false) {
          if (poiLayer) { map.removeLayer(poiLayer); poiLayer = null; }
          poiFc = null; status(fc.error); renderLegend(); return;
        }
        poiFc = fc;
        paintPois(fc);
        status(fc.features.length.toLocaleString() + " POIs shown");
        return fc;
      })
      .catch(function (e) {
        if (!isCurrent("poi", tk)) return;
        poiFc = null;
        status("POIs failed to load: " + esc(e));
        renderLegend();
      });
  }
  api.loadPois = loadPois;
  api.hidePois = function () {
    ticket("poi");            // a request in flight belongs to the old view
    if (poiLayer) { map.removeLayer(poiLayer); poiLayer = null; }
    poiFc = null;
    api.poiCount = 0;
    status("POIs hidden"); renderLegend();
  };

  // ── legend, docked under the map ──────────────────────────────────────
  function currentPoiLabel() {
    var cat = $("poiCategory") && $("poiCategory").value;
    var brand = $("poiBrand") && $("poiBrand").value;
    var src = $("poiSource") ? $("poiSource").value : "";
    var what = brand || cat || "all categories";
    return (meta.sources[src] ? meta.sources[src].label : src) + " · " + what;
  }

  function renderLegend() {
    var el = $("legend");
    if (!el) return;
    var items = [];
    Object.keys(boundary).forEach(function (k) {
      if (!map.hasLayer(boundary[k].layer)) return;
      var st = boundary[k].style;
      items.push('<span class="lg"><i class="ln" style="border-top:3px '
        + (st.dash ? "dashed" : "solid") + " " + st.color + '"></i>'
        + esc(st.label) + "</span>");
    });
    if (poiLayer) {
      items.push('<span class="lg">' + swatch(style.shape, style.color)
        + esc(currentPoiLabel()) + "</span>");
    }
    if (MODE === "retail") {
      meta.operators.filter(function (o) { return opOn[o.code]; }).forEach(function (o) {
        var mk = opMark(o.code);
        var mark = mk.url
          ? '<span class="oplogo lgl" style="border-color:' + mk.color
            + '"><img src="' + mk.url + '" alt=""></span>'
          : swatch("circle", mk.color);
        items.push('<span class="lg">' + mark + esc(mk.label)
          + " <b>" + o.count + "</b></span>");
      });
    }
    (api.extraLegend || []).forEach(function (x) { items.push(x); });
    el.innerHTML = items.length
      ? '<span class="lgtitle">Legend</span>' + items.join("")
      : '<span class="muted small">Nothing on the map yet.</span>';
    if (api.renderConfigBar) api.renderConfigBar();
  }
  api.renderLegend = renderLegend;
  api.extraLegend = [];

  // ── configuration summary, above the map ──────────────────────────────
  // What is on screen is the product of a dozen controls spread over
  // several tabs. Stating the applied settings in one line above the map
  // means you can read a screenshot six weeks later and still know what
  // produced it, without opening the sidebar to check.
  function chip(label, value, cls) {
    return '<span class="cfgchip' + (cls ? " " + cls : "") + '">'
      + (label ? '<span class="k">' + esc(label) + "</span>" : "")
      + "<b>" + esc(value) + "</b></span>";
  }
  api.chip = chip;

  function renderConfigBar() {
    var el = $("cfgbar");
    if (!el) return;
    var out = ['<span class="cfgtitle">Applied</span>'];
    (api.configChips || []).forEach(function (c) { out.push(c); });

    var src = $("poiSource") && $("poiSource").value;
    if (src) {
      out.push(chip("source", (meta.sources[src] || {}).label || src));
      var cat = $("poiCategory") && $("poiCategory").value;
      var brand = $("poiBrand") && $("poiBrand").value;
      var mc = $("poiMc") && $("poiMc").value;
      out.push(chip("category", cat || "all"));
      out.push(chip("brand", brand || "all"));
      if ($("poiMc")) out.push(chip("microcluster", mc || "all"));
      out.push(chip("on map", poiLayer
        ? (api.poiCount || 0).toLocaleString() + " POIs" : "POIs hidden",
        poiLayer ? "" : "warn"));
    }

    var bounds = Object.keys(boundary).filter(function (k) {
      return map.hasLayer(boundary[k].layer);
    }).map(function (k) { return boundary[k].style.label; });
    out.push(chip("boundaries", bounds.length ? bounds.join(" + ") : "none",
                  bounds.length ? "" : "warn"));

    if (MODE === "retail") {
      var on = meta.operators.filter(function (o) { return opOn[o.code]; });
      var n = on.reduce(function (a, o) { return a + o.count; }, 0);
      out.push(chip("service points", on.length
        ? n.toLocaleString() + " · " + on.length + " operators" : "hidden",
        on.length ? "" : "warn"));
    }
    el.innerHTML = out.join("");
  }
  api.renderConfigBar = renderConfigBar;
  api.configChips = [];

  // ── results panes under the map ───────────────────────────────────────
  function showPane(id) {
    document.querySelectorAll(".rpane").forEach(function (p) {
      p.hidden = p.id !== id;
    });
    document.querySelectorAll(".rtab").forEach(function (b) {
      b.classList.toggle("on", b.dataset.pane === id);
    });
  }
  api.showPane = showPane;
  document.querySelectorAll(".rtab").forEach(function (b) {
    b.addEventListener("click", function () { showPane(b.dataset.pane); });
  });

  // ── style controls ────────────────────────────────────────────────────
  var COLORS = ["#f0d04b", "#e8ecf6", "#7c5cff", "#4b96f3", "#6ec46e",
                "#ff6b6b", "#ff8a3d", "#ff2fbf", "#35d0e0"];
  var SHAPES = ["circle", "square", "triangle", "diamond"];

  function buildStyleControls(host) {
    if (!host) return;
    host.innerHTML =
      '<label class="fieldlabel">Marker colour</label><div class="swatches" id="swCol"></div>'
      + '<label class="fieldlabel">Marker shape</label><div class="swatches" id="swShp"></div>'
      + '<label class="fieldlabel">Size <span class="muted small" id="szV">7</span></label>'
      + '<input type="range" id="szR" min="4" max="16" value="7" class="range">';
    COLORS.forEach(function (c) {
      var b = document.createElement("button");
      b.type = "button"; b.className = "sw" + (c === style.color ? " on" : "");
      b.title = c;
      b.innerHTML = swatch(style.shape, c, 14);
      b.addEventListener("click", function () {
        style.color = c;
        host.querySelectorAll("#swCol .sw").forEach(function (x) { x.classList.remove("on"); });
        b.classList.add("on");
        refreshSwatches(host);
        restylePois();
      });
      host.querySelector("#swCol").appendChild(b);
    });
    SHAPES.forEach(function (sh) {
      var b = document.createElement("button");
      b.type = "button"; b.className = "sw" + (sh === style.shape ? " on" : "");
      b.title = sh;
      b.innerHTML = swatch(sh, style.color, 14);
      b.dataset.shape = sh;
      b.addEventListener("click", function () {
        style.shape = sh;
        host.querySelectorAll("#swShp .sw").forEach(function (x) { x.classList.remove("on"); });
        b.classList.add("on");
        refreshSwatches(host);
        restylePois();
      });
      host.querySelector("#swShp").appendChild(b);
    });
    host.querySelector("#szR").addEventListener("input", function (e) {
      style.size = Number(e.target.value);
      host.querySelector("#szV").textContent = style.size;
    });
    host.querySelector("#szR").addEventListener("change", function () {
      restylePois();
    });
  }
  function refreshSwatches(host) {
    host.querySelectorAll("#swCol .sw").forEach(function (b) {
      b.innerHTML = swatch(style.shape, b.title, 14);
    });
    host.querySelectorAll("#swShp .sw").forEach(function (b) {
      b.innerHTML = swatch(b.dataset.shape, style.color, 14);
    });
  }
  api.buildStyleControls = buildStyleControls;
  api.style = style;

  // ── full-screen map ───────────────────────────────────────────────────
  // Leaflet sizes its canvas from the container, so hiding the panels
  // around it is only half the job -- without invalidateSize the map keeps
  // painting at the old height and the new space stays grey.
  function applyMapMax(on) {
    document.body.classList.toggle("mapmax", on);
    var b = $("mapMax");
    if (b) { b.classList.toggle("on", on); b.textContent = on ? "Show panels" : "Expand map"; }
    try { window.localStorage.setItem("mapmax", on ? "1" : "0"); } catch (e) { /* private mode */ }
    if (map) window.setTimeout(function () { map.invalidateSize(); }, 60);
  }

  // Leaflet caches the container size and only re-reads it when told to, so
  // every route that changes the map's height had to remember to call
  // invalidateSize -- the Expand-map button, a results tab that grows, the
  // sidebar collapsing, the window resizing, a legend wrapping onto a second
  // line. Missing any one of them leaves the map painting at its old height
  // with dead grey space, which is what the manual off-and-on toggle was
  // working around. Watching the container instead covers all of them,
  // including the ones nobody has thought of yet.
  //
  // The guard matters: invalidateSize can itself change the container size,
  // and re-entering on our own write would be an endless loop.
  (function watchMapSize() {
    var el = $("map");
    if (!el || !map || !window.ResizeObserver) return;
    var last = { w: 0, h: 0 }, pending = 0;
    var ro = new window.ResizeObserver(function () {
      var w = el.clientWidth, h = el.clientHeight;
      if (!w || !h) return;                    // hidden: nothing to resize to
      if (w === last.w && h === last.h) return;
      last.w = w; last.h = h;
      if (pending) window.clearTimeout(pending);
      pending = window.setTimeout(function () {
        pending = 0;
        map.invalidateSize({ animate: false });
      }, 80);
    });
    ro.observe(el);
    api.mapSizeObserver = ro;
  })();
  api.applyMapMax = applyMapMax;
  if ($("mapMax")) {
    var saved = "0";
    try { saved = window.localStorage.getItem("mapmax") || "0"; } catch (e) { saved = "0"; }
    applyMapMax(saved === "1");
    $("mapMax").addEventListener("click", function () {
      applyMapMax(!document.body.classList.contains("mapmax"));
    });
  }

  // ── results height ────────────────────────────────────────────────────
  // The results strip is deliberately short so the map gets the window, but
  // a 121-row ranking is unreadable through a 190px slot. Rather than pick
  // one height and be wrong for one of the two jobs, let the strip grow to
  // half the window and remember which way you left it. The ResizeObserver
  // above repaints the map, so nothing else has to know this happened.
  (function resultsHeight() {
    var head = document.querySelector(".results-head");
    if (!head) return;
    var b = document.createElement("button");
    b.type = "button";
    b.className = "rtab resize";
    b.style.marginLeft = "auto";
    function apply(big) {
      document.body.classList.toggle("resultsbig", big);
      b.textContent = big ? "Shrink results" : "Expand results";
      b.classList.toggle("on", big);
      try { window.localStorage.setItem("resultsbig", big ? "1" : "0"); }
      catch (e) { /* private mode */ }
    }
    var saved = "0";
    try { saved = window.localStorage.getItem("resultsbig") || "0"; }
    catch (e) { saved = "0"; }
    apply(saved === "1");
    b.addEventListener("click", function () {
      apply(!document.body.classList.contains("resultsbig"));
    });
    head.appendChild(b);
  })();

  // ── boot ──────────────────────────────────────────────────────────────
  fetch("/api/territory/layers?domain=gapura").then(function (r) { return r.json(); })
    .then(function (d) {
      if (!d.ok) { status(d.error); return; }
      meta.operators = d.operators || [];
      var bl = $("boundaryList");
      if (bl) bl.innerHTML = "";
      // A layer marked `defer` gets its checkbox immediately and its
      // geometry only when someone ticks it. Desa is 887 polygons and about
      // 5 MB of coordinates — seven times the kecamatan layer — and paying
      // that on every page open to draw an outline most sessions never
      // switch on is the wrong trade.
      var styled = (d.layers || []).filter(function (l) {
        return T.styles[l.layer_key];
      });
      function boundaryRow(l, count) {
        var lab = document.createElement("label");
        var on = !l.style.defer;
        lab.innerHTML = '<input type="checkbox"' + (on ? " checked" : "") + ">"
          + '<span class="swatch" style="border-top-style:'
          + (l.style.dash ? "dashed" : "solid") + ";border-top-color:"
          + l.style.color + '"></span>' + esc(l.style.label)
          + '<span class="cnt">' + Number(count || 0).toLocaleString()
          + "</span>";
        if (bl) bl.appendChild(lab);
        var box = lab.querySelector("input");
        box.addEventListener("change", function (e) {
          if (!e.target.checked) {
            if (boundary[l.layer_key]) map.removeLayer(boundary[l.layer_key].layer);
            renderLegend();
            return;
          }
          if (boundary[l.layer_key]) {
            boundary[l.layer_key].layer.addTo(map);
            renderLegend();
            return;
          }
          box.disabled = true;
          lab.classList.add("loading");
          status("loading " + l.style.label + " — " +
                 Number(count || 0).toLocaleString() + " polygons…");
          loadBoundary(l.layer_key, l.style).then(function () {
            box.disabled = false;
            lab.classList.remove("loading");
            // Unticked while it was loading: honour that rather than
            // adding a layer the reader has already said they don't want.
            if (!box.checked) { status(l.style.label + " loaded"); return; }
            boundary[l.layer_key].layer.addTo(map);
            status(l.style.label + " loaded");
            if (api.onBoundaryLoaded) api.onBoundaryLoaded(l.layer_key);
            renderLegend();
          }).catch(function (err) {
            // Without this the checkbox stays disabled and the status line
            // stays on "loading…" for the rest of the session, with no way
            // to try again.
            box.disabled = false;
            box.checked = false;
            lab.classList.remove("loading");
            status(l.style.label + " failed to load: " + esc(err)
                   + " — tick the box to try again");
          });
        });
        return lab;
      }
      var jobs = styled.map(function (l) {
        if (l.style.defer) {
          boundaryRow(l, l.feature_count);
          return Promise.resolve();
        }
        return loadBoundary(l.layer_key, l.style).then(function (n) {
          boundaryRow(l, n);
          boundary[l.layer_key].layer.addTo(map);
        });
      });

      var ol = $("operatorList");
      if (ol && MODE === "retail") {
        ol.innerHTML = "";
        if (!meta.operators.length) {
          ol.innerHTML = '<span class="muted small">No service points loaded.</span>';
        }
        meta.operators.forEach(function (o) {
          opOn[o.code] = true;
          var mk = opMark(o.code);
          var lab = document.createElement("label");
          lab.innerHTML = '<input type="checkbox" checked>'
            + (mk.url
               ? '<span class="oplogo lgl" style="border-color:' + mk.color
                 + '"><img src="' + mk.url + '" alt=""></span>'
               : '<span class="dot" style="background:' + mk.color + '"></span>')
            + esc(mk.label)
            + '<span class="cnt">' + o.count.toLocaleString() + "</span>";
          lab.querySelector("input").addEventListener("change", function (e) {
            opOn[o.code] = e.target.checked; drawServicePoints(); renderLegend();
          });
          ol.appendChild(lab);
        });
      }
      return Promise.all(jobs);
    })
    .then(function () {
      if (MODE !== "retail") return null;
      return fetch("/api/territory/service-points.geojson")
        .then(function (r) { return r.json(); });
    })
    .then(function (fc) {
      if (fc) { spFeatures = fc.features || []; drawServicePoints(); }
      var b = [];
      Object.keys(boundary).forEach(function (k) {
        if (boundary[k].count) b.push(boundary[k].layer.getBounds()); });
      if (b.length) map.fitBounds(b.reduce(function (a, x) { return a.extend(x); }));
      buildStyleControls($("styleBox"));
      return loadCatalog();
    })
    .then(function () {
      if ($("poiApply")) return loadPois();
      renderLegend();
    })
    .then(function () { if (api.onReady) api.onReady(); })
    .catch(function (e) { status("failed: " + e); });

  if ($("poiSource"))
    $("poiSource").addEventListener("change", function () {
      loadCatalog().then(function () {
        if (api.onSourceChange) api.onSourceChange();
        return loadPois();
      });
    });
  if ($("poiApply"))
    $("poiApply").addEventListener("click", function () {
      loadPois().then(function () { if (api.onFilter) api.onFilter(); });
    });
  if ($("poiOff")) $("poiOff").addEventListener("click", api.hidePois);

  return api;
})();
