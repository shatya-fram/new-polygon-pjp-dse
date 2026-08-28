/* Dark / light theme.

   WHY THE ATTRIBUTE IS SET BEFORE THIS FILE RUNS
   A <script> in <head> stamps data-theme on <body> as early as the browser
   will allow. If it were left to this file, which loads at the end of the
   body, a light-mode user would get a full dark repaint first — the flash
   that makes a theme toggle feel broken. This file only owns the button and
   what happens after a click.

   WHAT "SYSTEM" MEANS HERE
   Three states, not two. Explicitly choosing dark or light stores that
   choice and it stops moving. Choosing neither — the default — follows the
   operating system, and keeps following it: a laptop that dims itself at
   sunset changes this page too, which is the behaviour people expect from an
   application and are surprised not to get. Cycling past light returns to
   system and forgets the stored choice, so the setting is escapable.

   THE BASEMAP IS PART OF THE THEME
   A pale page around near-black map tiles reads as a bug. So the toggle also
   swaps CARTO's dark_all for light_all and tells the page, which is how the
   territory polygons and the existing `lightmap` overlay rules find out. */
(function () {
  "use strict";
  var KEY = "apl.theme";                       // "dark" | "light" | absent
  var ORDER = ["system", "dark", "light"];

  function stored() {
    try { return localStorage.getItem(KEY); } catch (e) { return null; }
  }
  function store(v) {
    try {
      if (v === "system") localStorage.removeItem(KEY);
      else localStorage.setItem(KEY, v);
    } catch (e) { /* private window — the choice just will not persist */ }
  }

  var media = window.matchMedia
    ? window.matchMedia("(prefers-color-scheme: light)") : null;

  function resolved() {
    var s = stored();
    if (s === "dark" || s === "light") return s;
    return media && media.matches ? "light" : "dark";
  }

  function paint() {
    var mode = resolved();
    document.body.setAttribute("data-theme", mode);
    // The <head> boot script sets this before <body> exists, and the palette
    // keys off both. Updating it here is what stops a stale boot value from
    // outvoting a live toggle.
    document.documentElement.setAttribute("data-boot-theme", mode);
    // The Leaflet basemap and the overlay rules that assume a pale map.
    document.body.classList.toggle("lightmap", mode === "light");
    window.dispatchEvent(new CustomEvent("theme:changed",
                                         { detail: { mode: mode } }));
    label();
    return mode;
  }

  function label() {
    var b = document.getElementById("themeBtn");
    if (!b) return;
    var s = stored() || "system";
    var mode = resolved();
    b.textContent = s === "system"
      ? "Theme: auto" : (mode === "light" ? "Theme: light" : "Theme: dark");
    b.title = s === "system"
      ? "Following your system setting (" + mode + "). Click to pin a theme."
      : "Pinned to " + mode + ". Click to cycle; the third click follows your "
        + "system setting again.";
    b.setAttribute("aria-pressed", s === "system" ? "false" : "true");
  }

  function cycle() {
    var s = stored() || "system";
    store(ORDER[(ORDER.indexOf(s) + 1) % ORDER.length]);
    paint();
  }

  // A stored choice wins over the OS; with no stored choice, follow it live.
  if (media && media.addEventListener) {
    media.addEventListener("change", function () {
      if (!stored()) paint();
    });
  }

  function wire() {
    var b = document.getElementById("themeBtn");
    if (b && !b.dataset.wired) {
      b.dataset.wired = "1";
      b.addEventListener("click", cycle);
    }
    paint();
  }

  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", wire);
  else wire();

  window.APLTheme = { mode: resolved, cycle: cycle, refresh: paint };
})();
