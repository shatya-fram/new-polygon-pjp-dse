/* SAMPLE OVERLAY — your own workbook, over the application's layers.

   THE FILE DOES NOT LEAVE THE BROWSER
   The workbook is read here, in the page, and drawn from memory. On a public
   host that means nobody's working file is uploaded anywhere to be looked
   at, and it also happens to be the fast path: a 2.5 MB demarcation export
   parses in about a quarter of a second, against a second or more to send it
   somewhere and wait for an answer.

   Keeping one on the server is a separate button, offered only where the
   server says it will accept one. Previewing and publishing are different
   acts and they look different here.

   ON TOP, ALWAYS
   A sample draws in its own pane above every permanent layer. It is a
   proposal being read against the territory, never a replacement for it, so
   it can never hide what it is being compared to. */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  // The preview page keeps its map in preview_territory.js's closure and
  // publishes this narrow handle on it. Nothing else from that file is
  // reachable from here, so a sample overlay cannot reach into the layers
  // it is drawn over.
  var M = window.PVMAP;
  if (!M || !$("smpPanel") || typeof L === "undefined") return;

  var esc = M.esc, num = M.num;

  // The map is fetched when a sample is actually drawn, never at load.
  // Creating it early -- before this panel above it had settled its own
  // height -- left Leaflet holding a stale size, and every animated fit
  // after that silently did nothing.
  var map = null;
  function mp() {
    if (!map) { map = M.ensureMap(); map.invalidateSize(); }
    return map;
  }

  // Columns we can find, and what we call them. The demarcation exports and
  // the application's own outlet layer spell the same things differently --
  // "DSE CODE" against "DSE_CODE", "Desa Name" against "Desa_Name" -- so
  // both are accepted rather than one being declared correct.
  var FIELDS = {
    dse:          ["dsecode", "dse", "dseid", "unikdse"],
    outlet_code:  ["outletcode", "outletcod"],
    outlet_name:  ["outletname", "outletnam"],
    lat:          ["lat", "latitude", "latnew", "y"],
    lon:          ["long", "lon", "longitude", "longnew", "x"],
    brand:        ["brandname", "brand"],
    region:       ["regionname", "region"],
    area:         ["areaname", "area"],
    branch:       ["branchname", "branch"],
    mc:           ["microclustername", "microclus", "mc"],
    partner:      ["partnerterritoryname", "partnerterritory"],
    partner_type: ["partnertype"],
    partner_name: ["mpxnamepartnername", "partnernm"],
    supervisor:   ["supervisorcode"],
    category:     ["outletcategory"],
    kabupaten:    ["kabupaten", "kabkot"],
    kecamatan:    ["kecamatanname", "kecamatan"],
    desa:         ["desaname", "desa", "kelurahanname", "keluarahanname"],
    hybrid:       ["hybridnonhybrid", "hybrid"],
    schedule:     ["jadwalkunjungan", "pjp"],
    pairing:      ["pairingoutletcode", "outletpairing"],
    unikid:       ["unikid"],
    remarks:      ["remarks", "remark"]
  };
  var LABEL = {
    dse: "DSE", outlet_code: "Outlet code", outlet_name: "Outlet",
    brand: "Brand", category: "Category", mc: "Microcluster",
    branch: "Branch", area: "Area", region: "Region", kabupaten: "Kabupaten",
    kecamatan: "Kecamatan", desa: "Desa", partner: "Partner territory",
    partner_type: "Partner type", partner_name: "Partner",
    supervisor: "Supervisor", hybrid: "Hybrid",
    schedule: "Visit schedule (PJP)", pairing: "Pairing outlet",
    unikid: "Unique id", remarks: "Remarks"
  };
  var CONTEXT = ["brand", "category", "mc", "branch", "area", "region",
                 "kabupaten", "kecamatan", "desa", "partner", "partner_type",
                 "partner_name", "supervisor", "hybrid", "schedule",
                 "pairing", "unikid", "remarks"];

  // The application's own layers are drawn in panes up to 650. A sample sits
  // above all of them, because it is the thing being asked about.
  var PANE = "smpPane";
  // The model's polygons go between the base layers and the sample's own
  // dots: you read the shape against the territory underneath, and the
  // outlets that produced the shape stay visible on top of it.
  var MPANE = "smpModelPane";
  var pane = null, layer = null, loaded = null, ring = null;
  var HOME = null;

  function ensurePane() {
    if (pane) return;
    var m = mp();
    var mpane = m.createPane(MPANE);
    mpane.style.zIndex = 670;
    pane = m.createPane(PANE);
    pane.style.zIndex = 690;
  }

  function norm(name) {
    var s = String(name == null ? "" : name).toLowerCase();
    if (s.indexOf("(") >= 0) s = s.split("(")[0];
    return s.replace(/[^a-z0-9]/g, "");
  }

  function mapHeader(header) {
    var idx = {};
    for (var i = 0; i < header.length; i++) {
      var n = norm(header[i]);
      if (!n) continue;
      for (var f in FIELDS) {
        if (idx[f] !== undefined) continue;
        if (FIELDS[f].indexOf(n) >= 0) { idx[f] = i; break; }
      }
    }
    return idx;
  }

  function numOf(v) {
    if (v == null || v === "") return null;
    if (typeof v === "number") return isFinite(v) ? v : null;
    var t = String(v).trim().replace(",", ".");
    var n = parseFloat(t);
    return isFinite(n) ? n : null;
  }
  function txt(v) {
    if (v == null) return null;
    var t = String(v).trim();
    return t === "" ? null : t;
  }

  function msg(html, bad) {
    var el = $("smpMsg");
    if (!el) return;
    el.innerHTML = html;
    el.className = "smp-msg" + (bad ? " bad" : "");
  }

  // ── reading the workbook ───────────────────────────────────────────────
  // Every sheet is tried and the one carrying a DSE code, a longitude and a
  // latitude wins. These files also contain Summary and Maps sheets, and
  // taking the first sheet would read a pivot table as an outlet list.
  function pickSheet(wb) {
    var tried = [];
    for (var i = 0; i < wb.SheetNames.length; i++) {
      var name = wb.SheetNames[i];
      var rows = XLSX.utils.sheet_to_json(wb.Sheets[name],
        { header: 1, blankrows: false, raw: true, defval: null });
      for (var probe = 0; probe < Math.min(6, rows.length); probe++) {
        var idx = mapHeader(rows[probe] || []);
        var have = ["dse", "lat", "lon"].filter(function (f) {
          return idx[f] !== undefined; });
        tried.push({ sheet: name, row: probe + 1, have: have.length });
        if (have.length === 3) {
          return { sheet: name, idx: idx, rows: rows.slice(probe + 1) };
        }
      }
    }
    tried.sort(function (a, b) { return b.have - a.have; });
    var near = tried[0];
    throw new Error(
      "No sheet in this workbook has the three columns a sample needs: a "
      + "DSE code, a longitude and a latitude."
      + (near ? " The closest was <b>" + esc(near.sheet) + "</b> row "
                + near.row + ", which matched " + near.have + " of 3." : "")
      + " Accepted headings include DSE CODE or UNIKDSE, LONG or LONGITUDE, "
      + "and LAT or LATITUDE.");
  }

  function read(buf, filename) {
    var wb = XLSX.read(new Uint8Array(buf), { type: "array" });
    var picked = pickSheet(wb);
    var idx = picked.idx;
    function cell(row, f) {
      var i = idx[f];
      return (i === undefined || i >= row.length) ? null : row[i];
    }
    var out = [], stats = { read: 0, no_dse: 0, no_coords: 0, off_map: 0 };
    var dse = {}, box = null;
    for (var r = 0; r < picked.rows.length; r++) {
      var row = picked.rows[r] || [];
      if (!row.length) continue;
      stats.read++;
      var code = txt(cell(row, "dse"));
      if (!code) { stats.no_dse++; continue; }
      var lat = numOf(cell(row, "lat")), lon = numOf(cell(row, "lon"));
      // Same transposition guard the rest of the application uses: here
      // latitude is about -6 and longitude about 107, so the numbers settle
      // it and the column headings do not get a vote.
      if (lat != null && lon != null && Math.abs(lat) > 90
          && Math.abs(lon) <= 90) { var t = lat; lat = lon; lon = t; }
      if (lat == null || lon == null) { stats.no_coords++; continue; }
      if (HOME && (lat < HOME.minlat || lat > HOME.maxlat
                   || lon < HOME.minlon || lon > HOME.maxlon)) {
        stats.off_map++; continue;
      }
      var attrs = {};
      for (var c = 0; c < CONTEXT.length; c++) {
        var v = txt(cell(row, CONTEXT[c]));
        if (v != null) attrs[CONTEXT[c]] = v;
      }
      dse[code] = (dse[code] || 0) + 1;
      box = box ? [Math.min(box[0], lon), Math.min(box[1], lat),
                   Math.max(box[2], lon), Math.max(box[3], lat)]
                : [lon, lat, lon, lat];
      out.push({ dse: code, outlet_code: txt(cell(row, "outlet_code")),
                 outlet_name: txt(cell(row, "outlet_name")),
                 lat: lat, lon: lon, attrs: attrs });
    }
    return { sheet: picked.sheet, rows: out, stats: stats, dse: dse,
             box: box, filename: filename };
  }

  // ── drawing ────────────────────────────────────────────────────────────
  // One colour per DSE, so the demarcation reads as a demarcation rather
  // than as a scatter of identical dots.
  var PALETTE = ["#ff6b6b", "#4dd4c1", "#ffd166", "#8fd14f", "#c792ea",
                 "#ff9f68", "#63c7ff", "#f78fb3", "#a0e7a0", "#ffc4a3",
                 "#9bb8ff", "#e8d16a"];
  function colourOf(code, order) {
    return PALETTE[order[code] % PALETTE.length];
  }

  function clear() {
    if (layer) { mp().removeLayer(layer); layer = null; }
    if (ring) { mp().removeLayer(ring); ring = null; }
    clearModel();
    var mc = $("smpModel");
    if (mc) mc.hidden = true;
    loaded = null;
    M.hold = false;
    var l = $("smpLegend");
    if (l) { l.hidden = true; l.innerHTML = ""; }
    var k = $("smpKeep");
    if (k) k.hidden = true;
    msg("");
    var f = $("smpFile");
    if (f) f.value = "";
    // The panel under the map is built from the file that just went away.
    var det = $("smpDetail");
    if (det) det.hidden = true;
    dTicket++;
    dDesaRows = []; dOutRows = [];
  }
  window.addEventListener("pv:clear", clear);

  function draw(data) {
    ensurePane();
    if (layer) { mp().removeLayer(layer); layer = null; }
    var codes = Object.keys(data.dse).sort(function (a, b) {
      return data.dse[b] - data.dse[a]; });
    var order = {};
    codes.forEach(function (c, i) { order[c] = i; });

    var fc = { type: "FeatureCollection", features: data.rows.map(function (r) {
      return { type: "Feature",
               properties: { dse: r.dse, outlet_code: r.outlet_code,
                             outlet_name: r.outlet_name, attrs: r.attrs },
               geometry: { type: "Point", coordinates: [r.lon, r.lat] } };
    }) };

    // Same canvas as the base layers: a second one on top would swallow
    // every hover the boundaries under it are waiting for.
    var renderer = M.renderer;
    layer = L.geoJSON(fc, {
      pointToLayer: function (f, ll) {
        return L.circleMarker(ll, {
          renderer: renderer, radius: 4.5,
          color: "#0b0d13", weight: 1,
          fillColor: colourOf(f.properties.dse, order), fillOpacity: .95
        });
      }
    });
    // One delegated handler, not one per point: these files run to
    // thousands of outlets and a tooltip each is what makes a map stall.
    layer.on("mouseover", function (e) {
      var p = e.layer.feature.properties;
      if (!e.layer.getTooltip()) {
        e.layer.bindTooltip(
          "<strong>" + esc(p.outlet_name || p.outlet_code || "") + "</strong>"
          + "<br>" + esc(p.dse)
          + (p.attrs.desa ? "<br>" + esc(p.attrs.desa) : "")
          + '<br><span class="muted">sample overlay</span>',
          { className: "terr-tip" });
      }
      e.layer.openTooltip();
    });
    layer.on("click", function (e) {
      var p = e.layer.feature.properties;
      var rows = [["DSE", p.dse], ["Outlet", p.outlet_name],
                  ["Outlet code", p.outlet_code]];
      CONTEXT.forEach(function (k) {
        if (p.attrs[k]) rows.push([LABEL[k] || k, p.attrs[k]]);
      });
      var html = '<div class="smp-pop"><b>Sample overlay</b><table>'
        + rows.filter(function (r) { return r[1]; }).map(function (r) {
            return "<tr><th>" + esc(r[0]) + "</th><td>" + esc(r[1])
                   + "</td></tr>"; }).join("")
        + "</table></div>";
      if (!e.layer.getPopup()) e.layer.bindPopup(html, { maxHeight: 320 });
      else e.layer.setPopupContent(html);
      e.layer.openPopup();
    });
    layer.addTo(mp());
    if (M.top) M.top(layer);
    loaded = data;
    M.hold = true;      // the reader is looking at this now

    if (data.box) {
      // Not animated: this is a jump to a different dataset, not a nudge
      // within the current one, and an instant frame beats a long fly. It
      // also means the fit does not depend on an animation frame, which a
      // background tab never gets.
      mp().fitBounds(L.latLngBounds([[data.box[1], data.box[0]],
                                     [data.box[3], data.box[2]]]).pad(.06),
                     { animate: false });
    }

    // Legend: every DSE named and counted. Colour never carries the meaning
    // on its own -- twelve colours cannot label thirty-nine reps.
    var leg = $("smpLegend");
    leg.hidden = false;
    leg.innerHTML = '<div class="smp-leg-head"><b>' + esc(data.filename)
      + "</b> · " + num(data.rows.length) + " outlets · "
      + num(codes.length) + " DSE"
      + ' <button type="button" id="smpClear">clear</button></div>'
      + codes.map(function (c) {
          return '<button type="button" class="smp-dse" data-dse="'
            + esc(c) + '"><i style="background:' + colourOf(c, order)
            + '"></i>' + esc(c) + " <b>" + num(data.dse[c]) + "</b></button>";
        }).join("");
    $("smpClear").addEventListener("click", clear);
    leg.querySelectorAll(".smp-dse").forEach(function (b) {
      b.addEventListener("click", function () { focusDse(b.dataset.dse, b); });
    });

    var st = data.stats;
    var tail = [];
    if (st.no_dse) tail.push(num(st.no_dse) + " without a DSE code");
    if (st.no_coords) tail.push(num(st.no_coords) + " without coordinates");
    if (st.off_map) tail.push(num(st.off_map) + " outside the map area");
    msg("Read <b>" + num(st.read) + "</b> rows from sheet <b>"
        + esc(data.sheet) + "</b> — <b>" + num(data.rows.length)
        + "</b> drawn"
        + (tail.length ? ", " + tail.join(", ") : "")
        + ". This file has not been sent anywhere; it is drawn from your "
        + "browser's memory.");

    var keep = $("smpKeep");
    if (keep && keep.dataset.can === "1") keep.hidden = false;
    var mc = $("smpModel");
    if (mc) mc.hidden = false;
  }

  // ══════════════════════════════════════════════════════════════════════
  // THE TERRITORY MODEL, FROM THIS SAMPLE
  //
  // Exclusive, Coverage and Force fit are the application's own definitions
  // of a rep's patch, so they are computed where they are defined -- on the
  // server -- and nothing is kept. Only (DSE, lat, lon) is sent: the model
  // needs no more than that, and the rest of somebody's workbook has no
  // reason to travel.
  //
  // Every polygon carries the four figures the exercise is actually about:
  // whose it is, how many outlets produced it, how many desa those outlets
  // fall in, and how many masts stand inside it.
  // ══════════════════════════════════════════════════════════════════════
  var model = null, modelMode = "exclusive", modelPick = null, modelBusy = false;

  function mmsg(html, bad) {
    var el = $("smpModelMsg");
    if (!el) return;
    el.innerHTML = html;
    el.className = "smp-msg" + (bad ? " bad" : "");
  }

  function clearModel() {
    if (model) { mp().removeLayer(model); model = null; }
    if (M.setModel) M.setModel(null);
    modelPick = null;
    var ro = $("smpReadout");
    if (ro) ro.hidden = true;
    mmsg("");
  }

  function modelStyle(p, dim) {
    return { color: p.colour || "#35d0e0", weight: dim ? 1 : 1.8,
             opacity: dim ? .35 : .95, fillColor: p.colour || "#35d0e0",
             fillOpacity: dim ? .03 : .12 };
  }

  function figures(p) {
    return [["DSE", esc(p.dse)],
            ["Outlets", num(p.outlets)],
            ["Desa", num(p.desa)],
            ["Sites", num(p.sites)],
            ["Area", p.area_km2 + " km²"],
            ["Parts", num(p.parts)]];
  }

  function buildModel() {
    if (!loaded || modelBusy) return;
    ensurePane();
    modelBusy = true;
    $("smpBuild").disabled = true;
    mmsg("Building " + esc(modelMode) + " from " + num(loaded.rows.length)
         + " outlets… <span class='muted'>(the points are computed on the "
         + "server and not kept)</span>");
    var reach = Number($("smpReach").value);
    fetch("/api/samples/model.geojson", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mode: modelMode, reach: reach, cell: Math.max(100, reach / 2),
        // (DSE, lat, lon) and nothing else.
        points: loaded.rows.map(function (r) { return [r.dse, r.lat, r.lon]; })
      })
    }).then(function (r) { return r.json(); })
      .then(function (fc) {
        modelBusy = false;
        $("smpBuild").disabled = false;
        if (model) { mp().removeLayer(model); model = null; }
        if (fc.ok === false) { mmsg(esc(fc.error), !fc.empty); return; }
        model = L.geoJSON(fc, {
          renderer: M.renderer,
          style: function (f) { return modelStyle(f.properties, false); }
        });
        model.on("mouseover", function (e) {
          if (M.target && M.target !== "auto") return;
          var p = e.layer.feature.properties;
          if (!e.layer.getTooltip()) {
            e.layer.bindTooltip(
              "<strong>" + esc(p.dse) + "</strong><br>"
              + num(p.outlets) + " outlets · " + num(p.desa) + " desa · "
              + num(p.sites) + " sites<br>" + p.area_km2 + " km²",
              { sticky: true, className: "terr-tip" });
          }
          e.layer.openTooltip();
        });
        model.on("click", function (e) {
          if (M.target && M.target !== "auto") return;  // map handler owns it
          var p = e.layer.feature.properties;
          var html = '<div class="smp-pop"><b>' + esc(modelMode)
            + " territory</b><table>"
            + figures(p).map(function (r) {
                return "<tr><th>" + r[0] + "</th><td>" + r[1] + "</td></tr>";
              }).join("") + "</table></div>";
          if (!e.layer.getPopup()) e.layer.bindPopup(html);
          else e.layer.setPopupContent(html);
          e.layer.openPopup();
          // The popup is the glance; the panel under the map is the answer.
          showTerritory(e.layer.feature);
        });
        model.addTo(mp());
        if (M.top) M.top(model);
        if (M.setModel) M.setModel(model);
        readout(fc);
        legendFigures(fc);

        var tail = [];
        if (modelMode !== "forcefit") tail.push(fc.reach + " m reach");
        if ((fc.skipped || []).length)
          tail.push(fc.skipped.length + " skipped (under 3 outlets)");
        if (fc.islands_absorbed)
          tail.push("<b>" + num(fc.islands_absorbed) + "</b> slivers merged");
        if (fc.seam)
          tail.push("<b>" + num(fc.seam) + "</b> outlets on a seam");
        if (fc.forcefit && fc.forcefit.moved != null)
          tail.push("<b>" + num(fc.forcefit.moved) + "</b> desa moved of "
                    + num(fc.forcefit.total_desa));
        mmsg("<b>" + num(fc.drawn) + "</b> of " + num(fc.groups)
             + " DSE drawn" + (tail.length ? " · " + tail.join(" · ") : "")
             + ". Click a territory for its figures, or a DSE below to "
             + "isolate it.");
        // Open the ledger on the whole sample, so the panel is on screen
        // explaining itself rather than waiting hidden for somebody to
        // guess that clicking a polygon opens it.
        sampleLedger();
      })
      .catch(function (e) {
        modelBusy = false;
        $("smpBuild").disabled = false;
        mmsg("Could not build it: " + esc(String(e)), true);
      });
  }

  function readout(fc) {
    var ro = $("smpReadout");
    if (!ro) return;
    var f = fc.features || [];
    var sum = function (k) {
      return f.reduce(function (a, x) { return a + (x.properties[k] || 0); }, 0);
    };
    ro.hidden = false;
    $("smpRoDse").textContent = num(fc.drawn);
    $("smpRoOut").textContent = num(sum("outlets"));
    $("smpRoDesa").textContent = num(sum("desa"));
    $("smpRoSites").textContent = num(sum("sites"));
    $("smpRoArea").textContent = fc.area_km2;
    $("smpRoOver").textContent = fc.overlap_pct != null
      ? fc.overlap_pct + "%" : "—";
  }

  // Fold the model's figures into the DSE list, so one row says everything
  // known about a rep: their outlets from the file, and their desa and
  // sites from the territory those outlets produce.
  function legendFigures(fc) {
    var by = {};
    (fc.features || []).forEach(function (f) {
      by[f.properties.dse] = f.properties;
    });
    $("smpLegend").querySelectorAll(".smp-dse").forEach(function (b) {
      var p = by[b.dataset.dse];
      var extra = b.querySelector(".mfig");
      if (!p) { if (extra) extra.remove(); return; }
      if (!extra) {
        extra = document.createElement("span");
        extra.className = "mfig";
        b.appendChild(extra);
      }
      extra.innerHTML = " · " + num(p.desa) + " desa · " + num(p.sites)
        + " sites";
    });
  }

  // Isolate one rep: their territory kept, everyone else's dimmed.
  function soloModel(code) {
    if (!model) return false;
    modelPick = (modelPick === code) ? null : code;
    model.eachLayer(function (l) {
      var p = l.feature.properties;
      l.setStyle(modelStyle(p, modelPick && p.dse !== modelPick));
    });
    if (modelPick) {
      var b = null;
      model.eachLayer(function (l) {
        if (l.feature.properties.dse !== modelPick) return;
        b = b ? b.extend(l.getBounds()) : L.latLngBounds(
          l.getBounds().getSouthWest(), l.getBounds().getNorthEast());
      });
      if (b && b.isValid())
        mp().fitBounds(b.pad(.2), { maxZoom: 16, animate: false });
    }
    return true;
  }

  if ($("smpModes")) {
    $("smpModes").addEventListener("click", function (e) {
      var b = e.target.closest("button[data-mode]");
      if (!b || b.dataset.mode === modelMode) return;
      modelMode = b.dataset.mode;
      $("smpModes").querySelectorAll("button").forEach(function (x) {
        var on = x.dataset.mode === modelMode;
        x.classList.toggle("on", on);
        x.setAttribute("aria-checked", on ? "true" : "false");
      });
      buildModel();
    });
  }
  if ($("smpReach")) {
    $("smpReach").addEventListener("input", function (e) {
      $("smpReachVal").textContent = e.target.value + " m";
    });
    // Built on release, not on every tick: a model is seconds of work and
    // a drag would queue a dozen of them.
    $("smpReach").addEventListener("change", function () {
      if (model) buildModel();
    });
  }
  if ($("smpBuild")) $("smpBuild").addEventListener("click", buildModel);
  if ($("smpModelOff")) $("smpModelOff").addEventListener("click", clearModel);
  if ($("smpDots")) {
    $("smpDots").addEventListener("change", function (e) {
      if (!layer) return;
      if (e.target.checked) layer.addTo(mp());
      else mp().removeLayer(layer);
    });
  }

  // Zoom to one rep's outlets in the sample.
  function focusDse(code, btn) {
    if (!loaded) return;
    if (ring) { mp().removeLayer(ring); ring = null; }
    // With a model on screen, isolating a rep means isolating their
    // territory, not drawing a box round their dots.
    if (model && soloModel(code)) {
      $("smpLegend").querySelectorAll(".smp-dse.on").forEach(function (b) {
        b.classList.remove("on"); });
      if (btn && modelPick) btn.classList.add("on");
      var p = null;
      model.eachLayer(function (l) {
        if (l.feature.properties.dse === code) p = l.feature.properties; });
      if (p && modelPick) {
        mmsg("<b>" + esc(code) + "</b> — " + num(p.outlets) + " outlets · "
             + num(p.desa) + " desa · " + num(p.sites) + " sites · "
             + p.area_km2 + " km². Click again to show them all.");
        ledgerForDse(code);
      } else {
        mmsg("Showing every territory again.");
        sampleLedger();
      }
      return;
    }
    $("smpLegend").querySelectorAll(".smp-dse.on").forEach(function (b) {
      b.classList.remove("on"); });
    var pts = loaded.rows.filter(function (r) { return r.dse === code; });
    if (!pts.length) return;
    if (btn) btn.classList.add("on");
    var b = L.latLngBounds(pts.map(function (r) { return [r.lat, r.lon]; }));
    ring = L.rectangle(b.pad(.12), { renderer: M.renderer, color: "#ffffff", weight: 2,
                                     dashArray: "5 4", fill: false }).addTo(mp());
    mp().fitBounds(b.pad(.25), { maxZoom: 16, animate: false });
    msg("<b>" + esc(code) + "</b> — " + num(pts.length)
        + " outlets in this sample. Click the same row again to clear.");
    ledgerForDse(code);
  }

  // ── the file ───────────────────────────────────────────────────────────
  function load(file) {
    if (!file) return;
    if (typeof XLSX === "undefined") {
      msg("The spreadsheet reader did not load, so a workbook cannot be "
          + "opened here. Check the page's script sources.", true);
      return;
    }
    msg("Reading " + esc(file.name) + "…");
    var fr = new FileReader();
    fr.onload = function () {
      try {
        var t0 = Date.now();
        var data = read(fr.result, file.name);
        if (!data.rows.length) {
          var st = data.stats;
          msg("None of the " + num(st.read) + " rows could be placed: "
              + num(st.no_dse) + " without a DSE code, " + num(st.no_coords)
              + " without coordinates, " + num(st.off_map)
              + " outside the map area.", true);
          return;
        }
        draw(data);
        var el = $("smpMsg");
        if (el) el.innerHTML += " <span class='muted'>(" + (Date.now() - t0)
          + " ms)</span>";
      } catch (e) {
        msg(e && e.message ? e.message : String(e), true);
      }
    };
    fr.onerror = function () { msg("That file could not be read.", true); };
    fr.readAsArrayBuffer(file);
  }

  $("smpFile").addEventListener("change", function (e) {
    load(e.target.files && e.target.files[0]);
  });
  var drop = $("smpDrop");
  if (drop) {
    ["dragenter", "dragover"].forEach(function (ev) {
      drop.addEventListener(ev, function (e) {
        e.preventDefault(); drop.classList.add("over"); });
    });
    ["dragleave", "drop"].forEach(function (ev) {
      drop.addEventListener(ev, function (e) {
        e.preventDefault(); drop.classList.remove("over"); });
    });
    drop.addEventListener("drop", function (e) {
      load(e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0]);
    });
    drop.addEventListener("click", function () { $("smpFile").click(); });
  }

  // ── keeping one on this server ─────────────────────────────────────────
  function keep() {
    if (!loaded) return;
    var meta = { uploader: $("smpUser").value.trim(),
                 branch: $("smpBranch").value.trim(),
                 region: $("smpRegion").value.trim() };
    var missing = Object.keys(meta).filter(function (k) { return !meta[k]; });
    if (missing.length) {
      msg("Before this can be kept for others it has to say who uploaded it "
          + "and which branch and region it covers. Missing: "
          + missing.join(", ") + ".", true);
      return;
    }
    var btn = $("smpKeep");
    btn.disabled = true;
    btn.textContent = "keeping…";
    fetch("/api/samples", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        uploader: meta.uploader, branch: meta.branch, region: meta.region,
        label: $("smpLabel") ? $("smpLabel").value.trim() : "",
        filename: loaded.filename,
        rows: loaded.rows.map(function (r) {
          return { dse: r.dse, outlet_code: r.outlet_code,
                   outlet_name: r.outlet_name, lat: r.lat, lon: r.lon,
                   attrs: r.attrs };
        })
      })
    }).then(function (r) { return r.json(); })
      .then(function (d) {
        btn.disabled = false;
        btn.textContent = "Keep on this server";
        if (!d.ok) { msg(esc(d.error), true); return; }
        btn.hidden = true;
        msg("Kept as <b>" + esc(d.sample.label) + "</b> — anyone opening this "
            + "page on this server can now switch it on. Remove it from the "
            + "Configuration page.");
        refreshKept();
      })
      .catch(function (e) {
        btn.disabled = false;
        btn.textContent = "Keep on this server";
        msg("Could not keep it: " + esc(e), true);
      });
  }
  if ($("smpKeep")) $("smpKeep").addEventListener("click", keep);

  // ── samples already kept on this server ────────────────────────────────
  var kept = {};
  function refreshKept() {
    fetch("/api/samples").then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.ok) return;
        if ($("smpKeep")) $("smpKeep").dataset.can = d.can_store ? "1" : "0";
        var host = $("smpKept");
        if (!host) return;
        if (!d.samples.length) { host.hidden = true; host.innerHTML = ""; return; }
        host.hidden = false;
        host.innerHTML = '<div class="smp-tag">On this server</div>'
          + d.samples.map(function (s) {
              return '<label class="checkline"><input type="checkbox" '
                + 'data-sid="' + s.id + '"> ' + esc(s.label)
                + ' <span class="muted">' + num(s.rows_kept) + " outlets · "
                + esc(s.uploader) + "</span></label>";
            }).join("");
        host.querySelectorAll("input[data-sid]").forEach(function (b) {
          b.addEventListener("change", function () {
            toggleKept(+b.dataset.sid, b.checked);
          });
        });
      }).catch(function () {});
  }

  function toggleKept(sid, on) {
    ensurePane();
    if (!on) {
      if (kept[sid]) { mp().removeLayer(kept[sid]); delete kept[sid]; }
      return;
    }
    if (kept[sid]) { kept[sid].addTo(mp()); return; }
    fetch("/api/samples/" + sid + "/points.geojson")
      .then(function (r) { return r.json(); })
      .then(function (fc) {
        if (fc.ok === false) { msg(esc(fc.error), true); return; }
        var renderer = M.renderer;
        var g = L.geoJSON(fc, {
          pointToLayer: function (f, ll) {
            return L.circleMarker(ll, { renderer: renderer, radius: 4,
              color: "#0b0d13", weight: 1, fillColor: "#ffd166",
              fillOpacity: .9 });
          }
        });
        g.on("mouseover", function (e) {
          var p = e.layer.feature.properties;
          if (!e.layer.getTooltip())
            e.layer.bindTooltip("<strong>"
              + esc(p.outlet_name || p.outlet_code || "") + "</strong><br>"
              + esc(p.dse) + '<br><span class="muted">kept sample</span>',
              { className: "terr-tip" });
          e.layer.openTooltip();
        });
        kept[sid] = g;
        g.addTo(mp());
        if (M.top) M.top(g);
      }).catch(function (e) { msg("Could not load that sample: " + esc(e), true); });
  }


  // ══════════════════════════════════════════════════════════════════════
  // WHAT YOU CLICKED, IN FULL, UNDER THE MAP
  //
  // Hover gives the one-line version on the map, computed here from rows
  // already in memory, so moving the cursor over a thousand desa costs
  // nothing and fires no requests. Click gives the table below the map: the
  // desa this territory covers and by how much, and every outlet in it.
  //
  // THE TWO HALVES COME FROM DIFFERENT PLACES ON PURPOSE
  // Desa geometry, area, population and masts are the application's own
  // data and are answered by the server. Outlet codes, names, categories
  // and remarks are the reader's workbook and are answered here, from the
  // copy already parsed in this page. Nothing of the workbook is posted to
  // fill this panel -- the only thing sent is a polygon the server drew
  // itself a moment ago and handed to us.
  // ══════════════════════════════════════════════════════════════════════
  var dTicket = 0;              // the click that is allowed to answer
  var dDesaRows = [], dOutRows = [], dTab = "desa", dLabel = "area";

  function nrm(v) {
    return String(v == null ? "" : v).toUpperCase().replace(/[^A-Z0-9]/g, "");
  }
  function dkey(desa, kec) { return nrm(desa) + "|" + nrm(kec); }

  function dmsg(html, bad) {
    var el = $("smpDMsg");
    if (!el) return;
    el.innerHTML = html || "";
    el.className = "smp-msg" + (bad ? " bad" : "");
  }
  function dOpen(kind, title, sub) {
    var el = $("smpDetail");
    if (!el) return false;
    el.hidden = false;
    $("smpDKind").textContent = kind;
    $("smpDTitle").textContent = title;
    $("smpDSub").textContent = sub || "";
    return true;
  }
  function tiles(list) {
    $("smpDStats").innerHTML = list.map(function (t) {
      return "<div><b>" + t[1] + "</b><span>" + esc(t[0]) + "</span></div>";
    }).join("");
  }

  // Coverage reads as a bar behind the number: green when the whole desa is
  // held, amber when it is a corner of one, so twenty rows sort themselves
  // visually before anybody reads a digit.
  function covBar(pct) {
    if (pct == null) return '<span class="muted">—</span>';
    var cls = pct >= 99 ? " full" : (pct < 25 ? " thin" : "");
    return '<span class="smp-cov' + cls + '"><i style="width:'
      + Math.max(2, Math.min(100, pct)) + '%"></i><b>'
      + pct.toFixed(1) + "%</b></span>";
  }
  function km2(v) { return v == null ? "—" : Number(v).toFixed(3); }

  // ── the two tables ────────────────────────────────────────────────────
  function renderDesa() {
    var tb = $("smpDDesa");
    if (!tb) return;
    if (!dDesaRows.length) {
      tb.innerHTML = '<tr><td colspan="9" class="muted">Nothing to show '
        + "yet.</td></tr>";
      return;
    }
    tb.innerHTML = dDesaRows.map(function (r) {
      return '<tr class="click" data-desa="' + esc(r.desa) + '" data-kec="'
        + esc(r.kecamatan) + '"><td><b>' + esc(r.desa) + "</b></td><td>"
        + esc(r.kecamatan) + "</td><td>" + esc(r.mc || "—")
        + '</td><td class="n">' + km2(r.desa_km2)
        + '</td><td class="n">' + km2(r.inside_km2)
        + '</td><td class="n">' + covBar(r.pct)
        + '</td><td class="n">' + num(r.outlets || 0)
        + '</td><td class="n">' + (r.sites == null ? "—" : num(r.sites))
        + '</td><td class="n">' + (r.population ? num(r.population) : "—")
        + "</td></tr>";
    }).join("");
    tb.querySelectorAll("tr.click").forEach(function (tr) {
      tr.addEventListener("click", function () {
        showDesaByName(tr.dataset.desa, tr.dataset.kec);
      });
    });
  }

  function renderOut() {
    var tb = $("smpDOut");
    if (!tb) return;
    if (!dOutRows.length) {
      tb.innerHTML = '<tr><td colspan="9" class="muted">No outlet from the '
        + "loaded workbook falls in this area.</td></tr>";
      return;
    }
    // Capped, and the cap is stated rather than silently applied: a rep
    // with 1,282 outlets would otherwise put 1,282 rows in the DOM and make
    // the panel the slowest thing on the page.
    var slice = dOutRows.slice(0, 400);
    tb.innerHTML = slice.map(function (r) {
      var a = r.attrs || {};
      return "<tr><td>" + esc(r.outlet_code || "—") + "</td><td><b>"
        + esc(r.outlet_name || "—") + "</b></td><td>" + esc(r.dse)
        + "</td><td>" + esc(a.desa || "—") + "</td><td>"
        + esc(a.kecamatan || "—") + "</td><td>" + esc(a.category || "—")
        + "</td><td>" + esc(a.remarks || "—") + '</td><td class="n">'
        + r.lat.toFixed(6) + '</td><td class="n">' + r.lon.toFixed(6)
        + "</td></tr>";
    }).join("");
    if (dOutRows.length > slice.length) {
      tb.innerHTML += '<tr><td colspan="9" class="muted">Showing the first '
        + num(slice.length) + " of " + num(dOutRows.length)
        + " — export the CSV for all of them.</td></tr>";
    }
  }

  function outletsIn(test) {
    return (loaded && loaded.rows ? loaded.rows : []).filter(test);
  }
  function perDesa(rows) {
    var by = {};
    rows.forEach(function (r) {
      var a = r.attrs || {};
      var k = dkey(a.desa, a.kecamatan);
      by[k] = (by[k] || 0) + 1;
    });
    return by;
  }

  // Open the ledger for a rep named in the legend rather than clicked on the
  // map. With a territory drawn we have its polygon and can measure the desa
  // it covers; without one there is nothing to measure against, so the
  // outlet half is filled and the desa half says why it is empty.
  function ledgerForDse(code) {
    var feat = null;
    if (model) {
      model.eachLayer(function (l) {
        if (l.feature.properties.dse === code) feat = l.feature; });
    }
    if (feat) { showTerritory(feat); return; }

    var n = ++dTicket;
    dLabel = code;
    if (!dOpen("DSE profile", code, "from your workbook")) return;
    dOutRows = outletsIn(function (r) { return r.dse === code; });
    renderOut();
    var by = perDesa(dOutRows), meta = {};
    dOutRows.forEach(function (r) {
      var a = r.attrs || {};
      var k = dkey(a.desa, a.kecamatan);
      if (!meta[k]) meta[k] = { desa: a.desa || "—",
                                kecamatan: a.kecamatan || "", mc: a.mc || "" };
    });
    dDesaRows = Object.keys(by).map(function (k) {
      return { desa: meta[k].desa, kecamatan: meta[k].kecamatan,
               mc: meta[k].mc, desa_km2: null, inside_km2: null, pct: null,
               outlets: by[k], sites: null, population: 0 };
    }).sort(function (a, b) { return b.outlets - a.outlets; });
    renderDesa();
    tiles([["outlets", num(dOutRows.length)],
           ["desa in file", num(dDesaRows.length)],
           ["sites", "—"], ["km² area", "—"],
           ["% desa covered", "—"]]);
    dmsg("Build a territory above to measure the area, the masts and how "
         + "much of each desa this rep covers. Until then these are the "
         + "figures your workbook carries. Click a desa row for its own "
         + "area, people and masts.");
    if (n !== dTicket) return;
  }

  // The whole sample at once — what the panel shows the moment a model is
  // built, so it is on screen and explaining itself rather than waiting
  // hidden for somebody to guess that clicking a polygon opens it.
  function sampleLedger() {
    if (!loaded) return;
    ++dTicket;
    dLabel = loaded.filename || "sample";
    if (!dOpen("Sample profile", loaded.filename || "This sample",
               num(loaded.rows.length) + " outlets · "
               + num(Object.keys(loaded.dse).length) + " DSE")) return;
    dOutRows = loaded.rows.slice();
    renderOut();
    var by = perDesa(dOutRows), meta = {};
    dOutRows.forEach(function (r) {
      var a = r.attrs || {};
      var k = dkey(a.desa, a.kecamatan);
      if (!meta[k]) meta[k] = { desa: a.desa || "—",
                                kecamatan: a.kecamatan || "", mc: a.mc || "" };
    });
    dDesaRows = Object.keys(by).map(function (k) {
      return { desa: meta[k].desa, kecamatan: meta[k].kecamatan,
               mc: meta[k].mc, desa_km2: null, inside_km2: null, pct: null,
               outlets: by[k], sites: null, population: 0 };
    }).sort(function (a, b) { return b.outlets - a.outlets; });
    renderDesa();
    tiles([["outlets", num(dOutRows.length)],
           ["DSE", num(Object.keys(loaded.dse).length)],
           ["desa in file", num(dDesaRows.length)],
           ["sites", "—"], ["km² area", "—"]]);
    dmsg("Click a <b>territory</b> on the map, or a <b>rep</b> in the list "
         + "above, for its desa breakdown and the share of each desa it "
         + "covers. Click a <b>desa</b> row below, or a desa on the map, for "
         + "that desa's own area, people and masts.");
  }

  // ── a drawn territory ─────────────────────────────────────────────────
  function showTerritory(feat) {
    var p = feat.properties || {};
    var code = p.dse;
    var n = ++dTicket;
    dLabel = code;
    if (!dOpen("Area profile", code,
               modelMode + " model · " + p.area_km2 + " km²")) return;

    dOutRows = outletsIn(function (r) { return r.dse === code; });
    renderOut();
    dDesaRows = [];
    renderDesa();
    tiles([["outlets", num(dOutRows.length)],
           ["desa", num(p.desa || 0)],
           ["sites", num(p.sites || 0)],
           ["km² area", p.area_km2],
           ["% desa covered", "…"],
           ["whole desa", "…"]]);
    dmsg("Measuring how much of each desa this territory covers…");

    fetch("/api/samples/area-profile", {
      method: "POST", headers: { "Content-Type": "application/json" },
      // The polygon only. Not one column of the workbook goes with it.
      body: JSON.stringify({ geometry: feat.geometry, dse: code,
                             mode: modelMode })
    }).then(function (r) { return r.json(); })
      .then(function (d) {
        // A later click has already claimed the panel: drop this answer
        // rather than overwriting what the reader is now looking at.
        if (n !== dTicket) return;
        if (d.ok === false) { dmsg(esc(d.error || "could not measure"), true); return; }
        var by = perDesa(dOutRows);
        dDesaRows = (d.desa || []).map(function (r) {
          r.outlets = by[dkey(r.desa, r.kecamatan)] || 0;
          return r;
        });
        renderDesa();
        tiles([["outlets", num(dOutRows.length)],
               ["desa covered", num(d.desa_count)],
               ["sites", num(d.sites)],
               ["km² area", km2(d.area_km2)],
               ["% desa covered",
                d.coverage_pct == null ? "—" : d.coverage_pct + "%"],
               ["whole desa", num(d.desa_full) + " of " + num(d.desa_count)],
               ["people", num(d.population)]]);
        dmsg("<b>" + num(d.desa_full) + "</b> desa held whole, <b>"
          + num(d.desa_partial) + "</b> in part. Coverage is measured on a "
          + d.grid + "×" + d.grid + " lattice inside each desa — good to "
          + "about a percent, not an exact clip. Outlet rows come from your "
          + "workbook in this browser; desa, area and sites come from the "
          + "application's own layers.");
      })
      .catch(function (e) {
        if (n !== dTicket) return;
        dmsg("Could not measure that territory: " + esc(String(e)), true);
      });
  }

  // ── one desa, on its own ──────────────────────────────────────────────
  function showDesaByName(desa, kec) {
    var n = ++dTicket;
    dLabel = desa;
    if (!dOpen("Desa profile", desa, kec || "")) return;

    var k = dkey(desa, kec);
    dOutRows = outletsIn(function (r) {
      var a = r.attrs || {};
      return dkey(a.desa, a.kecamatan) === k
        || (!kec && nrm(a.desa) === nrm(desa));
    });
    renderOut();
    var reps = {};
    dOutRows.forEach(function (r) { reps[r.dse] = (reps[r.dse] || 0) + 1; });
    var repN = Object.keys(reps).length;

    dDesaRows = [];
    renderDesa();
    tiles([["outlets", num(dOutRows.length)], ["DSE here", num(repN)],
           ["sites", "…"], ["km² area", "…"], ["people", "…"]]);
    dmsg("Reading this desa from the application's own layer…");

    fetch("/api/samples/desa-profile?desa=" + encodeURIComponent(desa)
          + "&kecamatan=" + encodeURIComponent(kec || ""))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (n !== dTicket) return;
        if (d.ok === false) { dmsg(esc(d.error || "not found"), true); return; }
        $("smpDSub").textContent = [d.kecamatan, d.kabupaten, d.mc]
          .filter(Boolean).join(" · ");
        // A desa profile is one row at 100% of itself: the same table shape
        // as a territory's, so the two never need reading differently.
        dDesaRows = [{ desa: d.desa, kecamatan: d.kecamatan, mc: d.mc,
                       desa_km2: d.desa_km2, inside_km2: d.desa_km2,
                       pct: 100.0, outlets: dOutRows.length,
                       sites: d.sites, population: d.population }];
        renderDesa();
        tiles([["outlets", num(dOutRows.length)],
               ["DSE here", num(repN)],
               ["sites", num(d.sites)],
               ["km² area", km2(d.desa_km2)],
               ["people", num(d.population)],
               ["outlets per km²", d.desa_km2
                 ? (dOutRows.length / d.desa_km2).toFixed(1) : "—"]]);
        var top = Object.keys(reps).sort(function (a, b) {
          return reps[b] - reps[a]; }).slice(0, 6);
        dmsg(top.length
          ? "Worked by " + top.map(function (c) {
              return "<b>" + esc(c) + "</b> " + num(reps[c]); }).join(" · ")
            + (repN > top.length ? " and " + (repN - top.length) + " more" : "")
            + ". Outlet rows are from your workbook in this browser; area, "
            + "population and sites are the application's own."
          : "No outlet from the loaded workbook is filed in this desa.");
      })
      .catch(function (e) {
        if (n !== dTicket) return;
        dmsg("Could not read that desa: " + esc(String(e)), true);
      });
  }

  // ── a kecamatan, microcluster or kabupaten ────────────────────────────
  // These are answered entirely from the workbook, because the question a
  // reader asks of a coarse polygon is "who of mine is in here", and the
  // desa breakdown underneath it is already in the file.
  function showCoarse(feat, key, label) {
    var p = feat.properties || {};
    var name = p.name || p.kec || p.Kec || p.kab_kot || p.KAB_KOT
             || p.mc || p.mc281 || label;
    var want = nrm(name);
    ++dTicket;
    dLabel = name;
    if (!dOpen(label + " profile", name,
               "from your workbook · " + label.toLowerCase())) return;

    dOutRows = outletsIn(function (r) {
      var a = r.attrs || {};
      return nrm(a.kecamatan) === want || nrm(a.mc) === want
          || nrm(a.kabupaten) === want;
    });
    renderOut();

    var reps = {}, by = {}, meta = {};
    dOutRows.forEach(function (r) {
      var a = r.attrs || {};
      reps[r.dse] = (reps[r.dse] || 0) + 1;
      var k = dkey(a.desa, a.kecamatan);
      by[k] = (by[k] || 0) + 1;
      if (!meta[k]) meta[k] = { desa: a.desa || "—",
                                kecamatan: a.kecamatan || "", mc: a.mc || "" };
    });
    dDesaRows = Object.keys(by).map(function (k) {
      return { desa: meta[k].desa, kecamatan: meta[k].kecamatan,
               mc: meta[k].mc, desa_km2: null, inside_km2: null,
               pct: null, outlets: by[k], sites: null, population: 0 };
    }).sort(function (a, b) { return b.outlets - a.outlets; });
    renderDesa();

    tiles([["outlets", num(dOutRows.length)],
           ["desa in file", num(dDesaRows.length)],
           ["DSE here", num(Object.keys(reps).length)],
           ["people", p.population != null ? num(p.population) : "—"]]);
    dmsg(dOutRows.length
      ? "Every figure here is from your workbook. Click a desa row for its "
        + "area, population and masts from the application's own layers."
      : "No outlet from the loaded workbook is filed under this "
        + esc(label.toLowerCase()) + ".");
  }

  // ── tabs, export, close ───────────────────────────────────────────────
  if ($("smpDTabs")) {
    $("smpDTabs").addEventListener("click", function (e) {
      var b = e.target.closest("button[data-dtab]");
      if (!b) return;
      dTab = b.dataset.dtab;
      $("smpDTabs").querySelectorAll("button").forEach(function (x) {
        x.classList.toggle("on", x.dataset.dtab === dTab);
      });
      document.querySelectorAll("[data-dpane]").forEach(function (x) {
        x.hidden = x.dataset.dpane !== dTab;
      });
    });
  }
  if ($("smpDClose")) {
    $("smpDClose").addEventListener("click", function () {
      ++dTicket;
      $("smpDetail").hidden = true;
    });
  }

  // The export is built and downloaded here. Nothing is posted to produce
  // it, so a report of somebody's workbook never becomes a copy of it on
  // the server on its way back to the desk it came from.
  if ($("smpDCsv")) {
    $("smpDCsv").addEventListener("click", function () {
      function q(v) {
        var s = String(v == null ? "" : v);
        return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
      }
      var head, body;
      if (dTab === "desa") {
        head = ["Desa", "Kecamatan", "Microcluster", "Desa km2",
                "Covered km2", "% covered", "Outlets", "Sites", "Population"];
        body = dDesaRows.map(function (r) {
          return [r.desa, r.kecamatan, r.mc, r.desa_km2, r.inside_km2,
                  r.pct, r.outlets, r.sites, r.population].map(q).join(",");
        });
      } else {
        head = ["Outlet code", "Outlet", "DSE", "Desa", "Kecamatan",
                "Category", "Remarks", "Latitude", "Longitude"];
        body = dOutRows.map(function (r) {
          var a = r.attrs || {};
          return [r.outlet_code, r.outlet_name, r.dse, a.desa, a.kecamatan,
                  a.category, a.remarks, r.lat, r.lon].map(q).join(",");
        });
      }
      if (!body.length) { dmsg("There is nothing in that tab to export.", true); return; }
      var url = URL.createObjectURL(new Blob(
        ["﻿" + head.join(",") + "\n" + body.join("\n")],
        { type: "text/csv;charset=utf-8" }));
      var a = document.createElement("a");
      a.href = url;
      a.download = (String(dLabel).replace(/[^A-Za-z0-9_-]+/g, "-") || "area")
        + "-" + dTab + ".csv";
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    });
  }

  // ── the two ways in ───────────────────────────────────────────────────
  // A click aimed explicitly at the territory model, resolved by the map
  // rather than by whatever Leaflet drew on top.
  window.addEventListener("pv:modelclick", function (e) {
    var f = (e.detail || {}).feature;
    if (!f) {
      if (!model) {
        dmsg("No territory is built yet — build one above, then click it.", true);
      } else {
        dmsg("No DSE territory covers that point.", true);
      }
      return;
    }
    soloModel(f.properties.dse);
    showTerritory(f);
  });

  window.addEventListener("pv:areaclick", function (e) {
    var d = e.detail || {};
    if (!d.feature) {
      if (d.miss) {
        var u = d.under;
        dOpen(d.label + " profile",
              u ? "Not in this layer" : "Nothing there",
              u ? (u.desa + " · " + (u.kec || "")) : "");
        dDesaRows = []; dOutRows = [];
        renderDesa(); renderOut(); tiles([]);
        dmsg(u
          ? "<b>" + esc(u.desa) + "</b> sits in <b>"
            + esc(u.kec || "an area") + "</b>, which has no "
            + esc(String(d.label).toLowerCase())
            + " boundary in the loaded layer. Switch the target to "
            + "Desa / Kelurahan to read it."
          : "No " + esc(String(d.label).toLowerCase())
            + " polygon covers that point.", true);
      }
      return;
    }
    var p = d.feature.properties || {};
    if (d.key === "kelurahan")
      showDesaByName(p.name || p.kel_des || p.KEL_DES || "",
                     p.kecamatan || p.kec || p.KEC || "");
    else
      showCoarse(d.feature, d.key, d.label || "Area");
  });

  // What the loaded workbook says about an area, for the map tooltip. Rows
  // already in memory, so a hover costs nothing and fires no request.
  M.areaHint = function (feat, key) {
    if (!loaded || !loaded.rows.length) return null;
    var p = feat.properties || {};
    var rows;
    if (key === "kelurahan") {
      // The territory endpoint lower-cases every source column, so a desa
      // arrives with kec and kel_des, never the tidy `kecamatan` this used
      // to read -- which was undefined on every desa in the layer and made
      // the join key miss every time.
      var k = dkey(p.name || p.kel_des || p.KEL_DES,
                   p.kecamatan || p.kec || p.KEC);
      rows = loaded.rows.filter(function (r) {
        var a = r.attrs || {};
        return dkey(a.desa, a.kecamatan) === k;
      });
    } else {
      var want = nrm(p.name || p.kec || p.Kec || p.kab_kot || p.KAB_KOT
                     || p.mc || p.mc281);
      if (!want) return null;
      rows = loaded.rows.filter(function (r) {
        var a = r.attrs || {};
        return nrm(a.kecamatan) === want || nrm(a.mc) === want
            || nrm(a.kabupaten) === want;
      });
    }
    if (!rows.length) return "<b>0</b> outlets in your file";
    var reps = {};
    rows.forEach(function (r) { reps[r.dse] = 1; });
    return "<b>" + num(rows.length) + "</b> outlets · <b>"
      + num(Object.keys(reps).length) + "</b> DSE <span class='muted'>"
      + "(your file)</span>";
  };

  // HOME comes from the API so the browser rejects the same off-map rows the
  // server would, rather than having its own idea of where the map is.
  fetch("/api/preview/points.geojson?kinds=outlet&limit=500")
    .then(function (r) { return r.json(); })
    .then(function (d) { if (d.home) HOME = d.home; })
    .catch(function () {});
  refreshKept();
})();
