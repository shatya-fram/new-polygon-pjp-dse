#!/usr/bin/env python3
"""
CONFIGURATION — the front door of this application.

WHY IT EXISTS
    The parent app could upload a file and could import it, but it could not
    tell you whether the application was *ready*. Seven different inputs have
    to be present before the Distribution and Preview pages mean anything,
    and the only way to find out which one was missing was to open a page and
    find it empty. This module states the requirement first and reports each
    input against it.

THREE IMPORT PATHS, NAMED
    The parent app decided how to import a file from its extension alone,
    which is why a spreadsheet of reference figures could only ever be loaded
    from the command line: the web upload sent every .xlsx down the
    "plot these as points" path. Here the path is part of the requirement,
    not a guess:

        kml         placemark polygons  -> geo_layer + geo_feature
        points      a table of places   -> geo_layer + geo_feature (points)
        reference   a table of figures  -> ref_kelurahan / ref_kecamatan /
                                           ref_metric / ref_site

    An input declares which path it takes. Nothing is inferred from the file
    name, so a desa master workbook can never be silently scattered across
    the map as 887 dots.

READY MEANS READY FOR A PAGE, NOT READY IN GENERAL
    Each page names the inputs it cannot work without. A page whose inputs
    are missing says so on this screen, with the missing input named, rather
    than rendering an empty map and leaving you to guess.
"""
import os
import sqlite3

from flask import Blueprint, jsonify, render_template, request, send_file

import config
import db
import dse_coverage
import filestore as fstore
import formats
import geom
import import_local as il
import kmllayers as kml
import samples
import siting
import territory_api

bp = Blueprint("configuration", __name__)

TEMPLATE_DIR = os.path.join(config.BASE_DIR, "Data Templates")


# ── the requirement ──────────────────────────────────────────────────────
# FOUR SLOTS, NAMED BY THE REGIONAL HEAD (2026-08-25), AND THE PARTS IN THEM
#
# A slot is what somebody uploads against; a part is one file inside it. Desa
# Polygon & Profiles takes three different files -- the desa boundary, the
# kecamatan boundary and the village profile workbook -- and they arrive on
# different days, so the page has to be able to say which of the three is
# still missing. Collapsing them to one row would only be able to say
# "incomplete".
#
# Every part names its own reader. Nothing is inferred from a file name, and
# for the polygons nothing is inferred from the extension either: two of them
# are .kmz and they are told apart by the fields inside, because a desa
# export loaded into the kecamatan slot replaces 121 polygons with 887.
SLOTS = [
    {"key": "admin", "label": "Administrative Boundaries", "tier": "base",
     "note": "The three published boundary layers, straight from the ALL KML "
             "folder. Each is a national export; only the provinces the "
             "circle lies inside are kept."},
    {"key": "territory", "label": "Indosat Territory", "tier": "base",
     "note": "The commercial cut at kecamatan grain, carrying the whole "
             "hierarchy and both brands' channel structure."},
    {"key": "site", "label": "Site Locations", "tier": "base",
     "note": "Where the network is. The SnD reference export."},
    {"key": "outlet", "label": "DSE & Outlet Mapping", "tier": "analysis",
     "note": "Who covers what on the ground. The territory model is built "
             "from this and nothing else."},
    {"key": "profile", "label": "Village Profile (NCP)", "tier": "reference",
     "note": "The monthly figures the map quotes but never draws."},
]

# Everything a KML slot needs is declared in kmllayers.py -- the fields it is
# recognised by, what a click shows, what can be filtered on and what is
# computed. These entries carry only what the Configuration page itself needs
# so that the field model has exactly one home.
def _kml(key, slot, order, kml_key, example):
    spec = kml.BY_KEY[kml_key]
    return {"key": key, "slot": slot, "order": order,
            "label": spec["label"], "handler": "kml", "kml": kml_key,
            "layer_keys": [spec["layer_key"]],
            "kinds": [".kml", ".kmz"],
            "fields": " · ".join(f for f, _ in spec["preview"]),
            "example": example,
            "why": KML_WHY[kml_key],
            "template": None}


KML_WHY = {
 "desa": "The finest base grain. Population, area and twenty age bands per "
         "village — density and the 15–29 share are computed on import so "
         "they can be filtered and coloured like any other field.",
 "kecamatan": "The administrative level above desa. Thin: names, population "
              "and a gender split. Its code columns are zero in every row, "
              "so it joins by name only.",
 "kabkot": "Small but the most decision-useful layer in the folder — "
           "Kabupaten_Priority and Attack_3ID state the play rather than "
           "describing the place.",
 "territory": "The commercial cut, and the only layer carrying URBAN/RURAL — "
              "which changes reach, store format and rep workload more than "
              "any number on the card.",
 "sites": "Loads as a map layer and as the site reference table. Carries the "
          "hierarchy and the addressable/category tagging per site.",
}

LAYERS = [
    _kml("desa_poly", "admin", 1, "desa", "Border Desa.kml — 82,863 national"),
    _kml("kec_poly", "admin", 2, "kecamatan", "Border Kecamatan.kml — 6,830"),
    _kml("kab_poly", "admin", 3, "kabkot", "Border KabKot.kml — 515"),
    _kml("territory", "territory", 1, "territory",
         "Teritory Border Aug.kml — 7,176 kecamatan rows"),
    _kml("site", "site", 1, "sites",
         "SnD Site Reference Aug.kml — point per site"),

    {"key": "outlet", "slot": "outlet", "order": 1,
     "label": "Outlet → DSE mapping",
     "handler": "outlet",
     "layer_keys": ["outlet_dse"],
     "kinds": [".xlsx", ".xlsm", ".kmz", ".kml"],
     "fields": "DSE CODE · Outlet Code · Outlet Name · Desa Name · Micro "
               "Cluster Name (MC) · Region Name · LONG · LAT",
     "example": "DSE PJP into Desa JAYA vShare.xlsx  [sheet AUG-OUTLET TO "
                "DSE] — 73,698 rows, 1,501 DSE",
     "why": "Uploaded by location: every row carries LONG/LAT, so the layer "
            "draws as points and the desa it falls in is a variable on the "
            "row. Territories are grouped on DSE CODE and nothing else, so "
            "no outlet ever changes rep. The workbook may also hold the "
            "village profile on another sheet — each slot picks its own "
            "sheet by the columns it needs.",
     "template": "PJP_Outlet_DSE_Spec.xlsx"},

    {"key": "desa_profile", "slot": "profile", "order": 1,
     "label": "Village profile (NCP)",
     "handler": "ncp",
     "table": "ref_kelurahan",
     "kinds": [".xlsx", ".xlsm"],
     "fields": "Village (Desa) · District (Kecamatan) · City (Kota/Kab) · "
               "REGION · MC · BRANCH · Total Population · NCP 4G · PJP IM3 · PJP 3ID",
     "example": "DSE PJP into Desa JAYA vShare.xlsx  [sheet NCP Village] "
                "— 7,771 rows",
     "why": "Population, NCP and PJP coverage per village, filed under the "
            "period read from the sheet name. Kecamatan are rolled up from it.",
     "template": "PJP_Village_Profile_Spec.xlsx"},
]

BY_KEY = {ly["key"]: ly for ly in LAYERS}
BY_SLOT = {s["key"]: [ly for ly in LAYERS if ly["slot"] == s["key"]]
           for s in SLOTS}

# ── what has to be DERIVED after the files are in ────────────────────────
# Importing every file is not the same as being ready. The mapping from the
# microcluster polygons onto the kecamatan and desa tables is computed, not
# uploaded, and until it has been computed the Preview page has 36 shapes and
# figures for the generation the workbook shipped with. On the parent app
# that was a script you had to know to run.
DERIVATIONS = [
    {"key": "mc36",
     "label": "MC-36 mapping onto kecamatan and desa",
     "script": "remap_mc.py",
     "needs": ["territory", "desa_poly", "desa_profile"],
     "check": ("ref_kecamatan", "mc36"),
     "unit": "kecamatan carry a derived MC-36",
     "why": "Point-in-polygon at desa level, rolled up to kecamatan by a "
            "population-weighted majority — not by the kecamatan's own "
            "centroid, because a kecamatan lying across a seam should follow "
            "its people. The profile's own `mc` column is never overwritten; "
            "the derived value lands in `mc36` beside it, and the run prints "
            "how far the two agree."},
    {"key": "site_dse",
     "label": "Place sites against the DSE geography",
     "script": "derive_sites.py",
     "needs": ["site", "outlet"],
     "check": ("ref_site", "dse_assigned"),
     "unit": "sites placed against a rep",
     "why": "Every site is tested against the coverage polygons inferred "
            "from the outlets, and falls back to the nearest outlet's rep "
            "where it sits outside all of them — marked `nearest` rather "
            "than `polygon`, so a confident placement can be told from a "
            "best guess. With 496 reps and 26,342 outlets this is minutes of "
            "work, which is why it is a step you start rather than a cost "
            "hidden inside an upload."},
]
BY_DERIVED = {d["key"]: d for d in DERIVATIONS}

# What each page cannot work without. Named per page rather than as one
# global "ready" flag, because the Distribution map is perfectly usable with
# no reference figures at all, and saying otherwise would be a lie that
# stops somebody working.
PAGE_NEEDS = {
    "distribution": {
        "label": "Distribution Polygon", "path": "/distribution",
        "needs": ["outlet"],
        "helps": ["desa_poly", "kec_poly", "kab_poly", "territory", "site"]},
    "preview": {
        "label": "Preview Polygon", "path": "/preview-polygon",
        "needs": ["outlet", "desa_profile", "derive:mc36"],
        "helps": ["desa_poly", "kec_poly", "kab_poly", "site"]},
    "layers": {
        "label": "Layer Model", "path": "/layer-model",
        "needs": [],
        "helps": ["desa_poly", "kec_poly", "kab_poly", "territory", "site",
                  "outlet"]},
}


# ── reading the state ────────────────────────────────────────────────────
def _table_count(con, table):
    """A missing table is a zero, not a crash: this page has to render on a
    database that migrate.py has only just created."""
    try:
        return con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
    except sqlite3.OperationalError:
        return 0


def _geo_layers(con):
    try:
        return [dict(r) for r in con.execute(
            "SELECT layer_key,label,kind,source_file,feature_count,"
            "imported_utc FROM geo_layer ORDER BY layer_key")]
    except sqlite3.OperationalError:
        return []


def status(con):
    """One row per declared part, with what is actually loaded against it."""
    layers = _geo_layers(con)
    out = []
    for ly in LAYERS:
        if ly["handler"] == "ncp":
            n = _table_count(con, ly["table"])
            out.append(dict(
                ly, loaded=n > 0, rows=n, features=n, files=[], last=None,
                extra={"kecamatan": _table_count(con, "ref_kecamatan"),
                       "figures": _table_count(con, "ref_metric")}))
            continue

        # Every part now lands on a layer key its own reader assigns, so the
        # match is exact. The parent app matched on a guessed ROLE, which was
        # the only way to count eleven separate outlet exports as one input --
        # and the same looseness let an unrelated file answer for a slot.
        hits = [g for g in layers if g["layer_key"] in ly["layer_keys"]]
        feats = sum(g.get("feature_count") or 0 for g in hits)
        last = max([g.get("imported_utc") or "" for g in hits], default="") or None
        extra = {}
        if ly["key"] == "site":
            extra["ref_site"] = _table_count(con, "ref_site")
        out.append(dict(ly, loaded=bool(hits), rows=feats, features=feats,
                        files=[{"layer_key": g["layer_key"],
                                "source_file": g.get("source_file"),
                                "features": g.get("feature_count") or 0,
                                "imported_utc": g.get("imported_utc")}
                               for g in hits],
                        last=last, extra=extra))
    return out


def slots(state):
    """The four named slots, each carrying its parts. What the page shows."""
    by_key = {p["key"]: p for p in state}
    out = []
    for s in SLOTS:
        parts = [by_key[ly["key"]] for ly in BY_SLOT[s["key"]]]
        done = sum(1 for p in parts if p["loaded"])
        out.append(dict(s, parts=parts, loaded=done, total=len(parts),
                        complete=done == len(parts)))
    return out


def derivations(con, state):
    """Each derived step, whether it has run, and whether it can run yet."""
    have = {s["key"]: s["loaded"] for s in state}
    out = []
    for d in DERIVATIONS:
        table, col = d["check"]
        try:
            n = con.execute(
                f"SELECT count(*) FROM {table} "
                f"WHERE {col} IS NOT NULL AND {col} <> ''").fetchone()[0]
        except sqlite3.OperationalError:
            # The column is added by the script itself, so its absence is the
            # normal "has never run" state rather than a broken database.
            n = 0
        blocked = [BY_KEY[k]["label"] for k in d["needs"] if not have.get(k)]

        # A derivation that only reports "done" hides the thing worth
        # knowing. The microcluster mapping is interesting precisely where it
        # DISAGREES with the cut the profile shipped with, so that count is
        # carried out to the page rather than left in the log.
        note = None
        if d["key"] == "mc36" and n:
            try:
                diff = con.execute(
                    "SELECT count(*) FROM ref_kelurahan WHERE mc IS NOT NULL "
                    "AND mc36 IS NOT NULL AND mc <> mc36").fetchone()[0]
                total = con.execute(
                    "SELECT count(*) FROM ref_kelurahan").fetchone()[0]
                if diff:
                    note = (f"{diff:,} of {total:,} desa sit in a different "
                            f"microcluster than the profile says. Both are "
                            f"kept; neither is corrected.")
            except sqlite3.OperationalError:
                pass
        if d["key"] == "site_dse" and n:
            try:
                near = con.execute(
                    "SELECT count(*) FROM ref_site WHERE assign_mode='nearest'"
                ).fetchone()[0]
                if near:
                    note = (f"{near:,} of them fell outside every coverage "
                            f"polygon and took the nearest outlet's rep — a "
                            f"best guess, marked as one.")
            except sqlite3.OperationalError:
                pass

        out.append(dict(d, done=n > 0, rows=n, blocked=blocked,
                        can_run=not blocked, note=note))
    return out


def _label(key, derived):
    if key.startswith("derive:"):
        return BY_DERIVED[key.split(":", 1)[1]]["label"]
    return BY_KEY[key]["label"]


def readiness(state, derived):
    have = {s["key"]: s["loaded"] for s in state}
    for d in derived:
        have["derive:" + d["key"]] = d["done"]
    pages = []
    for key, p in PAGE_NEEDS.items():
        missing = [_label(k, derived) for k in p["needs"] if not have.get(k)]
        thin = [_label(k, derived) for k in p["helps"] if not have.get(k)]
        pages.append({"key": key, "label": p["label"], "path": p["path"],
                      "ready": not missing, "missing": missing, "thin": thin})
    return pages


# ── importing ────────────────────────────────────────────────────────────
def _clear_caches(layer_keys=()):
    """The boundary polygons are cached in the territory API for the life of
    the process. A new import that does not clear it is invisible until a
    restart, which reads as the upload having failed."""
    try:
        import territory_api
        territory_api._poly_cache.clear()
    except Exception:                                             # noqa: BLE001
        pass
    # The desa area table and the per-desa mast counts are both held for the
    # life of the process. An NCP refresh that does not clear them serves
    # last month's population and last month's sites until a restart.
    _STATS_CACHE.clear()
    _SITES_CACHE.clear()
    # The prebuilt map files are the whole reason preview is fast; a re-import
    # that leaves them in place would serve last month's boundaries for ever.
    try:
        import mapcache
        for k in layer_keys:
            mapcache.drop(k)
    except Exception:                                             # noqa: BLE001
        pass


def _prebuild(con, layer_key):
    """Prebuild one layer's map file, and never let it fail an import.

    A cache that could not be written is a slow map, not a lost import — the
    request path rebuilds it on demand. Losing the upload over it would be
    the worse trade."""
    try:
        import mapcache
        import territory_api
        mapcache.drop(layer_key)
        _p, n, b = mapcache.build(
            con, layer_key,
            props_for=territory_api._bulk_props(con, layer_key))
        log_line = f"      map file: {n:,} features, {b / 1e6:.1f} MB"
        print(log_line, flush=True)
    except Exception as exc:                                      # noqa: BLE001
        print(f"      map file not built ({type(exc).__name__}: {exc}) — "
              f"the first preview will build it instead", flush=True)


def run_import(con, path, spec):
    """-> (rows, note). Raises on a bad file; the caller keeps the file.

    The reader is chosen by the SLOT, never by the file name, and for the
    polygon slots not by the extension either -- three of the four boundary
    exports are .kmz and only their fields tell them apart."""
    ext = os.path.splitext(path)[1].lower()
    handler = spec["handler"]

    if handler == "kml":
        spec_kml = kml.BY_KEY[spec["kml"]]
        n = formats.import_kml_layer(con, path, spec_kml)
        # Build the map file here, while the operator is already waiting on an
        # import, rather than making the first person to open the map pay for
        # it. "Uploaded" and "ready to draw" should mean the same thing.
        if n:
            _prebuild(con, spec_kml["layer_key"])
        if n == 0:
            raise ValueError(
                f"Read successfully, but nothing was kept for "
                f"{spec_kml['label']}. Either this is the wrong file for the "
                f"slot, or every row fell outside the scope — check the "
                f"region scope on this page.")
        return n, spec_kml["label"]

    if handler == "ncp":
        return formats.import_ncp_village(con, path), "village profile"

    if handler == "outlet":
        if ext in (".xlsx", ".xlsm"):
            return formats.import_outlet_dse(con, path), "outlet mapping"
        # The branch-by-branch KMZ exports the parent app was built on still
        # load, so a month captured the old way is not stranded.
        return il.import_kml(con, path, follow=False), "outlet mapping (KMZ)"

    if handler == "site":
        if ext in (".xlsx", ".xlsm"):
            return formats.import_site_locations(con, path), \
                "site layer and ref_site"
        return il.import_kml(con, path, follow=False), "site layer"

    raise ValueError(f"no reader declared for {spec['label']}")


@bp.route("/api/configuration/status")
def api_status():
    con = db.connect()
    try:
        state = status(con)
        derived = derivations(con, state)
        files = fstore.listing(con)
    finally:
        con.close()
    return jsonify({
        "ok": True,
        # So the page can say it is read-only rather than let someone fill
        # in a form and discover it on the click.
        "public_mode": config.PUBLIC_MODE,
        "scope": list(formats.REGIONS),
        "db": os.path.basename(config.DB_PATH),
        "db_bytes": (os.path.getsize(config.DB_PATH)
                     if os.path.exists(config.DB_PATH) else 0),
        "folder": fstore.folder(),
        "layers": state,
        "slots": slots(state),
        "derivations": derived,
        "pages": readiness(state, derived),
        "files": files,
        "allowed": list(fstore.ALLOWED),
    })


@bp.route("/api/configuration/upload", methods=["POST"])
def api_upload():
    """Upload against a DECLARED input, not a guessed one."""
    key = (request.form.get("layer") or "").strip()
    if key not in BY_KEY:
        return jsonify({"ok": False,
                        "error": "Pick which data layer this file is."}), 400
    spec = BY_KEY[key]
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "No file was attached."}), 400
    if not f.filename.lower().endswith(tuple(spec["kinds"])):
        return jsonify({"ok": False, "error":
                        f"{spec['label']} must be one of "
                        + ", ".join(spec["kinds"])}), 400
    try:
        name, path = fstore.save_upload(f, f.filename)
    except (ValueError, OSError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    if request.form.get("import") == "0":
        return jsonify({"ok": True, "file": name, "layer": key,
                        "imported": False})

    con = db.connect()
    try:
        n, note = run_import(con, path, spec)
    except Exception as exc:                                      # noqa: BLE001
        con.close()
        # The file stays. A parse failure is nearly always the wrong file for
        # the slot, and deleting it hides the evidence of which one it was.
        return jsonify({"ok": False, "file": name, "saved": True,
                        "error": f"Saved as {name}, but it could not be read "
                                 f"as a {spec['label']} — "
                                 f"{type(exc).__name__}: {exc}"}), 400
    con.close()
    _clear_caches(spec.get("layer_keys", ()))
    return jsonify({"ok": True, "file": name, "layer": key, "imported": True,
                    "rows": n, "note": note})


@bp.route("/api/configuration/import", methods=["POST"])
def api_import():
    """Import a file already sitting in the data folder against an input."""
    key = (request.form.get("layer") or "").strip()
    name = (request.form.get("file") or "").strip()
    if key not in BY_KEY:
        return jsonify({"ok": False, "error": "unknown data layer"}), 400
    try:
        path = fstore.safe_path(name)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not os.path.isfile(path):
        return jsonify({"ok": False,
                        "error": f"{name} is not in the data folder"}), 404
    con = db.connect()
    try:
        n, note = run_import(con, path, BY_KEY[key])
    except Exception as exc:                                      # noqa: BLE001
        con.close()
        return jsonify({"ok": False,
                        "error": f"{type(exc).__name__}: {exc}"}), 400
    con.close()
    _clear_caches(BY_KEY[key].get("layer_keys", ()))
    return jsonify({"ok": True, "file": name, "rows": n, "note": note})


@bp.route("/api/configuration/derive", methods=["POST"])
def api_derive():
    """Run a derivation and hand back what it printed.

    It runs as a subprocess rather than being imported: the script owns its
    own argument parsing and its own exits, and a long derivation that fails
    should not take the web process with it. The log is returned in full
    because the interesting part of this step is its cross-check — the run
    prints how far the derived mapping agrees with the one Indosat ships,
    which is the evidence that the geometry is right."""
    import subprocess
    import sys

    key = (request.form.get("step") or "").strip()
    step = BY_DERIVED.get(key)
    if not step:
        return jsonify({"ok": False, "error": "unknown step"}), 400

    con = db.connect()
    try:
        blocked = [ly for ly in step["needs"]
                   if not next(s["loaded"] for s in status(con)
                               if s["key"] == ly)]
    finally:
        con.close()
    if blocked:
        return jsonify({"ok": False, "error":
                        "Load these first: "
                        + ", ".join(BY_KEY[k]["label"] for k in blocked)}), 400

    try:
        r = subprocess.run([sys.executable, step["script"]],
                           cwd=config.BASE_DIR, capture_output=True,
                           text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False,
                        "error": f"{step['script']} did not finish in 15 "
                                 f"minutes and was stopped."}), 504
    out = (r.stdout or "") + (r.stderr or "")
    _clear_caches()
    if r.returncode != 0:
        return jsonify({"ok": False, "step": key, "log": out[-4000:],
                        "error": f"{step['script']} exited "
                                 f"{r.returncode}"}), 400
    return jsonify({"ok": True, "step": key, "log": out[-4000:]})


# ══════════════════════════════════════════════════════════════════════════
# SAMPLE OVERLAYS
#
# Somebody's own DSE-to-outlet workbook, drawn over the application's layers
# so they can see their demarcation against the real territory. The page
# reads the file in the browser and draws it there: on a public host nothing
# anyone previews is sent to the server, and a 2.5 MB workbook is on the map
# in about a quarter of a second because there is no upload at all.
#
# Keeping one on the server is a separate act with its own permission, and
# deleting a kept one asks for a PIN -- a guard against a careless click,
# which is all a shared PIN can honestly be.
# ══════════════════════════════════════════════════════════════════════════
def _sample_meta(form):
    """Who is this, and what does it describe? Asked before the file is
    kept, because "whose overlay is this?" is the first question anyone
    will have about one they did not upload themselves."""
    meta = {k: (form.get(k) or "").strip()
            for k in ("label", "uploader", "branch", "region", "note")}
    missing = [k for k in ("uploader", "branch", "region") if not meta[k]]
    if missing:
        return None, ("Before a sample can be kept it has to say who "
                      "uploaded it and which branch and region it covers. "
                      "Missing: " + ", ".join(missing) + ".")
    if not meta["label"]:
        meta["label"] = f"{meta['branch']} — {meta['uploader']}"
    for k, cap in (("label", 120), ("uploader", 80), ("branch", 80),
                   ("region", 80), ("note", 500)):
        meta[k] = meta[k][:cap]
    return meta, None


@bp.route("/api/samples")
def api_samples():
    """What is kept on this server, and may this caller add to it."""
    con = db.connect()
    try:
        rows = samples.listing(con)
    finally:
        con.close()
    return jsonify({"ok": True, "samples": rows,
                    "can_store": config.sample_store_allowed(
                        request.remote_addr),
                    "store_mode": config.SAMPLE_STORE,
                    "max_rows": config.SAMPLE_MAX_ROWS})


@bp.route("/api/samples", methods=["POST"])
def api_samples_save():
    """Keep a sample the browser has already read.

    The rows arrive parsed. Nothing is re-read here -- two parsers for one
    format is two ways to disagree about what a file said."""
    if not config.sample_store_allowed(request.remote_addr):
        return jsonify({"ok": False, "error":
                        "This instance previews samples but does not keep "
                        "them. They stay in your browser."}), 403
    payload = request.get_json(silent=True) or {}
    meta, err = _sample_meta(payload)
    if err:
        return jsonify({"ok": False, "error": err}), 400
    rows = payload.get("rows") or []
    if not rows:
        return jsonify({"ok": False, "error": "No rows were sent."}), 400
    if len(rows) > config.SAMPLE_MAX_ROWS:
        return jsonify({"ok": False, "error":
                        f"{len(rows):,} rows is more than this instance "
                        f"keeps ({config.SAMPLE_MAX_ROWS:,}). Preview it "
                        f"instead, or filter the file first."}), 400
    meta["original_name"] = (payload.get("filename") or "")[:200]
    records, info = samples.from_browser(rows)
    if not records:
        st = info["stats"]
        return jsonify({"ok": False, "error":
                        f"None of the {st['read']:,} rows could be placed: "
                        f"{st['no_dse']:,} without a DSE code, "
                        f"{st['no_coords']:,} without coordinates, "
                        f"{st['off_map']:,} outside {config.HOME_NAME}."}), 400
    con = db.connect()
    try:
        sid = samples.save(con, meta, records, info)
        row = samples.one(con, sid)
    finally:
        con.close()
    return jsonify({"ok": True, "id": sid, "sample": row,
                    "stats": dict(info["stats"])})


def _sample_counts(con, feats, points):
    """Desa and sites for every territory the model drew.

    DESA is counted from the rep's own points -- which desa their outlets
    actually fall in -- because that is how the rest of the application
    counts a rep's desa, and a second definition here would disagree with
    the summary table for the same rep.

    SITES is counted by containment in the drawn polygon, because a mast is
    not an outlet: it does not belong to a rep by assignment, it is simply
    inside the patch or it is not. In Coverage mode the patches overlap and
    a site inside two of them is counted by both, which is the honest answer
    to "how many sites are in this territory".
    """
    idx = geom.build_index(geom.load_polys(con, "kelurahan"))
    desa_of = {}
    for dse, lat, lon in points:
        f = geom.locate_indexed(idx, lat, lon)
        if f:
            desa_of.setdefault(dse, set()).add(f["feature_key"])

    # The drawn polygons, in the shape geom's index wants.
    shaped = []
    for ft in feats:
        rings = geom.rings_of(ft["geometry"])
        if not rings:
            continue
        xs = [c[0] for poly in rings for ring in poly for c in ring]
        ys = [c[1] for poly in rings for ring in poly for c in ring]
        if not xs:
            continue
        shaped.append({"feature_key": ft["properties"]["dse"],
                       "polys": rings,
                       "bbox": (min(xs), min(ys), max(xs), max(ys))})
    sites = {}
    if shaped:
        sidx = geom.build_index(shaped)
        grid, cellsz = sidx
        try:
            rows = con.execute(
                "SELECT centroid_lat, centroid_lon FROM geo_feature "
                "WHERE layer_key = 'site_locations' "
                "AND centroid_lat IS NOT NULL")
        except sqlite3.OperationalError:
            rows = []
        for r in rows:
            lat, lon = r["centroid_lat"], r["centroid_lon"]
            for f in grid.get((int(lon // cellsz), int(lat // cellsz)), ()):
                x0, y0, x1, y1 = f["bbox"]
                if x0 <= lon <= x1 and y0 <= lat <= y1 \
                        and geom.contains(f["polys"], lon, lat):
                    sites[f["feature_key"]] = sites.get(f["feature_key"], 0) + 1

    for ft in feats:
        d = ft["properties"]["dse"]
        ft["properties"]["desa"] = len(desa_of.get(d, ()))
        ft["properties"]["sites"] = sites.get(d, 0)
    return feats


# ══════════════════════════════════════════════════════════════════════════
# AREA PROFILE — what one drawn territory actually contains
#
# WHY THE COVERAGE FIGURE IS SAMPLED AND NOT CLIPPED
# "How much of PEGADUNGAN does this rep hold" is an intersection area, and
# an exact polygon clip needs a clipping library this application does not
# carry. So the desa is sampled on a 50x50 lattice laid over its own
# bounding box: the sample points that land inside the desa are the
# denominator, the ones that also land inside the territory are the
# numerator. At desa scale that is good to about a percent -- finer than the
# boundary file's own disagreement with the ground -- and it costs
# milliseconds where a clip would cost seconds. The figure is reported to one
# decimal and never dressed up as exact: 100% means "the whole desa, within a
# percent", which is the answer the question is really asking for.
#
# IT RUNS ON A CLICK, NOT ON EVERY BUILD
# Doing this for all twenty-odd territories at build time would add seconds
# to every mode switch and every reach drag, to produce figures nobody has
# asked to see yet. One territory at a time, when it is clicked, is the same
# work spread across the reading of it.
# ══════════════════════════════════════════════════════════════════════════
GRID_N = 50
SITE_LAYER = "site_locations"
DESA_LAYER = "kelurahan"


def _nk(v):
    """Upper-cased and stripped of punctuation -- the same shape of key the
    rest of the application joins desa names on."""
    return "".join(c for c in str(v or "").upper() if c.isalnum())


def _bbox_of(rings):
    xs = [c[0] for poly in rings for ring in poly for c in ring]
    ys = [c[1] for poly in rings for ring in poly for c in ring]
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


def _overlaps(a, b):
    if not a or not b or None in a or None in b:
        return False
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def _covered_pct(dpolys, dbox, tpolys, tbox):
    """Percent of the desa that lies inside the territory."""
    x0, y0, x1, y1 = dbox
    if x1 <= x0 or y1 <= y0:
        return 0.0
    inside = both = 0
    for i in range(GRID_N):
        lon = x0 + (x1 - x0) * (i + 0.5) / GRID_N
        for j in range(GRID_N):
            lat = y0 + (y1 - y0) * (j + 0.5) / GRID_N
            if not geom.contains(dpolys, lon, lat):
                continue
            inside += 1
            if tbox[0] <= lon <= tbox[2] and tbox[1] <= lat <= tbox[3] \
                    and geom.contains(tpolys, lon, lat):
                both += 1
    return (both * 100.0 / inside) if inside else 0.0


def _desa_row(d):
    a = d["attrs"] or {}
    b = d["bbox"]
    lat0 = (b[1] + b[3]) / 2.0
    try:
        pop = int(float(a.get("Jumlah_Pen") or 0))
    except (TypeError, ValueError):
        pop = 0
    return {
        "desa": a.get("KEL_DES") or d["name"] or "",
        "kecamatan": a.get("KEC") or "",
        "kabupaten": a.get("KAB_KOT") or "",
        "mc": a.get("MC281") or a.get("MC260") or "",
        "code": a.get("connect") or d["feature_key"] or "",
        "population": pop,
        "desa_km2": round(siting.polys_area_km2(d["polys"], lat0), 3),
    }


_SITES_CACHE = {}


def _desa_sites(con):
    """{desa|kecamatan|kabupaten: masts} from ref_metric.

    THIS REPLACED A POINT-IN-POLYGON PASS OVER THE MAST LAYER, AND HAD TO
    The mast layer is not published to a shared server -- site locations are
    read from the user's own file -- so a figure computed from its geometry
    returns 0 everywhere the moment the layer is absent. Not an error, just
    a wrong number on every hover, with nothing to notice it by.

    ref_metric carries the same count per desa from the NCP village file. It
    matches all 7,761 desa exactly and totals 15,921 against the 15,919 rows
    in the mast layer, the two extra being masts the geometry pass could not
    place inside any desa. The reference figure is therefore no worse than
    what it replaces, and it survives the layer being dropped.

    Cached: it is one small table and it changes only on re-import."""
    if _SITES_CACHE.get("by_key") is not None:
        return _SITES_CACHE["by_key"]
    by_key = {}
    try:
        for r in con.execute("SELECT entity_key, value_num FROM ref_metric "
                             "WHERE metric = 'sites_all'"):
            p = str(r["entity_key"] or "").split("|")
            if len(p) < 3:
                continue
            by_key[_nk(p[0]) + "|" + _nk(p[1]) + "|" + _nk(p[2])] = \
                int(r["value_num"] or 0)
    except sqlite3.OperationalError:
        by_key = {}
    _SITES_CACHE["by_key"] = by_key
    return by_key


def _sites_of_row(con, row):
    """The mast count for one desa row built by _desa_row()."""
    return _desa_sites(con).get(
        _nk(row.get("desa")) + "|" + _nk(row.get("kecamatan")) + "|"
        + _nk(row.get("kabupaten")), 0)


def _sites_for_rows(con, rows):
    """Fill each desa row's mast count, and total them for the shape.

    WHAT THIS NUMBER NOW MEANS, AND WHAT IT NO LONGER MEANS
    It is the masts in the DESA THIS AREA COVERS, not the masts strictly
    inside the drawn border. Those were the same question when the answer
    came from mast coordinates and they are not any more: a rep holding
    half a desa is credited with that desa's masts.

    Reported rather than hidden -- the response carries `sites_basis` so the
    page can say "desa covered" beside the figure instead of implying a
    precision the data no longer supports.
    """
    total = 0
    for row in rows:
        n = _sites_of_row(con, row)
        row["sites"] = n
        total += n
    return total


_STATS_CACHE = {}


@bp.route("/api/samples/area-stats")
def api_area_stats():
    """Every desa's size, masts and people — once, for the whole map.

    WHY THIS IS ONE BIG RESPONSE AND NOT A LOOKUP PER HOVER
    The tooltip has to answer "how big is this, how many masts, how many
    desa" the instant a cursor crosses a boundary. A request per hover would
    be hundreds of requests across one sweep of the mouse, and the answer
    would arrive after the cursor had left. So the whole table is sent once,
    on the first hover that needs it, and every later hover is a dictionary
    lookup in the page.

    It is sent as bare arrays rather than objects because the field names
    repeated 7,761 times are most of the bytes: [desa, kec, kab, mc, km2,
    sites, population] is about a third the size of the same rows with keys.

    Kecamatan, microcluster and kabupaten totals are NOT sent. Desa nest
    inside all three, so the browser adds them up from this one table --
    which also means the coarse figures can never disagree with the fine
    ones, because there is only one set of numbers.

    Cached in memory: point-in-polygon for 15,919 masts is seconds of work
    and the answer only changes when a layer is re-imported.
    """
    stamp = _STATS_CACHE.get("stamp")
    con = db.connect()
    try:
        try:
            row = con.execute(
                "SELECT count(*) n, max(coalesce(imported_utc,'')) t "
                "FROM geo_feature WHERE layer_key = ?",
                (DESA_LAYER,)).fetchone()
            # The site figures come from ref_metric now, so the cache has to
            # notice ref_metric changing. Keyed on the mast layer it would
            # have served a stale table after every NCP refresh.
            try:
                m = con.execute(
                    "SELECT count(*) n, max(coalesce(imported_utc,'')) t "
                    "FROM ref_metric WHERE metric = 'sites_all'").fetchone()
                mstamp = "%s|%s" % (m["n"], m["t"])
            except sqlite3.OperationalError:
                mstamp = "-"
            now = "%s|%s|%s" % (row["n"], row["t"], mstamp)
        except sqlite3.OperationalError:
            now = "?"
        if stamp == now and _STATS_CACHE.get("body"):
            return jsonify(_STATS_CACHE["body"])

        desa = geom.load_polys(con, DESA_LAYER)
        rows, keys = [], []
        for d in desa:
            b = d["bbox"]
            if None in (b or (None,)):
                continue
            a = d["attrs"] or {}
            lat0 = (b[1] + b[3]) / 2.0
            try:
                pop = int(float(a.get("Jumlah_Pen") or 0))
            except (TypeError, ValueError):
                pop = 0
            rec = [a.get("KEL_DES") or d["name"] or "",
                   a.get("KEC") or "", a.get("KAB_KOT") or "",
                   a.get("MC281") or a.get("MC260") or "",
                   round(siting.polys_area_km2(d["polys"], lat0), 3), 0, pop]
            rows.append(rec)
            keys.append(_nk(rec[0]) + "|" + _nk(rec[1]) + "|" + _nk(rec[2]))

        # THE SITE COUNT COMES FROM ref_metric, NOT FROM MAST GEOMETRY
        #
        # This used to be an indexed point-in-polygon pass over 15,919 mast
        # coordinates. It worked, and it had to go: the mast layer is not
        # published to a shared server -- site locations are read from the
        # user's own file -- and a count computed from a layer that is not
        # there returns 0 for every desa. Not an error. Just a wrong number,
        # on every hover, with nothing to notice it by.
        #
        # ref_metric carries the same figure per desa from the NCP village
        # file, keyed desa|kecamatan|kabupaten. It matches all 7,761 desa
        # exactly, and totals 15,921 against the 15,919 masts in the layer --
        # the two extra being masts the geometry pass could not place in any
        # desa. So the reference figure is if anything the more complete of
        # the two, and it survives the layer being dropped.
        try:
            by_key = {}
            for r in con.execute(
                    "SELECT entity_key, value_num FROM ref_metric "
                    "WHERE metric = 'sites_all'"):
                p = str(r["entity_key"] or "").split("|")
                if len(p) < 3:
                    continue
                by_key[_nk(p[0]) + "|" + _nk(p[1]) + "|" + _nk(p[2])] = \
                    int(r["value_num"] or 0)
            for i, k in enumerate(keys):
                rows[i][5] = by_key.get(k, 0)
        except sqlite3.OperationalError:
            pass          # no ref_metric yet: the column stays 0 and says so
    except sqlite3.OperationalError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    finally:
        con.close()

    # MICROCLUSTER TOTALS COME THROUGH ref_kecamatan, NOT THROUGH THE NAME
    # ON THE DESA
    # The microcluster boundary layer calls a place MC-KOTA CILEGON while
    # the desa rows under it carry a different label entirely -- the same
    # naming disagreement the DSE roster resolves by going through
    # ref_kecamatan rather than trusting either label. So the roll-up is
    # built the same way: desa -> kecamatan -> ref_kecamatan.mc. Summing the
    # mc column on the desa rows instead would silently report nothing for
    # every microcluster whose two names disagree.
    mc_totals = {}
    try:
        con2 = db.connect()
        try:
            cols = territory_api._cols(con2, "ref_kecamatan")
            mc_col = "mc36" if "mc36" in cols else "mc"
            kec_mc = {}
            for r in con2.execute(
                    "SELECT upper(trim(kecamatan)) k, %s mc "
                    "FROM ref_kecamatan" % mc_col):
                if r["mc"]:
                    kec_mc.setdefault(_nk(r["k"]), str(r["mc"]).strip())
        finally:
            con2.close()
        for r in rows:
            mc = kec_mc.get(_nk(r[1]))
            if not mc:
                continue
            t = mc_totals.setdefault(mc, [0.0, 0, 0, 0])
            t[0] += r[4]; t[1] += r[5]; t[2] += r[6]; t[3] += 1
        for k in mc_totals:
            mc_totals[k][0] = round(mc_totals[k][0], 3)
    except sqlite3.OperationalError:
        mc_totals = {}

    body = {"ok": True,
            "cols": ["desa", "kecamatan", "kabupaten", "mc", "km2", "sites",
                     "population"],
            "mc_cols": ["km2", "sites", "population", "desa"],
            "mc_totals": mc_totals,
            "rows": rows}
    _STATS_CACHE["stamp"] = now
    _STATS_CACHE["body"] = body
    return jsonify(body)


@bp.route("/api/samples/area-profile", methods=["POST"])
def api_area_profile():
    """The desa breakdown behind one drawn territory.

    NOTHING OF THE WORKBOOK IS SENT TO GET THIS. The geometry posted here is
    a polygon this server drew itself and handed to the page a moment ago; it
    is coming back so the page does not have to keep a second copy of the
    desa layer to intersect it against. Outlet codes, names and every other
    column stay in the browser, where the outlet half of the panel is built.
    """
    payload = request.get_json(silent=True) or {}
    g = payload.get("geometry")
    if not g:
        return jsonify({"ok": False, "error": "No geometry was sent."}), 400
    tpolys = geom.rings_of(g)
    tbox = _bbox_of(tpolys)
    if not tbox:
        return jsonify({"ok": False,
                        "error": "That geometry has no usable ring."}), 400

    con = db.connect()
    try:
        lat0 = (tbox[1] + tbox[3]) / 2.0
        area_km2 = siting.polys_area_km2(tpolys, lat0)
        rows = []
        for d in geom.load_polys(con, DESA_LAYER):
            if not _overlaps(tbox, d["bbox"]):
                continue
            pct = _covered_pct(d["polys"], d["bbox"], tpolys, tbox)
            # Under half a percent is a boundary file's rounding, not a desa
            # this rep works, and listing it would bury the four that matter.
            if pct < 0.5:
                continue
            row = _desa_row(d)
            row["pct"] = round(pct, 1)
            row["inside_km2"] = round(row["desa_km2"] * pct / 100.0, 3)
            row["sites"] = 0
            row["_polys"] = d["polys"]
            row["_box"] = d["bbox"]
            rows.append(row)
        sites = _sites_for_rows(con, rows)
    except sqlite3.OperationalError as exc:
        con.close()
        return jsonify({"ok": False, "error": str(exc)}), 503
    else:
        con.close()

    for row in rows:
        row.pop("_polys", None)
        row.pop("_box", None)
    rows.sort(key=lambda r: (-r["pct"], -r["inside_km2"]))
    held = sum(r["inside_km2"] for r in rows)
    touched = sum(r["desa_km2"] for r in rows)
    return jsonify({
        "ok": True,
        "dse": (payload.get("dse") or "").strip(),
        "mode": (payload.get("mode") or "").strip(),
        "area_km2": round(area_km2, 3),
        "sites": sites,
        "sites_basis": "desa covered",
        "desa": rows,
        "desa_count": len(rows),
        "desa_full": sum(1 for r in rows if r["pct"] >= 99.0),
        "desa_partial": sum(1 for r in rows if r["pct"] < 99.0),
        # Population is pro-rated by the share of each desa held, because
        # holding a tenth of a desa is not holding its people.
        "population": int(sum(r["population"] * r["pct"] / 100.0
                              for r in rows)),
        "coverage_pct": round(held * 100.0 / touched, 1) if touched else None,
        "grid": GRID_N,
    })


@bp.route("/api/samples/desa-profile")
def api_desa_profile():
    """One desa on its own terms: how big it is, who lives in it, and how
    many masts stand in it. Nothing about outlets is here -- that half is
    answered in the browser from the workbook, and asking the server for it
    would mean sending the workbook."""
    want = _nk(request.args.get("desa"))
    kec = _nk(request.args.get("kecamatan"))
    if not want:
        return jsonify({"ok": False, "error": "No desa was named."}), 400
    con = db.connect()
    try:
        hit = None
        for d in geom.load_polys(con, DESA_LAYER):
            a = d["attrs"] or {}
            if _nk(a.get("KEL_DES") or d["name"]) != want:
                continue
            # Desa names repeat across kecamatan -- MEKARSARI is four
            # different places -- so the kecamatan settles it when given.
            if kec and _nk(a.get("KEC")) != kec:
                continue
            hit = d
            break
        if hit is None:
            return jsonify({"ok": False, "error":
                            "No desa by that name is in the layer."}), 404
        row = _desa_row(hit)
        row["sites"] = _sites_of_row(con, row)
    except sqlite3.OperationalError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    finally:
        con.close()
    row["ok"] = True
    return jsonify(row)


@bp.route("/api/samples/model.geojson", methods=["POST"])
def api_sample_model():
    """Build a territory model from a sample the browser is holding.

    NOTHING IS WRITTEN. This is the one thing the page cannot do for itself:
    Exclusive, Coverage and Force fit are the application's own definitions
    of a territory, and reimplementing them in the browser would be a second
    answer to "what is this rep's patch" that could disagree with the first.
    So the points are computed here and forgotten -- no row, no file, no
    cache key.

    Only (DSE, lat, lon) crosses the wire. Outlet names, codes, partner and
    the rest of the workbook stay in the browser, because the model does not
    need them and the less of somebody's working file travels, the better.
    """
    payload = request.get_json(silent=True) or {}
    pts_in = payload.get("points") or []
    if not pts_in:
        return jsonify({"ok": False, "error": "No points were sent."}), 400
    if len(pts_in) > config.SAMPLE_MODEL_MAX:
        return jsonify({"ok": False, "error":
                        f"{len(pts_in):,} points is more than this instance "
                        f"models at once "
                        f"({config.SAMPLE_MODEL_MAX:,})."}), 400
    mode = payload.get("mode", "exclusive")
    if mode not in ("exclusive", "coverage", "forcefit"):
        mode = "exclusive"
    try:
        cell = max(50.0, min(1000.0, float(payload.get("cell", 200))))
        reach = max(cell, min(3000.0, float(payload.get("reach", 400))))
    except (TypeError, ValueError):
        return jsonify({"ok": False,
                        "error": "cell and reach must be numeric"}), 400

    points, groups = [], {}
    for row in pts_in:
        try:
            dse = str(row[0]).strip()
            lat, lon = float(row[1]), float(row[2])
        except (TypeError, ValueError, IndexError):
            continue
        if not dse or not config.on_map(lat, lon):
            continue
        points.append((dse, lat, lon))
        groups.setdefault(dse, []).append((lat, lon))
    if not groups:
        return jsonify({"ok": False, "empty": True, "error":
                        "None of those points carry a DSE code and a "
                        "coordinate inside " + config.HOME_NAME + "."}), 200

    con = db.connect()
    try:
        if mode == "forcefit":
            feats, report = territory_api.force_fit_features(
                con, points, cell=cell, placed={})
            if not feats:
                return jsonify({"ok": False, "empty": True,
                                "error": report.get("error",
                                                    "nothing to fit")}), 200
            skipped, stats, seam = [], {}, 0
        else:
            feats, skipped, stats = dse_coverage.build(
                groups, mode=mode, cell=cell, reach=reach)
            report = None
            seam = 0
            by_key = {}
            for ft in feats:
                by_key.setdefault(ft["properties"]["dse"], []).append(ft)
            for key, pts in groups.items():
                mine = by_key.get(key, [])
                if mine:
                    seam += sum(1 for la, lo in pts
                                if dse_coverage.assign(mine, la, lo) != key)
        feats = _sample_counts(con, feats, points)
    except sqlite3.OperationalError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    finally:
        con.close()

    return jsonify({
        "type": "FeatureCollection", "name": "sample:model", "ok": True,
        "mode": mode, "reach": reach, "cell": cell,
        "groups": len(groups), "drawn": len(feats), "skipped": skipped,
        "points": len(points), "seam": seam,
        "area_km2": round(sum(f["properties"]["area_km2"] for f in feats), 1),
        "overlap_pct": (stats or {}).get("overlap_pct"),
        "islands_absorbed": (stats or {}).get("islands_absorbed"),
        "islands_kept": (stats or {}).get("islands_kept"),
        "forcefit": report, "features": feats})


@bp.route("/api/samples/<int:sid>/points.geojson")
def api_sample_points(sid):
    con = db.connect()
    try:
        if not samples.one(con, sid):
            return jsonify({"ok": False, "error": "no such sample"}), 404
        fc = samples.points(con, sid)
        fc["roster"] = samples.roster(con, sid)
    finally:
        con.close()
    return jsonify(fc)


@bp.route("/api/samples/<int:sid>/delete", methods=["POST"])
def api_sample_delete(sid):
    """Remove a kept sample. The PIN is a guard against a careless click --
    it is not access control, and it is not treated as any."""
    payload = request.get_json(silent=True) or request.form
    pin = (payload.get("pin") or "").strip()
    if pin != config.SAMPLE_PIN:
        return jsonify({"ok": False, "error":
                        "That PIN does not match."}), 403
    con = db.connect()
    try:
        row = samples.one(con, sid)
        if not row:
            return jsonify({"ok": False, "error": "no such sample"}), 404
        stored = samples.delete(con, sid)
    finally:
        con.close()
    removed = None
    if stored:
        try:
            removed, _p = fstore.remove(stored)
        except (FileNotFoundError, OSError):
            removed = None
    return jsonify({"ok": True, "id": sid, "label": row["label"],
                    "file": removed})


@bp.route("/api/configuration/template/<key>")
def api_template(key):
    spec = BY_KEY.get(key)
    if not spec or not spec.get("template"):
        return jsonify({"ok": False,
                        "error": "no template for that layer"}), 404
    path = os.path.join(TEMPLATE_DIR, spec["template"])
    if not os.path.isfile(path):
        return jsonify({"ok": False, "error":
                        f"{spec['template']} is not in Data Templates/"}), 404
    return send_file(path, as_attachment=True,
                     download_name=spec["template"])


# ── the pages ────────────────────────────────────────────────────────────
def _menus():
    """Imported at call time. territory_api imports plenty; keeping it out of
    this module's import line stops a cycle the moment app.py registers both
    blueprints in either order."""
    import territory_api
    return territory_api.MENUS


@bp.route("/configuration")
def page_configuration():
    return render_template("configuration.html", menus=_menus(),
                           active="config", page_title="Configuration",
                           slots=SLOTS, by_slot=BY_SLOT, layers=LAYERS,
                           derivations=DERIVATIONS)


@bp.route("/layer-model")
def page_layer_model():
    return render_template("layer_model.html", menus=_menus(),
                           active="layers", page_title="Layer Model")


@bp.route("/api/layer-model")
def api_layer_model():
    """The stack as it actually stands, in draw order.

    The Layer Model page is not a diagram of an intention — it reports what
    is loaded. A tier with nothing in it is drawn empty and labelled, which
    is the difference between a picture and a status."""
    con = db.connect()
    try:
        state = status(con)
        layers = _geo_layers(con)
    finally:
        con.close()
    by_key = {s["key"]: s for s in state}

    def tier(name, keys, blurb):
        items = []
        for k in keys:
            s = by_key[k]
            items.append({"key": k, "label": s["label"], "loaded": s["loaded"],
                          "rows": s["rows"], "fields": s["fields"],
                          "why": s["why"], "files": len(s["files"]),
                          # The tier is the mode: only the reference tier is
                          # counted in table rows, everything else in features.
                          "mode": name})
        return {"tier": name, "blurb": blurb, "items": items,
                "loaded": sum(1 for i in items if i["loaded"]),
                "total": len(items)}

    # Anything imported that no declared input claims. Surfacing it beats
    # hiding it: an unexpected layer on the map is exactly the thing you want
    # named, and the parent app had no place that named one.
    claimed = set()
    for s in state:
        for f in s["files"]:
            claimed.add(f["layer_key"])
    extra = [{"layer_key": g["layer_key"], "label": g.get("label"),
              "features": g.get("feature_count") or 0,
              "kind": g.get("kind"), "source_file": g.get("source_file")}
             for g in layers if g["layer_key"] not in claimed]

    return jsonify({
        "ok": True,
        "tiers": [
            tier("base", ["desa_poly", "kec_poly", "kab_poly", "territory",
                          "site"],
                 "Published geography. Loaded once, never derived, never "
                 "redrawn by anything above it."),
            tier("analysis", ["outlet"],
                 "Stamped onto the base grain. Switching one off changes "
                 "nothing beneath it."),
            tier("reference", ["desa_profile"],
                 "Figures the map quotes but never draws. Each one names the "
                 "grain it was measured at."),
        ],
        "unclaimed": extra,
        "rules": [
            {"n": "Rule 1", "title": "A base layer is never derived",
             "body": "Desa, kecamatan, microcluster and sites come from a "
                     "published file. Where two disagree both are shown and "
                     "the disagreement is logged, never reconciled away."},
            {"n": "Rule 2", "title": "An overlay names its grain",
             "body": "Every measured figure carries the unit it was measured "
                     "at. A kecamatan number is never shown as if it were a "
                     "desa number."},
            {"n": "Rule 3", "title": "Missing is not zero",
             "body": "An input this database does not hold is reported as "
                     "missing. A zero and a missing measurement rank "
                     "differently, and only one of them is honest."},
        ],
    })


@bp.route("/api/field-model")
def api_field_model():
    """What each loaded layer shows, filters on, and computes.

    One endpoint so the map card, the filter panel and any future chart all
    read the same answer. Filter values are counted from what is ACTUALLY
    loaded rather than from the registry — a filter offering a value that
    returns nothing is worse than no filter."""
    want = (request.args.get("layer") or "").strip()
    con = db.connect()
    try:
        loaded = {r["layer_key"]: r["feature_count"] for r in con.execute(
            "SELECT layer_key, feature_count FROM geo_layer")}
        out = []
        for spec in kml.LAYERS:
            lk = spec["layer_key"]
            if want and want not in (spec["key"], lk):
                continue
            entry = {
                "key": spec["key"], "layer_key": lk, "label": spec["label"],
                "kind": spec["kind"], "loaded": loaded.get(lk, 0),
                "preview": [{"field": f, "label": l,
                             "derived": f.startswith("_")}
                            for f, l in spec["preview"]],
                "context": spec.get("context", []),
                "derived": [{"field": d, "label": kml.DERIVED_LABEL.get(d, d)}
                            for d in spec.get("derive", ())],
                "filters": [],
            }
            CAP = 60
            for f in spec.get("filters", ()):
                vals, distinct = [], 0
                if loaded.get(lk):
                    path_expr = "$." + f.replace('"', '')
                    try:
                        # json_extract keeps this to one pass over the layer
                        # instead of decoding every attrs_json in Python.
                        distinct = con.execute(
                            "SELECT count(DISTINCT json_extract(attrs_json, ?))"
                            " FROM geo_feature WHERE layer_key = ?",
                            (path_expr, lk)).fetchone()[0]
                        vals = [{"value": r[0], "n": r[1]} for r in con.execute(
                            "SELECT json_extract(attrs_json, ?) v, count(*) n "
                            "FROM geo_feature WHERE layer_key = ? "
                            "AND v IS NOT NULL AND v <> '' "
                            "GROUP BY v ORDER BY n DESC LIMIT ?",
                            (path_expr, lk, CAP))]
                    except sqlite3.OperationalError:
                        vals, distinct = [], 0
                # `distinct` is the real count; `values` is the first CAP of
                # them by frequency. Reporting len(values) as the distinct
                # count would say every large filter has exactly 60 values,
                # which is the sort of number nobody questions.
                entry["filters"].append({
                    "field": f, "label": f, "distinct": distinct,
                    "returned": len(vals), "capped": distinct > len(vals),
                    "values": vals})
            out.append(entry)
    finally:
        con.close()
    return jsonify({"ok": True, "scope": list(formats.REGIONS),
                    "provinces": list(formats.SCOPE_PROV), "layers": out})
