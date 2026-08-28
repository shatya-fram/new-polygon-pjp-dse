/* Sidebar tabs — one configuration group at a time.

   Every page had grown a single tall column of controls that you had to
   scroll to reach the bottom of, which is what made them feel scattered:
   the thing you wanted was usually off-screen, and nothing told you what
   else was there. This groups the sections into tabs and keeps the page's
   primary action pinned where it can always be pressed.

   Templates opt in by putting data-group="Name" on a section. Nothing else
   is required — the strip, the scroll container and the persistence all
   come from here, so adding a section to a page is still one <section>.

   Sections carrying no data-group are always visible, which is how the
   sticky action footer stays put while the tabs change above it. */
window.SIDETABS = (function () {
  "use strict";

  function key(host) {
    return "sidetab:" + window.location.pathname + ":" + (host.dataset.tabkey || "main");
  }

  function build(host) {
    if (!host || host.dataset.tabbed === "1") return null;
    var groups = [], sections = [];
    Array.prototype.forEach.call(host.children, function (el) {
      if (el.classList.contains("side-run")) return;
      if (!el.dataset || !el.dataset.group) return;
      sections.push(el);
      if (groups.indexOf(el.dataset.group) < 0) groups.push(el.dataset.group);
    });
    if (groups.length < 2) return null;
    host.dataset.tabbed = "1";

    var strip = document.createElement("nav");
    strip.className = "side-tabs";
    strip.setAttribute("role", "tablist");

    var scroll = document.createElement("div");
    scroll.className = "side-scroll";
    // Move, do not clone: a cloned node loses every listener the page
    // scripts have already attached to the controls inside it.
    Array.prototype.slice.call(host.children).forEach(function (el) {
      if (el.classList.contains("side-run")) return;
      scroll.appendChild(el);
    });
    var run = host.querySelector(".side-run");
    host.insertBefore(strip, host.firstChild);
    host.insertBefore(scroll, run || null);

    var api = { host: host, groups: groups, sections: sections, active: null };

    function available(g) {
      return sections.some(function (s) {
        return s.dataset.group === g && !s.hasAttribute("hidden");
      });
    }

    function show(g) {
      if (!available(g)) {
        var first = groups.filter(available)[0];
        if (!first) return;
        g = first;
      }
      api.active = g;
      sections.forEach(function (s) {
        s.classList.toggle("sg-off", s.dataset.group !== g);
      });
      strip.querySelectorAll(".sidetab").forEach(function (b) {
        b.classList.toggle("on", b.dataset.g === g);
        b.setAttribute("aria-selected", b.dataset.g === g ? "true" : "false");
      });
      scroll.scrollTop = 0;
      try { window.localStorage.setItem(key(host), g); } catch (e) { /* private mode */ }
    }
    api.show = show;

    groups.forEach(function (g) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "sidetab";
      b.dataset.g = g;
      b.textContent = g;
      b.setAttribute("role", "tab");
      b.addEventListener("click", function () { show(g); });
      strip.appendChild(b);
    });

    function refresh() {
      var anyShown = false;
      strip.querySelectorAll(".sidetab").forEach(function (b) {
        var ok = available(b.dataset.g);
        // Assign only on a real change. Writing `hidden` unconditionally
        // mutates an attribute the observer below is watching for, and the
        // observer's own callback is what does the writing -- which is an
        // infinite loop that locks the tab up, not a slow render.
        if (b.hidden === ok) b.hidden = !ok;
        if (ok) anyShown = true;
      });
      if (anyShown && !available(api.active)) show(groups.filter(available)[0]);
    }
    api.refresh = refresh;

    // A section that the page reveals later (the territory model appears
    // only once a polygon layer is loaded) must grow its tab at that
    // moment, not on the next reload.
    //
    // Watch the sections, never the tab strip: the strip is what refresh()
    // writes to, so observing it would feed the observer its own output.
    if (window.MutationObserver) {
      new window.MutationObserver(refresh).observe(scroll, {
        subtree: true, attributes: true, attributeFilter: ["hidden"]
      });
    }

    var saved = null;
    try { saved = window.localStorage.getItem(key(host)); } catch (e) { saved = null; }
    show(saved && groups.indexOf(saved) >= 0 ? saved : groups[0]);
    refresh();
    return api;
  }

  // Declared before init can possibly run. Writing the result to
  // window.SIDETABS instead would depend on this IIFE having already
  // returned, which is not guaranteed at the moment DOMContentLoaded
  // fires -- and when it is not, init throws and no page gets tabs.
  var API = { build: build, init: init, panels: [] };

  function init() {
    var out = [];
    document.querySelectorAll(".terr-side, .layout > .panel").forEach(function (h) {
      var a = build(h);
      if (a) out.push(a);
    });
    API.panels = out;
    return out;
  }

  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", init);
  else
    init();

  return API;
})();
