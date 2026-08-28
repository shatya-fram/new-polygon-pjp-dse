/* Configuration page.

   The page is meaningful before this file runs: the requirement is rendered
   server-side from the same list the API reports against, so a reader with
   no JavaScript still sees what the application needs. This only fills in
   what is actually loaded. */
(function () {
  "use strict";

  var $ = function (id) { return document.getElementById(id); };
  var picked = null;      // the File waiting to go up

  // Uploader names, branch names and file names are typed by people and go
  // into innerHTML, so they are escaped rather than trusted.
  function esc(v) {
    return String(v == null ? "" : v).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;",
               '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function num(v) {
    return Number(v || 0).toLocaleString();
  }
  var slot = $("slot");

  function fmtBytes(n) {
    if (!n) return "";
    var u = ["B", "KB", "MB", "GB"], i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i += 1; }
    return (i ? n.toFixed(1) : n) + " " + u[i];
  }

  function fmtWhen(s) {
    if (!s) return "";
    // Stored UTC, read locally. A timestamp that silently means "somewhere
    // else" is worse than no timestamp.
    var d = new Date(s);
    return isNaN(d) ? s : d.toLocaleString(undefined,
      { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  }

  function paintSlots(slots) {
    slots.forEach(function (s) {
      var chip = document.querySelector('.cfg-slotcount[data-slot="' + s.key + '"]');
      if (!chip) return;
      chip.textContent = s.loaded + " of " + s.total + " loaded";
      chip.className = "cfg-slotcount" +
        (s.complete ? " ok" : (s.loaded ? " part" : ""));
    });
  }

  function paintCard(s) {
    var card = document.querySelector('.cfg-card[data-key="' + s.key + '"]');
    if (!card) return;
    card.classList.toggle("loaded", !!s.loaded);
    card.classList.toggle("missing", !s.loaded);
    card.querySelector(".cfg-dot").setAttribute(
      "data-state", s.loaded ? "loaded" : "missing");

    var pill = card.querySelector(".cfg-state");
    pill.textContent = s.loaded ? "loaded" : "not loaded";
    pill.className = "cfg-state pill " + (s.loaded ? "ok" : "off");

    var stat = card.querySelector(".cfg-stat");
    if (!s.loaded) {
      stat.textContent = "Nothing loaded for this layer yet.";
      return;
    }
    var bits = [];
    if (s.handler === "ncp") {
      bits.push(s.rows.toLocaleString() + " desa");
      if (s.extra && s.extra.kecamatan) {
        bits.push(s.extra.kecamatan.toLocaleString() + " kecamatan rolled up");
      }
      if (s.extra && s.extra.figures) {
        bits.push(s.extra.figures.toLocaleString() + " figures");
      }
    } else {
      bits.push(s.features.toLocaleString() + " features");
      if (s.extra && s.extra.ref_site) {
        bits.push(s.extra.ref_site.toLocaleString() + " in ref_site");
      }
      if (s.files.length > 1) bits.push(s.files.length + " files");
      if (s.last) bits.push("imported " + fmtWhen(s.last));
    }
    stat.textContent = bits.join(" · ");
  }

  function paintDerive(d) {
    var card = document.querySelector('.cfg-derive[data-step="' + d.key + '"]');
    if (!card) return;
    card.classList.toggle("done", !!d.done);
    card.classList.toggle("blocked", !d.can_run && !d.done);
    card.querySelector(".cfg-dot").setAttribute(
      "data-state", d.done ? "loaded" : (d.can_run ? "missing" : "unknown"));

    var pill = card.querySelector(".cfg-state");
    pill.textContent = d.done ? "derived" : (d.can_run ? "not run yet" : "waiting");
    pill.className = "cfg-state pill " + (d.done ? "ok" : "off");

    card.querySelector(".cfg-stat").textContent = d.done
      ? d.rows.toLocaleString() + " " + (d.unit || "rows")
        + (d.note ? " · " + d.note : "")
      : "Not run yet.";

    var btn = card.querySelector(".cfg-run");
    btn.disabled = !d.can_run;
    btn.textContent = d.done ? "Run again" : "Run";
    card.querySelector(".cfg-blocked").textContent = d.can_run ? ""
      : "needs " + d.blocked.join(", ");
  }

  function runDerive(btn) {
    var card = btn.closest(".cfg-derive");
    var log = card.querySelector(".cfg-log");
    var fd = new FormData();
    fd.append("step", btn.getAttribute("data-step"));
    btn.disabled = true;
    log.hidden = false;
    log.textContent = "running… this walks every row against the "
      + "geography, so give it a minute. The page stays put; the log "
      + "appears here when it finishes.";
    fetch("/api/configuration/derive", { method: "POST", body: fd })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        log.textContent = (d.log || "") + (d.ok ? "" : "\n\n" + d.error);
        load();
      })
      .catch(function (e) {
        log.textContent = e.message;
        btn.disabled = false;
      });
  }

  function paintPages(pages) {
    var box = $("pages");
    box.innerHTML = "";
    pages.forEach(function (p) {
      var a = document.createElement("a");
      a.className = "cfg-page " + (p.ready ? "ready" : "blocked");
      a.href = p.path;
      var b = document.createElement("b");
      b.textContent = p.label;
      var s = document.createElement("span");
      if (!p.ready) {
        s.textContent = "waiting for " + p.missing.join(", ");
      } else if (p.thin.length) {
        s.textContent = "works · thinner without " + p.thin.join(", ");
      } else {
        s.textContent = "everything it needs is loaded";
      }
      a.appendChild(b);
      a.appendChild(s);
      box.appendChild(a);
    });
  }

  function paintFiles(files) {
    var box = $("files");
    box.innerHTML = "";
    $("fileCount").textContent = files.length
      ? files.length + (files.length === 1 ? " file" : " files") : "";
    if (!files.length) {
      box.innerHTML = '<span class="muted small">Nothing uploaded yet. ' +
        'The layer folder is empty.</span>';
      return;
    }
    files.forEach(function (f) {
      var row = document.createElement("div");
      row.className = "cfg-file";
      var nm = document.createElement("span");
      nm.className = "nm";
      nm.textContent = f.file;
      nm.title = f.file;
      var tag = document.createElement("span");
      tag.className = "tag" + (f.loaded ? " on" : "");
      tag.textContent = f.loaded ? "imported" : "on disk";
      var n = document.createElement("span");
      n.className = "n";
      n.textContent = f.features ? f.features.toLocaleString() : fmtBytes(f.bytes);
      row.appendChild(nm);
      row.appendChild(tag);
      row.appendChild(n);
      box.appendChild(row);
    });
  }

  function load() {
    fetch("/api/configuration/status")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.ok) throw new Error(d.error || "could not read the status");
        applyPublicMode(d.public_mode);
        $("dbName").textContent = d.db;
        $("dbSize").textContent = d.db_bytes ? fmtBytes(d.db_bytes) : "empty";
        var sc = $("scopeNote");
        if (sc && d.scope) {
          sc.innerHTML = 'Region scope: <span class="mono">' +
            d.scope.join(" · ") + "</span>";
        }
        $("dirNote").innerHTML = 'Layer folder: <span class="mono">' +
          d.folder.replace(/</g, "&lt;") + "</span>";
        d.layers.forEach(paintCard);
        paintSlots(d.slots || []);
        (d.derivations || []).forEach(paintDerive);
        paintPages(d.pages);
        paintFiles(d.files);
      })
      .catch(function (e) {
        $("pages").innerHTML = '<span class="muted small">' + e.message + "</span>";
      });
  }

  // ── uploading ─────────────────────────────────────────────────────────
  function setPicked(file) {
    picked = file || null;
    var q = $("queue");
    q.innerHTML = "";
    if (picked) {
      var d = document.createElement("div");
      d.className = "uprow";
      d.textContent = picked.name + "  ·  " + fmtBytes(picked.size);
      q.appendChild(d);
    }
    $("upBtn").disabled = !(picked && slot.value);
    $("upClear").disabled = !picked;
  }

  function upload() {
    if (!picked || !slot.value) return;
    var fd = new FormData();
    fd.append("file", picked);
    fd.append("layer", slot.value);
    fd.append("import", $("upImport").checked ? "1" : "0");
    $("upBtn").disabled = true;
    $("upMsg").textContent = "uploading " + picked.name + "…";
    fetch("/api/configuration/upload", { method: "POST", body: fd })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.ok) throw new Error(d.error || "upload failed");
        $("upMsg").textContent = d.imported
          ? d.file + " — " + d.rows.toLocaleString() + " rows into the " + d.note
          : d.file + " — saved, not imported";
        setPicked(null);
        load();
      })
      .catch(function (e) {
        $("upMsg").textContent = e.message;
        $("upBtn").disabled = false;
      });
  }

  // A card's Upload button chooses the slot for you, then opens the picker.
  // Choosing the slot on the card is the whole point: it is the difference
  // between "upload a file" and "satisfy this requirement".
  document.querySelectorAll(".cfg-run").forEach(function (b) {
    b.addEventListener("click", function () { runDerive(b); });
  });

  document.querySelectorAll(".cfg-up").forEach(function (b) {
    b.addEventListener("click", function () {
      slot.value = b.getAttribute("data-key");
      $("file").click();
    });
  });

  slot.addEventListener("change", function () {
    $("upBtn").disabled = !(picked && slot.value);
  });
  $("file").addEventListener("change", function (e) {
    setPicked(e.target.files && e.target.files[0]);
  });
  $("drop").addEventListener("click", function () { $("file").click(); });
  $("drop").addEventListener("dragover", function (e) {
    e.preventDefault(); $("drop").classList.add("hot");
  });
  $("drop").addEventListener("dragleave", function () {
    $("drop").classList.remove("hot");
  });
  $("drop").addEventListener("drop", function (e) {
    e.preventDefault();
    $("drop").classList.remove("hot");
    setPicked(e.dataTransfer.files && e.dataTransfer.files[0]);
  });
  $("upBtn").addEventListener("click", upload);
  $("upClear").addEventListener("click", function () {
    setPicked(null); $("upMsg").textContent = "";
  });
  $("refresh").addEventListener("click", load);

  // ── sample overlays ────────────────────────────────────────────────────
  // Listed here rather than on the preview page because this is where things
  // are removed, and removing one affects everybody who opens that page.
  function smpMsg(html, bad) {
    var el = $("smpCfgMsg");
    if (!el) return;
    el.innerHTML = html;
    el.className = "fm-msg" + (bad ? " bad" : "");
  }

  // On a shared instance the upload controls are inert. Saying so up front
  // is the difference between a page that is read-only and a page that
  // looks broken.
  function applyPublicMode(on) {
    if (!on) return;
    document.body.classList.add("readonly");
    ["upBtn", "upClear", "slot", "file", "refresh"].forEach(function (id) {
      var el = $(id);
      if (el) el.disabled = id !== "refresh";
    });
    var drop = $("drop");
    if (drop) {
      drop.classList.add("off");
      drop.innerHTML = '<b>Read-only on this server</b>'
        + "<span>Data layers are loaded on the local copy and published "
        + "from there.</span>";
    }
    var msg = $("upMsg");
    if (msg) {
      msg.className = "fm-msg";
      msg.innerHTML = "This is a shared instance. Everything on this page "
        + "is readable; nothing here can be changed. To preview your own "
        + "workbook use <a href=\"/polygon-samples\">Polygon Samples</a> — "
        + "it is read in your browser and never uploaded.";
    }
  }

  function loadSamples() {
    var host = $("smpCfgList");
    if (!host) return;
    fetch("/api/samples").then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.ok) { host.innerHTML = '<span class="muted small">'
          + esc(d.error || "could not read") + "</span>"; return; }
        var mode = $("smpCfgMode");
        if (mode) mode.textContent = d.can_store
          ? "this server accepts kept samples"
          : "preview only — this server does not keep samples";
        if (!d.samples.length) {
          host.innerHTML = '<span class="muted small">Nothing kept here yet. '
            + "Previewing a file on the Preview Polygon page does not put it "
            + "here.</span>";
          return;
        }
        host.innerHTML = d.samples.map(function (s) {
          var bits = [num(s.rows_kept) + " outlets", num(s.dse_count) + " DSE",
                      esc(s.branch), esc(s.region)];
          return '<div class="uprow smprow" data-sid="' + s.id + '">'
            + '<div class="top"><span class="nm">' + esc(s.label) + "</span>"
            + '<span class="cnt">' + num(s.rows_kept) + "</span></div>"
            + '<div class="sub muted small">' + bits.join(" · ")
            + " · uploaded by " + esc(s.uploader)
            + (s.original_name ? " · " + esc(s.original_name) : "")
            + "</div>"
            + '<div class="smpdel">'
            + '<input type="password" inputmode="numeric" maxlength="12" '
            + 'placeholder="PIN to remove" data-pin="' + s.id + '">'
            + '<button type="button" data-del="' + s.id + '">Remove</button>'
            + "</div></div>";
        }).join("");
        host.querySelectorAll("button[data-del]").forEach(function (b) {
          b.addEventListener("click", function () { removeSample(+b.dataset.del, b); });
        });
        host.querySelectorAll("input[data-pin]").forEach(function (i) {
          i.addEventListener("keydown", function (e) {
            if (e.key === "Enter") {
              var b = host.querySelector('button[data-del="' + i.dataset.pin + '"]');
              if (b) b.click();
            }
          });
        });
      })
      .catch(function (e) {
        host.innerHTML = '<span class="muted small">could not read: '
          + esc(String(e)) + "</span>";
      });
  }

  function removeSample(sid, btn) {
    var host = $("smpCfgList");
    var input = host.querySelector('input[data-pin="' + sid + '"]');
    var pin = input ? input.value.trim() : "";
    if (!pin) { smpMsg("Enter the PIN to remove this sample.", true);
                if (input) input.focus(); return; }
    btn.disabled = true;
    btn.textContent = "removing…";
    fetch("/api/samples/" + sid + "/delete", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pin: pin })
    }).then(function (r) { return r.json(); })
      .then(function (d) {
        btn.disabled = false;
        btn.textContent = "Remove";
        if (!d.ok) { smpMsg(esc(d.error), true); if (input) input.select(); return; }
        smpMsg("Removed <b>" + esc(d.label) + "</b>"
               + (d.file ? " and its stored file" : "") + ".");
        loadSamples();
      })
      .catch(function (e) {
        btn.disabled = false;
        btn.textContent = "Remove";
        smpMsg("Could not remove it: " + esc(String(e)), true);
      });
  }

  load();
  loadSamples();
})();
