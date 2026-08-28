/* Data Files — upload, import, unload and delete the local data store.

   Several files at a time in both directions: one data type governs the whole
   upload and the queue below it can hold as many files as you like, and there
   are checkboxes on the list for importing or removing a batch.
   Everything runs one request at a time on purpose. The server names a saved
   file by looking at what is already on disk, so two uploads in flight could
   both be told the name is free and one would land as "(2)" for no reason;
   and when something fails, a sequence can stop and say which file it was. */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var meta = { roles: [], allowed: [] };
  var queue = [];       // {file, status, note}
  var selected = {};    // filename -> true

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function kb(n) {
    return n > 1048576 ? (n / 1048576).toFixed(1) + " MB"
                       : Math.max(1, Math.round(n / 1024)) + " KB";
  }
  function msg(text, kind) {
    $("upMsg").className = "fm-msg " + (kind || "");
    $("upMsg").innerHTML = text;
  }

  function guessRole(name) {
    var n = name.toLowerCase();
    if (/dse|outlet/.test(n)) return "outlet";
    if (/site|bts|tower/.test(n)) return "site";
    if (/service.?point/.test(n)) return "service";
    if (/mc36|mc_|microcluster/.test(n)) return "mc";
    if (/desa|kel/.test(n)) return "desa";
    if (/kec|city.?border|kabupaten/.test(n)) return "kec";
    return "other";
  }

  // One choice covers the batch. Blank means "work it out from each name",
  // which is what the server does too when it is handed no role.
  function chosenRole() {
    var el = $("role");
    return el ? el.value : "";
  }
  function roleFor(name) { return chosenRole() || guessRole(name); }
  function roleLabel(key) {
    var r = meta.roles.filter(function (x) { return x.key === key; })[0];
    return r ? r.label : key;
  }
  // "\u2026 takes .kml or .kmz" reads better than telling them what went wrong.
  function kindList(key) {
    var r = meta.roles.filter(function (x) { return x.key === key; })[0];
    if (!r || !r.kinds.length) return "";
    if (r.kinds.length === 1) return r.kinds[0];
    return r.kinds.slice(0, -1).join(", ") + " or " + r.kinds[r.kinds.length - 1];
  }

  // The list of types comes from the server, so it is filled in on first load.
  // Refilling it later must not throw away what the user picked.
  function fillRoles() {
    var el = $("role");
    if (!el || el.dataset.filled === "1" || !meta.roles.length) return;
    var keep = el.value;
    el.innerHTML = '<option value="">Guess from the file name</option>'
      + meta.roles.map(function (r) {
          return '<option value="' + esc(r.key) + '">' + esc(r.label) + "</option>";
        }).join("");
    el.value = keep;
    el.dataset.filled = "1";
    el.addEventListener("change", function () { renderQueue(); msg("", ""); });
  }

  function extOk(name, role) {
    var r = meta.roles.filter(function (x) { return x.key === role; })[0];
    if (!r) return true;
    return r.kinds.some(function (k) { return name.toLowerCase().endsWith(k); });
  }

  // ── upload queue ──────────────────────────────────────────────────────
  function addFiles(list) {
    Array.prototype.forEach.call(list, function (f) {
      if (!meta.allowed.some(function (a) { return f.name.toLowerCase().endsWith(a); })) {
        queue.push({ file: f, status: "bad", note: "not a handled type" });
        return;
      }
      var dup = queue.some(function (q) {
        return q.file.name === f.name && q.file.size === f.size; });
      if (dup) return;
      queue.push({ file: f, status: "ready", note: "" });
    });
    renderQueue();
  }

  function renderQueue() {
    var host = $("queue");
    if (!queue.length) {
      host.innerHTML = "";
      $("upBtn").disabled = true; $("upClear").disabled = true;
      $("upBtn").textContent = "Upload";
      return;
    }
    var pending = queue.filter(function (q) { return q.status === "ready"; }).length;
    $("upBtn").disabled = !pending;
    $("upClear").disabled = false;
    $("upBtn").textContent = pending > 1 ? "Upload " + pending + " files" : "Upload";

    host.innerHTML = queue.map(function (q, i) {
      var role = q.status === "bad" ? "" : roleFor(q.file.name);
      var warn = q.status === "ready" && !extOk(q.file.name, role)
        ? '<div class="q-note bad">' + esc(roleLabel(role)) + " takes "
          + esc(kindList(role)) + "</div>" : "";
      return '<div class="q-row ' + q.status + '">'
        + '<div class="q-top">'
        + '<span class="q-name" title="' + esc(q.file.name) + '">'
        + esc(q.file.name) + "</span>"
        + '<span class="q-size">' + kb(q.file.size) + "</span>"
        + (q.status === "ready"
           ? '<button type="button" class="q-x" data-i="' + i + '" title="remove">&times;</button>'
           : '<span class="q-st">' + esc(q.status) + "</span>")
        + "</div>"
        // When a type is chosen it is on the control right above, so saying it
        // again on every row is just noise. Guesses do need spelling out.
        + (role && !chosenRole()
           ? '<div class="q-as">' + esc(roleLabel(role)) + " &middot; from the name</div>"
           : "")
        + (q.note ? '<div class="q-note ' + (q.status === "done" ? "good" : "bad")
                    + '">' + esc(q.note) + "</div>" : "")
        + warn
        + "</div>";
    }).join("");

    host.querySelectorAll(".q-x").forEach(function (b) {
      b.addEventListener("click", function () {
        queue.splice(+b.dataset.i, 1);
        renderQueue();
      });
    });
  }

  function uploadOne(q) {
    var fd = new FormData();
    fd.append("file", q.file);
    fd.append("role", roleFor(q.file.name));
    fd.append("import", $("upImport").checked ? "1" : "0");
    return fetch("/api/files/upload", { method: "POST", body: fd })
      .then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.ok) {
          q.status = "failed";
          q.note = d.error + (d.saved ? " (the file was kept)" : "");
          return false;
        }
        q.status = "done";
        q.note = d.imported
          ? "imported as " + d.file + " — " + (d.features || 0).toLocaleString() + " features"
          : "saved as " + d.file + ", not imported";
        return true;
      })
      .catch(function (e) { q.status = "failed"; q.note = String(e); return false; });
  }

  function runQueue() {
    var todo = queue.filter(function (q) { return q.status === "ready"; });
    if (!todo.length) return;
    var bad = todo.filter(function (q) {
      return !extOk(q.file.name, roleFor(q.file.name)); });
    if (bad.length) {
      msg("<b>" + bad.length + "</b> file" + (bad.length === 1 ? "" : "s")
        + " cannot be filed as "
        + esc(roleLabel(chosenRole() || guessRole(bad[0].file.name)))
        + " — change the data type, or take " + (bad.length === 1 ? "it" : "them")
        + " out of the queue.", "bad");
      return;
    }
    $("upBtn").disabled = true;
    var i = 0, okCount = 0;
    msg("uploading 1 of " + todo.length + "…", "");
    function next() {
      if (i >= todo.length) {
        var failed = todo.length - okCount;
        msg("<b>" + okCount + " of " + todo.length + "</b> uploaded"
          + (failed ? ", <b>" + failed + "</b> failed — see the rows above."
                    : "."), failed ? "warn" : "good");
        renderQueue();
        return load();
      }
      var q = todo[i];
      q.status = "uploading";
      renderQueue();
      msg("uploading " + (i + 1) + " of " + todo.length + " — " + esc(q.file.name), "");
      uploadOne(q).then(function (ok) {
        if (ok) okCount++;
        i++;
        renderQueue();
        next();
      });
    }
    next();
  }

  $("drop").addEventListener("click", function () { $("file").click(); });
  $("file").addEventListener("change", function () {
    if ($("file").files.length) addFiles($("file").files);
    $("file").value = "";
  });
  ["dragenter", "dragover"].forEach(function (e) {
    $("drop").addEventListener(e, function (ev) {
      ev.preventDefault(); $("drop").classList.add("hot"); });
  });
  ["dragleave", "drop"].forEach(function (e) {
    $("drop").addEventListener(e, function (ev) {
      ev.preventDefault(); $("drop").classList.remove("hot"); });
  });
  $("drop").addEventListener("drop", function (ev) {
    if (ev.dataTransfer.files.length) addFiles(ev.dataTransfer.files);
  });
  $("upBtn").addEventListener("click", runQueue);
  $("upClear").addEventListener("click", function () {
    queue = []; renderQueue(); msg("", "");
  });

  // ── the file list ─────────────────────────────────────────────────────
  function syncSel() {
    var n = Object.keys(selected).length;
    $("selCount").textContent = n ? n + " selected" : "";
    $("bulkImport").disabled = !n;
    $("bulkDelete").disabled = !n;
    var boxes = $("files").querySelectorAll("input.pick");
    $("selAll").checked = boxes.length > 0 && n === boxes.length;
  }

  function load() {
    return fetch("/api/files").then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d.ok) { $("files").innerHTML = '<span class="muted small">'
          + esc(d.error) + "</span>"; return; }
        meta.roles = d.roles; meta.allowed = d.allowed;
        fillRoles();
        $("file").accept = d.allowed.join(",");
        $("dirNote").textContent = "Files are stored in " + d.dir;
        // forget selections for files that are no longer there
        var names = {};
        d.files.forEach(function (f) { names[f.file] = 1; });
        Object.keys(selected).forEach(function (k) {
          if (!names[k]) delete selected[k]; });
        render(d.files);
        renderQueue();
        // the shared header says "loading..." until a page tells it otherwise
        var st = $("status");
        if (st) st.textContent = d.files.length
          ? d.files.length + (d.files.length === 1 ? " file" : " files") + " stored"
          : "nothing stored yet";
      });
  }

  function render(files) {
    if (!files.length) {
      $("files").innerHTML = '<span class="muted small">Nothing uploaded yet.</span>';
      syncSel();
      return;
    }
    $("files").innerHTML = files.map(function (f) {
      var layers = (f.layers || []).map(function (l) {
        return '<span class="fm-layer">' + esc(l.layer_key) + " · "
          + (l.feature_count || 0).toLocaleString() + " features"
          + ' <a href="#" class="unload" data-key="' + esc(l.layer_key)
          + '">unload</a></span>';
      }).join("");
      return '<div class="fm-row' + (f.loaded ? " on" : "") + '">'
        + '<input type="checkbox" class="pick" data-file="' + esc(f.file) + '"'
        + (selected[f.file] ? " checked" : "") + ">"
        + '<span class="dot" style="background:' + f.colour + '"></span>'
        + '<div class="fm-main">'
        + '<div class="fm-name">' + esc(f.file) + "</div>"
        + '<div class="fm-meta">' + esc(f.role_label) + " &middot; " + kb(f.bytes)
        + " &middot; " + esc(f.modified)
        + (f.loaded ? " &middot; " + layers
                    : ' &middot; <span class="fm-off">not imported</span>')
        + "</div></div>"
        + '<div class="fm-act">'
        + '<button type="button" class="rtab imp" data-file="' + esc(f.file)
        + '" data-role="' + esc(f.role) + '">'
        + (f.loaded ? "Re-import" : "Import") + "</button>"
        + '<button type="button" class="rtab del" data-file="' + esc(f.file)
        + '" data-loaded="' + (f.loaded ? "1" : "0") + '">Delete</button>'
        + "</div></div>";
    }).join("");

    $("files").querySelectorAll("input.pick").forEach(function (b) {
      b.addEventListener("change", function () {
        if (b.checked) selected[b.dataset.file] = true;
        else delete selected[b.dataset.file];
        syncSel();
      });
    });
    $("files").querySelectorAll("button.imp").forEach(function (b) {
      b.addEventListener("click", function () {
        b.textContent = "importing…"; b.disabled = true;
        importOne(b.dataset.file, b.dataset.role).then(function (d) {
          if (!d.ok) window.alert("Import failed:\n\n" + d.error);
          load();
        });
      });
    });
    $("files").querySelectorAll("a.unload").forEach(function (a) {
      a.addEventListener("click", function (e) {
        e.preventDefault();
        if (!window.confirm("Drop the imported rows for '" + a.dataset.key
            + "'?\n\nThe file stays on the server and can be re-imported.")) return;
        var fd = new FormData(); fd.append("layer_key", a.dataset.key);
        fetch("/api/files/unload", { method: "POST", body: fd })
          .then(function (r) { return r.json(); }).then(load);
      });
    });
    $("files").querySelectorAll("button.del").forEach(function (b) {
      b.addEventListener("click", function () {
        var loaded = b.dataset.loaded === "1";
        var q = "Delete '" + b.dataset.file + "' from the server?";
        if (loaded) q += "\n\nOK  = delete the file AND its imported data."
                      + "\nCancel = keep everything.";
        if (!window.confirm(q)) return;
        deleteOne(b.dataset.file, loaded).then(function (d) {
          if (!d.ok) { window.alert("Delete failed:\n\n" + d.error); }
          else if (d.how === "trashed") {
            window.alert("This folder does not allow deleting, so the file was "
              + "moved to _trash/ instead.\n\nIt is out of the way and no "
              + "longer listed, but still on disk at:\n" + d.moved_to);
          }
          load();
        });
      });
    });
    syncSel();
  }

  function importOne(file, role) {
    var fd = new FormData();
    fd.append("file", file); fd.append("role", role || "");
    return fetch("/api/files/import", { method: "POST", body: fd })
      .then(function (r) { return r.json(); });
  }
  function deleteOne(file, withData) {
    var fd = new FormData();
    fd.append("file", file); fd.append("with_data", withData ? "1" : "0");
    return fetch("/api/files/delete", { method: "POST", body: fd })
      .then(function (r) { return r.json(); });
  }

  // ── bulk ──────────────────────────────────────────────────────────────
  function runBatch(names, fn, verb) {
    var i = 0, ok = 0, fails = [];
    $("bulkImport").disabled = true; $("bulkDelete").disabled = true;
    function next() {
      if (i >= names.length) {
        if (fails.length) {
          window.alert(verb + " failed for:\n\n"
            + fails.map(function (f) { return "• " + f[0] + " — " + f[1]; }).join("\n"));
        }
        selected = {};
        // load() redraws the list and that resets the counter, so the outcome
        // is written after it lands — otherwise it flashes up and vanishes.
        return load().then(function () {
          $("selCount").textContent = ok + " of " + names.length + " " + verb;
        });
      }
      $("selCount").textContent = verb + " " + (i + 1) + " of " + names.length + "…";
      fn(names[i]).then(function (d) {
        if (d && d.ok) ok++;
        else fails.push([names[i], (d && d.error) || "failed"]);
        i++;
        next();
      });
    }
    next();
  }

  $("selAll").addEventListener("change", function () {
    var boxes = $("files").querySelectorAll("input.pick");
    selected = {};
    boxes.forEach(function (b) {
      b.checked = $("selAll").checked;
      if (b.checked) selected[b.dataset.file] = true;
    });
    syncSel();
  });
  $("bulkImport").addEventListener("click", function () {
    var names = Object.keys(selected);
    if (!names.length) return;
    if (!window.confirm("Import " + names.length + " file(s)?")) return;
    runBatch(names, function (n) { return importOne(n, ""); }, "imported");
  });
  $("bulkDelete").addEventListener("click", function () {
    var names = Object.keys(selected);
    if (!names.length) return;
    if (!window.confirm("Delete " + names.length + " file(s) from the server,"
        + " together with any data they were imported into?\n\nThis cannot be"
        + " undone from here.")) return;
    runBatch(names, function (n) { return deleteOne(n, true); }, "deleted");
  });

  $("refresh").addEventListener("click", load);
  load();
})();
