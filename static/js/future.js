/* Future GAPURA — Stage-1 KSI v2.0 (docs/03-SCORING-MODEL.md §0).
   Rides on TMAP for the map, boundaries, service points and the POI overlay.
   Everything here is the model layer: parameters, the gate audit, the
   availability report, the ranking, the choropleth and candidate sites. */
(function () {
  "use strict";
  var T = window.TMAP, TM = T;
  if (!T || !T.map) return;
  var $ = function (id) { return document.getElementById(id); };
  var esc = T.esc, num = T.num;

  var last = null, byKey = {};
  var candLayer = null, candRows = [], candBatch = null;
  var showGated = false;

  var RAMP = [
    { min: 80, color: "#f0d04b" }, { min: 60, color: "#f0913d" },
    { min: 40, color: "#d9527e" }, { min: 20, color: "#8c3a80" },
    { min: 0, color: "#4a3358" }
  ];
  var GATED = "#2a2f3d";

  function rampColor(s) {
    if (s === null || s === undefined) return GATED;
    for (var i = 0; i < RAMP.length; i++) if (s >= RAMP[i].min) return RAMP[i].color;
    return RAMP[RAMP.length - 1].color;
  }
  function n1(v) { return v === null || v === undefined ? "—" : Number(v).toFixed(1); }
  function n0(v) { return v === null || v === undefined ? "—" : Math.round(v); }

  // ── parameters ────────────────────────────────────────────────────────
  var FIELDS = {
    strategy: "pStrategy", scope: "pScope",
    pop_metric: "pPopMetric",
    dist_weight: "pDistW", dist_zero_km: "pDistZero", dist_full_km: "pDistFull",
    min_km_hybrid: "pKmHybrid",
    min_km_im3: "pKmIm3", min_km_3id: "pKm3id", vlr_min: "pVlr",
    vlr_pct_min: "pVlrPct", ms_pct_min: "pMsMin", ms_pct_max: "pMsMax",
    top_banks: "pBanks", radius_urban_km: "pRadU", radius_rural_km: "pRadR"
  };
  var DEF = { pStrategy: "DENSIFY", pScope: "stratum", pDistMode: "term",
              pPopMetric: "density", pDistW: 0.15, pDistZero: 2, pDistFull: 15,
              pKmHybrid: 10, pKmIm3: 10, pKm3id: 10, pVlr: 80000, pVlrPct: 40,
              pMsMin: 40, pMsMax: 50, pBanks: "BCA, Bank Mandiri, BNI, BRI",
              pRadU: 1.5, pRadR: 3 };
  var CHECKS = { pHybrid: true, pIpp: false, pGates: true, pCpi: false };

  function params() {
    var p = {};
    Object.keys(FIELDS).forEach(function (k) { p[k] = $(FIELDS[k]).value; });
    // One control drives two server parameters: distance is either a scored
    // term or gate G1 pointing one of two ways, never both.
    var mode = $("pDistMode").value;
    p.dist_term = mode === "term" ? "1" : "0";
    p.g1_mode = mode === "max" ? "max" : "min";
    p.hybrid_own = $("pHybrid").checked ? "1" : "0";
    p.own_includes_ipp = $("pIpp").checked ? "1" : "0";
    p.gates = $("pGates").checked ? "1" : "0";
    p.use_cpi = $("pCpi").checked ? "1" : "0";
    return p;
  }
  function query() {
    var p = params(), q = new URLSearchParams();
    Object.keys(p).forEach(function (k) { if (p[k] !== "") q.set(k, p[k]); });
    return q;
  }

  // ── choropleth ────────────────────────────────────────────────────────
  function repaint() {
    var b = T.boundary && T.boundary.kecamatan;
    if (!b) return;
    b.layer.eachLayer(function (lyr) {
      var row = byKey[((lyr.feature || {}).properties || {}).join_key];
      var col = row ? rampColor(row.score) : GATED;
      var st = { color: col, weight: 1, opacity: .9, fillColor: col,
                 fillOpacity: row && row.score !== null ? .55 : .12, dashArray: null };
      lyr.setStyle(st);
      // TMAP's own mouseout resets to the plain boundary style. Ours is
      // registered afterwards so the score colour is what survives the
      // hover, instead of the map going grey as the cursor crosses it.
      if (!lyr._futureBound) {
        lyr._futureBound = true;
        lyr.on("mouseout", function () { lyr.setStyle(lyr._futureStyle); });
        lyr.on("click", function (e) {
          var r = byKey[(lyr.feature.properties || {}).join_key];
          if (r) { L.DomEvent.stop(e); showBreakdown(r); }
        });
      }
      lyr._futureStyle = st;
      if (row) {
        lyr.setTooltipContent(
          "<strong>" + esc(row.kecamatan) + "</strong><br>" + esc(row.kabkot || "")
          + ' <span class="muted">' + row.profile + "</span><br>"
          + (row.score === null
             ? '<span style="color:#d9744b">gated out — ' + esc(row.gate_fails[0] || "") + "</span>"
             : "KSI <b>" + row.score.toFixed(1) + "</b> (#" + row.rank
               + " overall, #" + row.rank_in_profile + " in " + row.profile + ")")
          + "<br>VLR " + num(row.vlr) + " · pct " + n0(row.vlr_pct)
          + " · share pct " + n0(row.ms_pct)
          + "<br>CVI " + n0(row.terms.cvi) + " · CPI " + n0(row.terms.cpi)
          + " · NET " + n0(row.terms.network)
          + "<br>nearest own counter " + n1(row.km_hybrid) + " km ("
          + n1(row.km_im3) + " km IM3 · " + n1(row.km_3id) + " km 3Store)");
      }
    });
    T.extraLegend = RAMP.slice().reverse().map(function (s, i, a) {
      return '<span class="lg">' + T.swatch("square", s.color) + s.min + "–"
        + (i === a.length - 1 ? 100 : a[i + 1].min) + "</span>";
    });
    T.extraLegend.unshift('<span class="lg"><b>KSI</b></span>');
    T.extraLegend.push('<span class="lg">' + T.swatch("square", GATED) + "gated out</span>");
    T.renderLegend();
  }

  // ── gate audit and availability ───────────────────────────────────────
  function renderGates(d) {
    $("gateBox").innerHTML = (d.gate_stats || []).map(function (g) {
      return '<div class="gaterow' + (g.active ? "" : " off") + '"><span class="k">'
        + esc(g.label) + '</span><span class="v">' + g.passes + "</span></div>";
    }).join("")
      + '<div class="gaterow final"><span class="k">pass every gate</span>'
      + '<span class="v">' + d.n_eligible + " of " + d.n_total + "</span></div>";

    var miss = [], av = d.availability || {};
    Object.keys(av).forEach(function (k) {
      if (k.charAt(0) === "_" || av[k].ok) return;
      miss.push({ k: k, why: av[k].why, w: d.ksi_weights_spec[k] });
    });
    var sub = av._cvi_sub || {};
    Object.keys(sub).forEach(function (k) {
      if (sub[k].ok) return;
      miss.push({ k: "CVI · " + k, why: sub[k].why, w: d.cvi_weights_spec[k], inCvi: true });
    });
    var g4 = (av._gates || {}).G4;
    var html = '<div class="gaterow"><span class="k"><b>Model as run</b></span></div>'
      + '<div class="gaterow"><span class="k">effective KSI weights</span></div>';
    Object.keys(d.ksi_weights).forEach(function (k) {
      var spec = d.ksi_weights_spec[k];
      html += '<div class="gaterow"><span class="k">' + esc(k) + '</span><span class="v">'
        + d.ksi_weights[k].toFixed(3)
        + (Math.abs(d.ksi_weights[k] - spec) > 1e-6
           ? ' <span class="muted">was ' + spec.toFixed(2) + "</span>" : "")
        + "</span></div>";
    });
    if (miss.length) {
      html += '<div class="gaterow"><span class="k"><b>Not available — weight '
        + "redistributed</b></span></div>";
      miss.forEach(function (m) {
        html += '<div class="gaterow"><span class="k gatetag">' + esc(m.k)
          + " (" + (m.w !== undefined ? m.w.toFixed(2) : "—") + ")</span></div>"
          + '<div class="gaterow off"><span class="k">' + esc(m.why) + "</span></div>";
      });
    }
    if (g4 && !g4.ok)
      html += '<div class="gaterow off"><span class="k">' + esc(g4.why) + "</span></div>";
    $("availBox").innerHTML = html;

    $("modelNote").innerHTML =
      esc(d.model) + ". Percentiles are computed "
      + (d.params.scope === "stratum"
         ? "<b>within stratum</b> — " + d.n_urban + " urban and " + d.n_rural
           + " rural kecamatan ranked separately, per §1"
         : "<b>across the whole territory</b>")
      + ". G1 measures from the kecamatan centroid to "
      + (d.params.hybrid_own
         ? "the nearest of <b>" + d.n_hybrid + "</b> own counters (<b>"
           + d.n_im3 + "</b> IM3 gerai + <b>" + d.n_3id + "</b> 3Store), "
           + "treated as one interchangeable network"
         : "<b>" + d.n_im3 + "</b> IM3 gerai and <b>" + d.n_3id
           + "</b> 3Store <b>separately</b> — with a 3Store list that short "
           + "its half of the gate is nearly inert")
      + ". Read the per-gate counts above before trusting the shortlist.";
  }

  // ── ranking table ─────────────────────────────────────────────────────
  function renderTable() {
    var rows = last.rows.filter(function (r) { return showGated || r.score !== null; });
    var head = [["#", 0], ["Kecamatan", 0], ["Kabupaten / Kota", 0], ["Profile", 0],
                ["KSI", 1], ["#prof", 1], ["Terr", 1], ["VLR%", 1], ["Share%", 1],
                ["CVI", 1], ["CPI", 1], ["NET", 1], ["Mini", 1], ["ATM", 1],
                ["Bank", 1], ["km own", 1], ["km IM3", 1], ["km 3ID", 1],
                ["km²", 1]];
    var html = '<table class="rank"><thead><tr>' + head.map(function (h) {
      return "<th" + (h[1] ? ' style="text-align:right"' : "") + ">" + h[0] + "</th>";
    }).join("") + "</tr></thead><tbody>";
    rows.forEach(function (r) {
      var t = r.terms || {};
      html += '<tr class="pick' + (r.score === null ? " gated" : "")
        + '" data-key="' + esc(r.join_key) + '">'
        + '<td class="n">' + (r.rank || "—") + "</td>"
        + "<td>" + esc(r.kecamatan) + "</td><td>" + esc(r.kabkot || "") + "</td>"
        + '<td><span class="pchip ' + r.profile + '">' + r.profile + "</span></td>"
        + '<td class="n">' + (r.score === null
            ? '<span class="gatetag">' + esc((r.gate_fails[0] || "gated").slice(0, 26)) + "</span>"
            : '<span class="scorechip" style="background:' + rampColor(r.score) + '">'
              + r.score.toFixed(1) + "</span>") + "</td>"
        + '<td class="n">' + (r.rank_in_profile || "—") + "</td>"
        + '<td class="n">' + n1(r.score_territory) + "</td>"
        + '<td class="n">' + n0(r.vlr_pct) + "</td>"
        + '<td class="n">' + n0(r.ms_pct) + "</td>"
        + '<td class="n">' + n0(t.cvi) + "</td>"
        + '<td class="n">' + n0(t.cpi) + "</td>"
        + '<td class="n">' + n0(t.network) + "</td>"
        + '<td class="n">' + num(r.minimarket_n) + "</td>"
        + '<td class="n">' + num(r.atm_n) + "</td>"
        + '<td class="n">' + num(r.bank_n) + "</td>"
        + '<td class="n">' + n1(r.km_hybrid) + "</td>"
        + '<td class="n">' + n1(r.km_im3) + "</td>"
        + '<td class="n">' + n1(r.km_3id) + "</td>"
        + '<td class="n">' + Number(r.area_km2).toFixed(1) + "</td></tr>";
    });
    $("tabKec").innerHTML = html + "</tbody></table>";
    $("tabKec").querySelectorAll("tr.pick").forEach(function (tr) {
      tr.addEventListener("click", function () { showBreakdown(byKey[tr.dataset.key]); });
    });
    $("rankTotal").innerHTML = last.n_eligible + " of " + last.n_total
      + ' pass the gates &middot; <a href="#" id="toggleGated">'
      + (showGated ? "hide" : "show") + " the " + (last.n_total - last.n_eligible)
      + " gated out</a>";
    var tg = $("toggleGated");
    if (tg) tg.addEventListener("click", function (e) {
      e.preventDefault(); showGated = !showGated; renderTable(); });
  }

  // ── score breakdown ───────────────────────────────────────────────────
  function showBreakdown(r) {
    if (!r) return;
    var isCand = r.location !== undefined;
    var title = isCand ? (r.location || "Candidate site") : r.kecamatan;
    var sub = isCand
      ? (r.kecamatan || "—") + " · " + (r.kabkot || "") + " · catchment " + r.radius_km + " km"
      : (r.kabkot || "") + (r.mc ? " · " + r.mc : "") + " · " + Number(r.area_km2).toFixed(1) + " km²";
    var h = '<div class="hit-grid"><div class="hit"><h3>' + esc(title) + "</h3>"
      + '<div class="sub">' + esc(sub) + ' &middot; <span class="pchip '
      + r.profile + '">' + r.profile + "</span></div>";
    if (r.score === null || r.score === undefined) {
      h += '<p class="gatetag">Not scored: '
        + esc((r.gate_fails && r.gate_fails.length ? r.gate_fails : [r.error]).join("; "))
        + "</p>";
    } else {
      h += '<p style="margin:0 0 8px"><span class="scorechip" style="background:'
        + rampColor(r.score) + ';font-size:15px;padding:3px 10px">' + r.score.toFixed(1)
        + "</span> " + (r.rank ? '<span class="muted small">#' + r.rank
            + (r.rank_in_profile ? " overall · #" + r.rank_in_profile + " in " + r.profile : "")
            + "</span>" : "") + "</p>";
      h += '<div class="bd"><span class="hd">term</span><span class="hd n">pct</span>'
        + '<span class="hd n">weight</span><span class="hd n">points</span>';
      (r.parts || []).forEach(function (x) {
        h += "<span>" + esc(x.label) + "</span>"
          + '<span class="n">' + n1(x.pct) + "</span>"
          + '<span class="n">' + Number(x.weight).toFixed(3) + "</span>"
          + '<span class="n">' + Number(x.points).toFixed(1) + "</span>";
      });
      h += "</div>";
      if (!isCand && r.sub) {
        h += '<div class="sub" style="margin-top:10px">inside CVI, CPI and network</div>'
          + '<div class="bd"><span class="hd">component</span><span class="hd n">pct</span>'
          + '<span class="hd n"></span><span class="hd n"></span>';
        [["ATM density", r.sub.atm_den], ["Bank density", r.sub.bank_den],
         ["Minimarket density", r.sub.minimarket], ["Pasar density", r.sub.pasar],
         ["Competitor store density", r.sub.comp_den],
         ["RSoV gap", r.sub.rsov_gap], ["Retail share of voice", r.sub.retail_sov],
         ["VLR per site", r.sub.vlr_per_site], ["Site count", r.sub.site_count]
        ].forEach(function (p) {
          h += "<span>" + p[0] + '</span><span class="n">' + n1(p[1])
            + '</span><span></span><span></span>';
        });
        h += "</div>";
        h += '<div class="sub" style="margin-top:10px">raw</div><div class="bd">'
          + "<span>own / competitor / IPP stores</span>"
          + '<span class="n">' + r.own_stores + " / " + r.comp_stores + " / "
          + r.ipp_stores + '</span><span></span><span></span>'
          + "<span>minimarket / ATM / bank / top-4 bank</span>"
          + '<span class="n">' + r.minimarket_n + " / " + r.atm_n + " / "
          + r.bank_n + " / " + r.bank_top4_n + '</span><span></span><span></span>'
          + "<span>VLR · sites · VLR per site</span>"
          + '<span class="n">' + num(r.vlr) + " · " + r.site_count + " · "
          + num(r.vlr_per_site) + '</span><span></span><span></span>'
          + "<span>market share IOH</span><span class=\"n\">"
          + (r.ms_ioh == null ? "—" : (100 * r.ms_ioh).toFixed(1) + "%")
          + '</span><span></span><span></span></div>';
      }
      if (r.gate_fails && r.gate_fails.length)
        h += '<p class="gatetag" style="margin-top:8px">Would fail: '
          + esc(r.gate_fails.join("; ")) + "</p>";
    }
    if (isCand && r.kecamatan_matches_input === false)
      h += '<p class="gatetag" style="margin-top:8px">The spreadsheet said &ldquo;'
        + esc(r.kecamatan_input || "") + '&rdquo;; the coordinate falls in '
        + esc(r.kecamatan) + ".</p>";
    $("panel").innerHTML = h + "</div></div>";
    TM.showPane("paneSel");
    if (r.lat != null && r.lon != null) T.map.setView([r.lat, r.lon], isCand ? 14 : 12);
  }

  // ── run ───────────────────────────────────────────────────────────────
  function run() {
    T.status("scoring kecamatan…");
    return fetch("/api/siting/rank?" + query().toString())
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.ok) { T.status(d.error || "siting failed"); return false; }
        last = d; byKey = {};
        d.rows.forEach(function (r) { byKey[r.join_key] = r; });
        renderGates(d); renderTable(); repaint(); syncExport();
        // The bar has to describe the model that ran, not the one on paper:
        // with the distance term on this is a local variant and G1 is not
        // gating, so a chip reading "G1 hybrid >= 10 km" would be false.
        var dterm = "distance" in (d.ksi_weights || {});
        TM.configChips = [
          TM.chip("model", dterm ? "KSI v2.0 + local dist" : "KSI v2.0",
                  dterm ? "warn" : "key"),
          TM.chip("strategy", d.params.strategy),
          TM.chip("percentiles", d.params.scope === "stratum"
            ? "within stratum" : "whole territory"),
          TM.chip("population", d.params.pop_metric === "density"
            ? "per km\u00b2" : "head count"),
          TM.chip("CPI", "cpi" in (d.ksi_weights || {}) ? "in" : "removed",
                  "cpi" in (d.ksi_weights || {}) ? "" : "warn"),
          dterm
            ? TM.chip("distance", "scored " + d.params.dist_zero_km
                + "\u2013" + d.params.dist_full_km + " km \u00b7 w "
                + d.params.dist_weight)
            : TM.chip("G1", (d.params.hybrid_own ? "hybrid " : "split ")
                + (d.params.g1_mode === "min" ? "\u2265 " : "\u2264 ")
                + (d.params.hybrid_own ? d.params.min_km_hybrid
                                       : d.params.min_km_im3)
                + " km"),
          TM.chip("VLR", "\u2265 " + num(d.params.vlr_min) + " · pct \u2265 "
            + d.params.vlr_pct_min),
          TM.chip("passing", d.n_eligible + " of " + d.n_total,
                  d.n_eligible ? "key" : "warn")
        ];
        TM.renderConfigBar();
        T.status(d.n_eligible + " of " + d.n_total + " kecamatan pass the gates");
        if (candBatch)
          $("uploadNote").innerHTML = "Parameters changed since this batch was "
            + "scored. Press <b>Score sites</b> again to re-run it under the "
            + "current gates.";
        return true;
      })
      .catch(function (e) { T.status("siting failed: " + e); return false; });
  }

  // ── candidate sites ───────────────────────────────────────────────────
  function drawCandidates() {
    if (candLayer) { T.map.removeLayer(candLayer); candLayer = null; }
    if (!candRows.length) return;
    candLayer = L.layerGroup();
    candRows.forEach(function (r) {
      if (r.lat == null || r.lon == null) return;
      L.marker([r.lat, r.lon], {
        icon: L.divIcon({ className: "", iconSize: [22, 22], iconAnchor: [11, 11],
          html: '<div class="candmark" style="background:' + rampColor(r.score)
            + '">' + (r.rank || "?") + "</div>" })
      }).bindTooltip("<strong>" + esc(r.location || "site " + r.row_no) + "</strong><br>"
        + esc(r.kecamatan || r.error || "")
        + (r.score != null ? "<br>proxy score <b>" + r.score.toFixed(1)
           + "</b><br>KSI " + n1(r.kec_score) + " · local CVI " + n1(r.cvi_local)
           + "<br>" + r.minimarket + " minimarket · " + r.atm + " ATM · "
           + r.bank_all + " bank within " + r.radius_km + " km" : ""),
        { className: "terr-tip" })
        .on("click", function () { showBreakdown(r); })
        .addTo(candLayer);
    });
    candLayer.addTo(T.map);
  }

  function renderCandidates() {
    if (!candRows.length) {
      $("tabCand").innerHTML = '<span class="muted small">No candidate batch loaded.</span>';
      return;
    }
    var head = [["#", 0], ["Location", 0], ["Kecamatan (typed)", 0],
                ["Kecamatan (geometry)", 0], ["Profile", 0], ["Proxy", 1],
                ["KSI kec", 1], ["CVI local", 1], ["Mini", 1], ["ATM", 1],
                ["Bank", 1], ["km IM3", 1], ["km 3ID", 1], ["Gates", 0]];
    var html = '<p class="hint">' + esc(candProxyNote || "") + "</p>"
      + '<table class="rank"><thead><tr>' + head.map(function (h) {
      return "<th" + (h[1] ? ' style="text-align:right"' : "") + ">" + h[0] + "</th>";
    }).join("") + "</tr></thead><tbody>";
    candRows.forEach(function (r) {
      html += '<tr class="pick' + (r.score == null ? " gated" : "") + '" data-row="'
        + r.row_no + '"><td class="n">' + (r.rank || "—") + "</td>"
        + "<td>" + esc(r.location || "") + "</td>"
        + "<td" + (r.kecamatan_matches_input === false ? ' class="gatetag"' : "") + ">"
        + esc(r.kecamatan_input || "") + "</td>"
        + "<td>" + esc(r.kecamatan || "") + "</td>"
        + "<td>" + (r.profile ? '<span class="pchip ' + r.profile + '">' + r.profile
            + "</span>" : "") + "</td>"
        + '<td class="n">' + (r.score == null
            ? '<span class="gatetag">' + esc(r.error || "—") + "</span>"
            : '<span class="scorechip" style="background:' + rampColor(r.score) + '">'
              + r.score.toFixed(1) + "</span>") + "</td>"
        + '<td class="n">' + n1(r.kec_score) + "</td>"
        + '<td class="n">' + n1(r.cvi_local) + "</td>"
        + '<td class="n">' + (r.minimarket == null ? "—" : num(r.minimarket)) + "</td>"
        + '<td class="n">' + (r.atm == null ? "—" : num(r.atm)) + "</td>"
        + '<td class="n">' + (r.bank_all == null ? "—" : num(r.bank_all)) + "</td>"
        + '<td class="n">' + n1(r.km_im3) + "</td>"
        + '<td class="n">' + n1(r.km_3id) + "</td>"
        + '<td class="gatetag">' + esc((r.gate_fails || []).join("; ")) + "</td></tr>";
    });
    html += "</tbody></table>";
    if (candBatch)
      html += '<p class="hint"><a href="/api/siting/batch/' + candBatch
        + '/export.csv">Download this batch as CSV</a></p>';
    $("tabCand").innerHTML = html;
    $("tabCand").querySelectorAll("tr.pick").forEach(function (tr) {
      tr.addEventListener("click", function () {
        showBreakdown(candRows.filter(function (x) {
          return String(x.row_no) === tr.dataset.row; })[0]);
      });
    });
  }
  var candProxyNote = "";

  function loadBatchList() {
    return fetch("/api/siting/batches").then(function (r) { return r.json(); })
      .then(function (d) {
        var el = $("batchList");
        if (!d.batches || !d.batches.length) {
          el.innerHTML = '<span class="muted small">No batches uploaded yet.</span>';
          return;
        }
        el.innerHTML = d.batches.map(function (b) {
          return '<label><a href="#" data-b="' + b.batch_id + '" class="bload">'
            + esc(b.label || b.source_file) + '</a><span class="cnt">'
            + b.n_scored + "/" + b.n_rows + '</span><a href="#" data-del="'
            + b.batch_id + '" class="bdel muted small" title="delete">&times;</a></label>';
        }).join("");
        el.querySelectorAll(".bload").forEach(function (a) {
          a.addEventListener("click", function (e) { e.preventDefault(); openBatch(a.dataset.b); });
        });
        el.querySelectorAll(".bdel").forEach(function (a) {
          a.addEventListener("click", function (e) {
            e.preventDefault();
            fetch("/api/siting/batch/" + a.dataset.del + "/delete", { method: "POST" })
              .then(function () {
                if (candBatch === a.dataset.del) {
                  candBatch = null; candRows = []; drawCandidates(); renderCandidates();
                }
                loadBatchList();
              });
          });
        });
      });
  }

  function openBatch(id) {
    return fetch("/api/siting/batch/" + id).then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.ok) { T.status(d.error); return; }
        candBatch = id; candRows = d.rows; candProxyNote = "";
        renderCandidates(); drawCandidates(); tab("cand"); syncExport();
        $("uploadNote").textContent = "Scored under " + (d.params.strategy || "")
          + ", G1 " + (d.params.g1_mode || "") + " " + d.params.min_km_im3
          + " km, VLR >= " + num(d.params.vlr_min) + ".";
      });
  }

  function upload() {
    var f = $("siteFile").files[0];
    if (!f) { $("uploadNote").textContent = "Choose an .xlsx file first."; return; }
    var fd = new FormData();
    fd.append("file", f);
    fd.append("label", $("siteLabel").value || "");
    var p = params();
    Object.keys(p).forEach(function (k) { fd.append(k, p[k]); });
    $("uploadNote").textContent = "scoring…";
    fetch("/api/siting/upload", { method: "POST", body: fd })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.ok) {
          $("uploadNote").innerHTML = '<span class="gatetag">' + esc(d.error) + "</span>";
          return;
        }
        candBatch = d.batch_id; candRows = d.rows; candProxyNote = d.proxy_note || "";
        renderCandidates(); drawCandidates(); tab("cand"); loadBatchList();
        syncExport();
        var out = d.rows.filter(function (r) { return r.error; }).length;
        $("uploadNote").innerHTML = d.n_scored + " of " + d.n + " sites scored"
          + (out ? ', <span class="gatetag">' + out
             + " outside every kecamatan boundary</span>" : "") + ".";
      })
      .catch(function (e) { $("uploadNote").textContent = "upload failed: " + e; });
  }

  // ── tabs and wiring ───────────────────────────────────────────────────
  // The results panes are the shared ones now, so a click on the map and a
  // click on a table row can bring the same breakdown pane forward.
  function tab(which) {
    TM.showPane(which === "cand" ? "tabCand" : which === "kec" ? "tabKec" : which);
  }

  // The download must carry whatever is on screen, so its href is rebuilt
  // every time the model runs or a batch is loaded rather than being fixed
  // at page load and quietly exporting the defaults.
  function syncExport() {
    var a = $("xlsxLink");
    if (!a) return;
    var q = query();
    if (candBatch) q.set("batch", candBatch);
    a.href = "/api/siting/export.xlsx?" + q.toString();
    a.textContent = candBatch
      ? "Download Excel analysis + sites" : "Download Excel analysis";
  }

  function syncHybrid() {
    var on = $("pHybrid").checked;
    $("hybridOn").hidden = !on;
    $("hybridOff").hidden = on;
  }

  // Distance is a scored term or a gate, never both, so only one set of
  // controls is ever live.
  function syncDist() {
    var term = $("pDistMode").value === "term";
    $("distTermBox").hidden = !term;
    $("distGateBox").hidden = term;
  }

  // ── configure, then run ───────────────────────────────────────────────
  // The model used to re-run on every dropdown change, which made a
  // half-configured combination — ATTACK still carrying the DENSIFY floor,
  // say — score itself and paint the map on the way past. Parameters are now
  // staged: edits mark the form dirty and nothing is scored until Run.
  var ranParams = null;

  function sameAsRun() {
    if (!ranParams) return false;
    var now = params();
    return Object.keys(now).every(function (k) {
      return String(now[k]) === String(ranParams[k]);
    });
  }

  function changedFields() {
    if (!ranParams) return [];
    var now = params();
    return Object.keys(now).filter(function (k) {
      return String(now[k]) !== String(ranParams[k]);
    });
  }

  function syncState() {
    var el = $("wfState");
    var btn = $("runModel");
    if (!ranParams) {
      el.className = "wfstate pending";
      el.innerHTML = "Not run yet — set the steps below, then <b>Run model</b>.";
      btn.disabled = false;
      btn.textContent = "Run model";
      return;
    }
    var ch = changedFields();
    if (!ch.length) {
      el.className = "wfstate clean";
      el.innerHTML = "Showing the model as configured. "
        + '<span class="muted">Edit any step to stage a change.</span>';
      btn.disabled = true;
      btn.textContent = "Run model";
    } else {
      el.className = "wfstate dirty";
      el.innerHTML = "<b>" + ch.length + " change" + (ch.length > 1 ? "s" : "")
        + " staged</b>, not yet scored: <span class=\"muted\">"
        + ch.map(esc).join(", ") + "</span>";
      btn.disabled = false;
      btn.textContent = "Run model \u2192";
    }
  }

  function stage() { syncDist(); syncHybrid(); syncState(); }

  $("runModel").addEventListener("click", function () {
    var sent = params();
    run().then(function (ok) { if (ok) { ranParams = sent; } syncState(); });
  });
  $("resetModel").addEventListener("click", function () {
    Object.keys(DEF).forEach(function (k) { $(k).value = DEF[k]; });
    Object.keys(CHECKS).forEach(function (k) { $(k).checked = CHECKS[k]; });
    stage();
  });

  // Every control stages rather than runs. Enter on a number field is the
  // one shortcut kept, because typing a threshold and pressing Enter is how
  // people expect a form to behave.
  Object.keys(FIELDS).concat(["pDistMode"]).forEach(function (k) {
    var id = FIELDS[k] || k, el = $(id);
    if (!el) return;
    el.addEventListener("change", stage);
    el.addEventListener("input", syncState);
    if (el.tagName === "INPUT")
      el.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); $("runModel").click(); }
      });
  });
  Object.keys(CHECKS).forEach(function (k) {
    $(k).addEventListener("change", stage); });
  $("siteUpload").addEventListener("click", upload);
  $("siteClear").addEventListener("click", function () {
    candBatch = null; candRows = []; $("siteFile").value = "";
    drawCandidates(); renderCandidates(); syncExport();
    $("uploadNote").textContent = "";
  });

  T.onReady = function () {
    loadBatchList();
    stage();
    // One run on load with the defaults, so the map is never blank; after
    // that the form is in charge.
    var sent = params();
    run().then(function (ok) { if (ok) { ranParams = sent; } syncState(); });
  };
})();
