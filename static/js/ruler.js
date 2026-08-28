/* Ruler — click a path on the map, get distances.

   WHY A SITING PAGE NEEDS ONE
   Every question this page raises is a question about distance. Is this
   candidate far enough from the outlet that already serves the area? How
   wide is the gap two sites would have to cover between them? The scale bar
   answers that to the nearest "about", and about is not good enough when the
   difference between 300 m and 800 m decides whether a site is worth
   building.

   WHAT IT MEASURES, AND WHAT THAT NUMBER MEANS
   Great-circle distance between the points you click, via Leaflet's own
   map.distance() — the same spheroid the rest of the app uses. It is a
   straight line over the ground, NOT a route: nobody walks through a
   building. Treat it as the floor on a real journey, which is exactly what
   you want when judging whether two things are too close together.

   Area appears once three points are down, by the shoelace formula on a
   local equirectangular projection anchored at the first point. Over a few
   kilometres that is accurate to well under a percent; it is not meant for
   measuring a province.

   INTERCEPTING CLICKS
   The boundary polygons underneath have their own click handlers, and
   Leaflet dispatches to a layer before it dispatches to the map. So while
   measuring, clicks are caught on the container in the CAPTURE phase and
   stopped there — otherwise every point dropped would also open the popup
   for whatever kecamatan it landed in. */
(function () {
  "use strict";
  var T = window.TMAP;
  if (!T || !T.map || !window.L) return;
  var map = T.map;

  var on = false;                 // measuring right now?
  var pts = [];                   // [L.LatLng]
  var layer = L.layerGroup().addTo(map);
  var ghost = null;               // rubber band to the cursor
  var box = null, btn = null;

  // ── formatting ─────────────────────────────────────────────────────────
  function m(v) {
    if (v < 1000) return Math.round(v) + " m";
    return (v / 1000).toFixed(v < 10000 ? 2 : 1) + " km";
  }
  function bearing(a, b) {
    var r = Math.PI / 180;
    var y = Math.sin((b.lng - a.lng) * r) * Math.cos(b.lat * r);
    var x = Math.cos(a.lat * r) * Math.sin(b.lat * r)
          - Math.sin(a.lat * r) * Math.cos(b.lat * r)
            * Math.cos((b.lng - a.lng) * r);
    var d = (Math.atan2(y, x) / r + 360) % 360;
    var names = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
    return Math.round(d) + "° " + names[Math.round(d / 45) % 8];
  }
  function total() {
    var t = 0;
    for (var i = 1; i < pts.length; i++) t += map.distance(pts[i - 1], pts[i]);
    return t;
  }
  // Shoelace on metres east/north of the first point.
  function area() {
    if (pts.length < 3) return 0;
    var lat0 = pts[0].lat * Math.PI / 180;
    var mx = 111320 * Math.cos(lat0), my = 110540;
    var s = 0;
    for (var i = 0; i < pts.length; i++) {
      var a = pts[i], b = pts[(i + 1) % pts.length];
      s += (a.lng * mx) * (b.lat * my) - (b.lng * mx) * (a.lat * my);
    }
    return Math.abs(s) / 2;
  }
  function km2(v) {
    return v < 100000 ? Math.round(v).toLocaleString() + " m²"
                      : (v / 1e6).toFixed(2) + " km²";
  }

  // ── drawing ────────────────────────────────────────────────────────────
  function redraw() {
    layer.clearLayers();
    if (pts.length > 1) {
      L.polyline(pts, { color: "#f0d04b", weight: 3, opacity: .95,
                        dashArray: "6 4", interactive: false }).addTo(layer);
    }
    pts.forEach(function (p, i) {
      L.circleMarker(p, { radius: 4.5, color: "#f0d04b", weight: 2,
                          fillColor: "#1a1400", fillOpacity: 1,
                          interactive: false }).addTo(layer);
      if (i === 0) return;
      // The leg length sits on its own vertex, so a single distance is
      // readable without counting rows in the panel.
      var d = map.distance(pts[i - 1], pts[i]);
      L.marker(p, { interactive: false, icon: L.divIcon({
        className: "ruler-label", html: m(d), iconSize: null }) }).addTo(layer);
    });
    render();
  }

  function moveGhost(latlng) {
    if (ghost) { map.removeLayer(ghost); ghost = null; }
    if (!on || !pts.length) return;
    ghost = L.polyline([pts[pts.length - 1], latlng],
      { color: "#f0d04b", weight: 1.6, opacity: .6, dashArray: "3 5",
        interactive: false }).addTo(map);
  }

  // ── the readout ────────────────────────────────────────────────────────
  function render() {
    if (!box) return;
    if (!pts.length) {
      box.innerHTML = on
        ? '<span class="rl-hint">Click to drop points. Double-click or Esc '
          + "to finish, Backspace to undo one.</span>"
        : "";
      box.classList.toggle("off", !on);
      return;
    }
    box.classList.remove("off");
    if (pts.length === 1) {
      box.innerHTML = '<span class="rl-hint">Start point set — click again '
        + "for a distance.</span>"
        + '<button type="button" class="rl-x" id="rlClear">clear</button>';
      var x1 = document.getElementById("rlClear");
      if (x1) x1.addEventListener("click", function (e) {
        e.stopPropagation();
        reset();
      });
      return;
    }
    var rows = [];
    for (var i = 1; i < pts.length; i++) {
      rows.push("<tr><td>" + i + "</td><td>"
        + m(map.distance(pts[i - 1], pts[i])) + "</td><td>"
        + bearing(pts[i - 1], pts[i]) + "</td></tr>");
    }
    box.innerHTML =
      '<div class="rl-head"><b>' + m(total()) + "</b>"
      + (pts.length > 2
         ? '<span class="rl-area">' + km2(area()) + " enclosed</span>" : "")
      + '<button type="button" class="rl-x" id="rlClear">clear</button></div>'
      + (rows.length ? '<table class="rl-tbl"><thead><tr><th>Leg</th>'
          + "<th>Distance</th><th>Bearing</th></tr></thead><tbody>"
          + rows.join("") + "</tbody></table>" : "")
      + (on ? '<span class="rl-hint">Double-click or Esc to finish.</span>'
            : "");
    var x = document.getElementById("rlClear");
    if (x) x.addEventListener("click", function (e) {
      e.stopPropagation();
      reset();
    });
  }

  function reset() {
    pts = [];
    if (ghost) { map.removeLayer(ghost); ghost = null; }
    redraw();
  }

  // ── click capture ──────────────────────────────────────────────────────
  function onClick(e) {
    if (!on) return;
    e.stopPropagation();
    e.preventDefault();
    pts.push(map.mouseEventToLatLng(e));
    redraw();
  }
  function onMove(e) {
    if (!on) return;
    moveGhost(map.mouseEventToLatLng(e));
  }
  function onDbl(e) {
    if (!on) return;
    e.stopPropagation();
    e.preventDefault();
    // A double-click is two clicks, and both have already dropped a point by
    // the time this fires — so finishing a measurement left a duplicate
    // vertex and a zero-length leg on the end of the table. Drop the second
    // and keep the first: double-clicking places the last point AND
    // finishes, which is what every other measure tool does.
    if (pts.length > 1) {
      var a = map.latLngToContainerPoint(pts[pts.length - 1]);
      var b = map.latLngToContainerPoint(pts[pts.length - 2]);
      if (Math.abs(a.x - b.x) <= 3 && Math.abs(a.y - b.y) <= 3) pts.pop();
    }
    stop();                       // the path stays on the map
    redraw();
  }
  function onKey(e) {
    if (!on) return;
    if (e.key === "Escape") { stop(); }
    else if (e.key === "Backspace") {
      e.preventDefault();
      pts.pop();
      redraw();
    }
  }

  function start() {
    if (on) return;
    // A finished measurement is still on the map. Picking the tool up again
    // means a NEW measurement, not a continuation of that one -- silently
    // extending a path from wherever it happened to end is a surprise, and
    // the old path is right there to be read while the new one is drawn.
    if (pts.length) reset();
    on = true;
    var c = map.getContainer();
    c.classList.add("measuring");
    map.doubleClickZoom.disable();
    c.addEventListener("click", onClick, true);
    c.addEventListener("dblclick", onDbl, true);
    c.addEventListener("mousemove", onMove, true);
    document.addEventListener("keydown", onKey);
    if (btn) { btn.classList.add("on"); btn.title = "Finish measuring (Esc)"; }
    render();
  }

  function stop() {
    if (!on) return;
    on = false;
    var c = map.getContainer();
    c.classList.remove("measuring");
    map.doubleClickZoom.enable();
    c.removeEventListener("click", onClick, true);
    c.removeEventListener("dblclick", onDbl, true);
    c.removeEventListener("mousemove", onMove, true);
    document.removeEventListener("keydown", onKey);
    if (ghost) { map.removeLayer(ghost); ghost = null; }
    if (btn) { btn.classList.remove("on"); btn.title = "Measure a distance"; }
    render();
  }

  // ── the control ────────────────────────────────────────────────────────
  var Ruler = L.Control.extend({
    options: { position: "topright" },
    onAdd: function () {
      var wrap = L.DomUtil.create("div",
        "leaflet-bar leaflet-control ruler-ctl");
      btn = L.DomUtil.create("a", "", wrap);
      btn.href = "#";
      btn.innerHTML = "\u{1F4CF}";
      btn.title = "Measure a distance";
      btn.setAttribute("role", "button");
      btn.setAttribute("aria-label", "Measure a distance");
      L.DomEvent.disableClickPropagation(wrap);
      L.DomEvent.on(btn, "click", function (e) {
        L.DomEvent.preventDefault(e);
        if (on) { stop(); } else { start(); }
      });
      return wrap;
    }
  });
  map.addControl(new Ruler());

  // The panel sits beside the map legend rather than floating over the map:
  // a table of legs is something you read, and reading it should not mean
  // covering the thing you just measured.
  box = document.createElement("div");
  box.className = "ruler-box off";
  box.id = "rulerBox";
  var legend = document.getElementById("legend");
  if (legend && legend.parentNode)
    legend.parentNode.insertBefore(box, legend.nextSibling);
  else map.getContainer().parentNode.appendChild(box);

  window.TMAPRuler = { start: start, stop: stop, reset: reset,
                       points: function () { return pts.slice(); } };
})();
