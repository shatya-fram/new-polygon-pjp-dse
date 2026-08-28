"""Merge a group of adjacent polygons into their outline.

WHY THIS EXISTS
FORCE FIT hands each desa to exactly one rep, so a rep's patch is already a
set of whole desa and cannot overlap anybody else's. It used to be DRAWN,
though, by rasterising those desa onto a 600 m lattice -- and a cell whose
centre falls in one rep's desa still covers ground inside the neighbour's.
Every border therefore came out serrated and overlapping, outlets near a
boundary appeared to belong to two reps at once, and no desa was ever
covered more than about a fifth by the shape that supposedly owned it.

Drawing the desa themselves fixes all of that at once: exact edges, no
overlap by construction, and a desa is either wholly in a patch or not in it.

HOW THE MERGE WORKS, AND WHY IT IS SAFE
Two adjacent desa in this data share an identical run of vertices along their
common border -- 479 of 1,098 edges in Cikampek, none shared by more than two
polygons. So an edge that appears twice is interior and an edge that appears
once is on the outline. Cancel the pairs, stitch what is left into rings, and
the result is the union's boundary without any geometry library.

Where the data is NOT edge-matched, nothing cancels and the stitch returns
the original outlines unchanged -- the caller still gets every desa, just
with the internal borders visible. It degrades to the honest answer rather
than to a wrong one, which is why the fallback needs no special case.
"""
import collections

# Coordinates are compared at about a centimetre. Tighter and a shared border
# written out with different rounding on each side stops cancelling; looser
# and two genuinely distinct vertices start to merge.
Q = 7


def _key(p):
    return (round(p[0], Q), round(p[1], Q))


def _ring_area(ring):
    """Shoelace in degrees. Sign gives orientation, magnitude only ranks."""
    s = 0.0
    for i in range(len(ring) - 1):
        s += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1]
    return s / 2.0


def _contains(ring, pt):
    x, y = pt
    inside = False
    for i in range(len(ring) - 1):
        xi, yi = ring[i]
        xj, yj = ring[i + 1]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
    return inside


def outline(polys_list):
    """[polys, ...] -> [[ring, ...], ...] in GeoJSON Polygon-coordinate shape.

    Each input is geom.rings_of() output: a list of polygons, each a list of
    rings, each ring a list of [lon, lat]. Rings may or may not be closed.
    """
    seen = collections.Counter()
    directed = []
    for polys in polys_list:
        for poly in polys:
            for ring in poly:
                if len(ring) < 3:
                    continue
                pts = [_key(p) for p in ring]
                if pts[0] != pts[-1]:
                    pts.append(pts[0])
                for i in range(len(pts) - 1):
                    a, b = pts[i], pts[i + 1]
                    if a == b:
                        continue
                    seen[(a, b) if a <= b else (b, a)] += 1
                    directed.append((a, b))

    # An edge walked by two desa is the border between them: interior.
    nxt = collections.defaultdict(list)
    kept = 0
    for a, b in directed:
        if seen[(a, b) if a <= b else (b, a)] == 1:
            nxt[a].append(b)
            kept += 1
    if not kept:
        return []

    rings = []
    for start in list(nxt):
        while nxt.get(start):
            ring = [start]
            cur = start
            while True:
                opts = nxt.get(cur)
                if not opts:
                    break                      # open chain: dropped below
                step = opts.pop()
                if not opts:
                    nxt.pop(cur, None)
                ring.append(step)
                cur = step
                if cur == start:
                    break
            if len(ring) > 3 and ring[0] == ring[-1]:
                rings.append([list(p) for p in ring])

    if not rings:
        return []

    # OUTER RINGS AND HOLES, TOLD APART BY CONTAINMENT -- NOT BY WINDING
    #
    # Orientation looked like the cheap test and was wrong: these boundaries
    # come from KML with mixed winding, so a perfectly good outer ring that
    # happened to be stored clockwise was filed as a hole and SUBTRACTED
    # from whichever ring it landed in. A rep holding six scattered desa
    # came out at half their true area.
    #
    # Nesting here is one level deep -- a desa can sit inside a rep's patch,
    # but not inside a hole inside it -- so a ring is a hole exactly when
    # some larger ring contains it, and it belongs to the smallest such.
    rings.sort(key=lambda r: -abs(_ring_area(r)))
    parent = [None] * len(rings)
    for i in range(len(rings)):
        pt = rings[i][0]
        for j in range(i - 1, -1, -1):          # larger rings only, smallest first
            if parent[j] is None and _contains(rings[j], pt):
                parent[i] = j
                break

    out = []
    slot = {}
    for i, r in enumerate(rings):
        if parent[i] is None:
            slot[i] = len(out)
            out.append([_wind(r, True)])
    for i, r in enumerate(rings):
        if parent[i] is not None:
            out[slot[parent[i]]].append(_wind(r, False))
    return out


def _wind(ring, ccw):
    """GeoJSON wants outer rings counter-clockwise and holes clockwise. The
    source is inconsistent about it, so it is settled here rather than left
    to whatever happens to read the output."""
    return ring if (_ring_area(ring) > 0) == ccw else ring[::-1]


def geometry(polys_list):
    """-> a GeoJSON geometry for the union, or None if nothing stitched."""
    coords = outline(polys_list)
    if not coords:
        return None
    if len(coords) == 1:
        return {"type": "Polygon", "coordinates": coords[0]}
    return {"type": "MultiPolygon", "coordinates": coords}
