#!/usr/bin/env python3
"""
Territory roll-up: one number set, at whatever level of the hierarchy you ask.

    python territory_rollup.py                 Inner Jakarta, broken down by area
    python territory_rollup.py --level branch
    python territory_rollup.py --level mc --area "INNER JAKARTA EAST"

THE HIERARCHY
    region -> area -> branch (sales area) -> microcluster -> kecamatan -> desa

    Everything hangs off ref_kecamatan.mc36 / mc36_branch / mc36_area, which
    remap_mc.py derives from the current polygons. Desa figures come from
    ref_kelurahan, business volume from ref_metric, and the DSE / outlet
    counts from the uploaded DSE-to-outlet layer.

ON COVERAGE, WHICH MATTERS MORE THAN THE NUMBERS
    The DSE-to-outlet export currently loaded covers ONE branch and ONE brand
    -- 29 DSEs and 1,765 outlets across 4 of the 36 microclusters. Reporting
    "0 DSE" for the other 32 would read as a finding when it is an absence of
    data, so every row carries `has_dse_data` and the UI is expected to show
    a dash rather than a zero where that is false. A management audience
    cannot tell those apart from the number alone, and the difference is the
    difference between "no coverage" and "not measured".
"""
import argparse
import collections
import gzip
import json
import os
import sys

import config
import db

REGION = "INNER JAKARTA"

# How the most recent outlet pass resolved each row: by the name it gave, by
# the polygon it sits in, or not at all. The UI shows it so a gap between the
# map and the table can never sit quietly again.

# ── which generation of the hierarchy columns this database has ──────────
# remap_mc.py derives mc36 / mc36_branch / mc36_area from the current MC
# polygons and writes them onto the reference tables. Until it has been run
# -- and on a database imported by this app, which names the same columns
# mc / sa / area -- those columns do not exist, and every query here failed
# with "no such column: mc36", which is what left the Preview Polygon page
# blank. The names are resolved once and the SQL is built from them, so the
# roll-up answers either way and uses the derived mapping when it is there.
def _pick(cols, *names):
    for n in names:
        if n in cols:
            return n
    return None


def hier_cols(con):
    kec = {r[1] for r in con.execute("PRAGMA table_info(ref_kecamatan)")}
    kel = {r[1] for r in con.execute("PRAGMA table_info(ref_kelurahan)")}
    return {
        "k_mc": _pick(kec, "mc36", "mc") or "NULL",
        "k_branch": _pick(kec, "mc36_branch", "sa", "branch") or "NULL",
        "k_area": _pick(kec, "mc36_area", "area") or "NULL",
        "k_region": _pick(kec, "region") or "NULL",
        "l_mc": _pick(kel, "mc36", "mc") or "NULL",
        "l_branch": _pick(kel, "branch12", "branch", "sa") or "NULL",
        "l_area": _pick(kel, "area12", "area") or "NULL",
        "l_partner": _pick(kel, "partner12", "partner") or "NULL",
    }


LAST_RECON = {}
LEVELS = ("region", "area", "branch", "mc", "kecamatan", "desa")

# level -> (label, the column on the per-kecamatan base row)
LEVEL_KEY = {
    "region": ("Region", "region"),
    "area": ("Area", "area"),
    "branch": ("Branch / Sales Area", "branch"),
    "mc": ("Microcluster", "mc"),
    "kecamatan": ("Kecamatan", "kecamatan"),
    "desa": ("Desa / Kelurahan", "desa"),
}

# The order the summary table shows, and how each cell is formatted.
COLUMNS = [
    ("n_mc", "MC", "int"),
    ("n_kecamatan", "Kecamatan", "int"),
    ("n_desa", "Desa", "int"),
    ("area_km2", "Area km²", "num1"),
    ("population", "Population", "int"),
    ("pop_density", "Pop / km²", "int"),
    ("geo_mix", "Geo type", "text"),
    ("dse", "DSE", "int_dse"),
    ("outlets", "Outlets", "int_dse"),
    ("sites", "Sites", "int"),
    ("vlr", "VLR subs", "int"),
    ("prepaid_revenue", "Prepaid rev (nett)", "idr"),
    ("rgu_ga", "RGU / GA", "int_dse"),
    ("sell_in", "Sell-in SP", "int_dse"),
    ("ms_ioh", "IOH share", "pct"),
    ("sites_per_dse", "Site / DSE", "num1_dse"),
    ("outlets_per_dse", "Outlet / DSE", "num1_dse"),
    ("desa_per_dse", "Desa / DSE", "num1_dse"),
]


# A site file names its columns differently depending on who exported it.
# Most specific first: LAT_NEW is the mast, LAT is whatever the row is about.
SITE_ID_FIELDS = ("NEW_SITE_ID", "SITE_ID", "SITEID", "SITE_CODE", "SITECODE",
                  "NODE_ID", "CELL_ID", "SITE")
SITE_LAT_FIELDS = ("LAT_NEW", "NEW_LAT", "SITE_LAT", "LATITUDE", "LAT")
SITE_LON_FIELDS = ("LONG_NEW", "LON_NEW", "NEW_LONG", "SITE_LON", "SITE_LONG",
                   "LONGITUDE", "LONG", "LON")


def _pick_field(attr_fields, wanted):
    """The stored spelling of the first wanted column this layer carries."""
    have = {f.strip().upper(): f.strip()
            for f in (attr_fields or "").split(",") if f.strip()}
    for w in wanted:
        if w in have:
            return have[w]
    return None


def _jx(field):
    """json_extract for a column name that may contain anything at all."""
    return "json_extract(attrs_json,'$.\"" + field.replace('"', '') + "\"')"


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _latest(periods):
    if not periods:
        return None
    keys = [k for k in periods if k]
    return periods[max(keys)] if keys else periods.get("")


def _metrics(con):
    """metric -> kec_key -> latest value."""
    out = collections.defaultdict(dict)
    for r in con.execute(
            "SELECT metric, entity_key, coalesce(period,'') p, value_num "
            "FROM ref_metric WHERE entity_type='kecamatan'"):
        out[r["metric"]].setdefault(r["entity_key"], {})[r["p"]] = r["value_num"]
    return {m: {k: _latest(v) for k, v in d.items()} for m, d in out.items()}


def _outlets(con):
    """Per (kecamatan, kabupaten): DSE codes, outlet count, GA, sell-in.

    Read from whichever uploaded layer carries DSE_CODE, so a fresh export
    dropped on the Distribution page is picked up without a code change."""
    # A DSE_CODE alone is not enough: the site export carries one too, and
    # its 200k rows have no Kecamatan, so every one of them was parsed into a
    # dict and thrown away. Ask for the column this function actually reads.
    layers = [r[0] for r in con.execute(
        "SELECT layer_key FROM geo_layer "
        "WHERE upper(attr_fields) LIKE '%DSE_CODE%' "
        "  AND upper(attr_fields) LIKE '%KECAMATAN%'")]
    per = collections.defaultdict(
        lambda: {"dse": set(), "outlets": 0, "rgu_ga": 0.0, "sell_in": 0.0,
                 "cats": collections.Counter()})

    # The exports name a kecamatan and a kabupaten, and for 609 outlets the
    # kabupaten is simply wrong -- Penjaringan, Tanjung Priok and Pademangan
    # filed under Kepulauan Seribu, Tegalwaru under Purwakarta. Keyed on the
    # pair, those outlets matched no kecamatan and vanished from every total.
    # The coordinates are not in dispute, so where the pair fails the outlet
    # is placed in the polygon that contains it and the roll-up reports how
    # many needed it.
    import geom as _geom
    valid = {((r["kecamatan"] or "").strip().upper(),
              (r["kabkot"] or "").strip().upper())
             for r in con.execute(
                 "SELECT kecamatan, kabkot FROM ref_kecamatan")}
    _kidx = [None]
    _kmeta = {}

    def _by_polygon(lat, lon):
        if lat is None or lon is None:
            return None
        if _kidx[0] is None:
            polys = _geom.load_polys(con, "kecamatan")
            _kidx[0] = _geom.build_index(polys)
            for f in polys:
                at = {k.upper(): v for k, v in (f["attrs"] or {}).items()}
                _kmeta[f["feature_key"]] = (
                    (at.get("KEC") or "").strip().upper(),
                    (at.get("KABKOT") or "").strip().upper())
        hit = _geom.locate_indexed(_kidx[0], lat, lon)
        return _kmeta.get(hit["feature_key"]) if hit else None

    recon = {"rows": 0, "by_name": 0, "by_polygon": 0, "unplaced": 0}
    for lk in layers:
        for r in con.execute("SELECT attrs_json, centroid_lat, centroid_lon "
                             "FROM geo_feature WHERE layer_key=?", (lk,)):
            a = {k.upper(): v for k, v in json.loads(r["attrs_json"] or "{}").items()}
            key = ((a.get("KECAMATAN") or "").strip().upper(),
                   (a.get("KABUPATEN") or a.get("KABKOT") or "").strip().upper())
            recon["rows"] += 1
            if key in valid:
                recon["by_name"] += 1
            else:
                alt = _by_polygon(r["centroid_lat"], r["centroid_lon"])
                if alt and alt in valid:
                    key = alt
                    recon["by_polygon"] += 1
                elif key[0]:
                    recon["unplaced"] += 1
                else:
                    recon["unplaced"] += 1
                    continue
            if not key[0]:
                continue
            slot = per[key]
            if a.get("DSE_CODE"):
                slot["dse"].add(str(a["DSE_CODE"]).strip())
            slot["outlets"] += 1
            for src, dst in (("RGU_GA", "rgu_ga"), ("SELL_IN_SP", "sell_in")):
                try:
                    slot[dst] += float(a.get(src) or 0)
                except (TypeError, ValueError):
                    pass
            if a.get("OUTLET_CAT"):
                slot["cats"][a["OUTLET_CAT"]] += 1
    # Reported alongside the numbers rather than mixed into them: `per` is
    # keyed by (kecamatan, kabupaten) and a stray string key there would be
    # read as a territory by everything downstream.
    LAST_RECON.clear()
    LAST_RECON.update(recon)
    return per, layers


def _site_locations(con):
    """One entry per real site: {site key: (lat, lon)}, plus the layers read.

    Split out of _site_points because the desa pass needs exactly the same
    collapsed set of masts, just dropped into a different set of polygons.
    Collapsing twice would be slow; collapsing differently would be worse --
    the map and the two table levels have to be counting the same 5,698
    things or the reconciliation this was written for is a fiction.

    ONE ROW IS NOT ONE SITE
        The site export Indosat produces is a site x outlet join: the same
        NEW_SITE_ID repeats once per outlet it serves, and each of those rows
        carries the OUTLET's Lat/Long, not the mast's. 3,262 real sites
        arrive as 200,787 rows. Counting rows put two hundred thousand masts
        in the report and made every Site/DSE ratio meaningless, so the rows
        are collapsed on the site's own id, at the site's own coordinates,
        before anything is counted or placed.
    """
    import filestore as fs

    layers = []
    for r in con.execute("SELECT layer_key, source_file, attr_fields FROM geo_layer "
                         "WHERE kind='point'"):
        if fs.guess_role(r["source_file"] or r["layer_key"]) == "site":
            layers.append((r["layer_key"], r["attr_fields"] or ""))
    if not layers:
        return {}, [], 0

    seen = {}            # site key -> (lat, lon)
    rows_read = 0
    for lk, fields in layers:
        idf = _pick_field(fields, SITE_ID_FIELDS)
        latf = _pick_field(fields, SITE_LAT_FIELDS)
        lonf = _pick_field(fields, SITE_LON_FIELDS)
        if idf:
            # Let SQLite do the collapsing. It reads the same rows, but in C
            # and without building a dict per row -- 200k rows in under a
            # second instead of the best part of a minute.
            # coalesce, not choose. `attr_fields` is the header of the file
            # the layer came from, and this export advertises LATITUDE and
            # LONGITUDE columns that its placemarks do not actually carry --
            # so json_extract returned NULL for every row, every site was
            # skipped as coordinate-less, and 5,698 masts reported as zero.
            # The placemark's own centroid is always there; fall back to it
            # per row rather than per layer.
            lat_sql = ("coalesce(" + _jx(latf) + ", centroid_lat)"
                       if latf else "centroid_lat")
            lon_sql = ("coalesce(" + _jx(lonf) + ", centroid_lon)"
                       if lonf else "centroid_lon")
            sql = ("SELECT DISTINCT " + _jx(idf) + " sid, "
                   + lat_sql + " lat, " + lon_sql + " lon "
                   "FROM geo_feature WHERE layer_key=?")
            for r in con.execute(sql, (lk,)):
                rows_read += 1
                if r["sid"] in (None, ""):
                    continue
                lat, lon = _f(r["lat"]), _f(r["lon"])
                if lat is None or lon is None:
                    continue
                seen.setdefault(str(r["sid"]).strip().upper(), (lat, lon))
        else:
            # A plain site KML: one placemark really is one site.
            for r in con.execute("SELECT feature_key, centroid_lat, centroid_lon "
                                 "FROM geo_feature WHERE layer_key=?", (lk,)):
                rows_read += 1
                if r["centroid_lat"] is None or r["centroid_lon"] is None:
                    continue
                seen.setdefault(lk + "/" + r["feature_key"],
                                (r["centroid_lat"], r["centroid_lon"]))
    return seen, [lk for lk, _ in layers], rows_read


def _site_points(con):
    """Real site locations, when a site file has been uploaded.

    Until one is, the site column comes from ref_metric.site_id, which is a
    COUNT per kecamatan out of the profile spreadsheet -- fine for a total,
    useless for anything below kecamatan and impossible to put on a map.
    An uploaded site list is strictly better, so it wins, and the caller is
    told which of the two produced the number.

    The collapse to one entry per mast lives in _site_locations; this
    function only drops the result into kecamatan polygons and counts.
    """
    seen, layers, rows_read = _site_locations(con)
    if not layers:
        return None, []

    import geom
    polys = geom.load_polys(con, "kecamatan")
    idx = geom.build_index(polys)
    kmeta = {f["feature_key"]: {k.upper(): v
                                for k, v in (f["attrs"] or {}).items()}
             for f in polys}
    per = collections.Counter()
    placed = outside = 0
    for lat, lon in seen.values():
        hit = geom.locate_indexed(idx, lat, lon)
        if not hit:
            outside += 1
            continue
        a = kmeta.get(hit["feature_key"], {})
        per[((a.get("KEC") or "").strip().upper(),
             (a.get("KABKOT") or "").strip().upper())] += 1
        placed += 1
    return {"per": per, "placed": placed, "outside": outside,
            "sites": len(seen), "rows_read": rows_read,
            "layers": list(layers)}, list(layers)


# One page load asks for the roll-up, the selector options and the map bounds,
# and each of those needs the same base rows. Rebuilding them three times over
# was most of what the browser was waiting for. The stamp is what the data
# looks like from the outside -- which layers exist, how big they are, when
# they were imported -- so an upload, an import or a delete invalidates it
# without anyone having to remember to say so.
_MEMO = {"stamp": None, "rows": None, "layers": None}


def _stamp(con):
    parts = [str(tuple(r)) for r in con.execute(
        "SELECT layer_key, feature_count, imported_utc FROM geo_layer "
        "ORDER BY layer_key")]
    for t in ("ref_kecamatan", "ref_kelurahan", "ref_metric"):
        try:
            parts.append("%s=%d" % (t, con.execute(
                "SELECT count(*) FROM " + t).fetchone()[0]))
        except Exception:
            parts.append(t + "=?")
    return "|".join(parts)


def forget():
    """Drop the memo. Call after anything that changes the underlying data."""
    _MEMO["stamp"] = None


# Sets and Counters do not survive a JSON round trip on their own, and the
# base rows carry both. Tagged rather than guessed at, so a plain dict in
# the data is never mistaken for one on the way back in.
def _enc(o):
    if isinstance(o, set):
        return {"__set__": sorted(o)}
    if isinstance(o, dict):
        # (kecamatan, kabupaten) tuples are used as keys all over this
        # module, and JSON has no such thing -- those dicts go out as a list
        # of pairs and come back keyed on tuples again.
        cnt = isinstance(o, collections.Counter)
        if any(not isinstance(k, str) for k in o):
            return {"__pairs__": [[list(k) if isinstance(k, tuple) else k,
                                   _enc(v)] for k, v in o.items()],
                    "__counter__": cnt}
        enc = {k: _enc(v) for k, v in o.items()}
        return {"__counterdict__": enc} if cnt else enc
    if isinstance(o, (list, tuple)):
        return [_enc(v) for v in o]
    return o


def _dec(o):
    if isinstance(o, dict):
        if "__set__" in o and len(o) == 1:
            return set(o["__set__"])
        if "__counterdict__" in o and len(o) == 1:
            return collections.Counter(
                {k: _dec(v) for k, v in o["__counterdict__"].items()})
        if "__pairs__" in o:
            d = {(tuple(k) if isinstance(k, list) else k): _dec(v)
                 for k, v in o["__pairs__"]}
            return collections.Counter(d) if o.get("__counter__") else d
        return {k: _dec(v) for k, v in o.items()}
    if isinstance(o, list):
        return [_dec(v) for v in o]
    return o


def _rows_cache_path():
    import config
    d = os.path.join(os.path.dirname(config.DB_PATH), "mapcache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "base_rows.json.gz")


def base_rows(con):
    """One row per kecamatan, carrying every level's key and every measure.

    Built from geometry -- every outlet and mast dropped into a polygon --
    which is half a minute of work. The in-process memo below loses that on
    every restart, so it is also written to disk under the import stamp:
    change a layer and it rebuilds, otherwise it is read back in under a
    second. This is what the Preview Polygon page waits for."""
    stamp = _stamp(con)
    if _MEMO["stamp"] == stamp:
        return _MEMO["rows"], _MEMO["layers"]
    p = _rows_cache_path()
    try:
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            d = json.load(fh)
        if d.get("stamp") == stamp:
            _MEMO.update(stamp=stamp, rows=_dec(d["rows"]),
                         layers=d["layers"], sites=_dec(d.get("sites")),
                         geo_orphans=_dec(d.get("geo_orphans")),
                         counts_from=d.get("counts_from"))
            return _MEMO["rows"], _MEMO["layers"]
    except (OSError, ValueError, KeyError):
        pass
    rows, layers = _base_rows(con)
    _MEMO.update(stamp=stamp, rows=rows, layers=layers)
    try:
        with gzip.open(p + ".tmp", "wt", encoding="utf-8", compresslevel=6) as fh:
            json.dump({"stamp": stamp, "rows": _enc(rows), "layers": layers,
                       "sites": _enc(_MEMO.get("sites")),
                       "geo_orphans": _enc(_MEMO.get("geo_orphans")),
                       "counts_from": _MEMO.get("counts_from")},
                      fh, separators=(",", ":"))
        os.replace(p + ".tmp", p)
    except (OSError, TypeError):
        pass
    return rows, layers


def site_info(con):
    """What the last base_rows run learned about the site file, without
    paying for the scan a second time."""
    base_rows(con)
    return _MEMO.get("sites")


def kec_from_desa(con):
    """Kecamatan figures rolled up from the desa placement.

    WHY THE KECAMATAN LEVEL IS NOT ALLOWED ITS OWN OPINION

    It used to have one. The kecamatan row believed the KECAMATAN column in
    the export; the desa rows underneath it believed the polygons. Both were
    defensible and they disagreed: Medan Satria read 205 outlets while the
    four desa inside it added to 185, because twenty shops filed under Medan
    Satria physically stand in Bekasi Utara. Nothing was lost -- the region
    total was right either way -- but a manager who opens a kecamatan and
    adds up its desa is entitled to get the same number back, and did not.

    So there is one placement, done once, against the desa polygons, and
    every level above is a sum of it. The export's own kecamatan column is
    still read -- it is what catches an outlet whose coordinates are wrong --
    but it decides which desa a stray point is offered, never how many
    outlets a kecamatan has.
    """
    per, _recon = desa_points(con)
    if not per:
        return {}, 0
    kof = {}
    for r in con.execute(
            "SELECT join_key, kecamatan, kabkot FROM ref_kelurahan"):
        kof[r["join_key"]] = ((r["kecamatan"] or "").strip().upper(),
                              (r["kabkot"] or "").strip().upper())
    out = collections.defaultdict(
        lambda: {"dse": set(), "outlets": 0, "sites": 0,
                 "rgu_ga": 0.0, "sell_in": 0.0})
    orphan = 0
    for k, v in per.items():
        key = kof.get(k)
        if not key or not key[0]:
            # A desa polygon with no row in the reference sheet. Counted as
            # an orphan and reported rather than folded into a neighbour.
            orphan += v["outlets"]
            continue
        s = out[key]
        s["dse"] |= v["dse"]
        s["outlets"] += v["outlets"]
        s["sites"] += v["sites"]
        s["rgu_ga"] += v["rgu_ga"]
        s["sell_in"] += v["sell_in"]
    return dict(out), orphan


def _base_rows(con):
    m = _metrics(con)
    # Read for its reconciliation, and as the fallback for a database with no
    # desa polygons loaded. Where there are desa polygons, geometry wins.
    outlets, dse_layers = _outlets(con)
    sites, _ = _site_points(con)
    _MEMO["sites"] = sites
    geo, geo_orphans = kec_from_desa(con)
    _MEMO["geo_orphans"] = geo_orphans
    _MEMO["counts_from"] = "desa polygons" if geo else "export kecamatan column"

    desa = collections.defaultdict(
        lambda: {"n": 0, "pop": 0.0, "area_m2": 0.0,
                 "geo": collections.Counter(), "names": []})
    for r in con.execute(
            "SELECT trim(upper(kecamatan)) kec, trim(upper(kabkot)) kab, "
            "kelurahan, coalesce(population,0) pop, coalesce(area_m2,0) am2, "
            "geo_type FROM ref_kelurahan"):
        d = desa[(r["kec"], r["kab"])]
        d["n"] += 1
        d["pop"] += r["pop"] or 0
        d["area_m2"] += r["am2"] or 0
        if r["geo_type"]:
            d["geo"][r["geo_type"]] += 1
        d["names"].append(r["kelurahan"])

    rows = []
    hc = hier_cols(con)
    for r in con.execute(
            "SELECT kec_key, kecamatan, kabkot, %(k_mc)s mc36, "
            "%(k_branch)s mc36_branch, %(k_area)s mc36_area, "
            "%(k_region)s region FROM ref_kecamatan" % hc):
        key = ((r["kecamatan"] or "").strip().upper(),
               (r["kabkot"] or "").strip().upper())
        d = desa.get(key, {"n": 0, "pop": 0.0, "area_m2": 0.0,
                           "geo": collections.Counter(), "names": []})
        named = outlets.get(key)
        if geo:
            # A kecamatan with nothing in it is a zero, not a missing key --
            # every field below reads off `o` unconditionally.
            o = geo.get(key) or {"dse": set(), "outlets": 0, "sites": 0,
                                 "rgu_ga": 0.0, "sell_in": 0.0}
        else:
            o = outlets.get(key)
        rows.append({
            "region": r["region"] or REGION,
            "area": r["mc36_area"], "branch": r["mc36_branch"],
            "mc": r["mc36"], "kecamatan": r["kecamatan"], "kabkot": r["kabkot"],
            "kec_key": r["kec_key"],
            "n_desa": d["n"], "population": d["pop"],
            "area_km2": d["area_m2"] / 1e6, "geo": d["geo"],
            "dse_set": set(o["dse"]) if o else set(),
            "outlets": o["outlets"] if o else 0,
            "rgu_ga": o["rgu_ga"] if o else 0.0,
            "sell_in": o["sell_in"] if o else 0.0,
            # "was this kecamatan in an export at all", which is a question
            # about the file, not about geometry -- so it still asks the file.
            "has_dse_data": bool(named) or bool(o),
            "sites": (o["sites"] if (geo and sites)
                      else (sites["per"].get(key, 0) if sites
                            else (m.get("site_id", {}).get(r["kec_key"]) or 0))),
            "vlr": m.get("vlr_subs", {}).get(r["kec_key"]) or 0,
            "prepaid_revenue": m.get("prepaid_revenue_nett", {}).get(r["kec_key"]) or 0,
            "sso": m.get("sso", {}).get(r["kec_key"]) or 0,
            "ms_ioh": m.get("fbms_ioh", {}).get(r["kec_key"]),
        })
    return rows, dse_layers


def _filtered(rows, area=None, branch=None, mc=None, kabkot=None,
              kecamatan=None):
    def keep(r):
        for val, col in ((area, "area"), (branch, "branch"), (mc, "mc"),
                         (kabkot, "kabkot"), (kecamatan, "kecamatan")):
            if val and (r[col] or "").strip().upper() != val.strip().upper():
                return False
        return True
    return [r for r in rows if keep(r)]


def rollup(con, level="area", **filters):
    rows, dse_layers = base_rows(con)
    rows = _filtered(rows, **filters)
    label, col = LEVEL_KEY[level]

    if level == "desa":
        return _desa_rollup(con, filters), label, dse_layers

    # CIPAYUNG is a kecamatan of Jakarta Timur and a kecamatan of Kota Depok.
    # Grouping the leaf level on the name alone added the two together, so
    # the level reported 120 rows for 121 kecamatan and one of them carried
    # both populations. Below MC the name is only unique within its kabkot.
    ambiguous = set()
    if level == "kecamatan":
        seen_names = collections.Counter()
        for r in rows:
            seen_names[((r["kecamatan"] or "").strip().upper(),
                        (r["kabkot"] or "").strip().upper())] += 0
            seen_names[(r["kecamatan"] or "").strip().upper()] += 0
        byname = collections.Counter()
        for r in rows:
            byname[(r["kecamatan"] or "").strip().upper()] += 1
        ambiguous = {n for n, c in byname.items() if c > 1}

    def group_key(r):
        if level != "kecamatan":
            return r[col] or "(unassigned)"
        name = (r["kecamatan"] or "").strip()
        if name.upper() in ambiguous and r["kabkot"]:
            return name + " (" + r["kabkot"].strip() + ")"
        return name or "(unassigned)"

    groups = collections.OrderedDict()
    for r in rows:
        k = group_key(r)
        g = groups.setdefault(k, {
            "key": k, "n_kecamatan": 0, "n_desa": 0, "population": 0.0,
            "area_km2": 0.0, "geo": collections.Counter(), "dse_set": set(),
            "outlets": 0, "rgu_ga": 0.0, "sell_in": 0.0, "sites": 0.0,
            "vlr": 0.0, "prepaid_revenue": 0.0, "sso": 0.0,
            "mcs": set(), "areas": set(), "branches": set(),
            # Carried so a caller can turn a row back into a filter. The
            # kecamatan level disambiguates its key as "CIPAYUNG (KOTA
            # DEPOK)", which is the right label and the wrong filter value --
            # anything drilling into the row needs the plain name and the
            # kabupaten separately.
            "kecs": set(), "kabs": set(),
            "has_dse_data": False, "ms_num": 0.0, "ms_den": 0.0})
        g["n_kecamatan"] += 1
        if r["kecamatan"]:
            g["kecs"].add(r["kecamatan"].strip())
        if r["kabkot"]:
            g["kabs"].add(r["kabkot"].strip())
        g["n_desa"] += r["n_desa"]
        g["population"] += r["population"]
        g["area_km2"] += r["area_km2"]
        g["geo"].update(r["geo"])
        g["dse_set"] |= r["dse_set"]
        for f in ("outlets", "rgu_ga", "sell_in", "sites", "vlr",
                  "prepaid_revenue", "sso"):
            g[f] += r[f] or 0
        g["has_dse_data"] = g["has_dse_data"] or r["has_dse_data"]
        if r["mc"]:
            g["mcs"].add(r["mc"])
        if r["area"]:
            g["areas"].add(r["area"])
        if r["branch"]:
            g["branches"].add(r["branch"])
        # A share is meaningless summed; weight it by subscribers.
        if r["ms_ioh"] is not None and r["vlr"]:
            g["ms_num"] += r["ms_ioh"] * r["vlr"]
            g["ms_den"] += r["vlr"]

    out = []
    for g in groups.values():
        dse = len(g["dse_set"])
        out.append({
            "key": g["key"], "level": level,
            # Kept so totals can UNION rather than sum: a DSE working two
            # branches is one person, and summing the per-branch counts
            # reported 30 of them where there are 29.
            "_dse_set": g["dse_set"],
            # Same reason as _dse_set one line up. Every kecamatan row counts
            # 1 microcluster, and summing 121 of those said the region had
            # 121 microclusters where it has 36. A microcluster, like a
            # person, is counted once however many rows mention it.
            "_mc_set": set(g["mcs"]),
            "n_mc": len(g["mcs"]), "n_area": len(g["areas"]),
            "n_branch": len(g["branches"]),
            "n_kecamatan": g["n_kecamatan"], "n_desa": g["n_desa"],
            # Only when unambiguous. A branch spans many kabupaten and naming
            # one of them would be worse than naming none.
            "kecamatan": (next(iter(g["kecs"])) if len(g["kecs"]) == 1
                          else None),
            "kabkot": (next(iter(g["kabs"])) if len(g["kabs"]) == 1
                       else None),
            "area_km2": round(g["area_km2"], 2),
            "population": int(g["population"]),
            "pop_density": int(g["population"] / g["area_km2"])
                           if g["area_km2"] else 0,
            "geo_mix": ", ".join(f"{k} {v}" for k, v in g["geo"].most_common()),
            "geo": dict(g["geo"]),
            "dse": dse, "outlets": g["outlets"],
            "rgu_ga": int(g["rgu_ga"]), "sell_in": int(g["sell_in"]),
            "sites": int(g["sites"]), "vlr": int(g["vlr"]),
            "prepaid_revenue": g["prepaid_revenue"], "sso": int(g["sso"]),
            "ms_ioh": (g["ms_num"] / g["ms_den"]) if g["ms_den"] else None,
            "has_dse_data": g["has_dse_data"],
            "sites_per_dse": round(g["sites"] / dse, 1) if dse else None,
            "outlets_per_dse": round(g["outlets"] / dse, 1) if dse else None,
            "desa_per_dse": round(g["n_desa"] / dse, 1) if dse else None,
        })
    out.sort(key=lambda x: -x["vlr"])
    return out, label, dse_layers


# The desa pass is its own memo. It is the only pass that touches all 887
# kelurahan polygons and all 20k points, and the levels above it do not need
# it, so it is built the first time somebody asks for the Desa level and not
# on every page load.
_DESA_MEMO = {"stamp": None, "per": None, "recon": None,
              "at": None}


# ── the placement cache, on disk ─────────────────────────────────────────
# Placing 73,699 outlets and 15,922 masts inside 7,761 desa polygons is a
# thirty-second job, and the in-process memo below loses it the moment the
# server restarts -- so every first visit to Preview Polygon after a restart
# waited half a minute for a number that had not changed. The result is
# written next to the map caches under the same import stamp: it is rebuilt
# when a layer is re-imported and read from disk otherwise.
def _place_cache_path():
    import config
    d = os.path.join(os.path.dirname(config.DB_PATH), "mapcache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "desa_placement.json.gz")


def _place_load(stamp):
    p = _place_cache_path()
    try:
        with gzip.open(p, "rt", encoding="utf-8") as fh:
            d = json.load(fh)
    except (OSError, ValueError):
        return None
    if d.get("stamp") != stamp:
        return None
    per = collections.defaultdict(
        lambda: {"dse": set(), "outlets": 0, "sites": 0,
                 "rgu_ga": 0.0, "sell_in": 0.0})
    for k, v in d["per"].items():
        per[k] = {"dse": set(v["dse"]), "outlets": v["outlets"],
                  "sites": v["sites"], "rgu_ga": v["rgu_ga"],
                  "sell_in": v["sell_in"]}
    at = {(float(a), float(b)): k
          for a, b, k in d.get("at", ())}
    return per, d["recon"], at


def _place_save(stamp, per, recon, at):
    payload = {
        "stamp": stamp, "recon": recon,
        "per": {k: {"dse": sorted(v["dse"]), "outlets": v["outlets"],
                    "sites": v["sites"], "rgu_ga": v["rgu_ga"],
                    "sell_in": v["sell_in"]} for k, v in per.items()},
        "at": [[a, b, k] for (a, b), k in at.items()],
    }
    p = _place_cache_path()
    try:
        with gzip.open(p + ".tmp", "wt", encoding="utf-8", compresslevel=6) as fh:
            json.dump(payload, fh, separators=(",", ":"))
        os.replace(p + ".tmp", p)
    except OSError:
        pass


def desa_points(con):
    """Outlets, sites and DSE placed into desa polygons -- by geometry only.

    WHY NOT BY NAME, THE WAY THE KECAMATAN LEVEL DOES IT

    At kecamatan level the export's own KECAMATAN column is right 14,024
    times out of 14,633 and the polygon is only needed for the 609 it gets
    wrong. One level down that trade reverses: the outlet exports carry no
    kelurahan column at all, and desa names repeat constantly -- there are
    several MEKARSARI and several SUKAMAJU inside this region alone -- so a
    name match would be a guess wearing a number's clothes. The coordinates
    are not in dispute. Every outlet and every mast is placed by asking which
    of the 887 desa polygons contains it, and anything that lands outside all
    of them is reported as outside rather than pushed into the nearest one.

    Returns ({desa join_key: {...}}, recon).
    """
    stamp = _stamp(con)
    if _DESA_MEMO["stamp"] == stamp:
        return _DESA_MEMO["per"], _DESA_MEMO["recon"]
    cached = _place_load(stamp)
    if cached:
        per, recon, at = cached
        _DESA_MEMO.update(stamp=stamp, per=per, recon=recon, at=at)
        return per, recon

    import geom
    import math

    polys = geom.load_polys(con, "kelurahan")
    idx = geom.build_index(polys)
    per = collections.defaultdict(
        lambda: {"dse": set(), "outlets": 0, "sites": 0,
                 "rgu_ga": 0.0, "sell_in": 0.0})
    # Where each outlet ended up, by its own coordinates. The table is not the
    # only thing that needs this answer -- FORCE FIT hands out whole desa and
    # has to hand out the same ones the table is counting, or the model and
    # the summary describe two different territories. Rather than reimplement
    # the rules over there and hope they stay in step, the answer itself is
    # published and the model looks it up.
    at = {}
    recon = {"desa_polygons": len(polys),
             "outlet_rows": 0, "outlets_placed": 0,
             "outlets_snapped": 0, "outlets_outside": 0,
             "site_rows": 0, "sites_placed": 0,
             "sites_snapped": 0, "sites_outside": 0}

    # THE SEAM BETWEEN TWO DIFFERENT EXPORTS
    # The desa borders and the kecamatan borders come from different files
    # and do not agree to the metre. Along a coastline or a river the desa
    # cover has hairline gaps, and a shop standing in one of them lands in a
    # kecamatan but in no desa: 39 outlets and 16 masts region-wide. Left
    # alone they make the Desa rows add up to less than the Kecamatan rows
    # above them, which is exactly the discrepancy this work set out to
    # remove -- and for a reason that is a rounding error in a shapefile,
    # not anything true about the territory.
    #
    # So a point that misses every desa is offered to the nearest desa
    # CENTROID INSIDE THE KECAMATAN THAT DOES CONTAIN IT, and only within
    # 3 km. It cannot cross a kecamatan boundary, it cannot travel far, and
    # it is counted separately as `snapped` so the number is never quietly
    # passed off as a containment.
    kpolys = geom.load_polys(con, "kecamatan")
    kidx = geom.build_index(kpolys)
    # `pa` and `ka`, not `at`: `at` is the placement map this function is
    # building, and these two loops used to overwrite it with a polygon's
    # attribute dict -- so desa_placement() handed FORCE FIT one polygon's
    # attributes with a few coordinate keys grafted on, instead of the
    # placement of every outlet.
    kmeta = {}
    for f in kpolys:
        ka = {k.upper(): v for k, v in (f["attrs"] or {}).items()}
        kmeta[f["feature_key"]] = ((ka.get("KEC") or "").strip().upper(),
                                   (ka.get("KABKOT")
                                    or ka.get("KAB_KOT") or "").strip().upper())
    by_kec = collections.defaultdict(list)
    for f in polys:
        pa = {k.upper(): v for k, v in (f["attrs"] or {}).items()}
        x0, y0, x1, y1 = f["bbox"]
        by_kec[((pa.get("KEC") or "").strip().upper(),
                (pa.get("KABKOT")
                 or pa.get("KAB_KOT") or "").strip().upper())].append(
                    (f["feature_key"], (x0 + x1) / 2.0, (y0 + y1) / 2.0))

    # PENJARINGAN filed under KEPULAUAN SERIBU is the same wrong-kabupaten
    # class the Kecamatan pass already documents. Where a kecamatan name
    # belongs to exactly one kabupaten in this region, the name alone is
    # enough and the bad kabupaten can be ignored; where it does not
    # (CIPAYUNG is both a Jakarta Timur and a Depok kecamatan), it is not
    # offered at all rather than guessed.
    by_name = collections.defaultdict(list)
    _kabs = collections.defaultdict(set)
    for (kec, kab) in by_kec:
        _kabs[kec].add(kab)
    for (kec, kab), lst in by_kec.items():
        if len(_kabs[kec]) == 1:
            by_name[kec] = lst

    SNAP_DEG = 3000.0 / 111000.0       # a seam is metres wide, not kilometres
    NAMED_DEG = 25000.0 / 111000.0     # a named kecamatan is a bigger promise
    all_cent = [c for lst in by_kec.values() for c in lst]

    def _nearest(cands, lat, lon, cap):
        best, bd = None, cap
        for key, cx, cy in cands:
            d = math.hypot(cx - lon, cy - lat)
            if d < bd:
                best, bd = key, d
        return best

    def _snap(lat, lon, named=None):
        """Where a point belongs when no desa polygon contains it.

        Three chances, most trustworthy first, and none of them is allowed to
        invent a location: the point must already be standing in the
        kecamatan, or naming one, before it is offered a desa inside it.
        """
        if lat is None or lon is None:
            return None
        khit = geom.locate_indexed(kidx, lat, lon)
        if khit:
            # No metre cap here on purpose: the point is already standing
            # inside this kecamatan, and every candidate is a desa of that
            # same kecamatan, so containment -- not distance -- is what makes
            # the answer defensible. Three masts on a wide coastal kecamatan
            # sat further than any sensible cap from the nearest desa centroid
            # and were being dropped for it.
            hit = _nearest(by_kec.get(kmeta.get(khit["feature_key"]), ()),
                           lat, lon, NAMED_DEG)
            if hit:
                return hit
        # The export's own kecamatan column. This is the same evidence the
        # Kecamatan level trusts for 14,024 of 14,633 rows, so a shop that
        # counts there must count here too -- otherwise the Desa rows quietly
        # add up to less than the Kecamatan rows above them.
        if named and named in by_kec:
            hit = _nearest(by_kec[named], lat, lon, NAMED_DEG)
            if hit:
                return hit
        if named and named[0] in by_name:
            hit = _nearest(by_name[named[0]], lat, lon, NAMED_DEG)
            if hit:
                return hit
        # Last: the two covers spell a kecamatan differently, so neither of
        # the above could match. Only for a point that named a territory at
        # all, and only across a seam's width. A mast carries no name, so a
        # mast standing outside every kecamatan stays outside here too --
        # which is the same answer the Kecamatan level gives it, and matching
        # answers is the whole point of this pass.
        if not named:
            return None
        return _nearest(all_cent, lat, lon, SNAP_DEG)

    # Same layer test as _outlets: a DSE_CODE alone also matches the site
    # export, whose 200k join rows would be counted as outlets.
    layers = [r[0] for r in con.execute(
        "SELECT layer_key FROM geo_layer "
        "WHERE upper(attr_fields) LIKE '%DSE_CODE%' "
        "  AND upper(attr_fields) LIKE '%KECAMATAN%'")]
    for lk in layers:
        for r in con.execute("SELECT attrs_json, centroid_lat, centroid_lon "
                             "FROM geo_feature WHERE layer_key=?", (lk,)):
            recon["outlet_rows"] += 1
            if not config.on_map(r["centroid_lat"], r["centroid_lon"]):
                recon["outlets_offmap"] = recon.get("outlets_offmap", 0) + 1
                continue
            hit = geom.locate_indexed(idx, r["centroid_lat"], r["centroid_lon"])
            a = {k.upper(): v
                 for k, v in json.loads(r["attrs_json"] or "{}").items()}
            key = hit["feature_key"] if hit else _snap(
                r["centroid_lat"], r["centroid_lon"],
                ((a.get("KECAMATAN") or "").strip().upper(),
                 (a.get("KABUPATEN") or a.get("KABKOT") or "").strip().upper()))
            if not key:
                recon["outlets_outside"] += 1
                continue
            at[(r["centroid_lat"], r["centroid_lon"])] = key
            slot = per[key]
            slot["outlets"] += 1
            if hit:
                recon["outlets_placed"] += 1
            else:
                recon["outlets_snapped"] += 1
            if a.get("DSE_CODE"):
                slot["dse"].add(str(a["DSE_CODE"]).strip())
            for srcf, dst in (("RGU_GA", "rgu_ga"), ("SELL_IN_SP", "sell_in")):
                try:
                    slot[dst] += float(a.get(srcf) or 0)
                except (TypeError, ValueError):
                    pass

    seen, site_layers, _rows = _site_locations(con)
    for lat, lon in seen.values():
        recon["site_rows"] += 1
        if not config.on_map(lat, lon):
            recon["sites_offmap"] = recon.get("sites_offmap", 0) + 1
            continue
        hit = geom.locate_indexed(idx, lat, lon)
        key = hit["feature_key"] if hit else _snap(lat, lon)
        if not key:
            recon["sites_outside"] += 1
            continue
        per[key]["sites"] += 1
        if hit:
            recon["sites_placed"] += 1
        else:
            recon["sites_snapped"] += 1

    recon["desa_with_outlets"] = sum(1 for v in per.values() if v["outlets"])
    recon["desa_with_sites"] = sum(1 for v in per.values() if v["sites"])
    recon["outlet_layers"] = layers
    recon["site_layers"] = site_layers
    recon["outlets_located"] = len(at)
    per = dict(per)
    _DESA_MEMO.update(stamp=stamp, per=per, recon=recon, at=at)
    _place_save(stamp, per, recon, at)
    return per, recon


def desa_placement(con):
    """{(lat, lon): desa join_key} for every outlet the roll-up placed.

    The single source both the table and FORCE FIT read, so a rep's desa on
    the map is a desa the summary agrees they hold.
    """
    desa_points(con)
    return _DESA_MEMO.get("at") or {}


def _desa_rollup(con, filters):
    """Desa are the leaf: no aggregation, just the detail rows."""
    where, params = ["1=1"], []
    hc = hier_cols(con)
    for val, col in ((filters.get("mc"), "k." + hc["k_mc"]),
                     (filters.get("branch"), "k." + hc["k_branch"]),
                     (filters.get("area"), "k." + hc["k_area"]),
                     (filters.get("kabkot"), "l.kabkot"),
                     (filters.get("kecamatan"), "l.kecamatan")):
        if val:
            where.append(f"trim(upper({col})) = ?")
            params.append(val.strip().upper())
    sql = ("""
        SELECT l.kelurahan, l.kecamatan, l.kabkot, l.%(l_mc)s mc36,
               l.%(l_branch)s branch12, l.%(l_area)s area12,
               l.%(l_partner)s partner12,
               l.join_key, coalesce(l.population,0) population,
               coalesce(l.area_m2,0)/1e6 area_km2, l.geo_type
        FROM ref_kelurahan l
        LEFT JOIN ref_kecamatan k
          ON trim(upper(k.kecamatan))=trim(upper(l.kecamatan))
         AND trim(upper(k.kabkot))=trim(upper(l.kabkot))
        WHERE """ % hc) + " AND ".join(where) + """
        ORDER BY l.population DESC"""

    # The counts come from the polygons, not from a name on a spreadsheet.
    per, _recon = desa_points(con)
    # A desa with no outlets in it and a desa in a branch nobody has exported
    # both show zero, and only one of those is a finding. Coverage is a
    # property of the file, so it is inherited from the kecamatan -- the level
    # the exports are actually organised by.
    krows, _lay = base_rows(con)
    covered = {((r["kecamatan"] or "").strip().upper(),
                (r["kabkot"] or "").strip().upper())
               for r in krows if r["has_dse_data"]}

    out = []
    for r in con.execute(sql, params):
        d = dict(r)
        d["key"] = d["kelurahan"]
        d["level"] = "desa"
        d["n_desa"] = 1
        d["pop_density"] = int(d["population"] / d["area_km2"]) if d["area_km2"] else 0
        d["geo_mix"] = d.get("geo_type") or ""
        d["geo"] = {d["geo_type"]: 1} if d.get("geo_type") else {}
        p = per.get(d.get("join_key")) or {}
        dse = p.get("dse") or set()
        d["_dse_set"] = set(dse)
        d["dse"] = len(dse)
        d["outlets"] = p.get("outlets", 0)
        d["sites"] = p.get("sites", 0)
        d["rgu_ga"] = int(p.get("rgu_ga", 0))
        d["sell_in"] = int(p.get("sell_in", 0))
        d["has_dse_data"] = ((d.get("kecamatan") or "").strip().upper(),
                             (d.get("kabkot") or "").strip().upper()) in covered
        # A desa view still wants a truthful TOTAL line. Summing a per-row
        # "1 kecamatan" would count Bekasi Utara six times, so the parents
        # travel with the row and the total counts them distinctly.
        d["_kec_of"] = ((d.get("kecamatan") or "").strip().upper(),
                        (d.get("kabkot") or "").strip().upper())
        d["_mc_of"] = d.get("mc36") or ""
        d["outlets_per_dse"] = round(d["outlets"] / d["dse"], 1) if d["dse"] else None
        d["sites_per_dse"] = round(d["sites"] / d["dse"], 1) if d["dse"] else None
        d["desa_per_dse"] = round(1 / d["dse"], 2) if d["dse"] else None
        out.append(d)
    return out


def totals(rows):
    """The 'all of it' line. Counts add; per-DSE ratios are recomputed from
    the totals rather than averaged, because an average of averages weights
    a one-DSE microcluster the same as a ten-DSE one."""
    if not rows:
        return {}
    t = {"key": "TOTAL", "level": "total"}
    for f in ("n_kecamatan", "n_desa", "population", "outlets",
              "sites", "vlr", "rgu_ga", "sell_in", "sso"):
        t[f] = sum(r.get(f) or 0 for r in rows)
    # Distinct people, not the sum of per-group counts.
    everyone = set()
    for r in rows:
        everyone |= (r.get("_dse_set") or set())
    t["dse"] = len(everyone) if everyone else sum(r.get("dse") or 0 for r in rows)
    mcs = set()
    for r in rows:
        mcs |= (r.get("_mc_set") or set())
    t["n_mc"] = len(mcs) if mcs else sum(r.get("n_mc") or 0 for r in rows)
    # Leaf rows have no child counts to add up; they have parents to count.
    if any("_kec_of" in r for r in rows):
        t["n_kecamatan"] = len({r["_kec_of"] for r in rows if r.get("_kec_of")})
        t["n_mc"] = len({r["_mc_of"] for r in rows if r.get("_mc_of")})
    t["_dse_set"] = everyone
    t["area_km2"] = round(sum(r.get("area_km2") or 0 for r in rows), 2)
    t["prepaid_revenue"] = sum(r.get("prepaid_revenue") or 0 for r in rows)
    t["pop_density"] = int(t["population"] / t["area_km2"]) if t["area_km2"] else 0
    num = sum((r["ms_ioh"] or 0) * (r["vlr"] or 0) for r in rows
              if r.get("ms_ioh") is not None)
    den = sum(r["vlr"] or 0 for r in rows if r.get("ms_ioh") is not None)
    t["ms_ioh"] = (num / den) if den else None
    geo = collections.Counter()
    for r in rows:
        geo.update(r.get("geo") or {})
    t["geo_mix"] = ", ".join(f"{k} {v}" for k, v in geo.most_common())
    t["has_dse_data"] = any(r.get("has_dse_data") for r in rows)
    d = t["dse"]
    t["sites_per_dse"] = round(t["sites"] / d, 1) if d else None
    t["outlets_per_dse"] = round(t["outlets"] / d, 1) if d else None
    t["desa_per_dse"] = round(t["n_desa"] / d, 1) if d else None
    return t


def coverage(con):
    """How much of the territory the DSE export actually covers, so the UI
    can say so instead of printing zeros."""
    rows, layers = base_rows(con)
    sites = site_info(con)
    with_data = [r for r in rows if r["has_dse_data"]]
    # A kecamatan the site file never mentions shows 0 sites, which reads as
    # "none built" rather than "not in this export". Count them so the page
    # can say which it is.
    with_sites = [r for r in rows if r["sites"]]
    return {
        "layers": layers,
        "sites_source": "site file" if sites else "kecamatan metric",
        "site_layers": (sites or {}).get("layers", []),
        "sites_placed": (sites or {}).get("placed", 0),
        "sites_outside": (sites or {}).get("outside", 0),
        "sites_total": (sites or {}).get("sites", 0),
        "site_rows_read": (sites or {}).get("rows_read", 0),
        "kecamatan_with_sites": len(with_sites),
        "kecamatan_with_dse": len(with_data),
        "kecamatan_total": len(rows),
        "mc_with_dse": len({r["mc"] for r in with_data if r["mc"]}),
        "mc_total": len({r["mc"] for r in rows if r["mc"]}),
        "branches_with_dse": sorted({r["branch"] for r in with_data if r["branch"]}),
        "dse_total": len(set().union(*[r["dse_set"] for r in rows]) if rows else set()),
        "outlets_total": sum(r["outlets"] for r in rows),
        # How the outlet pass resolved every row it read. A gap between the
        # map and the table used to be invisible; now the page can print it.
        "outlet_recon": dict(LAST_RECON),
        "counts_from": _MEMO.get("counts_from") or "export kecamatan column",
        "count_orphans": _MEMO.get("geo_orphans") or 0,
    }


def desa_coverage(con):
    """The Desa level's own reconciliation, for the page to state.

    Kept out of coverage() on purpose: it is the only pass that walks all 887
    desa polygons against all 20k points, and every page load asks for
    coverage() while only the Desa level asks for this.
    """
    _per, recon = desa_points(con)
    return dict(recon)


def _main(argv):
    ap = argparse.ArgumentParser(description="Territory roll-up")
    ap.add_argument("--level", choices=LEVELS, default="area")
    for f in ("area", "branch", "mc", "kabkot", "kecamatan"):
        ap.add_argument(f"--{f}")
    a = ap.parse_args(argv)
    con = db.connect()
    try:
        f = {k: getattr(a, k) for k in ("area", "branch", "mc", "kabkot",
                                        "kecamatan") if getattr(a, k)}
        rows, label, _ = rollup(con, a.level, **f)
        cov = coverage(con)
        print(f"\n  {label} — {len(rows)} groups"
              + (f"   filters: {f}" if f else ""))
        print(f"  DSE data covers {cov['mc_with_dse']} of {cov['mc_total']} "
              f"microclusters ({', '.join(cov['branches_with_dse']) or 'none'})\n")
        head = f"  {'':<26}{'MC':>4}{'KEC':>5}{'DESA':>6}{'km2':>9}{'POP':>11}" \
               f"{'DSE':>6}{'OUTLET':>8}{'SITE':>6}{'VLR':>10}{'O/DSE':>7}"
        print(head)
        for r in rows + [totals(rows)]:
            dse = r.get("dse") or 0
            print(f"  {str(r['key'])[:25]:<26}{r.get('n_mc',0):>4}"
                  f"{r.get('n_kecamatan',0):>5}{r.get('n_desa',0):>6}"
                  f"{r.get('area_km2',0):>9,.0f}{r.get('population',0):>11,}"
                  f"{(dse if r.get('has_dse_data') else 0):>6}"
                  f"{r.get('outlets',0):>8,}{r.get('sites',0):>6,}"
                  f"{r.get('vlr',0):>10,}"
                  f"{(r.get('outlets_per_dse') or 0):>7.1f}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
