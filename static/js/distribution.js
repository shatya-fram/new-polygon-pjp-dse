/* Distribution Polygon: upload KML/KMZ, plot sites, outlets and polygons
   over the territory, and group an uploaded layer by any of its own fields. */
(function () {
  "use strict";
  var M = window.TMAP;
  if (!M || !M.map) return;
  var $ = function (id) { return document.getElementById(id); };
  var PALETTE = ["#ff8a3d", "#35d0e0", "#ff2fbf", "#6ec46e", "#f0d04b",
                 "#7c5cff", "#ff6b6b", "#4b96f3"];
  var layers = {};        // layer_key -> {group, meta, color, shape}
  var active = null;

  function msg(t, bad) {
    $("uploadMsg").innerHTML = t;
    $("uploadMsg").style.color = bad ? "#ef8073" : "#8b93a7";
  }

  // ── upload ────────────────────────────────────────────────────────────
  function upload(file) {
    if (!file) return;
    if (!/\.(kml|kmz)$/i.test(file.name)) {
      msg("Only .kml and .kmz files can be uploaded.", true); return;
    }
    msg("Reading " + M.esc(file.name) + " …");
    var fd = new FormData();
    fd.append("file", file);
    fetch("/api/upload-layer", { method: "POST", body: fd })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.ok) { msg(M.esc(d.error), true); return; }
        msg(d.features.toLocaleString() + " features imported as <b>"
            + M.esc(d.layer_key) + "</b>");
        return refreshLayers().then(function () { show(d.layer_key, true); });
      })
      .catch(function (e) { msg("Upload failed: " + M.esc(e), true); });
  }

  $("drop").addEventListener("click", function () { $("file").click(); });
  $("file").addEventListener("change", function (e) { upload(e.target.files[0]); });
  ["dragenter", "dragover"].forEach(function (ev) {
    $("drop").addEventListener(ev, function (e) {
      e.preventDefault(); $("drop").classList.add("hot");
    });
  });
  ["dragleave", "drop"].forEach(function (ev) {
    $("drop").addEventListener(ev, function (e) {
      e.preventDefault(); $("drop").classList.remove("hot");
    });
  });
  $("drop").addEventListener("drop", function (e) {
    upload(e.dataTransfer.files && e.dataTransfer.files[0]);
  });

  // ── layer list ────────────────────────────────────────────────────────
  function refreshLayers() {
    return fetch("/api/layers/all?domain=distribution").then(function (r) { return r.json(); })
      .catch(function (e) { return { layers: [], _err: String(e) }; })
      .then(function (d) {
        var host = $("uplist");
        if (d._err) {
          host.innerHTML = '<span class="muted small">Could not read the '
            + "layer list: " + M.esc(d._err) + "</span>";
          return;
        }
        var user = (d.layers || []).filter(function (l) { return !l.builtin; });
        if (!user.length) {
          host.innerHTML = '<span class="muted small">Nothing uploaded yet. '
            + "The kecamatan and MC boundaries load automatically.</span>";
          return;
        }
        host.innerHTML = "";
        user.forEach(function (l, i) {
          var col = PALETTE[i % PALETTE.length];
          if (!layers[l.layer_key]) layers[l.layer_key] = { color: col, shape: "diamond" };
          layers[l.layer_key].meta = l;
          var row = document.createElement("div");
          row.className = "uprow";
          row.innerHTML = '<div class="top"><input type="checkbox" id="lk_'
            + M.esc(l.layer_key) + '">' + M.swatch(layers[l.layer_key].shape, col, 12)
            + '<span class="nm" title="' + M.esc(l.source_file || "") + '">'
            + M.esc(l.label || l.layer_key) + '</span><span class="cnt">'
            + Number(l.feature_count || 0).toLocaleString() + "</span></div>"
            + '<div class="meta">' + M.esc((l.attr_fields || "").split(",").slice(0, 5).join(" · "))
            + "</div>";
          host.appendChild(row);
          row.querySelector("input").addEventListener("change", function (e) {
            if (e.target.checked) show(l.layer_key);
            else hide(l.layer_key);
          });
        });
      });
  }

  // ── request tickets ───────────────────────────────────────────────────
  // Overlapping requests are resolved by whichever the server finishes
  // last, not by whichever was asked for last. A handler takes a ticket
  // before it fetches and checks it when the answer lands, so a superseded
  // response is dropped instead of painted over the current one.
  var SEQ = {};
  function ticket(k) { SEQ[k] = (SEQ[k] || 0) + 1; return SEQ[k]; }
  function isCurrent(k, n) { return SEQ[k] === n; }

  // ── draw ──────────────────────────────────────────────────────────────
  function show(key, check) {
    var cfg = layers[key] || (layers[key] = { color: PALETTE[0], shape: "diamond" });
    active = key;
    if (check) {
      var box = $("lk_" + key);
      if (box) box.checked = true;
    }
    // Already loaded: re-adding it is free, and the two field lists it
    // needs are already populated. Fetching them again on every re-tick was
    // two requests for a picture that did not change.
    if (cfg.group) { cfg.group.addTo(M.map); M.renderLegend(); return; }
    loadFields(key);
    loadDseFields(key);
    var lbox = $("lk_" + key);
    if (lbox) lbox.disabled = true;
    var tk = ticket("layer:" + key);
    // Points and polygons arrive in the same table, so both are fetched and
    // whichever the layer actually holds gets drawn.
    Promise.all([
      fetch("/api/territory/" + key + ".geojson").then(function (r) { return r.json(); }),
      fetch("/api/layer/" + key + "/points.geojson").then(function (r) { return r.json(); })
    ]).then(function (res) {
      if (lbox) lbox.disabled = false;
      // Superseded by a later tick of the same layer, or unticked while it
      // was loading. Without the second check the layer went on to a map
      // whose checkbox was clear, and hide() could not remove it because
      // cfg.group had not been assigned when hide() ran.
      if (!isCurrent("layer:" + key, tk)) return;
      if (lbox && !lbox.checked) return;
      var g = L.featureGroup();
      var polys = (res[0].features || []).filter(function (f) {
        return f.geometry && /Polygon/.test(f.geometry.type);
      });
      if (polys.length) {
        // Popups are built when a shape is clicked, not for every shape up
        // front — a full properties table per feature is what stalled the
        // tab on the big boundary layers.
        var pl = L.geoJSON({ type: "FeatureCollection", features: polys }, {
          style: { color: cfg.color, weight: 2, opacity: .95,
                   fillColor: cfg.color, fillOpacity: .07 }
        });
        pl.on("click", function (e) {
          var lyr = e.layer;
          if (!lyr.getPopup()) lyr.bindPopup(table(lyr.feature.properties));
          lyr.openPopup();
        });
        pl.addTo(g);
      }
      var pts = (res[1].features || []).filter(function (f) {
        return f.geometry && f.geometry.type === "Point";
      });
      if (pts.length) {
        M.placeMany(pts, cfg.shape, cfg.color, 8, function (props) {
          return "<strong>" + M.esc(props.Outlet_Nam || props.name || "") + "</strong>";
        }, function (props, lyr) {
          // The collection carries only the preview and filter fields; the
          // rest of the row is fetched for the one feature being opened.
          if (props.feature_key) {
            fetch("/api/layer/" + key + "/feature/"
                  + encodeURIComponent(props.feature_key))
              .then(function (r) { return r.json(); })
              .then(function (d) {
                if (d && d.ok && lyr.getPopup()) lyr.setPopupContent(table(d.properties));
              }).catch(function () {});
          }
          return table(props);
        }).addTo(g);
      }
      cfg.group = g;
      cfg.points = pts.length;
      cfg.polys = polys.length;
      g.addTo(M.map);
      var b = g.getBounds && g.getBounds();
      // Only frame it if this is still the layer being looked at. A slow
      // layer landing after the reader has moved on used to drag the map
      // away from whatever they were reading.
      try {
        if (b && b.isValid() && active === key) M.map.fitBounds(b.pad(.05));
      } catch (e) {}
      M.renderLegend();
    }).catch(function (e) {
      if (lbox) { lbox.disabled = false; lbox.checked = false; }
      if (!isCurrent("layer:" + key, tk)) return;
      var msg = $("uploadMsg");
      if (msg) msg.textContent = key + " failed to load: " + e
        + " — tick it again to retry";
    });
  }
  function hide(key) {
    var cfg = layers[key];
    ticket("layer:" + key);      // a load in flight belongs to the old view
    if (cfg && cfg.group) M.map.removeLayer(cfg.group);
    if (active === key) {
      active = null;
      $("groupField").innerHTML = '<option value="">— pick a field —</option>';
      $("groupOut").innerHTML = "";
      dseReady(false);
      clearDse();
    }
    M.renderLegend();
  }

  function table(props) {
    var rows = Object.keys(props || {}).filter(function (k) {
      return props[k] !== "" && props[k] != null;
    }).map(function (k) {
      return "<tr><td style='color:#8b93a7;padding-right:10px'>" + M.esc(k)
        + "</td><td>" + M.esc(props[k]) + "</td></tr>";
    }).join("");
    return "<table style='font:12px sans-serif'>" + rows + "</table>";
  }

  // ── group by any field the layer carries ──────────────────────────────
  function loadFields(key) {
    var tk = ticket("fields");
    fetch("/api/layer/" + key + "/summary").then(function (r) { return r.json(); })
      .then(function (d) {
        if (!isCurrent("fields", tk)) return;
        if (!d.ok) return;
        var sel = $("groupField");
        sel.innerHTML = '<option value="">— pick a field —</option>';
        (d.fields || []).forEach(function (f) {
          var o = document.createElement("option");
          o.value = f; o.textContent = f;
          sel.appendChild(o);
        });
      })
      .catch(function () {
        if (!isCurrent("fields", tk)) return;
        $("groupField").innerHTML =
          '<option value="">— field list failed to load —</option>';
      });
  }
  $("groupField").addEventListener("change", function (e) {
    var f = e.target.value;
    if (!active || !f) { $("groupOut").innerHTML = ""; return; }
    // Say so while it loads. Leaving the previous field's tally on screen
    // under a select that already reads the new one is worse than a blank:
    // the numbers look like an answer to a question nobody asked.
    $("groupOut").innerHTML = '<div class="uprow muted small">counting '
      + M.esc(f) + "…</div>";
    var tk = ticket("group");
    fetch("/api/layer/" + active + "/summary?field=" + encodeURIComponent(f))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!isCurrent("group", tk)) return;
        if (!d.ok) {
          $("groupOut").innerHTML = '<div class="uprow muted small">'
            + M.esc(d.error || "no tally for that field") + "</div>";
          return;
        }
        $("groupOut").innerHTML = d.groups.slice(0, 40).map(function (g) {
          return '<div class="uprow"><div class="top"><span class="nm">'
            + M.esc(g.value) + '</span><span class="cnt">'
            + g.count.toLocaleString() + "</span></div></div>";
        }).join("");
      })
      .catch(function (e) {
        if (!isCurrent("group", tk)) return;
        $("groupOut").innerHTML = '<div class="uprow muted small">failed: '
          + M.esc(String(e)) + "</div>";
      });
  });

  // uploaded layers join the shared legend rather than getting their own
  M.extraLegend = [];
  var baseRender = M.renderLegend;
  M.renderLegend = function () {
    M.extraLegend = Object.keys(layers).filter(function (k) {
      return layers[k].group && M.map.hasLayer(layers[k].group);
    }).map(function (k) {
      var c = layers[k];
      var bits = [];
      if (c.points) bits.push(c.points.toLocaleString() + " pts");
      if (c.polys) bits.push(c.polys.toLocaleString() + " polys");
      return '<span class="lg">' + M.swatch(c.shape, c.color)
        + M.esc((c.meta && c.meta.label) || k)
        + (bits.length ? ' <b>' + bits.join(" · ") + "</b>" : "") + "</span>";
    });
    baseRender();
  };

  // ── colour the desa polygons by one of their own fields ───────────────
  // The GeoJSON already carries every field the pulldown offers, so tinting
  // is a restyle of the layer that is already on the map — no second fetch,
  // and no chance of the colours describing a different vintage of the data
  // from the outlines they sit on.
  var THEMES = (window.TERR && window.TERR.kelurahanThemes) || [];
  var CAT_COLORS = ["#35d0e0", "#ff8a3d", "#6ec46e", "#f0d04b", "#ff2fbf",
                    "#7c5cff", "#4b96f3", "#ff6b6b", "#c48ee0", "#3dd6a0",
                    "#e0b23d", "#8b93a7"];
  var BAND_COLORS = ["#0d3b4f", "#12617a", "#1a8fa1", "#35d0e0", "#9ae6ef",
                     "#e8fbfd"];

  function themeFor(field) {
    for (var i = 0; i < THEMES.length; i++) {
      if (THEMES[i].field === field) return THEMES[i];
    }
    return null;
  }

  function bandOf(v, breaks) {
    if (v == null || v === "") return -1;
    var n = Number(v);
    if (isNaN(n)) return -1;
    for (var i = 0; i < breaks.length; i++) { if (n < breaks[i]) return i; }
    return breaks.length;
  }

  function paintDesa(field) {
    var b = M.boundary && M.boundary.kelurahan;
    if (!b) return;
    var legend = $("desaLegend");
    if (!field) {
      b.layer.eachLayer(function (lyr) { setBase(lyr, M.bstyle(b.style)); });
      if (legend) legend.innerHTML = "";
      return;
    }
    var th = themeFor(field) || { kind: "cat" };
    var assign = {}, order = [], counts = {}, missing = 0;

    if (th.kind === "band") {
      var labels = [], br = th.breaks;
      for (var i = 0; i <= br.length; i++) {
        labels.push(i === 0 ? "< " + br[0].toLocaleString()
          : i === br.length ? "≥ " + br[br.length - 1].toLocaleString()
          : br[i - 1].toLocaleString() + " – " + br[i].toLocaleString());
      }
      b.layer.eachLayer(function (lyr) {
        var k = bandOf(lyr.feature.properties[field], br);
        if (k < 0) { missing++; return; }
        counts[k] = (counts[k] || 0) + 1;
      });
      order = labels.map(function (lab, i) {
        return { key: i, label: lab,
                 color: BAND_COLORS[Math.min(i, BAND_COLORS.length - 1)],
                 count: counts[i] || 0 };
      });
      b.layer.eachLayer(function (lyr) {
        var k = bandOf(lyr.feature.properties[field], br);
        setBase(lyr, tint(b.style, k < 0 ? null
          : BAND_COLORS[Math.min(k, BAND_COLORS.length - 1)]));
      });
    } else {
      b.layer.eachLayer(function (lyr) {
        var v = lyr.feature.properties[field];
        if (v == null || v === "") { missing++; return; }
        counts[v] = (counts[v] || 0) + 1;
      });
      Object.keys(counts).sort(function (a, c) { return counts[c] - counts[a]; })
        .forEach(function (v, i) {
          assign[v] = CAT_COLORS[i % CAT_COLORS.length];
          order.push({ key: v, label: v, color: assign[v], count: counts[v] });
        });
      b.layer.eachLayer(function (lyr) {
        var v = lyr.feature.properties[field];
        setBase(lyr, tint(b.style, assign[v] || null));
      });
    }

    if (legend) {
      legend.innerHTML = order.map(function (o) {
        return '<div class="uprow"><div class="top">'
          + M.swatch("square", o.color, 12) + '<span class="nm">'
          + M.esc(o.label) + '</span><span class="cnt">'
          + o.count.toLocaleString() + "</span></div></div>";
      }).join("") + (missing
        ? '<div class="uprow"><div class="top"><span class="nm muted">'
          + "no value</span><span class=\"cnt\">" + missing.toLocaleString()
          + "</span></div></div>" : "");
    }
  }

  function setBase(lyr, st) { lyr._baseStyle = st; lyr.setStyle(st); }

  // Exactly one DSE polygon is ever highlighted, and this is the only thing
  // that knows which. Tracking it here rather than per-layer is what makes
  // "cool whatever was hot, then heat this one" a single ordered operation.
  var hotDse = null;
  function coolDse() {
    if (!hotDse) return;
    // Restore what the shape looked like before the hover, which is not
    // always its default: with a legend row soloed, everything else is
    // dimmed, and cooling to the default would light the dimmed ones back up
    // one by one as the pointer crossed them.
    try {
      hotDse.setStyle(hotDse._dseDim || dseStyle(hotDse.feature.properties));
    } catch (e) { /* layer already removed */ }
    hotDse = null;
  }

  function tint(st, color) {
    if (!color) {
      return { color: st.color, weight: st.weight, opacity: .5,
               fillColor: st.color, fillOpacity: 0 };
    }
    // Enough fill to read the class at a glance, little enough that the
    // basemap and the POI dots on top of it stay legible. The stroke goes
    // to the same colour so 887 outlines do not turn the tint into a grid.
    return { color: color, weight: Math.max(st.weight, 1), opacity: .85,
             fillColor: color, fillOpacity: .38 };
  }

  // The desa layer is fetched on first tick, so the control only appears
  // once there is something to colour.
  M.onBoundaryLoaded = function (key) {
    if (key !== "kelurahan") return;
    var sec = $("desaTheme");
    if (sec) sec.hidden = false;
    paintDesa($("desaField") ? $("desaField").value : "");
  };
  if ($("desaField")) {
    $("desaField").addEventListener("change", function (e) {
      paintDesa(e.target.value);
    });
  }

  // ── Territory model (Distribution page only) ──────────────────────────
  // Three models over the same grouping. Nothing here reassigns an outlet:
  // the groups are whatever the chosen field says, and only the ground
  // between the outlets differs between models.
  var dseLayer = null, dseMode = "exclusive", dseTimer = null;

  var MODE_NOTE = {
    existing: "The polygons the uploaded file carries, drawn as supplied. "
            + "Nothing is inferred.",
    exclusive: "Derived. Ground reached by two reps goes to the nearer one, "
             + "so the shapes tile instead of overlapping. Recommended.",
    coverage: "Derived. Each rep's reach drawn independently, so shapes "
            + "overlap where reps work the same ground — the overlap is "
            + "itself the finding.",
    forcefit: "Derived, then rebalanced. Territories are whole desa, moved "
            + "between neighbouring reps until each sits inside the workload "
            + "norms — urban 1–3 desa / 50–70 outlets, rural 1–5 / 50–60. "
            + "Moves as few desa as it can and reports every one."
  };

  function $v(id) { var e = $(id); return e ? e.value : null; }

  // Swap the panel body, never the panel itself. Hiding the whole section
  // takes its sidebar tab down with it (sidetabs.js drops a tab whose
  // sections are all hidden), so the Territory tab appeared and vanished
  // depending on whether a layer happened to be ticked.
  function dseReady(on, why) {
    var body = $("dseBody"), empty = $("dseNeedsLayer");
    if (body) body.hidden = !on;
    if (empty) {
      empty.hidden = !!on;
      if (!on && why) empty.innerHTML = why;
    }
  }

  function dseMsg(t, bad) {
    var el = $("dseMsg");
    if (!el) return;
    el.innerHTML = t || "";
    el.style.color = bad ? "#ef8073" : "#8b93a7";
  }

  function fillOpacity() { return (parseInt($v("dseFill"), 10) || 0) / 100; }

  function dseStyle(p) {
    return { color: p.colour, weight: 1.6, opacity: .95,
             fillColor: p.colour, fillOpacity: fillOpacity() };
  }

  function setReadout(fc) {
    var box = $("dseReadout");
    if (!box) return;
    if (!fc) { box.hidden = true; return; }
    box.hidden = false;
    $("roPolys").textContent = fc.drawn != null ? fc.drawn : "—";
    $("roArea").textContent = fc.area_km2 != null ? fc.area_km2 : "—";
    $("roOverlap").textContent =
      fc.overlap_pct != null ? fc.overlap_pct + "%" : "—";
    $("roSeam").textContent = fc.seam != null ? fc.seam : "—";
  }

  function clearDse(keepMsg) {
    hotDse = null;               // it is about to stop existing
    if (dseLayer) { M.map.removeLayer(dseLayer); dseLayer = null; }
    if ($("dseLegend")) $("dseLegend").innerHTML = "";
    setReadout(null);
    if (!keepMsg) dseMsg("");
    M.renderLegend();
  }

  function syncControls() {
    var derived = dseMode !== "existing";
    var wrap = document.querySelector("#dseCfg .derived-only");
    if (wrap) wrap.classList.toggle("off", !derived);
    var note = $("dseModeNote");
    if (note) note.textContent = MODE_NOTE[dseMode] || "";
    $("dseModes").querySelectorAll("button").forEach(function (b) {
      var on = b.dataset.mode === dseMode;
      b.classList.toggle("on", on);
      b.setAttribute("aria-checked", on ? "true" : "false");
    });
  }

  // With no layer ticked, fall back to the hierarchy: the same model, drawn
  // across every uploaded outlet export at once and filtered by the
  // territory bar. This is what makes "Go to area" from Preview Polygon land
  // on a working territory model instead of an empty one.
  function terrFilters() {
    return (M.territoryFilters && M.territoryFilters()) || {};
  }
  function hasFilter() {
    var f = terrFilters();
    return Object.keys(f).some(function (k) { return f[k]; });
  }

  function drawDse() {
    if (!active) { return hasFilter() ? drawFromHierarchy()
      : dseMsg("Tick an uploaded layer on the Layers tab, or pick an area in "
               + "the territory bar above the map.", true); }
    var field = $v("dseField");
    if (!field) { dseMsg("No field on this layer looks like a rep code.", true); return; }
    var reach = $v("dseReach"), cell = $v("dseCell");
    dseMsg("Building…");
    if (dseMode === "forcefit" && hasFilter()) return drawFromHierarchy();
    var q = "layer=" + encodeURIComponent(active)
          + "&field=" + encodeURIComponent(field)
          + "&mode=" + dseMode + "&reach=" + reach + "&cell=" + cell;
    // The mode is captured with the request, not read live when the answer
    // arrives, so a superseded build cannot label its own geometry with the
    // mode the reader has since switched to.
    var tk = ticket("dse"), mode = dseMode;
    fetch("/api/distribution/dse-coverage.geojson?" + q)
      .then(function (r) { return r.json(); })
      .then(function (fc) {
        if (!isCurrent("dse", tk)) return;
        if (fc.ok === false) { clearDse(true); dseMsg(M.esc(fc.error), !fc.empty); return; }
        paint(fc, reach, null, mode);
      })
      .catch(function (e) {
        if (isCurrent("dse", tk)) dseMsg("Failed: " + M.esc(e), true);
      });
  }

  // The territory bar announces a change; the model follows it. Only when
  // something is already drawn or auto-redraw is on — arriving on the page
  // and picking an area should not start a build nobody asked for.
  var terrTimer = null;
  window.addEventListener("terr:filters", function () {
    if (!dseLayer && !($("dseAuto") && $("dseAuto").checked)) return;
    if (!active && !hasFilter()) return;
    clearTimeout(terrTimer);
    terrTimer = setTimeout(drawDse, 200);
  });

  // One renderer for both sources: a ticked layer and a hierarchy
  // selection produce the same FeatureCollection, so they must produce
  // the same map, legend and readout.
  function paint(fc, reach, source, mode) {
    mode = mode || dseMode;
        clearDse(true);
    dseLayer = L.geoJSON(fc, {
      style: function (f) { return dseStyle(f.properties); },
      onEachFeature: function (f, lyr) {
        var p = f.properties;
        var bits = ["<strong>" + M.esc(p.dse) + "</strong>"];
        if (p.outlets != null) bits.push(p.outlets.toLocaleString() + " outlets");
        if (p.area_km2 != null) bits.push(p.area_km2 + " km²");
        if (p.parts > 1) bits.push(p.parts + " separate areas");
        bits.push("<span style='opacity:.7'>"
          + (p.derived ? "inferred from outlet positions" : "as uploaded")
          + "</span>");
        lyr.bindTooltip(bits.join("<br>"), { sticky: true, className: "terr-tip" });
        lyr.on("mouseover", function () {
          // Cool the previous shape FIRST. Per-layer mouseout is not enough:
          // crossing a shared border fires the neighbour's mouseover before
          // the old shape's mouseout, so the shape you left kept its
          // highlight and its tooltip appeared to belong to the new one.
          coolDse();
          hotDse = lyr;
          lyr.setStyle({ weight: 3.4,
                         fillOpacity: Math.min(.55, fillOpacity() + .18) });
        });
        lyr.on("mouseout", coolDse);
      }
    }).addTo(M.map);
    // Leaving the map entirely never fires a polygon mouseout at all, which
    // is the other way a highlight used to get stranded.
    M.map.off("mouseout", coolDse).on("mouseout", coolDse);

    setReadout(fc);
    var agg = {};
    (fc.features || []).forEach(function (f) {
      var p = f.properties;
      var a = agg[p.dse] || (agg[p.dse] = { n: 0, colour: p.colour, outlets: p.outlets });
      a.n += 1;
    });
    var keys = Object.keys(agg).sort(function (a, b) {
      return (agg[b].outlets || agg[b].n) - (agg[a].outlets || agg[a].n);
    });
    $("dseLegend").innerHTML = keys.map(function (k) {
      var a = agg[k];
      return '<div class="dse-row" data-dse="' + M.esc(k) + '">'
        + '<i class="sw" style="border-top-color:' + a.colour + '"></i>'
        + '<span class="nm">' + M.esc(k) + "</span>"
        + '<span class="cnt">' + (a.outlets != null ? a.outlets : a.n)
        + "</span></div>";
    }).join("");
    $("dseLegend").querySelectorAll(".dse-row").forEach(function (row) {
      row.addEventListener("click", function () {
        var pick = row.dataset.dse;
        var solo = !row.classList.contains("active");
        $("dseLegend").querySelectorAll(".dse-row").forEach(function (r) {
          r.classList.toggle("dim", solo && r.dataset.dse !== pick);
          r.classList.toggle("active", solo && r.dataset.dse === pick);
        });
        coolDse();
        dseLayer.eachLayer(function (lyr) {
          var p = lyr.feature.properties;
          var dim = solo && p.dse !== pick
            ? { opacity: .08, fillOpacity: 0, weight: 1 } : null;
          lyr._dseDim = dim;     // what a hover must cool back to
          lyr.setStyle(dim || dseStyle(p));
        });
      });
    });

    // ONE tail, not two. There were two near-identical blocks here and both
    // called dseMsg, so the first message was computed and immediately
    // overwritten -- and they disagreed: one excluded force-fit from the
    // reach tail, the other named the source and formatted the seam count.
    // This is the union of what each got right.
    var skipped = (fc.skipped || []).length;
    var tail = [];
    if (mode !== "existing" && mode !== "forcefit")
      tail.push(reach + " m reach");
    if (source) tail.push(source);
    if (fc.layers_used) tail.push("from " + fc.layers_used.length
      + " of " + fc.layers_available + " uploaded exports");
    if (fc.forcefit) {
      var ff = fc.forcefit;
      tail.push("<b>" + ff.moved + "</b> desa moved of " + ff.total_desa);
      tail.push(ff.in_norm + " of " + ff.dse + " reps inside the norms");
    }
    if (skipped) tail.push(skipped + " skipped (under 3 outlets)");
    // Specks folded into the rep around them, so the shapes on screen are
    // the ones worth reading. Said out loud rather than tidied away.
    if (fc.islands_absorbed)
      tail.push("<b>" + fc.islands_absorbed + "</b> sliver"
        + (fc.islands_absorbed === 1 ? "" : "s") + " merged into the "
        + "surrounding rep");
    if (fc.islands_kept)
      tail.push(fc.islands_kept + " left standing alone (no neighbour to join)");
    var seam = "";
    if (fc.seam) {
      // Not an error. Those outlets sit nearer a neighbour's outlets than
      // their own rep's -- interleaving in the territory, worth seeing.
      seam = "<br><b>" + fc.seam.toLocaleString() + "</b> outlet"
           + (fc.seam === 1 ? "" : "s") + " on a seam — closer to a "
           + "neighbouring rep's outlets than to their own.";
    }
    dseMsg(fc.drawn + " of " + fc.groups + " " + M.esc(fc.field) + " drawn"
           + (tail.length ? " · " + tail.join(" · ") : "") + seam
           + "<br>Click a row below to isolate one.");
    M.renderLegend();
  }

  function drawFromHierarchy() {
    var f = terrFilters();
    var reach = $v("dseReach"), cell = $v("dseCell");
    var q = ["mode=" + dseMode, "reach=" + reach, "cell=" + cell];
    Object.keys(f).forEach(function (k) {
      if (f[k]) q.push(k + "=" + encodeURIComponent(f[k]));
    });
    dseMsg("Building from the territory selection…");
    var tk = ticket("dse"), mode = dseMode;
    fetch("/api/preview/territory.geojson?" + q.join("&"))
      .then(function (r) { return r.json(); })
      .then(function (fc) {
        if (!isCurrent("dse", tk)) return;
        if (fc.ok === false) { clearDse(true); dseMsg(M.esc(fc.error), !fc.empty); return; }
        paint(fc, reach, "territory selection", mode);
      })
      .catch(function (e) {
        if (isCurrent("dse", tk)) dseMsg("Failed: " + M.esc(e), true);
      });
  }

  function autoDraw() {
    if (!$("dseAuto") || !$("dseAuto").checked) return;
    clearTimeout(dseTimer);
    dseTimer = setTimeout(drawDse, 350);
  }

  function loadDseFields(key) {
    var tk = ticket("dsefields");
    fetch("/api/distribution/dse-fields?layer=" + encodeURIComponent(key))
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!isCurrent("dsefields", tk)) return;
        var sel = $("dseField");
        if (!sel) return;
        if (!d.ok || !(d.fields || []).length) {
          // The layer loaded fine, it just has no field that could name a
          // rep. Say which layer and why, rather than showing the generic
          // "load a layer" prompt over a layer that is already loaded.
          dseReady(false, "<b>Nothing to group by.</b> No attribute on <b>"
            + M.esc(key) + "</b> looks like a rep or territory code — a "
            + "territory needs a field with between 2 and a few dozen "
            + "distinct values.");
          return;
        }
        sel.innerHTML = d.fields.map(function (f) {
          return '<option value="' + M.esc(f.field) + '"'
            + (f.suggested ? " selected" : "") + ">" + M.esc(f.field)
            + " (" + f.distinct + ")</option>";
        }).join("");
        dseReady(true);
        syncControls();
      })
      .catch(function (e) {
        if (!isCurrent("dsefields", tk)) return;
        dseReady(false, "Could not read the fields on <b>" + M.esc(key)
          + "</b>: " + M.esc(String(e)));
      });
  }

  if ($("dseModes")) {
    $("dseModes").addEventListener("click", function (e) {
      var b = e.target.closest("button[data-mode]");
      if (!b || b.dataset.mode === dseMode) return;
      dseMode = b.dataset.mode;
      syncControls();
      if ($("dseAuto").checked || dseLayer) drawDse();
    });
    $("dseDraw").addEventListener("click", drawDse);
    $("dseOff").addEventListener("click", function () {
    clearTimeout(dseTimer); clearTimeout(terrTimer);
    ticket("dse");            // a build in flight belongs to the cleared view
    clearDse();
  });
    $("dseField").addEventListener("change", autoDraw);
    $("dseReach").addEventListener("input", function (e) {
      $("dseReachVal").textContent = e.target.value + " m"; autoDraw();
    });
    $("dseCell").addEventListener("input", function (e) {
      $("dseCellVal").textContent = e.target.value + " m"; autoDraw();
    });
    // Opacity is pure styling — restyle in place rather than refetch.
    $("dseFill").addEventListener("input", function (e) {
      $("dseFillVal").textContent = e.target.value + "%";
      if (dseLayer) dseLayer.eachLayer(function (lyr) {
        lyr.setStyle(dseStyle(lyr.feature.properties)); });
    });
    syncControls();
  }

  // Arriving from "Go to area" on Preview Polygon: the territory bar carries
  // a selection, so the model can draw immediately with nothing ticked.
  (function () {
    var qs = new URLSearchParams(window.location.search);
    var keys = ["area", "branch", "mc", "kabkot", "kecamatan"];
    if (!keys.some(function (k) { return qs.get(k); })) return;
    var what = keys.map(function (k) { return qs.get(k); }).filter(Boolean)[0];
    dseReady(true);
    if ($("dseField") && !$("dseField").options.length) {
      $("dseField").innerHTML = '<option value="DSE_CODE" selected>DSE_CODE</option>';
    }
    syncControls();
    dseMsg("Ready to draw <b>" + M.esc(what) + "</b> from every uploaded "
      + "outlet export — no layer needs ticking. Press <b>Draw</b>.");
    setTimeout(function () { if ($("dseAuto") && $("dseAuto").checked) drawDse(); }, 900);
  })();

  refreshLayers();
})();

/* Layer folder — the recurring boundary and overlay files, one click away.
   They change quarterly, not per session, so dragging them onto the page
   every time was the wrong shape of work. */
(function () {
  "use strict";
  var M = window.TMAP;
  var host = document.getElementById("localList");
  if (!M || !host) return;
  var esc = M.esc;

  function load() {
    return fetch("/api/local-layers").then(function (r) { return r.json(); })
      .catch(function (e) { return { ok: false, error: String(e), files: [] }; })
      .then(function (d) {
        var dir = document.getElementById("localDir");
        if (dir) dir.textContent = d.ok
          ? "Reading " + d.dir.replace(/^.*\//, "") + "/ — drop new exports there."
          : "Layer folder unreadable: " + (d.error || "");
        if (!d.ok || !d.files.length) {
          host.innerHTML = '<span class="muted small">No .kml or .kmz in the layer folder.</span>';
          return;
        }
        host.innerHTML = d.files.map(function (f) {
          return '<div class="uprow"><div class="top">'
            + '<span class="dot" style="background:' + f.colour + '"></span>'
            + '<span class="nm" title="' + esc(f.file) + '">' + esc(f.label) + "</span>"
            + '<span class="cnt">' + (f.loaded ? (f.features || 0).toLocaleString()
                : Math.round(f.bytes / 1024) + " KB") + "</span></div>"
            + '<div class="meta">' + esc(f.file) + " &middot; "
            + (f.loaded ? "loaded as <b>" + esc(f.layer_key) + "</b>"
                        : '<a href="#" class="imp" data-file="' + esc(f.file) + '">import now</a>')
            + "</div></div>";
        }).join("");
        host.querySelectorAll("a.imp").forEach(function (a) {
          a.addEventListener("click", function (e) {
            e.preventDefault();
            if (a.dataset.busy === "1") return;   // one import per click
            a.dataset.busy = "1";
            a.textContent = "importing…";
            var fd = new FormData(); fd.append("file", a.dataset.file);
            fetch("/api/local-layers/import", { method: "POST", body: fd })
              .then(function (r) { return r.json(); })
              .then(function (d2) {
                a.dataset.busy = "";
                if (!d2.ok) { a.textContent = "failed: " + d2.error
                  + " — click to retry"; return; }
                load();
                if (window.location.reload) window.location.reload();
              })
              .catch(function (err) {
                // Without this the link reads "importing…" for the rest of
                // the session and there is no way to try again.
                a.dataset.busy = "";
                a.textContent = "import failed: " + err + " — click to retry";
              });
          });
        });
      });
  }
  load();
})();
