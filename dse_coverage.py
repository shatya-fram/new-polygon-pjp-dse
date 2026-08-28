"""
Servicing-coverage polygons per DSE, derived from the outlets a DSE holds.

There is no territory boundary file for DSE. What there is, is the set of
outlets each rep carries -- so the coverage area is *inferred* from where that
rep's outlets actually are. Every polygon this module produces is a derived
shape, not a published boundary, and it is labelled that way in the UI.

Two modes, because they answer different questions:

  coverage  A footprint. Each outlet stamps a disc of `reach` metres onto a
            grid; the union of a DSE's stamped cells is its serviced area.
            Concave, allows holes, allows disjoint clusters -- so a rep who
            works two separate neighbourhoods gets two shapes, which is the
            truth. This is the default.

  exclusive The same stamp, but a cell reached by two reps goes to the
            NEARER one rather than to both. Non-overlapping by construction,
            still leaves genuine gaps unowned, and never moves an outlet
            between reps. Use it when the question is "who works this
            street"; use `coverage` when it is "where does this rep work".

  hull      The convex hull of the rep's outlets. One shape, no gaps, always
            larger than reality. Useful for seeing overlap between reps at a
            glance and for nothing else -- it will happily claim a river.

Pure standard library, like geom.py, and for the same reason: this runs inside
a request and inside a batch script, and neither should need a GIS stack.

All distance work happens in metres on a local equirectangular projection
anchored at the layer's own centroid. Over a kabupaten that is accurate to
well under a metre, and unlike working in degrees it does not make a 250 m
cell 11% wider than it is tall at this latitude.
"""
import json
import math

EARTH_R = 6371008.8

# Defaults tuned for Kota Bekasi's outlet density: median nearest-neighbour
# spacing between outlets sits in the low hundreds of metres, so a 400 m
# reach closes the gaps within a cluster without bridging across a kecamatan.
DEFAULT_CELL_M = 200.0
DEFAULT_REACH_M = 400.0
MIN_POINTS = 3


# ── projection ───────────────────────────────────────────────────────────
def projector(lat0):
    """-> (to_xy, to_lonlat) around a local origin latitude."""
    k = math.cos(math.radians(lat0))

    def to_xy(lat, lon):
        return (math.radians(lon) * EARTH_R * k, math.radians(lat) * EARTH_R)

    def to_lonlat(x, y):
        return (math.degrees(x / (EARTH_R * k)), math.degrees(y / EARTH_R))

    return to_xy, to_lonlat


# ── grid-union footprint ─────────────────────────────────────────────────
def _stamp(pts_xy, cell, reach):
    """Cells whose centre falls within `reach` of at least one point."""
    cells = set()
    r_cells = int(math.ceil(reach / cell))
    r2 = reach * reach
    for x, y in pts_xy:
        ci, cj = int(math.floor(x / cell)), int(math.floor(y / cell))
        for i in range(ci - r_cells, ci + r_cells + 1):
            for j in range(cj - r_cells, cj + r_cells + 1):
                cx, cy = (i + 0.5) * cell, (j + 0.5) * cell
                if (cx - x) ** 2 + (cy - y) ** 2 <= r2:
                    cells.add((i, j))
    return cells


def _boundary_edges(cells, owned_by=None, key=None):
    """As below, but each edge also says whether it faces empty ground.

    An edge between two DIFFERENT owners is a seam: both territories trace the
    same lattice segment, in opposite directions. Rounding it independently on
    each side bends the two copies apart and their outlines cross -- which is
    what "the polygons intersect" looks like on screen even when ownership
    overlap is exactly zero. Seam edges are therefore left on the lattice and
    only the outward-facing ones are smoothed.
    """
    return _edges_impl(cells, owned_by, key)


def _edges_impl(cells, owned_by=None, key=None):
    def free(c):
        # True when the neighbouring cell is nobody's -- the outside world.
        if c in cells:
            return None
        if owned_by is None:
            return True
        return owned_by.get(c) is None

    out = {}
    for i, j in cells:
        for nb, a, b in (((i, j - 1), (i, j), (i + 1, j)),
                         ((i + 1, j), (i + 1, j), (i + 1, j + 1)),
                         ((i, j + 1), (i + 1, j + 1), (i, j + 1)),
                         ((i - 1, j), (i, j + 1), (i, j))):
            f = free(nb)
            if f is None:
                continue
            out.setdefault(a, []).append((b, bool(f)))
    return out


def _boundary_edges_plain(cells):
    """Directed edges of the union boundary, filled cell always on the left.

    An edge between two filled cells is interior and never emitted. Orienting
    what remains means the walk below produces counter-clockwise outer rings
    and clockwise holes without a separate winding pass.
    """
    out = {}
    for i, j in cells:
        if (i, j - 1) not in cells:
            out.setdefault((i, j), []).append((i + 1, j))          # bottom →
        if (i + 1, j) not in cells:
            out.setdefault((i + 1, j), []).append((i + 1, j + 1))  # right ↑
        if (i, j + 1) not in cells:
            out.setdefault((i + 1, j + 1), []).append((i, j + 1))  # top ←
        if (i - 1, j) not in cells:
            out.setdefault((i, j + 1), []).append((i, j))          # left ↓
    return out


def _walk_rings(edges):
    """Chain directed edges into closed rings of grid vertices."""
    edges = {k: list(v) for k, v in edges.items()}
    rings = []
    for start in list(edges):
        while edges.get(start):
            ring = [start]
            cur = start
            while True:
                nxts = edges.get(cur)
                if not nxts:
                    break                       # open chain: drop it
                nxt = nxts.pop()
                if not nxts:
                    edges.pop(cur, None)
                if nxt == start:
                    break
                ring.append(nxt)
                cur = nxt
            if len(ring) >= 4:
                rings.append(ring)
    return rings


def _walk_tagged(edges):
    """Chain tagged edges into rings, carrying each edge's `free` flag.

    -> [(vertices, flags)] where flags[i] describes the edge leaving
    vertices[i]. A False flag marks a seam shared with another territory.
    """
    e = {k: list(v) for k, v in edges.items()}
    rings = []
    for start in list(e):
        while e.get(start):
            verts, flags, cur = [start], [], start
            while True:
                nxts = e.get(cur)
                if not nxts:
                    break
                nxt, free = nxts.pop()
                if not nxts:
                    e.pop(cur, None)
                flags.append(free)
                if nxt == start:
                    break
                verts.append(nxt)
                cur = nxt
            if len(verts) >= 4 and len(flags) == len(verts):
                rings.append((verts, flags))
    return rings


def _drop_collinear_tagged(verts, flags):
    """Collapse straight runs, but only where the flag does not change --
    a seam meeting a free edge is a corner worth keeping."""
    ov, of = [], []
    n = len(verts)
    for i in range(n):
        ax, ay = verts[i - 1]
        bx, by = verts[i]
        cx, cy = verts[(i + 1) % n]
        straight = (bx - ax) * (cy - by) == (by - ay) * (cx - bx)
        if straight and flags[i - 1] == flags[i]:
            continue
        ov.append((bx, by))
        of.append(flags[i])
    return (ov, of) if len(ov) >= 4 else (verts, flags)


def _chaikin_tagged(verts, flags, iters=2):
    """Chaikin, but a corner is only cut when BOTH of its edges face empty
    ground. A vertex on a seam stays exactly where the lattice put it, so the
    two territories sharing that seam trace identical geometry and their
    outlines meet instead of crossing."""
    for _ in range(iters):
        nv, nf = [], []
        n = len(verts)
        for i in range(n):
            ax, ay = verts[i]
            bx, by = verts[(i + 1) % n]
            free_here = flags[i]
            free_prev = flags[i - 1]
            if free_here and free_prev:
                nv.append((ax * 0.75 + bx * 0.25, ay * 0.75 + by * 0.25))
                nf.append(free_here)
                nv.append((ax * 0.25 + bx * 0.75, ay * 0.25 + by * 0.75))
                nf.append(free_here)
            else:
                nv.append((ax, ay))
                nf.append(free_here)
                if free_here:
                    nv.append((ax * 0.25 + bx * 0.75, ay * 0.25 + by * 0.75))
                    nf.append(free_here)
        verts, flags = nv, nf
    return verts


def _drop_collinear(ring):
    out = []
    n = len(ring)
    for i in range(n):
        ax, ay = ring[i - 1]
        bx, by = ring[i]
        cx, cy = ring[(i + 1) % n]
        if (bx - ax) * (cy - by) != (by - ay) * (cx - bx):
            out.append((bx, by))
    return out or ring


def _chaikin(ring, iters=2):
    """Round the staircase off. The grid is a computational device, not a
    claim about where the boundary is to the metre; leaving 90-degree steps on
    an inferred polygon reads as false precision."""
    for _ in range(iters):
        nxt = []
        n = len(ring)
        for i in range(n):
            ax, ay = ring[i]
            bx, by = ring[(i + 1) % n]
            nxt.append((ax * 0.75 + bx * 0.25, ay * 0.75 + by * 0.25))
            nxt.append((ax * 0.25 + bx * 0.75, ay * 0.25 + by * 0.75))
        ring = nxt
    return ring


def _area(ring):
    s = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s / 2.0


def _in_ring(pt, ring):
    x, y = pt
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if (yi > y) != (yj > y):
            if x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-15) + xi:
                inside = not inside
        j = i
    return inside


def _assemble(rings):
    """Group clockwise rings (holes) into the smallest CCW ring containing
    them. Without this a donut renders as a filled blob and a rep's coverage
    looks bigger than it is."""
    outers = [r for r in rings if _area(r) > 0]
    holes = [r for r in rings if _area(r) <= 0]
    outers.sort(key=lambda r: abs(_area(r)))
    polys = [[o] for o in outers]
    for h in holes:
        probe = h[0]
        for idx, o in enumerate(outers):
            if _in_ring(probe, o):
                polys[idx].append(h)
                break
    return polys


# ── exclusive footprint (reach-clipped nearest-outlet partition) ─────────
def owner_cells(keyed_xy, cell, reach):
    """cell -> key, awarding each cell to the rep whose nearest outlet is
    closest, and to nobody at all beyond `reach`.

    This is the same disc stamp as `coverage_polys`, with one extra rule: a
    cell reached by two reps goes to the nearer one instead of to both. That
    single change is what turns a footprint into a territory.

    Note what it does NOT do: it never moves an outlet between reps. Group
    membership is whatever the file says; only the ground between the outlets
    is being adjudicated. An outlet's own cell is at distance zero, so an
    outlet is always inside its own rep's area.

    And it is still not exhaustive -- ground further than `reach` from every
    outlet stays unowned, which is the honest answer for a paddy field nobody
    works. A Voronoi partition would hand it to whoever is least far away and
    report full coverage.
    """
    best = {}
    r_cells = int(math.ceil(reach / cell))
    r2 = reach * reach
    for x, y, key in keyed_xy:
        ci, cj = int(math.floor(x / cell)), int(math.floor(y / cell))
        for i in range(ci - r_cells, ci + r_cells + 1):
            for j in range(cj - r_cells, cj + r_cells + 1):
                cx, cy = (i + 0.5) * cell, (j + 0.5) * cell
                d2 = (cx - x) ** 2 + (cy - y) ** 2
                if d2 > r2:
                    continue
                cur = best.get((i, j))
                if cur is None or d2 < cur[0]:
                    best[(i, j)] = (d2, key)
    return {c: v[1] for c, v in best.items()}


# ── sliver islands ───────────────────────────────────────────────────────
# The number of cells below which a detached fragment is treated as noise
# rather than as an outpost. At the 200 m default that is 8 cells = 0.32 km²,
# about four city blocks -- small enough that no one would staff it, big
# enough that a genuine satellite cluster survives.
MIN_ISLAND_CELLS = 8


def absorb_islands(owner, min_cells=MIN_ISLAND_CELLS):
    """Fold each rep's tiny detached fragments into the rep around them.

    WHY THE FRAGMENTS EXIST
        Exclusive awards every cell to the rep whose nearest outlet is
        closest. When one of A's shops stands in the middle of B's patch, the
        few cells around that shop are genuinely nearer to A, so A's
        territory sprouts a three-cell island inside B's. The partition is
        correct and the picture is unreadable.

    WHY NOT JUST STOP DRAWING THEM
        Because the cells would still be A's, and the map would show a white
        hole where a territory used to be -- swapping a confusing shape for a
        missing one. Reassigning the cells keeps the partition complete: the
        ground goes to the rep already surrounding it, the tiling still has
        no gaps and no overlaps, and the only thing that changes is that a
        speck stops being drawn as a separate country.

    WHAT IT WILL NOT DO
        It never moves an outlet between reps -- group membership is whatever
        the file says. An outlet whose own cell was absorbed now stands inside
        a neighbour's polygon, and that is reported as a seam outlet rather
        than hidden. And a fragment with no neighbouring territory to join --
        a real satellite cluster out on its own -- is left exactly where it
        is, however small, because there is nobody it could sensibly belong
        to and its isolation is the fact worth seeing.

    Returns (owner, report). `owner` is a new dict; the input is untouched.
    """
    by_key = {}
    for c, k in owner.items():
        by_key.setdefault(k, set()).add(c)

    out = dict(owner)
    absorbed = moved = kept = 0
    for key, cells in by_key.items():
        comps = _components(cells)
        if len(comps) < 2:
            continue
        # The rep's largest fragment is its territory by definition, whatever
        # its size -- a rep with one small patch is small, not fragmentary.
        comps.sort(key=len, reverse=True)
        for comp in comps[1:]:
            if len(comp) >= min_cells:
                continue
            host = _surrounding_owner(comp, owner, key)
            if host is None:
                kept += 1
                continue
            for c in comp:
                out[c] = host
            absorbed += 1
            moved += len(comp)
    return out, {"islands_absorbed": absorbed, "cells_moved": moved,
                 "islands_kept": kept, "min_island_cells": min_cells}


def _components(cells):
    """4-connected components of a cell set."""
    seen, comps = set(), []
    for start in cells:
        if start in seen:
            continue
        comp, stack = set(), [start]
        seen.add(start)
        while stack:
            i, j = stack.pop()
            comp.add((i, j))
            for n in ((i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)):
                if n in cells and n not in seen:
                    seen.add(n)
                    stack.append(n)
        comps.append(comp)
    return comps


def _surrounding_owner(comp, owner, mine):
    """The rep holding most of the ground around this fragment, or None.

    Counted by contact length, not by area: the neighbour a fragment shares
    the most border with is the one it reads as belonging to.
    """
    touch = {}
    for (i, j) in comp:
        for n in ((i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)):
            if n in comp:
                continue
            k = owner.get(n)
            if k is None or k == mine:
                continue
            touch[k] = touch.get(k, 0) + 1
    if not touch:
        return None
    return max(sorted(touch), key=lambda k: touch[k])


def rings_from_cells(cells, cell, owned_by=None):
    """owned_by: the full cell->owner map. Supplied, shared seams are left
    unsmoothed so neighbouring territories tile exactly."""
    if owned_by is None:
        rings = _walk_rings(_boundary_edges_plain(cells))
        rings = [_chaikin(_drop_collinear([(i * cell, j * cell) for i, j in r]))
                 for r in rings]
        return _assemble([r for r in rings if len(r) >= 4])

    out = []
    for verts, flags in _walk_tagged(_edges_impl(cells, owned_by)):
        verts = [(i * cell, j * cell) for i, j in verts]
        verts, flags = _drop_collinear_tagged(verts, flags)
        r = _chaikin_tagged(verts, flags)
        if len(r) >= 4:
            out.append(r)
    return _assemble(out)


def coverage_cells(pts_xy, cell, reach):
    return _stamp(pts_xy, cell, reach)


def coverage_polys(pts_xy, cell, reach):
    return rings_from_cells(_stamp(pts_xy, cell, reach), cell)


# ── convex hull ──────────────────────────────────────────────────────────
def hull_polys(pts_xy):
    pts = sorted(set(pts_xy))
    if len(pts) < 3:
        return []

    def half(seq):
        out = []
        for p in seq:
            while len(out) >= 2:
                (ax, ay), (bx, by) = out[-2], out[-1]
                if (bx - ax) * (p[1] - ay) - (by - ay) * (p[0] - ax) > 0:
                    break
                out.pop()
            out.append(p)
        return out[:-1]

    ring = half(pts) + half(reversed(pts))
    return [[ring]] if len(ring) >= 3 else []


# ── colour ───────────────────────────────────────────────────────────────
def _hsl_hex(h, s, li):
    def f(n):
        k = (n + h / 30.0) % 12
        a = s * min(li, 1 - li)
        return round(255 * (li - a * max(-1, min(k - 3, 9 - k, 1))))
    return "#%02x%02x%02x" % (f(0), f(8), f(4))


def colour_for(index):
    """Golden-angle hue stepping, alternating lightness.

    Twenty-nine territories is far past the point where a categorical palette
    can guarantee every pair is distinguishable -- so colour here is a lookup
    aid, never the only channel. Every polygon carries its DSE code in a hover
    label and in the legend, and the golden angle at least guarantees that
    numerically adjacent DSE never land on adjacent hues.
    """
    h = (index * 137.508) % 360.0
    light = 0.62 if index % 2 == 0 else 0.46
    return _hsl_hex(h, 0.68, light)


# ── build ────────────────────────────────────────────────────────────────
def build(groups, mode="coverage", cell=DEFAULT_CELL_M, reach=DEFAULT_REACH_M):
    """groups: {key: [(lat, lon), ...]} -> (features, skipped)

    `skipped` names the groups with too few points to enclose. They are
    reported rather than silently dropped: a DSE missing from the map because
    it holds two outlets is a finding, not a rendering detail.
    """
    allpts = [p for pts in groups.values() for p in pts]
    if not allpts:
        return [], [], {"overlap_pct": None, "union_km2": None}
    lat0 = sum(p[0] for p in allpts) / len(allpts)
    to_xy, to_lonlat = projector(lat0)

    # Exclusive mode adjudicates the whole region at once -- a cell cannot be
    # awarded to the nearer rep without seeing every rep -- so the ownership
    # pass runs before the per-group loop rather than inside it.
    owned = None
    island_report = {}
    if mode == "exclusive":
        keyed = [(x, y, key) for key, pts in groups.items()
                 for x, y in (to_xy(la, lo) for la, lo in pts)]
        owner = owner_cells(keyed, cell, reach)
        # Before any ring is traced: fold each rep's specks into whoever
        # surrounds them, so the shapes that get drawn are the shapes that
        # mean something. Done here rather than after tracing because the
        # partition has to stay gapless.
        owner, island_report = absorb_islands(owner)
        owned = {}
        for c, key in owner.items():
            owned.setdefault(key, set()).add(c)

    feats, skipped = [], []
    cells_total, cells_union = 0, set()
    for idx, key in enumerate(sorted(groups)):
        pts = groups[key]
        if len(pts) < MIN_POINTS:
            skipped.append({"key": key, "points": len(pts)})
            continue
        xy = [to_xy(la, lo) for la, lo in pts]
        if mode == "hull":
            polys = hull_polys(xy)
        else:
            cells = (owned.get(key, set()) if mode == "exclusive"
                     else coverage_cells(xy, cell, reach))
            owner_map = owner if mode == "exclusive" else None
            # Counting cells rather than measuring polygons is what makes
            # overlap computable without a geometry library: a cell claimed
            # by two reps is counted twice in the total and once in the union.
            cells_total += len(cells)
            cells_union |= cells
            polys = rings_from_cells(cells, cell, owner_map)
        if not polys:
            skipped.append({"key": key, "points": len(pts)})
            continue
        coords, area_m2 = [], 0.0
        for poly in polys:
            rings = []
            for ri, ring in enumerate(poly):
                area_m2 += _area(ring) if ri == 0 else -abs(_area(ring))
                r = [list(to_lonlat(x, y)) for x, y in ring]
                r.append(r[0])
                rings.append(r)
            coords.append(rings)
        geom = ({"type": "Polygon", "coordinates": coords[0]}
                if len(coords) == 1
                else {"type": "MultiPolygon", "coordinates": coords})
        feats.append({
            "type": "Feature", "geometry": geom,
            "properties": {
                "dse": key, "colour": colour_for(idx), "outlets": len(pts),
                "parts": len(polys), "mode": mode,
                "area_km2": round(abs(area_m2) / 1e6, 3),
                "derived": True,
            }})
    stats = {"overlap_pct": None, "union_km2": None}
    stats.update(island_report)
    if cells_total:
        stats["overlap_pct"] = round(
            (1 - len(cells_union) / cells_total) * 100, 1)
        stats["union_km2"] = round(len(cells_union) * cell * cell / 1e6, 1)
    return feats, skipped, stats


def assign(feats, lat, lon):
    """Which DSE coverage polygon contains this point? -> key or None.

    Used to place an uploaded site into a rep's territory. Overlaps are
    possible by construction, so the smallest containing polygon wins -- the
    tighter shape is the better claim.
    """
    best, best_area = None, None
    for f in feats:
        g = f["geometry"]
        polys = ([g["coordinates"]] if g["type"] == "Polygon"
                 else g["coordinates"])
        for rings in polys:
            outer = [(p[0], p[1]) for p in rings[0]]
            if not _in_ring((lon, lat), outer):
                continue
            if any(_in_ring((lon, lat), [(p[0], p[1]) for p in h])
                   for h in rings[1:]):
                continue
            a = f["properties"]["area_km2"]
            if best_area is None or a < best_area:
                best, best_area = f["properties"]["dse"], a
    return best
