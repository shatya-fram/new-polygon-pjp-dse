"""
FORCE FIT — rebalance DSE territories onto whole desa, against workload norms.

WHAT IT DOES DIFFERENTLY

The Exclusive model draws whatever the outlets say and lets the workload fall
where it may: a rep can end up with nine desa or with two, because nobody
asked. FORCE FIT starts from the same assignment and *moves whole desa*
between neighbouring reps until each rep sits inside the norms:

    urban   1-3 desa   ·  50-70 outlets
    rural   1-5 desa   ·  50-60 outlets   (7 desa tolerated as an outlier)

Territories are unions of whole desa, never part of one. That is the point:
a rep who owns "half of Pekayon Jaya" owns a line on a map, whereas a rep who
owns Pekayon Jaya owns something the field organisation can actually name,
hand over, and hold to a number.

MINIMAL CHANGE IS A CONSTRAINT, NOT A PREFERENCE

Every move is a real disruption -- a rep loses shops they know. So the search
is greedy and local: only a desa on the boundary between two reps can move,
only to a rep that already works next door, and only when the move reduces
total violation. It stops as soon as no move improves things, and reports how
many desa it touched. A run that fixes eleven reps by moving four desa is a
better answer than one that fixes twelve by moving forty, and the report says
which you got.

WHERE IT CANNOT WIN

If a branch holds 29 reps and 63 desa, no arrangement gives every rep 1-3
desa: the desa run out. Under-target reps are then reported as such rather
than papered over by stealing desa from someone who needs them. The norms are
a target to measure against, not an axiom to force the data to satisfy.
"""
import collections
import math

import geom

# stratum -> (min_desa, max_desa, outlier_desa, min_outlets, max_outlets)
NORMS = {
    "urban": (1, 3, 4, 50, 70),
    "rural": (1, 5, 7, 50, 60),
}
URBAN_GEO = ("METRO / DENSE URBAN", "METRO", "DENSE URBAN", "URBAN")
MAX_PASSES = 400


def stratum_of(geo_type):
    return "urban" if (geo_type or "").strip().upper() in URBAN_GEO else "rural"


# ── the desa layer, with its stratum ─────────────────────────────────────
def desa_index(con):
    """-> (features, spatial index, {name: stratum})."""
    feats = geom.load_polys(con, "kelurahan")
    strat = {}
    try:
        for r in con.execute(
                "SELECT join_key, geo_type FROM ref_kelurahan"):
            strat[r["join_key"]] = stratum_of(r["geo_type"])
    except Exception:                                             # noqa: BLE001
        pass
    return feats, geom.build_index(feats), strat


def assign_outlets(idx, points, placed=None):
    """[(dse, lat, lon)] -> {desa_key: Counter(dse -> n)}, unplaced count.

    `placed` is the roll-up's own {(lat, lon): desa} answer. When it is
    supplied it wins, and that is the point: this function used to decide for
    itself with a bare containment test, which quietly disagreed with the
    summary table for the few dozen outlets that sit in a seam between the
    desa and kecamatan covers, or whose coordinates need their named
    kecamatan to resolve. A model that hands a rep a desa the table credits
    to someone else is worse than a model that is merely approximate -- it
    cannot be checked. Containment stays as the fallback for a point the
    roll-up never saw.
    """
    per = collections.defaultdict(collections.Counter)
    unplaced = 0
    for dse, lat, lon in points:
        key = (placed or {}).get((lat, lon))
        if key is None:
            f = geom.locate_indexed(idx, lat, lon)
            key = f["feature_key"] if f else None
        if key is None:
            unplaced += 1
            continue
        per[key][dse] += 1
    return per, unplaced


# ── adjacency ────────────────────────────────────────────────────────────
def adjacency(feats, keys, pad=0.004):
    """Desa that touch. Bounding boxes with a small pad, not exact shared
    edges: the desa polygons come from three different exports and their
    borders do not align to the metre, so an exact test would call genuine
    neighbours strangers. A slightly generous neighbour list only widens the
    set of moves considered; every move is still scored on its merits."""
    byk = {f["feature_key"]: f for f in feats if f["feature_key"] in keys}
    items = list(byk.items())
    adj = collections.defaultdict(set)
    for i in range(len(items)):
        ka, fa = items[i]
        ax0, ay0, ax1, ay1 = fa["bbox"]
        for j in range(i + 1, len(items)):
            kb, fb = items[j]
            bx0, by0, bx1, by1 = fb["bbox"]
            if (ax0 - pad <= bx1 and bx0 - pad <= ax1
                    and ay0 - pad <= by1 and by0 - pad <= ay1):
                adj[ka].add(kb)
                adj[kb].add(ka)
    return adj


# ── scoring ──────────────────────────────────────────────────────────────
def _violation(n_desa, n_out, stratum):
    """How far outside the norms, in comparable units. Desa and outlets are
    both counted, but a desa is worth more: it is the unit being moved, and a
    rep two desa over target is a bigger problem than one five outlets over."""
    lo_d, hi_d, out_d, lo_o, hi_o = NORMS[stratum]
    v = 0.0
    if n_desa > hi_d:
        v += (n_desa - hi_d) * (1.0 if n_desa <= out_d else 3.0)
    elif n_desa < lo_d:
        v += (lo_d - n_desa) * 1.0
    if n_out > hi_o:
        v += (n_out - hi_o) / 20.0
    elif n_out < lo_o:
        v += (lo_o - n_out) / 20.0
    return v


def _state(owner, desa_out, desa_strat):
    """-> {dse: (desa_keys, outlets, stratum)}"""
    per = collections.defaultdict(lambda: [set(), 0])
    for k, d in owner.items():
        per[d][0].add(k)
        per[d][1] += sum(desa_out[k].values())
    out = {}
    for d, (keys, n) in per.items():
        rural = sum(1 for k in keys if desa_strat.get(k, "rural") == "rural")
        out[d] = (keys, n, "rural" if rural * 2 > len(keys) else "urban")
    return out


def total_violation(state):
    return sum(_violation(len(k), n, s) for k, n, s in state.values())


# ── the rebalance ────────────────────────────────────────────────────────
def rebalance(desa_out, desa_strat, adj, max_moves=None):
    """Greedy, local, and it stops when nothing improves.

    Returns (owner, report). `owner` maps desa -> DSE.
    """
    owner = {}
    for k, c in desa_out.items():
        if c:
            owner[k] = c.most_common(1)[0][0]
    before = dict(owner)
    state = _state(owner, desa_out, desa_strat)
    start_v = total_violation(state)

    moves, seen = [], set()
    for _ in range(MAX_PASSES):
        state = _state(owner, desa_out, desa_strat)
        best = None
        for k, cur in list(owner.items()):
            # only a desa with a neighbour under a different rep can move
            cands = {owner[n] for n in adj.get(k, ()) if owner.get(n)
                     and owner[n] != cur}
            if not cands:
                continue
            n_out = sum(desa_out[k].values())
            ck, cn, cs = state[cur]
            v_cur = _violation(len(ck), cn, cs)
            for tgt in cands:
                if (k, tgt) in seen:
                    continue
                tk, tn, ts = state[tgt]
                v_tgt = _violation(len(tk), tn, ts)
                after = (_violation(len(ck) - 1, cn - n_out, cs)
                         + _violation(len(tk) + 1, tn + n_out, ts))
                gain = (v_cur + v_tgt) - after
                # A move must actually help, and the shop count it drags
                # along breaks ties: same relief, fewer outlets uprooted.
                if gain > 1e-9 and (best is None or
                                    (gain, -n_out) > (best[0], -best[3])):
                    best = (gain, k, tgt, n_out, cur)
        if not best:
            break
        _, k, tgt, n_out, was = best
        owner[k] = tgt
        seen.add((k, was))
        moves.append({"desa": k, "from": was, "to": tgt, "outlets": n_out})
        if max_moves and len(moves) >= max_moves:
            break

    state = _state(owner, desa_out, desa_strat)
    ok, over_d, under_d, over_o, under_o = 0, 0, 0, 0, 0
    for keys, n, s in state.values():
        lo_d, hi_d, out_d, lo_o, hi_o = NORMS[s]
        nd = len(keys)
        good = True
        if nd > hi_d:
            over_d += 1
            good = False
        elif nd < lo_d:
            under_d += 1
            good = False
        if n > hi_o:
            over_o += 1
            good = False
        elif n < lo_o:
            under_o += 1
            good = False
        if good:
            ok += 1
    return owner, {
        "moved": len(moves),
        "moves": moves[:80],
        "changed_desa": sum(1 for k in owner if before.get(k) != owner[k]),
        "total_desa": len(owner),
        "dse": len(state),
        "in_norm": ok,
        "over_desa": over_d, "under_desa": under_d,
        "over_outlets": over_o, "under_outlets": under_o,
        "violation_before": round(start_v, 2),
        "violation_after": round(total_violation(state), 2),
    }


# ── rasterise whole desa into the cell model ─────────────────────────────
def owner_cells(feats, owner, to_xy, cell):
    """Cells covered by each owned desa, keyed by owner.

    Reuses the same lattice the other models draw on, so FORCE FIT territories
    render through exactly the same seam-aware path and tile without crossing.
    """
    out = {}
    for f in feats:
        d = owner.get(f["feature_key"])
        if not d or not f["polys"]:
            continue
        x0, y0, x1, y1 = f["bbox"]
        ax, ay = to_xy(y0, x0)
        bx, by = to_xy(y1, x1)
        i0, i1 = int(math.floor(min(ax, bx) / cell)), int(math.ceil(max(ax, bx) / cell))
        j0, j1 = int(math.floor(min(ay, by) / cell)), int(math.ceil(max(ay, by) / cell))
        for i in range(i0, i1 + 1):
            for j in range(j0, j1 + 1):
                if (i, j) in out:
                    continue
                cx, cy = (i + 0.5) * cell, (j + 0.5) * cell
                lon, lat = _inv(to_xy, cx, cy)
                if geom.contains(f["polys"], lon, lat):
                    out[(i, j)] = d
    return out


def _inv(to_xy, x, y):
    """to_xy is linear, so one probe recovers the scale and inverts it."""
    px, py = to_xy(1.0, 1.0)
    return (x / px, y / py)
