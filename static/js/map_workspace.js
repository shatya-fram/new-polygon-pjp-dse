/* MAP WORKSPACE — reading a local demarcation against the cloud's boundaries.

   THE WHOLE POINT OF THIS PAGE IS THE LINE DOWN THE MIDDLE OF IT

   Cloud, solid-bordered: Desa / Kelurahan, Kecamatan, Kota / Kabupaten,
   Indosat microcluster and site locations. Served by this application from
   its own database, identical for every reader, versioned with the deploy.

   Local, dashed-bordered: the DSE x outlet mapping and any site list of your
   own. Read by this page from a file you point it at, parsed in the browser,
   held in one JavaScript object, drawn from memory. It is never POSTed, it
   never touches the server's disk, and it exists only until you sign out,
   clear it, or close the tab. There is no code path here that sends it
   anywhere -- not a cache, not an autosave, not a "temporary" upload.

   THE TWO ARE JOINED BY NAME, NOT BY GEOMETRY
   A demarkasi workbook already says which desa and which kecamatan every
   outlet belongs to, and that assignment is the thing being reviewed.
   Re-deriving it by dropping points into polygons would answer a different
   question -- where the pin happens to fall -- and would quietly disagree
   with the file wherever a pin sits a few metres over a boundary. So the key
   is (desa, kecamatan), upper-cased and stripped of punctuation. Desa names
   repeat across kecamatan -- MEKARSARI is four different places -- which is
   why the kecamatan is part of the key and not decoration on the end of it.

   ONE MAP, LAZY LAYERS
   Cloud layers are fetched once, on first tick, from the prebuilt gzipped
   cache, and kept. Tooltips and clicks are delegated to the layer rather
   than bound per feature: the desa layer is thousands of polygons and a
   handler each is what stalls a tab. */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  if (typeof L === "undefined" || !$("mwMap")) return;

  var T = window.TERRCFG || { lat: -6.17, lon: 106.75, home: "" };
  var HOME_NAME = T.home || "";

  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;",
               '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function num(v) { return Number(v || 0).toLocaleString(); }
  function norm(v) {
    return String(v == null ? "" : v).toUpperCase().replace(/[^A-Z0-9]/g, "");
  }
  function key(desa, kec) { return norm(desa) + "|" + norm(kec); }

  /* ─────────────────────────────────────────────────────────────────────
     THE MAP
     ───────────────────────────────────────────────────────────────────── */
  var map = L.map("mwMap", { zoomControl: true, preferCanvas: true })
              .setView([T.lat, T.lon], 11);
  // The ground is chosen in basemap.js, not fixed here: the CARTO default
  // this used to carry runs out of anonymous allowance and starts serving
  // "API KEY REQUIRED" as a picture, which no error handler can catch.
  var BASE = window.PVBASE ? window.PVBASE.attach(map) : null;
  if (BASE) BASE.mount($("mwBase"), "inp");

  /* Cloud layers, coarsest underneath, so the finest grain stays readable
     and stays the one a click lands on. */
  /* FOUR BOUNDARIES, FOUR ANSWERS TO "WHICH LINE IS THAT"

     Weight alone was not enough: 1.2px warm grey against 1.6px warm grey is
     a difference you can measure but not see, and over a city where desa and
     kecamatan edges run together for hundreds of metres a reader could not
     tell which boundary they were looking at.

     So each level now differs in BOTH hue and weight, and the hues are the
     ones the Polygon Samples page already uses for the same four layers --
     orange kabupaten, purple microcluster, cyan kecamatan, green desa. Two
     pages showing the same boundary in two different colours would be worse
     than either choice on its own.

     Coarse is heavy and dark, fine is light and thin, so the hierarchy reads
     even in a screenshot printed in grey. */
  var CLOUD = [
    { key: "kabkot",     label: "Kota / Kabupaten", z: 402,
      colour: "#E0721F", weight: 3.0, dash: "8 5" },
    { key: "indosat_mc", label: "Microcluster",     z: 404,
      colour: "#8659E8", weight: 2.6, dash: "7 5" },
    { key: "kecamatan",  label: "Kecamatan",        z: 406,
      colour: "#0F9CB0", weight: 1.9, dash: null },
    { key: "kelurahan",  label: "Desa / Kelurahan", z: 408,
      colour: "#4E9E4E", weight: 0.9, dash: null }
  ];
  var CL = {};
  CLOUD.forEach(function (c) { CL[c.key] = { def: c, layer: null, count: 0,
                                             busy: false }; });

  /* ── ONE CANVAS, NOT NINE ──────────────────────────────────────────────
     Every vector layer here used to draw into its own pane, which meant its
     own <canvas>, stacked by z-index. That looked right and was quietly
     broken: canvases are opaque to the mouse, so the topmost one swallowed
     every hover and click before the layers underneath saw them. Load a
     local file and the outlet canvas covered the whole map -- after which
     no boundary could be hovered or clicked at all.

     So there is one renderer for everything, and depth comes from draw
     order inside it. Leaflet hit-tests every drawn layer in that one canvas
     and takes the last match, which is exactly the rule we want: a dot
     beats the desa under it, a desa beats the kecamatan under that.

     ORDER HAS TO BE RE-ASSERTED, NOT ASSUMED
     A layer joins at the top of the draw order whenever it is added, and
     these arrive whenever their fetch happens to land. reorder() puts them
     back into coarse-to-fine order after every change; it is cheap and it
     is the only thing keeping the hit rule true. */
  var RENDER = L.canvas({ padding: 0.3 });

  // bottom to top: coarse boundaries, fine boundaries, then the points.
  var DEPTH = ["kabkot", "indosat_mc", "kecamatan", "kelurahan"];

  function front(layer) {
    if (!layer) return;
    if (layer.eachLayer) layer.eachLayer(function (l) {
      if (l.bringToFront) l.bringToFront(); });
    else if (layer.bringToFront) layer.bringToFront();
  }
  function reorder() {
    DEPTH.forEach(function (k) { if (CL[k]) front(CL[k].layer); });
    front(CASE);
    front(MODEL);
    front(lsiteLayer);
    front(outLayer);
    front(spotLayer);
    // The PJP flags sit above everything they annotate. Added last because
    // reorder() runs after drawReb() adds the layer, and an add alone would
    // be undone by the fronting above it.
    front(rebTargetLayer);
    front(rebLayer);
    front(pickLayer);
  }

  var home = null;                   // the frame "Reset view" goes back to
  var pickLayer = null;              // the red outline of the current pick
  var busyN = 0;

  function busy(on) {
    busyN = Math.max(0, busyN + (on ? 1 : -1));
    $("mwBusy").hidden = busyN === 0;
  }

  /* WHERE THE NAMES ACTUALLY LIVE

     The territory endpoint lower-cases every source column, so a desa
     arrives carrying kec, kab_kot, kel_des and mc281 -- not the Kecamatan,
     Kabupaten and MC the KML headers use, and not the tidy `kecamatan` a
     reader of this file would assume. Reading p.kecamatan returned
     undefined on every desa in the layer, which silently broke the join
     key: KAMPUNGBALI| never matches KAMPUNGBALI|TANAHABANG, so a desa's
     figures came back empty however correct the table behind them was.

     Every property is read through these four from here on, and each spells
     out the variants rather than trusting one. */
  function featName(p) {
    return p.name || p.kel_des || p.KEL_DES || p.Kelurahan || p.Desa
        || p.kec || p.Kec || p.kab_kot || p.KAB_KOT
        || p["MC IOH"] || p.MC36 || p.mc || "";
  }
  function featKec(p) {
    return p.kecamatan || p.kec || p.KEC || p.Kec || "";
  }
  function featKab(p) {
    return p.kabupaten || p.kab_kot || p.KAB_KOT || p.Kabupaten || "";
  }
  function featMc(p) {
    return p.mc || p.mc281 || p.mc260 || p.MC281 || p["MC IOH"] || p.MC36 || "";
  }
  function featPop(p) {
    var v = p.population != null ? p.population : p.jumlah_pen;
    if (v == null || v === "") return null;
    var n = parseFloat(v);
    return isFinite(n) ? Math.round(n) : null;
  }

  /* Fill carries the heatmap when it is on, and otherwise only enough wash
     to make the interior a click target rather than a hole in the map. */
  function polyStyle(c, f) {
    var p = f.properties || {};
    var st = { color: c.colour, weight: c.weight, opacity: .9,
               dashArray: c.dash, fillColor: "#ffffff", fillOpacity: .12 };
    if (heatOn && c.key === "kelurahan") {
      var n = areaCount[key(featName(p), featKec(p))] || 0;
      st.fillColor = "#ec3013";
      st.fillOpacity = n ? Math.min(.48, .06 + (n / heatMax) * .42) : .02;
    }
    return st;
  }

  function restyle(k) {
    var c = CL[k];
    if (!c || !c.layer) return;
    c.layer.setStyle(function (f) { return polyStyle(c.def, f); });
  }


  /* ══════════════════════════════════════════════════════════════════════
     WHAT A BOUNDARY IS WORTH, UNDER THE CURSOR

     A hover has to answer immediately or it answers nothing: by the time a
     request comes back the cursor has moved on. So the whole desa table --
     size, masts and people for all 7,761 of them -- is fetched once and
     every hover after that is a dictionary lookup in this page.

     Kecamatan, microcluster and kabupaten totals are added up here from
     that same desa table rather than asked for separately. Desa nest inside
     all three, so the coarse figures can never disagree with the fine ones:
     there is only one set of numbers.

     Outlets and DSE come from the loaded workbook, which is why they are
     labelled as such in the tooltip. Size, masts, desa and people are the
     application's own.
     ══════════════════════════════════════════════════════════════════════ */
  var STATS = null, statsBusy = false;
  var byDesaS = {}, byKecS = {}, byMcS = {}, byKabS = {};

  function plural(n, one, many) {
    return num(n) + " " + (n === 1 ? one : (many || one + "s"));
  }

  function blankStat() { return { km2: 0, sites: 0, pop: 0, desa: 0 }; }
  function addStat(into, k, r) {
    if (!k) return;
    var t = into[k] || (into[k] = blankStat());
    t.km2 += r[4]; t.sites += r[5]; t.pop += r[6]; t.desa += 1;
  }

  function ensureStats(then) {
    if (STATS) { if (then) then(); return; }
    if (statsBusy) return;
    statsBusy = true;
    fetch("/api/samples/area-stats")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        statsBusy = false;
        if (d.ok === false) return;
        STATS = d.rows || [];
        byDesaS = {}; byKecS = {}; byMcS = {}; byKabS = {};
        STATS.forEach(function (r) {
          // [desa, kecamatan, kabupaten, mc, km2, sites, population]
          byDesaS[key(r[0], r[1])] = { km2: r[4], sites: r[5], pop: r[6],
                                       desa: 1, kec: r[1], kab: r[2], mc: r[3] };
          addStat(byKecS, norm(r[1]), r);
          
          addStat(byKabS, norm(r[2]), r);
        });
        // Microclusters are rolled up on the server, through
        // ref_kecamatan, because the boundary layer and the desa rows call
        // the same microcluster by different names.
        var mt = d.mc_totals || {};
        Object.keys(mt).forEach(function (name) {
          var v = mt[name];
          byMcS[norm(name)] = { km2: v[0], sites: v[1], pop: v[2], desa: v[3] };
        });
        if (then) then();
      })
      .catch(function () { statsBusy = false; });
  }

  // The cloud's figures for whatever polygon is under the cursor.
  function statFor(feat, layerKey) {
    var p = feat.properties || {}, nm = featName(p);
    if (!STATS) return null;
    if (layerKey === "kelurahan") return byDesaS[key(nm, featKec(p))] || null;
    if (layerKey === "kecamatan") return byKecS[norm(nm)] || null;
    if (layerKey === "indosat_mc") return byMcS[norm(nm)] || null;
    if (layerKey === "kabkot") return byKabS[norm(nm)] || null;
    return null;
  }

  // The workbook's figures for the same polygon.
  function localFor(feat) {
    if (!LOCAL || !LOCAL.rows.length) return null;
    var rs = rowsFor(feat);
    var reps = {}, desa = {}, ono = 0;
    rs.forEach(function (r) {
      reps[r.dse] = 1;
      desa[key(r.desa, r.kec)] = 1;
      if (r.ono) ono++;
    });
    return { outlets: rs.length, dse: Object.keys(reps).length,
             desa: Object.keys(desa).length, ono: ono };
  }

  /* The tooltip itself. Cloud figures first, because they are true whether
     or not anybody has loaded a file; the workbook's figures after, marked
     as local so the two are never read as one number. */
  function tipHtml(feat, def) {
    var p = feat.properties || {};
    var st = statFor(feat, def.key), lo = localFor(feat);
    var out = ["<strong>" + esc(featName(p)) + "</strong>",
               "<span class='muted'>" + esc(def.label)
               + (featKec(p) ? " · " + esc(featKec(p)) : "") + "</span>"];
    if (st) {
      var bits = [plural(st.sites, "site"), st.km2.toFixed(2) + " km²"];
      if (def.key !== "kelurahan") bits.unshift(num(st.desa) + " desa");
      out.push(bits.join(" · "));
      if (st.pop) out.push(num(st.pop) + " people");
    } else {
      out.push("<span class='muted'>reading area and sites…</span>");
    }
    // The cloud figure above is the reference count for the desa. This is
    // how many of YOUR sites actually fall in it -- a different question,
    // and the reason both are shown rather than one replacing the other.
    if (SITES) {
      var ls = localSitesIn(feat, def.key + "|" + key(featName(p), featKec(p)));
      if (ls !== null) {
        out.push("<b>" + num(ls) + "</b> site" + (ls === 1 ? "" : "s")
          + " <span class='muted'>from your site file</span>");
      }
    }
    if (lo) {
      out.push("<b>" + num(lo.outlets) + "</b> outlet"
        + (lo.outlets === 1 ? "" : "s") + " · <b>" + num(lo.dse)
        + "</b> DSE · <b>" + num(lo.desa) + "</b> desa covered"
        + (lo.ono ? " · " + num(lo.ono) + " ONO" : "")
        + " <span class='muted'>(your file)</span>");
    } else if (LOCAL) {
      out.push("<span class='muted'>no outlet from your file here</span>");
    }
    out.push("<span class='muted'>click for the full profile</span>");
    return out.join("<br>");
  }

  function showCloud(k, on, then) {
    var c = CL[k];
    if (!on) { if (c.layer) map.removeLayer(c.layer); if (then) then(); return; }
    if (c.layer) { c.layer.addTo(map); reorder(); if (then) then(); return; }
    if (c.busy) return;
    c.busy = true; busy(true);
    fetch("/api/territory/" + k + ".geojson")
      .then(function (r) { return r.json(); })
      .then(function (fc) {
        c.busy = false; busy(false);
        if (fc.ok === false) throw new Error(fc.error || "layer unavailable");
        var g = L.geoJSON(fc, { renderer: RENDER,
          style: function (f) { return polyStyle(c.def, f); } });
        c.count = (fc.features || []).length;
        c.features = fc.features || [];
        // Delegated, not per feature: thousands of polygons with a handler
        // each is what makes a tab stop responding.
        g.on("click", function (e) {
          if (profileTarget !== "auto") return;   // the map handler owns it
          pickArea(e.layer.feature, c.def);
        });
        // Rebuilt on every hover, never bound once: the figures change when
        // a different workbook is loaded, and a tooltip bound at first hover
        // would go on reporting the previous file's numbers for ever.
        g.on("mouseover", function (e) {
          if (profileTarget !== "auto") return;
          var lyr = e.layer;
          function paint() {
            var html = tipHtml(lyr.feature, c.def);
            if (lyr.getTooltip()) lyr.setTooltipContent(html);
            else lyr.bindTooltip(html, { sticky: true, className: "terr-tip" });
            lyr.openTooltip();
          }
          paint();
          // First hover of the session pays for the table; every one after
          // it is a lookup, so the tooltip fills itself in a moment later
          // rather than making the reader hover twice.
          if (!STATS) ensureStats(function () {
            if (lyr.getTooltip() && lyr.isTooltipOpen && lyr.isTooltipOpen())
              lyr.setTooltipContent(tipHtml(lyr.feature, c.def));
          });
        });
        c.layer = g;
        if (btnOn("[data-layer='" + k + "']")) { g.addTo(map); reorder(); }
        if (!home) {
          try {
            var b = g.getBounds();
            if (b.isValid()) { home = b.pad(.03); map.fitBounds(home); }
          } catch (e2) { /* empty layer */ }
        }
        scopeLine(); dashCloud();
        if (then) then();
      })
      .catch(function (e) {
        c.busy = false; busy(false);
        setBtn("[data-layer='" + k + "']", false);
        console.warn("cloud layer " + k + ": " + e);
      });
  }

  /* ══════════════════════════════════════════════════════════════════════
     SITE LOCATIONS ARE LOCAL, NOT CLOUD

     There used to be a cloud layer here that fetched 15,919 mast positions
     from the server. It is gone, and its absence is the point: mast
     coordinates are not published to a shared instance. A site file is read
     from the user's own machine, in this browser, exactly like the outlet
     workbook, and loading one is optional.

     Nothing else lost a number. The masts per desa shown in every tooltip
     and in the desa profile come from the application's reference figures,
     not from counting dots, so they are unchanged whether or not anybody
     loads a site file. What went is the ability to see individual masts
     that belong to somebody else's copy of the data.

     The local site layer is `drawLocalSites()`, under Local layers.
     ══════════════════════════════════════════════════════════════════════ */

  /* ─────────────────────────────────────────────────────────────────────
     LOCAL DATA — read here, held here, dropped here
     ───────────────────────────────────────────────────────────────────── */
  var FIELDS = {
    dse:         ["dsecode", "dse", "dseid", "unikdse", "dsecodenew"],
    outlet_code: ["outletcode", "outletcod", "outletid", "idoutlet"],
    outlet_name: ["outletname", "outletnam", "namaoutlet"],
    lat:         ["lat", "latitude", "latnew", "y"],
    lon:         ["long", "lon", "longitude", "longnew", "x"],
    desa:        ["desaname", "desa", "kelurahanname", "keluarahanname", "kelurahan"],
    kecamatan:   ["kecamatanname", "kecamatan", "kec"],
    kabupaten:   ["kabupaten", "kabkot", "kota"],
    mc:          ["microclustername", "microclus", "mc", "microcluster"],
    category:    ["outletcategory", "outletcat", "category"],
    brand:       ["brandname", "brand"],
    partner:     ["partnerterritoryname", "partnerterritory"],
    supervisor:  ["supervisorcode", "supervisor", "spv"],
    schedule:    ["jadwalkunjungan", "pjp", "visitfreq"],
    pairing:     ["pairingoutletcode", "outletpairing"],
    remarks:     ["remarks", "remark", "flag", "status"],
    // The identity of the PERSON, as against the code a brand gives them.
    unikdse:     ["unikdse", "unikid"]
  };
  var NEED = ["dse", "lat", "lon"];

  /* ══════════════════════════════════════════════════════════════════════
     SITE LOCATIONS — A SECOND FILE, WITH ITS OWN VOCABULARY

     Sites used to be a cloud layer. They are local now, and that made this
     reader load bearing rather than a convenience: if it cannot find the
     columns, nobody sees a mast anywhere.

     It gets its own field map instead of borrowing the outlet one. "Site
     Name" would match the outlet reader's `name` list and "Site Type" its
     `category` list, so a site file read through the outlet vocabulary is
     not rejected -- it is quietly mis-read, which is worse.

     Only latitude and longitude are required. A site with no ID and no type
     is still a mast at a place, and refusing the file over a missing label
     would help nobody.
     ══════════════════════════════════════════════════════════════════════ */
  var SITE_FIELDS = {
    site_id:   ["siteid", "newsiteid", "sitecode", "siteno", "id"],
    site_name: ["sitename", "newsitename", "namesite", "name", "site"],
    lat:       ["lat", "latitude", "y", "lintang"],
    lon:       ["long", "lon", "lng", "longitude", "x", "bujur"],
    site_type: ["sitetype", "type", "sitecategory", "sitestatus",
                "technology", "tech", "band"]
  };
  var SITE_NEED = ["lat", "lon"];
  var SITE_LABEL = { site_id: "Site ID", site_name: "Site name",
                     lat: "Latitude", lon: "Longitude",
                     site_type: "Site type" };

  // The colours a site type is drawn in. Assigned in order of first
  // appearance so the file decides the legend, not a hard-coded list of
  // types somebody has to keep in step with the network.
  var SITE_HUES = ["#E0721F", "#0F9CB0", "#8659E8", "#4E9E4E", "#C6407A",
                   "#B08900", "#3C6DD0", "#8A8A8A"];
  var LABEL = { dse: "DSE", outlet_code: "Outlet ID", outlet_name: "Outlet name",
    lat: "Latitude", lon: "Longitude", desa: "Desa / Kelurahan",
    kecamatan: "Kecamatan", kabupaten: "Kabupaten", mc: "Microcluster",
    category: "Category", brand: "Brand", partner: "Partner territory",
    supervisor: "Supervisor", schedule: "Visit freq", pairing: "Pairing outlet",
    remarks: "Flag" };

  // The single object every local row lives in. Clearing the session is
  // literally setting this back to null.
  var LOCAL = null;

  /* Sites are their own file and their own variable, not a property hanging
     off the outlet one. Two reasons, and the second is the important one:
     a site list can be loaded WITHOUT an outlet workbook -- somebody
     checking mast coverage against the boundaries has no reason to hand
     over a demarcation file first -- and one home for a value means there
     is never a stale second copy to disagree with it. */
  var SITES = null;
  var siteHue = {};      // site type -> colour, in order of first appearance

  function nkey(s) {
    var t = String(s == null ? "" : s).toLowerCase();
    if (t.indexOf("(") >= 0) t = t.split("(")[0];
    return t.replace(/[^a-z0-9]/g, "");
  }
  function mapHeaderWith(header, fields) {
    var idx = {}, src = {};
    for (var i = 0; i < header.length; i++) {
      var n = nkey(header[i]);
      if (!n) continue;
      for (var f in fields) {
        if (idx[f] !== undefined) continue;
        if (fields[f].indexOf(n) >= 0) { idx[f] = i; src[f] = header[i]; break; }
      }
    }
    return { idx: idx, src: src };
  }
  function mapHeader(header) { return mapHeaderWith(header, FIELDS); }
  function numOf(v) {
    if (v == null || v === "") return null;
    if (typeof v === "number") return isFinite(v) ? v : null;
    var n = parseFloat(String(v).trim().replace(/\s/g, "").replace(",", "."));
    return isFinite(n) ? n : null;
  }
  function txt(v) {
    if (v == null) return null;
    var t = String(v).trim();
    return t === "" ? null : t;
  }

  function sniff(line) {
    var best = ",", n = -1;
    [",", ";", "\t", "|"].forEach(function (d) {
      var c = line.split(d).length;
      if (c > n) { n = c; best = d; }
    });
    return best;
  }
  function parseCSV(text) {
    var nl = text.indexOf("\n");
    var d = sniff(nl < 0 ? text : text.slice(0, nl));
    var rows = [], row = [], cur = "", q = false;
    for (var i = 0; i < text.length; i++) {
      var c = text[i];
      if (q) {
        if (c === '"') { if (text[i + 1] === '"') { cur += '"'; i++; } else q = false; }
        else cur += c;
      } else if (c === '"') { q = true; }
      else if (c === d) { row.push(cur); cur = ""; }
      else if (c === "\n") { row.push(cur); cur = ""; rows.push(row); row = []; }
      else if (c !== "\r") { cur += c; }
    }
    if (cur !== "" || row.length) { row.push(cur); rows.push(row); }
    return rows;
  }

  /* Every sheet is tried and the one carrying a DSE code, a longitude and a
     latitude wins. These workbooks also ship Summary and Maps tabs, and
     taking the first sheet would read a pivot table as an outlet list. */
  function pickTable(grids) {
    var near = null;
    for (var g = 0; g < grids.length; g++) {
      var rows = grids[g].rows;
      for (var probe = 0; probe < Math.min(6, rows.length); probe++) {
        var m = mapHeader(rows[probe] || []);
        var have = NEED.filter(function (f) { return m.idx[f] !== undefined; });
        if (!near || have.length > near.have)
          near = { sheet: grids[g].name, row: probe + 1, have: have.length };
        if (have.length === NEED.length)
          return { sheet: grids[g].name, idx: m.idx, src: m.src,
                   header: rows[probe], rows: rows.slice(probe + 1) };
      }
    }
    throw new Error("No sheet here has the three columns an outlet mapping "
      + "needs: a DSE code, a longitude and a latitude."
      + (near ? " The closest was <b>" + esc(near.sheet) + "</b> row "
                + near.row + ", which matched " + near.have + " of 3." : ""));
  }

  function readOutlets(grids, filename) {
    var t = pickTable(grids), idx = t.idx;
    function cell(row, f) {
      var i = idx[f];
      return (i === undefined || i >= row.length) ? null : row[i];
    }
    var out = [], stats = { read: 0, no_dse: 0, no_coords: 0 }, dse = {}, box = null;
    for (var r = 0; r < t.rows.length; r++) {
      var row = t.rows[r] || [];
      if (!row.length) continue;
      stats.read++;
      var code = txt(cell(row, "dse"));
      if (!code) { stats.no_dse++; continue; }
      var lat = numOf(cell(row, "lat")), lon = numOf(cell(row, "lon"));
      // Latitude here is about -6 and longitude about 107, so when the two
      // are swapped the numbers settle it and the headings get no vote.
      if (lat != null && lon != null && Math.abs(lat) > 90 && Math.abs(lon) <= 90) {
        var sw = lat; lat = lon; lon = sw;
      }
      if (lat == null || lon == null) { stats.no_coords++; continue; }
      var rem = txt(cell(row, "remarks"));
      out.push({
        dse: code,
        code: txt(cell(row, "outlet_code")) || "",
        name: txt(cell(row, "outlet_name")) || "",
        desa: txt(cell(row, "desa")) || "",
        kec: txt(cell(row, "kecamatan")) || "",
        kab: txt(cell(row, "kabupaten")) || "",
        mc: txt(cell(row, "mc")) || "",
        cat: txt(cell(row, "category")) || "",
        spv: txt(cell(row, "supervisor")) || "",
        freq: txt(cell(row, "schedule")) || "",
        pair: txt(cell(row, "pairing")) || "",
        brand: txt(cell(row, "brand")) || "",
        unik: txt(cell(row, "unikdse")) || "",
        rem: rem || "",
        ono: !!(rem && /ONO/i.test(rem)),
        lat: lat, lon: lon
      });
      dse[code] = (dse[code] || 0) + 1;
      box = box ? [Math.min(box[0], lon), Math.min(box[1], lat),
                   Math.max(box[2], lon), Math.max(box[3], lat)]
                : [lon, lat, lon, lat];
    }
    return { rows: out, stats: stats, dse: dse, box: box,
             sheet: t.sheet, src: t.src, idx: t.idx, filename: filename };
  }

  /* A site list needs only a name and a pair of coordinates; a DSE code is
     not expected of it, so it is matched on its own terms rather than being
     rejected for failing the outlet test. */
  /* Read a site file: SITE ID, Site Name, Long, Lat, Site Type.

     The header is hunted for in the first six rows, because an export that
     opens with a title row and a blank one is the normal case rather than
     the awkward one.

     WHEN NO HEADER MATCHES, IT STILL TRIES
     A file with bare coordinate columns and no recognisable labels used to
     work here, and breaking it in order to be stricter would be a poor
     trade. So a loose second pass takes any two numeric columns that read
     as a coordinate and the first text column as a name -- and says so, so
     nobody mistakes the guess for a mapping. */
  function readSites(grids, filename) {
    for (var g = 0; g < grids.length; g++) {
      var rows = grids[g].rows;
      for (var p = 0; p < Math.min(6, rows.length); p++) {
        var m = mapHeaderWith(rows[p] || [], SITE_FIELDS);
        var lack = SITE_NEED.filter(function (f) {
          return m.idx[f] === undefined; });
        if (lack.length) continue;
        var out = [], types = {}, bad = 0;
        for (var r = p + 1; r < rows.length; r++) {
          var row = rows[r] || [];
          var lat = numOf(row[m.idx.lat]), lon = numOf(row[m.idx.lon]);
          if (lat == null || lon == null) { bad++; continue; }
          // Latitude here is about -6 and longitude about 107, so when the
          // two are swapped the numbers settle it and the headings get no
          // vote. Same rule the outlet reader uses.
          if (Math.abs(lat) > 90 && Math.abs(lon) <= 90) {
            var sw = lat; lat = lon; lon = sw;
          }
          var t = m.idx.site_type !== undefined
                ? (txt(row[m.idx.site_type]) || "") : "";
          if (t) types[t] = (types[t] || 0) + 1;
          out.push({
            id:   m.idx.site_id   !== undefined ? (txt(row[m.idx.site_id]) || "") : "",
            name: m.idx.site_name !== undefined ? (txt(row[m.idx.site_name]) || "") : "",
            type: t, lat: lat, lon: lon });
        }
        if (out.length) {
          return { rows: out, filename: filename, sheet: grids[g].name,
                   src: m.src, idx: m.idx, types: types, skipped: bad,
                   guessed: false };
        }
      }
    }
    // ── the loose pass ──────────────────────────────────────────────────
    for (var g2 = 0; g2 < grids.length; g2++) {
      var rs = grids[g2].rows;
      for (var p2 = 0; p2 < Math.min(6, rs.length); p2++) {
        var mm = mapHeader(rs[p2] || []);
        if (mm.idx.lat === undefined || mm.idx.lon === undefined) continue;
        var o2 = [];
        for (var r2 = p2 + 1; r2 < rs.length; r2++) {
          var w = rs[r2] || [];
          var la = numOf(w[mm.idx.lat]), lo = numOf(w[mm.idx.lon]);
          if (la == null || lo == null) continue;
          if (Math.abs(la) > 90 && Math.abs(lo) <= 90) {
            var s2 = la; la = lo; lo = s2;
          }
          var nm = "";
          for (var c = 0; c < w.length; c++) {
            if (c === mm.idx.lat || c === mm.idx.lon) continue;
            var v = txt(w[c]);
            if (v && !/^-?\d+([.,]\d+)?$/.test(v)) { nm = v; break; }
          }
          o2.push({ id: "", name: nm, type: "", lat: la, lon: lo });
        }
        if (o2.length) {
          return { rows: o2, filename: filename, sheet: grids[g2].name,
                   src: { lat: "(guessed)", lon: "(guessed)" },
                   idx: mm.idx, types: {}, skipped: 0, guessed: true };
        }
      }
    }
    return null;
  }

  function gridsOf(file, buf) {
    var lower = (file.name || "").toLowerCase();
    if (/\.csv$|\.txt$/.test(lower)) {
      var text = new TextDecoder("utf-8").decode(new Uint8Array(buf));
      return [{ name: file.name, rows: parseCSV(text) }];
    }
    var wb = XLSX.read(new Uint8Array(buf), { type: "array" });
    return wb.SheetNames.map(function (n) {
      return { name: n, rows: XLSX.utils.sheet_to_json(wb.Sheets[n],
        { header: 1, blankrows: false, raw: true, defval: null }) };
    });
  }

  function readFiles(files) {
    lerr("");
    var list = Array.prototype.slice.call(files || []);
    if (!list.length) return;
    var got = { outlets: null, sites: null }, errs = [], left = list.length;
    list.forEach(function (f) {
      var fr = new FileReader();
      fr.onerror = function () { errs.push(f.name + ": could not be read"); done(); };
      fr.onload = function () {
        try {
          var grids = gridsOf(f, fr.result);
          // The outlet mapping is tried first because it is the stricter
          // test: anything that passes it is an outlet file, and only what
          // fails it is offered to the looser site reader.
          // Outlets first because it is the stricter test, then sites.
          // This used to try sites ONLY when the outlet reader threw, so a
          // site file that merely produced no outlet rows was reported as
          // unreadable and never offered to the reader that wanted it.
          var o = null, why = "";
          try {
            o = readOutlets(grids, f.name);
          } catch (e1) { why = e1.message; }
          if (o && o.rows.length) { got.outlets = o; done(); return; }
          var st = readSites(grids, f.name);
          if (st && st.rows.length) { got.sites = st; done(); return; }
          errs.push(f.name + ": " + (why || "no usable rows"));
        } catch (e2) {
          errs.push(f.name + ": " + e2.message);
        }
        done();
      };
      fr.readAsArrayBuffer(f);
    });

    function done() {
      if (--left > 0) return;
      if (!got.outlets && !got.sites && !LOCAL) {
        lerr(errs.join("<br>") || "Nothing readable in those files.");
        return;
      }
      adopt(got, errs);
    }
  }

  /* ─────────────────────────────────────────────────────────────────────
     THE JOIN — local rows against cloud areas, and the derived counts
     ───────────────────────────────────────────────────────────────────── */
  var byArea = {};      // "DESA|KEC" -> [row, ...]
  var areaCount = {};   // "DESA|KEC" -> outlets
  var heatMax = 1, heatOn = false;

  /* ══════════════════════════════════════════════════════════════════════
     ONE REP, OR ONE BRAND CODE?

     A demarkasi export lists 3ID and IM3 separately, and gives the same
     person a different DSE CODE under each: CVSCJU013 on one side and
     1-163544572541 on the other. Counting DSE CODE therefore counts a rep
     who carries both brands twice. In the August Jaya file that is exactly
     154 people, which is why 1,501 codes are only 1,347 reps.

     UNIKDSE is the person and is used wherever the question is "how many
     reps". Where the column is missing the code is all there is, and the
     count silently means brand codes again -- so the dashboard says which
     of the two it is counting rather than leaving a reader to wonder why
     two figures for the same file disagree.

     The DSE filter and the roster still key on DSE CODE, because that is
     what identifies a set of rows in the file. Identity and selection are
     different jobs.
     ══════════════════════════════════════════════════════════════════════ */
  function repKey(r) { return r.unik || r.dse; }
  function hasUnik() {
    return !!(LOCAL && LOCAL.src && LOCAL.src.unikdse);
  }
  function repCount(rows) {
    var by = {};
    rows.forEach(function (r) { by[repKey(r)] = 1; });
    return Object.keys(by).length;
  }

  function reindex() {
    byArea = {}; areaCount = {}; heatMax = 1;
    if (!LOCAL) return;
    LOCAL.rows.forEach(function (r) {
      var k = key(r.desa, r.kec);
      (byArea[k] = byArea[k] || []).push(r);
    });
    for (var k in byArea) {
      areaCount[k] = byArea[k].length;
      if (areaCount[k] > heatMax) heatMax = areaCount[k];
    }
  }

  function rowsFor(feat) {
    var p = feat.properties || {};
    var nm = featName(p);
    var exact = byArea[key(nm, featKec(p))];
    if (exact) return exact;
    // A kecamatan or microcluster polygon has no desa of its own, so it is
    // answered by everything filed under its name at the level below.
    var want = norm(nm), out = [];
    if (!want || !LOCAL) return out;
    LOCAL.rows.forEach(function (r) {
      if (norm(r.kec) === want || norm(r.mc) === want || norm(r.kab) === want)
        out.push(r);
    });
    return out;
  }


  /* ─────────────────────────────────────────────────────────────────────
     THE INSPECTOR
     ───────────────────────────────────────────────────────────────────── */
  function tiles(list) {
    return list.map(function (t) {
      return '<div class="cell' + (t.dash ? " dash" : "") + '"><span class="k">'
        + esc(t.k) + "</span><b>" + t.v + "</b></div>";
    }).join("");
  }
  function rows(list, click) {
    return '<div class="mw-list">' + list.map(function (r) {
      return '<div class="mw-row' + (click ? " click" : "")
        + '"' + (r.d ? ' data-dse="' + esc(r.d) + '"' : "") + "><span>"
        + esc(r.a) + "</span><span>" + esc(r.b) + "</span></div>";
    }).join("") + "</div>";
  }

  function inspect(kind, title, sub, statHtml, listHtml) {
    $("mwIKind").textContent = kind;
    $("mwITitle").textContent = title;
    $("mwISub").textContent = sub;
    $("mwIStats").innerHTML = statHtml || "";
    $("mwIList").innerHTML = listHtml || "";
    Array.prototype.forEach.call(
      $("mwIList").querySelectorAll(".mw-row.click"), function (el) {
        el.addEventListener("click", function () {
          var d = el.getAttribute("data-dse");
          if (d) { $("mwDse").value = d; onDse(); }
        });
      });
  }

  function idle() {
    var cl = $("mwCloud");
    if (cl) cl.innerHTML = "";
    inspect("Inspector",
      LOCAL ? "Nothing selected" : "Local data not loaded",
      LOCAL ? "Click a polygon or an outlet on the map."
            : "Go to Local data and load the demarkasi file.",
      "", "");
  }

  function outline(feat) {
    if (pickLayer) { map.removeLayer(pickLayer); pickLayer = null; }
    if (!feat || !feat.geometry) return null;
    pickLayer = L.geoJSON(feat, { renderer: RENDER, interactive: false,
      style: { color: "#ec3013", weight: 2.4, opacity: 1,
               fillColor: "#ec3013", fillOpacity: .22 } }).addTo(map);
    try {
      var b = pickLayer.getBounds();
      return b.isValid() ? b : null;
    } catch (e) { return null; }
  }

  function pickArea(feat, def) {
    var p = feat.properties || {};
    var nm = featName(p) || def.label;
    var b = outline(feat);
    if (b) map.fitBounds(b.pad(.15), { animate: false });

    var rs = rowsFor(feat);
    if (!LOCAL) {
      inspect("Area", nm,
        [featKec(p), featMc(p)].filter(Boolean).join(" · ") || def.label,
        tiles([{ k: "Layer", v: esc(def.label) },
               { k: "Outlets", v: "&mdash;", dash: true },
               { k: "DSE covering", v: "&mdash;", dash: true },
               { k: "Population",
                 v: featPop(p) != null ? num(featPop(p)) : "&mdash;" }]),
        '<div class="mw-note" style="margin-top:10px">Outlet figures come '
        + "from your local file. Load it under Local data.</div>");
      cloudProfile(def, p, []);
      if (def.key === "kelurahan") drawerDesa(featName(p), featKec(p));
      else drawerCoarse(feat, def);
      return;
    }
    var dse = {}, ono = 0;
    rs.forEach(function (r) {
      dse[r.dse] = (dse[r.dse] || 0) + 1;
      if (r.ono) ono++;
    });
    var codes = Object.keys(dse).sort(function (a, b2) { return dse[b2] - dse[a]; });
    inspect("Area", nm,
      [featKec(p), featMc(p)].filter(Boolean).join(" · ") || def.label,
      tiles([{ k: "Outlets", v: num(rs.length), dash: true },
             { k: "DSE covering", v: num(codes.length), dash: true },
             { k: "ONO", v: num(ono), dash: true },
             { k: "Pairing", v: num(rs.length - ono), dash: true }]),
      codes.length
        ? '<span class="k">Outlets per DSE</span>'
          + rows(codes.map(function (c) {
              return { a: c, b: num(dse[c]), d: c }; }), true)
        : '<div class="mw-note" style="margin-top:10px">No outlet in your '
          + "local file is filed under this area.</div>");
    cloudProfile(def, p, rs);
    if (def.key === "kelurahan") drawerDesa(featName(p), featKec(p));
    else drawerCoarse(feat, def);
  }

  function pickOutlet(r) {
    if (pickLayer) { map.removeLayer(pickLayer); pickLayer = null; }
    pickLayer = L.circleMarker([r.lat, r.lon], { renderer: RENDER,
      radius: 5.5, color: "#ec3013", weight: 2, fillColor: "#ec3013",
      fillOpacity: .9, interactive: false }).addTo(map);
    inspect("Outlet",
      (r.code ? r.code + " · " : "") + (r.name || "outlet"),
      [r.desa, r.kec].filter(Boolean).join(", ")
        + (r.cat ? " · " + r.cat : ""),
      tiles([{ k: "DSE", v: esc(r.dse), dash: true },
             { k: "Microcluster", v: esc(r.mc || "—"), dash: true },
             { k: "Visit freq", v: esc(r.freq || "—"), dash: true },
             { k: "Flag", v: esc(r.rem || "—"), dash: true }]),
      '<span class="k">Record</span>'
      + rows([{ a: "Supervisor", b: r.spv || "—" },
              { a: "Pairing outlet", b: r.pair || "—" },
              { a: "Longitude", b: r.lon.toFixed(6) },
              { a: "Latitude", b: r.lat.toFixed(6) },
              { a: "Source", b: "local file" }], false));
  }

  /* ─────────────────────────────────────────────────────────────────────
     THE LOCAL LAYER ON THE MAP
     ───────────────────────────────────────────────────────────────────── */
  var outLayer = null, lsiteLayer = null, spotLayer = null, dseFilter = "";

  /* THE OUTLETS THAT MADE THE BORDER

     Selecting a rep's patch used to change only the outline. The dots stayed
     as they were -- every outlet in the slice, all the same size -- so the
     one question a border invites, "which outlets is this drawn from", was
     the one thing the map would not answer.

     Picking a border now spotlights its own outlets: full size, in the
     border's own colour, cased in white so they read over the tint. Everyone
     else's fade rather than vanish, because the point of looking at a patch
     is usually how it sits against its neighbours -- except in Focus, where
     fading everything else is the whole idea.

     The spotlight is its own layer, drawn whether or not the Outlets toggle
     is on. That toggle governs the background dots; the outlets belonging to
     the thing you just selected are part of the selection, not a layer you
     should have had to remember to switch on first. */
  function spotDse() { return dseFilter || modelPick || ""; }

  function drawSpot() {
    if (spotLayer) { map.removeLayer(spotLayer); spotLayer = null; }
    var code = modelPick;
    if (!LOCAL || !code) return;
    var col = modelBy[code] ? borderColour(modelBy[code]) : "#ec3013";
    var rs = LOCAL.rows.filter(function (r) {
      return r.dse === code && inSlice(r); });
    spotLayer = L.layerGroup([]);
    rs.forEach(function (r) {
      var m = L.circleMarker([r.lat, r.lon], { renderer: RENDER,
        radius: 4.2, color: "#ffffff", weight: 1.6,
        fillColor: r.ono ? "#ec3013" : col, fillOpacity: 1 });
      m.on("click", function (e) { L.DomEvent.stop(e); pickOutlet(r); });
      m.on("mouseover", function () {
        if (!m.getTooltip()) {
          m.bindTooltip("<strong>" + esc(r.name || r.code || "outlet")
            + "</strong><br>" + esc(r.dse)
            + (r.desa ? "<br>" + esc(r.desa) : "")
            + (r.ono ? "<br>ONO" : "")
            + "<br><span class='muted'>local &middot; this session</span>",
            { className: "terr-tip" });
        }
        m.openTooltip();
      });
      spotLayer.addLayer(m);
    });
    spotLayer.addTo(map);
    reorder();
  }

  function drawOutlets() {
    if (outLayer) { map.removeLayer(outLayer); outLayer = null; }
    if (!LOCAL || !btnOn("[data-local='outlets']")) return;
    var spot = spotDse();
    var rs = LOCAL.rows.filter(function (r) {
      if (!inSlice(r)) return false;
      if (dseFilter && r.dse !== dseFilter) return false;
      // In Focus, everyone else's dots go entirely: leaving them in is the
      // thing Focus exists to stop.
      if (spot && r.dse !== spot && bView === "focus" && MODEL) return false;
      return true;
    });
    outLayer = L.layerGroup([]);
    rs.forEach(function (r) {
      var other = spot && r.dse !== spot;
      var m = L.circleMarker([r.lat, r.lon], { renderer: RENDER,
        radius: other ? 1.9 : 2.6,
        color: other ? "#8d8781" : "#201e1d",
        weight: 1, opacity: other ? .5 : 1,
        fillColor: other ? "#a9a29c" : (r.ono ? "#ec3013" : "#201e1d"),
        fillOpacity: other ? .45 : .95 });
      m.on("click", function (e) { L.DomEvent.stop(e); pickOutlet(r); });
      m.on("mouseover", function () {
        if (!m.getTooltip()) {
          m.bindTooltip("<strong>" + esc(r.name || r.code || "outlet")
            + "</strong><br>" + esc(r.dse)
            + (r.desa ? "<br>" + esc(r.desa) : "")
            + "<br><span class='muted'>local &middot; this session</span>",
            { className: "terr-tip" });
        }
        m.openTooltip();
      });
      outLayer.addLayer(m);
    });
    outLayer.addTo(map);
    reorder();
  }

  /* One hue per site type, assigned as types are met. The file decides the
     legend: hard-coding "2G / 4G / 5G" would be a list somebody has to keep
     in step with the network, and it would silently draw an unlisted type
     in whatever colour fell through. */
  function hueFor(t) {
    if (!t) return "#6f6a66";
    if (!siteHue[t]) {
      siteHue[t] = SITE_HUES[Object.keys(siteHue).length % SITE_HUES.length];
    }
    return siteHue[t];
  }

  function drawLocalSites() {
    if (lsiteLayer) { map.removeLayer(lsiteLayer); lsiteLayer = null; }
    if (!SITES || !btnOn("[data-local='lsites']")) { renderSiteKey(); return; }
    lsiteLayer = L.layerGroup([]);
    SITES.rows.forEach(function (s) {
      var m = L.circleMarker([s.lat, s.lon], { renderer: RENDER,
        radius: 3.6, color: "#201e1d", weight: 1.2,
        fillColor: hueFor(s.type), fillOpacity: 1 });
      m.on("mouseover", function () {
        if (!m.getTooltip()) {
          var head = s.name || s.id || "site";
          var sub = [];
          if (s.id && s.name) sub.push(esc(s.id));
          if (s.type) sub.push(esc(s.type));
          m.bindTooltip("<strong>" + esc(head) + "</strong>"
            + (sub.length ? "<br>" + sub.join(" &middot; ") : "")
            + "<br><span class='muted'>local &middot; this session</span>",
            { className: "terr-tip" });
        }
        m.openTooltip();
      });
      lsiteLayer.addLayer(m);
    });
    lsiteLayer.addTo(map);
    reorder();
    renderSiteKey();
  }

  /* The legend under the button. Without it a map of coloured dots is a map
     of coloured dots. */
  function renderSiteKey() {
    var el = $("mwSiteKey");
    if (!el) return;
    if (!SITES || !btnOn("[data-local='lsites']")
        || !Object.keys(SITES.types || {}).length) {
      el.innerHTML = ""; el.hidden = true; return;
    }
    var ts = Object.keys(SITES.types).sort(function (a, b) {
      return SITES.types[b] - SITES.types[a]; });
    el.innerHTML = ts.map(function (t) {
      return '<span class="mw-skey"><i style="background:' + hueFor(t)
        + '"></i>' + esc(t) + " <b>" + num(SITES.types[t]) + "</b></span>";
    }).join("");
    el.hidden = false;
  }

  /* HOW MANY OF THE LOADED SITES FALL IN THIS DESA
     Counted on demand for the one polygon under the cursor and cached, not
     precomputed for all 7,761. A sweep of the mouse asks this a few dozen
     times; precomputing asks it 7,761 times for an answer nobody looked at.
     The bbox rejects almost everything before the ray cast runs. */
  var localSiteCount = {};
  function localSitesIn(feat, cacheKey) {
    if (!SITES) return null;
    if (localSiteCount[cacheKey] !== undefined) return localSiteCount[cacheKey];
    var b = bbOf(feat), n = 0;
    if (b) {
      for (var i = 0; i < SITES.rows.length; i++) {
        var r = SITES.rows[i];
        if (r.lon < b[0] || r.lon > b[2] || r.lat < b[1] || r.lat > b[3]) continue;
        if (featHas(feat, r.lon, r.lat)) n++;
      }
    }
    localSiteCount[cacheKey] = n;
    return n;
  }

  /* ─────────────────────────────────────────────────────────────────────
     BUTTONS THAT BEHAVE LIKE CHECKBOXES
     ───────────────────────────────────────────────────────────────────── */
  function btn(sel) { return document.querySelector(".mw " + sel); }
  function btnOn(sel) { var b = btn(sel); return !!b && b.classList.contains("on"); }
  function setBtn(sel, on) {
    var b = btn(sel);
    if (b) b.classList.toggle("on", !!on);
  }

  Array.prototype.forEach.call(
    document.querySelectorAll(".mw [data-layer]"), function (b) {
      b.addEventListener("click", function () {
        var on = !b.classList.contains("on");
        b.classList.toggle("on", on);
        showCloud(b.dataset.layer, on);
      });
    });

  Array.prototype.forEach.call(
    document.querySelectorAll(".mw [data-local]"), function (b) {
      b.addEventListener("click", function () {
        var on = !b.classList.contains("on");
        b.classList.toggle("on", on);
        if (b.dataset.local === "outlets") drawOutlets();
        else if (b.dataset.local === "lsites") drawLocalSites();
        else { heatOn = on; restyle("kelurahan"); }
      });
    });

  // Each cloud-layer button carries the line it draws: same hue, same
  // weight, same dash. The rail is then a legend as well as a switch.
  CLOUD.forEach(function (c) {
    var b = btn("[data-layer='" + c.key + "']");
    if (!b || b.querySelector(".mw-swatch")) return;
    var i = document.createElement("i");
    i.className = "mw-swatch";
    i.style.borderTopColor = c.colour;
    i.style.borderTopWidth = Math.max(2, c.weight) + "px";
    i.style.borderTopStyle = c.dash ? "dashed" : "solid";
    b.insertBefore(i, b.firstChild);
  });

  function onDse() {
    dseFilter = $("mwDse").value || "";
    $("tbDse").value = dseFilter;
    drawOutlets();
    if (!dseFilter || !LOCAL) { renderTable(); return; }
    var rs = LOCAL.rows.filter(function (r) { return r.dse === dseFilter; });
    if (rs.length) {
      var b = L.latLngBounds(rs.map(function (r) { return [r.lat, r.lon]; }));
      if (b.isValid()) map.fitBounds(b.pad(.12), { animate: false });
      var desa = {}, ono = 0;
      rs.forEach(function (r) {
        var d = r.desa || "—";
        desa[d] = (desa[d] || 0) + 1;
        if (r.ono) ono++;
      });
      var names = Object.keys(desa).sort(function (a, b2) { return desa[b2] - desa[a]; });
      if (pickLayer) { map.removeLayer(pickLayer); pickLayer = null; }
      inspect("DSE", dseFilter,
        (rs[0].mc || "") + (rs[0].kec ? " · " + rs[0].kec : ""),
        tiles([{ k: "Outlets", v: num(rs.length), dash: true },
               { k: "Desa", v: num(names.length), dash: true },
               { k: "ONO", v: num(ono), dash: true },
               { k: "Pairing", v: num(rs.length - ono), dash: true }]),
        '<span class="k">Outlets per desa</span>'
        + rows(names.map(function (n) {
            return { a: n, b: num(desa[n]) }; }), false));
    }
    renderTable();
  }
  $("mwDse").addEventListener("change", onDse);

  $("mwReset").addEventListener("click", function () {
    if (pickLayer) { map.removeLayer(pickLayer); pickLayer = null; }
    $("mwSearch").value = "";
    $("mwDse").value = ""; dseFilter = ""; $("tbDse").value = "";
    drawOutlets(); renderTable(); idle();
    if (home) map.fitBounds(home, { animate: false });
    else map.setView([T.lat, T.lon], 11);
  });
  $("mwOpenTable").addEventListener("click", function () { go("table"); });

  /* Search runs against whichever cloud layers are loaded, finest grain
     first, so typing a desa name finds the desa and not the kecamatan it
     shares a name with. */
  function search(term) {
    var want = norm(term);
    if (!want) return;
    var order = ["kelurahan", "kecamatan", "indosat_mc", "kabkot"];
    for (var i = 0; i < order.length; i++) {
      var c = CL[order[i]];
      if (!c.features) continue;
      for (var j = 0; j < c.features.length; j++) {
        var f = c.features[j];
        if (norm(featName(f.properties || {})).indexOf(want) === 0) {
          pickArea(f, c.def);
          return;
        }
      }
    }
    inspect("Inspector", "Not found",
      "No area whose name starts with " + term + " in the layers loaded.",
      "", "");
  }
  $("mwSearch").addEventListener("keydown", function (e) {
    if (e.key === "Enter") search($("mwSearch").value.trim());
  });

  /* ─────────────────────────────────────────────────────────────────────
     DASHBOARD
     ───────────────────────────────────────────────────────────────────── */
  function dashCloud() {
    $("dbKec").textContent = CL.kecamatan.count ? num(CL.kecamatan.count) : "—";
    $("dbDesa").textContent = CL.kelurahan.count ? num(CL.kelurahan.count) : "—";
    $("dbMc").textContent = CL.indosat_mc.count ? num(CL.indosat_mc.count) : "—";
  }
  function scopeLine() {
    var bits = [HOME_NAME];
    if (CL.kecamatan.count) bits.push(num(CL.kecamatan.count) + " kecamatan");
    if (CL.kelurahan.count) bits.push(num(CL.kelurahan.count) + " desa");
    $("mwScope").textContent = bits.filter(Boolean).join(" · ");
  }


  /* ══════════════════════════════════════════════════════════════════════
     OUTLET IDENTITY — IS ANY OUTLET ID CARRIED BY TWO DIFFERENT DSE?

     The question sounds like one check and is really four, and answering it
     as one would send somebody chasing thirty-two ghosts.

     An outlet ID appearing under two DSE codes can mean:

     1. ONE OUTLET, TWO REPS. Same brand, same ID, two rounds. This is the
        real conflict: two people are driving to the same shop, and one of
        them is wasting the trip. Nothing else on this list is worth a
        demarcation change.

     2. BOTH BRANDS CALLING ON ONE SHOP. 3ID and IM3 keep separate rounds,
        so a shop that sells both is legitimately on two. Not an error --
        it is what a hybrid market looks like -- but worth counting, because
        it is why "outlets" and "outlet visits" are different numbers.

     3. THE SAME NUMBER GIVEN TO TWO DIFFERENT SHOPS. The two brands number
        their outlets separately, so a collision is arithmetic, not
        demarcation. In the August file all 32 shared IDs are this: the two
        rows sit a median of 80 km apart, under different names, in
        different branches. The finding is not "32 outlets double covered".
        It is OUTLET ID ALONE IS NOT A KEY -- brand plus ID is, and so is
        UNIKID.

     4. THE SAME ROW TWICE. Same brand, same ID, same DSE. A file artefact,
        not a field problem, but it inflates every count on the page until
        somebody notices.

     Distance is what separates 2 from 3, and 500 m is the cut: two records
     of one shop rarely disagree by more than a street, and two different
     shops that happen to share a number are almost never that close.

     Counting is all this does. Nothing is merged, renumbered or reassigned.
     ══════════════════════════════════════════════════════════════════════ */
  var DUPE_SAME_M = 0.5;          // km — closer than this is one shop
  var DUPE = null;

  function dupeRows(list) {
    var out = [];
    list.forEach(function (g) { out = out.concat(g.rows); });
    return out;
  }

  function dupeScan() {
    if (DUPE) return DUPE;
    var empty = { multi: [], conflict: [], samePlace: [], diffPlace: [],
                  repeat: [], ids: 0, noId: 0 };
    if (!LOCAL) return empty;

    var byCode = {}, noId = 0;
    LOCAL.rows.forEach(function (r) {
      var c = String(r.code == null ? "" : r.code).trim();
      if (!c) { noId++; return; }
      (byCode[c] = byCode[c] || []).push(r);
    });

    var multi = [], conflict = [], samePlace = [], diffPlace = [], repeat = [];

    Object.keys(byCode).forEach(function (c) {
      var rs = byCode[c];
      if (rs.length < 2) return;

      // The same row twice is its own finding and is counted per
      // brand+ID+DSE, so a genuine two-rep case is not also reported as a
      // repeat.
      var seen = {};
      rs.forEach(function (r) {
        var k = (r.brand || "-") + "|" + (r.dse || "-");
        seen[k] = (seen[k] || 0) + 1;
      });
      var dupRows = rs.filter(function (r) {
        return seen[(r.brand || "-") + "|" + (r.dse || "-")] > 1;
      });
      if (dupRows.length) repeat.push({ code: c, rows: dupRows });

      var dse = {}, brand = {};
      rs.forEach(function (r) { dse[r.dse] = 1; brand[r.brand || "—"] = 1; });
      var dseList = Object.keys(dse), brandList = Object.keys(brand);
      if (dseList.length < 2) return;                 // one rep — not this check

      var far = 0;
      for (var i = 0; i < rs.length; i++) {
        for (var j = i + 1; j < rs.length; j++) {
          var d = rkm(rs[i].lat, rs[i].lon, rs[j].lat, rs[j].lon);
          if (d > far) far = d;
        }
      }
      var rec = { code: c, rows: rs, dse: dseList, brands: brandList, km: far };
      multi.push(rec);
      if (brandList.length < 2) conflict.push(rec);
      else if (far <= DUPE_SAME_M) samePlace.push(rec);
      else diffPlace.push(rec);
    });

    function worst(a, b) { return b.km - a.km; }
    conflict.sort(worst); diffPlace.sort(worst); samePlace.sort(worst);
    DUPE = { multi: multi, conflict: conflict, samePlace: samePlace,
             diffPlace: diffPlace, repeat: repeat,
             ids: Object.keys(byCode).length, noId: noId };
    return DUPE;
  }

  /* The dashboard panel. Severity order, not size order: the row that can
     cost somebody a wasted trip is first even when it is the smallest
     number on the list. */
  /* ── THE BOUNDARY SUMMARY ON THE DASHBOARD ──────────────────────────
     Counts first, then the worst dozen, then the whole thing as a CSV.
     A supervisor reading this wants to know how big the problem is before
     they want to know which outlets it is. */
  function boundsCsv() {
    if (!BOUNDS) return;
    function q(v) {
      var t = String(v == null ? "" : v);
      return /[",\n]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t;
    }
    var head = ["Outlet Code", "Outlet Name", "Brand",
                "DSE Code", "UNIKDSE", "Supervisor",
                "Desa / Kelurahan", "Kecamatan", "City / Kabupaten",
                "Microcluster", "Region", "Area", "Sales Area / Branch",
                "Territory",
                "Stratum", "People per km2", "Allowed km",
                "Km to nearest own outlet", "Flag",
                "Recommended DSE", "Km to recommended",
                "Outlets after move", "Stands inside border of",
                "Latitude", "Longitude"];
    var body = BOUNDS.rows.map(function (r) {
      return [r.code, r.name, r.brand,
              r.dse, r.unik, r.spv,
              r.desa, r.kec, r.kab,
              r.mc, r.region, r.area, r.branch, r.terr,
              r.stratum, r.known ? Math.round(r.density) : "",
              r.lim, r.kmOwn.toFixed(3), r.flag,
              r.to, r.kmNew == null ? "" : r.kmNew.toFixed(3),
              r.toLoad == null ? "" : r.toLoad, r.inside,
              r.lat, r.lon].map(q).join(",");
    });
    var url = URL.createObjectURL(new Blob(
      ["\ufeff" + head.join(",") + "\n" + body.join("\n")],
      { type: "text/csv;charset=utf-8" }));
    var a = document.createElement("a");
    a.href = url;
    a.download = "out-of-boundaries-"
      + new Date().toISOString().slice(0, 10) + ".csv";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  function renderBounds() {
    var el = $("dbBounds");
    if (!el || !BOUNDS) return;
    var t = BOUNDS.totals, all = BOUNDS.rows;
    var rows = all.filter(function (r) { return !r.bad; });
    var bad = all.filter(function (r) { return r.bad; });
    var pct = t.outlets ? (rows.length * 100 / t.outlets) : 0;

    // Which sales areas carry the problem. A total tells you the size; this
    // tells you where to send somebody.
    var byBranch = {};
    rows.forEach(function (r) {
      var k = r.branch || "—";
      byBranch[k] = (byBranch[k] || 0) + 1;
    });
    var top = Object.keys(byBranch).sort(function (a, b) {
      return byBranch[b] - byBranch[a]; }).slice(0, 6);
    var maxB = top.length ? byBranch[top[0]] : 1;

    var head = '<div class="mw-5up" style="margin-top:12px">'
      + '<div class="cell dash"><span class="k">Out of boundaries</span><b class="mw-flag">'
      + num(rows.length) + "</b><span class='mw-note'>" + pct.toFixed(2)
      + "% of " + num(t.outlets) + " outlets</span></div>"
      + '<div class="cell dash"><span class="k">Urban · over 1.5 km</span><b>'
      + num(t.flagUrban) + "</b><span class='mw-note'>of " + num(t.urban)
      + " in urban desa</span></div>"
      + '<div class="cell dash"><span class="k">Rural · over 4 km</span><b>'
      + num(t.flagRural) + "</b><span class='mw-note'>of " + num(t.rural)
      + " in rural desa</span></div>"
      + '<div class="cell dash"><span class="k">DSE affected</span><b>'
      + num(t.repsFlagged) + "</b><span class='mw-note'>of " + num(t.reps)
      + " · " + num(t.repsOver) + " carry over " + num(REB_N) + "</span></div>"
      + '<div class="cell dash"><span class="k">Detour removable</span><b>'
      + num(Math.round(t.kmSaved)) + " km</b><span class='mw-note'>"
      + num(t.noReceiver) + " have no nearer rep</span></div>"
      + "</div>"
      + (bad.length
          ? '<div class="mw-note" style="margin-top:10px;color:var(--red)">'
            + "<b>" + num(bad.length) + "</b> more sit over " + REB_BAD_KM
            + " km from their own round — further than this circle is wide. "
            + "Those are coordinates to fix, not outlets to move, so they are "
            + "counted apart and marked <b>" + REB_BAD_FLAG
            + "</b> in the CSV.</div>"
          : "");

    var bars = top.length
      ? '<div class="mw-bars" style="margin-top:14px">'
        + '<span class="k">Where they are — sales area</span>'
        + top.map(function (b) {
            return '<div class="mw-bar1"><span>' + esc(b)
              + '</span><i><b style="width:'
              + Math.round(byBranch[b] * 100 / maxB) + '%"></b></i><span>'
              + num(byBranch[b]) + "</span></div>";
          }).join("") + "</div>"
      : "";

    var list = rows.slice(0, 12).map(function (r) {
      return '<div class="mw-dtr bnd body"><span>' + esc(r.code || "—")
        + "</span><span><b>" + esc(r.name || "—") + "</b></span><span>"
        + esc(r.desa || "—") + "</span><span>" + esc(r.mc || "—")
        + "</span><span>" + esc(r.branch || "—") + "</span><span>"
        + esc(r.kab || "—") + '</span><span class="n"><b>'
        + r.kmOwn.toFixed(2) + "</b></span><span>"
        + (r.to ? "<b>" + esc(r.to) + "</b>" : "<span class='mw-why'>none within "
           + REB_NEAR + " km</span>") + "</span></div>";
    }).join("");

    el.innerHTML = head + bars
      + '<div class="mw-dtr bnd head" style="margin-top:16px">'
      + "<span>Outlet</span><span>Name</span><span>Desa / Kelurahan</span>"
      + "<span>Microcluster</span><span>Sales area</span><span>City</span>"
      + '<span class="n">Km to own</span><span>Recommended DSE</span></div>'
      + (list || '<div class="mw-dtr bnd"><span>Nothing is out of '
                 + 'boundaries in this file.</span></div>')
      + (rows.length > 12
          ? '<div class="mw-note" style="margin-top:8px">Showing the 12 '
            + "worst of " + num(rows.length)
            + " — the CSV has every one, with region, area, supervisor and "
            + "the recommended DSE.</div>"
          : "")
      + '<div class="mw-note" style="margin-top:10px">Urban is a desa at '
      + num(REB_DENSE) + " people per km² or more (" + REB_URBAN_KM
      + " km allowed); everything else is rural (" + REB_RURAL_KM + " km). "
      + "Measured to the rep's own nearest outlet, not to a centre."
      + (t.borders ? "" : " Borders are not drawn, so no outlet is credited "
        + "with standing inside another rep's patch — build them for that.")
      + "</div>";
    $("dbBoundsCsv").disabled = !rows.length;
  }

  function dashBounds(force) {
    var el = $("dbBounds");
    if (!el) return;
    if (!LOCAL) {
      el.innerHTML = '<div class="mw-note">Connect local data to run this '
        + "review.</div>";
      $("dbBoundsCsv").disabled = true;
      return;
    }
    if (BOUNDS && !force) { renderBounds(); return; }
    if (boundsBusy) return;
    if (!STATS) {                       // the stratum needs the desa table
      el.innerHTML = '<div class="mw-note">Reading desa area and '
        + "population…</div>";
      ensureStats(function () { dashBounds(force); });
      return;
    }
    boundsBusy = true;
    $("dbBoundsCsv").disabled = true;
    el.innerHTML = '<div class="mw-note" id="dbBoundsWait">'
      + "Measuring every outlet against its own round…</div>";
    rebScanAll(function (res) {
      boundsBusy = false;
      BOUNDS = res;
      renderBounds();
    }, function (done, all) {
      var w = $("dbBoundsWait");
      if (w) w.textContent = "Measuring every outlet against its own round — "
        + num(done) + " of " + num(all) + " DSE…";
    });
  }

  if ($("dbBoundsCsv")) {
    $("dbBoundsCsv").addEventListener("click", boundsCsv);
  }
  if ($("dbBoundsRun")) {
    $("dbBoundsRun").addEventListener("click", function () {
      BOUNDS = null; dashBounds(true);
    });
  }

  function dashDupe() {
    var el = $("dbDupe");
    if (!el) return;
    if (!LOCAL) {
      el.innerHTML = '<div class="mw-note">Connect local data to run this '
        + "check.</div>";
      return;
    }
    var D = dupeScan();
    var lines = [
      { k: "One outlet, two reps",
        n: D.conflict.length, set: dupeRows(D.conflict), bad: true,
        why: "Same brand and the same outlet ID on two rounds. Two people "
           + "call on this shop." },
      { k: "Both brands, same shop",
        n: D.samePlace.length, set: dupeRows(D.samePlace),
        why: "3ID and IM3 both visit it, within " + (DUPE_SAME_M * 1000)
           + " m. Expected where the brands share a market — not a "
           + "demarcation error." },
      { k: "Same ID, two different shops",
        n: D.diffPlace.length, set: dupeRows(D.diffPlace),
        why: "The brands reused one number for shops in different places. "
           + "Not double coverage — but outlet ID alone is not a key here. "
           + "Brand + ID is, and so is UNIKID." },
      { k: "The same row twice",
        n: D.repeat.length, set: dupeRows(D.repeat), bad: true,
        why: "Same brand, ID and DSE on more than one line. Inflates every "
           + "count on this page." },
      { k: "Rows with no outlet ID at all",
        n: D.noId, set: null,
        why: "Nothing to check them against." }
    ];

    var head = '<div class="mw-dupehead"><b>' + num(D.multi.length)
      + "</b> outlet ID" + (D.multi.length === 1 ? " is" : "s are")
      + " carried by more than one DSE, of " + num(D.ids)
      + " distinct IDs in this file."
      + (D.conflict.length
          ? ' <span class="mw-flag">' + num(D.conflict.length)
            + " of them same-brand.</span>"
          : ' <span class="mw-ok">None of them same-brand.</span>')
      + "</div>";

    el.innerHTML = head + '<div class="mw-list">' + lines.map(function (r, i) {
      return '<div class="mw-row dupe' + (r.set && r.n ? " click" : "")
        + '" data-i="' + i + '"><span>' + esc(r.k)
        + '<span class="mw-note">' + r.why + "</span></span><span class='"
        + (r.bad && r.n ? "mw-flag" : "") + "'>" + num(r.n) + "</span></div>";
    }).join("") + "</div>";

    el.querySelectorAll(".mw-row.click").forEach(function (row) {
      row.addEventListener("click", function () {
        var r = lines[Number(row.dataset.i)];
        if (r && r.set && r.set.length) pinRows(r.set, r.k);
      });
    });
  }

  function dashLocal() {
    var connected = !!LOCAL;
    $("dbOut").textContent = connected ? num(LOCAL.rows.length) : "—";
    var codes = connected ? Object.keys(LOCAL.dse).length : 0;
    var reps = connected ? repCount(LOCAL.rows) : 0;
    $("dbDse").textContent = connected ? num(reps) : "—";
    $("dbDseNote").innerHTML = !connected ? "" : (hasUnik()
      ? (codes === reps ? "by UNIKDSE"
         : num(codes) + " brand codes · " + num(codes - reps)
           + " work both brands")
      : "by DSE code — no UNIKDSE column in this file");
    $("dbAlert").hidden = connected;

    dashDupe();
    dashBounds();
    renderSlabs();
    if (!connected) {
      $("dbBars").innerHTML = '<div class="mw-note">Connect local data to see this.</div>';
      $("dbChecks").innerHTML = '<div class="mw-note">Connect local data to see this.</div>';
      return;
    }
    var kec = {}, ono = 0;
    LOCAL.rows.forEach(function (r) {
      var k = r.kec || "—";
      kec[k] = (kec[k] || 0) + 1;
      if (r.ono) ono++;
    });
    var names = Object.keys(kec).sort(function (a, b) { return kec[b] - kec[a]; });
    var top = names.slice(0, 8), max = kec[top[0]] || 1;
    $("dbBars").innerHTML = top.map(function (n) {
      return '<div class="mw-bar1"><span>' + esc(n) + '</span><i><b style="width:'
        + Math.round(kec[n] * 100 / max) + '%"></b></i><span>'
        + num(kec[n]) + "</span></div>";
    }).join("") + (names.length > top.length
      ? '<div class="mw-note">and ' + num(names.length - top.length)
        + " more kecamatan</div>" : "");

    // "Desa with no outlet" is only answerable once the desa layer is in
    // hand; until then it says so rather than reporting a zero it cannot
    // stand behind.
    var empty = "—";
    if (CL.kelurahan.features) {
      var n = 0;
      CL.kelurahan.features.forEach(function (f) {
        var p = f.properties || {};
        if (!areaCount[key(featName(p), featKec(p))]) n++;
      });
      empty = num(n) + " of " + num(CL.kelurahan.features.length);
    }
    var nDse = Object.keys(LOCAL.dse).length || 1;
    $("dbChecks").innerHTML = [
      { a: "Outlets with coordinates",
        b: num(LOCAL.rows.length) + " of " + num(LOCAL.stats.read) },
      { a: "Rows without a DSE code", b: num(LOCAL.stats.no_dse) },
      { a: "Desa with no outlet", b: empty },
      { a: "Outlets flagged ONO", b: num(ono) },
      { a: "Avg outlets per DSE", b: (LOCAL.rows.length / nDse).toFixed(1) }
    ].map(function (r) {
      return '<div class="mw-row"><span>' + esc(r.a) + "</span><span>"
        + esc(r.b) + "</span></div>";
    }).join("");
  }

  /* ─────────────────────────────────────────────────────────────────────
     DSE × OUTLET TABLE
     ───────────────────────────────────────────────────────────────────── */
  var CAP = 300, shown = [];

  /* A set of rows handed to this table from somewhere else -- a dashboard
     check, say -- so the finding and the rows behind it are one click apart
     rather than a search the reader has to reconstruct. The ordinary
     search and DSE filters still narrow whatever is pinned. */
  var tbPin = null, tbPinLabel = "";

  function pinRows(rows, label) {
    tbPin = rows.slice();
    tbPinLabel = label || "";
    $("tbSearch").value = "";
    $("tbDse").value = "";
    go("table");
    renderTable();
  }

  function filtered() {
    if (!LOCAL) return [];
    var q = norm($("tbSearch").value), d = $("tbDse").value;
    return (tbPin || LOCAL.rows).filter(function (r) {
      if (!inSlice(r)) return false;
      if (d && r.dse !== d) return false;
      if (!q) return true;
      return norm(r.code + r.name + r.desa + r.kec + r.dse + r.mc).indexOf(q) >= 0;
    });
  }
  function renderTable() {
    shown = filtered();
    $("tbCount").textContent = num(shown.length) + " rows · local file";
    if (!LOCAL) {
      $("tbBody").innerHTML = "";
      $("tbFoot").textContent = "No local file loaded.";
      return;
    }
    var slice = shown.slice(0, CAP);
    $("tbBody").innerHTML = slice.map(function (r, i) {
      return '<div class="mw-tr body" data-i="' + i + '"><span>' + esc(r.code)
        + "</span><span>" + esc(r.name) + "</span><span>" + esc(r.kec)
        + "</span><span>" + esc(r.desa) + "</span><span>" + esc(r.dse)
        + "</span><span>" + esc(r.mc) + '</span><span class="'
        + (r.ono ? "mw-flag" : "") + '">' + esc(r.rem || "—") + "</span></div>";
    }).join("");
    var foot = shown.length > CAP
      ? "Showing first " + num(CAP) + " of " + num(shown.length) + " rows"
      : num(shown.length) + " rows";
    if (tbPin) {
      $("tbFoot").innerHTML = "<b>" + esc(tbPinLabel) + "</b> &middot; " + foot
        + ' <button type="button" class="nb" id="tbUnpin">Show every row</button>';
      $("tbUnpin").addEventListener("click", function () {
        tbPin = null; tbPinLabel = ""; renderTable();
      });
    } else {
      $("tbFoot").textContent = foot;
    }
    Array.prototype.forEach.call(
      $("tbBody").querySelectorAll(".mw-tr.body"), function (el) {
        el.addEventListener("click", function () {
          Array.prototype.forEach.call($("tbBody").children, function (x) {
            x.classList.remove("sel"); });
          el.classList.add("sel");
          var r = slice[Number(el.getAttribute("data-i"))];
          go("map");
          map.setView([r.lat, r.lon], 16, { animate: false });
          pickOutlet(r);
        });
      });
  }
  $("tbSearch").addEventListener("input", renderTable);
  $("tbDse").addEventListener("change", function () {
    $("mwDse").value = $("tbDse").value;
    onDse();
  });
  $("tbShow").addEventListener("click", function () {
    if (!shown.length) return;
    go("map");
    var b = L.latLngBounds(shown.map(function (r) { return [r.lat, r.lon]; }));
    if (b.isValid()) map.fitBounds(b.pad(.1), { animate: false });
  });

  /* The export is built and downloaded here. Nothing is posted to produce
     it, so a report of local data never becomes a copy of local data on the
     server on its way back to the desk it came from. */
  $("tbExport").addEventListener("click", function () {
    if (!shown.length) return;
    var cols = ["code", "name", "kec", "desa", "dse", "mc", "cat", "spv",
                "freq", "pair", "rem", "lat", "lon"];
    var head = ["Outlet", "Name", "Kecamatan", "Desa", "DSE", "Microcluster",
                "Category", "Supervisor", "Visit freq", "Pairing", "Flag",
                "Latitude", "Longitude"];
    function q(v) {
      var s = String(v == null ? "" : v);
      return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
    }
    var csv = head.join(",") + "\n" + shown.map(function (r) {
      return cols.map(function (c) { return q(r[c]); }).join(",");
    }).join("\n");
    var url = URL.createObjectURL(
      new Blob(["﻿" + csv], { type: "text/csv;charset=utf-8" }));
    var a = document.createElement("a");
    a.href = url;
    a.download = "dse-outlet-" + shown.length + "-rows.csv";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  });

  /* ─────────────────────────────────────────────────────────────────────
     LOCAL DATA TAB — load, map the columns, and clear
     ───────────────────────────────────────────────────────────────────── */
  function lerr(html) {
    var el = $("ldErr");
    el.innerHTML = html || "";
    el.hidden = !html;
  }

  $("ldPick").addEventListener("click", function () { $("ldFile").click(); });
  $("ldFile").addEventListener("change", function () {
    readFiles($("ldFile").files);
    $("ldFile").value = "";
  });
  ["dragenter", "dragover"].forEach(function (t) {
    $("ldDrop").addEventListener(t, function (e) {
      e.preventDefault(); $("ldDrop").classList.add("over"); });
  });
  ["dragleave", "drop"].forEach(function (t) {
    $("ldDrop").addEventListener(t, function (e) {
      e.preventDefault(); $("ldDrop").classList.remove("over"); });
  });
  $("ldDrop").addEventListener("drop", function (e) {
    if (e.dataTransfer && e.dataTransfer.files) readFiles(e.dataTransfer.files);
  });

  /* The Site locations slot on the Local data page. It reports what was
     matched and what was not, in the same red-for-missing grammar the
     outlet mapping uses, because "Site Type → not found" is exactly the
     thing worth knowing before somebody wonders why every dot is grey. */
  function renderSiteSlot() {
    var cell = $("ldSites");
    if (!cell) return;
    if (!SITES) { cell.hidden = true; return; }
    var order = ["site_id", "site_name", "lat", "lon", "site_type"];
    $("ldSiteMap").innerHTML = order.map(function (f) {
      var src = (SITES.src || {})[f];
      return '<div class="mw-chip' + (src ? "" : " miss") + '">'
        + esc(src || "not found") + " → " + esc(SITE_LABEL[f]) + "</div>";
    }).join("");
    var types = Object.keys(SITES.types || {});
    var bits = [num(SITES.rows.length) + " site"
                + (SITES.rows.length === 1 ? "" : "s")];
    if (types.length) bits.push(num(types.length) + " type"
                                + (types.length === 1 ? "" : "s"));
    if (SITES.skipped) bits.push(num(SITES.skipped) + " row"
      + (SITES.skipped === 1 ? "" : "s") + " with no coordinates, skipped");
    $("ldSiteSum").textContent = bits.join(" · ");
    $("ldSiteNote").innerHTML = SITES.guessed
      ? "<b>No site column headings were recognised.</b> Two numeric "
        + "columns were read as a coordinate and the first text column as a "
        + "name. Check the dots land where you expect before trusting them."
      : "Read from “" + esc(SITES.sheet) + "” in " + esc(SITES.filename)
        + ". Held in this browser and dropped on sign-out — the server is "
        + "never sent a row of it.";
    cell.hidden = false;
  }

  if ($("ldClearSites")) {
    $("ldClearSites").addEventListener("click", function () {
      SITES = null; siteHue = {}; localSiteCount = {};
      if (lsiteLayer) { map.removeLayer(lsiteLayer); lsiteLayer = null; }
      renderSiteSlot();
      renderSiteKey();
      $("mwLSiteBtn").textContent = "Local sites (0)";
      setBtn("[data-local='lsites']", false);
      // The outlet file is untouched: these are two files, and dropping one
      // is not a reason to make somebody load the other again.
      if (LOCAL) {
        $("ldSummary").textContent = 1 + " file · "
          + num(LOCAL.rows.length) + " outlets · "
          + num(Object.keys(LOCAL.dse).length) + " DSE";
        $("mwLocalChip").textContent = "Local: 1 file loaded";
      } else {
        signOut();
      }
    });
  }

  function adopt(got, errs) {
    if (got.outlets) {
      LOCAL = got.outlets;
      DUPE = null;
      BOUNDS = null;
    }
    if (got.sites) { SITES = got.sites; siteHue = {}; localSiteCount = {}; }
    reindex();
    renderSiteSlot();

    // A site file on its own is a complete load. Everything below describes
    // the outlet workbook, and without one there is nothing there to say.
    if (!LOCAL) {
      $("ldStep2").hidden = true;
      $("ldSummary").textContent = num(SITES.rows.length) + " site"
        + (SITES.rows.length === 1 ? "" : "s") + " · " + SITES.filename
        + " · no outlet file loaded";
      $("ldStep3").hidden = false;
      $("mwLocalChip").textContent = "Local: sites only";
      $("mwLocalChip").classList.remove("dash");
      $("mwSignOut").hidden = false;
      $("mwLSiteBtn").textContent = "Local sites ("
        + num(SITES.rows.length) + ")";
      setBtn("[data-local='lsites']", true);
      drawLocalSites();
      lerr(errs && errs.length ? errs.join("<br>") : "");
      return;
    }

    // Step 2 — what was found in the header, and what was not. A missing
    // column is shown in red rather than left out, because "site_id
    // unmatched" is the thing worth knowing.
    var src = LOCAL.src || {};
    var order = ["outlet_code", "outlet_name", "dse", "lat", "lon", "desa",
                 "kecamatan", "mc", "category", "supervisor", "schedule",
                 "pairing", "remarks"];
    $("ldMap").innerHTML = order.map(function (f) {
      var s = src[f];
      return '<div class="mw-chip' + (s ? "" : " miss") + '">'
        + esc(s || "not found") + " → " + esc(LABEL[f] || f) + "</div>";
    }).join("");
    $("ldSheet").textContent = "Read from sheet “" + LOCAL.sheet + "” in "
      + LOCAL.filename + ".";
    $("ldStep2").hidden = false;

    var files = [LOCAL.filename];
    if (SITES) files.push(SITES.filename);
    $("ldSummary").textContent = files.length
      + (files.length === 1 ? " file · " : " files · ")
      + num(LOCAL.rows.length) + " outlets · "
      + num(Object.keys(LOCAL.dse).length) + " DSE"
      + (SITES ? " · " + num(SITES.rows.length) + " sites" : "");
    $("ldStep3").hidden = false;
    lerr(errs && errs.length ? errs.join("<br>") : "");

    // Chips, filters, layers, panels — everything downstream of the file.
    $("mwLocalChip").textContent = "Local: " + files.length
      + (files.length === 1 ? " file loaded" : " files loaded");
    $("mwLocalChip").classList.remove("dash");
    $("mwSignOut").hidden = false;

    var codes = Object.keys(LOCAL.dse).sort();
    var opts = '<option value="">All DSE</option>' + codes.map(function (c) {
      return '<option value="' + esc(c) + '">' + esc(c) + " ("
        + num(LOCAL.dse[c]) + ")</option>";
    }).join("");
    $("mwDse").innerHTML = opts;
    $("tbDse").innerHTML = opts;
    dseFilter = "";

    $("mwOutBtn").textContent = "Outlets (" + num(LOCAL.rows.length) + ")";
    $("mwLSiteBtn").textContent = "Local sites ("
      + num(SITES ? SITES.rows.length : 0) + ")";
    setBtn("[data-local='outlets']", true);

    drawOutlets(); drawLocalSites();
    if (heatOn) restyle("kelurahan");
    dashLocal(); renderTable(); idle(); roster();

    if (LOCAL.box) {
      map.fitBounds(L.latLngBounds([[LOCAL.box[1], LOCAL.box[0]],
                                    [LOCAL.box[3], LOCAL.box[2]]]).pad(.06),
                    { animate: false });
    }
    go("map");
  }

  /* ─────────────────────────────────────────────────────────────────────
     SIGN OUT — the one function that has to be complete
     ─────────────────────────────────────────────────────────────────────
     Every place a local row can have reached: the model, the two map
     layers, the derived index, the selection outline, the two DSE pickers,
     the table, the dashboard tiles, the inspector, the three steps of the
     connector, and the chip in the bar. If a local value can outlive this
     function then the promise on the page is not true, so it is written as
     one list rather than scattered through the handlers that made each
     thing. */
  function signOut() {
    LOCAL = null;
    SITES = null; siteHue = {}; localSiteCount = {};
    DUPE = null; BOUNDS = null;
    tbPin = null; tbPinLabel = "";
    byArea = {}; areaCount = {}; heatMax = 1;
    dseFilter = ""; shown = [];

    if (outLayer) { map.removeLayer(outLayer); outLayer = null; }
    if (lsiteLayer) { map.removeLayer(lsiteLayer); lsiteLayer = null; }
    if (spotLayer) { map.removeLayer(spotLayer); spotLayer = null; }
    if (pickLayer) { map.removeLayer(pickLayer); pickLayer = null; }
    // The borders were drawn from the file that is going away, and the
    // ledger is a reading of them.
    clearBorders();
    wTicket++; wDesa = []; wOut = [];
    var dr = $("mwDrawer");
    if (dr) dr.hidden = true;
    mmsg("Load a local file, then build the borders its outlets imply.");
    if (heatOn) {
      heatOn = false;
      setBtn("[data-local='heat']", false);
      restyle("kelurahan");
    }

    var opts = '<option value="">All DSE</option>';
    $("mwDse").innerHTML = opts; $("tbDse").innerHTML = opts;
    $("tbSearch").value = ""; $("mwSearch").value = "";
    $("mwOutBtn").textContent = "Outlets (0)";
    $("mwLSiteBtn").textContent = "Local sites (0)";
    setBtn("[data-local='outlets']", true);
    setBtn("[data-local='lsites']", false);

    $("ldStep2").hidden = true; $("ldStep3").hidden = true;
    $("ldMap").innerHTML = ""; $("ldSummary").textContent = "";
    $("ldSheet").textContent = ""; $("ldFile").value = ""; lerr("");

    $("mwLocalChip").textContent = "Local: not connected";
    $("mwLocalChip").classList.add("dash");
    $("mwSignOut").hidden = true;

    dashLocal(); renderTable(); idle(); roster();

    // Nothing on this page is meant to be recoverable from storage. These
    // two lines are belt and braces against a future edit that decides a
    // cache would be convenient.
    try { sessionStorage.removeItem("mw.local"); } catch (e) { /* private */ }
    try { localStorage.removeItem("mw.local"); } catch (e) { /* private */ }
  }
  $("mwSignOut").addEventListener("click", signOut);
  $("ldClear").addEventListener("click", function () { signOut(); go("local"); });

  /* ─────────────────────────────────────────────────────────────────────
     VIEWS
     ───────────────────────────────────────────────────────────────────── */
  function go(v) {
    Array.prototype.forEach.call(
      document.querySelectorAll(".mw-bar .nb"), function (b) {
        b.classList.toggle("on", b.dataset.view === v); });
    Array.prototype.forEach.call(
      document.querySelectorAll(".mw-view"), function (el) {
        el.classList.toggle("on", el.dataset.view === v); });
    if (v === "map") setTimeout(function () { map.invalidateSize(); }, 0);
  }
  Array.prototype.forEach.call(
    document.querySelectorAll(".mw-bar .nb"), function (b) {
      b.addEventListener("click", function () { go(b.dataset.view); });
    });
  Array.prototype.forEach.call(
    document.querySelectorAll(".mw [data-goto]"), function (b) {
      b.addEventListener("click", function () { go(b.dataset.goto); });
    });


  /* ══════════════════════════════════════════════════════════════════════
     FOLDING RAIL SECTIONS
     The rail is 230px and five sections deep. Somebody hunting through the
     territory tree wants that tree tall and everything else out of the way,
     so each header is a toggle rather than a label.
     ══════════════════════════════════════════════════════════════════════ */
  Array.prototype.forEach.call(
    document.querySelectorAll(".mw .kfold"), function (b) {
      b.addEventListener("click", function () {
        b.setAttribute("aria-expanded",
          b.getAttribute("aria-expanded") === "false" ? "true" : "false");
      });
    });

  /* ══════════════════════════════════════════════════════════════════════
     REGION > AREA > SALES AREA > TERRITORY > DSE

     The first four levels come from the cloud, in one response: the whole
     tree is 89 territories over 825 kecamatan, small enough that a request
     per level would only put a round trip between every click of a filter
     somebody is using to hunt.

     The fifth level does not come from the cloud at all. The DSE under a
     territory are read from the workbook on this desk, because that is the
     roster being reviewed; a list served from the server would be a second
     roster that could disagree with it.

     The tree is flattened to one row per territory on arrival, so each
     select is a distinct over rows rather than a walk down four levels of
     nested objects -- which is also what makes "all regions, one sales
     area" work without a special case.
     ══════════════════════════════════════════════════════════════════════ */
  var ROWS = [];          // {region, area, branch, terr, kecs[], n}
  var terrKec = null;     // normalised kecamatan in the current slice
  var terrLabel = "";

  function inSlice(r) {
    if (!terrKec) return true;
    return terrKec[norm(r.kec)] === 1;
  }

  function distinct(rows, k) {
    var by = {};
    rows.forEach(function (x) { by[x[k]] = (by[x[k]] || 0) + x.n; });
    return Object.keys(by).sort().map(function (n) {
      return { name: n, n: by[n] }; });
  }
  function fill(id, rows, all) {
    var el = $(id), keep = el.value;
    el.innerHTML = '<option value="">' + all + "</option>"
      + rows.map(function (r) {
          return '<option value="' + esc(r.name) + '">' + esc(r.name)
            + " · " + num(r.n) + "</option>"; }).join("");
    // A pick that is still reachable survives a change further up the tree;
    // one that is not is dropped rather than left showing a slice that no
    // longer exists.
    el.value = rows.some(function (r) { return r.name === keep; }) ? keep : "";
  }
  function picks() {
    return { r: $("mwRegion").value, a: $("mwArea").value,
             b: $("mwBranch").value, t: $("mwTerr").value };
  }
  function matching(depth) {
    var p = picks();
    return ROWS.filter(function (x) {
      if (depth > 0 && p.r && x.region !== p.r) return false;
      if (depth > 1 && p.a && x.area !== p.a) return false;
      if (depth > 2 && p.b && x.branch !== p.b) return false;
      if (depth > 3 && p.t && x.terr !== p.t) return false;
      return true;
    });
  }

  function cascade(changed) {
    if (changed === "region") { $("mwArea").value = ""; }
    if (changed === "region" || changed === "area") { $("mwBranch").value = ""; }
    if (changed !== "terr") { $("mwTerr").value = ""; }

    fill("mwRegion", distinct(ROWS, "region"), "All regions");
    fill("mwArea", distinct(matching(1), "area"), "All areas");
    fill("mwBranch", distinct(matching(2), "branch"), "All sales areas");
    fill("mwTerr", distinct(matching(3), "terr"), "All territories");

    // CHOOSING IS NOT APPLYING
    // The four selects only stage a choice now. Committing on every change
    // meant the map, the table and the DSE roster all moved three times
    // while somebody worked down from region to territory, and the roster
    // they were reaching for kept being rebuilt under them. Apply commits.
    var slice = matching(4), p = picks();
    var staged = p.t || p.b || p.a || p.r;
    var kecN = 0;
    slice.forEach(function (x) { kecN += x.kecs.length; });
    $("mwTerrNote").innerHTML = staged
      ? "<b>" + esc(staged) + "</b> · " + num(slice.length)
        + (slice.length === 1 ? " territory · " : " territories · ")
        + num(kecN) + " kecamatan staged"
        + (staged === terrLabel ? " · applied"
           : " · <b>press Apply</b> to filter the map and list its DSE")
      : num(ROWS.length) + " territories · "
        + num(ROWS.reduce(function (a, x) { return a + x.n; }, 0))
        + " kecamatan. Narrow it, then Apply, to list the DSE working there.";
  }

  function applySlice() {
    var slice = matching(4), p = picks();
    var named = p.t || p.b || p.a || p.r;
    terrLabel = named || "";
    if (!named) {
      terrKec = null;
    } else {
      terrKec = {};
      slice.forEach(function (x) {
        x.kecs.forEach(function (k) { terrKec[norm(k)] = 1; });
      });
    }
    // A DSE picked inside the old slice may not exist in the new one, so the
    // filter is dropped rather than left selecting nothing.
    if (dseFilter && LOCAL) {
      var still = LOCAL.rows.some(function (r) {
        return r.dse === dseFilter && inSlice(r); });
      if (!still) { dseFilter = ""; $("mwDse").value = ""; $("tbDse").value = ""; }
    }
    roster();
    drawOutlets();
    renderTable();
    renderSlabs();
    // Borders built over a different slice are not this slice's borders.
    if (MODEL) {
      clearBorders();
      mmsg("The slice changed — build the borders again for it.");
    }
    if (named) zoomSlice();
    cascade("terr");
  }

  // Zoom to the kecamatan the slice names, from the layer already in hand.
  // If that layer is not loaded yet the outlets in the slice frame it well
  // enough, and the request that would fetch it is not worth a filter click.
  function zoomSlice() {
    var b = null;
    if (CL.kecamatan.features) {
      CL.kecamatan.features.forEach(function (f) {
        var p = f.properties || {};
        if (!terrKec[norm(featName(p))]) return;
        var l = L.geoJSON(f);
        try {
          var bb = l.getBounds();
          if (bb.isValid()) b = b ? b.extend(bb) : bb;
        } catch (e) { /* empty ring */ }
      });
    }
    if (!b && LOCAL) {
      var pts = LOCAL.rows.filter(inSlice).map(function (r) {
        return [r.lat, r.lon]; });
      if (pts.length) b = L.latLngBounds(pts);
    }
    if (b && b.isValid()) map.fitBounds(b.pad(.08), { animate: false });
  }

  // The DSE roster, from the workbook, for whatever the tree is showing.
  function roster() {
    var el = $("mwDseList");
    if (!el) return;
    if (!LOCAL) {
      el.innerHTML = terrKec
        ? '<div class="mw-dsehead">DSE · local file</div>'
          + '<div class="mw-note" style="padding:8px 9px">Load the demarkasi '
          + "file to see who works here.</div>"
        : "";
      return;
    }
    var by = {};
    LOCAL.rows.forEach(function (r) {
      if (!inSlice(r)) return;
      by[r.dse] = (by[r.dse] || 0) + 1;
    });
    var codes = Object.keys(by).sort(function (a, b) { return by[b] - by[a]; });
    if (!codes.length) {
      el.innerHTML = '<div class="mw-dsehead">DSE · local file</div>'
        + '<div class="mw-note" style="padding:8px 9px">No outlet in your '
        + "file falls in this slice.</div>";
      return;
    }
    el.innerHTML = '<div class="mw-dsehead">' + num(codes.length)
      + " DSE · local file</div>"
      + codes.map(function (c) {
          return '<button type="button" class="mw-drow'
            + (c === dseFilter ? " on" : "") + '" data-dse="' + esc(c)
            + '"><span>' + esc(c) + "</span><span>" + num(by[c])
            + "</span></button>";
        }).join("");
    el.querySelectorAll(".mw-drow").forEach(function (b) {
      b.addEventListener("click", function () {
        // Clicking the rep already showing steps back out to the slice,
        // so the roster is a toggle rather than a one-way door.
        var pick = (b.dataset.dse === dseFilter) ? "" : b.dataset.dse;
        $("mwDse").value = pick;
        onDse();
        roster();
        if (pick) drawerDse(pick);
      });
    });
  }

  ["mwRegion", "mwArea", "mwBranch", "mwTerr"].forEach(function (id) {
    var el = $(id);
    if (el) el.addEventListener("change", function () {
      cascade(id.replace("mw", "").toLowerCase());
    });
  });

  if ($("mwApply")) $("mwApply").addEventListener("click", applySlice);
  if ($("mwClearTerr")) $("mwClearTerr").addEventListener("click", function () {
    ["mwRegion", "mwArea", "mwBranch", "mwTerr"].forEach(function (id) {
      $(id).value = "";
    });
    cascade(null);
    applySlice();
  });

  fetch("/api/territory/hierarchy")
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (d.ok === false) throw new Error(d.error || "unavailable");
      (d.regions || []).forEach(function (rg) {
        (rg.children || []).forEach(function (ar) {
          (ar.children || []).forEach(function (br) {
            (br.children || []).forEach(function (te) {
              ROWS.push({ region: rg.name, area: ar.name, branch: br.name,
                          terr: te.name, n: te.n,
                          kecs: (te.kecamatan || []).map(function (k) {
                            return k.kecamatan; }) });
            });
          });
        });
      });
      cascade(null);
      // Until the tree lands there is nowhere to file a rep, so the matrix
      // waits for it rather than showing everyone under "—".
      renderSlabs();
    })
    .catch(function (e) {
      $("mwTerrNote").textContent =
        "The territory tree is not available: " + String(e);
    });

  /* ══════════════════════════════════════════════════════════════════════
     THE CLOUD HALF OF THE INSPECTOR
     Dashed tiles above are the reader's file. These are the application's
     own: how big the area is, who lives in it, how many masts stand in it.
     Fetched on the click, never on the hover -- a hover that fired requests
     over a desa layer would send hundreds of them across one sweep.
     ══════════════════════════════════════════════════════════════════════ */
  var cloudN = 0;

  function cloudProfile(def, p, rs) {
    var el = $("mwCloud");
    if (!el) return;
    var n = ++cloudN;
    var head = '<span class="k mw-cloudhead">From the cloud</span>';
    if (def.key !== "kelurahan") {
      el.innerHTML = featPop(p) != null
        ? head + tiles([{ k: "Population", v: num(featPop(p)) },
                        { k: "Layer", v: esc(def.label) }])
        : "";
      return;
    }
    el.innerHTML = head + '<div class="mw-note" style="margin-top:8px">'
      + "reading area, population and masts…</div>";
    fetch("/api/samples/desa-profile?desa="
          + encodeURIComponent(featName(p))
          + "&kecamatan=" + encodeURIComponent(featKec(p)))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (n !== cloudN) return;
        if (d.ok === false) { el.innerHTML = ""; return; }
        var per = d.desa_km2 ? (rs.length / d.desa_km2) : null;
        el.innerHTML = head
          + tiles([{ k: "Area km²", v: d.desa_km2.toFixed(3) },
                   { k: "People", v: num(d.population) },
                   { k: "Sites", v: num(d.sites) },
                   { k: "Outlets / km²", v: per == null ? "—" : per.toFixed(1) }])
          + '<div class="mw-note" style="margin-top:8px">'
          + esc([d.kecamatan, d.kabupaten, d.mc].filter(Boolean).join(" · "))
          + (d.code ? " · BPS " + esc(d.code) : "") + "</div>";
      })
      .catch(function () { if (n === cloudN) el.innerHTML = ""; });
  }


  /* ══════════════════════════════════════════════════════════════════════
     BORDER POLYGONS — the same three definitions, from the same server

     Exclusive, Coverage and Force fit are the application's own answers to
     "what is this rep's patch". They are computed on the server from
     (DSE, lat, lon) alone and nothing is kept -- no row, no file, no cache
     key. Writing a second implementation in the browser would be a second
     answer that could quietly disagree with the Polygon Samples page for
     the same workbook, which is the one thing a demarcation review cannot
     afford.

     Only three values per outlet cross the wire. Codes, names, categories,
     remarks and everything else stay here.

     The build honours the territory cascade: with a slice picked it models
     that slice, which is both faster and what somebody looking at one sales
     area actually wants.
     ══════════════════════════════════════════════════════════════════════ */
  var MODEL = null, modelMode = "exclusive", modelBusy = false;
  var modelPick = null, modelBy = {};

  var PALETTE = ["#ec3013", "#1f7a8c", "#c98a00", "#5a7d2a", "#7d5ba6",
                 "#b5651d", "#2f6fb5", "#a8437a", "#3f8f6f", "#8a6d3b",
                 "#4a5bbf", "#9c6b1f"];
  var ORDER = {};
  function colourOf(code) {
    if (ORDER[code] === undefined) ORDER[code] = Object.keys(ORDER).length;
    return PALETTE[ORDER[code] % PALETTE.length];
  }
  /* ══════════════════════════════════════════════════════════════════════
     MAKING A BORDER READABLE OVER FOUR OTHER BOUNDARIES

     A DSE patch is drawn on top of desa, kecamatan, microcluster and
     kabupaten lines, an outlet layer and a basemap. A plain 2px stroke in
     one of twelve rotating colours loses that competition: it disappears
     into whatever it crosses, and two neighbouring patches that happen to
     draw the same colour read as one territory.

     Four things fix it, and they are independent so each can be judged on
     its own:

     1. CASING. Every border is drawn twice -- a wide pale line underneath,
        the coloured line on top. That is the oldest trick in cartography and
        the only one that survives an unknown background: the halo separates
        the stroke from whatever it crosses, so the same border reads on a
        pale basemap, on a dark one, and over a dense desa mesh.

     2. NEIGHBOURS NEVER SHARE A COLOUR. A rotating palette gives adjacent
        patches the same hue often enough to matter -- and two touching
        patches in one colour look like one patch. Colours are assigned by
        greedy graph colouring over which patches actually touch, so
        adjacency is guaranteed to differ; the palette is only a source of
        hues, not an order.

     3. THE VIEW MODE decides what competes. Outline drops the fill so the
        boundary layers below stay legible. Tinted adds a light wash so a
        patch reads as an area without hiding the ground. Focus mutes the
        cloud layers and every unselected patch, which is the only honest
        way to look at one territory in a mesh of 1,360.

     4. LABELS. The code on the patch is what removes the need to hover at
        all. They are gated on how many are visible: past about forty they
        collide into noise and are worth less than the map they cover.
     ══════════════════════════════════════════════════════════════════════ */
  var bView = "tint", bLabels = true, CASE = null, labelLayer = null;

  // Eight hues far enough apart to be told apart at a 2px stroke, dark
  // enough to hold their own against a pale casing.
  var HUES = ["#d1341c", "#1f6fb2", "#3f8f43", "#8a4fbf", "#c47b0a",
              "#0f8f8f", "#b03a7a", "#5a6b1f"];

  /* Two patches are treated as neighbours when their bounding boxes meet.
     That is looser than true adjacency -- it occasionally separates two
     patches that never touch -- but it errs towards more distinct colours,
     which is the harmless direction, and it costs one pass instead of an
     edge comparison across every pair. */
  function paintBorders(features) {
    var boxes = features.map(function (f) {
      var b = bbOf(f);
      return b ? [b[0] - 1e-4, b[1] - 1e-4, b[2] + 1e-4, b[3] + 1e-4] : null;
    });
    var near = features.map(function () { return []; });
    for (var i = 0; i < features.length; i++) {
      if (!boxes[i]) continue;
      for (var j = i + 1; j < features.length; j++) {
        if (!boxes[j]) continue;
        var a = boxes[i], b2 = boxes[j];
        if (a[2] < b2[0] || b2[2] < a[0] || a[3] < b2[1] || b2[3] < a[1]) continue;
        near[i].push(j); near[j].push(i);
      }
    }
    // Most-constrained first, so the crowded middle of the map is coloured
    // before the palette is spent on the quiet edges.
    var order = features.map(function (_, i) { return i; })
      .sort(function (x, y) { return near[y].length - near[x].length; });
    var col = new Array(features.length);
    order.forEach(function (i) {
      var taken = {};
      near[i].forEach(function (j) { if (col[j] != null) taken[col[j]] = 1; });
      var c = 0;
      while (taken[c]) c++;
      col[i] = c;
      features[i].properties._c = HUES[c % HUES.length];
    });
    return Math.max.apply(null, col.map(function (c) { return c + 1; }));
  }

  function borderColour(p) { return p._c || colourOf(p.dse); }

  function borderStyle(p, dim) {
    var c = borderColour(p);
    var fill = bView === "outline" ? 0 : (bView === "focus" ? .16 : .12);
    if (dim) {
      return { color: c, weight: bView === "focus" ? .8 : 1,
               opacity: bView === "focus" ? .16 : .3,
               fillColor: c, fillOpacity: bView === "focus" ? 0 : .02 };
    }
    return { color: c, weight: bView === "outline" ? 2.6 : 2.2, opacity: 1,
             fillColor: c, fillOpacity: fill };
  }
  // The casing is not interactive: it sits under the border purely to
  // separate it from the ground, and a click meant for the patch must not
  // land on its halo.
  function caseStyle(p, dim) {
    return { color: "#ffffff", weight: dim ? 2 : 5,
             opacity: dim ? (bView === "focus" ? .1 : .25) : .85,
             fill: false, interactive: false };
  }

  function restyleBorders() {
    if (!MODEL) return;
    drawOutlets();
    drawSpot();
    MODEL.eachLayer(function (l) {
      var p = l.feature.properties;
      l.setStyle(borderStyle(p, modelPick && p.dse !== modelPick));
    });
    if (CASE) CASE.eachLayer(function (l) {
      var p = l.feature.properties;
      l.setStyle(caseStyle(p, modelPick && p.dse !== modelPick));
    });
    dimCloud();
    drawLabels();
  }

  /* Focus mutes the cloud boundaries rather than switching them off: the
     ground a territory sits on is still part of reading it, and a layer
     that vanishes and comes back is more disorienting than one that fades. */
  function dimCloud() {
    var mute = (bView === "focus" && MODEL);
    DEPTH.forEach(function (k) {
      var c = CL[k];
      if (!c || !c.layer) return;
      c.layer.setStyle(function (f) {
        var st = polyStyle(c.def, f);
        if (mute) {
          st.opacity = 0.18;
          st.fillOpacity = Math.min(st.fillOpacity, 0.03);
        }
        return st;
      });
    });
  }

  /* Labels are placed at the largest ring's centroid rather than the
     bounding-box centre: a patch shaped like an L has its box centre
     outside itself, and a code floating in a neighbour's ground is worse
     than no code at all. */
  function ringCentroid(ring) {
    var a = 0, cx = 0, cy = 0;
    for (var i = 0; i < ring.length - 1; i++) {
      var x0 = ring[i][0], y0 = ring[i][1];
      var x1 = ring[i + 1][0], y1 = ring[i + 1][1];
      var f = x0 * y1 - x1 * y0;
      a += f; cx += (x0 + x1) * f; cy += (y0 + y1) * f;
    }
    if (!a) return [ring[0][0], ring[0][1]];
    return [cx / (3 * a), cy / (3 * a)];
  }
  function labelPoint(f) {
    var g = f.geometry;
    if (!g) return null;
    var rings = g.type === "Polygon" ? [g.coordinates]
              : (g.type === "MultiPolygon" ? g.coordinates : null);
    if (!rings) return null;
    var best = null, bestA = -1;
    rings.forEach(function (poly) {
      var r = poly[0];
      if (!r || r.length < 4) return;
      var a = 0;
      for (var i = 0; i < r.length - 1; i++)
        a += r[i][0] * r[i + 1][1] - r[i + 1][0] * r[i][1];
      a = Math.abs(a);
      if (a > bestA) { bestA = a; best = r; }
    });
    if (!best) return null;
    var c = ringCentroid(best);
    return [c[1], c[0]];
  }

  var LABEL_CAP = 40;
  function drawLabels() {
    if (labelLayer) { map.removeLayer(labelLayer); labelLayer = null; }
    if (!MODEL || !bLabels) return;
    var fs = [];
    MODEL.eachLayer(function (l) {
      if (l.feature && (!modelPick || l.feature.properties.dse === modelPick))
        fs.push(l.feature);
    });
    // Past the cap they collide into noise and cover more than they name.
    if (fs.length > LABEL_CAP) return;
    labelLayer = L.layerGroup();
    fs.forEach(function (f) {
      var ll = labelPoint(f);
      if (!ll) return;
      labelLayer.addLayer(L.marker(ll, {
        interactive: false, keyboard: false,
        icon: L.divIcon({ className: "mw-lab", html: esc(f.properties.dse),
                          iconSize: null })
      }));
    });
    labelLayer.addTo(map);
  }

  if ($("mwBorderView")) {
    $("mwBorderView").addEventListener("click", function (e) {
      var b = e.target.closest("button[data-bview]");
      if (!b || b.dataset.bview === bView) return;
      bView = b.dataset.bview;
      $("mwBorderView").querySelectorAll("button").forEach(function (x) {
        x.classList.toggle("on", x.dataset.bview === bView);
      });
      restyleBorders();
    });
  }
  if ($("mwBorderLab")) {
    $("mwBorderLab").addEventListener("change", function () {
      bLabels = $("mwBorderLab").checked;
      drawLabels();
    });
  }
  function mmsg(html, bad) {
    var el = $("mwModelMsg");
    if (!el) return;
    el.innerHTML = html || "";
    el.style.color = bad ? "var(--red)" : "";
  }

  function clearBorders() {
    if (MODEL) { map.removeLayer(MODEL); MODEL = null; }
    if (CASE) { map.removeLayer(CASE); CASE = null; }
    if (labelLayer) { map.removeLayer(labelLayer); labelLayer = null; }
    if (spotLayer) { map.removeLayer(spotLayer); spotLayer = null; }
    modelPick = null; modelBy = {};
    drawOutlets();
    dimCloud();          // the cloud layers come back up
  }

  function buildBorders() {
    if (!LOCAL) { mmsg("Load a local file first — the borders are drawn "
                       + "from its outlets.", true); return; }
    if (modelBusy) return;
    var pts = LOCAL.rows.filter(inSlice);
    if (!pts.length) { mmsg("No outlet in your file falls in this slice.", true); return; }
    modelBusy = true;
    $("mwBuild").disabled = true;
    busy(true);
    mmsg("Building " + esc(modelMode) + " borders from " + num(pts.length)
         + " outlets… <span class='mw-note'>(computed on the server and "
         + "not kept)</span>");
    var reach = Number($("mwReach").value);

    fetch("/api/samples/model.geojson", {
      method: "POST", headers: { "Content-Type": "application/json" },
      // (DSE, lat, lon) and nothing else.
      body: JSON.stringify({ mode: modelMode, reach: reach,
                             cell: Math.max(100, reach / 2),
                             points: pts.map(function (r) {
                               return [r.dse, r.lat, r.lon]; }) })
    }).then(function (r) { return r.json(); })
      .then(function (fc) {
        modelBusy = false; $("mwBuild").disabled = false; busy(false);
        clearBorders();
        if (fc.ok === false) { mmsg(esc(fc.error || "could not build"), !fc.empty); return; }
        (fc.features || []).forEach(function (f) {
          modelBy[f.properties.dse] = f.properties;
        });
        var hues = paintBorders(fc.features || []);
        // The casing goes on first so it sits under every border, including
        // the borders of neighbours: a halo drawn per patch on top of the
        // last one would cut the previous patch's line.
        CASE = L.geoJSON(fc, { renderer: RENDER, interactive: false,
          style: function (f) { return caseStyle(f.properties, false); } });
        CASE.addTo(map);
        MODEL = L.geoJSON(fc, { renderer: RENDER,
          style: function (f) { return borderStyle(f.properties, false); } });
        MODEL.on("mouseover", function (e) {
          if (profileTarget !== "auto") return;
          var p = e.layer.feature.properties;
          var t = "<strong>" + esc(p.dse) + "</strong><br>" + num(p.outlets)
            + " outlets · " + num(p.desa) + " desa · " + num(p.sites)
            + " sites<br>" + p.area_km2 + " km²<br>"
            + "<span class='muted'>click for the full profile</span>";
          if (e.layer.getTooltip()) e.layer.setTooltipContent(t);
          else e.layer.bindTooltip(t, { sticky: true, className: "terr-tip" });
          e.layer.openTooltip();
        });
        MODEL.on("click", function (e) {
          if (profileTarget !== "auto") return;
          L.DomEvent.stop(e);
          soloBorder(e.layer.feature.properties.dse);
          drawerTerritory(e.layer.feature);
        });
        MODEL.addTo(map);
        reorder();
        dimCloud();
        drawLabels();

        var tail = [];
        if (modelMode !== "forcefit") tail.push(fc.reach + " m reach");
        if ((fc.skipped || []).length)
          tail.push(fc.skipped.length + " skipped (under 3 outlets)");
        if (fc.seam) tail.push(num(fc.seam) + " outlets on a seam");
        if (fc.overlap_pct != null) tail.push(fc.overlap_pct + "% overlap");
        mmsg("<b>" + num(fc.drawn) + "</b> of " + num(fc.groups)
             + " DSE drawn · " + fc.area_km2 + " km²"
             + (tail.length ? " · " + tail.join(" · ") : "")
             + ". Click a border for its desa breakdown.");
        legendBorders();
        sliceLedger();
      })
      .catch(function (e) {
        modelBusy = false; $("mwBuild").disabled = false; busy(false);
        mmsg("Could not build it: " + esc(String(e)), true);
      });
  }

  // Isolate one rep: their border kept, everyone else's dimmed. Clicking the
  // same one again steps back out, so it is a toggle and not a one-way door.
  function soloBorder(code) {
    if (!MODEL) return;
    modelPick = (modelPick === code) ? null : code;
    restyleBorders();
  }

  // Fold the border figures into the rail roster, so one row says
  // everything known about a rep: outlets from the file, desa and sites
  // from the territory those outlets produce.
  function legendBorders() {
    // The roster swatch has to be the colour the map drew, not the palette
    // slot the code would have had: the two stopped agreeing the moment
    // colours started being chosen by adjacency.
    var el = $("mwDseList");
    if (!el) return;
    el.querySelectorAll(".mw-drow").forEach(function (b) {
      var p = modelBy[b.dataset.dse];
      if (!p) return;
      var last = b.lastElementChild;
      last.textContent = num(p.outlets) + " · " + num(p.desa) + "d · "
        + p.area_km2 + "km²";
      if (!b.querySelector(".mw-swatch")) {
        var i = document.createElement("i");
        i.className = "mw-swatch dot";
        b.insertBefore(i, b.firstChild);
      }
      b.querySelector(".mw-swatch").style.background = borderColour(p);
    });
  }

  if ($("mwModes")) {
    $("mwModes").addEventListener("click", function (e) {
      var b = e.target.closest("button[data-mode]");
      if (!b || b.dataset.mode === modelMode) return;
      modelMode = b.dataset.mode;
      $("mwModes").querySelectorAll("button").forEach(function (x) {
        x.classList.toggle("on", x.dataset.mode === modelMode);
      });
      if (MODEL) buildBorders();
    });
  }
  if ($("mwReach")) {
    $("mwReach").addEventListener("input", function (e) {
      $("mwReachVal").textContent = e.target.value + " m";
    });
    // Built on release, not on every tick: a model is seconds of work and a
    // drag would queue a dozen of them.
    $("mwReach").addEventListener("change", function () {
      if (MODEL) buildBorders();
    });
  }
  if ($("mwBuild")) $("mwBuild").addEventListener("click", buildBorders);
  if ($("mwBorderOff")) $("mwBorderOff").addEventListener("click", function () {
    clearBorders();
    mmsg("Borders hidden. Build again to bring them back.");
  });

  /* ══════════════════════════════════════════════════════════════════════
     THE LEDGER UNDER THE MAP

     The inspector on the right is the glance: four tiles and a list, always
     there. This is the full account — every desa a border covers and by how
     much, and every outlet inside it.

     THE TWO HALVES COME FROM DIFFERENT PLACES, ON PURPOSE
     Desa geometry, area, population and masts are the application's own and
     are answered by the server. Outlet codes, names, categories and flags
     are the reader's workbook and are answered here, from the copy already
     parsed in this page. Nothing of the workbook is posted to fill this:
     the only thing sent is a polygon the server drew itself a moment ago.
     ══════════════════════════════════════════════════════════════════════ */
  var wTicket = 0, wDesa = [], wOut = [], wTab = "desa", wLabel = "area";

  function wmsg(html, bad) {
    var el = $("mwDMsg");
    if (!el) return;
    el.innerHTML = html || "";
    el.style.color = bad ? "var(--red)" : "";
  }
  function wOpen(kind, title, sub) {
    var el = $("mwDrawer");
    if (!el) return false;
    var was = el.hidden;
    el.hidden = false;
    $("mwDKind").textContent = kind;
    $("mwDTitle").textContent = title;
    $("mwDSub").textContent = sub || "";
    // The map shares its column with this, so it has to be told it shrank.
    if (was) setTimeout(function () { map.invalidateSize(); }, 0);
    return true;
  }
  function wTiles(list) {
    $("mwDStats").innerHTML = list.map(function (t) {
      return '<div class="cell' + (t.dash ? " dash" : "") + '"><span class="k">'
        + esc(t.k) + "</span><b>" + t.v + "</b></div>";
    }).join("");
  }
  function cbar(pct) {
    if (pct == null) return '<span class="mw-note">—</span>';
    var cls = pct >= 99 ? " full" : (pct < 25 ? " thin" : "");
    return '<span class="mw-cbar' + cls + '"><i style="width:'
      + Math.max(3, Math.min(100, pct)) + '%"></i><b>'
      + pct.toFixed(1) + "%</b></span>";
  }
  function km3(v) { return v == null ? "—" : Number(v).toFixed(3); }

  function wRenderDesa() {
    var tb = $("mwDDesa");
    if (!tb) return;
    if (!wDesa.length) {
      tb.innerHTML = '<div class="mw-dtr"><span>Nothing to show yet.</span></div>';
      return;
    }
    tb.innerHTML = wDesa.map(function (r) {
      return '<div class="mw-dtr body" data-desa="' + esc(r.desa)
        + '" data-kec="' + esc(r.kecamatan) + '"><span><b>' + esc(r.desa)
        + "</b></span><span>" + esc(r.kecamatan) + "</span><span>"
        + esc(r.mc || "—") + '</span><span class="n">' + km3(r.desa_km2)
        + '</span><span class="n">' + km3(r.inside_km2)
        + '</span><span class="n">' + cbar(r.pct)
        + '</span><span class="n">' + num(r.outlets || 0)
        + '</span><span class="n">' + (r.sites == null ? "—" : num(r.sites))
        + '</span><span class="n">' + (r.population ? num(r.population) : "—")
        + "</span></div>";
    }).join("");
    tb.querySelectorAll(".mw-dtr.body").forEach(function (el) {
      el.addEventListener("click", function () {
        drawerDesa(el.dataset.desa, el.dataset.kec);
      });
    });
  }

  function wRenderOut() {
    var tb = $("mwDOut");
    if (!tb) return;
    if (!wOut.length) {
      tb.innerHTML = '<div class="mw-dtr out"><span>No outlet from your '
        + "file falls here.</span></div>";
      return;
    }
    // Capped, and the cap is stated rather than silently applied: a rep with
    // 1,282 outlets would otherwise put 1,282 rows in the DOM and make this
    // the slowest thing on the page.
    var slice = wOut.slice(0, 400);
    tb.innerHTML = slice.map(function (r, i) {
      return '<div class="mw-dtr out body" data-i="' + i + '"><span>'
        + esc(r.code || "—") + "</span><span><b>" + esc(r.name || "—")
        + "</b></span><span>" + esc(r.dse) + "</span><span>"
        + esc(r.desa || "—") + "</span><span>" + esc(r.kec || "—")
        + "</span><span>" + esc(r.cat || "—") + '</span><span class="'
        + (r.ono ? "mw-flag" : "") + '">' + esc(r.rem || "—")
        + '</span><span class="n">' + r.lat.toFixed(6)
        + '</span><span class="n">' + r.lon.toFixed(6) + "</span></div>";
    }).join("");
    if (wOut.length > slice.length) {
      tb.innerHTML += '<div class="mw-dtr out"><span>Showing the first '
        + num(slice.length) + " of " + num(wOut.length)
        + " — export the CSV for all of them.</span></div>";
    }
    tb.querySelectorAll(".mw-dtr.body").forEach(function (el) {
      el.addEventListener("click", function () {
        var r = slice[Number(el.dataset.i)];
        map.setView([r.lat, r.lon], 16, { animate: false });
        pickOutlet(r);
      });
    });
  }

  function perDesaCount(rows) {
    var by = {};
    rows.forEach(function (r) {
      var k = key(r.desa, r.kec);
      by[k] = (by[k] || 0) + 1;
    });
    return by;
  }

  // Open the ledger for a rep picked from the roster rather than clicked on
  // the map. With borders built we have the polygon and can measure the desa
  // it covers; without them there is nothing to measure against, so the
  // outlet half is filled and the desa half says why it is empty.
  function drawerDse(code) {
    renderReb(code);
    var feat = null;
    if (MODEL) {
      MODEL.eachLayer(function (l) {
        if (l.feature.properties.dse === code) feat = l.feature; });
    }
    if (feat) { drawerTerritory(feat); return; }

    var n = ++wTicket;
    wLabel = code;
    if (!wOpen("DSE profile", code, "from your local file")) return;
    wOut = LOCAL ? LOCAL.rows.filter(function (r) {
      return r.dse === code && inSlice(r); }) : [];
    wRenderOut();

    /* WHICH COLUMNS ACTUALLY NEED A POLYGON, AND WHICH NEVER DID

       This panel used to dash every measured column, and for three of them
       that was simply wrong. Desa area, masts and people are facts about
       the DESA. They do not depend on where a rep's border falls, and the
       browser already holds them from area-stats. Only "covered km²" and
       "% covered" ask about the OVERLAP between a border and a desa, and
       only those two are unanswerable without a polygon.

       Dashing all six made a working panel look broken. Filling the four
       that are knowable, and dashing the two that are not, also makes the
       two remaining dashes mean something. */
    var fill = function () {
      if (n !== wTicket) return;
      var by = perDesaCount(wOut), meta = {};
      wOut.forEach(function (r) {
        var k = key(r.desa, r.kec);
        if (!meta[k]) meta[k] = { desa: r.desa || "—", kecamatan: r.kec || "",
                                  mc: r.mc || "" };
      });
      var km2 = 0, sites = 0, pop = 0, known = 0;
      wDesa = Object.keys(by).map(function (k) {
        var st = byDesaS[k] || null;
        if (st) { km2 += st.km2; sites += st.sites; pop += st.pop; known++; }
        return { desa: meta[k].desa, kecamatan: meta[k].kecamatan,
                 mc: (st && st.mc) ? st.mc : meta[k].mc,
                 desa_km2: st ? st.km2 : null,
                 inside_km2: null, pct: null,          /* need a border */
                 outlets: by[k],
                 sites: st ? st.sites : null,
                 population: st ? st.pop : 0 };
      }).sort(function (a, b) { return b.outlets - a.outlets; });
      wRenderDesa();

      var ono = wOut.filter(function (r) { return r.ono; }).length;
      wTiles([{ k: "Outlets", v: num(wOut.length), dash: true },
              { k: "Desa in file", v: num(wDesa.length), dash: true },
              { k: "ONO", v: num(ono), dash: true },
              { k: "Pairing", v: num(wOut.length - ono), dash: true },
              { k: "Desa km² total", v: known ? km3(km2) : "—" },
              { k: "Sites", v: known ? num(sites) : "—" },
              { k: "People", v: known ? num(pop) : "—" },
              { k: "% desa covered", v: "needs borders" }]);

      /* Two different situations, and telling them apart is the point: no
         borders at all is a button not yet pressed; borders that exist but
         not for THIS rep almost always means the territory filter moved
         after they were drawn. */
      var drawn = MODEL ? Object.keys(modelBy).length : 0;
      wmsg(drawn
        ? "Borders are drawn for <b>" + num(drawn) + "</b> rep"
          + (drawn === 1 ? "" : "s") + ", but not for <b>" + esc(code)
          + "</b> — the territory filter has moved since they were built. "
          + "Press <b>Build borders</b> again to include this slice, and the "
          + "covered km² and % covered fill in."
        : "No borders are drawn yet. Desa area, masts and people above are "
          + "the application's own figures for the desa this rep works. "
          + "<b>Covered km²</b> and <b>% covered</b> measure the overlap "
          + "between a border and a desa, so they need <b>Build borders</b> "
          + "in the rail.");
    };

    /* The desa figures come from the cloud table. Wait for it rather than
       rendering a row of dashes and never coming back to fill them. */
    if (STATS) fill(); else ensureStats(fill);
  }

  // ── a drawn border ────────────────────────────────────────────────────
  function drawerTerritory(feat) {
    var p = feat.properties || {}, code = p.dse, n = ++wTicket;
    wLabel = code;
    renderReb(code);
    if (!wOpen("Area profile", code,
               modelMode + " border · " + p.area_km2 + " km²")) return;

    wOut = LOCAL ? LOCAL.rows.filter(function (r) {
      return r.dse === code && inSlice(r); }) : [];
    wRenderOut();
    wDesa = []; wRenderDesa();
    wTiles([{ k: "Outlets", v: num(wOut.length), dash: true },
            { k: "Desa", v: num(p.desa || 0) },
            { k: "Sites", v: num(p.sites || 0) },
            { k: "km² area", v: p.area_km2 },
            { k: "% desa covered", v: "…" },
            { k: "Whole desa", v: "…" }]);
    wmsg("Measuring how much of each desa this border covers…");

    fetch("/api/samples/area-profile", {
      method: "POST", headers: { "Content-Type": "application/json" },
      // The polygon only. Not one column of the workbook goes with it.
      body: JSON.stringify({ geometry: feat.geometry, dse: code,
                             mode: modelMode })
    }).then(function (r) { return r.json(); })
      .then(function (d) {
        // A later click already owns the ledger: drop this answer rather
        // than overwriting what the reader is now looking at.
        if (n !== wTicket) return;
        // A refused request used to leave the table reading "Nothing to
        // show yet", which is indistinguishable from a border that covers
        // no desa. Say what happened instead.
        if (d.ok === false) {
          wDesa = [];
          $("mwDDesa").innerHTML = '<div class="mw-dtr"><span>'
            + esc(d.error || "The desa breakdown could not be measured.")
            + "</span></div>";
          wmsg(esc(d.error || "could not measure"), true);
          return;
        }
        var by = perDesaCount(wOut);
        wDesa = (d.desa || []).map(function (r) {
          r.outlets = by[key(r.desa, r.kecamatan)] || 0;
          return r;
        });
        wRenderDesa();
        wTiles([{ k: "Outlets", v: num(wOut.length), dash: true },
                { k: "Desa covered", v: num(d.desa_count) },
                { k: "Sites", v: num(d.sites) },
                { k: "km² area", v: km3(d.area_km2) },
                { k: "% desa covered",
                  v: d.coverage_pct == null ? "—" : d.coverage_pct + "%" },
                { k: "Whole desa",
                  v: num(d.desa_full) + " of " + num(d.desa_count) },
                { k: "People", v: num(d.population) }]);
        wmsg("<b>" + num(d.desa_full) + "</b> desa held whole, <b>"
          + num(d.desa_partial) + "</b> in part. Coverage is measured on a "
          + d.grid + "×" + d.grid + " lattice inside each desa — good to "
          + "about a percent, not an exact clip. Outlet rows are your file; "
          + "desa, area, people and sites are the application's own.");
      })
      .catch(function (e) {
        if (n !== wTicket) return;
        wmsg("Could not measure that border: " + esc(String(e)), true);
      });
  }

  // ── one desa ──────────────────────────────────────────────────────────
  function drawerDesa(desa, kec) {
    renderReb(null);
    var n = ++wTicket;
    wLabel = desa;
    if (!wOpen("Desa profile", desa, kec || "")) return;
    var k = key(desa, kec);
    wOut = LOCAL ? LOCAL.rows.filter(function (r) {
      return key(r.desa, r.kec) === k || (!kec && norm(r.desa) === norm(desa));
    }) : [];
    wRenderOut();
    var reps = {};
    wOut.forEach(function (r) { reps[r.dse] = (reps[r.dse] || 0) + 1; });
    var repN = Object.keys(reps).length;
    wDesa = []; wRenderDesa();
    wTiles([{ k: "Outlets", v: num(wOut.length), dash: true },
            { k: "DSE here", v: num(repN), dash: true },
            { k: "Sites", v: "…" }, { k: "km² area", v: "…" },
            { k: "People", v: "…" }]);
    wmsg("Reading this desa from the application's own layer…");

    fetch("/api/samples/desa-profile?desa=" + encodeURIComponent(desa)
          + "&kecamatan=" + encodeURIComponent(kec || ""))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (n !== wTicket) return;
        if (d.ok === false) { wmsg(esc(d.error || "not found"), true); return; }
        $("mwDSub").textContent = [d.kecamatan, d.kabupaten, d.mc]
          .filter(Boolean).join(" · ");
        // A desa is one row at 100% of itself: the same table shape as a
        // border's, so the two never need reading differently.
        wDesa = [{ desa: d.desa, kecamatan: d.kecamatan, mc: d.mc,
                   desa_km2: d.desa_km2, inside_km2: d.desa_km2, pct: 100.0,
                   outlets: wOut.length, sites: d.sites,
                   population: d.population }];
        wRenderDesa();
        wTiles([{ k: "Outlets", v: num(wOut.length), dash: true },
                { k: "DSE here", v: num(repN), dash: true },
                { k: "Sites", v: num(d.sites) },
                { k: "km² area", v: km3(d.desa_km2) },
                { k: "People", v: num(d.population) },
                { k: "Outlets / km²", v: d.desa_km2
                    ? (wOut.length / d.desa_km2).toFixed(1) : "—", dash: true }]);
        var top = Object.keys(reps).sort(function (a, b) {
          return reps[b] - reps[a]; }).slice(0, 6);
        wmsg(top.length
          ? "Worked by " + top.map(function (c) {
              return "<b>" + esc(c) + "</b> " + num(reps[c]); }).join(" · ")
            + (repN > top.length ? " and " + (repN - top.length) + " more" : "")
            + "."
          : "No outlet from your file is filed in this desa.");
      })
      .catch(function (e) {
        if (n !== wTicket) return;
        wmsg("Could not read that desa: " + esc(String(e)), true);
      });
  }

  // ── a kecamatan, microcluster or kota ─────────────────────────────────
  // Answered from the workbook alone, because the question a reader asks of
  // a coarse polygon is "who of mine is in here", and the desa under it are
  // already named in the file.
  function drawerCoarse(feat, def) {
    renderReb(null);
    var p = feat.properties || {}, name = featName(p) || def.label;
    ++wTicket;
    wLabel = name;
    if (!wOpen(def.label + " profile", name, "from your local file")) return;
    wOut = LOCAL ? rowsFor(feat) : [];
    wRenderOut();
    var by = {}, meta = {}, reps = {};
    wOut.forEach(function (r) {
      reps[r.dse] = 1;
      var k = key(r.desa, r.kec);
      by[k] = (by[k] || 0) + 1;
      if (!meta[k]) meta[k] = { desa: r.desa || "—", kecamatan: r.kec || "",
                                mc: r.mc || "" };
    });
    wDesa = Object.keys(by).map(function (k) {
      return { desa: meta[k].desa, kecamatan: meta[k].kecamatan,
               mc: meta[k].mc, desa_km2: null, inside_km2: null, pct: null,
               outlets: by[k], sites: null, population: 0 };
    }).sort(function (a, b) { return b.outlets - a.outlets; });
    wRenderDesa();
    wTiles([{ k: "Outlets", v: num(wOut.length), dash: true },
            { k: "Desa in file", v: num(wDesa.length), dash: true },
            { k: "DSE here", v: num(Object.keys(reps).length), dash: true },
            { k: "People", v: featPop(p) != null ? num(featPop(p)) : "—" }]);
    wmsg(wOut.length
      ? "Every figure here is from your file. Click a desa row for its area, "
        + "people and masts from the application's own layers."
      : "No outlet from your file is filed under this "
        + esc(def.label.toLowerCase()) + ".");
  }

  // The whole slice at once — what the ledger shows the moment borders are
  // built, so the panel is on screen explaining itself rather than waiting
  // hidden for somebody to guess that clicking a polygon opens it.
  function sliceLedger() {
    renderReb(null);
    if (!LOCAL) return;
    ++wTicket;
    wLabel = terrLabel || "sample";
    var rows = LOCAL.rows.filter(inSlice);
    var reps = {};
    rows.forEach(function (r) { reps[r.dse] = 1; });
    if (!wOpen("Slice profile", terrLabel || (LOCAL.filename || "This file"),
               num(rows.length) + " outlets · "
               + num(Object.keys(reps).length) + " DSE")) return;
    wOut = rows;
    wRenderOut();
    var by = perDesaCount(rows), meta = {};
    rows.forEach(function (r) {
      var k = key(r.desa, r.kec);
      if (!meta[k]) meta[k] = { desa: r.desa || "—", kecamatan: r.kec || "",
                                mc: r.mc || "" };
    });
    wDesa = Object.keys(by).map(function (k) {
      return { desa: meta[k].desa, kecamatan: meta[k].kecamatan,
               mc: meta[k].mc, desa_km2: null, inside_km2: null, pct: null,
               outlets: by[k], sites: null, population: 0 };
    }).sort(function (a, b) { return b.outlets - a.outlets; });
    wRenderDesa();
    wTiles([{ k: "Outlets", v: num(rows.length), dash: true },
            { k: "DSE", v: num(Object.keys(reps).length), dash: true },
            { k: "Desa in file", v: num(wDesa.length), dash: true },
            { k: "Borders drawn", v: num(Object.keys(modelBy).length) }]);
    wmsg("Click a <b>border</b> on the map, or a <b>DSE</b> in the rail, for "
         + "its desa breakdown and the share of each desa it covers. Click a "
         + "<b>desa</b> row below, or a desa on the map, for that desa's own "
         + "area, people and masts.");
  }

  // ── tabs, export, close ───────────────────────────────────────────────

  /* ══════════════════════════════════════════════════════════════════════
     PJP RATIONALISATION — WHICH OUTLETS ARE OUT OF BOUNDARIES

     A rep with 56 outlets and one of them four kilometres south is not
     carrying 56 outlets. They are carrying 55 and a separate trip. That one
     detached stop can cost more of a visit day than a dozen clustered ones,
     which is why it is worth naming rather than leaving to be noticed on
     the map.

     THE MEASURE IS THE GAP TO THE NEAREST OWN OUTLET
     Not the distance to the rep's centre. A round is a chain of stops: what
     costs the day is the gap you have to cross to reach a stop, not how far
     it sits from an average. An outlet 200 m from a sibling is free even if
     the patch is wide; one 4 km from any sibling is a detour whatever the
     average says.

     THE ACCEPTABLE GAP DEPENDS ON THE GROUND
     Urban  1.5 km.  Rural  4 km.  Beyond it the outlet is flagged
     "out of boundaries" and a nearby rep is named to bind it to.

     WHERE URBAN AND RURAL COME FROM
     ref_kelurahan.geo_type is empty in this database -- every row is NULL --
     so it cannot be the source. Population and area are complete for all
     7,761 desa and are already in the browser (area-stats sends km² and
     people per desa), so the stratum is the desa's own population density,
     cut at 1,500 people per km². That is the Degree-of-Urbanisation
     threshold for an urban centre, and it also happens to be where this
     circle's own outlet spacing changes character: below it the 95th
     percentile gap is 1.7 km, above it 1.0 km.

     CALIBRATION AGAINST THE AUGUST FILE (73,661 outlets, 1,501 DSE codes)
       desa density        median gap   p95     over 1.5 km   over 4 km
       under 500/km²         0.14 km   2.79 km     12.0%        2.5%
       500 - 1,500           0.13 km   1.68 km      6.0%        1.1%
       1,500 - 5,000         0.13 km   1.01 km      2.6%        0.6%
       5,000 and over        0.08 km   0.52 km      0.9%        0.2%
     So 1.5 km urban and 4 km rural flag on the order of 1.5% of outlets --
     a review list, not a flood.

     WHY THE RECEIVER IS NOT SIMPLY THE NEAREST REP
     Handing every stray to whoever is closest just moves the overload
     around. A receiver under the review threshold is preferred, the count
     is tracked as suggestions are made so ten strays near one rep do not
     all pile onto them, and a rep whose own border the outlet is standing
     in wins ties -- the territory model already put that ground in their
     hands.

     NOTHING HERE IS APPLIED. These are recommendations against a file held
     in this browser. The binding is executed elsewhere, by a person.
     ══════════════════════════════════════════════════════════════════════ */
  var REB_N = 50;                 // a rep over this many outlets is reviewed
  var REB_URBAN_KM = 1.5;         // acceptable gap to the nearest own outlet
  var REB_RURAL_KM = 4.0;
  var REB_DENSE = 1500;           // people/km² at or above which a desa is urban
  var REB_FLAG = "out of boundaries";
  var REB_NEAR = 20;              // km — how far to look for a receiving rep

  /* BEYOND THIS IT IS NOT A STRAY, IT IS A BAD COORDINATE.
     The circle spans about 350 km from Pandeglang to Cirebon. An outlet
     352 km from its own nearest sibling is not a demarcation problem that
     a supervisor can fix by moving it to another rep -- it is a latitude
     with a sign error or a decimal in the wrong place, and one of them is
     the row already known to carry latitude -67.
     Left in the main count they would top the table, and a summary whose
     three worst rows are obvious rubbish is a summary nobody trusts. So
     they are counted and exported separately, under their own flag. */
  var REB_BAD_KM = 50;
  var REB_BAD_FLAG = "check the coordinate";
  var rebRows = [], rebLayer = null, rebCode = null;
  var rebTargetLayer = null;   // the polygon a recommendation points at

  function rkm(aLat, aLon, bLat, bLon) {
    var R = 6371.0088;
    var p1 = aLat * Math.PI / 180, p2 = bLat * Math.PI / 180;
    var dp = (bLat - aLat) * Math.PI / 180;
    var dl = (bLon - aLon) * Math.PI / 180;
    var h = Math.sin(dp / 2) * Math.sin(dp / 2)
          + Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2) * Math.sin(dl / 2);
    return 2 * R * Math.asin(Math.sqrt(h));
  }

  // Nearest outlet of a given rep, and how far. Linear, but it is only ever
  // run for an outlet that has already been flagged, against reps whose
  // bounding box is within reach, so it costs nothing worth indexing away.
  function nearestOf(rows, lat, lon, skipRow) {
    var best = null, bd = Infinity;
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      if (r === skipRow) continue;
      var d = rkm(lat, lon, r.lat, r.lon);
      if (d < bd) { bd = d; best = r; }
    }
    return { row: best, km: bd };
  }

  /* Urban or rural, from the desa the outlet is filed under. When the desa
     is not in the cloud table at all the answer is the generous one: a
     4 km allowance flags fewer outlets, and a flag we cannot justify is
     worse than a flag we did not raise. */
  function rebStratum(r) {
    var s = byDesaS[key(r.desa, r.kec)];
    if (!s || !s.km2 || !s.pop) {
      return { s: "rural", den: null, lim: REB_RURAL_KM, known: false };
    }
    var den = s.pop / s.km2;
    return den >= REB_DENSE
      ? { s: "urban", den: den, lim: REB_URBAN_KM, known: true }
      : { s: "rural", den: den, lim: REB_RURAL_KM, known: true };
  }

  function rebBox(rows) {
    var b = null;
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      b = b ? [Math.min(b[0], r.lon), Math.min(b[1], r.lat),
               Math.max(b[2], r.lon), Math.max(b[3], r.lat)]
            : [r.lon, r.lat, r.lon, r.lat];
    }
    return b;
  }

  function rebScan(code) {
    if (!LOCAL) return [];
    var all = LOCAL.rows.filter(inSlice);
    var byDse = {};
    all.forEach(function (r) { (byDse[r.dse] = byDse[r.dse] || []).push(r); });
    var mine = byDse[code] || [];
    if (mine.length < 2) return [];

    var boxes = {};
    Object.keys(byDse).forEach(function (e) { boxes[e] = rebBox(byDse[e]); });

    var taken = {}, out = [];
    var dLat = REB_NEAR / 111.32;

    mine.forEach(function (o) {
      var self = nearestOf(mine, o.lat, o.lon, o);
      if (!self.row) return;

      var st = rebStratum(o);
      var far = self.km > st.lim;

      // Whose ground is it standing on? Only meaningful once borders exist.
      var insideCode = null;
      if (MODEL) {
        var f = findAt("border", { lat: o.lat, lng: o.lon });
        if (f && f.properties && f.properties.dse !== code) {
          insideCode = f.properties.dse;
        }
      }
      if (!far && !insideCode) return;

      // ── a receiver ──────────────────────────────────────────────────
      var dLon = REB_NEAR / (111.32 * Math.max(0.2, Math.cos(o.lat * Math.PI / 180)));
      var best = null, bestScore = Infinity;
      Object.keys(byDse).forEach(function (e) {
        if (e === code) return;
        var b = boxes[e];
        if (!b || o.lon < b[0] - dLon || o.lon > b[2] + dLon
               || o.lat < b[1] - dLat || o.lat > b[3] + dLat) return;
        var n = nearestOf(byDse[e], o.lat, o.lon, null);
        if (!n.row) return;
        var load = byDse[e].length + (taken[e] || 0);
        var score = n.km;
        if (e === insideCode) score *= 0.5;   // the model gave them this ground
        if (load >= REB_N) score += 100;      // full: only if nothing else is near
        if (score < bestScore) {
          bestScore = score;
          best = { dse: e, km: n.km, load: load, row: n.row };
        }
      });
      // A move that lands the outlet further from its new round than it was
      // from its old one is not a recommendation, it is a shuffle.
      if (!best || best.km >= self.km) {
        out.push({ row: o, code: o.code, name: o.name, desa: o.desa,
                   kec: o.kec, from: code, to: null, toRow: null,
                   kmOwn: self.km, kmNew: null, gain: null,
                   strat: st.s, den: st.den, known: st.known, lim: st.lim,
                   far: far, inside: !!insideCode, insideCode: insideCode,
                   flag: far ? REB_FLAG : "inside another border",
                   toLoad: null, full: false });
        return;
      }

      taken[best.dse] = (taken[best.dse] || 0) + 1;
      out.push({ row: o, code: o.code, name: o.name, desa: o.desa,
                 kec: o.kec, from: code, to: best.dse, toRow: best.row,
                 kmOwn: self.km, kmNew: best.km, gain: self.km - best.km,
                 strat: st.s, den: st.den, known: st.known, lim: st.lim,
                 far: far, inside: !!insideCode, insideCode: insideCode,
                 flag: far ? REB_FLAG : "inside another border",
                 toLoad: best.load + 1, full: best.load >= REB_N });
    });

    // Out of boundaries first, and inside those the worst gap first: the
    // top of this table should be the trip that costs the most.
    out.sort(function (a, b) {
      return (b.far - a.far) || (b.kmOwn - a.kmOwn);
    });
    return out;
  }

  /* On the map: an amber ring round every flagged outlet and a hairline to
     the rep it is recommended to. The line is what makes a recommendation
     legible -- a ring on its own says something is wrong, a line says where
     it should go. */
  function drawReb() {
    if (rebLayer) { map.removeLayer(rebLayer); rebLayer = null; }
    clearRebTarget();
    if (!rebRows.length) return;
    rebLayer = L.layerGroup([]);
    rebRows.forEach(function (s) {
      if (s.toRow) {
        rebLayer.addLayer(L.polyline(
          [[s.row.lat, s.row.lon], [s.toRow.lat, s.toRow.lon]],
          { renderer: RENDER, color: "#e08a00", weight: 1.4, opacity: .85,
            dashArray: "4,4", interactive: false }));
      }
      rebLayer.addLayer(L.circleMarker([s.row.lat, s.row.lon], {
        renderer: RENDER, radius: 9,
        color: s.far ? "#e08a00" : "#7a7a7a", weight: 2.4,
        fill: false, interactive: false }));
    });
    rebLayer.addTo(map);
    reorder();
  }

  /* SHOW ME THE POLYGON YOU ARE RECOMMENDING

     A row that says "move this to 2613051015" is asking the reader to take
     a demarcation decision on the strength of a code. Drawing the receiving
     border is the difference between reading a recommendation and seeing
     one: the flagged outlet, the patch it would join, and the gap between
     them, in one view.

     The donor's own border stays selected underneath, so what is on screen
     is the before and the after together rather than the after alone. */
  function clearRebTarget() {
    if (rebTargetLayer) { map.removeLayer(rebTargetLayer); rebTargetLayer = null; }
  }

  function showRebTarget(s) {
    clearRebTarget();
    if (!s || !s.to) return null;
    var feat = null;
    if (MODEL) {
      MODEL.eachLayer(function (l) {
        if (l.feature && l.feature.properties
            && l.feature.properties.dse === s.to) feat = l.feature;
      });
    }
    if (!feat) return null;
    rebTargetLayer = L.geoJSON(feat, {
      renderer: RENDER,
      style: { color: "#e08a00", weight: 3.2, opacity: 1,
               fillColor: "#e08a00", fillOpacity: .16,
               interactive: false }
    });
    rebTargetLayer.addTo(map);
    reorder();
    return rebTargetLayer.getBounds();
  }

  /* ══════════════════════════════════════════════════════════════════════
     THE SAME RULE, ACROSS THE WHOLE FILE

     The PJP review tab answers "which of THIS rep's outlets are out of
     boundaries". This answers it for every rep at once, which is a
     different question for a different reader: a supervisor deciding where
     to spend a demarcation review, rather than a person looking at one
     round.

     Same thresholds, same measure, same flag text. If these two ever
     disagree the summary is worthless, so they share rebStratum() and
     nearestOf() rather than each having their own copy of the rule.

     IT YIELDS BETWEEN REPS
     73,661 outlets across 1,501 codes is a few million distance
     calculations. Done in one go the tab locks up and the browser offers to
     kill the page; done forty reps at a time with a setTimeout between, the
     progress line moves and the page stays alive.
     ══════════════════════════════════════════════════════════════════════ */
  var BOUNDS = null;          // the last completed scan
  var boundsBusy = false;

  function rebScanAll(onDone, onTick) {
    if (!LOCAL) { onDone(null); return; }
    var all = LOCAL.rows.filter(inSlice);
    var byDse = {};
    all.forEach(function (r) { (byDse[r.dse] = byDse[r.dse] || []).push(r); });
    var codes = Object.keys(byDse);
    var boxes = {};
    codes.forEach(function (e) { boxes[e] = rebBox(byDse[e]); });

    // The border features once, not once per outlet: findAt() rebuilds its
    // array from the layer on every call, and calling it twelve hundred
    // times is the difference between a second and a minute.
    var bfeat = [];
    if (MODEL) {
      MODEL.eachLayer(function (l) { if (l.feature) bfeat.push(l.feature); });
    }
    function insideOther(o, code) {
      for (var i = 0; i < bfeat.length; i++) {
        var f = bfeat[i], p = f.properties || {};
        if (p.dse === code) continue;
        var b = bbOf(f);
        if (!b || o.lon < b[0] || o.lon > b[2]
               || o.lat < b[1] || o.lat > b[3]) continue;
        if (featHas(f, o.lon, o.lat)) return p.dse;
      }
      return null;
    }

    var kx = kecIndex();
    var t = { outlets: all.length, reps: codes.length, repsOver: 0,
              urban: 0, rural: 0, unknown: 0,
              flagUrban: 0, flagRural: 0, repsFlagged: {},
              noReceiver: 0, kmSaved: 0, bad: 0, borders: bfeat.length };
    all.forEach(function (r) {
      var st = rebStratum(r);
      if (!st.known) t.unknown++;
      if (st.s === "urban") t.urban++; else t.rural++;
    });

    var taken = {}, out = [], i = 0;
    var dLat = REB_NEAR / 111.32;

    function step() {
      var end = Math.min(i + 40, codes.length);
      for (; i < end; i++) {
        var code = codes[i], mine = byDse[code];
        if (mine.length > REB_N) t.repsOver++;
        if (mine.length < 2) continue;
        for (var j = 0; j < mine.length; j++) {
          var o = mine[j];
          var st = rebStratum(o);
          var self = nearestOf(mine, o.lat, o.lon, o);
          if (!self.row || self.km <= st.lim) continue;

          // A bad coordinate needs a data fix, not a demarcation change, so
          // it is set aside before any receiver is looked for.
          if (self.km > REB_BAD_KM) {
            t.bad++;
            var gb = kx[norm(o.kec)] || {};
            out.push({ code: o.code, name: o.name, brand: o.brand || "",
              dse: code, unik: o.unik || "", spv: o.spv || "",
              desa: o.desa || "", kec: o.kec || "", kab: o.kab || "",
              mc: o.mc || "", region: gb.region || "", area: gb.area || "",
              branch: gb.branch || "", terr: gb.terr || "",
              stratum: st.s, density: st.den, known: st.known, lim: st.lim,
              kmOwn: self.km, flag: REB_BAD_FLAG, bad: true,
              to: "", kmNew: null, toLoad: null, inside: "",
              lat: o.lat, lon: o.lon, row: o });
            continue;
          }

          var insideCode = bfeat.length ? insideOther(o, code) : null;
          var dLon = REB_NEAR / (111.32 * Math.max(0.2,
                       Math.cos(o.lat * Math.PI / 180)));
          var best = null, bestScore = Infinity;
          for (var k = 0; k < codes.length; k++) {
            var e = codes[k];
            if (e === code) continue;
            var b = boxes[e];
            if (!b || o.lon < b[0] - dLon || o.lon > b[2] + dLon
                   || o.lat < b[1] - dLat || o.lat > b[3] + dLat) continue;
            var n = nearestOf(byDse[e], o.lat, o.lon, null);
            if (!n.row) continue;
            var load = byDse[e].length + (taken[e] || 0);
            var sc = n.km;
            if (e === insideCode) sc *= 0.5;
            if (load >= REB_N) sc += 100;
            if (sc < bestScore) {
              bestScore = sc;
              best = { dse: e, km: n.km, load: load };
            }
          }
          if (best && best.km >= self.km) best = null;
          if (!best) t.noReceiver++;
          else {
            taken[best.dse] = (taken[best.dse] || 0) + 1;
            t.kmSaved += self.km - best.km;
          }

          var g = kx[norm(o.kec)] || {};
          if (st.s === "urban") t.flagUrban++; else t.flagRural++;
          t.repsFlagged[code] = 1;
          out.push({
            code: o.code, name: o.name, brand: o.brand || "",
            dse: code, unik: o.unik || "", spv: o.spv || "",
            desa: o.desa || "", kec: o.kec || "", kab: o.kab || "",
            mc: o.mc || "", region: g.region || "", area: g.area || "",
            branch: g.branch || "", terr: g.terr || "",
            stratum: st.s, density: st.den, known: st.known, lim: st.lim,
            kmOwn: self.km, flag: REB_FLAG,
            to: best ? best.dse : "", kmNew: best ? best.km : null,
            toLoad: best ? best.load + 1 : null,
            inside: insideCode || "",
            lat: o.lat, lon: o.lon, row: o
          });
        }
      }
      if (onTick) onTick(i, codes.length);
      if (i < codes.length) { setTimeout(step, 0); return; }
      // Real findings first, worst gap at the top; bad coordinates last,
      // because they are somebody else's job.
      out.sort(function (a, b) {
        return (a.bad ? 1 : 0) - (b.bad ? 1 : 0) || b.kmOwn - a.kmOwn;
      });
      t.repsFlagged = Object.keys(t.repsFlagged).length;
      onDone({ rows: out, totals: t });
    }
    step();
  }

  function rebNote(html) {
    var tb = $("mwDReb");
    if (tb) tb.innerHTML = '<div class="mw-dtr reb"><span>' + html + "</span></div>";
  }

  function rebHead(html) {
    var el = $("mwRebSum");
    if (el) el.innerHTML = html || "";
  }

  function renderReb(code) {
    rebCode = code || null;
    var tb = $("mwDReb");
    if (!tb) return;
    if (!code || !LOCAL) {
      rebRows = []; drawReb(); rebHead("");
      rebNote("Click a <b>DSE border</b> on the map, or a rep in the rail, "
              + "to review that round.");
      return;
    }
    // The stratum needs the cloud's area and population table. Without it
    // every desa would read rural and the urban rule would never fire, so
    // the table waits for it rather than quietly using the wrong radius.
    if (!STATS) {
      rebHead("");
      rebNote("Reading desa area and population…");
      ensureStats(function () { if (rebCode === code) renderReb(code); });
      return;
    }

    var mine = LOCAL.rows.filter(function (r) {
      return r.dse === code && inSlice(r); });
    rebRows = rebScan(code);
    drawReb();

    var flagged = rebRows.filter(function (s) { return s.far; }).length;
    var over = mine.length > REB_N;
    rebHead("<b>" + esc(code) + "</b> carries " + num(mine.length)
      + " outlets — " + (over
          ? "over the " + num(REB_N) + " this reviews"
          : "at or under the " + num(REB_N) + " this reviews")
      + " · <b class='mw-gain'>" + num(flagged) + "</b> "
      + REB_FLAG + (rebRows.length > flagged
          ? " · " + num(rebRows.length - flagged)
            + " standing in another rep's border" : ""));

    if (!rebRows.length) {
      rebNote(num(mine.length) + " outlets and every one of them within "
        + "reach of another stop on the same round. Nothing is "
        + REB_FLAG + ".");
      return;
    }

    tb.innerHTML = rebRows.map(function (s, i) {
      var lim = s.lim.toFixed(1) + " km";
      var area = "<b>" + s.strat + "</b>"
        + (s.known
            ? " <span class='mw-why'>" + num(Math.round(s.den))
              + "/km² · " + lim + "</span>"
            : " <span class='mw-why'>desa not in cloud table · "
              + lim + "</span>");
      var to = s.to
        ? "<b>" + esc(s.to) + "</b>"
          + (s.inside && s.insideCode === s.to
              ? " <span class='mw-why'>its border</span>" : "")
          + (s.full ? " <span class='mw-flag'>at capacity</span>" : "")
        : "<span class='mw-why'>no nearer rep within " + REB_NEAR
          + " km</span>";
      return '<div class="mw-dtr reb body" data-i="' + i + '"><span>'
        + esc(s.code || "—") + "</span><span><b>" + esc(s.name || "—")
        + "</b></span><span>" + esc(s.desa || "—")
        + "</span><span>" + area
        + '</span><span class="n"><b>' + s.kmOwn.toFixed(2) + "</b></span>"
        + '<span class="' + (s.far ? "mw-flag" : "mw-why") + '">'
        + esc(s.flag) + "</span><span>" + to
        + '</span><span class="n">'
        + (s.kmNew == null ? "—" : s.kmNew.toFixed(2))
        + '</span><span class="n">'
        + (s.toLoad == null ? "—" : num(s.toLoad)) + "</span></div>";
    }).join("");

    tb.querySelectorAll(".mw-dtr.body").forEach(function (el) {
      el.addEventListener("click", function () {
        var s = rebRows[Number(el.dataset.i)];
        if (!s) return;
        tb.querySelectorAll(".mw-dtr.body").forEach(function (x) {
          x.classList.toggle("sel", x === el); });

        var b = showRebTarget(s);
        // Frame the outlet, the round it would join, and the border it
        // would join -- whichever of the three exist.
        var pts = [[s.row.lat, s.row.lon]];
        if (s.toRow) pts.push([s.toRow.lat, s.toRow.lon]);
        var box = L.latLngBounds(pts);
        if (b) box.extend(b);
        if (pts.length > 1 || b) {
          map.fitBounds(box.pad(0.25), { animate: false });
        } else {
          map.setView([s.row.lat, s.row.lon], 15, { animate: false });
        }
        pickOutlet(s.row);

        if (s.to && !b) {
          wmsg("Borders are not drawn, so <b>" + esc(s.to) + "</b> has no "
             + "polygon to show. Build them in the rail and click the row "
             + "again to see the patch this outlet would join.", true);
        } else if (b) {
          wmsg("<b>" + esc(s.to) + "</b> is outlined on the map — the patch "
             + "this outlet would join. Nothing has been changed: the "
             + "binding is executed elsewhere.");
        }
      });
    });
  }

  if ($("mwRebN")) {
    $("mwRebN").addEventListener("input", function () {
      REB_N = Number(this.value) || 50;
      var v = $("mwRebVal");
      if (v) v.textContent = REB_N;
    });
    $("mwRebN").addEventListener("change", function () {
      if (rebCode) renderReb(rebCode);
    });
  }

  if ($("mwDTabs")) {
    $("mwDTabs").addEventListener("click", function (e) {
      var b = e.target.closest("button[data-dtab]");
      if (!b) return;
      wTab = b.dataset.dtab;
      $("mwDTabs").querySelectorAll("button").forEach(function (x) {
        x.classList.toggle("on", x.dataset.dtab === wTab);
      });
      document.querySelectorAll(".mw [data-dpane]").forEach(function (x) {
        x.hidden = x.dataset.dpane !== wTab;
      });
    });
  }
  if ($("mwDClose")) {
    $("mwDClose").addEventListener("click", function () {
      ++wTicket;
      renderReb(null);
      $("mwDrawer").hidden = true;
      setTimeout(function () { map.invalidateSize(); }, 0);
    });
  }
  if ($("mwDCsv")) {
    $("mwDCsv").addEventListener("click", function () {
      function q(v) {
        var s = String(v == null ? "" : v);
        return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
      }
      var head, body;
      if (wTab === "desa") {
        head = ["Desa", "Kecamatan", "Microcluster", "Desa km2",
                "Covered km2", "% covered", "Outlets", "Sites", "Population"];
        body = wDesa.map(function (r) {
          return [r.desa, r.kecamatan, r.mc, r.desa_km2, r.inside_km2, r.pct,
                  r.outlets, r.sites, r.population].map(q).join(",");
        });
      } else if (wTab === "rebal") {
        head = ["Outlet", "Name", "Desa", "Kecamatan", "Area",
                "People per km2", "Allowed km", "Km to nearest own outlet",
                "Flag", "Recommend DSE", "Km to that DSE",
                "Outlets after move", "Latitude", "Longitude"];
        body = rebRows.map(function (s) {
          return [s.code, s.name, s.desa, s.kec, s.strat,
                  s.den == null ? "" : Math.round(s.den), s.lim,
                  s.kmOwn.toFixed(3), s.flag, s.to || "",
                  s.kmNew == null ? "" : s.kmNew.toFixed(3),
                  s.toLoad == null ? "" : s.toLoad,
                  s.row.lat, s.row.lon].map(q).join(",");
        });
      } else {
        head = ["Outlet", "Name", "DSE", "Desa", "Kecamatan", "Category",
                "Flag", "Latitude", "Longitude"];
        body = wOut.map(function (r) {
          return [r.code, r.name, r.dse, r.desa, r.kec, r.cat, r.rem,
                  r.lat, r.lon].map(q).join(",");
        });
      }
      if (!body.length) { wmsg("There is nothing in that tab to export.", true); return; }
      var url = URL.createObjectURL(new Blob(
        ["﻿" + head.join(",") + "\n" + body.join("\n")],
        { type: "text/csv;charset=utf-8" }));
      var a = document.createElement("a");
      a.href = url;
      a.download = (String(wLabel).replace(/[^A-Za-z0-9_-]+/g, "-") || "area")
        + "-" + wTab + ".csv";
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    });
  }


  /* ══════════════════════════════════════════════════════════════════════
     HOW MANY DESA IS ONE REP CARRYING?

     The question the whole exercise is about. Every DSE in the loaded file
     is counted by the number of DISTINCT desa their outlets fall in, then
     dropped into a slab. Read down a column to find the reps holding a
     single desa; read across a row to see the shape of a branch.

     THE SLABS DO NOT OVERLAP
     The brief named them as "3", then "3–5", then "5–7", then "7–10", which
     puts 3, 5 and 7 in two buckets each and would make the row totals
     disagree with the DSE count beside them. They are cut here so every
     whole number lands in exactly one place, and labelled so the cut is
     visible rather than assumed.

     WHICH BRANCH IS A REP IN?
     A rep's outlets can straddle two kecamatan and therefore two branches.
     Rather than count them twice — which would make the region rows add up
     to more than the circle — each rep is filed where MOST of their outlets
     are. The reps that straddle are worth knowing about, so the count of
     them is reported under the table instead of being hidden by the rule.

     The hierarchy comes from the cloud, the reps and their desa come from
     the file, and neither half is written anywhere.
     ══════════════════════════════════════════════════════════════════════ */
  var SLABS = [
    { k: "1",     lo: 1,  hi: 1 },
    { k: "2",     lo: 2,  hi: 2 },
    { k: "3",     lo: 3,  hi: 3 },
    { k: "4–5",   lo: 4,  hi: 5 },
    { k: "6–7",   lo: 6,  hi: 7 },
    { k: "8–10",  lo: 8,  hi: 10 },
    { k: ">10",   lo: 11, hi: Infinity }
  ];
  var LEVELS = { circle: "Jaya circle", region: "Region",
                 area: "Area", branch: "Branch" };
  var slabLevel = "circle", REPS = [], slabStraddle = 0, slabBrand = "";

  function slabOf(n) {
    for (var i = 0; i < SLABS.length; i++)
      if (n >= SLABS[i].lo && n <= SLABS[i].hi) return i;
    return -1;
  }

  // kecamatan -> where it sits in the organisation, built once from the tree.
  function kecIndex() {
    var by = {};
    ROWS.forEach(function (r) {
      r.kecs.forEach(function (k) {
        if (!by[norm(k)]) by[norm(k)] = { region: r.region, area: r.area,
                                          branch: r.branch, terr: r.terr };
      });
    });
    return by;
  }

  // One row per rep: their desa count, their outlets, and where they sit.
  function buildReps() {
    REPS = []; slabStraddle = 0;
    if (!LOCAL || !ROWS.length) return;
    var by = kecIndex(), per = {};
    LOCAL.rows.forEach(function (r) {
      if (slabBrand && r.brand !== slabBrand) return;
      // Keyed on the person. A rep carrying both brands is one rep with one
      // patch, not two reps with half a patch each.
      var k = repKey(r);
      var d = per[k] || (per[k] = { dse: k, codes: {}, brands: {}, outlets: 0,
                                    desa: {}, kec: {}, mc: {} });
      d.codes[r.dse] = 1;
      if (r.brand) d.brands[r.brand] = 1;
      d.outlets++;
      d.desa[key(r.desa, r.kec)] = 1;
      var k = norm(r.kec);
      d.kec[k] = (d.kec[k] || 0) + 1;
      if (r.mc) d.mc[r.mc] = (d.mc[r.mc] || 0) + 1;
    });
    Object.keys(per).forEach(function (code) {
      var d = per[code];
      var kecs = Object.keys(d.kec).sort(function (a, b) {
        return d.kec[b] - d.kec[a]; });
      if (kecs.length > 1) slabStraddle++;
      // Filed where most of their outlets are, so the region rows still add
      // up to the circle.
      var g = by[kecs[0]] || {};
      var mcs = Object.keys(d.mc).sort(function (a, b) {
        return d.mc[b] - d.mc[a]; });
      REPS.push({ dse: code, outlets: d.outlets,
                  codes: Object.keys(d.codes),
                  brands: Object.keys(d.brands).sort(),
                  desa: Object.keys(d.desa).length,
                  desaKeys: Object.keys(d.desa),
                  region: g.region || "—", area: g.area || "—",
                  branch: g.branch || "—", terr: g.terr || (mcs[0] || "—"),
                  kecN: kecs.length,
                  kec: kecs[0] });
    });
    REPS.sort(function (a, b) { return b.desa - a.desa || b.outlets - a.outlets; });
  }

  function slabRows() {
    var groups = {};
    function bucket(name) {
      return groups[name] || (groups[name] = {
        name: name, slab: SLABS.map(function () { return 0; }),
        dse: 0, seen: {}, assign: 0, outlets: 0 });
    }
    REPS.forEach(function (r) {
      var name = slabLevel === "circle" ? "JAYA CIRCLE" : (r[slabLevel] || "—");
      var g = bucket(name), i = slabOf(r.desa);
      if (i >= 0) g.slab[i]++;
      g.dse++;
      // Two columns, because they answer different questions: how many desa
      // this group touches at all, and how many rep-to-desa assignments
      // there are. Summing the per-rep counts and calling the total "desa"
      // reported 15,824 for a file covering 6,488 -- every desa worked by
      // more than one rep counted once per rep.
      r.desaKeys.forEach(function (k) { g.seen[k] = 1; });
      g.assign += r.desa;
      g.outlets += r.outlets;
    });
    Object.keys(groups).forEach(function (k) {
      groups[k].desa = Object.keys(groups[k].seen).length;
    });
    var out = Object.keys(groups).map(function (k) { return groups[k]; });
    out.sort(function (a, b) { return b.dse - a.dse || (a.name < b.name ? -1 : 1); });
    return out;
  }

  // Built from the brands the file actually carries, so a single-brand
  // export gets no buttons rather than two that filter to nothing.
  function renderBrands() {
    var el = $("dbBrand");
    if (!el) return;
    // The buttons were read from a file that has gone; leaving them on
    // screen would offer a filter over nothing.
    if (!LOCAL) { el.innerHTML = ""; slabBrand = ""; return; }
    var seen = {};
    LOCAL.rows.forEach(function (r) { if (r.brand) seen[r.brand] = 1; });
    var names = Object.keys(seen).sort();
    if (names.length < 2) { el.innerHTML = ""; slabBrand = ""; return; }
    el.innerHTML = '<button type="button" class="nb'
      + (slabBrand ? "" : " on") + '" data-brand="">Both brands</button>'
      + names.map(function (n) {
          return '<button type="button" class="nb' + (slabBrand === n ? " on" : "")
            + '" data-brand="' + esc(n) + '">' + esc(n) + "</button>";
        }).join("");
    el.querySelectorAll("button").forEach(function (b) {
      b.addEventListener("click", function () {
        slabBrand = b.getAttribute("data-brand") || "";
        $("dbDrill").innerHTML = "";
        renderSlabs();
      });
    });
  }

  function renderSlabs() {
    var wrap = $("dbSlab");
    if (!wrap) return;
    // Before the early return, not after it: on sign-out this is the call
    // that takes the brand buttons away with the file they were read from.
    renderBrands();
    if (!LOCAL) {
      wrap.innerHTML = '<div class="mw-note">Connect local data to see this.</div>';
      $("dbSlabMsg").textContent = "";
      $("dbDrill").innerHTML = "";
      return;
    }
    if (!ROWS.length) {
      wrap.innerHTML = '<div class="mw-note">The territory tree has not '
        + "loaded, so the reps cannot be filed by region yet.</div>";
      return;
    }
    buildReps();
    var rows = slabRows();
    var head = ['<span class="head">' + esc(LEVELS[slabLevel]) + "</span>"]
      .concat(SLABS.map(function (s) {
        return '<span class="head n">' + esc(s.k) + " desa</span>"; }))
      // "Desa" is how many distinct desa this group touches; "Assign." is
      // how many rep-to-desa assignments there are. They differ wherever a
      // desa is worked by more than one rep, and conflating them is what
      // reported 15,824 desa for a file covering 6,488.
      .concat(['<span class="head n">' + (hasUnik() ? "Reps" : "DSE") + "</span>",
               '<span class="head n">Desa</span>',
               '<span class="head n">Assign.</span>',
               '<span class="head n">Outlets</span>']);

    var body = rows.map(function (g) {
      var all = slabLevel === "circle" ? " all" : "";
      return '<span class="grp' + all + '">' + esc(g.name) + "</span>"
        + g.slab.map(function (n, i) {
            return '<span class="n' + all + (n ? " hit" : " zero")
              + '"' + (n ? ' data-g="' + esc(g.name) + '" data-s="' + i + '"' : "")
              + ">" + (n || "—") + "</span>";
          }).join("")
        + '<span class="n tot' + all + '">' + num(g.dse) + "</span>"
        + '<span class="n tot' + all + '">' + num(g.desa) + "</span>"
        + '<span class="n tot' + all + '">' + num(g.assign) + "</span>"
        + '<span class="n tot' + all + '">' + num(g.outlets) + "</span>";
    }).join("");

    wrap.innerHTML = '<div class="mw-mtx">' + head.join("") + body + "</div>";
    wrap.querySelectorAll(".mw-mtx .hit").forEach(function (el) {
      el.addEventListener("click", function () {
        drill(el.getAttribute("data-g"), Number(el.getAttribute("data-s")));
      });
    });

    var solo = REPS.filter(function (r) { return r.desa === 1; }).length;
    var wide = REPS.filter(function (r) { return r.desa > 10; }).length;
    var avg = REPS.length
      ? (REPS.reduce(function (a, r) { return a + r.desa; }, 0) / REPS.length)
      : 0;
    $("dbSlabMsg").innerHTML =
      "<b>" + num(REPS.length) + "</b> "
      + (hasUnik() ? "reps <span class='mw-note'>(by UNIKDSE — a rep on both "
                     + "brands is one rep)</span>" : "DSE codes")
      + (slabBrand ? " in <b>" + esc(slabBrand) + "</b>" : "")
      + " · <b>" + avg.toFixed(1)
      + "</b> desa each on average · <b>" + num(solo)
      + "</b> holding a single desa · <b>" + num(wide)
      + "</b> above ten. "
      + (slabStraddle
          ? "<b>" + num(slabStraddle) + "</b> rep"
            + (slabStraddle === 1 ? " has" : "s have")
            + " outlets in more than one kecamatan and " + (slabStraddle === 1
              ? "is" : "are") + " filed where most of them are, so the rows "
            + "still add up to the circle. "
          : "")
      + "Click any count for the reps behind it.";
  }

  function drill(group, slabIdx) {
    var el = $("dbDrill");
    if (!el) return;
    var s = SLABS[slabIdx];
    var list = REPS.filter(function (r) {
      if (slabLevel !== "circle" && (r[slabLevel] || "—") !== group) return false;
      return r.desa >= s.lo && r.desa <= s.hi;
    });
    el.innerHTML = '<div class="mw-drill"><div class="mw-drillhead">'
      + '<span class="k">' + esc(group) + " · " + esc(s.k) + " desa</span>"
      + '<b style="font:700 12px/1.3 inherit">' + num(list.length) + " DSE</b>"
      + '<span class="mw-dgap"></span>'
      + '<button type="button" class="nb" data-drillclose>Close</button></div>'
      + '<div class="mw-drillrow head"><span>DSE</span><span>Territory</span>'
      + '<span class="n">Desa</span><span class="n">Outlets</span>'
      + '<span class="n">Per desa</span><span></span></div>'
      + list.map(function (r) {
          return '<div class="mw-drillrow"><span><b>' + esc(r.dse)
            + "</b>" + (r.brands && r.brands.length > 1
                ? " <span class='mw-note'>" + esc(r.brands.join("+"))
                  + "</span>" : "")
            + "</span><span>" + esc(r.terr)
            + (r.kecN > 1 ? " <span class='mw-note'>+" + (r.kecN - 1)
                            + " kec</span>" : "")
            + '</span><span class="n">' + num(r.desa)
            + '</span><span class="n">' + num(r.outlets)
            + '</span><span class="n">' + (r.outlets / r.desa).toFixed(1)
            + '</span><span><button type="button" class="nb" data-goto-dse="'
            // The rows are keyed on the person, but the map filter and the
            // roster select a DSE CODE, so hand it one of the codes this
            // person actually holds rather than their UNIKDSE -- which is
            // not in that list and would have selected nothing.
            + esc((r.codes && r.codes[0]) || r.dse)
            + '">Show on map</button></span></div>';
        }).join("")
      + "</div>";
    el.querySelector("[data-drillclose]").addEventListener("click", function () {
      el.innerHTML = "";
    });
    el.querySelectorAll("[data-goto-dse]").forEach(function (b) {
      b.addEventListener("click", function () {
        // Straight to the territory: the map view, filtered to this rep,
        // with the ledger open on them.
        var code = b.getAttribute("data-goto-dse");
        $("mwDse").value = code;
        go("map");
        onDse();
        roster();
        drawerDse(code);
      });
    });
    el.scrollIntoView({ block: "nearest" });
  }

  if ($("dbLevel")) {
    $("dbLevel").addEventListener("click", function (e) {
      var b = e.target.closest("button[data-lvl]");
      if (!b || b.dataset.lvl === slabLevel) return;
      slabLevel = b.dataset.lvl;
      $("dbLevel").querySelectorAll("button").forEach(function (x) {
        x.classList.toggle("on", x.dataset.lvl === slabLevel);
      });
      $("dbDrill").innerHTML = "";
      renderSlabs();
    });
  }
  if ($("dbSlabCsv")) {
    $("dbSlabCsv").addEventListener("click", function () {
      if (!LOCAL || !REPS.length) return;
      function q(v) {
        var t = String(v == null ? "" : v);
        return /[",\n]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t;
      }
      var head = [LEVELS[slabLevel]].concat(SLABS.map(function (s) {
        return s.k + " desa"; })).concat(["DSE", "Desa", "Outlets"]);
      var body = slabRows().map(function (g) {
        return [g.name].concat(g.slab).concat([g.dse, g.desa, g.outlets])
          .map(q).join(",");
      });
      var url = URL.createObjectURL(new Blob(
        ["﻿" + head.join(",") + "\n" + body.join("\n")],
        { type: "text/csv;charset=utf-8" }));
      var a = document.createElement("a");
      a.href = url;
      a.download = "dse-by-desa-" + slabLevel + ".csv";
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
    });
  }


  /* ══════════════════════════════════════════════════════════════════════
     WHICH POLYGON IS THE CLICK ASKING ABOUT?

     Everything draws into one canvas, and Leaflet hands a click to the
     last-drawn layer that contains the point. That is the right default --
     a dot beats the desa under it, a desa beats the kecamatan under that --
     but it means that once borders are built they sit on top of everything
     and a click can only ever profile the border. A reader who wanted the
     desa underneath got a rep's territory instead, with no way to say
     otherwise. That is the mismatch.

     So the target is named BEFORE the click. On "auto" nothing changes and
     the layer handlers answer as they always did. Name a level and this
     takes over both hover and click: the point is tested against that
     layer's own geometry, whatever happens to be drawn over it.

     THE TEST IS DONE HERE RATHER THAN ASKED OF LEAFLET
     Leaflet will only report what it drew on top. Finding the desa beneath
     a border means testing the point against the desa layer directly, so
     there is a ray-cast here over the features already in memory, with a
     bounding-box reject first -- 7,761 desa comes down to a handful of real
     tests and the hover stays instant.
     ══════════════════════════════════════════════════════════════════════ */
  var profileTarget = "auto";

  function ringHas(ring, x, y) {
    var inside = false;
    for (var i = 0, j = ring.length - 1; i < ring.length; j = i++) {
      var xi = ring[i][0], yi = ring[i][1], xj = ring[j][0], yj = ring[j][1];
      if (((yi > y) !== (yj > y))
          && (x < (xj - xi) * (y - yi) / (yj - yi) + xi)) inside = !inside;
    }
    return inside;
  }
  // A hole is a hole: inside the outer ring but inside a hole is outside.
  function polyHas(rings, x, y) {
    if (!rings.length || !ringHas(rings[0], x, y)) return false;
    for (var i = 1; i < rings.length; i++)
      if (ringHas(rings[i], x, y)) return false;
    return true;
  }
  function featHas(f, x, y) {
    var g = f.geometry;
    if (!g) return false;
    if (g.type === "Polygon") return polyHas(g.coordinates, x, y);
    if (g.type === "MultiPolygon") {
      for (var i = 0; i < g.coordinates.length; i++)
        if (polyHas(g.coordinates[i], x, y)) return true;
    }
    return false;
  }
  // Cached on the feature: computed once, then every later hover is four
  // comparisons before any ray-casting happens at all.
  function bbOf(f) {
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

  function targetFeatures(k) {
    if (k === "border") {
      if (!MODEL) return [];
      var out = [];
      MODEL.eachLayer(function (l) { if (l.feature) out.push(l.feature); });
      return out;
    }
    return (CL[k] && CL[k].features) || [];
  }
  function findAt(k, ll) {
    var fs = targetFeatures(k), x = ll.lng, y = ll.lat;
    for (var i = 0; i < fs.length; i++) {
      var b = bbOf(fs[i]);
      if (!b || x < b[0] || x > b[2] || y < b[1] || y > b[3]) continue;
      if (featHas(fs[i], x, y)) return fs[i];
    }
    return null;
  }

  function targetLabel(k) {
    if (k === "border") return "DSE border";
    return (CL[k] && CL[k].def.label) || k;
  }

  // One tooltip reused, rather than one bound per feature: the target sweeps
  // across thousands of polygons and a tooltip each is what stalls a tab.
  var tgtTip = null, tgtLast = 0, tgtOn = false;

  function targetHover(e) {
    if (profileTarget === "auto") return;
    var now = Date.now();
    if (now - tgtLast < 60) return;      // a mousemove fires far faster than
    tgtLast = now;                       // anybody can read
    var f = findAt(profileTarget, e.latlng);
    if (!f) {
      if (tgtOn) { map.closeTooltip(tgtTip); tgtOn = false; }
      return;
    }
    var html = profileTarget === "border"
      ? "<strong>" + esc(f.properties.dse) + "</strong><br>"
        + num(f.properties.outlets) + " outlets · " + num(f.properties.desa)
        + " desa · " + num(f.properties.sites) + " sites<br>"
        + f.properties.area_km2 + " km²<br>"
        + "<span class='muted'>click for the full profile</span>"
      : tipHtml(f, CL[profileTarget].def);
    if (!tgtTip) tgtTip = L.tooltip({ className: "terr-tip", sticky: true });
    tgtTip.setContent(html).setLatLng(e.latlng);
    if (!tgtOn) { map.openTooltip(tgtTip); tgtOn = true; }
  }
  map.on("mousemove", targetHover);
  map.on("mouseout", function () {
    if (tgtOn) { map.closeTooltip(tgtTip); tgtOn = false; }
  });

  map.on("click", function (e) {
    if (profileTarget === "auto") return;
    var k = profileTarget;
    if (k === "border") {
      if (!MODEL) {
        inspect("Inspector", "No borders built",
                "Build the borders in the rail, then click one.", "", "");
        return;
      }
      var f = findAt("border", e.latlng);
      if (f) { soloBorder(f.properties.dse); drawerTerritory(f); }
      else inspect("Inspector", "No border there",
                   "No DSE territory covers that point.", "", "");
      return;
    }
    var c = CL[k];
    if (!c.layer && !c.busy) {
      // Asked to profile a layer that is not on: fetch it and tick it,
      // rather than answering nothing and leaving the reader to work out
      // why their choice did nothing.
      setBtn("[data-layer='" + k + "']", true);
      showCloud(k, true, function () { map.fire("click", e); });
      return;
    }
    var g = findAt(k, e.latlng);
    if (g) { pickArea(g, c.def); return; }

    // A MISS IS USUALLY A GAP IN THE LAYER, NOT A MIS-CLICK
    // The kecamatan layer carries 719 names where the desa rows carry 754:
    // 36 kecamatan have desa but no boundary of their own, Tanah Abang
    // among them. Saying only "nothing there" would leave a reader
    // re-clicking a place that is never going to answer, so where the desa
    // layer knows what sits at that point, say what it is and why the
    // chosen layer cannot show it.
    var under = CL.kelurahan.features ? findAt("kelurahan", e.latlng) : null;
    if (under) {
      var up = under.properties || {};
      var where = k === "kecamatan" ? featKec(up)
                : (k === "indosat_mc" ? featMc(up) : featKab(up));
      inspect("Inspector", "Not in this layer",
              featName(up) + " sits in " + (where || "an area")
              + ", which has no " + targetLabel(k).toLowerCase()
              + " boundary loaded. Switch the target to Desa / Kelurahan to "
              + "read it.", "", "");
    } else {
      inspect("Inspector", "Nothing there",
              "No " + targetLabel(k).toLowerCase()
              + " polygon covers that point.", "", "");
    }
  });

  if ($("mwTarget")) {
    $("mwTarget").addEventListener("change", function () {
      profileTarget = $("mwTarget").value || "auto";
      if (tgtOn) { map.closeTooltip(tgtTip); tgtOn = false; }
      $("mwTargetNote").innerHTML = profileTarget === "auto"
        ? "Hover and click answer for whichever polygon is drawn on top."
        : "Hover and click answer for the <b>"
          + esc(targetLabel(profileTarget))
          + "</b> under the cursor, whatever is drawn over it.";
    });
  }

  /* The sheet is sized against the real height of the application's own
     header rather than a number guessed here, because that header is shared
     and can grow a row without this page being touched. */
  function fit() {
    var nav = document.querySelector(".terr-top, .topbar");
    var h = nav ? nav.getBoundingClientRect().height : 46;
    $("mwRoot").style.height = Math.max(360, window.innerHeight - h) + "px";
    map.invalidateSize();
  }
  window.addEventListener("resize", fit);

  /* ── boot ───────────────────────────────────────────────────────────
     Kecamatan first: it frames the working area in a fifth of a second, so
     the map is usable before the desa layer -- the heaviest of the four --
     has landed. */
  fit();
  idle();
  dashCloud(); dashLocal(); renderTable(); scopeLine();
  showCloud("kecamatan", true, function () {
    setTimeout(function () { showCloud("kelurahan", true); }, 120);
    // Warm the area table behind the first paint, so the first hover of the
    // session already has its figures instead of filling in a beat later.
    setTimeout(ensureStats, 600);
  });
})();
