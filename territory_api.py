"""
Territory overlay API — boundaries, service points and performance.

Registered on the main app as a blueprint. Everything here reads SQLite
only; nothing calls an API or costs money.

The design point worth knowing: kecamatan and MC are *independent* overlays,
not a hierarchy you drill through. An MC does not nest cleanly inside a
kabupaten and a kecamatan can be split across MCs, so both are served as
separate layers and a click reports whichever polygons contain the point --
possibly one of each, possibly neither.
"""
import collections
import gzip
import json
import os
import re
import sqlite3

from flask import Blueprint, Response, jsonify, render_template, request, send_file

import config
import db
import dse_coverage
import dissolve
import force_fit
import geom
import siting
import kmllayers
import mapcache
import mcprofile

bp = Blueprint("territory", __name__)

# metric key -> (label, format). Order is display order.
HEADLINE = [
    ("vlr_subs", "VLR subs", "int"),
    ("prepaid_revenue_nett", "Prepaid revenue (nett)", "idr"),
    ("sso", "SSO", "int"),
    ("site_id", "Sites", "int"),
    ("rank", "Rank", "int"),
]
SHARE = [
    ("fbms_ioh", "IOH", None),
    ("fbms_im3", "IM3", "IM3"),
    ("fbms_3id", "3ID", "3ID"),
    ("fbms_tsel", "Telkomsel", "TSEL"),
    ("fbms_xl", "XL", "XL"),
    ("fbms_sf", "Smartfren", "SF"),
]
# Summing a share across kecamatan is meaningless; summing subscribers is
# not. Anything not listed here is averaged weighted by VLR subs instead.
SUMMABLE = {"vlr_subs", "prepaid_revenue_nett", "sso", "site_id"}

_poly_cache = {}


# ── geometry ─────────────────────────────────────────────────────────────
# The maths lives in geom.py so the batch enricher can use it without
# importing Flask. These names are kept as thin aliases.
_rings = geom.rings_of
_in_ring = geom.in_ring
_contains = geom.contains


# ══════════════════════════════════════════════════════════════════════════
# Schema and attribute compatibility
#
# These preview endpoints came from the app on 5001, where the kecamatan
# reference table names its hierarchy columns mc36 / mc36_branch / mc36_area
# and every boundary polygon carries KEC / KABKOT / MC36. This app writes
# mc / sa / area / region, and its KML layers name the same things KEC or
# Kec or KECAMATAN, KAB_KOT or Kab_kot or KABKOT, "MC IOH". Querying a
# column that does not exist raised OperationalError, which the endpoints
# turned into a 503 -- which is why the Preview Polygon page drew nothing at
# all. Both generations are resolved here, once.
# ══════════════════════════════════════════════════════════════════════════
ALIAS = {
    "kecamatan": ("KEC", "KECAMATAN", "KEC_NAME", "NAMA_KEC"),
    "kabkot": ("KABKOT", "KAB_KOT", "KABUPATEN", "KOTA", "NAMA_KAB"),
    "mc": ("MC36", "MC IOH", "MC_IOH", "MC"),
    "region": ("REGION", "REGION_NAM", "REGION_NAME"),
}


def aval(attrs, which):
    """Read one logical field out of an attribute dict whatever the export
    happened to call it. Returns upper-cased and trimmed, or ""."""
    for n in ALIAS[which]:
        v = attrs.get(n)
        if v not in (None, ""):
            return str(v).strip().upper()
    return ""


def on_map(bbox):
    """Does this feature sit inside the area the application covers?

    Judged on the centroid, and applied to every layer on every request. The
    map is Inner Jakarta, Outer Jakarta and West Java; a polygon or a point
    anywhere else is either a national row that should never have been kept
    or a broken coordinate, and neither belongs on it."""
    x0, y0, x1, y1 = bbox
    return config.on_map((y0 + y1) / 2.0, (x0 + x1) / 2.0)


def _cols(con, table):
    try:
        return {r[1] for r in con.execute("PRAGMA table_info(%s)" % table)}
    except sqlite3.OperationalError:
        return set()


def kec_hier(con):
    """(KECAMATAN, KABKOT) -> (mc, branch, area, region) for every kecamatan
    the territory model knows, plus a (KECAMATAN, "") fallback for rows that
    carry no kabupaten, plus kecamatan -> kabupaten.

    A kecamatan the model does NOT know is not an error and is not dropped
    here -- callers hand it (None, None, None, None) and show it as outside
    the territory."""
    cols = _cols(con, "ref_kecamatan")
    if not cols:
        return {}, {}
    mc = "mc36" if "mc36" in cols else "mc"
    br = ("mc36_branch" if "mc36_branch" in cols
          else ("sa" if "sa" in cols else ("branch" if "branch" in cols else None)))
    ar = "mc36_area" if "mc36_area" in cols else ("area" if "area" in cols else None)
    rg = "region" if "region" in cols else None
    sel = ", ".join([mc] + [c or "NULL" for c in (br, ar, rg)])
    hier, kab_of = {}, {}
    for r in con.execute(
            "SELECT upper(trim(kecamatan)) k, upper(trim(kabkot)) kb, "
            + sel + " FROM ref_kecamatan"):
        row = (r[2], r[3], r[4], r[5])
        hier[(r["k"], r["kb"])] = row
        hier.setdefault((r["k"], ""), row)
        kab_of.setdefault(r["k"], r["kb"])
    return hier, kab_of


def kel_mc_col(con):
    """Which column on ref_kelurahan holds the microcluster."""
    cols = _cols(con, "ref_kelurahan")
    return "mc36" if "mc36" in cols else ("mc" if "mc" in cols else None)


def load_polys(con, layer_key):
    return geom.load_polys(con, layer_key)


def locate(con, layer_key, lat, lon):
    for f in geom.load_polys(con, layer_key):
        x0, y0, x1, y1 = f["bbox"]
        if x0 <= lon <= x1 and y0 <= lat <= y1 and \
                geom.contains(f["polys"], lon, lat):
            return f
    return None


def geojson(payload):
    """Geometry goes out compact, always.

    Flask's default JSON provider pretty-prints while debug is on, which
    turns the desa layer from 5 MB into 17 MB — the same polygons, three
    times the wire and three times the parse. Indentation is worth having on
    a twenty-line diagnostic response and worth nothing on a FeatureCollection
    no one reads by eye."""
    return geojson_bytes(_encode(payload))


def _encode(payload):
    """-> (body, gzipped?). Built once so a cache can hold the result."""
    body = json.dumps(payload, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")
    # Coordinate JSON compresses about 5:1, and these responses run to tens
    # of megabytes on the wider layers. Flask does not compress anything by
    # itself, so a 25 MB desa outline crossed localhost uncompressed and the
    # browser spent longer receiving it than drawing it.
    if len(body) > 262144:
        return gzip.compress(body, 6), True
    return body, False


def geojson_bytes(pair):
    body, gz = pair
    if gz and "gzip" not in (request.headers.get("Accept-Encoding") or ""):
        body, gz = gzip.decompress(body), False
    resp = Response(body, mimetype="application/json")
    if gz:
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Vary"] = "Accept-Encoding"
    return resp


# ── kelurahan / desa ─────────────────────────────────────────────────────
# Desa is a third *independent* overlay, on the same footing as kecamatan and
# MC. It nests inside kecamatan administratively, but the click handler still
# reports it separately rather than as a child node -- a point can land in a
# desa whose kecamatan polygon is missing, and hiding that behind a hierarchy
# would make the gap invisible.
KEL_FIELDS = ("kel_key", "unique_id", "kelurahan", "kecamatan", "kabkot",
              "prov", "mc", "branch", "area", "region", "circle", "pt",
              "partner", "mc35", "branch11", "population", "pop_index",
              "geo_type", "area_m2", "lat", "lon")


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def kel_row(con, join_key):
    try:
        return con.execute(
            "SELECT * FROM ref_kelurahan WHERE join_key=?",
            (join_key,)).fetchone()
    except sqlite3.OperationalError:
        return None


def kel_props(row):
    """Flat properties for the map. `pop_density` is derived here rather than
    stored, because area_m2 comes from the KMZ and population from the
    spreadsheet -- persisting a ratio across two sources is how the two end
    up disagreeing after one of them is reloaded."""
    out = {k: row[k] for k in KEL_FIELDS if k in row.keys()}
    area_km2 = (row["area_m2"] or 0) / 1e6
    pop = row["population"]
    out["area_km2"] = round(area_km2, 3) if area_km2 else None
    out["pop_density"] = (round(pop / area_km2) if pop and area_km2 > 0
                          else None)
    out["mc_changed"] = bool(row["mc35"] and row["mc"]
                             and row["mc35"] != row["mc"])
    return out


# ── metrics ──────────────────────────────────────────────────────────────
def metrics_for(con, entity_type, entity_key):
    """-> {metric: {period_or_'': value}}"""
    out = {}
    for r in con.execute(
            "SELECT metric, coalesce(period,'') p, value_num, value_text "
            "FROM ref_metric WHERE entity_type=? AND entity_key=?",
            (entity_type, entity_key)):
        out.setdefault(r["metric"], {})[r["p"]] = (
            r["value_num"] if r["value_num"] is not None else r["value_text"])
    return out


def latest(periods):
    """Prefer the most recent period. 'MTD 1708' sorts after '07', which is
    the order we want; '' (no period) is the fallback."""
    if not periods:
        return None, None
    keys = [k for k in periods if k]
    key = max(keys) if keys else ""
    return key or None, periods.get(key if keys else "")


def kec_rows_for_mc(con, mc):
    """Which kecamatan sit in this microcluster.

    `mc36` is the mapping derived from the current polygons by remap_mc.py;
    `mc` is the generation that shipped with the kecamatan spreadsheet. Try
    the derived one first and fall back, so a database that has not been
    remapped yet still answers, and a microcluster that only exists in the
    new cut is not silently blank."""
    key = str(mc).strip().upper()
    cols = {r[1] for r in con.execute("PRAGMA table_info(ref_kecamatan)")}
    if "mc36" in cols:
        rows = [r["kec_key"] for r in con.execute(
            "SELECT kec_key FROM ref_kecamatan WHERE trim(upper(mc36)) = ?",
            (key,))]
        if rows:
            return rows
    return [r["kec_key"] for r in con.execute(
        "SELECT kec_key FROM ref_kecamatan WHERE trim(upper(mc)) = ?", (key,))]


def aggregate_mc(con, mc):
    """MC figures are derived, never sourced: the profile is per kecamatan.
    Counts sum; shares are averaged weighted by VLR subs, because an
    unweighted mean of percentages across unequal populations is wrong."""
    keys = kec_rows_for_mc(con, mc)
    if not keys:
        return {}, 0
    acc, weights = {}, {}
    for k in keys:
        m = metrics_for(con, "kecamatan", k)
        wperiod, w = latest(m.get("vlr_subs", {}))
        w = w if isinstance(w, (int, float)) else 0.0
        for metric, periods in m.items():
            for period, val in periods.items():
                if not isinstance(val, (int, float)):
                    continue
                slot = acc.setdefault(metric, {}).setdefault(period, 0.0)
                if metric in SUMMABLE:
                    acc[metric][period] = slot + val
                else:
                    acc[metric][period] = slot + val * w
                    weights.setdefault(metric, {})
                    weights[metric][period] = \
                        weights[metric].get(period, 0.0) + w
    for metric, periods in acc.items():
        if metric in SUMMABLE:
            continue
        for period in list(periods):
            w = weights.get(metric, {}).get(period, 0.0)
            periods[period] = (periods[period] / w) if w else None
    return acc, len(keys)


def pack(metrics, derived_from=0):
    """Flatten to a UI-ready list, latest period first, period labelled."""
    out = []
    for key, label, _ in HEADLINE:
        periods = metrics.get(key, {})
        for period in sorted(periods, reverse=True):
            out.append({"key": key, "label": label, "period": period or None,
                        "value": periods[period], "fmt": dict(
                            (k, f) for k, _l, f in
                            [(a, b, c) for a, b, c in HEADLINE]).get(key)})
    shares = []
    for key, label, op in SHARE:
        periods = metrics.get(key, {})
        period, val = latest(periods)
        if val is None:
            continue
        shares.append({"key": key, "label": label, "operator": op,
                       "period": period, "value": val,
                       "color": config.OPERATORS.get(op, {}).get("color")
                       if op else "#c8cde0"})
    return {"rows": out, "shares": shares, "derived_from": derived_from}


# ── routes ───────────────────────────────────────────────────────────────
@bp.route("/territory")
def page():
    """The old path, kept alive — it is in the README and in muscle memory."""
    from flask import redirect
    return redirect("/retail-gapura")


def operator_meta():
    """Operator config with icon_url filled in only where the file is really
    there. Resolving it server-side means the browser never asks for a logo
    that does not exist, and adding one is a file copy with no code change."""
    import os
    base = os.path.join(config.BASE_DIR, "static", "img", "operators")
    out = {}
    for code, cfg in config.OPERATORS.items():
        m = dict(cfg)
        icon = cfg.get("icon")
        if icon and os.path.isfile(os.path.join(base, icon)):
            m["icon_url"] = "/static/img/operators/" + icon
        out[code] = m
    return out


@bp.route("/api/territory/layers")
def layers():
    # Retail Gapura asks here, so the default keeps the trade-channel layers
    # off this map even if the page forgets to say which project it is.
    domain = request.args.get("domain", "gapura")
    con = db.connect()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT layer_key, label, feature_count, source_file FROM geo_layer "
            "ORDER BY layer_key")
            if fstore.in_domain(domain, r["layer_key"], r["source_file"])]
        ops = [dict(code=c, count=n, sp_class=k) for c, k, n in con.execute(
            "SELECT operator, max(coalesce(sp_class,'operator')), count(*) "
            "FROM ref_service_point GROUP BY operator ORDER BY 3 DESC")]
        poi_cats = [dict(name=c, count=n) for c, n in con.execute(
            "SELECT category, count(*) n FROM poi_overture "
            "WHERE category IS NOT NULL GROUP BY 1 ORDER BY n DESC LIMIT 40")]
    except sqlite3.OperationalError as exc:
        con.close()
        return jsonify({"ok": False, "error": str(exc)}), 503
    con.close()
    for r in rows:
        r["style"] = config.BOUNDARY_STYLE.get(
            r["layer_key"], {"label": r["label"], "color": "#8b93a7",
                             "weight": 1.5, "dash": None, "fill": 0.04})
    return jsonify({"ok": True, "layers": rows, "operators": ops,
                    "operator_meta": operator_meta(),
                    "poi_categories": poi_cats})


def _bulk_props(con, layer_key):
    """-> a callable(rows) -> {feature_key: extra props}.

    The popup figures used to be fetched one feature at a time, inside the
    loop that built the response: 7,761 queries to draw one desa layer, on
    every single request. They are gathered in one pass here, and the whole
    thing now happens once per import rather than once per map."""
    def gather(rows):
        out = {}
        if layer_key == "kecamatan":
            by_join = {}
            for r in con.execute(
                    "SELECT kec_key, join_key FROM ref_kecamatan"):
                by_join[r["join_key"]] = r["kec_key"]
            for r in rows:
                kec_key = by_join.get(r["join_key"])
                if not kec_key:
                    continue
                m = metrics_for(con, "kecamatan", kec_key)
                out[r["feature_key"]] = {
                    "kec_key": kec_key,
                    "vlr": latest(m.get("vlr_subs", {}))[1],
                    "rev": latest(m.get("prepaid_revenue_nett", {}))[1],
                    "ioh": latest(m.get("fbms_ioh", {}))[1]}
        elif layer_key == "kelurahan":
            by_join = {}
            try:
                for r in con.execute("SELECT * FROM ref_kelurahan"):
                    by_join[r["join_key"]] = r
            except sqlite3.OperationalError:
                by_join = {}
            for r in rows:
                row = by_join.get(r["join_key"])
                if row is not None:
                    out[r["feature_key"]] = kel_props(row)
                else:
                    # No profile row: say so rather than showing a blank card
                    # that looks like a desa with no people in it.
                    out[r["feature_key"]] = {"profile_missing": True}
        elif layer_key == "indosat_mc":
            for r in rows:
                acc, n = aggregate_mc(con, r["feature_key"])
                out[r["feature_key"]] = {
                    "kec_count": n,
                    "vlr": latest(acc.get("vlr_subs", {}))[1],
                    "rev": latest(acc.get("prepaid_revenue_nett", {}))[1],
                    "ioh": latest(acc.get("fbms_ioh", {}))[1]}
        return out
    return gather


@bp.route("/api/territory/<layer_key>.geojson")
def layer_geojson(layer_key):
    """Serve the layer from its prebuilt file.

    Everything expensive -- joining the figures, simplifying the geometry,
    compressing it -- happened when the layer was imported. A request that
    finds a fresh cache does no work at all beyond streaming a file, which
    is the only way a 7,761-polygon layer previews instantly."""
    domain = request.args.get("domain")
    con = db.connect()
    try:
        if domain:
            r = con.execute("SELECT source_file FROM geo_layer WHERE layer_key=?",
                            (layer_key,)).fetchone()
            if not fstore.in_domain(domain, layer_key,
                                    r["source_file"] if r else None):
                return jsonify({"ok": False, "error":
                                "'%s' is not part of the %s data set"
                                % (layer_key, domain)}), 404
        if not mapcache.is_fresh(con, layer_key):
            mapcache.build(con, layer_key, props_for=_bulk_props(con, layer_key))
    finally:
        con.close()

    path = mapcache.path_for(layer_key)
    if not os.path.exists(path):
        return jsonify({"ok": False,
                        "error": f"no layer '{layer_key}' is loaded"}), 404
    resp = send_file(path, mimetype="application/geo+json",
                     conditional=True)
    # The file is stored gzipped and served gzipped -- the browser inflates
    # it. Without this header it would arrive as unreadable bytes.
    resp.headers["Content-Encoding"] = "gzip"
    resp.headers["Cache-Control"] = "no-cache"
    resp.direct_passthrough = True
    return resp


@bp.route("/api/territory/service-points.geojson")
def service_points():
    con = db.connect()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT sp_key, operator, sp_class, name, sp_type, address, lat, lon "
            "FROM ref_service_point WHERE lat IS NOT NULL")]
    except sqlite3.OperationalError:
        rows = []
    con.close()
    return geojson({"type": "FeatureCollection", "name": "service_points",
                    "features": [
                        {"type": "Feature",
                         "geometry": {"type": "Point",
                                      "coordinates": [r["lon"], r["lat"]]},
                         "properties": r} for r in rows]})


def territory_where(con, args):
    """Turn a territory selection into SQL over the POI columns.

    Area and branch are not on the POI rows, so they are resolved to the
    microclusters they contain and applied as an IN list. The two families
    -- Indosat territory and administrative -- are alternatives, so whichever
    the caller sent is the one that is applied."""
    where, params = [], []
    mc = (args.get("mc") or "").strip()
    area = (args.get("area") or "").strip()
    branch = (args.get("branch") or "").strip()
    kabkot = (args.get("kabkot") or args.get("kota") or "").strip()
    kec = (args.get("kecamatan") or "").strip()

    if mc:
        where.append("mc_ioh = ?")
        params.append(mc)
    elif area or branch:
        rows, _ = tr.base_rows(con)
        want = {r["mc"] for r in rows if r["mc"] and (
            (not area or (r["area"] or "").strip().upper() == area.upper())
            and (not branch or (r["branch"] or "").strip().upper() == branch.upper()))}
        if want:
            where.append("mc_ioh IN (" + ",".join("?" * len(want)) + ")")
            params.extend(sorted(want))
        else:
            where.append("1=0")
    if kec:
        where.append("upper(adm_kecamatan) = ?")
        params.append(kec.upper())
    elif kabkot:
        where.append("upper(adm_kota) = ?")
        params.append(kabkot.upper())
    return where, params


@bp.route("/api/territory/poi.geojson")
def poi_geojson():
    table = {"overture": "poi_overture", "osm": "poi_osm",
             "google": "poi_google", "unified": "poi_unified"}.get(
                 request.args.get("source", "overture"), "poi_overture")
    con = db.connect()
    try:
        twhere, tparams = territory_where(con, request.args)
        if twhere:
            cols = catalog._cols(con, table)
            twhere = [w for w in twhere
                      if not (("mc_ioh" in w and "mc_ioh" not in cols)
                              or ("adm_kecamatan" in w and "adm_kecamatan" not in cols)
                              or ("adm_kota" in w and "adm_kota" not in cols))]
        if twhere:
            base, bargs = [], []
            for arg, col in (("category", "category"),
                             ("brand", "brand_resolved")):
                v = (request.args.get(arg) or "").strip()
                if v:
                    base.append(f"{col} = ?")
                    bargs.append(v)
            sql = (f"SELECT * FROM {table} WHERE "
                   + " AND ".join(base + twhere)
                   + " ORDER BY coalesce(brand_resolved, name) LIMIT ?")
            rows = [dict(r) for r in con.execute(
                sql, bargs + tparams + [int(request.args.get("limit", 4000))])]
        else:
            rows = db.query(con, table,
                            category=request.args.get("category") or None,
                            brand=request.args.get("brand") or None,
                            limit=int(request.args.get("limit", 4000)))
    except sqlite3.OperationalError as exc:
        con.close()
        return jsonify({"ok": False, "error": str(exc)}), 503
    con.close()
    return geojson({"type": "FeatureCollection", "name": table, "features": [
        {"type": "Feature",
         "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
         "properties": {"name": r.get("name"), "brand": r.get("brand_resolved"),
                        "category": r.get("category"),
                        "source": r.get("source"),
                        "n_sources": r.get("n_sources"),
                        "sources_seen": r.get("sources_seen")}}
        for r in rows if r.get("lat") is not None and r.get("lon") is not None]})


@bp.route("/api/territory/at")
def at():
    """What is under this point? Reports every overlay independently, so a
    click can return a kecamatan, an MC, both, or neither."""
    try:
        lat = float(request.args["lat"])
        lon = float(request.args["lon"])
    except (KeyError, ValueError):
        return jsonify({"ok": False, "error": "lat and lon required"}), 400

    con = db.connect()
    out = {"ok": True, "lat": lat, "lon": lon, "hits": []}

    kel = locate(con, "kelurahan", lat, lon)
    if kel:
        row = kel_row(con, kel["join_key"])
        a = {k.upper(): v for k, v in kel["attrs"].items()}
        p = kel_props(row) if row else {}
        facts = [
            ("Population", p.get("population") or _int(a.get("POPULATION")),
             "int"),
            ("Population / km2", p.get("pop_density"), "int"),
            ("Area", p.get("area_km2"), "km2"),
            ("Geo type", p.get("geo_type") or a.get("GEO_TYPE"), None),
            ("Microcluster (24)", p.get("mc") or a.get("MC"), None),
            ("Branch (9)", p.get("branch") or a.get("BRANCH"), None),
            ("Microcluster (35)", p.get("mc35"), None),
            ("Branch (11)", p.get("branch11"), None),
            ("Area", p.get("area") or a.get("AREA"), None),
            ("Region", p.get("region") or a.get("REGION"), None),
            ("Partner / AF", p.get("partner") or a.get("PARTNER"), None),
            ("BPS Unique_ID", p.get("unique_id") or kel["feature_key"], None),
        ]
        out["hits"].append({
            "layer": "kelurahan",
            "title": kel["name"] or p.get("kelurahan"),
            "subtitle": " · ".join(str(x) for x in (
                p.get("kecamatan") or a.get("KEC"),
                p.get("kabkot") or a.get("KABKOT")) if x),
            "style": config.BOUNDARY_STYLE["kelurahan"],
            "profile_missing": row is None,
            "note": ("The 35-MC re-split moves this desa to "
                     f"{p.get('mc35')}." if p.get("mc_changed") else None),
            "shares": [],
            "rows": [{"label": lab, "value": val, "fmt": fmt, "period": None}
                     for lab, val, fmt in facts if val not in (None, "")]})

    kec = locate(con, "kecamatan", lat, lon)
    if kec:
        row = con.execute("SELECT * FROM ref_kecamatan WHERE join_key=?",
                          (kec["join_key"],)).fetchone()
        m = metrics_for(con, "kecamatan", row["kec_key"]) if row else {}
        out["hits"].append({
            "layer": "kecamatan", "title": kec["name"],
            "subtitle": (f"{row['kabkot']} · {str(row['mc'] or '').strip()}"
                         if row else kec["attrs"].get("KABKOT")),
            "style": config.BOUNDARY_STYLE["kecamatan"],
            "profile_missing": row is None, **pack(m)})


    mc = locate(con, "indosat_mc", lat, lon)
    if mc:
        acc, n = aggregate_mc(con, mc["feature_key"])
        hier = con.execute("SELECT * FROM ref_mc WHERE mc=?",
                           (mc["feature_key"],)).fetchone()
        out["hits"].append({
            "layer": "indosat_mc", "title": mc["name"],
            "subtitle": (f"{hier['branch']} · {hier['area']} · {hier['region']}"
                         if hier else mc["attrs"].get("BRANCH")),
            "style": config.BOUNDARY_STYLE["indosat_mc"],
            "profile_missing": not acc, **pack(acc, derived_from=n)})

    # Service points inside the clicked kecamatan, split by operator.
    if kec or mc or kel:
        area = kel or kec or mc
        x0, y0, x1, y1 = area["bbox"]
        tally = {}
        for r in con.execute(
                "SELECT operator, lat, lon FROM ref_service_point "
                "WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
                (y0, y1, x0, x1)):
            if _contains(area["polys"], r["lon"], r["lat"]):
                tally[r["operator"]] = tally.get(r["operator"], 0) + 1
        out["service_points"] = [
            {"operator": k, "count": v,
             "label": config.OPERATORS.get(k, {}).get("label", k),
             "color": config.OPERATORS.get(k, {}).get("color", "#8b93a7")}
            for k, v in sorted(tally.items(), key=lambda x: -x[1])]
    con.close()
    return jsonify(out)


# ══════════════════════════════════════════════════════════════════════════
# Catalogue, insights and layer upload — the three menus
# ══════════════════════════════════════════════════════════════════════════
import catalog                                                    # noqa: E402
import territory_rollup as tr                                     # noqa: E402

MENUS = [
    {"key": "config",  "path": "/configuration",  "label": "Configuration",
     "blurb": "Load the base data layers and see what is still missing"},
    {"key": "distrib", "path": "/distribution",   "label": "Distribution Polygon",
     "blurb": "Outlets, DSE territory models and the boundaries beneath them"},
    {"key": "preview", "path": "/preview-polygon", "label": "Preview Polygon",
     "blurb": "Territory summary by area, branch and microcluster"},
    {"key": "samples", "path": "/polygon-samples", "label": "Polygon Samples",
     "blurb": "Try a DSE-to-outlet demarcation against the base layers — "
              "read in your browser, never uploaded"},
    {"key": "workspace", "path": "/map-workspace", "label": "Map Workspace",
     "blurb": "The cloud's boundaries with your own DSE-to-outlet file read over them \u2014 held in the browser, cleared on sign-out"},
    {"key": "layers",  "path": "/layer-model",    "label": "Layer Model",
     "blurb": "What is loaded, in draw order: base geography, then analysis"},
]

# A nav that offers seven pages, six of which 404, is worse than no nav: it
# reads as a broken application rather than a deliberately narrow one. In
# workspace-only mode the list is the one page that answers.
if config.WORKSPACE_ONLY:
    MENUS = [m for m in MENUS if m["key"] == "workspace"]


@bp.route("/api/territory/hierarchy")
def api_territory_hierarchy():
    """REGION > AREA > SALES AREA / BRANCH > TERRITORY, from ref_kecamatan.

    SENT WHOLE, ONCE
    Three regions, six areas, twenty-five sales areas and eighty-nine
    territories over 825 kecamatan rows -- small enough that the entire tree
    fits in one response. A request per level would put a round trip between
    every click of a filter somebody is using to hunt, which is exactly the
    moment latency is least forgivable.

    THE DSE ARE NOT HERE, AND THAT IS THE POINT
    On the Map Workspace the reps come from the reader's own file, which this
    server never sees. The cloud supplies the shape of the organisation; the
    local file supplies who is in it. A DSE list served from here would be a
    second roster that could disagree with the one on the reader's desk.
    """
    con = db.connect()
    try:
        cols = _cols(con, "ref_kecamatan")
        if not cols:
            return jsonify({"ok": False,
                            "error": "No ref_kecamatan in this database."}), 503
        mc = "mc36" if "mc36" in cols else "mc"
        br = ("mc36_branch" if "mc36_branch" in cols
              else ("sa" if "sa" in cols else "branch"))
        ar = "mc36_area" if "mc36_area" in cols else "area"
        rg = "region" if "region" in cols else "NULL"
        tree = {}
        total = 0
        for r in con.execute(
                f"SELECT {rg} rg, {ar} ar, {br} br, {mc} mc, "
                "kecamatan kec, kabkot kab FROM ref_kecamatan"):
            kec = (r["kec"] or "").strip()
            if not kec:
                continue
            node = (tree
                    .setdefault((r["rg"] or "—").strip(), {})
                    .setdefault((r["ar"] or "—").strip(), {})
                    .setdefault((r["br"] or "—").strip(), {})
                    .setdefault((r["mc"] or "—").strip(), []))
            node.append({"kecamatan": kec,
                         "kabupaten": (r["kab"] or "").strip()})
            total += 1
    finally:
        con.close()

    def shape(d):
        out = []
        for name in sorted(d):
            kids = d[name]
            if isinstance(kids, list):
                out.append({"name": name, "kecamatan": sorted(
                    kids, key=lambda k: k["kecamatan"]), "n": len(kids)})
            else:
                sub = shape(kids)
                out.append({"name": name, "children": sub,
                            "n": sum(c["n"] for c in sub)})
        return out

    regions = shape(tree)
    return jsonify({"ok": True, "kecamatan": total,
                    "levels": ["Region", "Area", "Sales area / branch",
                               "Territory"],
                    "regions": regions})


@bp.route("/api/catalog")
def api_catalog():
    """Everything selectable, for the source in view. The category list is
    counted from the selected table, so an option that returns nothing
    cannot appear in the dropdown."""
    source = request.args.get("source", catalog.DEFAULT_SOURCE)
    if source not in catalog.SOURCES:
        return jsonify({"ok": False, "error": f"unknown source {source!r}"}), 400
    con = db.connect()
    try:
        f = catalog.facets(con, source,
                           in_territory=request.args.get("all") != "1")
    finally:
        con.close()
    return jsonify({**f, "sources": catalog.SOURCES,
                    "buckets": [{"key": b["key"], "label": b["label"]}
                                for b in catalog.BUCKETS]})


def _filter_where(args, cols):
    """Shared filter clause for the map, the counters and the detail popup —
    one function so the three can never disagree about what is on screen."""
    where, params = [], []
    for arg, col in (("category", "category"), ("brand", "brand_resolved"),
                     ("kota", "adm_kota"), ("mc", "mc_ioh")):
        v = (args.get(arg) or "").strip()
        if v and col in cols:
            where.append(f"{col} = ?")
            params.append(v)
    name = (args.get("name") or "").strip()
    if name:
        where.append("lower(name) LIKE ?")
        params.append(f"%{name.lower()}%")
    return where, params


@bp.route("/api/insights")
def api_insights():
    """Headline counts under the map, for whatever is currently filtered."""
    source = request.args.get("source", catalog.DEFAULT_SOURCE)
    if source not in catalog.SOURCES:
        return jsonify({"ok": False, "error": "unknown source"}), 400
    table = catalog.table_for(source)
    con = db.connect()
    try:
        cols = catalog._cols(con, table)
        if not cols:
            return jsonify({"ok": False,
                            "error": f"{table} is empty or not built yet"}), 503
        where, params = _filter_where(request.args, cols)
        extra = " AND ".join(where) if where else None
        if params:
            # counts() takes literal SQL, so inline the values it needs.
            extra = None
            for arg, col in (("category", "category"), ("brand", "brand_resolved"),
                             ("kota", "adm_kota"), ("mc", "mc_ioh")):
                v = (request.args.get(arg) or "").strip()
                if v and col in cols:
                    lit = v.replace("'", "''")
                    extra = ((extra + " AND ") if extra else "") + f"{col} = '{lit}'"
        rows = catalog.counts(con, source, extra_where=extra)
        total = con.execute(
            f"SELECT count(*) FROM {table} WHERE "
            + (" AND ".join(["adm_kecamatan IS NOT NULL"]
                            + ([extra] if extra else [])))).fetchone()[0]
    finally:
        con.close()
    return jsonify({"ok": True, "source": source, "table": table,
                    "total": total, "buckets": rows})


@bp.route("/api/insight-detail")
def api_insight_detail():
    """The rows behind one counter — what opens when a counter is clicked."""
    source = request.args.get("source", catalog.DEFAULT_SOURCE)
    key = request.args.get("bucket", "")
    bucket = catalog.BUCKET_BY_KEY.get(key)
    if not bucket:
        return jsonify({"ok": False, "error": f"unknown counter {key!r}"}), 400
    table = catalog.table_for(source)
    sql = catalog.bucket_sql(bucket, source)
    if not sql:
        return jsonify({"ok": False,
                        "error": f"{bucket['label']} is not expressible in "
                                 f"{source} — that is different from zero"}), 200
    con = db.connect()
    try:
        cols = catalog._cols(con, table)
        where, params = _filter_where(request.args, cols)
        clause = " AND ".join(["adm_kecamatan IS NOT NULL", sql] + where)
        pick = [c for c in ("name", "brand_resolved", "category", "adm_kecamatan",
                            "adm_kota", "mc_ioh", "lat", "lon", "source",
                            "address") if c in cols]
        rows = [dict(r) for r in con.execute(
            f"SELECT {', '.join(pick)} FROM {table} WHERE {clause} "
            f"ORDER BY coalesce(brand_resolved, name) LIMIT 500", params)]
        n = con.execute(
            f"SELECT count(*) FROM {table} WHERE {clause}", params).fetchone()[0]
    finally:
        con.close()
    return jsonify({"ok": True, "label": bucket["label"], "count": n,
                    "shown": len(rows), "rows": rows})


# ── uploaded layers ──────────────────────────────────────────────────────
@bp.route("/api/layers/all")
def api_layers_all():
    # Distribution asks here; the Data Files page passes domain=all because
    # managing the files means seeing all of them.
    domain = request.args.get("domain", "distribution")
    con = db.connect()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT layer_key, label, kind, source_file, feature_count, "
            "attr_fields, imported_utc FROM geo_layer ORDER BY imported_utc DESC")
            if fstore.in_domain(domain, r["layer_key"], r["source_file"])]
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()
    for r in rows:
        r["style"] = config.BOUNDARY_STYLE.get(r["layer_key"])
        r["builtin"] = r["layer_key"] in config.BOUNDARY_STYLE
    return jsonify({"ok": True, "layers": rows})


@bp.route("/api/upload-layer", methods=["POST"])
def api_upload_layer():
    """Ingest a KML or KMZ the user drops on the Distribution page.

    Points and polygons both land in geo_feature, so an outlet export and a
    boundary export are the same kind of thing to everything downstream.
    """
    import os
    import tempfile
    import import_local as il

    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "No file was attached."}), 400
    if not f.filename.lower().endswith((".kml", ".kmz")):
        return jsonify({"ok": False,
                        "error": "Only .kml and .kmz files can be uploaded."}), 400

    layer = (request.form.get("layer") or "").strip()
    if not layer:
        layer = re.sub(r"[^a-z0-9]+", "_",
                       os.path.splitext(f.filename)[0].lower()).strip("_")

    tmp = tempfile.NamedTemporaryFile(
        delete=False, suffix=os.path.splitext(f.filename)[1])
    try:
        f.save(tmp.name)
        tmp.close()
        con = db.connect()
        try:
            n = il.import_kml(con, tmp.name, override_key=layer, follow=False)
        finally:
            con.close()
    except Exception as exc:                                  # noqa: BLE001
        return jsonify({"ok": False,
                        "error": f"{type(exc).__name__}: {exc}"}), 400
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    if not n:
        return jsonify({"ok": False,
                        "error": "No placemarks with geometry were found. If "
                                 "this is a Google My Maps export it may be a "
                                 "NetworkLink stub — use Download KML with "
                                 "'Export as KML' ticked."}), 400
    _poly_cache.pop(layer, None)
    return jsonify({"ok": True, "layer_key": layer, "features": n})


@bp.route("/api/local-layers")
def api_local_layers():
    """What is sitting in the local layer folder, and whether it is loaded.

    The recurring files -- microclusters, desa, kecamatan, the DSE-to-outlet
    export, sites -- change on a quarterly cadence, not per session. Reading
    the folder means they are one click away instead of a drag from
    somewhere in Finder, and the page can say which generation is loaded."""
    import os
    con = db.connect()
    try:
        have = {r["source_file"]: dict(r) for r in con.execute(
            "SELECT layer_key,label,source_file,feature_count,imported_utc "
            "FROM geo_layer")}
    finally:
        con.close()
    out = []
    d = config.LAYER_DIR
    try:
        names = sorted(os.listdir(d))
    except OSError as exc:
        return jsonify({"ok": False, "dir": d, "error": str(exc), "files": []})
    for n in names:
        if not n.lower().endswith((".kml", ".kmz")) or n.startswith("~$"):
            continue
        role, label, colour = config.layer_role(n)
        st = os.stat(os.path.join(d, n))
        loaded = have.get(n)
        out.append({"file": n, "role": role, "label": label, "colour": colour,
                    "bytes": st.st_size,
                    "loaded": bool(loaded),
                    "layer_key": (loaded or {}).get("layer_key"),
                    "features": (loaded or {}).get("feature_count"),
                    "imported_utc": (loaded or {}).get("imported_utc")})
    return jsonify({"ok": True, "dir": d, "files": out})


@bp.route("/api/local-layers/import", methods=["POST"])
def api_local_layer_import():
    """Import one file from the local layer folder by name."""
    import os
    import import_local as il

    name = (request.form.get("file") or request.args.get("file") or "").strip()
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return jsonify({"ok": False, "error": "bad file name"}), 400
    path = os.path.join(config.LAYER_DIR, name)
    if not os.path.isfile(path):
        return jsonify({"ok": False, "error": f"{name} is not in the layer folder"}), 404
    con = db.connect()
    try:
        n = il.import_kml(con, path, follow=False)
    except Exception as exc:                                      # noqa: BLE001
        con.close()
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
    con.close()
    _poly_cache.clear()
    return jsonify({"ok": True, "file": name, "features": n})


def _gz(path):
    """Send a gzipped GeoJSON file as-is and let the browser inflate it."""
    resp = send_file(path, mimetype="application/geo+json", conditional=False)
    resp.headers["Content-Encoding"] = "gzip"
    resp.headers["Cache-Control"] = "no-cache"
    resp.direct_passthrough = True
    return resp


@bp.route("/api/layer/<layer_key>/points.geojson")
def api_layer_points(layer_key):
    """Uploaded point layers -- outlets, sites -- as GeoJSON.

    Only the fields the map, tooltip and filters use travel with the
    geometry; the full attribute row is one fetch away on
    /api/layer/<key>/feature/<feature_key>. The trimmed collection is
    cached gzipped, so a warm request is a file send."""
    slim = None if request.args.get("full") == "1" else kmllayers.slim_fields(layer_key)
    if slim is not None:
        if mapcache.points_fresh(layer_key):
            return _gz(mapcache.points_path(layer_key))

    con = db.connect()
    feats = []
    try:
        for r in con.execute(
                "SELECT feature_key, name, attrs_json, centroid_lat, "
                "centroid_lon, geometry_geojson FROM geo_feature "
                "WHERE layer_key = ?", (layer_key,)):
            if r["centroid_lat"] is None or r["centroid_lon"] is None:
                continue
            # Point layers only. The type is read off the head of the stored
            # GeoJSON rather than by parsing it -- a desa polygon can be
            # hundreds of kilobytes and parsing 7,700 of them to reject them
            # all is pure waste.
            head = (r["geometry_geojson"] or "")[:60].replace(" ", "")
            if '"type":"Point"' not in head and '"type":"MultiPoint"' not in head:
                continue
            if not config.on_map(r["centroid_lat"], r["centroid_lon"]):
                continue
            props = {"feature_key": r["feature_key"], "name": r["name"]}
            attrs = json.loads(r["attrs_json"] or "{}")
            if slim is None:
                props.update(attrs)
            else:
                props.update({k: v for k, v in attrs.items() if k in slim})
            feats.append({"type": "Feature", "properties": props,
                          "geometry": {"type": "Point",
                                       "coordinates": [r["centroid_lon"],
                                                       r["centroid_lat"]]}})
    finally:
        con.close()
    fc = {"type": "FeatureCollection", "name": layer_key, "features": feats}
    if slim is not None:
        try:
            mapcache.write_points(layer_key, fc)
            return _gz(mapcache.points_path(layer_key))
        except OSError:
            pass
    return geojson(fc)


@bp.route("/api/layer/<layer_key>/feature/<path:feature_key>")
def api_layer_feature(layer_key, feature_key):
    """The full attribute row for one feature -- what a popup shows once the
    user actually clicks, rather than for all 73k points up front."""
    con = db.connect()
    try:
        row = con.execute(
            "SELECT feature_key, name, attrs_json FROM geo_feature "
            "WHERE layer_key = ? AND feature_key = ?",
            (layer_key, feature_key)).fetchone()
    finally:
        con.close()
    if not row:
        return jsonify({"ok": False, "error": "not found"}), 404
    props = {"feature_key": row["feature_key"], "name": row["name"]}
    props.update(json.loads(row["attrs_json"] or "{}"))
    return jsonify({"ok": True, "properties": props})


@bp.route("/api/layer/<layer_key>/summary")
def api_layer_summary(layer_key):
    """Group an uploaded layer by any of its own attribute fields — the
    outlet export carries DSE, microcluster and kecamatan, and which of
    those matters is the user's call, not ours."""
    field = request.args.get("field")
    con = db.connect()
    try:
        rows = list(con.execute(
            "SELECT attrs_json FROM geo_feature WHERE layer_key = ?",
            (layer_key,)))
    finally:
        con.close()
    if not rows:
        return jsonify({"ok": False, "error": "layer not found"}), 404
    attrs = [json.loads(r["attrs_json"] or "{}") for r in rows]
    fields = sorted({k for a in attrs for k in a})
    if not field:
        return jsonify({"ok": True, "fields": fields, "count": len(attrs)})
    tally = {}
    for a in attrs:
        tally[a.get(field) or "(blank)"] = tally.get(a.get(field) or "(blank)", 0) + 1
    return jsonify({"ok": True, "field": field, "fields": fields,
                    "count": len(attrs),
                    "groups": sorted([{"value": k, "count": v}
                                      for k, v in tally.items()],
                                     key=lambda x: -x["count"])})


# ══════════════════════════════════════════════════════════════════════════
# Distribution Polygon only — DSE servicing coverage
# ══════════════════════════════════════════════════════════════════════════
# These endpoints exist for the Distribution page and are not wired into
# Retail Gapura or Future GAPURA. Nothing here is a published boundary: a DSE
# has no boundary file, so the shape is inferred from the outlets the rep
# actually holds, and every response says so in `derived`.

def _dse_groups(con, layer_key, field="DSE_CODE"):
    """{dse: [(lat, lon), ...]} from an uploaded point layer."""
    groups = {}
    for r in con.execute(
            "SELECT attrs_json, centroid_lat, centroid_lon FROM geo_feature "
            "WHERE layer_key = ? AND centroid_lat IS NOT NULL", (layer_key,)):
        a = {k.upper(): v for k, v in
             json.loads(r["attrs_json"] or "{}").items()}
        key = a.get(field.upper())
        if key is None or str(key).strip() == "":
            continue
        groups.setdefault(str(key).strip(), []).append(
            (r["centroid_lat"], r["centroid_lon"]))
    return groups


def force_fit_features(con, points, cell=200.0, max_moves=None, placed=None):
    """FORCE FIT: whole-desa territories rebalanced against the workload norms.

    Returns (features, report). Rendering goes through the same lattice and
    the same seam-aware smoothing as Exclusive, so the shapes tile exactly.

    `placed` defaults to the roll-up's own answer for the application's
    outlets. A sample passes an empty one: its points are not our outlets,
    the roll-up has never seen them, and borrowing our placement for a
    coordinate that merely happens to match would credit a desa to the
    wrong rep."""
    feats, idx, strat_by_join = force_fit.desa_index(con)
    # The same placement the summary table counts off, so a rep's desa here
    # is a desa the table agrees they hold.
    per, unplaced = force_fit.assign_outlets(
        idx, points,
        placed=tr.desa_placement(con) if placed is None else placed)
    if not per:
        return [], {"error": "no outlet fell inside a desa polygon"}
    strat = {f["feature_key"]: strat_by_join.get(f["join_key"], "rural")
             for f in feats}
    adj = force_fit.adjacency(feats, set(per))
    owner, report = force_fit.rebalance(per, strat, adj, max_moves=max_moves)
    report["unplaced"] = unplaced

    lat0 = sum(p[1] for p in points) / len(points)

    outlets = collections.Counter()
    desa_n = collections.Counter()
    for k, c in per.items():
        d = owner.get(k)
        if d:
            outlets[d] += sum(c.values())
            desa_n[d] += 1

    # THE PATCH IS DRAWN FROM THE DESA, NOT FROM A LATTICE OVER THEM
    #
    # This used to rasterise each owner's desa onto the same 600 m lattice
    # the reach-based models use. Force fit hands every desa to exactly one
    # rep, so the territories could never really overlap -- but a cell whose
    # centre falls in one rep's desa still covers ground inside the
    # neighbour's, so the DRAWINGS overlapped along every border. Outlets
    # near a boundary sat under two patches at once, the edges came out
    # serrated, and a desa was never more than about a fifth covered by the
    # shape that owned it.
    #
    # Dissolving the owned desa instead gives exact edges, no overlap by
    # construction, and a desa that is either wholly in a patch or not in it
    # -- which is what a mode called "whole desa" is promising. Where the
    # boundary data is not edge-matched the dissolve returns the desa
    # outlines unchanged, so the fallback is simply the same desa with their
    # internal borders showing.
    by_owner = {}
    for f in feats:
        d = owner.get(f["feature_key"])
        if d and f["polys"]:
            by_owner.setdefault(d, []).append(f)

    out = []
    for i, d in enumerate(sorted(by_owner)):
        group = by_owner[d]
        g = dissolve.geometry([f["polys"] for f in group])
        if not g:
            continue
        rings = geom.rings_of(g)
        out.append({"type": "Feature", "geometry": g, "properties": {
            "dse": d, "colour": dse_coverage.colour_for(i),
            "outlets": outlets[d], "desa": desa_n[d],
            # Every desa here is held whole, so the two are the same number.
            # It is reported anyway: a reader comparing modes should not have
            # to remember which one guarantees it.
            "whole_desa": len(group),
            "parts": len(rings),
            "mode": "forcefit",
            "area_km2": round(siting.polys_area_km2(rings, lat0), 3),
            "derived": True}})
    return out, report


def _existing_polys(con, layer_key, field):
    """The uploaded layer's OWN polygons, coloured by the group field.

    No geometry is invented here — this is the file as supplied, which is the
    point of having it beside the two derived models. A layer of points has
    nothing to draw, and says so rather than rendering an empty map.
    """
    feats, keys, seen = [], {}, 0
    for f in geom.load_polys(con, layer_key):
        if not f["polys"]:
            continue
        seen += 1
        a = {k.upper(): v for k, v in f["attrs"].items()}
        key = str(a.get(field.upper(), "") or "").strip() or "(no value)"
        keys.setdefault(key, len(keys))
        g = ({"type": "Polygon", "coordinates": f["polys"][0]}
             if len(f["polys"]) == 1
             else {"type": "MultiPolygon", "coordinates": f["polys"]})
        feats.append({"type": "Feature", "geometry": g, "properties": {
            "dse": key, "name": f["name"], "feature_key": f["feature_key"],
            "outlets": None, "parts": len(f["polys"]), "mode": "existing",
            "area_km2": None, "derived": False}})
    for f in feats:
        f["properties"]["colour"] = dse_coverage.colour_for(
            keys[f["properties"]["dse"]])
    return feats, len(keys), seen


def _coverage(con, layer_key, field, mode, cell, reach):
    groups = _dse_groups(con, layer_key, field)
    if not groups:
        return None, None, None, None
    feats, skipped, stats = dse_coverage.build(
        groups, mode=mode, cell=cell, reach=reach)
    return groups, feats, skipped, stats


@bp.route("/api/distribution/dse-coverage.geojson")
def dse_coverage_geojson():
    """Inferred servicing coverage, one polygon per DSE, one colour per DSE."""
    layer = request.args.get("layer", "")
    field = request.args.get("field", "DSE_CODE")
    mode = request.args.get("mode", "exclusive")
    if mode not in ("existing", "exclusive", "coverage", "hull", "forcefit"):
        mode = "exclusive"
    try:
        cell = max(50.0, min(1000.0, float(
            request.args.get("cell", dse_coverage.DEFAULT_CELL_M))))
        reach = max(cell, min(3000.0, float(
            request.args.get("reach", dse_coverage.DEFAULT_REACH_M))))
    except ValueError:
        return jsonify({"ok": False, "error": "cell and reach must be numeric"}), 400
    if not layer:
        return jsonify({"ok": False, "error": "layer is required"}), 400

    con = db.connect()
    if mode == "existing":
        try:
            feats, nkeys, seen = _existing_polys(con, layer, field)
        except sqlite3.OperationalError as exc:
            con.close()
            return jsonify({"ok": False, "error": str(exc)}), 503
        con.close()
        if not feats:
            return jsonify({
                "ok": False, "empty": True,
                "error": "This layer carries no polygons of its own — it is "
                         "points only, so there is nothing 'existing' to "
                         "draw. Switch to Exclusive or Coverage to derive a "
                         "territory from the outlet positions."}), 200
        return geojson({
            "type": "FeatureCollection", "name": f"{layer}:existing",
            "ok": True, "mode": mode, "field": field,
            "groups": nkeys, "drawn": len(feats), "skipped": [],
            "points": None, "seam": None, "overlap_pct": None,
            "area_km2": None, "derived": False,
            "features": feats})

    try:
        groups, feats, skipped, stats = _coverage(
            con, layer, field, mode, cell, reach)
    except sqlite3.OperationalError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    finally:
        con.close()
    if groups is None:
        return jsonify({"ok": False, "error":
                        f"No point in '{layer}' carries a {field}."}), 404
    # How many outlets end up outside their own rep's shape? In exclusive
    # mode that is not an error: it means a neighbouring rep's outlets are
    # closer to that ground than the owner's are, which is interleaving in
    # the territory and worth surfacing rather than smoothing over.
    seam = 0
    by_key = {}
    for f in feats:
        by_key.setdefault(f["properties"]["dse"], []).append(f)
    for key, pts in groups.items():
        mine = by_key.get(key, [])
        if not mine:
            continue
        for la, lo in pts:
            if dse_coverage.assign(mine, la, lo) != key:
                seam += 1
    total_pts = sum(len(v) for v in groups.values())
    return geojson({
        "type": "FeatureCollection", "name": f"{layer}:dse_coverage",
        "ok": True, "mode": mode, "field": field,
        "cell_m": cell, "reach_m": reach,
        "groups": len(groups), "drawn": len(feats), "skipped": skipped,
        "points": total_pts, "seam": seam,
        "area_km2": round(sum(f["properties"]["area_km2"] for f in feats), 1),
        "overlap_pct": (stats or {}).get("overlap_pct"),
        "union_km2": (stats or {}).get("union_km2"),
        # Specks folded into the rep surrounding them. Reported, because a
        # map that quietly tidies itself is a map you cannot audit.
        "islands_absorbed": (stats or {}).get("islands_absorbed"),
        "islands_kept": (stats or {}).get("islands_kept"),
        "derived": True,
        "features": feats})


# ── Sales Area summary (Distribution Polygon only) ───────────────────────
# Six columns, and no more: Territory Name, DSE, Site, Outlet, Desa, km².
# The roll-up carries thirty-odd measures and putting them all here would
# make a reference table out of something whose job is to let you see twelve
# sales areas at once and pick one. Everything else is a click away on
# Preview Polygon.
SUMMARY_LEVELS = {
    "branch": ("Sales Area", "branch"),
    "mc": ("Microcluster", "mc"),
    "kecamatan": ("Kecamatan", "kecamatan"),
    "desa": ("Desa / Kelurahan", "desa"),
}


@bp.route("/api/distribution/territory-summary")
def territory_summary():
    """The Sales Area table, and whatever level you drill into from it.

    Counts come from territory_rollup, which places every outlet and every
    mast in a desa polygon once and sums upward -- so this table agrees with
    the map beside it and with the Preview Polygon roll-up, rather than being
    a third opinion.
    """
    level = request.args.get("level", "branch")
    if level not in SUMMARY_LEVELS:
        return jsonify({"ok": False,
                        "error": f"unknown level {level!r}"}), 400
    f = {k: request.args.get(k) for k in ROLLUP_FILTERS if request.args.get(k)}
    con = db.connect()
    try:
        rows, _label, _ = tr.rollup(con, level, **f)
        tot = tr.totals(rows)
    except sqlite3.OperationalError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    finally:
        con.close()

    def slim(r):
        return {
            "key": r.get("key"),
            "dse": r.get("dse") or 0,
            "sites": r.get("sites") or 0,
            "outlets": r.get("outlets") or 0,
            "desa": r.get("n_desa") or 0,
            "area_km2": round(r.get("area_km2") or 0, 1),
            # A territory no export reaches shows n/a, not 0 -- the two mean
            # opposite things and only one of them is a finding.
            "has_dse_data": bool(r.get("has_dse_data")),
            # What the row filters to when you click it. The key is a label
            # -- at kecamatan level it may read "CIPAYUNG (KOTA DEPOK)" to
            # tell two of them apart -- so the filter value travels with it.
            "kabkot": r.get("kabkot"), "kecamatan": r.get("kecamatan"),
            "filter_key": (r.get("kecamatan") if level == "kecamatan"
                           else r.get("key")),
        }

    out = [slim(r) for r in rows]
    out.sort(key=lambda x: -x["outlets"])
    return geojson({
        "ok": True, "level": level, "label": SUMMARY_LEVELS[level][0],
        "filters": f, "rows": out, "totals": slim(tot) if tot else None,
        "next_level": {"branch": "mc", "mc": "kecamatan",
                       "kecamatan": "desa"}.get(level)})


@bp.route("/api/distribution/dse-fields")
def dse_fields():
    """Which attributes of an uploaded layer look like a rep identifier —
    enough distinct values to be a territory, few enough not to be a key."""
    layer = request.args.get("layer", "")
    con = db.connect()
    try:
        rows = [json.loads(r["attrs_json"] or "{}") for r in con.execute(
            "SELECT attrs_json FROM geo_feature WHERE layer_key = ?", (layer,))]
    except sqlite3.OperationalError:
        rows = []
    con.close()
    if not rows:
        return jsonify({"ok": False, "error": "no such layer"}), 404
    n = len(rows)
    out = []
    for key in sorted(rows[0]):
        vals = {str(r.get(key, "")).strip() for r in rows}
        vals.discard("")
        d = len(vals)
        if 2 <= d <= max(3, n // 4):
            out.append({"field": key, "distinct": d,
                        "suggested": key.upper().startswith("DSE")})
    out.sort(key=lambda f: (not f["suggested"], f["distinct"]))
    return jsonify({"ok": True, "features": n, "fields": out})


# ── the three pages ──────────────────────────────────────────────────────
def _page(template, menu_key, title):
    return render_template(
        template, menus=MENUS, active=menu_key, page_title=title,
        operators=operator_meta(), styles=config.BOUNDARY_STYLE,
        sources=catalog.SOURCES, default_source=catalog.DEFAULT_SOURCE,
        aoi=config.AOI, lat=config.DEFAULT_LAT, lon=config.DEFAULT_LON,
        kelurahan_themes=config.KELURAHAN_THEMES)


@bp.route("/retail-gapura")
def page_retail():
    return _page("territory.html", "retail", "Retail Gapura Project")


@bp.route("/distribution")
def page_distribution():
    return _page("distribution.html", "distrib", "Distribution Polygon")


# ══════════════════════════════════════════════════════════════════════════
# Future GAPURA — where the next IM3 / 3ID service point should go
# ══════════════════════════════════════════════════════════════════════════
import siting                                                     # noqa: E402

SITING_ARGS = ("strategy", "g1_mode", "scope", "vlr_min", "vlr_pct_min",
               "hybrid_own", "min_km_hybrid",
               "min_km_im3", "min_km_3id", "ms_pct_min", "ms_pct_max",
               "own_includes_ipp", "top_banks", "radius_urban_km",
               "radius_rural_km", "gates",
               # population term and the local distance term
               "pop_metric", "dist_term", "dist_weight", "dist_zero_km",
               "dist_full_km", "use_cpi")


def _siting_overrides(args):
    """Only keys that were actually sent become overrides; anything absent
    keeps the model default rather than being reset to zero."""
    return {k: args.get(k) for k in SITING_ARGS if args.get(k) not in (None, "")}


@bp.route("/api/siting/rank")
def api_siting_rank():
    con = db.connect()
    try:
        res = siting.rank(con, _siting_overrides(request.args))
    except sqlite3.OperationalError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    finally:
        con.close()
    return jsonify(res)


@bp.route("/api/siting/batches")
def api_siting_batches():
    con = db.connect()
    try:
        return jsonify({"ok": True, "batches": siting.list_batches(con)})
    finally:
        con.close()


@bp.route("/api/siting/upload", methods=["POST"])
def api_siting_upload():
    """Score a spreadsheet of candidate places.

    Expected columns are LOCATIONS, KECAMATAN, LONG and LAT, matched by
    header name in any order. The KECAMATAN column is read but not trusted:
    each site is placed by point-in-polygon and any disagreement with the
    typed value is reported per row instead of being silently accepted."""
    import os
    import tempfile
    import uuid

    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "No file was attached."}), 400
    if not f.filename.lower().endswith((".xlsx", ".xlsm")):
        return jsonify({"ok": False,
                        "error": "Only .xlsx spreadsheets can be scored. "
                                 "Save a .csv or .xls as .xlsx first."}), 400

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    try:
        f.save(tmp.name)
        tmp.close()
        sites = siting.read_sites_xlsx(tmp.name)
    except Exception as exc:                                      # noqa: BLE001
        return jsonify({"ok": False,
                        "error": f"{type(exc).__name__}: {exc}"}), 400
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    if not sites:
        return jsonify({"ok": False,
                        "error": "No rows with a usable latitude and "
                                 "longitude were found in the first sheet."}), 400

    over = _siting_overrides(request.form)
    con = db.connect()
    try:
        res = siting.score_candidates(con, sites, over)
        batch_id = uuid.uuid4().hex[:12]
        label = (request.form.get("label") or "").strip() or f.filename
        siting.save_batch(con, batch_id, label, f.filename, res)
    except Exception as exc:                                      # noqa: BLE001
        con.close()
        return jsonify({"ok": False,
                        "error": f"{type(exc).__name__}: {exc}"}), 400
    con.close()
    return jsonify({**res, "batch_id": batch_id, "label": label})


@bp.route("/api/siting/batch/<batch_id>")
def api_siting_batch(batch_id):
    con = db.connect()
    try:
        out = siting.load_batch(con, batch_id)
    finally:
        con.close()
    if not out:
        return jsonify({"ok": False, "error": "batch not found"}), 404
    return jsonify(out)


@bp.route("/api/siting/batch/<batch_id>/delete", methods=["POST"])
def api_siting_batch_delete(batch_id):
    con = db.connect()
    try:
        siting.delete_batch(con, batch_id)
    finally:
        con.close()
    return jsonify({"ok": True})


@bp.route("/api/siting/batch/<batch_id>/export.csv")
def api_siting_export(batch_id):
    import csv
    import io as _io

    from flask import Response
    con = db.connect()
    try:
        out = siting.load_batch(con, batch_id)
    finally:
        con.close()
    if not out:
        return jsonify({"ok": False, "error": "batch not found"}), 404
    buf = _io.StringIO()
    cols = siting.CAND_COLS + ["gate_fails"]
    w = csv.writer(buf)
    w.writerow(cols)
    for r in out["rows"]:
        w.writerow([("; ".join(r["gate_fails"]) if c == "gate_fails"
                     else r.get(c)) for c in cols])
    return Response(
        buf.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="future-gapura-{batch_id}.csv"'})


@bp.route("/api/siting/export.xlsx")
def api_siting_export_xlsx():
    """The whole analysis as a workbook: the model and its gate audit, the
    ranked kecamatan, the candidate batch if one is named, and a summary.

    The parameters are read from the query string exactly as /api/siting/rank
    reads them, so the download always matches what is on screen rather than
    silently re-running the defaults."""
    import io as _io

    from flask import Response
    import siting_xlsx

    con = db.connect()
    try:
        res = siting.rank(con, _siting_overrides(request.args))
        batch_id = (request.args.get("batch") or "").strip()
        batch = siting.load_batch(con, batch_id) if batch_id else None
    except sqlite3.OperationalError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    finally:
        con.close()

    buf = _io.BytesIO()
    siting_xlsx.build(res, batch).save(buf)
    buf.seek(0)
    name = "future-gapura-{}-{}-{}.xlsx".format(
        res["params"]["strategy"].lower(), res["params"]["scope"],
        db.utcnow()[:10])
    return Response(
        buf.getvalue(),
        mimetype="application/vnd.openxmlformats-officedocument."
                 "spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'})


@bp.route("/future-gapura")
def page_future():
    return _page("future.html", "future", "Future GAPURA")


# ══════════════════════════════════════════════════════════════════════════
# Preview Polygon — the territory summary
# ══════════════════════════════════════════════════════════════════════════

ROLLUP_FILTERS = ("area", "branch", "mc", "kabkot", "kecamatan")


@bp.route("/api/rollup")
def api_rollup():
    """One number set at whichever level is asked for, plus the totals line
    and a statement of how much of the territory the DSE export covers."""
    level = request.args.get("level", "area")
    if level not in tr.LEVELS:
        return jsonify({"ok": False, "error": f"unknown level {level!r}"}), 400
    f = {k: request.args.get(k) for k in ROLLUP_FILTERS if request.args.get(k)}
    con = db.connect()
    try:
        rows, label, _ = tr.rollup(con, level, **f)
        cov = tr.coverage(con)
        # Free at every level: the roll-up itself now counts off the desa
        # placement, so by the time we are here the pass has already run and
        # this is a memo read.
        cov = dict(cov, desa_recon=tr.desa_coverage(con))
    except sqlite3.OperationalError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    finally:
        con.close()
    clean = [{k: v for k, v in r.items() if not k.startswith("_")} for r in rows]
    tot = {k: v for k, v in tr.totals(rows).items() if not k.startswith("_")}
    # Compact, for the same reason the geometry is: the Desa level is 887
    # rows wide now that it carries real counts, and debug-mode pretty
    # printing triples it for nobody's benefit.
    return geojson({"ok": True, "level": level, "label": label,
                    "filters": f, "rows": clean, "totals": tot,
                    "columns": [{"key": k, "label": lab, "fmt": fmt}
                                for k, lab, fmt in tr.COLUMNS],
                    "coverage": cov,
                    "levels": list(tr.LEVELS)})


def _terr_out(tk, payload):
    """Encode once, keep the bytes, serve them. A repeat of the same
    selection then costs a dictionary lookup rather than four seconds."""
    enc = _encode(payload)
    if tk:
        _keep(_TERR_CACHE, tk, enc, _TERR_KEEP)
    return geojson_bytes(enc)


@bp.route("/api/preview/territory.geojson")
def preview_territory():
    """DSE territory polygons for a slice of the hierarchy, with no layer
    selection step.

    The Distribution page draws one uploaded layer at a time because that is
    what you are inspecting there. Here the question starts from the
    territory -- "show me this area" -- so the outlet layers are discovered
    the same way the roll-up discovers them, every layer that carries a
    DSE_CODE and a KECAMATAN, and the filter comes from the hierarchy rather
    than from a checkbox.

    Filtering is by kecamatan -> mc36 -> branch -> area, not by the outlet's
    own Area_Name column: the exports disagree with each other on spelling
    and one of them is a hybrid covering two branches, whereas every outlet
    can be placed by the kecamatan it names.
    """
    f = {k: request.args.get(k) for k in ROLLUP_FILTERS if request.args.get(k)}
    mode = request.args.get("mode", "exclusive")
    if mode not in ("exclusive", "coverage", "hull", "forcefit"):
        mode = "exclusive"
    field = request.args.get("field", "DSE_CODE")
    try:
        cell = max(50.0, min(1000.0, float(request.args.get("cell", 200))))
        reach = max(cell, min(3000.0, float(request.args.get("reach", 400))))
    except ValueError:
        return jsonify({"ok": False, "error": "cell and reach must be numeric"}), 400

    con = db.connect()
    tk = None
    try:
        tk = (mode, field, cell, reach, tuple(sorted(f.items())),
              _bnd_stamp(con))
        hit = _TERR_CACHE.get(tk)
        if hit is not None:
            con.close()
            _TERR_CACHE.move_to_end(tk)
            return geojson_bytes(hit)
    except sqlite3.OperationalError:
        tk = None
    try:
        hier, kab_of = kec_hier(con)
        layers = [r[0] for r in con.execute(
            "SELECT layer_key FROM geo_layer "
            "WHERE upper(attr_fields) LIKE '%DSE_CODE%' "
            "  AND upper(attr_fields) LIKE '%KECAMATAN%'")]
        groups, seen, unplaced, used = {}, 0, 0, set()
        for lk in layers:
            for r in con.execute(
                    "SELECT attrs_json, centroid_lat, centroid_lon "
                    "FROM geo_feature WHERE layer_key=? "
                    "AND centroid_lat IS NOT NULL", (lk,)):
                if not config.on_map(r["centroid_lat"], r["centroid_lon"]):
                    continue
                a = {k.upper(): v for k, v in
                     json.loads(r["attrs_json"] or "{}").items()}
                key = a.get(field.upper())
                if key is None or str(key).strip() == "":
                    continue
                kec = (a.get("KECAMATAN") or "").strip().upper()
                kab = (a.get("KABUPATEN") or a.get("KABKOT") or "").strip().upper()
                h = hier.get((kec, kab)) or hier.get((kec, ""))
                if not h:
                    # An outlet in a kecamatan the territory model does not
                    # cover is still a real outlet on a real street. It is
                    # counted as unplaced and kept, so it draws; a hierarchy
                    # filter below will exclude it, which is correct -- it
                    # belongs to no branch, area or MC.
                    unplaced += 1
                    h = (None, None, None, None)
                mc, branch, area = h[0], h[1], h[2]
                row = {"mc": mc, "branch": branch, "area": area,
                       "kecamatan": kec, "kabkot": kab or kab_of.get(kec, "")}
                if any(v and (row.get(k) or "").strip().upper()
                       != v.strip().upper() for k, v in f.items()):
                    continue
                seen += 1
                used.add(lk)
                groups.setdefault(str(key).strip(), []).append(
                    (r["centroid_lat"], r["centroid_lon"]))
    except sqlite3.OperationalError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    finally:
        con.close()

    if not groups:
        return jsonify({
            "ok": False, "empty": True,
            "error": "No outlets with a " + field + " fall inside this "
                     "selection. Either no export covers it yet, or the "
                     "filter is narrower than the data."}), 200

    if mode == "forcefit":
        pts = [(d, la, lo) for d, lst in groups.items() for la, lo in lst]
        con2 = db.connect()
        try:
            feats, report = force_fit_features(con2, pts, cell=cell)
        finally:
            con2.close()
        if not feats:
            return jsonify({"ok": False, "empty": True,
                            "error": report.get("error", "nothing to fit")}), 200
        return _terr_out(tk, {
            "type": "FeatureCollection", "name": "preview:forcefit",
            "ok": True, "mode": mode, "field": field, "reach": reach, "cell": cell, "filters": f,
            "groups": len(groups), "drawn": len(feats), "skipped": [],
            "points": seen, "seam": 0, "unplaced": report.get("unplaced", 0),
            "layers_used": sorted(used), "layers_available": len(layers),
            "area_km2": round(sum(ft["properties"]["area_km2"]
                                  for ft in feats), 1),
            "overlap_pct": 0.0, "forcefit": report,
            "derived": True, "features": feats})

    feats, skipped, stats = dse_coverage.build(
        groups, mode=mode, cell=cell, reach=reach)
    seam = 0
    by_key = {}
    for ft in feats:
        by_key.setdefault(ft["properties"]["dse"], []).append(ft)
    for key, pts in groups.items():
        mine = by_key.get(key, [])
        if mine:
            seam += sum(1 for la, lo in pts
                        if dse_coverage.assign(mine, la, lo) != key)
    return _terr_out(tk, {
        "type": "FeatureCollection", "name": "preview:territory",
        "ok": True, "mode": mode, "field": field, "reach": reach, "cell": cell, "filters": f,
        "groups": len(groups), "drawn": len(feats), "skipped": skipped,
        "points": seen, "seam": seam, "unplaced": unplaced,
        "layers_used": sorted(used), "layers_available": len(layers),
        "area_km2": round(sum(ft["properties"]["area_km2"] for ft in feats), 1),
        "overlap_pct": (stats or {}).get("overlap_pct"),
        "islands_absorbed": (stats or {}).get("islands_absorbed"),
        "islands_kept": (stats or {}).get("islands_kept"),
        "derived": True, "features": feats})


# The outlines are re-read and re-thinned from the database on every
# request, which is five seconds on the wider selections -- and the page
# refetches whenever a filter changes, so the same handful of answers get
# built over and over. The last few are kept, keyed on the request and on
# the import stamp so a re-upload drops them.
_BND_CACHE = collections.OrderedDict()
_BND_KEEP = 12
# The territory model is the expensive one: 73,659 outlets grouped into
# 1,501 DSE polygons is four seconds of work, and the page rebuilds it on
# every mode click, every settled position of the reach slider and every
# change of filter. Most of those are a return to somewhere the reader has
# already been -- back to Exclusive, back to 400 m -- so the last dozen
# answers are kept. Sixteen because the three modes times a handful of
# reaches is about the span of one sitting.
_TERR_CACHE = collections.OrderedDict()
_TERR_KEEP = 16
# The point layer and the microcluster roster are cheaper than the model but
# are asked for on every change of selection, and a reader moves back and
# forth between the same few. Same rule: keyed on the request and on the
# import stamp, so a re-upload drops them.
_PTS_CACHE = collections.OrderedDict()
_PTS_KEEP = 12
_MC_CACHE = collections.OrderedDict()
_MC_KEEP = 24


def _keep(cache, key, value, limit):
    cache[key] = value
    while len(cache) > limit:
        cache.popitem(last=False)
    return value


def _bnd_stamp(con):
    try:
        return "|".join("%s:%s:%s" % tuple(r) for r in con.execute(
            "SELECT layer_key, feature_count, imported_utc FROM geo_layer "
            "ORDER BY layer_key"))
    except sqlite3.OperationalError:
        return "?"


@bp.route("/api/preview/boundaries.geojson")
def preview_boundaries():
    """Administrative outlines behind the territory model, filtered to the
    selection and stripped to what an outline needs.

    WHY NOT REUSE /api/territory/layer/<key>.geojson
    That endpoint answers a different question. On the Distribution page a
    boundary layer IS the subject -- you tick it to inspect it -- so it ships
    every polygon in the region with a metrics lookup per feature. Here a
    boundary is context: something to see the DSE polygons against. Sending
    5 MB of desa geometry and 887 SQL round-trips so that two dozen of them
    can be drawn under a branch-level view would make the map slower than the
    thing it is decorating.

    So this one filters by the same hierarchy the rest of the page uses, and
    carries only what a hover needs. Coordinates are rounded to five decimals
    -- about a metre, which is finer than any of these borders is surveyed to
    and roughly halves the payload.

    Multi-select on purpose: desa inside kecamatan inside microcluster is a
    nesting you often want to see all of at once, and forcing one at a time
    would hide exactly the mismatches worth finding.
    """
    want = [k.strip() for k in (request.args.get("layers") or "").split(",")
            if k.strip() in config.BOUNDARY_STYLE]
    if not want:
        return geojson({"ok": True, "type": "FeatureCollection",
                        "features": [], "layers": {}, "filters": {}})
    f = {k: request.args.get(k) for k in ROLLUP_FILTERS if request.args.get(k)}
    # Region is not one of the roll-up levels, but it is the only handle on
    # the national polygons -- "show me North Sumatera" has no area or branch
    # a Jakarta reader would know. Accepted here, and here only.
    reg_f = (request.args.get("region") or "").strip().upper()

    con = db.connect()
    ck = None
    try:
        ck = (",".join(sorted(want)), reg_f,
              tuple(sorted(f.items())), _bnd_stamp(con))
        hit = _BND_CACHE.get(ck)
        if hit is not None:
            con.close()
            _BND_CACHE.move_to_end(ck)
            return geojson_bytes(hit)
    except sqlite3.OperationalError:
        ck = None
    try:
        # (kecamatan, kabupaten) -> where it sits in the hierarchy, so the
        # filter can be applied to a polygon that only knows its own name.
        hier, kab_of = kec_hier(con)
        allow, allow_mc = set(), set()
        for (k, kb), h in hier.items():
            if not kb:
                continue
            mc, branch, area = h[0], h[1], h[2]
            if f.get("area") and (area or "").strip().upper() \
                    != f["area"].strip().upper():
                continue
            if f.get("branch") and (branch or "").strip().upper() \
                    != f["branch"].strip().upper():
                continue
            if f.get("mc") and (mc or "").strip().upper() \
                    != f["mc"].strip().upper():
                continue
            if f.get("kabkot") and kb != f["kabkot"].strip().upper():
                continue
            if f.get("kecamatan") and k != f["kecamatan"].strip().upper():
                continue
            allow.add((k, kb))
            if mc:
                allow_mc.add(mc.strip().upper())

        # A polygon whose kecamatan is not in the territory model -- a desa
        # outside the three regions, or one whose kabupaten is spelled
        # differently in the two sources -- is still drawn, as long as the
        # selection is not narrowed to a part of the hierarchy it cannot be
        # in. Dropping it silently is what made whole provinces invisible.
        hier_filter = any(f.get(k) for k in ("area", "branch", "mc"))

        def allowed(kec, kab):
            if (kec, kab) in allow or (kec, "") in allow:
                return True
            if hier_filter or (kec, kab) in hier or (kec, "") in hier:
                return False
            if f.get("kabkot") and kab != f["kabkot"].strip().upper():
                return False
            if f.get("kecamatan") and kec != f["kecamatan"].strip().upper():
                return False
            return True

        # One query, not one per polygon.
        kel = {}
        if "kelurahan" in want:
            mcc = kel_mc_col(con)
            for r in con.execute("SELECT join_key, population, geo_type, "
                                 + (mcc or "NULL") + " mc FROM ref_kelurahan"):
                kel[r["join_key"]] = (r["population"], r["geo_type"], r["mc"])

        # No filter means "show me my patch"; a filter means the reader has
        # named something and should get it wherever it is.
        outside_view = 0
        # HOW FINE THE OUTLINE NEEDS TO BE DEPENDS ON HOW MUCH IS ON SCREEN
        # A desa border carries survey-grade vertices. Drawn across the whole
        # region that is 4 MB to render outlines a third of a pixel wide --
        # detail no screen can show and no reader asked for. So the tolerance
        # is derived from the span of the selection: filtered to a branch it
        # comes out near zero and nothing is thinned, and at region level it
        # reaches the cap of about 44 m, which is still finer than a pixel at
        # that zoom. It is capped rather than scaled without limit so that
        # zooming in after the fetch never reveals a border made of a
        # straight line.
        span = 0.0
        if want:
            box = None
            for ft in load_polys(con, "kecamatan"):
                a = {k.upper(): v for k, v in (ft["attrs"] or {}).items()}
                if not allowed(aval(a, "kecamatan"), aval(a, "kabkot")):
                    continue
                if not on_map(ft["bbox"]):
                    continue
                x0, y0, x1, y1 = ft["bbox"]
                box = ((min(box[0], x0), min(box[1], y0),
                        max(box[2], x1), max(box[3], y1)) if box
                       else (x0, y0, x1, y1))
            if box:
                span = max(box[2] - box[0], box[3] - box[1])
        tol = min(0.0004, span / 2800.0)      # ~half a pixel on a 1400px map

        feats, counts = [], {}
        kept = dropped = 0
        for key in want:
            n = 0
            for ft in load_polys(con, key):
                a = {k.upper(): v for k, v in (ft["attrs"] or {}).items()}
                if not on_map(ft["bbox"]):
                    outside_view += 1
                    continue
                if key == "indosat_mc":
                    mc = aval(a, "mc") or (ft["name"] or "").strip().upper()
                    # The MC polygon carries its own MC, branch, area and
                    # region, so it is matched on those directly. Going only
                    # through ref_kecamatan meant a filter naming something
                    # outside the three regions found nothing there, fell
                    # through, and returned all 7,176 polygons.
                    own = {"mc": mc,
                           "branch": (a.get("BRANCH") or "").strip().upper(),
                           "area": (a.get("AREA") or "").strip().upper()}
                    named = [k for k in ("mc", "branch", "area") if f.get(k)]
                    if any(own[k] != f[k].strip().upper() for k in named):
                        continue
                    if reg_f and aval(a, "region") != reg_f:
                        continue
                    if not named and f and allow_mc and mc not in allow_mc:
                        continue
                    props = {"layer": key, "name": ft["name"], "mc": mc,
                             "region": aval(a, "region") or None,
                             "in_territory": a.get("_OUTSIDE") != "1"}
                else:
                    kec, kab = aval(a, "kecamatan"), aval(a, "kabkot")
                    if not allowed(kec, kab):
                        continue
                    inside = (a.get("_OUTSIDE") != "1"
                              and ((kec, kab) in hier or (kec, "") in hier))
                    props = {"layer": key, "name": ft["name"],
                             "kecamatan": kec or None, "kabkot": kab or None,
                             "in_territory": inside}
                    if key == "kelurahan":
                        pop, geo, mc = kel.get(ft["join_key"], (None, None, None))
                        props.update(population=pop, geo_type=geo, mc=mc)
                if not ft["polys"]:
                    continue
                polys = []
                for poly in ft["polys"]:
                    rings = []
                    for ring in poly:
                        r = [[round(x, 5), round(y, 5)] for x, y in ring]
                        dropped += len(r)
                        r = _thin(r, tol)
                        kept += len(r)
                        rings.append(r)
                    polys.append(rings)
                geo_ = ({"type": "Polygon", "coordinates": polys[0]}
                        if len(polys) == 1 else
                        {"type": "MultiPolygon", "coordinates": polys})
                feats.append({"type": "Feature", "geometry": geo_,
                              "properties": props})
                n += 1
            st = config.BOUNDARY_STYLE[key]
            counts[key] = {"label": st.get("label", key), "colour": st.get("color"),
                           "weight": st.get("weight", 1), "dash": st.get("dash"),
                           "fill": st.get("fill", 0), "count": n}
    except sqlite3.OperationalError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    finally:
        con.close()

    payload = {"ok": True, "type": "FeatureCollection",
               "layers": counts, "filters": f, "features": feats,
               "home": config.HOME_BOUNDS, "home_name": config.HOME_NAME,
               "outside_view": outside_view,
               # Stated so a coarse outline is never a silent surprise.
               "simplify_m": round(tol * 111000),
               "vertices": kept, "vertices_raw": dropped}
    enc = _encode(payload)
    if ck:
        _keep(_BND_CACHE, ck, enc, _BND_KEEP)
    return geojson_bytes(enc)


def _thin(ring, tol):
    """Drop vertices a reader could not tell apart, keeping the ends.

    Distance-based rather than Douglas-Peucker: one pass, no recursion, and
    on a border made of survey points the two are hard to tell apart at the
    tolerances used here. A ring that would collapse below a triangle is
    returned untouched -- a simplification that destroys the shape is not a
    simplification.
    """
    if len(ring) < 5 or tol <= 0:
        return ring
    out = [ring[0]]
    lx, ly = ring[0]
    for x, y in ring[1:-1]:
        if abs(x - lx) >= tol or abs(y - ly) >= tol:
            out.append([x, y])
            lx, ly = x, y
    out.append(ring[-1])
    return out if len(out) >= 4 else ring


@bp.route("/api/preview/feature.geojson")
def preview_feature():
    """One named boundary polygon, so the map can go and look at it.

    The Desa level lists 887 rows and, until now, clicking one did nothing --
    the drilldown stops there because there is nothing below a desa. But
    "which of these is worst" is only half a question; the other half is
    "and where is it". A row that names a place you cannot then see is a
    dead end in the middle of the task.

    Deliberately not filtered by the hierarchy: the caller already has the
    row in front of it and is asking for that exact polygon by key.
    """
    layer = request.args.get("layer", "kelurahan")
    key = (request.args.get("key") or "").strip()
    if layer not in config.BOUNDARY_STYLE or not key:
        return jsonify({"ok": False, "error": "layer and key are required"}), 400
    con = db.connect()
    try:
        want = key.upper()
        for ft in load_polys(con, layer):
            if (ft["join_key"] or "").upper() != want \
                    and (ft["feature_key"] or "").upper() != want:
                continue
            if not ft["polys"]:
                break
            x0, y0, x1, y1 = ft["bbox"]
            a = {k.upper(): v for k, v in (ft["attrs"] or {}).items()}
            g = ({"type": "Polygon", "coordinates": ft["polys"][0]}
                 if len(ft["polys"]) == 1
                 else {"type": "MultiPolygon", "coordinates": ft["polys"]})
            return geojson({
                "ok": True, "type": "Feature", "geometry": g,
                # Leaflet wants [[south, west], [north, east]].
                "bounds": [[y0, x0], [y1, x1]],
                "properties": {"layer": layer, "name": ft["name"],
                               "kecamatan": a.get("KEC"),
                               "kabkot": a.get("KABKOT")}})
    except sqlite3.OperationalError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    finally:
        con.close()
    return jsonify({"ok": False, "error":
                    "no %s polygon named %r" % (layer, key)}), 404


@bp.route("/api/preview/mc.json")
def preview_mc():
    """Everyone working one microcluster, and the shape of their patch.

    The map answers "where is the territory"; this answers "whose is it".
    Clicking a microcluster on the Preview Polygon map opens this, and the
    two are built from the same kecamatan-to-MC mapping, so the roster can
    never list a rep the polygons do not draw.

    Sites are counted per desa from the roll-up's placement -- the same
    placement the summary table reports -- rather than re-derived here,
    because two numbers for "sites in this patch" is one too many.
    """
    mc = (request.args.get("mc") or "").strip()
    if not mc:
        return jsonify({"ok": False, "error": "no microcluster named"}), 400
    con = db.connect()
    mk = None
    try:
        mk = (mc.upper(), _bnd_stamp(con))
        hit = _MC_CACHE.get(mk)
        if hit is not None:
            con.close()
            _MC_CACHE.move_to_end(mk)
            _MC_CACHE.move_to_end(mk)
            return jsonify(hit)
    except sqlite3.OperationalError:
        mk = None
    try:
        per_desa = {}
        try:
            per, _recon = tr.desa_points(con)
            # (desa, kecamatan), never desa alone: the name repeats across
            # the region and a bare-name join would pool every MEKARSARI in
            # West Java into whichever rep happened to serve one of them.
            names = {}
            for r in con.execute("SELECT feature_key, name, attrs_json FROM "
                                 "geo_feature WHERE layer_key = 'kelurahan'"):
                a = json.loads(r["attrs_json"] or "{}")
                names[r["feature_key"]] = (
                    (r["name"] or "").strip().upper(),
                    (a.get("KEC") or a.get("KECAMATAN") or "").strip().upper())
            for k, v in per.items():
                nm = names.get(k)
                if nm and nm[0]:
                    per_desa[nm] = per_desa.get(nm, 0) + (v.get("sites") or 0)
        except sqlite3.OperationalError:
            per_desa = {}
        d = mcprofile.profile(con, mc, sites_per_desa=per_desa or None)
        if not d:
            return jsonify({"ok": False,
                            "error": f"no outlets resolve to '{mc}'"}), 404
        d["bounds"] = mcprofile.mc_bounds(con, d["mc"])
    except sqlite3.OperationalError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    finally:
        con.close()
    d["ok"] = True
    if mk:
        _keep(_MC_CACHE, mk, d, _MC_KEEP)
    return jsonify(d)


@bp.route("/api/preview/points.geojson")
def preview_points():
    """Outlets, sites and third-party POI for the slice being previewed.

    `kinds` selects which of the three to return, so each has its own toggle
    rather than one all-or-nothing switch.

    ON THE CAP, WHICH USED TO LIE
    -----------------------------
    This endpoint read layer by layer and stopped at the limit. The limit fell
    inside the sixth of ten exports, so four whole branches -- both Karawangs,
    South and West Jakarta -- returned no points at all, and the map showed a
    region with outlets in the west and none in the east. That is not a busy
    map, it is a wrong one.

    Everything is collected first now, and only then thinned, by taking every
    Nth row across the whole set. A thinned map is still evenly spread, and
    the response says `sampled` so the UI can say so too.
    """
    f = {k: request.args.get(k) for k in ROLLUP_FILTERS if request.args.get(k)}
    kinds = set((request.args.get("kinds")
                 or "outlet,site,poi").replace(" ", "").split(","))
    cap = max(500, min(40000, int(request.args.get("limit", 15000))))

    con = db.connect()
    pk = None
    try:
        pk = (tuple(sorted(kinds)), cap, tuple(sorted(f.items())),
              request.args.get("poiclass") or "", _bnd_stamp(con))
        hit = _PTS_CACHE.get(pk)
        if hit is not None:
            con.close()
            _PTS_CACHE.move_to_end(pk)
            return geojson_bytes(hit)
    except sqlite3.OperationalError:
        pk = None
    feats = []
    counts = collections.Counter()
    try:
        hier, kab_of = kec_hier(con)

        def placed(kec, kab):
            """Does the territory model cover this kecamatan at all?"""
            return (kec, kab) in hier or (kec, "") in hier

        def here(lat, lon):
            """On the map at all. A point outside the three regions is a
            broken coordinate, not a place, and is left off."""
            return config.on_map(lat, lon)

        def keep(kec, kab):
            h = hier.get((kec, kab)) or hier.get((kec, "")) \
                or (None, None, None, None)
            # The POI catalogue records a kecamatan and no kabupaten. Comparing
            # a kabkot filter against that blank rejected every POI in the
            # selection, so the kabupaten is resolved from the kecamatan when
            # the row does not carry one.
            row = {"mc": h[0], "branch": h[1], "area": h[2],
                   "kecamatan": kec, "kabkot": kab or kab_of.get(kec, "")}
            return not any(v and (row.get(k) or "").strip().upper()
                           != v.strip().upper() for k, v in f.items())

        if "outlet" in kinds:
            layers = [r[0] for r in con.execute(
                "SELECT layer_key FROM geo_layer "
                "WHERE upper(attr_fields) LIKE '%DSE_CODE%' "
                "  AND upper(attr_fields) LIKE '%KECAMATAN%'")]
            for lk in layers:
                for r in con.execute(
                        "SELECT attrs_json, centroid_lat, centroid_lon FROM "
                        "geo_feature WHERE layer_key=? AND centroid_lat IS NOT NULL",
                        (lk,)):
                    a = {k.upper(): v for k, v in
                         json.loads(r["attrs_json"] or "{}").items()}
                    if not here(r["centroid_lat"], r["centroid_lon"]):
                        continue
                    kec = (a.get("KECAMATAN") or "").strip().upper()
                    kab = (a.get("KABUPATEN") or a.get("KABKOT") or "").strip().upper()
                    if not keep(kec, kab):
                        continue
                    feats.append({"type": "Feature", "properties": {
                        "cls": "outlet", "dse": a.get("DSE_CODE"),
                        "name": a.get("OUTLET_NAM") or a.get("OUTLET_NAME"),
                        "sub": a.get("OUTLET_CAT"),
                        "in_territory": placed(kec, kab)},
                        "geometry": {"type": "Point", "coordinates": [
                            r["centroid_lon"], r["centroid_lat"]]}})
                    counts["outlet"] += 1

        if "site" in kinds:
            # Sites arrive two ways and both must count. A site MASTER
            # imported from the spreadsheet lands in ref_site; a site export
            # dropped on the map as a KML lands in geo_feature like any other
            # layer. Reading only ref_site meant 5,698 sites sat in the
            # database, correctly geocoded, and drew nothing.
            kec_idx = None
            slayers = [r[0] for r in con.execute(
                "SELECT layer_key FROM geo_layer WHERE "
                "upper(attr_fields) LIKE '%SITE_ID%' "
                "OR upper(attr_fields) LIKE '%SITE_NAME%'")]
            seen_sites = set()
            for lk in slayers:
                for r in con.execute(
                        "SELECT attrs_json, centroid_lat, centroid_lon FROM "
                        "geo_feature WHERE layer_key=? AND centroid_lat IS NOT NULL",
                        (lk,)):
                    a = {k.upper(): v for k, v in
                         json.loads(r["attrs_json"] or "{}").items()}
                    sid = (a.get("NEW_SITE_ID") or a.get("SITE_ID")
                           or a.get("SITEID") or a.get("SITE_CODE"))
                    if sid:
                        sid = str(sid).strip()
                        # One row per site-and-outlet pair in these exports,
                        # so the same mast repeats. Count and draw it once.
                        if sid in seen_sites:
                            continue
                        seen_sites.add(sid)
                    if not here(r["centroid_lat"], r["centroid_lon"]):
                        continue
                    kec = (a.get("KECAMATAN") or a.get("KEC_UNIK") or "").strip().upper()
                    kab = (a.get("KABUPATEN") or a.get("KABKOT") or "").strip().upper()
                    if "|" in kec:            # "TEBET|JAKARTA SELATAN"
                        kec, kab = [x.strip() for x in kec.split("|", 1)]
                    if f and not kec:
                        # This export carries five columns and none of them is
                        # a kecamatan, so a filter on the hierarchy would drop
                        # every site. They have coordinates, and 5,697 of
                        # 5,698 land inside a kecamatan polygon -- so place
                        # them by geometry rather than declare them unfilterable.
                        if kec_idx is None:
                            kec_idx = geom.build_index(
                                geom.load_polys(con, "kecamatan"))
                        hit = geom.locate_indexed(
                            kec_idx, r["centroid_lat"], r["centroid_lon"])
                        if not hit:
                            continue
                        ha = {k.upper(): v for k, v in hit["attrs"].items()}
                        kec = (hit["name"] or "").strip().upper()
                        kab = (ha.get("KABKOT") or "").strip().upper()
                    if f and not keep(kec, kab):
                        continue
                    feats.append({"type": "Feature", "properties": {
                        "cls": "site",
                        "name": (a.get("NEW_SITE_NAME") or a.get("SITE_NAME")
                                 or sid),
                        "dse": a.get("SE_ID_3ID") or a.get("SE_ID_IM3"),
                        "sub": a.get("SITE_TYPE") or a.get("CATEGORY_APR_26"),
                        "in_territory": placed(kec, kab)},
                        "geometry": {"type": "Point", "coordinates": [
                            r["centroid_lon"], r["centroid_lat"]]}})
                    counts["site"] += 1
            try:
                for r in con.execute(
                        "SELECT site_id, site_name, lat, lon, vlr, "
                        "dse_assigned, kecamatan, kabkot FROM ref_site "
                        "WHERE lat IS NOT NULL AND lon IS NOT NULL"):
                    if str(r["site_id"] or "").strip() in seen_sites:
                        continue
                    if not here(r["lat"], r["lon"]):
                        continue
                    if f and not keep((r["kecamatan"] or "").strip().upper(),
                                      (r["kabkot"] or "").strip().upper()):
                        continue
                    feats.append({"type": "Feature", "properties": {
                        "cls": "site", "name": r["site_name"] or r["site_id"],
                        "dse": r["dse_assigned"],
                        "in_territory": placed(
                            (r["kecamatan"] or "").strip().upper(),
                            (r["kabkot"] or "").strip().upper()),
                        "sub": ("VLR " + format(int(r["vlr"]), ",")
                                if r["vlr"] else None)},
                        "geometry": {"type": "Point",
                                     "coordinates": [r["lon"], r["lat"]]}})
                    counts["site"] += 1
            except sqlite3.OperationalError:
                pass

        if "poi" in kinds:
            want = request.args.get("poiclass") or ""
            try:
                for r in con.execute(
                        "SELECT name, category, brand_resolved, lat, lon, "
                        "adm_kecamatan FROM poi_unified "
                        "WHERE lat IS NOT NULL AND lon IS NOT NULL"):
                    if not here(r["lat"], r["lon"]):
                        continue
                    kec = (r["adm_kecamatan"] or "").strip().upper()
                    # 12% of the catalogue has no kecamatan stamped on it.
                    # That is a reason to exclude it from a kecamatan filter,
                    # not a reason to hide it on an unfiltered map.
                    if f and (not kec or not keep(kec, "")):
                        continue
                    cls = config.poi_class(r["category"])
                    if want and cls != want:
                        continue
                    feats.append({"type": "Feature", "properties": {
                        "cls": cls, "name": r["name"],
                        "sub": r["brand_resolved"] or r["category"],
                        "in_territory": placed(kec, "")},
                        "geometry": {"type": "Point",
                                     "coordinates": [r["lon"], r["lat"]]}})
                    counts[cls] += 1
            except sqlite3.OperationalError:
                pass
    except sqlite3.OperationalError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 503
    finally:
        con.close()

    total = len(feats)
    stride = 1
    if total > cap:
        # Thin evenly across everything collected, never by truncation: the
        # point of the map is where things are, and a prefix is a lie about that.
        stride = (total + cap - 1) // cap
        feats = feats[::stride]
        counts = collections.Counter(ft["properties"]["cls"] for ft in feats)

    legend = [{"cls": k, "label": config.POINT_LABEL[k],
               "colour": config.POINT_COLOUR[k], "count": counts.get(k, 0)}
              for k, _l, _c in config.POINT_CLASSES if counts.get(k)]
    enc = _encode({"type": "FeatureCollection", "name": "preview:points",
                   "ok": True, "total": total, "shown": len(feats),
                   "home": config.HOME_BOUNDS, "home_name": config.HOME_NAME,
                   "sampled": stride > 1, "stride": stride, "limit": cap,
                   "legend": legend, "features": feats})
    if pk:
        _keep(_PTS_CACHE, pk, enc, _PTS_KEEP)
    return geojson_bytes(enc)


@bp.route("/api/rollup/options")
def api_rollup_options():
    """The values available at each level, honouring what is already chosen,
    so the selectors chain instead of offering combinations that are empty."""
    f = {k: request.args.get(k) for k in ROLLUP_FILTERS if request.args.get(k)}
    con = db.connect()
    try:
        allrows, _ = tr.base_rows(con)
        # Administrative options are not narrowed by the Indosat filters and
        # vice versa: the requirement is that the two are alternatives, so
        # each list is built from everything the OTHER family allows.
        adm = tr._filtered(allrows, kabkot=f.get("kabkot"),
                           kecamatan=f.get("kecamatan"))
        ind = tr._filtered(allrows, area=f.get("area"), branch=f.get("branch"),
                           mc=f.get("mc"))
        def vals_of(rs, col):
            return sorted({(r[col] or "").strip() for r in rs if r.get(col)})
        kec_list = sorted({(r["kecamatan"] or "").strip() for r in ind
                           if r.get("kecamatan")})
        out = {
            "area": vals_of(adm, "area"),
            "branch": vals_of(adm, "branch"),
            "mc": vals_of(adm, "mc"),
            "kabkot": vals_of(ind, "kabkot"),
            "kecamatan": kec_list,
        }
    finally:
        con.close()
    return jsonify({"ok": True, "options": out, "selected": f})


@bp.route("/api/rollup/bounds")
def api_rollup_bounds():
    """The bounding box of a named territory, so the map can zoom to it."""
    f = {k: request.args.get(k) for k in ROLLUP_FILTERS if request.args.get(k)}
    con = db.connect()
    try:
        rows, _ = tr.base_rows(con)
        rows = tr._filtered(rows, **f)
        keys = {(r["kecamatan"] or "").strip().upper() for r in rows}
        if not keys:
            return jsonify({"ok": False, "error": "nothing matches"}), 404
        box = None
        for feat in geom.load_polys(con, "kecamatan"):
            a = {k.upper(): v for k, v in (feat["attrs"] or {}).items()}
            if (a.get("KEC") or "").strip().upper() not in keys:
                continue
            x0, y0, x1, y1 = feat["bbox"]
            box = (min(box[0], x0), min(box[1], y0),
                   max(box[2], x1), max(box[3], y1)) if box else (x0, y0, x1, y1)
    finally:
        con.close()
    if not box:
        return jsonify({"ok": False, "error": "no polygons matched"}), 404
    return jsonify({"ok": True, "bounds": [[box[1], box[0]], [box[3], box[2]]],
                    "kecamatan": sorted(keys)})


# ══════════════════════════════════════════════════════════════════════════
# Data Files — the local store
# ══════════════════════════════════════════════════════════════════════════
import filestore as fstore


def _import_file(con, path, role, layer_key=None, label=None):
    """Route a file to the importer that suits it."""
    import import_local as il
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xlsm", ".csv"):
        return il.import_points_table(con, path, override_key=layer_key,
                                      label=label)
    return il.import_kml(con, path, override_key=layer_key, follow=False)


@bp.route("/api/files")
def api_files():
    con = db.connect()
    try:
        files = fstore.listing(con)
    finally:
        con.close()
    return jsonify({"ok": True, "dir": fstore.folder(), "files": files,
                    "roles": [{"key": k, "label": fstore.ROLES[k]["label"],
                               "colour": fstore.ROLES[k]["colour"],
                               "kinds": list(fstore.ROLES[k]["kinds"])}
                              for k in fstore.ORDER],
                    "allowed": list(fstore.ALLOWED)})


@bp.route("/api/files/upload", methods=["POST"])
def api_files_upload():
    """Save an upload into the local data folder, and import it unless the
    caller asked to hold off."""
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "No file was attached."}), 400
    if not f.filename.lower().endswith(fstore.ALLOWED):
        return jsonify({"ok": False, "error":
                        "Handled types are "
                        + ", ".join(fstore.ALLOWED)}), 400
    role = (request.form.get("role") or "").strip() or fstore.guess_role(f.filename)
    if role not in fstore.ROLES:
        role = "other"
    if not f.filename.lower().endswith(tuple(fstore.ROLES[role]["kinds"])):
        return jsonify({"ok": False, "error":
                        f"A {fstore.ROLES[role]['label']} file must be one of "
                        + ", ".join(fstore.ROLES[role]["kinds"])}), 400
    try:
        name, path = fstore.save_upload(f, f.filename)
    except (ValueError, OSError) as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    if request.form.get("import") == "0":
        return jsonify({"ok": True, "file": name, "role": role,
                        "imported": False})
    con = db.connect()
    try:
        n = _import_file(con, path, role,
                         layer_key=(request.form.get("layer_key") or "").strip() or None,
                         label=(request.form.get("label") or "").strip() or None)
    except Exception as exc:                                      # noqa: BLE001
        con.close()
        # The file is kept: a parse failure is usually a wrong-file problem,
        # and deleting the evidence makes it harder to see that.
        return jsonify({"ok": False, "file": name, "saved": True,
                        "error": f"Saved, but could not be imported — "
                                 f"{type(exc).__name__}: {exc}"}), 400
    con.close()
    _poly_cache.clear()
    return jsonify({"ok": True, "file": name, "role": role,
                    "imported": True, "features": n})


@bp.route("/api/files/import", methods=["POST"])
def api_files_import():
    name = (request.form.get("file") or request.args.get("file") or "").strip()
    try:
        path = fstore.safe_path(name)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not os.path.isfile(path):
        return jsonify({"ok": False, "error": f"{name} is not in the data folder"}), 404
    role = (request.form.get("role") or fstore.guess_role(name))
    con = db.connect()
    try:
        n = _import_file(con, path, role,
                         layer_key=(request.form.get("layer_key") or "").strip() or None)
    except Exception as exc:                                      # noqa: BLE001
        con.close()
        return jsonify({"ok": False, "error": f"{type(exc).__name__}: {exc}"}), 400
    con.close()
    _poly_cache.clear()
    return jsonify({"ok": True, "file": name, "features": n})


@bp.route("/api/files/unload", methods=["POST"])
def api_files_unload():
    """Drop an imported layer's rows and leave the file in place."""
    key = (request.form.get("layer_key") or "").strip()
    if not key:
        return jsonify({"ok": False, "error": "layer_key required"}), 400
    con = db.connect()
    try:
        n = fstore.unload(con, key)
    finally:
        con.close()
    _poly_cache.clear()
    return jsonify({"ok": True, "layer_key": key, "removed": n})


@bp.route("/api/files/delete", methods=["POST"])
def api_files_delete():
    """Remove a file. `with_data=1` also drops whatever it imported."""
    name = (request.form.get("file") or "").strip()
    with_data = request.form.get("with_data") == "1"
    con = db.connect()
    dropped = []
    try:
        if with_data:
            for r in con.execute(
                    "SELECT layer_key FROM geo_layer WHERE source_file=?", (name,)):
                dropped.append(r["layer_key"])
            for k in dropped:
                fstore.unload(con, k)
        how, where = fstore.remove(name)
    except FileNotFoundError:
        con.close()
        return jsonify({"ok": False, "error": f"{name} is not there"}), 404
    except (ValueError, OSError) as exc:
        con.close()
        return jsonify({"ok": False, "error": str(exc)}), 400
    finally:
        try:
            con.close()
        except Exception:                                         # noqa: BLE001
            pass
    _poly_cache.clear()
    return jsonify({"ok": True, "file": name, "how": how,
                    "moved_to": where if how == "trashed" else None,
                    "layers_dropped": dropped})


@bp.route("/data-files")
def page_files():
    return render_template(
        "files.html", menus=MENUS, active="files", page_title="Data Files",
        operators=operator_meta(), styles=config.BOUNDARY_STYLE,
        sources=catalog.SOURCES, default_source=catalog.DEFAULT_SOURCE,
        aoi=config.AOI, lat=config.DEFAULT_LAT, lon=config.DEFAULT_LON)


@bp.route("/polygon-samples")
def page_polygon_samples():
    """A working area for reading a proposed demarcation against the
    territory. Deliberately not part of Preview Polygon: that page answers
    "what does our territory look like" and this one answers "does this
    proposal fit", and the two want different defaults -- here all four base
    layers are on from the start because the territory is the thing being
    compared against, not optional context."""
    return render_template(
        "polygon_samples.html", menus=MENUS, active="samples",
        page_title="Polygon Samples", home_name=config.HOME_NAME,
        basemap=config.basemap_cfg(),
        lat=config.DEFAULT_LAT, lon=config.DEFAULT_LON)


@bp.route("/map-workspace")
def page_map_workspace():
    """The demarcation read against the boundaries, with the line between
    cloud and local drawn on the page itself.

    WHY THIS IS NOT A PANEL ON POLYGON SAMPLES
    Polygon Samples asks "does this proposal fit" and answers it by building
    territory models on the server. This page asks a smaller and more
    frequent question -- "what is actually in this desa, and whose is it" --
    and answers it without the server seeing the file at all. Everything
    here is either a cached boundary layer this application already serves,
    or a row that never leaves the browser; there is no endpoint behind this
    page that accepts an outlet, which is what makes the promise on it
    checkable rather than merely stated."""
    return render_template(
        "map_workspace.html", menus=MENUS, active="workspace",
        page_title="Map Workspace", home_name=config.HOME_NAME,
        basemap=config.basemap_cfg(),
        lat=config.DEFAULT_LAT, lon=config.DEFAULT_LON)


@bp.route("/preview-polygon")
def page_preview():
    return render_template(
        "preview.html", menus=MENUS, active="preview",
        page_title="Preview Polygon", levels=list(tr.LEVELS),
        operators=operator_meta(), styles=config.BOUNDARY_STYLE,
        sources=catalog.SOURCES, default_source=catalog.DEFAULT_SOURCE,
        aoi=config.AOI, lat=config.DEFAULT_LAT, lon=config.DEFAULT_LON)
