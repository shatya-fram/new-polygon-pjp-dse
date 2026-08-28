"""
Point-in-polygon over the boundaries stored in geo_feature.

Pure Python, no GIS extension, no Flask. It lives on its own because both
the web API and the batch enricher need it, and making a command-line script
import a web framework to reach two geometry helpers is a dependency you pay
for every time you run it.
"""
import json
import sqlite3


def rings_of(geom):
    """Flatten Polygon / MultiPolygon into a list of ring-lists."""
    if not geom:
        return []
    if geom["type"] == "Polygon":
        return [geom["coordinates"]]
    if geom["type"] == "MultiPolygon":
        return list(geom["coordinates"])
    return []


def in_ring(lon, lat, ring):
    """Ray casting. `ring` is [[lon, lat], ...]."""
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if (yi > lat) != (yj > lat):
            x_at = (xj - xi) * (lat - yi) / ((yj - yi) or 1e-15) + xi
            if lon < x_at:
                inside = not inside
        j = i
    return inside


def contains(polys, lon, lat):
    """Outer ring hit, minus any hole hit. A point in a donut's hole is
    outside the polygon, and skipping that test quietly mis-assigns enclaves."""
    for rings in polys:
        if not rings or not in_ring(lon, lat, rings[0]):
            continue
        if any(in_ring(lon, lat, h) for h in rings[1:]):
            continue
        return True
    return False


def representative_point(polys):
    """A point guaranteed to lie INSIDE the polygon.

    The obvious centroid -- the mean of the vertices -- is not inside a
    shape that is bent, coastal or made of islands. Six of the 121 kecamatan,
    thirty-three of the 887 desa and two of the 36 microclusters have a
    vertex-mean that lands outside their own boundary, sometimes in the sea.
    Anything that reasons from "the middle of this area" then reasons from
    the wrong place: a distance measured from a kecamatan starts outside it,
    and a label is drawn on somebody else's territory.

    The method is the standard one: scan the widest ring at its mid-latitude,
    take every crossing, and return the middle of the longest span that is
    actually inside. Cheap, and it cannot return a point outside the shape.
    """
    best = None
    for rings in polys:
        if not rings:
            continue
        ring = rings[0]
        lats = [p[1] for p in ring]
        y = (min(lats) + max(lats)) / 2.0
        xs = []
        n = len(ring)
        for i in range(n):
            x1, y1 = ring[i][0], ring[i][1]
            x2, y2 = ring[(i + 1) % n][0], ring[(i + 1) % n][1]
            if (y1 > y) != (y2 > y):
                xs.append((x2 - x1) * (y - y1) / ((y2 - y1) or 1e-15) + x1)
        xs.sort()
        for i in range(0, len(xs) - 1, 2):
            mid = (xs[i] + xs[i + 1]) / 2.0
            if contains(polys, mid, y):
                width = xs[i + 1] - xs[i]
                if best is None or width > best[0]:
                    best = (width, y, mid)
    return (best[1], best[2]) if best else None


def inside_or_representative(polys, lat, lon):
    """Keep the given point if it is inside; otherwise find one that is."""
    if lat is not None and lon is not None and contains(polys, lon, lat):
        return lat, lon
    return representative_point(polys) or (lat, lon)


def load_polys(con, layer_key, _cache={}):
    if layer_key in _cache:
        return _cache[layer_key]
    out = []
    try:
        cur = con.execute(
            "SELECT feature_key, name, join_key, attrs_json, geometry_geojson,"
            " minlon, minlat, maxlon, maxlat FROM geo_feature "
            "WHERE layer_key = ?", (layer_key,))
    except sqlite3.OperationalError:
        return []
    for r in cur:
        try:
            geom = json.loads(r["geometry_geojson"])
        except (TypeError, ValueError):
            continue
        out.append({
            "feature_key": r["feature_key"], "name": r["name"],
            "join_key": r["join_key"],
            "attrs": json.loads(r["attrs_json"] or "{}"),
            "polys": rings_of(geom),
            "bbox": (r["minlon"], r["minlat"], r["maxlon"], r["maxlat"]),
        })
    _cache[layer_key] = out
    return out


def build_index(feats, cell=0.02):
    """Coarse grid over the polygon bounding boxes. Without it, every point
    is bbox-tested against all 121 kecamatan; with it, against two or three.
    Across 15,000 points that is a minute versus a second."""
    grid = {}
    for f in feats:
        x0, y0, x1, y1 = f["bbox"]
        for i in range(int(x0 // cell), int(x1 // cell) + 1):
            for j in range(int(y0 // cell), int(y1 // cell) + 1):
                grid.setdefault((i, j), []).append(f)
    return grid, cell


def locate_indexed(index, lat, lon):
    grid, cell = index
    for f in grid.get((int(lon // cell), int(lat // cell)), ()):
        x0, y0, x1, y1 = f["bbox"]
        if x0 <= lon <= x1 and y0 <= lat <= y1 and contains(f["polys"], lon, lat):
            return f
    return None
