/* POI counters under the map (Retail Gapura). Click one to open the rows
   behind it. Counts follow whatever the catalogue is currently filtered to,
   so the strip always describes what is actually on screen. */
(function () {
  "use strict";
  var M = window.TMAP;
  if (!M || !M.map) return;
  var $ = function (id) { return document.getElementById(id); };

  function load() {
    var p = M.poiQuery();
    $("counters").innerHTML = '<span class="muted small">counting…</span>';
    fetch("/api/insights?" + p.toString())
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.ok) {
          $("counters").innerHTML = '<span class="muted small">' + M.esc(d.error) + "</span>";
          return;
        }
        $("insightTotal").textContent = "· " + d.total.toLocaleString()
          + " in territory from " + d.table;
        $("counters").innerHTML = "";
        d.buckets.forEach(function (b) {
          var el = document.createElement("button");
          el.type = "button";
          el.className = "counter" + (b.available ? "" : " na");
          // "not expressible in this source" is a different fact from zero,
          // and showing it as 0 would be a quiet lie about coverage.
          el.innerHTML = '<span class="n">'
            + (b.available ? b.count.toLocaleString() : "n/a")
            + '</span><span class="l">' + M.esc(b.label) + "</span>";
          if (b.available && b.count) {
            el.addEventListener("click", function () { detail(b.key, b.label); });
          } else {
            el.classList.add("na");
            el.title = b.available
              ? "None in the current filter"
              : b.label + " cannot be expressed in this source — that is not "
                + "the same as none existing";
          }
          $("counters").appendChild(el);
        });
      })
      .catch(function (e) {
        $("counters").innerHTML = '<span class="muted small">failed: ' + M.esc(e) + "</span>";
      });
  }

  function detail(key, label) {
    var p = M.poiQuery();
    p.set("bucket", key);
    $("modalTitle").textContent = label;
    $("modalBody").innerHTML = '<p class="panel-empty muted">loading…</p>';
    $("modal").hidden = false;
    fetch("/api/insight-detail?" + p.toString())
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.ok) {
          $("modalBody").innerHTML = '<p class="panel-empty muted">' + M.esc(d.error) + "</p>";
          return;
        }
        $("modalTitle").textContent = label + " — " + d.count.toLocaleString()
          + (d.shown < d.count ? " (showing first " + d.shown.toLocaleString() + ")" : "");
        if (!d.rows.length) {
          $("modalBody").innerHTML = '<p class="panel-empty muted">Nothing matches.</p>';
          return;
        }
        var cols = Object.keys(d.rows[0]).filter(function (c) {
          return c !== "lat" && c !== "lon";
        });
        var h = "<table><thead><tr>"
          + cols.map(function (c) { return "<th>" + M.esc(c.replace(/^adm_/, "")) + "</th>"; }).join("")
          + "</tr></thead><tbody>";
        d.rows.forEach(function (r, i) {
          h += '<tr class="rowlink" data-i="' + i + '">'
            + cols.map(function (c) { return "<td>" + M.esc(r[c] == null ? "" : r[c]) + "</td>"; }).join("")
            + "</tr>";
        });
        $("modalBody").innerHTML = h + "</tbody></table>";
        // Clicking a row flies the map to it — the counter, the table and
        // the map stay one conversation rather than three.
        $("modalBody").querySelectorAll(".rowlink").forEach(function (tr) {
          tr.addEventListener("click", function () {
            var r = d.rows[Number(tr.dataset.i)];
            if (r.lat && r.lon) {
              $("modal").hidden = true;
              M.map.setView([r.lat, r.lon], 18);
            }
          });
        });
      });
  }

  $("modalClose").addEventListener("click", function () { $("modal").hidden = true; });
  $("modal").addEventListener("click", function (e) {
    if (e.target === $("modal")) $("modal").hidden = true;
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") $("modal").hidden = true;
  });

  M.onReady = load;
  M.onFilter = load;
  M.onSourceChange = load;
})();
