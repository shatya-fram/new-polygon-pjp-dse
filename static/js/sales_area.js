/* Sales Area summary — Distribution Polygon only.

   Six columns, twelve rows, one click to go deeper. The point is to let a
   regional head see every sales area at once and pick the one that looks
   wrong, so this table shows what a territory IS — reps, masts, shops, desa,
   ground — and nothing about how any of it is performing. Performance has a
   page of its own; mixing the two produces a table you have to read twice.

   THE NUMBERS ARE NOT THIS FILE'S OPINION
   Every count comes from /api/distribution/territory-summary, which is a thin
   view over the same roll-up that Preview Polygon prints. That roll-up places
   each outlet and each mast in a desa polygon exactly once and sums upward,
   so a sales area here equals its microclusters, which equal their kecamatan,
   which equal their desa. If this table and the map ever disagree, one of
   them has stopped reading the roll-up — that is the only way it can happen.

   CLICKING A ROW MOVES THE MAP
   Drilling down without moving the map would leave you reading about North
   Bekasi while looking at Karawang. So a click does both: it descends a level
   AND sets the territory filter above the map, which zooms and redraws the
   DSE model for that territory. The table and the polygons are never
   describing different places. */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var T = window.TMAP;
  if (!$("saTable")) return;

  // level -> the filter key a row of that level sets
  var FILTER_OF = { branch: "branch", mc: "mc", kecamatan: "kecamatan" };
  var TITLE = { branch: "Sales Area", mc: "Microcluster",
                kecamatan: "Kecamatan", desa: "Desa / Kelurahan" };

  var level = "branch";
  var filters = {};
  var crumbs = [{ level: "branch", label: "All sales areas", filters: {} }];
  var loaded = false;

  function esc(s) {
    return String(s === null || s === undefined ? "" : s)
      .replace(/[&<>"']/g, function (c) {
        return { "&": "&amp;", "<": "&lt;", ">": "&gt;",
                 '"': "&quot;", "'": "&#39;" }[c];
      });
  }
  function num(v) {
    var n = Number(v);
    return isNaN(n) ? "—" : Math.round(n).toLocaleString();
  }
  function km2(v) {
    var n = Number(v);
    return isNaN(n) ? "—" : n.toLocaleString(undefined,
      { minimumFractionDigits: 1, maximumFractionDigits: 1 });
  }

  // A territory no export reaches must read as "not measured", never as a
  // zero. They mean opposite things and only one of them is a finding.
  function dse(r) {
    return r.has_dse_data === false
      ? '<span class="muted" title="no DSE export covers this territory">n/a</span>'
      : num(r.dse);
  }

  function renderCrumbs() {
    $("saCrumbs").innerHTML = crumbs.map(function (c, i) {
      var last = i === crumbs.length - 1;
      return last
        ? '<span class="sa-crumb on">' + esc(c.label) + "</span>"
        : '<button type="button" class="sa-crumb" data-i="' + i + '">'
          + esc(c.label) + "</button>";
    }).join('<span class="sa-sep">›</span>');
  }

  function render(d) {
    var next = d.next_level;
    var head = "<thead><tr>"
      + "<th>" + esc(d.label) + "</th>"
      + "<th class='n'>DSE</th><th class='n'>Sites</th>"
      + "<th class='n'>Outlets</th><th class='n'>Desa</th>"
      + "<th class='n'>Size km²</th>"
      + "<th class='n'>Outlets / DSE</th></tr></thead>";

    var body = (d.rows || []).map(function (r) {
      var per = r.dse ? (r.outlets / r.dse) : null;
      return "<tr" + (next ? ' class="drill" data-key="'
          + esc(r.filter_key || r.key) + '"' : "")
        + (r.kabkot ? ' data-kabkot="' + esc(r.kabkot) + '"' : "")
        + "><td>" + esc(r.key) + (next ? '<span class="sa-go">›</span>' : "")
        + "</td>"
        + "<td class='n'>" + dse(r) + "</td>"
        + "<td class='n'>" + num(r.sites) + "</td>"
        + "<td class='n'>" + num(r.outlets) + "</td>"
        + "<td class='n'>" + num(r.desa) + "</td>"
        + "<td class='n'>" + km2(r.area_km2) + "</td>"
        + "<td class='n'>" + (per === null ? "—"
            : per.toLocaleString(undefined, { maximumFractionDigits: 1 }))
        + "</td></tr>";
    }).join("");

    var t = d.totals;
    var foot = t ? "<tfoot><tr><td>Total</td>"
      + "<td class='n'>" + num(t.dse) + "</td>"
      + "<td class='n'>" + num(t.sites) + "</td>"
      + "<td class='n'>" + num(t.outlets) + "</td>"
      + "<td class='n'>" + num(t.desa) + "</td>"
      + "<td class='n'>" + km2(t.area_km2) + "</td>"
      + "<td class='n'>" + (t.dse
          ? (t.outlets / t.dse).toLocaleString(undefined,
              { maximumFractionDigits: 1 }) : "—")
      + "</td></tr></tfoot>" : "";

    $("saTable").innerHTML = head + "<tbody>" + body + "</tbody>" + foot;

    // The DSE total is a headcount, not a sum: a rep working two sales areas
    // is one person and appears in both rows. Saying so stops the column from
    // looking like an arithmetic error.
    $("saNote").innerHTML = next
      ? "click a row to open its " + esc(TITLE[next].toLowerCase())
        + " — the map follows"
      : "deepest level — every row is a single desa";

    $("saTable").querySelectorAll("tr.drill").forEach(function (tr) {
      tr.addEventListener("click", function () {
        drill(next, tr.dataset.key, tr.dataset.kabkot);
      });
    });
  }

  // A ticket per request, checked when the answer lands. Drill twice
  // quickly and the shallower response could arrive last, repainting the
  // table for a level the crumbs and filters had already moved past -- and
  // the next click then computed its filter against a table showing
  // something else.
  var SEQ = {};
  function ticket(k) { SEQ[k] = (SEQ[k] || 0) + 1; return SEQ[k]; }
  function isCurrent(k, n) { return SEQ[k] === n; }

  function load() {
    var q = ["level=" + level];
    Object.keys(filters).forEach(function (k) {
      if (filters[k]) q.push(k + "=" + encodeURIComponent(filters[k]));
    });
    $("saNote").textContent = "Loading…";
    var tk = ticket("sa");
    fetch("/api/distribution/territory-summary?" + q.join("&"))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!isCurrent("sa", tk)) return;
        if (d.ok === false) {
          $("saTable").innerHTML = "";
          $("saNote").textContent = d.error || "could not load";
          return;
        }
        renderCrumbs();
        render(d);
      })
      .catch(function (e) {
        if (!isCurrent("sa", tk)) return;
        $("saNote").textContent = "Failed: " + e;
      });
  }

  function drill(next, key, kabkot) {
    if (!next) return;
    var fkey = FILTER_OF[level];
    var f = {};
    Object.keys(filters).forEach(function (k) { f[k] = filters[k]; });
    if (fkey) f[fkey] = key;
    // Below MC the name alone is not unique — CIPAYUNG is a kecamatan of
    // Jakarta Timur and one of Depok — so the kabupaten travels with it.
    if (level === "kecamatan" && kabkot) f.kabkot = kabkot;
    filters = f;
    level = next;
    crumbs.push({ level: next, label: key, filters: f });
    load();
    moveMap(f);
  }

  // The filter bar above the map owns zoom and redraw; this only tells it
  // where to look. Guarded because the bar is a separate file that may not
  // have initialised on a page without a map.
  function moveMap(f) {
    if (T && T.setTerritory) T.setTerritory(f, true);
  }

  $("saCrumbs").addEventListener("click", function (e) {
    var b = e.target.closest("button[data-i]");
    if (!b) return;
    var i = Number(b.dataset.i);
    crumbs = crumbs.slice(0, i + 1);
    var c = crumbs[i];
    level = c.level;
    filters = c.filters;
    load();
    moveMap(filters);
  });

  // Built on first sight, not on page load: the roll-up walks 887 polygons
  // and there is no reason to make everyone who opens the map wait for a
  // table they may never look at.
  document.querySelectorAll('.rtab[data-pane="paneArea"]').forEach(function (b) {
    b.addEventListener("click", function () {
      if (loaded) return;
      loaded = true;
      load();
    });
  });
})();
