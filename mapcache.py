#!/usr/bin/env python3
"""
Build each boundary layer ONCE into a small, gzipped GeoJSON on disk.

THE PROBLEM THIS SOLVES
    `/api/territory/<layer>.geojson` rebuilt the whole answer on every
    request: 46 MB of desa coordinates at full precision, plus a database
    query PER FEATURE to attach the popup figures -- 7,761 round trips for
    one map. The data has not changed since it was uploaded, so all of that
    work was being repeated to produce a byte-identical answer.

    Here it is done once, when the layer is imported, and the result is a
    file. Serving a map becomes reading a file.

TWO THINGS MAKE THE FILE SMALL
    Simplification -- Douglas-Peucker at a tolerance chosen per layer -- and
    coordinate rounding. Five decimal places is about a metre; a desa
    boundary drawn on a screen 1,500 px wide covering 400 km cannot express
    better than 250 m per pixel, so the extra digits were never visible.

    Simplification is per-polygon, not topological, so two neighbours can
    part company by up to the tolerance. At 11 m on a map of West Java that
    is invisible; it would NOT be acceptable for area calculations, which is
    why nothing computes from these files -- they are for drawing. Every
    figure still comes from `geo_feature`.
"""
import gzip
import json
import os
import sqlite3

import config

CACHE_DIR = os.path.join(config.DATA_DIR, "mapcache")

# metres, roughly, at this latitude: 0.0001° ≈ 11 m
TOLERANCE = {
    "kelurahan": 0.00010,
    "kecamatan": 0.00015,
    "indosat_mc": 0.00015,
    "kabkot": 0.00030,
}
DEFAULT_TOLERANCE = 0.00010
PRECISION = 5

# Nothing outside the three regions is cached, whatever layer it is in and
# however it got into the database. The import gate should already have
# stopped it; this is the second lock, so a layer loaded by an older build
# or a hand-run script cannot put Central Java on a map of Jakarta.
def _on_map(lat, lon):
    return config.on_map(lat, lon)


def _sq_seg_dist(p, a, b):
    x, y = a
    dx, dy = b[0] - x, b[1] - y
    if dx or dy:
        t = ((p[0] - x) * dx + (p[1] - y) * dy) / (dx * dx + dy * dy)
        if t > 1:
            x, y = b
        elif t > 0:
            x += dx * t
            y += dy * t
    dx, dy = p[0] - x, p[1] - y
    return dx * dx + dy * dy


def simplify(points, tol):
    """Douglas-Peucker, iterative so a 40,000-point ring cannot blow the
    stack -- and some of these rings are that long."""
    if len(points) < 4:
        return points
    sq = tol * tol
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        first, last = stack.pop()
        worst, index = 0.0, None
        for i in range(first + 1, last):
            d = _sq_seg_dist(points[i], points[first], points[last])
            if d > worst:
                worst, index = d, i
        if index is not None and worst > sq:
            keep[index] = True
            stack.append((first, index))
            stack.append((index, last))
    return [p for p, k in zip(points, keep) if k]


def _ring(ring, tol):
    r = simplify([(p[0], p[1]) for p in ring], tol)
    out, prev = [], None
    for x, y in r:
        p = [round(x, PRECISION), round(y, PRECISION)]
        if p != prev:
            out.append(p)
            prev = p
    # A ring needs four points to close; if simplification has taken it below
    # that the shape was smaller than the tolerance and is dropped, not bent
    # into a triangle that was never there.
    if len(out) >= 4:
        if out[0] != out[-1]:
            out.append(out[0])
        return out
    return None


def thin(geom, tol):
    t = geom.get("type")
    if t == "Point":
        c = geom["coordinates"]
        return {"type": "Point",
                "coordinates": [round(c[0], PRECISION), round(c[1], PRECISION)]}
    polys = [geom["coordinates"]] if t == "Polygon" else geom.get("coordinates", [])
    out = []
    for parts in polys:
        rings = [r for r in (_ring(p, tol) for p in parts) if r]
        if rings:
            out.append(rings)
    if not out:
        return None
    return ({"type": "Polygon", "coordinates": out[0]} if len(out) == 1
            else {"type": "MultiPolygon", "coordinates": out})


def path_for(layer_key):
    os.makedirs(CACHE_DIR, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in layer_key)
    return os.path.join(CACHE_DIR, f"{safe}.geojson.gz")


def points_path(layer_key):
    os.makedirs(CACHE_DIR, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in layer_key)
    return os.path.join(CACHE_DIR, f"{safe}.points.geojson.gz")


def points_fresh(layer_key):
    p = points_path(layer_key)
    if not os.path.exists(p) or not os.path.exists(p + ".stamp"):
        return False
    con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    try:
        stamp = stamp_of(con, layer_key)
    finally:
        con.close()
    if not stamp:
        return False
    return open(p + ".stamp", encoding="utf-8").read().strip() == stamp


def write_points(layer_key, fc):
    """Cache a trimmed point collection gzipped, stamped with the import it
    came from so a re-upload invalidates it."""
    p = points_path(layer_key)
    tmp = p + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as fh:
        json.dump(fc, fh, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, p)
    con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    try:
        stamp = stamp_of(con, layer_key)
    finally:
        con.close()
    if stamp:
        open(p + ".stamp", "w", encoding="utf-8").write(stamp)
    return p, os.path.getsize(p)


def stamp_of(con, layer_key):
    try:
        r = con.execute("SELECT imported_utc, feature_count FROM geo_layer "
                        "WHERE layer_key=?", (layer_key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return f"{r[0]}|{r[1]}" if r else None


def is_fresh(con, layer_key):
    """A cache is fresh only if it names the import it was built from."""
    p = path_for(layer_key)
    if not os.path.exists(p):
        return False
    stamp = stamp_of(con, layer_key)
    side = p + ".stamp"
    if not stamp or not os.path.exists(side):
        return False
    return open(side, encoding="utf-8").read().strip() == stamp


def build(con, layer_key, props_for=None, tol=None):
    """Write the gzipped FeatureCollection. -> (path, features, bytes).

    `props_for` is called ONCE with every row, not once per row: the caller
    hands back a dict keyed on feature_key, so the per-feature database
    queries become one query per layer."""
    tol = TOLERANCE.get(layer_key, DEFAULT_TOLERANCE) if tol is None else tol
    rows = list(con.execute(
        "SELECT feature_key, name, join_key, attrs_json, geometry_geojson, "
        "minlon, minlat, maxlon, maxlat "
        "FROM geo_feature WHERE layer_key = ?", (layer_key,)))
    def _keep(r):
        if r["minlon"] is None:
            return True                 # no bbox to judge it by
        return _on_map((r["minlat"] + r["maxlat"]) / 2.0,
                       (r["minlon"] + r["maxlon"]) / 2.0)
    rows = [r for r in rows if _keep(r)]
    extra = props_for(rows) if props_for else {}

    p = path_for(layer_key)
    tmp = p + ".tmp"
    n = 0
    with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as fh:
        fh.write('{"type":"FeatureCollection","name":%s,"features":['
                 % json.dumps(layer_key))
        for r in rows:
            try:
                geom = json.loads(r["geometry_geojson"] or "null")
            except (TypeError, ValueError):
                continue
            if not geom:
                continue
            geom = thin(geom, tol)
            if geom is None:
                continue
            props = {"feature_key": r["feature_key"], "name": r["name"],
                     "join_key": r["join_key"], "layer": layer_key}
            try:
                props.update({k.lower(): v for k, v in
                              json.loads(r["attrs_json"] or "{}").items()})
            except (TypeError, ValueError):
                pass
            props.update(extra.get(r["feature_key"], {}))
            if n:
                fh.write(",")
            fh.write(json.dumps({"type": "Feature", "geometry": geom,
                                 "properties": props},
                                separators=(",", ":"), ensure_ascii=False))
            n += 1
        fh.write("]}")
    os.replace(tmp, p)
    stamp = stamp_of(con, layer_key)
    if stamp:
        open(p + ".stamp", "w", encoding="utf-8").write(stamp)
    return p, n, os.path.getsize(p)


def drop(layer_key):
    for base in (path_for(layer_key), points_path(layer_key)):
        for suffix in ("", ".stamp", ".tmp"):
            try:
                os.remove(base + suffix)
            except OSError:
                pass
