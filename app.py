#!/usr/bin/env python3
"""
NEW POLYGON PJP DSE — a sibling of API Location Pulldown that
carries only the four pages that matter for territory work, and puts the
loading of the base data layers on a page of its own.

It is a separate application with its own database and its own layer folder;
nothing here reads or writes the parent app on 5001.

    python app.py            -> http://127.0.0.1:5002
"""
import os
import sqlite3
import subprocess
import sys

from flask import (Flask, Response, jsonify, redirect,
                   render_template, request)

from werkzeug.middleware.proxy_fix import ProxyFix

import config
import configuration
import db
import territory_api

app = Flask(__name__)
app.config["SECRET_KEY"] = config.SECRET_KEY
app.config["MAX_CONTENT_LENGTH"] = int(
    os.getenv("MAX_UPLOAD_MB", "1024")) * 1024 * 1024

# ══════════════════════════════════════════════════════════════════════════
# PUBLIC MODE — a fail-closed guard, not thirteen decorators
#
# This application has thirteen endpoints that change what is stored: layer
# uploads, imports, derivations, sample keeps and deletes. On a shared server
# none of them should answer. Marking each one by hand would be one forgotten
# decorator away from exposing the lot, and the fourteenth -- written months
# from now by someone who never read this file -- would be exposed by
# default.
#
# So the rule is inverted. Anything that is not a read is refused unless its
# path is on the allowlist below. A new endpoint is safe the day it is
# written; making it public is a deliberate edit here.
#
# /api/samples/model.geojson is the single exception and belongs there: it
# computes a territory from points the browser sends and keeps nothing --
# no row, no file, no cache key.
# ══════════════════════════════════════════════════════════════════════════
#
# TWO EXCEPTIONS, AND THEY ARE THE SAME EXCEPTION TWICE
# Both take a geometry the browser is holding, measure it against the
# boundary layers, and return the answer. Neither writes a row, a file or a
# cache key, and neither is sent a column of anybody's workbook -- the
# polygon posted to area-profile is one this server drew and handed to the
# page a moment ago, coming back so the page need not keep a second copy of
# the desa layer to intersect it against.
#
# area-profile was missing here, and the symptom was not an error: the
# ledger under the map simply said "Nothing to show yet" for every border
# clicked on a public instance, with the tiles beside it correctly filled
# from the model. A refusal that looks like an empty result is the worst
# kind, so the guard now says which path it refused.
PUBLIC_WRITE_ALLOW = frozenset({"/api/samples/model.geojson",
                                "/api/samples/area-profile"})
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


@app.before_request
def _public_mode_guard():
    if not config.PUBLIC_MODE:
        return None
    if request.method in READ_METHODS:
        return None
    if request.path in PUBLIC_WRITE_ALLOW:
        return None
    return jsonify({
        "ok": False, "public_mode": True, "path": request.path,
        "error": "This is a shared, read-only instance. Loading and "
                 "changing data layers is done on the local copy and "
                 "published from there. You can still preview your own "
                 "workbook here — it is read in your browser and is not "
                 "uploaded."}), 403


# ══════════════════════════════════════════════════════════════════════════
# WORKSPACE-ONLY MODE — one page is published, not the application
#
# The guard above refuses writes. This one refuses everything that is not
# part of Map Workspace, and it is a separate guard on purpose: they answer
# different questions, and folding them together would mean a future change
# to one silently loosening the other.
#
# It runs SECOND, so a write to an allowlisted path is still refused by the
# first guard. Both are fail-closed: a route added next year is invisible
# here until somebody adds it to config.WORKSPACE_READS on purpose.
#
# 404, not 403. A 403 confirms the path exists and is merely forbidden,
# which tells a stranger what this server is running. A 404 tells them
# nothing.
# ══════════════════════════════════════════════════════════════════════════
@app.before_request
def _workspace_only_guard():
    if not config.WORKSPACE_ONLY:
        return None
    p = request.path.rstrip("/") or "/"
    if p == "/":
        return redirect(config.WORKSPACE_PAGE, code=302)
    # The page cannot render without its own stylesheets and scripts.
    if p.startswith("/static/"):
        return None
    if p in config.WORKSPACE_READS:
        return None
    return render_template("404_workspace.html"), 404


app.register_blueprint(territory_api.bp)
app.register_blueprint(configuration.bp)

TABLE_FOR = {"overture": "poi_overture", "google": "poi_google",
             "osm": "poi_osm", "unified": "poi_unified"}

DISPLAY_COLUMNS = [
    ("name", "Name"), ("brand_resolved", "Brand"), ("category", "Category"),
    ("confidence", "Conf."), ("rating", "Rating"), ("address", "Address"),
    ("kecamatan", "Kecamatan"), ("locality", "Kota"),
    ("lat", "Lat"), ("lon", "Lon"), ("source_id", "ID"),
]


@app.context_processor
def globals_():
    ready = db.db_exists()
    c = {}
    if ready:
        con = db.connect()
        try:
            c = db.counts(con)
        finally:
            con.close()
    return {"db_ready": ready, "row_counts": c,
            "menus": territory_api.MENUS, "active_menu": "config",
            "google_ready": bool(config.GOOGLE_API_KEY),
            "overture_release": config.OVERTURE_RELEASE,
            "db_name": os.path.basename(config.DB_PATH)}


@app.route("/")
def index():
    """There is no Overview page here. An application whose first act is to
    ask for data should open on the screen that takes it, not on an empty
    table of POIs this app does not pull."""
    if config.WORKSPACE_ONLY:
        return redirect(config.WORKSPACE_PAGE, code=302)
    return redirect("/configuration", code=302)


def _guard():
    if not db.db_exists():
        return jsonify({"ok": False,
                        "error": "No database yet. Run `python migrate.py` "
                                 "then `python pull_overture.py --all`."}), 503
    return None


@app.route("/api/rows")
def api_rows():
    guard = _guard()
    if guard:
        return guard
    source = request.args.get("source", "overture")
    table = TABLE_FOR.get(source)
    if not table:
        return jsonify({"ok": False, "error": "unknown source"}), 400

    con = db.connect()
    try:
        rows = db.query(
            con, table,
            category=request.args.get("category") or None,
            brand=request.args.get("brand") or None,
            name_like=request.args.get("name") or None,
            limit=int(request.args.get("limit", 5000)))
    except sqlite3.OperationalError as exc:
        con.close()
        # poi_unified is a view built by reconcile.py, so it is absent until
        # that has run at least once. Say what to do rather than leaking a
        # bare "no such table" at the user.
        if table == "poi_unified":
            return jsonify({"ok": False,
                            "error": "The unified view does not exist yet — "
                                     "run `python reconcile.py` to build it "
                                     "from the source tables."}), 503
        return jsonify({"ok": False, "error": str(exc)}), 500
    con.close()
    return jsonify({"ok": True, "source": source, "count": len(rows),
                    "rows": rows})


@app.route("/api/facets")
def api_facets():
    """Only offer filter values that actually exist in the table — an empty
    dropdown is more honest than one full of options that return nothing."""
    guard = _guard()
    if guard:
        return guard
    table = TABLE_FOR.get(request.args.get("source", "overture"), "poi_overture")
    con = db.connect()
    try:
        cats = [r[0] for r in con.execute(
            f"SELECT category, count(*) n FROM {table} WHERE category IS NOT NULL "
            f"GROUP BY 1 ORDER BY n DESC")]
        brands = [r[0] for r in con.execute(
            f"SELECT brand_resolved, count(*) n FROM {table} "
            f"WHERE brand_resolved IS NOT NULL GROUP BY 1 ORDER BY n DESC")]
    except sqlite3.OperationalError:
        cats, brands = [], []
    con.close()
    return jsonify({"ok": True, "categories": cats, "brands": brands})


@app.route("/api/runs")
def api_runs():
    guard = _guard()
    if guard:
        return guard
    con = db.connect()
    out = db.runs(con, limit=30)
    con.close()
    return jsonify({"ok": True, "runs": out})


@app.route("/api/export")
def api_export():
    """Streams a CSV of the current filter straight to the browser. The
    xlsx path lives in export.py — this one is for a quick download."""
    guard = _guard()
    if guard:
        return guard
    import csv
    import io
    table = TABLE_FOR.get(request.args.get("source", "overture"), "poi_overture")
    con = db.connect()
    rows = db.query(con, table,
                    category=request.args.get("category") or None,
                    brand=request.args.get("brand") or None,
                    name_like=request.args.get("name") or None,
                    limit=1_000_000)
    con.close()
    buf = io.StringIO()
    if rows:
        w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{table}.csv"'})


@app.route("/api/export.geojson")
def api_export_geojson():
    """Same filter as the table, delivered as a map file. Drag straight into
    QGIS, or point Leaflet/Mapbox at it."""
    guard = _guard()
    if guard:
        return guard
    import json as _json
    table = TABLE_FOR.get(request.args.get("source", "overture"), "poi_overture")
    con = db.connect()
    rows = db.query(con, table,
                    category=request.args.get("category") or None,
                    brand=request.args.get("brand") or None,
                    name_like=request.args.get("name") or None,
                    limit=1_000_000)
    con.close()
    feats = [{"type": "Feature",
              "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
              "properties": {k: v for k, v in r.items() if k not in ("lat", "lon")}}
             for r in rows if r.get("lat") is not None and r.get("lon") is not None]
    fc = {"type": "FeatureCollection", "name": table,
          "crs": {"type": "name",
                  "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
          "features": feats}
    return Response(
        _json.dumps(fc, ensure_ascii=False), mimetype="application/geo+json",
        headers={"Content-Disposition": f'attachment; filename="{table}.geojson"'})


@app.route("/healthz")
def healthz():
    return jsonify({"ok": True, "db": config.DB_PATH,
                    "db_exists": db.db_exists(),
                    "google_ready": bool(config.GOOGLE_API_KEY),
                    "overture_release": config.OVERTURE_RELEASE})


# Behind nginx, Flask sees the proxy's address and scheme on every request.
# ProxyFix restores the visitor's, which matters for logs and for building a
# correct absolute URL. It is deliberately NOT used to decide who may write:
# a header a client can set is not a permission, which is the whole reason
# public mode does not look at addresses at all.
if os.getenv("BEHIND_PROXY", "1" if config.PUBLIC_MODE else "0") \
        .strip().lower() in ("1", "true", "yes", "on"):
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

if config.PUBLIC_MODE and not config.secret_key_ok():
    raise SystemExit(
        "PUBLIC_MODE is on but SECRET_KEY is the development placeholder.\n"
        "Generate one and put it in .env before starting:\n"
        "    python3 -c \"import secrets; print(secrets.token_urlsafe(48))\"")


if __name__ == "__main__":
    if not db.db_exists():
        print("No database found — running migrate.py first …")
        subprocess.run([sys.executable, "migrate.py"], check=False,
                       cwd=os.path.dirname(os.path.abspath(__file__)))
    port = int(os.getenv("PORT", "5002"))
    # Debug follows public mode rather than being hard-coded on. The
    # Werkzeug debugger is an interactive Python console on an exception:
    # priceless on a laptop, a remote shell for anyone who can reach the
    # page on a server. It was hard-coded True here, which is the single
    # most dangerous line in this repository once the port is reachable.
    debug = not config.PUBLIC_MODE and os.getenv(
        "FLASK_DEBUG", "1").strip().lower() in ("1", "true", "yes", "on")
    host = os.getenv("HOST", "127.0.0.1")
    if not os.getenv("BANNER_ALREADY_PRINTED"):
        where = "public mode" if config.PUBLIC_MODE else "local"
        print(f"\n  NEW POLYGON PJP DSE ({where})  ->  http://{host}:{port}\n")
        if config.PUBLIC_MODE:
            print("  NOTE: app.run is the development server. On a real "
                  "host use gunicorn — see DEPLOY.md.\n")
    app.run(host=host, port=port, debug=debug)
