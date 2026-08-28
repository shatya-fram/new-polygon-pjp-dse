/* THE GROUND UNDER THE POLYGONS

   WHY THIS FILE EXISTS
   The pages drew on CARTO's public basemap with no key. CARTO still serves
   those tiles, but anonymously they are rate limited per referrer, and once
   the allowance is spent the answer is not an error -- it is a valid 200
   PNG with "API KEY REQUIRED" printed across it. Leaflet cannot tell that
   from a map, so nothing fails, no handler fires, and the reader is left
   looking at a grid of notices where the city should be. Reloading the same
   heavy view a few times is enough to trip it.

   That failure mode cannot be detected from the browser -- the placeholder
   is a well-formed image of the right size -- so it is designed around
   rather than caught: the default provider is one that needs no key, and
   the reader can switch provider in one click when a ground stops working.

   RETINA COSTS FOUR TILES' WORTH OF ALLOWANCE FOR ONE TILE'S WORTH OF MAP
   Leaflet asks for @2x tiles on a retina screen. They are four times the
   pixels and count the same against any quota, which is what makes an
   anonymous basemap trip so quickly. Retina is therefore opt-in here, not
   automatic.

   NONE IS A REAL CHOICE
   The design these pages were drawn from had no basemap at all -- pale
   ground, ink boundaries. On a slow link, or behind a proxy that eats tile
   CDNs, it is also the only ground that always works. */
(function () {
  "use strict";
  if (typeof L === "undefined") return;

  var KEY = "apl.basemap";
  var cfg = (window.TERRCFG && window.TERRCFG.basemap) || {};

  // Esri's canvas basemaps ship the labels as a second layer, so a provider
  // is a list of URLs rather than one.
  var P = {
    gray: {
      label: "Light grey", attr: "Tiles &copy; Esri", max: 16, retina: false,
      urls: ["https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/"
             + "World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}",
             "https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/"
             + "World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}"]
    },
    osm: {
      label: "OpenStreetMap", max: 19, retina: false,
      attr: "&copy; OpenStreetMap contributors",
      urls: ["https://tile.openstreetmap.org/{z}/{x}/{y}.png"]
    },
    carto: {
      label: "CARTO light", max: 19, retina: true, key: true,
      attr: "&copy; OpenStreetMap &copy; CARTO",
      urls: ["https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png"]
    },
    carto_dark: {
      label: "CARTO dark", max: 19, retina: true, key: true,
      attr: "&copy; OpenStreetMap &copy; CARTO",
      urls: ["https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png"]
    },
    none: { label: "None — plain ground", urls: [] }
  };

  function saved() {
    try {
      var v = localStorage.getItem(KEY);
      return (v && P[v]) ? v : null;
    } catch (e) { return null; }        // private window: the default stands
  }
  function remember(v) {
    try { localStorage.setItem(KEY, v); } catch (e) { /* nothing to do */ }
  }

  function attach(map) {
    var layers = [];
    var name = saved() || (P[cfg.provider] ? cfg.provider : "gray");

    function clear() {
      layers.forEach(function (l) { map.removeLayer(l); });
      layers = [];
    }
    function paint() {
      clear();
      var p = P[name];
      if (!p || !p.urls.length) return;
      p.urls.forEach(function (u, i) {
        // A key is appended only where the provider takes one; without it
        // the anonymous allowance applies and the ground may go blank.
        if (p.key && cfg.carto_key) u += "?api_key=" + encodeURIComponent(cfg.carto_key);
        layers.push(L.tileLayer(u, {
          maxZoom: p.max || 19,
          // Retina only where the provider is paid for: see the note above.
          detectRetina: !!(p.retina && (cfg.carto_key || p.key !== true)),
          attribution: i === 0 ? p.attr : null,
          crossOrigin: true
        }).addTo(map));
      });
    }
    paint();

    return {
      get name() { return name; },
      set: function (v) {
        if (!P[v] || v === name) return;
        name = v; remember(v); paint();
      },
      mount: function (el, cls) {
        if (!el) return;
        var sel = document.createElement("select");
        sel.className = cls || "";
        sel.title = "The ground under the polygons";
        Object.keys(P).forEach(function (k) {
          var o = document.createElement("option");
          o.value = k; o.textContent = P[k].label;
          if (k === name) o.selected = true;
          sel.appendChild(o);
        });
        sel.addEventListener("change", function () {
          name = sel.value; remember(name); paint();
        });
        el.appendChild(sel);
        return sel;
      }
    };
  }

  window.PVBASE = { providers: P, attach: attach };
})();
