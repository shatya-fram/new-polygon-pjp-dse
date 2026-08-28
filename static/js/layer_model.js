/* Layer Model page — the stack as it actually stands.

   Everything here is drawn from /api/layer-model, which reports what the
   database holds against the declared stack. Nothing is hard-coded: if a
   tier is empty the tier still draws, labelled empty. */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  var TIER_TITLE = {
    analysis: "Analysis — stamped on top",
    base: "Base — the geography",
    reference: "Reference — quoted, never drawn"
  };

  function paintPlane(t) {
    var plane = el("div", "lm-plane " + t.tier);
    var head = el("div", "lm-planehead");
    head.appendChild(el("h2", null, TIER_TITLE[t.tier] || t.tier));
    head.appendChild(el("span", "muted small", t.blurb));
    head.appendChild(el("span", "muted small",
      t.loaded + " of " + t.total + " loaded"));
    plane.appendChild(head);

    var items = el("div", "lm-items");
    t.items.forEach(function (i) {
      var card = el("div", "lm-item" + (i.loaded ? "" : " off"));
      card.appendChild(el("b", null, i.label));
      var n = el("span", "n", i.loaded ? i.rows.toLocaleString() : "—");
      card.appendChild(n);
      card.appendChild(el("span", "l", i.loaded
        ? (i.mode === "reference" ? "rows" : "features")
          + (i.files > 1 ? " · " + i.files + " files" : "")
        : "not loaded"));
      card.appendChild(el("div", "f", i.fields));
      card.title = i.why;
      items.appendChild(card);
    });
    plane.appendChild(items);
    return plane;
  }

  function paint(d) {
    var byTier = {};
    d.tiers.forEach(function (t) { byTier[t.tier] = t; });

    // Top of the screen is top of the pile — the same order the map draws.
    var stack = $("stack");
    stack.innerHTML = "";
    ["analysis", "base"].forEach(function (k) {
      if (byTier[k]) stack.appendChild(paintPlane(byTier[k]));
    });

    var ref = byTier.reference;
    var refBox = $("refList");
    refBox.innerHTML = "";
    if (ref) {
      ref.items.forEach(function (i) {
        var row = el("div", "lm-item" + (i.loaded ? "" : " off"));
        row.appendChild(el("b", null, i.label));
        row.appendChild(el("span", "n", i.loaded ? i.rows.toLocaleString() : "—"));
        row.appendChild(el("span", "l", i.loaded ? "rows" : "not loaded"));
        row.appendChild(el("div", "f", i.fields));
        row.title = i.why;
        refBox.appendChild(row);
      });
    }

    var loaded = 0, total = 0;
    d.tiers.forEach(function (t) { loaded += t.loaded; total += t.total; });
    var score = $("score");
    score.innerHTML = "";
    [[loaded + " / " + total, "inputs loaded"],
     [String(d.unclaimed.length), "unclaimed layers"]].forEach(function (p) {
      var c = el("div", "counter");
      c.appendChild(el("span", "n", p[0]));
      c.appendChild(el("span", "l", p[1]));
      score.appendChild(c);
    });

    var rules = $("rules");
    rules.innerHTML = "";
    d.rules.forEach(function (r) {
      var box = el("div", "lm-rule");
      box.appendChild(el("div", "n", r.n));
      box.appendChild(el("b", null, r.title));
      box.appendChild(el("p", null, r.body));
      rules.appendChild(box);
    });

    // Draw order reads bottom-up, the way the map is painted: basemap first.
    var rail = $("order");
    rail.innerHTML = "";
    var seq = [{ label: "Basemap", on: true, over: false }];
    (byTier.base ? byTier.base.items : []).forEach(function (i) {
      seq.push({ label: i.label, on: i.loaded, over: false });
    });
    seq.push({ sep: true });
    (byTier.analysis ? byTier.analysis.items : []).forEach(function (i) {
      seq.push({ label: i.label, on: i.loaded, over: true });
    });
    seq.push({ label: "DSE territory model", on: true, over: true });
    var n = 0;
    seq.forEach(function (s) {
      if (s.sep) { rail.appendChild(el("span", "lm-chip sep", "——")); return; }
      n += 1;
      rail.appendChild(el("span",
        "lm-chip" + (s.over ? " over" : "") + (s.on ? "" : " off"),
        n + " · " + s.label));
    });

    if (d.unclaimed.length) {
      $("extraBox").hidden = false;
      var box = $("extra");
      box.innerHTML = "";
      d.unclaimed.forEach(function (u) {
        var row = el("div", "cfg-file");
        var nm = el("span", "nm", u.label || u.layer_key);
        nm.title = u.source_file || "";
        row.appendChild(nm);
        row.appendChild(el("span", "tag", u.kind || "layer"));
        row.appendChild(el("span", "n", (u.features || 0).toLocaleString()));
        box.appendChild(row);
      });
    }
  }

  fetch("/api/layer-model")
    .then(function (r) { return r.json(); })
    .then(function (d) {
      if (!d.ok) throw new Error(d.error || "could not read the stack");
      paint(d);
    })
    .catch(function (e) {
      $("stack").innerHTML = '<span class="muted small">' + e.message + "</span>";
    });
})();
