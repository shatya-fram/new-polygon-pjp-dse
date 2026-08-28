/* Preview Polygon — the territory summary, drilled from Inner Jakarta down
   to a microcluster, with a Go To Area button that carries the selection
   over to the map. */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var LEVELS = [
    { key: "area", label: "Area" },
    { key: "branch", label: "Branch / Sales Area" },
    { key: "mc", label: "Microcluster" },
    { key: "kecamatan", label: "Kecamatan" },
    { key: "desa", label: "Desa" }
  ];
  var state = { level: "area", filters: {}, data: null,
                sort: null, dir: 1 };     // dir: 1 = small to big

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function num(v) { return v == null ? "—" : Number(v).toLocaleString(); }

  function fmt(v, kind, row) {
    if (v == null || v === "") return '<span class="muted">—</span>';
    switch (kind) {
      case "int": return num(Math.round(v));
      case "num1": return Number(v).toLocaleString(undefined, { maximumFractionDigits: 1 });
      case "idr": return (Number(v) / 1e9).toFixed(2) + " Bn";
      case "pct": return (Number(v) * 100).toFixed(1) + "%";
      // A territory the DSE export does not reach must read as "not
      // measured", never as a zero — they mean opposite things to anyone
      // reading the table.
      case "int_dse":
        return row && row.has_dse_data === false
          ? '<span class="muted" title="no DSE data loaded for this territory">n/a</span>'
          : num(Math.round(v));
      case "num1_dse":
        return row && row.has_dse_data === false
          ? '<span class="muted" title="no DSE data loaded for this territory">n/a</span>'
          : Number(v).toLocaleString(undefined, { maximumFractionDigits: 1 });
      default: return esc(v);
    }
  }

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

  function query(extra) {
    var q = new URLSearchParams();
    q.set("level", (extra && extra.level) || state.level);
    Object.keys(state.filters).forEach(function (k) {
      if (state.filters[k]) q.set(k, state.filters[k]);
    });
    return q;
  }

  // ── chrome ────────────────────────────────────────────────────────────
  function renderCrumb() {
    var f = state.filters;
    var parts = ['<a href="#" data-drop="all">Inner Jakarta</a>'];
    [["area", "Area"], ["branch", "Branch"], ["mc", "MC"],
     ["kabkot", "Kota/Kab"], ["kecamatan", "Kecamatan"]].forEach(function (p) {
      if (f[p[0]]) parts.push('<a href="#" data-drop="' + p[0] + '">'
        + esc(f[p[0]]) + "</a>");
    });
    $("crumb").innerHTML = parts.join('<span class="sep">›</span>');
    $("crumb").querySelectorAll("a").forEach(function (a) {
      a.addEventListener("click", function (e) {
        e.preventDefault();
        var d = a.dataset.drop;
        if (d === "all") state.filters = {};
        else {
          // dropping a level drops everything below it
          var order = ["area", "branch", "mc", "kabkot", "kecamatan"];
          var i = order.indexOf(d);
          order.slice(i + 1).forEach(function (k) { delete state.filters[k]; });
        }
        load();
      });
    });
  }

  function renderLevels() {
    $("levels").innerHTML = LEVELS.map(function (l) {
      return '<button type="button" class="rtab' + (l.key === state.level ? " on" : "")
        + '" data-level="' + l.key + '">' + l.label + "</button>";
    }).join("");
    $("levels").querySelectorAll("button").forEach(function (b) {
      b.addEventListener("click", function () {
        state.level = b.dataset.level; load();
      });
    });
  }

  function renderCoverage(d) {
    var c = d.coverage || {};
    var full = c.mc_with_dse === c.mc_total;
    var bits = [];
    bits.push(full
      ? "DSE and outlet data covers all " + c.mc_total + " microclusters."
      : "<b>DSE and outlet figures cover " + c.mc_with_dse + " of " + c.mc_total
        + " microclusters</b> — " + esc((c.branches_with_dse || []).join(", "))
        + " only, from the DSE-to-outlet export currently loaded ("
        + num(c.dse_total) + " DSE, " + num(c.outlets_total) + " outlets). "
        + "Everywhere else those columns read <b>n/a</b>, meaning not measured "
        + "— not zero. Upload an export covering the other branches on the "
        + "<a href=\"/data-files\">Data Files</a> menu to fill them in.");
    // Where the site figure comes from changes what it means, so say it.
    if (c.sites_source === "site file") {
      var kecGap = (c.kecamatan_total || 0) - (c.kecamatan_with_sites || 0);
      bits.push("<b>" + num(c.sites_total) + " sites</b> from the uploaded site"
        + " file, placed inside a kecamatan"
        + (c.sites_outside ? " (" + num(c.sites_outside)
           + " fell outside the territory and are not counted)" : "")
        + ". The export lists one row per site-and-outlet pair"
        + (c.site_rows_read ? " — " + num(c.site_rows_read)
           + " rows in all —" : ",") + " so rows are collapsed on the site"
        + " id and the site's own coordinates; counting rows would report the"
        + " same mast once per outlet it serves."
        + (kecGap > 0
           ? " <b>" + kecGap + " of " + c.kecamatan_total + " kecamatan are not"
             + " in this file at all</b> and show 0 sites — that is an"
             + " absence in the export, not an absence of sites."
           : ""));
    } else {
      bits.push("Sites come from the <b>per-kecamatan count</b> in the profile "
        + "spreadsheet, not from site locations — so they cannot be broken "
        + "down below kecamatan or drawn on the map. Upload a site KML or "
        + "Excel on the <a href=\"/data-files\">Data Files</a> menu to "
        + "replace it with real positions.");
    }
    // WHERE THE NUMBERS IN THIS TABLE CAME FROM
    // Every level has to add up to the same thing, and for a while they did
    // not: the export names a kecamatan and for 608 outlets that name was
    // wrong, so those shops fell out of the table while still sitting on the
    // map. They are placed by the polygon that contains them now. Saying so
    // is not a footnote -- a count nobody can reconcile is a count nobody
    // should act on.
    var orc = c.outlet_recon || {};
    if (orc.rows && c.counts_from === "desa polygons") {
      bits.push("Every count in this table is <b>one placement, summed</b>: "
        + "each outlet and each site is put in the desa polygon that contains"
        + " it, and kecamatan, microcluster, branch and area are sums of that."
        + " A kecamatan therefore always equals its desa added up."
        + (orc.by_polygon
           ? " The export's own kecamatan column disagreed with the map for <b>"
             + num(orc.by_polygon) + " of " + num(orc.rows) + " outlets</b>"
             + " — the map is what the table counts." : ""));
    } else if (orc.rows) {
      bits.push("<b>" + num(orc.rows) + " outlets</b> reconciled: "
        + num(orc.by_name) + " placed by the kecamatan named in the export"
        + (orc.by_polygon
           ? ", <b>" + num(orc.by_polygon) + " by the polygon they sit in</b>"
             + " where that name was wrong" : "")
        + (orc.unplaced ? ", " + num(orc.unplaced) + " placed by neither and"
           + " left out" : "") + ". Upload the desa boundaries to count every"
        + " level off one placement instead.");
    }
    var drc = c.desa_recon;
    if (drc && drc.outlet_rows) {
      // The desa cover and the kecamatan cover come from different files and
      // disagree by metres along coastlines, so a handful of points land in
      // a kecamatan and in no desa. They are offered the nearest desa of the
      // kecamatan they are already standing in -- never one further afield.
      bits.push("The placement itself, against " + num(drc.desa_polygons)
        + " desa boundaries: " + num(drc.outlets_placed) + " outlets and "
        + num(drc.sites_placed) + " sites fall inside one"
        + ((drc.outlets_snapped || drc.sites_snapped)
           ? ", " + num((drc.outlets_snapped || 0) + (drc.sites_snapped || 0))
             + " sit in a seam between the desa and kecamatan boundaries and"
             + " are put in the nearest desa of the kecamatan that does"
             + " contain them" : "")
        + ((drc.outlets_outside || drc.sites_outside)
           ? ", and <b>" + num((drc.outlets_outside || 0)
             + (drc.sites_outside || 0)) + " carry coordinates too far from"
             + " the territory they name to place</b> and are excluded"
           : "") + ".");
    }
    $("coverage").className = "pv-cover " + (full ? "ok" : "warn");
    $("coverage").innerHTML = bits.join(" ");
  }

  function renderCards(t) {
    var cards = [
      ["Microclusters", num(t.n_mc)], ["Kecamatan", num(t.n_kecamatan)],
      ["Desa", num(t.n_desa)],
      ["Area", Number(t.area_km2 || 0).toLocaleString(undefined,
        { maximumFractionDigits: 0 }) + " km²"],
      ["Population", num(t.population)],
      ["VLR subs", num(t.vlr)],
      ["Prepaid rev", ((t.prepaid_revenue || 0) / 1e9).toFixed(1) + " Bn"],
      ["Sites", num(t.sites)],
      ["DSE", t.has_dse_data ? num(t.dse) : "n/a"],
      ["Outlets", t.has_dse_data ? num(t.outlets) : "n/a"],
      ["Outlet / DSE", t.has_dse_data ? (t.outlets_per_dse || "—") : "n/a"],
      ["Desa / DSE", t.has_dse_data ? (t.desa_per_dse || "—") : "n/a"]
    ];
    $("cards").innerHTML = cards.map(function (c) {
      return '<div class="pv-card"><span class="n">' + c[1]
        + '</span><span class="l">' + c[0] + "</span></div>";
    }).join("");
  }

  // ── sorting ─────────────────────────────────────────────────────────────
  // Click a column to rank by it, smallest first; click again to flip. A
  // third click drops the sort and hands the order back to the server, which
  // is by subscribers — so there is always a way out of a sort, and the
  // default is never lost.
  //
  // "—" IS NOT ZERO, AND IT DOES NOT SORT LIKE ZERO
  // A dash in this table means not measured: a territory no export reaches,
  // or a metric that only exists above desa level. Sorted as zero it would
  // fill the top of every ascending sort and bury the real smallest value,
  // which is exactly the thing someone sorting ascending is looking for. So
  // blanks sink to the bottom in BOTH directions. They are absent, not last.
  function cmp(a, b, key, dir) {
    var x = a[key], y = b[key];
    var xn = x === null || x === undefined || x === "";
    var yn = y === null || y === undefined || y === "";
    if (xn || yn) return xn && yn ? 0 : (xn ? 1 : -1);
    if (typeof x === "number" && typeof y === "number")
      return (x - y) * dir;
    var nx = Number(x), ny = Number(y);
    if (!isNaN(nx) && !isNaN(ny)) return (nx - ny) * dir;
    return String(x).localeCompare(String(y)) * dir;
  }

  function sorted(rows) {
    if (!state.sort) return rows;
    // A copy: the server's order is the fallback a third click returns to.
    return rows.slice().sort(function (a, b) {
      return cmp(a, b, state.sort, state.dir);
    });
  }

  function arrow(key) {
    if (state.sort !== key) return '<span class="srt"></span>';
    return '<span class="srt on">' + (state.dir === 1 ? "▲" : "▼") + "</span>";
  }

  function onSort(key) {
    if (state.sort !== key) { state.sort = key; state.dir = 1; }
    else if (state.dir === 1) { state.dir = -1; }
    else { state.sort = null; state.dir = 1; }
    renderTable(state.data);
  }

  function renderTable(d) {
    var cols = d.columns;
    // The rank number only appears once a sort is on. Without a sort there is
    // no rank to state, and a column of 1..n against the server's own order
    // would be a number that means nothing.
    var ranked = !!state.sort;
    // The rank has to travel with the name when the table scrolls sideways,
    // and this table is far wider than the window. The class is what moves
    // the sticky offsets; see table.pv-table.ranked in the stylesheet.
    $("tbl").classList.toggle("ranked", ranked);
    $("thead").innerHTML = (ranked ? '<th class="rk">#</th>' : "")
      + '<th class="lv sortable" data-sort="key">' + esc(d.label)
      + arrow("key") + "</th>"
      + cols.map(function (c) {
          return '<th class="sortable" style="text-align:right" data-sort="'
            + esc(c.key) + '">' + esc(c.label) + arrow(c.key) + "</th>";
        }).join("")
      + "<th></th>";
    if (!d.rows.length) {
      $("tbody").innerHTML = '<tr><td colspan="' + (cols.length + 3)
        + '" class="muted small">Nothing at this level for the current selection.</td></tr>';
      $("tfoot").innerHTML = "";
      wireSort();
      return;
    }
    $("tbody").innerHTML = sorted(d.rows).map(function (r, i) {
      var cells = cols.map(function (c) {
        return '<td class="n">' + fmt(r[c.key], c.fmt, r) + "</td>";
      }).join("");
      return '<tr data-key="' + esc(r.key) + '">'
        + (ranked ? '<td class="rk">' + (i + 1) + "</td>" : "")
        + '<td class="lv"><b>' + esc(r.key) + "</b></td>"
        + cells
        + '<td class="n"><button type="button" class="rtab go" data-key="'
        + esc(r.key) + '">Go to area &rsaquo;</button></td>'
        + "</tr>";
    }).join("");
    var t = d.totals;
    // The total is not a row in the ranking — it is the sum of them — so it
    // stays pinned at the bottom whatever the sort says.
    $("tfoot").innerHTML = '<tr class="tot">'
      + (ranked ? '<td class="rk"></td>' : "")
      + '<td class="lv"><b>TOTAL</b></td>'
      + cols.map(function (c) {
          return '<td class="n">' + fmt(t[c.key], c.fmt, t) + "</td>"; }).join("")
      + "<td></td></tr>";
    wireSort();

    $("tbody").querySelectorAll("button.go").forEach(function (b) {
      b.addEventListener("click", function (e) {
        e.stopPropagation();
        goToArea(b.dataset.key);
      });
    });
    // clicking the row drills one level down instead of leaving the page
    var byKey = {};
    d.rows.forEach(function (r) { byKey[r.key] = r; });
    $("tbody").querySelectorAll("tr").forEach(function (row) {
      row.classList.add("pick");
      if (state.level === "desa") row.classList.add("focusable");
      row.addEventListener("click", function () {
        // Mark which row the map is showing, so a focused desa is findable
        // again in a list of 887.
        if (state.level === "desa") {
          $("tbody").querySelectorAll("tr").forEach(function (o) {
            o.classList.toggle("focus", o === row);
          });
        }
        drill(row.dataset.key, byKey[row.dataset.key]);
      });
    });
  }

  function wireSort() {
    $("thead").querySelectorAll("th.sortable").forEach(function (th) {
      th.setAttribute("role", "button");
      th.setAttribute("tabindex", "0");
      th.title = state.sort === th.dataset.sort
        ? (state.dir === 1 ? "Smallest first — click for largest first"
                           : "Largest first — click to clear the sort")
        : "Click to rank by this column, smallest first";
      th.addEventListener("click", function () { onSort(th.dataset.sort); });
      th.addEventListener("keydown", function (e) {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          onSort(th.dataset.sort);
        }
      });
    });
  }

  var NEXT = { area: "branch", branch: "mc", mc: "kecamatan",
               kecamatan: "desa", desa: null };

  function drill(key, row) {
    var f = state.level;
    // A desa is the bottom of the hierarchy, so there is nothing to drill
    // into -- but the row still names a place, and the useful next move is
    // to go and look at it. Clicking one focuses the map on that polygon
    // instead of doing nothing at all.
    if (f === "desa") {
      window.dispatchEvent(new CustomEvent("pv:focus", { detail: {
        layer: "kelurahan",
        key: (row && row.join_key) || key,
        label: key,
        sub: row && row.kecamatan ? row.kecamatan : ""
      } }));
      return;
    }
    state.filters[f === "kecamatan" ? "kecamatan" : f] = key;
    state.level = NEXT[f] || f;
    // A microcluster is the level where the question stops being "how much"
    // and becomes "who". Drilling into one still lists its kecamatan, and
    // now also opens the roster of reps working it and takes the map there.
    if (f === "mc") {
      window.dispatchEvent(new CustomEvent("pv:mc", { detail: { mc: key } }));
    }
    load();
  }

  function goToArea(key) {
    var q = new URLSearchParams();
    Object.keys(state.filters).forEach(function (k) { q.set(k, state.filters[k]); });
    q.set(state.level === "kecamatan" ? "kecamatan" : state.level, key);
    q.set("zoom", "1");
    window.location = "/distribution?" + q.toString();
  }

  function renderCfg(d) {
    var chips = ['<span class="cfgtitle">Showing</span>'];
    chips.push('<span class="cfgchip key"><b>' + esc(d.label) + "</b></span>");
    var f = d.filters || {};
    if (!Object.keys(f).length)
      chips.push('<span class="cfgchip"><span class="k">scope</span><b>All Inner Jakarta</b></span>');
    Object.keys(f).forEach(function (k) {
      chips.push('<span class="cfgchip"><span class="k">' + esc(k)
        + '</span><b>' + esc(f[k]) + "</b></span>");
    });
    chips.push('<span class="cfgchip"><span class="k">groups</span><b>'
      + d.rows.length + "</b></span>");
    $("cfgbar").innerHTML = chips.join("");
  }

  function load() {
    $("tbody").innerHTML = '<tr><td class="muted small">loading…</td></tr>';
    // A stale roll-up does not merely repaint an old table: it publishes an
    // old PVSTATE, and the map layer reads its filters from there. One slow
    // response could put the table on one selection and the map on another.
    var tk = ticket("rollup");
    return fetch("/api/rollup?" + query().toString())
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!isCurrent("rollup", tk)) return;
        if (!d.ok) { $("tbody").innerHTML = '<tr><td class="muted small">'
          + esc(d.error) + "</td></tr>"; return; }
        state.data = d;
        // Keep the ranking across a drilldown when the column survives -- if
        // you sorted by outlets and opened a branch you still want outlets
        // first -- and drop it silently when the new level has no such
        // column, rather than sorting by a field that is not on screen.
        if (state.sort && state.sort !== "key"
            && !(d.columns || []).some(function (c) { return c.key === state.sort; })) {
          state.sort = null;
          state.dir = 1;
        }
        renderCrumb(); renderLevels(); renderCfg(d);
        renderCoverage(d); renderCards(d.totals); renderTable(d);
        // The territory preview draws from the same selection. Publish the
        // state rather than let that file reach into this one's closure.
        window.PVSTATE = { filters: state.filters, level: state.level };
        window.dispatchEvent(new CustomEvent("pv:filters"));
      })
      .catch(function (e) {
        if (!isCurrent("rollup", tk)) return;
        $("tbody").innerHTML = '<tr><td class="muted small">failed: '
          + esc(e) + "</td></tr>";
      });
  }

  // CLEAR — back to the opening view, in one click.
  //
  // Drilling four levels deep and then wanting to start again meant clicking
  // "Inner Jakarta" in the breadcrumb, re-picking the level, resetting the
  // mode, dragging reach back to 400 and re-ticking the point toggles. Five
  // controls in three places to undo what one path of clicks did.
  //
  // What it deliberately does NOT clear is the theme. That is a preference,
  // not a view: someone who has pinned light mode did not ask to be put back
  // in the dark because they wanted to start their analysis over.
  $("pvClear").addEventListener("click", function () {
    state.filters = {};
    state.level = "area";
    state.sort = null;
    state.dir = 1;
    // The territory preview owns its own controls; it resets them on this.
    window.dispatchEvent(new CustomEvent("pv:clear"));
    load();
  });

  $("pvCsv").addEventListener("click", function () {
    var d = state.data;
    if (!d) return;
    // The export follows the screen. Someone who ranked the table by outlets
    // and then downloaded it expects that order in the file, not the
    // server's.
    var head = (state.sort ? ["#"] : [])
      .concat([d.label], d.columns.map(function (c) { return c.label; }));
    var lines = [head.join(",")];
    sorted(d.rows).concat([d.totals]).forEach(function (r, i) {
      var rank = state.sort ? [r === d.totals ? "" : (i + 1)] : [];
      lines.push(rank.concat([r.key], d.columns.map(function (c) {
        var v = r[c.key];
        if (v == null) return "";
        return typeof v === "string" ? '"' + v.replace(/"/g, '""') + '"' : v;
      })).join(","));
    });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([lines.join("\n")], { type: "text/csv" }));
    a.download = "territory-" + d.level + ".csv";
    a.click();
  });

  load();
})();
