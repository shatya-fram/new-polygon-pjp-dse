#!/usr/bin/env python3
"""
Future GAPURA — Stage 1 kecamatan screening for new IM3 / 3ID service points.

THE MODEL IS NOT MINE
    This implements **KSI v2.0** exactly as specified in
    `Retail Geolocator/docs/03-SCORING-MODEL.md` §0. Weights, sub-weights and
    gates are transcribed from that document; nothing here is invented. Where
    the document asks for an input this database does not hold, the term is
    reported as UNAVAILABLE and its weight is redistributed across the terms
    that do exist — never silently zeroed, because a zero and a missing
    measurement produce very different rankings and only one of them is
    honest.

    Stage 2 (SAI, H3 cell -> site point, §2-§4 of the same document) needs
    travel-time isochrones, rent, day/night population and a coverage-quality
    index. None of those are in this database, so SAI is NOT implemented.
    Uploaded candidate sites get an explicitly-labelled proxy built from the
    Stage-1 terms that do exist. It is not SAI and must not be reported as
    SAI.

WHAT IS MISSING, AND WHAT IT COSTS
    coverage_quality_index  gate G4           no source; G4 is skipped
    Everything else the document asks for is now present, so only G4 is
    dropped. The API still reports availability and any redistribution, so a
    reader can always see the model they actually got rather than the one on
    paper.

    Two terms that used to be dark, and how they were lit:

    population   Summed from `ref_kelurahan.population` -- 887 desa, all 121
                 kecamatan, nothing missing. DERIVED, and labelled so: there
                 is no population column on ref_kecamatan. Do NOT use the
                 desa KMZ's `POPULASI_4` for this; it carries the kecamatan
                 total stamped onto every desa in the kecamatan, so summing
                 it multiplies the region by about six.
                 `pop_metric` chooses head count (the document's r(population))
                 or people per km2. Density is the default here because a
                 compact kecamatan and a sprawling one with the same head
                 count are not the same catchment.
                 `ref_kelurahan.pop_index` is NOT a usable index -- it holds
                 two distinct values across all 887 desa.
    pasar        `pull_osm.py --target marketplaces` took it from 54 found in
                 35% of kecamatan to 356 in 71%, over the 60% floor, so CVI
                 now runs at the document's full 0.35/0.35/0.30.

THE DISTANCE TERM IS LOCAL, AND SAYS SO
    `dist_term` converts gate G1 into a graded, weighted term. It is not in
    the document. It exists because a hard 10 km cut removes all twenty of
    the largest kecamatan in the territory -- every one sits 2-6 km from a
    counter -- and no weight on population can reach a kecamatan the gate has
    already deleted. When it is on, `model` says so, the document's six
    weights are renormalised to (1 - dist_weight) so they keep their ratios,
    and G1 stops gating. Turn it off for the document as written.

URBAN vs RURAL
    §1 of the document offers percentile ranking "within urban / suburban /
    rural strata ... report both the territory-wide and within-stratum
    score", precisely to stop a Karawang kecamatan being invisible for the
    crime of not being Sudirman. That is what the two-profile requirement
    means here, and both scores are returned on every row.

      urban = DKI Jakarta (5 kota), Kota Depok, Kota Bekasi
      rural = Kepulauan Seribu, Kabupaten Bekasi, Karawang, and anything
              else the AOI rectangle clips in (one Purwakarta kecamatan)

GATE G1 POINTS THE OTHER WAY
    The document's G1 is `dist_nearest_store_km <= max_dist_km` -- stay
    within reach of the existing estate. The brief for this menu asked for
    the opposite: at least 10 km FROM the nearest gerai, to avoid
    cannibalising it. Both are defensible and they select opposite
    kecamatan, so the direction is a parameter (`g1_mode`) rather than a
    decision made quietly in code. Default is the brief's `min`; switch to
    `max` to run the document as written.

HYBRID IM3 / 3STORE  (`hybrid_own`, on by default)
    A Gerai IM3 serves 3ID customers and a 3Store serves IM3 customers, so
    for a siting decision the two are one network with two signboards, not
    two networks. With `hybrid_own` on, G1 measures the distance to the
    nearest service point of EITHER brand and tests it once. With it off,
    each brand is gated separately against its own threshold.

    The difference is not cosmetic. Split gating is the weaker test: with
    only 5 3Store on file its half of the gate is nearly inert, so a
    kecamatan two kilometres from a Gerai still clears the 3ID half and
    looks partly open. Hybrid gating asks the question the business
    actually cares about -- is there already a counter a customer can walk
    into -- and both distances stay on every row either way.
"""
import bisect
import json
import math
import re
import sqlite3

import db

MODEL_VERSION = "KSI v2.0 — 03-SCORING-MODEL.md §0"
DOC_PATH = "Retail Geolocator/docs/03-SCORING-MODEL.md"

# ── profiles / strata ────────────────────────────────────────────────────
URBAN_KABKOT = {
    "JAKARTA PUSAT", "JAKARTA UTARA", "JAKARTA BARAT",
    "JAKARTA SELATAN", "JAKARTA TIMUR", "KOTA DEPOK", "DEPOK", "KOTA BEKASI",
}
RURAL_KABKOT = {"KEPULAUAN SERIBU", "BEKASI", "KARAWANG", "PURWAKARTA"}


def profile_of(kabkot):
    return "urban" if (kabkot or "").strip().upper() in URBAN_KABKOT else "rural"


# ── the model, transcribed ───────────────────────────────────────────────
KSI_WEIGHTS = {                     # §0, sums to 1.00
    "population": 0.20,
    "vlr_total":  0.20,
    "ms_score":   0.12,
    "cvi":        0.28,
    "cpi":        0.12,
    "network":    0.08,
}
CVI_WEIGHTS = {"finance": 0.35, "minimarket": 0.35, "pasar": 0.30}
FINANCE_WEIGHTS = {"atm_den": 0.60, "bank_den": 0.40}
CPI_WEIGHTS = {"primary": 0.65, "comp_den": 0.35}
NETWORK_WEIGHTS = {"vlr_per_site": 0.60, "site_count": 0.40}

TERM_LABELS = {
    "population":  "Population (percentile)",
    "distance":    "Distance from nearest IM3 / 3Store",
    "vlr_total":   "VLR subscribers (percentile)",
    "ms_score":    "Market-share score",
    "cvi":         "CVI — commercial vitality",
    "cpi":         "CPI — competitive presence",
    "network":     "Network score",
    "finance":     "Finance (0.6 ATM + 0.4 bank density)",
    "minimarket":  "Minimarket density",
    "pasar":       "Pasar density",
}

STRATEGIES = ("DENSIFY", "ATTACK")

# ── the distance term is NOT in the document ─────────────────────────────
# §0 spends distance as gate G1: a kecamatan is in or out. The brief asked
# for the opposite direction (stay away from the estate, not near it), and
# then for the gate to become a graded term, because a hard 10 km cut
# excludes every one of the twenty largest kecamatan in the territory --
# they all sit 2-6 km from a counter -- and no weight on population can
# reach a kecamatan the gate already removed.
#
# So this is a LOCAL VARIANT and is labelled as one. When it is on, the
# model version string says so, the document's six weights are renormalised
# to (1 - dist_weight), and G1 stops gating. `g1_mode` keeps 'min' and 'max'
# so the document as written, and the brief's inversion of it, both remain
# one parameter away.
#
# The ramp: score 0 at or below `dist_zero_km`, 1.0 at or above
# `dist_full_km`, linear between. Both ends are parameters because the
# right distance is a commercial judgement, not a measurement.
DIST_TERM_DEFAULTS = {"dist_weight": 0.15, "dist_zero_km": 2.0,
                      "dist_full_km": 15.0}


def distance_score(km, zero_km, full_km):
    """-> 0..1, or None when there is no service point to measure against."""
    if km is None:
        return None
    if full_km <= zero_km:
        return 1.0 if km >= full_km else 0.0
    return max(0.0, min(1.0, (km - zero_km) / (full_km - zero_km)))

DEFAULTS = {
    # Gates
    "strategy":     "DENSIFY",   # G3 direction, §0
    "g1_mode":      "min",       # 'min' = brief (keep away) · 'max' = document
    "hybrid_own":   True,        # IM3 and 3Store are one interchangeable network
    "min_km_hybrid": 10.0,       # hybrid: km to the nearest of EITHER brand
    "min_km_im3":   10.0,        # split: >= 10 km from nearest Gerai IM3
    "min_km_3id":   10.0,        # split: >= 10 km from nearest 3Store
    "vlr_min":      80000.0,     # brief: absolute VLR floor
    "vlr_pct_min":  40.0,        # G2 as written in the document
    "ms_pct_min":   40.0,        # G3 DENSIFY
    "ms_pct_max":   50.0,        # G3 ATTACK
    "gates":        True,
    # Scoring
    "use_cpi":      False,       # CPI needs competitor coverage this DB lacks
    "pop_metric":   "density",   # 'density' = people per km2 · 'total' = head count
    "dist_term":    True,        # distance as a graded term instead of gate G1
    "dist_weight":  0.15,        # local, not from the document — see above
    "dist_zero_km": 2.0,         # at or below this, the distance term scores 0
    "dist_full_km": 15.0,        # at or above this, it scores 1.0
    "scope":        "stratum",   # which percentile drives the headline rank
    "own_includes_ipp": False,   # count IPP outlets as own retail presence
    "top_banks":    ["BCA", "Bank Mandiri", "BNI", "BRI"],
    # Candidate catchments (Stage-2 proxy only)
    "radius_urban_km": 1.5,
    "radius_rural_km": 3.0,
    "top_n":        25,
}


def merge_params(overrides=None):
    p = dict(DEFAULTS)
    p["top_banks"] = list(DEFAULTS["top_banks"])
    for k, v in (overrides or {}).items():
        if v is None or v == "" or k not in p:
            continue
        if k == "top_banks":
            p[k] = [s.strip() for s in (v if isinstance(v, list)
                                        else str(v).split(",")) if s.strip()]
        elif k == "pop_metric":
            p[k] = "total" if str(v).lower() == "total" else "density"
        elif k in ("gates", "own_includes_ipp", "hybrid_own", "dist_term",
                   "use_cpi"):
            p[k] = v if isinstance(v, bool) else \
                str(v).lower() in ("1", "true", "yes", "on")
        elif k == "strategy":
            p[k] = str(v).upper() if str(v).upper() in STRATEGIES else "DENSIFY"
        elif k == "g1_mode":
            p[k] = "max" if str(v).lower() == "max" else "min"
        elif k == "scope":
            p[k] = "territory" if str(v).lower() == "territory" else "stratum"
        elif k == "top_n":
            p[k] = int(v)
        else:
            p[k] = float(v)
    return p


# ── geometry ─────────────────────────────────────────────────────────────
R_EARTH_KM = 6371.0088


def haversine_km(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * R_EARTH_KM * math.asin(math.sqrt(a))


def ring_area_km2(ring, lat0):
    """Shoelace on a local equirectangular projection. At kecamatan scale the
    error against a geodesic area is under a tenth of a percent — far smaller
    than the disagreement between the boundary file and reality."""
    if len(ring) < 3:
        return 0.0
    kx = math.cos(math.radians(lat0)) * R_EARTH_KM * math.pi / 180.0
    ky = R_EARTH_KM * math.pi / 180.0
    s = 0.0
    for i in range(len(ring)):
        x1, y1 = ring[i][0] * kx, ring[i][1] * ky
        x2, y2 = ring[(i + 1) % len(ring)][0] * kx, ring[(i + 1) % len(ring)][1] * ky
        s += x1 * y2 - x2 * y1
    return abs(s) / 2.0


def polys_area_km2(polys, lat0):
    total = 0.0
    for rings in polys:
        if not rings:
            continue
        total += ring_area_km2(rings[0], lat0)
        for hole in rings[1:]:
            total -= ring_area_km2(hole, lat0)
    return max(total, 0.0)


def norm_key(kec, kabkot):
    return re.sub(r"[^A-Z0-9]", "", f"{kec or ''}{kabkot or ''}".upper())


# ── percentile rank, §1 ──────────────────────────────────────────────────
def pct_ranks(vals):
    """r(x) in [0, 1], ties sharing the average position. Percentile rather
    than min-max because this territory spans Sudirman and rural Karawang and
    one extreme value under min-max flattens everything else (§1)."""
    n = len(vals)
    if n == 0:
        return []
    if n == 1:
        return [0.5]
    order = sorted(range(n), key=lambda i: vals[i])
    out = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        p = ((i + j) / 2.0) / (n - 1)
        for k in range(i, j + 1):
            out[order[k]] = p
        i = j + 1
    return out


def _pct_against(sorted_vals, v):
    if not sorted_vals:
        return 0.5
    lo = bisect.bisect_left(sorted_vals, v)
    hi = bisect.bisect_right(sorted_vals, v)
    return ((lo + hi) / 2.0) / len(sorted_vals)


# ── spatial index ────────────────────────────────────────────────────────
CELL = 0.02          # ~2.2 km


class PointIndex:
    def __init__(self, rows):
        self.grid = {}
        for lat, lon, kind in rows:
            self.grid.setdefault(
                (int(lon // CELL), int(lat // CELL)), []).append((lat, lon, kind))

    def within(self, lat, lon, km):
        span = int(km / (CELL * 111.0)) + 1
        cx, cy = int(lon // CELL), int(lat // CELL)
        tally = {}
        for i in range(cx - span, cx + span + 1):
            for j in range(cy - span, cy + span + 1):
                for plat, plon, kind in self.grid.get((i, j), ()):
                    if haversine_km(lat, lon, plat, plon) <= km:
                        tally[kind] = tally.get(kind, 0) + 1
        return tally


# ── loading ──────────────────────────────────────────────────────────────
OWN_OPS = ("IM3", "3ID")
COMP_OPS = ("TSEL", "XL", "SF")

# A pasar has to be recognisable as one. The marketplace tags are the honest
# signal; the name match is a fallback with the classes that produce false
# positives excluded, because half of "Pasar Minggu"-style names in this data
# are bus stops and stations named after the market, not the market.
PASAR_SQL = ("(category IN ('market','amenity=marketplace','shop=marketplace') "
             "OR (lower(name) LIKE 'pasar %' AND coalesce(poi_class,'') "
             "NOT IN ('bus','rail','atm','bank','industry','fuel')))")
PASAR_MIN_COVERAGE = 0.60      # share of kecamatan that must have >0 to rank


def _latest(periods):
    if not periods:
        return None
    keys = [k for k in periods if k]
    return periods[max(keys)] if keys else periods.get("")


def load_metrics(con, names):
    """metric -> kec_key -> latest value."""
    out = {n: {} for n in names}
    q = ", ".join("?" * len(names))
    for r in con.execute(
            f"SELECT metric, entity_key, coalesce(period,'') p, value_num "
            f"FROM ref_metric WHERE entity_type='kecamatan' AND metric IN ({q})",
            tuple(names)):
        out[r["metric"]].setdefault(r["entity_key"], {})[r["p"]] = r["value_num"]
    return {m: {k: _latest(v) for k, v in d.items()} for m, d in out.items()}


def load_service_points(con, include_ipp=False):
    """Own and competitor retail presence, per kecamatan and as coordinates.

    IPP outlets are Indosat's indirect partner estate. Whether they count as
    "own stores" for retail share of voice is a commercial question, not a
    technical one, so it is a parameter — but they are always excluded from
    the G1 distance test, which is about cannibalising a *gerai*."""
    own_ops = list(OWN_OPS) + (["IPP"] if include_ipp else [])
    per_kec = {}
    # "hybrid" is IM3 + 3ID and never IPP: G1 is about a branded counter a
    # customer can walk into, which a partner outlet is not.
    coords = {"IM3": [], "3ID": [], "hybrid": [], "own": [], "comp": []}
    for r in con.execute(
            "SELECT operator, adm_kecamatan, adm_kota, lat, lon "
            "FROM ref_service_point"):
        op = r["operator"]
        if r["lat"] is not None and r["lon"] is not None:
            if op in ("IM3", "3ID"):
                coords[op].append((r["lat"], r["lon"]))
                coords["hybrid"].append((r["lat"], r["lon"]))
            if op in own_ops:
                coords["own"].append((r["lat"], r["lon"]))
            elif op in COMP_OPS:
                coords["comp"].append((r["lat"], r["lon"]))
        if not r["adm_kecamatan"]:
            continue
        k = norm_key(r["adm_kecamatan"], r["adm_kota"])
        slot = per_kec.setdefault(k, {"own": 0, "comp": 0, "ipp": 0})
        if op in own_ops:
            slot["own"] += 1
        elif op in COMP_OPS:
            slot["comp"] += 1
        if op == "IPP":
            slot["ipp"] += 1
    return per_kec, coords


def _bank_clause(top_banks):
    quoted = ", ".join("'" + str(b).replace("'", "''") + "'" for b in top_banks)
    return f"brand_resolved IN ({quoted})" if quoted else "0"


def load_poi_counts(con, top_banks):
    counts = {}
    sql = f"""
        SELECT adm_kecamatan k, adm_kota kab,
               sum(poi_class = 'retail')                              AS minimarket_n,
               sum(poi_class = 'atm')                                 AS atm_n,
               sum(poi_class = 'bank')                                AS bank_n,
               sum(poi_class = 'bank' AND {_bank_clause(top_banks)})  AS bank_top4_n,
               sum({PASAR_SQL})                                       AS pasar_n,
               sum(poi_class = 'mall')                                AS mall_n,
               sum(poi_class = 'agent')                               AS agent_n
        FROM poi_unified WHERE adm_kecamatan IS NOT NULL GROUP BY 1, 2"""
    for r in con.execute(sql):
        counts[norm_key(r["k"], r["kab"])] = {
            c: (r[c] or 0) for c in
            ("minimarket_n", "atm_n", "bank_n", "bank_top4_n",
             "pasar_n", "mall_n", "agent_n")}
    return counts


def load_poi_points(con, top_banks):
    rows = []
    for r in con.execute(
            f"""SELECT lat, lon,
                       CASE WHEN poi_class = 'retail' THEN 'minimarket'
                            WHEN poi_class = 'atm'    THEN 'atm'
                            WHEN poi_class = 'bank' AND {_bank_clause(top_banks)}
                                 THEN 'bank_top4'
                            ELSE 'bank' END AS kind
                FROM poi_unified
                WHERE lat IS NOT NULL AND lon IS NOT NULL
                  AND poi_class IN ('retail','atm','bank')"""):
        rows.append((r["lat"], r["lon"], r["kind"]))
    return PointIndex(rows)


def _nearest(lat, lon, points):
    if not points:
        return None
    return round(min(haversine_km(lat, lon, a, b) for a, b in points), 3)


def load_kecamatan(con, params):
    """One row per boundary polygon with every KSI input attached."""
    import geom

    m = load_metrics(con, ["vlr_subs", "site_id", "fbms_ioh", "sso",
                           "prepaid_revenue_nett"])
    counts = load_poi_counts(con, params["top_banks"])
    per_kec, coords = load_service_points(con, params["own_includes_ipp"])

    ref = {}
    for r in con.execute("SELECT * FROM ref_kecamatan WHERE join_key IS NOT NULL"):
        ref[r["join_key"]] = dict(r)

    # Population is a DERIVED figure here: ref_kecamatan has no population
    # column, so it is summed from the desa export. Keyed both ways because
    # the desa and kecamatan files spell their join keys differently.
    # `POPULASI_4` on the desa KMZ is NOT usable for this -- it carries the
    # kecamatan total stamped onto every desa in the kecamatan, so summing it
    # multiplies the region by roughly six. ref_kelurahan.population is the
    # genuine per-desa figure.
    pop_roll = {}
    try:
        for r in con.execute(
                "SELECT kec_join_key, kecamatan, kabkot, SUM(population) pop "
                "FROM ref_kelurahan WHERE population IS NOT NULL "
                "GROUP BY kec_join_key"):
            if r["pop"] is None:
                continue
            pop_roll[r["kec_join_key"]] = r["pop"]
            pop_roll[norm_key(r["kecamatan"], r["kabkot"])] = r["pop"]
    except sqlite3.OperationalError:
        pass

    centro = {r["feature_key"]: (r["centroid_lat"], r["centroid_lon"])
              for r in con.execute(
                  "SELECT feature_key, centroid_lat, centroid_lon FROM geo_feature "
                  "WHERE layer_key='kecamatan'")}

    rows = []
    for f in geom.load_polys(con, "kecamatan"):
        attrs = f["attrs"] or {}
        kec = attrs.get("KEC") or f["name"]
        kab = attrs.get("KABKOT") or ""
        key = norm_key(kec, kab)
        meta = ref.get(f["join_key"]) or ref.get(key) or {}
        kk = meta.get("kec_key")
        lat0 = (f["bbox"][1] + f["bbox"][3]) / 2.0
        area = polys_area_km2(f["polys"], lat0)
        clat, clon = centro.get(
            f["feature_key"], (lat0, (f["bbox"][0] + f["bbox"][2]) / 2.0))

        c = counts.get(key, {})
        s = per_kec.get(key, {"own": 0, "comp": 0, "ipp": 0})
        vlr = m["vlr_subs"].get(kk) or 0.0
        sites = m["site_id"].get(kk) or 0.0
        row = {
            "join_key": f["join_key"], "key": key, "kec_key": kk,
            "kecamatan": kec, "kabkot": kab,
            # mc36 is the current microcluster cut; mc is the generation
            # that came with the profile spreadsheet. Show the current one
            # where it exists so the ranking and the map agree.
            "mc": meta.get("mc36") or meta.get("mc"), "sa": meta.get("sa"),
            "profile": profile_of(kab),
            "area_km2": round(area, 3), "lat": clat, "lon": clon,
            "population": pop_roll.get(f["join_key"]) or pop_roll.get(key),
            "pop_source": "ref_kelurahan (summed from desa)",
            "vlr": vlr,
            "ms_ioh": m["fbms_ioh"].get(kk),
            "site_count": sites,
            "vlr_per_site": round(vlr / sites, 1) if sites else 0.0,
            "own_stores": s["own"], "comp_stores": s["comp"],
            "ipp_stores": s["ipp"],
        }
        for k in ("minimarket_n", "atm_n", "bank_n", "bank_top4_n",
                  "pasar_n", "mall_n", "agent_n"):
            row[k] = c.get(k, 0)
        for src, den in (("minimarket_n", "minimarket_den"), ("atm_n", "atm_den"),
                         ("bank_n", "bank_den"), ("pasar_n", "pasar_den"),
                         ("bank_top4_n", "bank_top4_den")):
            row[den] = round(row[src] / area, 4) if area > 0 else 0.0
        row["comp_den"] = round(s["comp"] / area, 4) if area > 0 else 0.0
        row["pop_density"] = (round(row["population"] / area, 1)
                              if (row["population"] and area > 0) else None)

        tot = s["own"] + s["comp"]
        # §0: retail_sov is NULL when neither we nor a rival has a store there.
        # Zero would claim we measured an absence; NULL says we did not.
        row["retail_sov"] = (s["own"] / tot) if tot else None
        row["rsov_gap"] = ((row["ms_ioh"] - row["retail_sov"])
                           if (row["ms_ioh"] is not None
                               and row["retail_sov"] is not None) else None)
        row["km_im3"] = _nearest(clat, clon, coords["IM3"])
        row["km_3id"] = _nearest(clat, clon, coords["3ID"])
        row["km_hybrid"] = _nearest(clat, clon, coords["hybrid"])
        row["km_own"] = _nearest(clat, clon, coords["own"])
        rows.append(row)
    return rows


# ── availability audit ───────────────────────────────────────────────────
def availability(rows):
    """Which KSI terms this database can actually support, and why not.

    Returned to the UI verbatim. A model that quietly drops a 20% term is a
    different model from the one in the document, and the reader is entitled
    to know which one produced the number they are looking at."""
    n = len(rows) or 1
    pasar_cov = sum(1 for r in rows if r["pasar_n"] > 0) / n
    store_cov = sum(1 for r in rows
                    if r["own_stores"] or r["comp_stores"]) / n
    out = {
        "population": {
            "ok": any(r["population"] is not None for r in rows),
            "why": "" if any(r["population"] is not None for r in rows) else
                   "No population in ref_kelurahan either. Import the desa "
                   "export and the 0.20 term switches itself on."},
        "vlr_total": {"ok": any(r["vlr"] for r in rows), "why": ""},
        "ms_score": {"ok": any(r["ms_ioh"] is not None for r in rows), "why": ""},
        "cvi": {"ok": True, "why": ""},
        "cpi": {"ok": store_cov > 0,
                "why": f"Only {store_cov:.0%} of kecamatan have any own or "
                       f"competitor service point, so retail share of voice "
                       f"is NULL for the rest and scores neutral there."},
        "network": {"ok": any(r["site_count"] for r in rows), "why": ""},
    }
    sub = {
        "finance": {"ok": True, "why": ""},
        "minimarket": {"ok": True, "why": ""},
        "pasar": {
            "ok": pasar_cov >= PASAR_MIN_COVERAGE,
            "why": "" if pasar_cov >= PASAR_MIN_COVERAGE else
                   f"Marketplaces found in only {pasar_cov:.0%} of kecamatan "
                   f"({sum(r['pasar_n'] for r in rows)} in total) — too sparse "
                   f"to rank. Fix: pull OSM amenity=marketplace "
                   f"(pull_osm.py --target marketplaces)."},
    }
    out["_cvi_sub"] = sub
    out["_gates"] = {
        "G4": {"ok": False,
               "why": "No coverage_quality_index in this database; G4 is "
                      "skipped rather than assumed to pass."}}
    return out


def _renorm(weights, avail):
    keep = {k: v for k, v in weights.items() if avail.get(k, {}).get("ok", True)}
    total = sum(keep.values()) or 1.0
    return {k: v / total for k, v in keep.items()}


# ── scoring ──────────────────────────────────────────────────────────────
def _ranks_with_nulls(group, key):
    """Rank the rows that have a value against each other; a row with no
    value scores the neutral 0.5 rather than the bottom of the scale."""
    idx = [i for i, r in enumerate(group) if r.get(key) is not None]
    out = [0.5] * len(group)
    if idx:
        pr = pct_ranks([group[i][key] for i in idx])
        for j, i in enumerate(idx):
            out[i] = pr[j]
    return out


def _score_group(group, params, ksiw, cviw):
    if not group:
        return
    strategy = params["strategy"]
    r_vlr = pct_ranks([r["vlr"] or 0 for r in group])
    r_ms = _ranks_with_nulls(group, "ms_ioh")
    r_atm = pct_ranks([r["atm_den"] for r in group])
    r_bank = pct_ranks([r["bank_den"] for r in group])
    r_mini = pct_ranks([r["minimarket_den"] for r in group])
    r_pasar = pct_ranks([r["pasar_den"] for r in group])
    r_comp = pct_ranks([r["comp_den"] for r in group])
    r_vps = pct_ranks([r["vlr_per_site"] for r in group])
    r_sites = pct_ranks([r["site_count"] for r in group])
    r_gap = _ranks_with_nulls(group, "rsov_gap")
    r_sov = _ranks_with_nulls(group, "retail_sov")
    # Density or head count -- the document says r(population) and means the
    # head count; density is the local option, because a compact kecamatan
    # and a sprawling one with the same population are not the same catchment.
    r_pop = _ranks_with_nulls(
        group, "pop_density" if params.get("pop_metric") == "density"
        else "population")

    out = []
    for i, r in enumerate(group):
        finance = (FINANCE_WEIGHTS["atm_den"] * r_atm[i]
                   + FINANCE_WEIGHTS["bank_den"] * r_bank[i])
        cvi = (cviw.get("finance", 0) * finance
               + cviw.get("minimarket", 0) * r_mini[i]
               + cviw.get("pasar", 0) * r_pasar[i])
        ms_score = r_ms[i] if strategy == "DENSIFY" else 1.0 - r_ms[i]
        primary = r_gap[i] if strategy == "DENSIFY" else 1.0 - r_sov[i]
        cpi = (CPI_WEIGHTS["primary"] * primary
               + CPI_WEIGHTS["comp_den"] * r_comp[i]) if "cpi" in ksiw else None
        network = (NETWORK_WEIGHTS["vlr_per_site"] * r_vps[i]
                   + NETWORK_WEIGHTS["site_count"] * r_sites[i])
        terms = {"population": (r_pop[i] if r.get("population") is not None
                                else None),
                 "vlr_total": r_vlr[i], "ms_score": ms_score,
                 "cvi": cvi, "cpi": cpi, "network": network}
        if "distance" in ksiw:
            terms["distance"] = distance_score(
                r.get("km_hybrid") if params.get("hybrid_own", True)
                else r.get("km_own"),
                params["dist_zero_km"], params["dist_full_km"])
        ksi = sum(w * terms[t] for t, w in ksiw.items() if terms.get(t) is not None)
        out.append({
            "ksi": round(100 * ksi, 2),
            "terms": {t: round(100 * v, 1) for t, v in terms.items() if v is not None},
            "sub": {"finance": round(100 * finance, 1),
                    "minimarket": round(100 * r_mini[i], 1),
                    "pasar": round(100 * r_pasar[i], 1),
                    "atm_den": round(100 * r_atm[i], 1),
                    "bank_den": round(100 * r_bank[i], 1),
                    "comp_den": round(100 * r_comp[i], 1),
                    "vlr_per_site": round(100 * r_vps[i], 1),
                    "site_count": round(100 * r_sites[i], 1),
                    "rsov_gap": round(100 * r_gap[i], 1),
                    "retail_sov": round(100 * r_sov[i], 1)},
            "parts": [{"term": t, "label": TERM_LABELS[t],
                       "pct": round(100 * terms[t], 1), "weight": round(w, 4),
                       "points": round(100 * w * terms[t], 2)}
                      for t, w in sorted(ksiw.items(), key=lambda x: -x[1])
                      if terms.get(t) is not None],
        })
    for r, o in zip(group, out):
        r["_scored"] = o


def apply_gates(row, params, vlr_pct, ms_pct):
    """G1-G4 of §0. Returns the list of failures; empty means eligible.

    With `dist_term` on, G1 does not run: distance is being scored instead,
    and gating on it as well would remove the kecamatan the term exists to
    rank."""
    fails = []
    mode = params["g1_mode"]
    if params.get("dist_term"):
        mode = None
    if params.get("hybrid_own", True):
        # One network, two signboards: the nearest counter of either brand.
        tests = (("IM3 or 3Store", "km_hybrid", "min_km_hybrid"),)
    else:
        tests = (("IM3", "km_im3", "min_km_im3"),
                 ("3Store", "km_3id", "min_km_3id"))
    for op, key, pkey in tests:
        need = params.get(pkey) or 0
        got = row.get(key)
        if mode is None or not need or got is None:
            continue
        if mode == "min" and got < need:
            fails.append(f"G1 {got:.1f} km to nearest {op} < {need:g} km")
        elif mode == "max" and got > need:
            fails.append(f"G1 {got:.1f} km to nearest {op} > {need:g} km")
    if params.get("vlr_min") and (row.get("vlr") or 0) < params["vlr_min"]:
        fails.append("G2 VLR {:,} < {:,}".format(int(row.get("vlr") or 0),
                                                 int(params["vlr_min"])))
    if params.get("vlr_pct_min") and vlr_pct is not None \
            and vlr_pct < params["vlr_pct_min"]:
        fails.append(f"G2 VLR percentile {vlr_pct:.0f} < "
                     f"{params['vlr_pct_min']:g}")
    if ms_pct is not None:
        if params["strategy"] == "DENSIFY" and params.get("ms_pct_min") \
                and ms_pct < params["ms_pct_min"]:
            fails.append(f"G3 DENSIFY: share percentile {ms_pct:.0f} < "
                         f"{params['ms_pct_min']:g}")
        if params["strategy"] == "ATTACK" and params.get("ms_pct_max") \
                and ms_pct > params["ms_pct_max"]:
            fails.append(f"G3 ATTACK: share percentile {ms_pct:.0f} > "
                         f"{params['ms_pct_max']:g}")
    return fails


def score_rows(rows, params):
    """Score every kecamatan twice — once against the whole territory, once
    within its own stratum — exactly as §1 asks, then let `scope` decide
    which of the two drives the headline rank."""
    avail = availability(rows)
    # Turning CPI off is a CONFIGURATION choice, not a missing measurement,
    # and the audit says which of the two happened. The weight is
    # redistributed the same way either way -- never zeroed, because a
    # kecamatan scoring 0 on competition and one we cannot measure rank very
    # differently and only one of those is true.
    if not params.get("use_cpi", True):
        n = len(rows) or 1
        seen = sum(1 for r in rows
                   if r["own_stores"] or r["comp_stores"]) / n
        avail["cpi"] = {
            "ok": False,
            "why": f"Switched off. Only {seen:.0%} of kecamatan hold any own "
                   f"or competitor service point "
                   f"({sum(r['comp_stores'] for r in rows)} competitor points "
                   f"in total), so the term scored the neutral 50 almost "
                   f"everywhere and its 0.12 was noise. Weight redistributed."}
    ksiw = _renorm(KSI_WEIGHTS, avail)
    cviw = _renorm(CVI_WEIGHTS, avail["_cvi_sub"])
    # The local distance term takes its weight off the top and the document's
    # terms share what is left, so the six keep their ratios to each other and
    # only their absolute size changes. Anyone comparing a run with the term
    # against a run without it is then comparing like with like.
    if params.get("dist_term") and any(r.get("km_hybrid") is not None
                                       for r in rows):
        w = max(0.0, min(0.9, float(params.get("dist_weight") or 0)))
        if w:
            ksiw = {k: v * (1.0 - w) for k, v in ksiw.items()}
            ksiw["distance"] = w

    # Gate percentiles are territory-wide, as the document specifies.
    tv = pct_ranks([r["vlr"] or 0 for r in rows])
    tm = _ranks_with_nulls(rows, "ms_ioh")
    for i, r in enumerate(rows):
        r["vlr_pct"] = round(100 * tv[i], 1)
        r["ms_pct"] = round(100 * tm[i], 1) if r["ms_ioh"] is not None else None

    _score_group(rows, params, ksiw, cviw)
    for r in rows:
        r["ksi_territory"] = r.pop("_scored")
    for prof in ("urban", "rural"):
        grp = [r for r in rows if r["profile"] == prof]
        _score_group(grp, params, ksiw, cviw)
        for r in grp:
            r["ksi_stratum"] = r.pop("_scored")

    scope = params["scope"]
    for r in rows:
        chosen = r["ksi_stratum" if scope == "stratum" else "ksi_territory"]
        other = r["ksi_territory" if scope == "stratum" else "ksi_stratum"]
        r["score"] = chosen["ksi"]
        r["parts"] = chosen["parts"]
        r["terms"] = chosen["terms"]
        r["sub"] = chosen["sub"]
        r["score_territory"] = r["ksi_territory"]["ksi"]
        r["score_stratum"] = r["ksi_stratum"]["ksi"]
        r["score_other"] = other["ksi"]
        del r["ksi_territory"], r["ksi_stratum"]
        r["gate_fails"] = apply_gates(r, params, r["vlr_pct"], r["ms_pct"])
        r["eligible"] = (not r["gate_fails"]) or not params.get("gates", True)
        if not r["eligible"]:
            r["score"] = None

    ranked = sorted([r for r in rows if r["score"] is not None],
                    key=lambda r: -r["score"])
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    for prof in ("urban", "rural"):
        for i, r in enumerate(sorted([x for x in ranked if x["profile"] == prof],
                                     key=lambda r: -r["score"]), 1):
            r["rank_in_profile"] = i
    for r in rows:
        r.setdefault("rank", None)
        r.setdefault("rank_in_profile", None)
    return {"availability": avail, "ksi_weights": ksiw, "cvi_weights": cviw}


def gate_stats(rows, params):
    """How many kecamatan each gate passes on its own. On the defaults the
    VLR floor and the G1 exclusion barely overlap, because the kecamatan
    holding the subscribers are the ones that already have a gerai — a real
    finding about the territory, visible only if the gates are counted
    separately."""
    keys = ("g1", "vlr_min", "vlr_pct_min", "ms")

    def only(r, keep):
        p = dict(params)
        if keep != "g1":
            p["min_km_im3"] = p["min_km_3id"] = p["min_km_hybrid"] = 0
        if keep != "vlr_min":
            p["vlr_min"] = 0
        if keep != "vlr_pct_min":
            p["vlr_pct_min"] = 0
        if keep != "ms":
            p["ms_pct_min"] = p["ms_pct_max"] = 0
        return not apply_gates(r, p, r["vlr_pct"], r["ms_pct"])

    if params.get("dist_term"):
        out = [{"key": "G1", "active": False, "passes": len(rows),
                "label": "G1 · distance is a scored term, not a gate  "
                         "({:g} km \u2192 0, {:g} km \u2192 100, weight {:g})".format(
                             params["dist_zero_km"], params["dist_full_km"],
                             params["dist_weight"])}]
    else:
        out = None
    labels = {
        "g1": (("G1 · {} {:g} km from the nearest IM3 or 3Store (hybrid)"
                if params.get("hybrid_own", True)
                else "G1 · {} {:g} km from IM3 and from 3Store separately").format(
            "at least" if params["g1_mode"] == "min" else "at most",
            params["min_km_hybrid"] if params.get("hybrid_own", True)
            else params["min_km_im3"]),
               bool(params["min_km_hybrid"] if params.get("hybrid_own", True)
                    else (params["min_km_im3"] or params["min_km_3id"]))),
        "vlr_min": ("G2 · VLR >= {:,}".format(int(params["vlr_min"] or 0)),
                    bool(params["vlr_min"])),
        "vlr_pct_min": ("G2 · VLR percentile >= {:g}".format(
            params["vlr_pct_min"] or 0), bool(params["vlr_pct_min"])),
        "ms": ("G3 · {} share percentile {} {:g}".format(
            params["strategy"], ">=" if params["strategy"] == "DENSIFY" else "<=",
            params["ms_pct_min"] if params["strategy"] == "DENSIFY"
            else params["ms_pct_max"]), True),
    }
    if out is None:
        out = []
    else:
        keys = tuple(k for k in keys if k != "g1")
    for k in keys:
        label, active = labels[k]
        out.append({"key": k, "label": label + ("" if active else "  (off)"),
                    "active": active,
                    "passes": len(rows) if not active
                              else sum(1 for r in rows if only(r, k))})
    out.append({"key": "G4", "label": "G4 · coverage quality  (no data — skipped)",
                "active": False, "passes": len(rows)})
    return out


def model_label(params, ksiw):
    """The version string must name the model that actually ran.

    A local variant reported as 'KSI v2.0' is the same failure as a derived
    number reported as a measured one."""
    if "distance" in (ksiw or {}):
        return (f"{MODEL_VERSION}  + LOCAL distance term "
                f"(w={params['dist_weight']:g}, {params['dist_zero_km']:g}"
                f"\u2013{params['dist_full_km']:g} km, G1 not gating)")
    return MODEL_VERSION


def rank(con, overrides=None):
    params = merge_params(overrides)
    rows = load_kecamatan(con, params)
    info = score_rows(rows, params)
    rows.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0)))
    _, coords = load_service_points(con, params["own_includes_ipp"])
    return {"ok": True, "model": model_label(params, info["ksi_weights"]),
            "model_spec": MODEL_VERSION, "doc": DOC_PATH,
            "pop_source": "ref_kelurahan.population, summed to kecamatan (derived)",
            "params": params, "strategies": list(STRATEGIES),
            "term_labels": TERM_LABELS,
            "ksi_weights_spec": KSI_WEIGHTS, "cvi_weights_spec": CVI_WEIGHTS,
            "ksi_weights": {k: round(v, 4) for k, v in info["ksi_weights"].items()},
            "cvi_weights": {k: round(v, 4) for k, v in info["cvi_weights"].items()},
            "availability": info["availability"],
            "profiles": {"urban": sorted(URBAN_KABKOT), "rural": sorted(RURAL_KABKOT)},
            "n_total": len(rows),
            "n_eligible": sum(1 for r in rows if r["score"] is not None),
            "n_urban": sum(1 for r in rows if r["profile"] == "urban"),
            "n_rural": sum(1 for r in rows if r["profile"] == "rural"),
            "n_im3": len(coords["IM3"]), "n_3id": len(coords["3ID"]),
            "n_hybrid": len(coords["hybrid"]),
            "gate_stats": gate_stats(rows, params),
            "rows": rows}


# ── catchment simulation ─────────────────────────────────────────────────
# "If a service point went in the middle of this kecamatan, how many people
# would sit inside a 10 km ride of it?"
#
# Population is apportioned by the share of each desa's AREA that falls in
# the circle, not by whether its centroid does. A 24 km2 desa half inside the
# circle contributes half its people; the centroid test would contribute all
# or none of them, and at 10 km the ring crosses dozens of desa, so that
# rounding is the whole answer rather than a detail.
#
# The share is measured by sampling, on the same grid geom.py works in. It is
# a DERIVED figure: it assumes people are spread evenly inside a desa, which
# they are not. It is honest about ranking catchments against each other and
# should not be quoted as a census count.
CATCHMENT_STEP_M = 100.0


def catchment_population(con, lat, lon, radius_km, step_m=CATCHMENT_STEP_M):
    """-> dict: population inside `radius_km` of (lat, lon), area-apportioned."""
    import geom

    r_m = radius_km * 1000.0
    k = math.cos(math.radians(lat))
    dlat = radius_km / 110.574
    dlon = radius_km / (111.320 * max(k, 1e-6))

    pop_by_key = {}
    for r in con.execute("SELECT join_key, population FROM ref_kelurahan "
                         "WHERE population IS NOT NULL"):
        pop_by_key[r["join_key"]] = r["population"]

    def to_xy(la, lo):
        return (math.radians(lo - lon) * R_EARTH_KM * 1000.0 * k,
                math.radians(la - lat) * R_EARTH_KM * 1000.0)

    whole, partial = 0, 0
    total_pop = 0.0
    total_area = 0.0
    parts = []
    for f in geom.load_polys(con, "kelurahan"):
        blon0, blat0, blon1, blat1 = f["bbox"]
        if (blon1 < lon - dlon or blon0 > lon + dlon
                or blat1 < lat - dlat or blat0 > lat + dlat):
            continue
        rings = [[to_xy(p[1], p[0]) for p in ring]
                 for polys in f["polys"] for ring in polys]
        n_in, n_hit = _scan_rings(rings, step_m, r_m)
        if not n_in or not n_hit:
            continue
        share = n_hit / n_in
        pop = pop_by_key.get(f["join_key"])
        area = n_in * step_m * step_m / 1e6
        if pop is not None:
            total_pop += pop * share
        total_area += area * share
        whole += share >= 0.999
        partial += 0.0 < share < 0.999
        parts.append({"desa": (f["attrs"] or {}).get("KEL_DES") or f["name"],
                      "kecamatan": (f["attrs"] or {}).get("KEC"),
                      "share": round(share, 4),
                      "population": pop,
                      "population_in": round((pop or 0) * share)})
    parts.sort(key=lambda x: -x["population_in"])
    return {"lat": lat, "lon": lon, "radius_km": radius_km,
            "population": int(round(total_pop)),
            "area_km2": round(total_area, 1),
            "desa_touched": whole + partial,
            "desa_whole": whole, "desa_partial": partial,
            "derived": True,
            "method": ("area-apportioned from ref_kelurahan.population, "
                       f"{step_m:.0f} m sampling"),
            "parts": parts}


def _scan_rings(rings_xy, step, r_m):
    """(samples inside the polygon, samples also inside the circle).

    Even-odd scanline fill, so holes and multi-part desa come out right."""
    ys = [p[1] for r in rings_xy for p in r]
    if not ys:
        return 0, 0
    y0, y1 = min(ys), max(ys)
    r2 = r_m * r_m
    inside = hit = 0
    y = y0 + step / 2
    while y < y1:
        xs = []
        for ring in rings_xy:
            n = len(ring)
            for i in range(n):
                ax, ay = ring[i]
                bx, by = ring[(i + 1) % n]
                if (ay > y) != (by > y):
                    xs.append(ax + (y - ay) * (bx - ax) / (by - ay))
        xs.sort()
        for i in range(0, len(xs) - 1, 2):
            xa, xb = xs[i], xs[i + 1]
            x = math.floor(xa / step) * step + step / 2
            if x < xa:
                x += step
            while x < xb:
                inside += 1
                if x * x + y * y <= r2:
                    hit += 1
                x += step
        y += step
    return inside, hit


def kecamatan_catchment(con, kecamatan, radius_km=10.0, kabkot=None):
    """The same, centred on a named kecamatan's interior point."""
    import geom

    want = norm_key(kecamatan, kabkot) if kabkot else None
    for f in geom.load_polys(con, "kecamatan"):
        attrs = f["attrs"] or {}
        kec = attrs.get("KEC") or f["name"]
        kab = attrs.get("KABKOT") or ""
        if want:
            if norm_key(kec, kab) != want:
                continue
        elif (kec or "").strip().upper() != kecamatan.strip().upper():
            continue
        # An interior point, not the vertex mean: six of the 121 kecamatan
        # have a mean that lands outside their own boundary.
        pt = geom.representative_point(f["polys"])
        if not pt:
            continue
        out = catchment_population(con, pt[0], pt[1], radius_km)
        out["kecamatan"] = kec
        out["kabkot"] = kab
        out["centre"] = {"lat": round(pt[0], 6), "lon": round(pt[1], 6),
                         "kind": "representative point (inside the polygon)"}
        return out
    return None


# ── candidate sites — Stage-2 PROXY, not SAI ─────────────────────────────
# §2-§4 of the document define SAI over H3 cells with travel-time isochrones,
# day/night population, rent, betweenness centrality and a coverage-quality
# index. None of those exist in this database, so SAI is not implemented and
# must not be reported as implemented. What a candidate gets instead is:
#
#     site_score = 0.50 · KSI(containing kecamatan)      Stage-1, real
#                + 0.50 · CVI_local(catchment)           the CVI terms that
#                                                        do exist, measured
#                                                        in a radius around
#                                                        the point itself
#
# It is a screening aid for a shortlist, not a business case. The half that
# comes from CVI_local uses the same renormalised CVI weights as Stage 1, so
# a site and its kecamatan are at least measured with the same ruler.
PROXY_SPLIT = {"ksi": 0.50, "cvi_local": 0.50}


def score_candidates(con, sites, overrides=None):
    """`sites` = [{'location','kecamatan_input','lat','lon'}, ...]"""
    import geom

    params = merge_params(overrides)
    kec_rows = load_kecamatan(con, params)
    info = score_rows(kec_rows, params)
    cviw = info["cvi_weights"]
    by_key = {r["key"]: r for r in kec_rows}
    idx = load_poi_points(con, params["top_banks"])
    _, coords = load_service_points(con, params["own_includes_ipp"])
    feats = geom.load_polys(con, "kecamatan")
    grid = geom.build_index(feats)

    # Reference distribution: the identical catchment measured at every
    # kecamatan centroid of the same stratum. Ranking candidates only against
    # each other would make the best of a bad list look excellent.
    ref = {}
    for prof in ("urban", "rural"):
        radius = params["radius_urban_km" if prof == "urban" else "radius_rural_km"]
        acc = {"minimarket": [], "atm": [], "bank": []}
        for r in [x for x in kec_rows if x["profile"] == prof]:
            t = idx.within(r["lat"], r["lon"], radius)
            acc["minimarket"].append(t.get("minimarket", 0))
            acc["atm"].append(t.get("atm", 0))
            acc["bank"].append(t.get("bank", 0) + t.get("bank_top4", 0))
        ref[prof] = {k: sorted(v) for k, v in acc.items()}

    out = []
    for i, s in enumerate(sites, 1):
        lat, lon = s.get("lat"), s.get("lon")
        # A pair written the wrong way round is unambiguous here: latitude in
        # this AOI is about -6 and longitude about 107.
        if lat is not None and lon is not None and abs(lat) > 90 >= abs(lon):
            lat, lon = lon, lat
        rec = {"row_no": i, "location": s.get("location"),
               "kecamatan_input": s.get("kecamatan_input"),
               "lat": lat, "lon": lon}
        if lat is None or lon is None:
            rec.update({"error": "missing LAT/LONG", "score": None})
            out.append(rec)
            continue
        hit = geom.locate_indexed(grid, lat, lon)
        if not hit:
            rec.update({"error": "outside every kecamatan boundary",
                        "score": None, "profile": None})
            out.append(rec)
            continue
        attrs = hit["attrs"] or {}
        kec = attrs.get("KEC") or hit["name"]
        key = norm_key(kec, attrs.get("KABKOT") or "")
        krow = by_key.get(key, {})
        prof = krow.get("profile") or profile_of(attrs.get("KABKOT"))
        radius = params["radius_urban_km" if prof == "urban" else "radius_rural_km"]
        cat = idx.within(lat, lon, radius)
        n_mini = cat.get("minimarket", 0)
        n_atm = cat.get("atm", 0)
        n_bank = cat.get("bank", 0) + cat.get("bank_top4", 0)
        base = ref.get(prof, {"minimarket": [], "atm": [], "bank": []})
        p_mini = _pct_against(base["minimarket"], n_mini)
        p_atm = _pct_against(base["atm"], n_atm)
        p_bank = _pct_against(base["bank"], n_bank)
        finance = (FINANCE_WEIGHTS["atm_den"] * p_atm
                   + FINANCE_WEIGHTS["bank_den"] * p_bank)
        # pasar is unavailable, so cviw already excludes it and the remaining
        # weights are renormalised; reuse them rather than inventing a split.
        denom = cviw.get("finance", 0) + cviw.get("minimarket", 0)
        cvi_local = ((cviw.get("finance", 0) * finance
                      + cviw.get("minimarket", 0) * p_mini) / denom) if denom else 0.0
        ksi_kec = krow.get("score_stratum")
        rec.update({
            "kecamatan": kec, "kabkot": attrs.get("KABKOT"),
            "join_key": hit["join_key"], "key": key,
            "mc": krow.get("mc"), "profile": prof, "radius_km": radius,
            "kecamatan_matches_input": _same(s.get("kecamatan_input"), kec),
            "minimarket": n_mini, "atm": n_atm, "bank_all": n_bank,
            "bank_top4": cat.get("bank_top4", 0),
            "vlr": krow.get("vlr") or 0,
            "km_im3": _nearest(lat, lon, coords["IM3"]),
            "km_3id": _nearest(lat, lon, coords["3ID"]),
            "km_hybrid": _nearest(lat, lon, coords["hybrid"]),
            "kec_score": ksi_kec, "kec_rank": krow.get("rank"),
            "cvi_local": round(100 * cvi_local, 2),
            "kec_gate_fails": krow.get("gate_fails") or [],
        })
        rec["gate_fails"] = apply_gates(rec, params, krow.get("vlr_pct"),
                                        krow.get("ms_pct"))
        rec["eligible"] = (not rec["gate_fails"]) or not params.get("gates", True)
        rec["score"] = round(PROXY_SPLIT["ksi"] * (ksi_kec or 0)
                             + PROXY_SPLIT["cvi_local"] * 100 * cvi_local, 2)
        rec["parts"] = [
            {"term": "ksi_kecamatan", "label": "Stage-1 KSI of the kecamatan",
             "pct": ksi_kec, "weight": PROXY_SPLIT["ksi"],
             "points": round(PROXY_SPLIT["ksi"] * (ksi_kec or 0), 2)},
            {"term": "cvi_local", "label": f"CVI within {radius} km of the site",
             "pct": round(100 * cvi_local, 1), "weight": PROXY_SPLIT["cvi_local"],
             "points": round(PROXY_SPLIT["cvi_local"] * 100 * cvi_local, 2)},
        ]
        out.append(rec)

    ranked = sorted([r for r in out if r.get("score") is not None],
                    key=lambda r: -r["score"])
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    return {"ok": True, "model": MODEL_VERSION + " · Stage-2 proxy",
            "params": params, "rows": out, "n": len(out), "n_scored": len(ranked),
            "proxy_note": "Stage-2 SAI (§2-§4) is not implemented: it needs "
                          "travel-time isochrones, day/night population, rent "
                          "and a coverage-quality index, none of which are in "
                          "this database. This score is 50% the kecamatan's "
                          "Stage-1 KSI and 50% the CVI terms measured in the "
                          "catchment around the point."}


def _same(a, b):
    if not a or not b:
        return None
    return re.sub(r"[^A-Z0-9]", "", str(a).upper()) == \
        re.sub(r"[^A-Z0-9]", "", str(b).upper())


# ── Excel reader ─────────────────────────────────────────────────────────
HEADER_ALIASES = {
    "location": "location", "locations": "location", "lokasi": "location",
    "nama": "location", "name": "location", "site": "location",
    "kecamatan": "kecamatan_input", "kec": "kecamatan_input",
    "district": "kecamatan_input",
    "long": "lon", "lon": "lon", "longitude": "lon", "lng": "lon", "x": "lon",
    "lat": "lat", "latitude": "lat", "y": "lat",
}


def read_sites_xlsx(path):
    """LOCATIONS / KECAMATAN / LONG / LAT in any column order and any case.
    Matched by header name, not position — a spreadsheet that has been
    through three people has its columns somewhere else than specified."""
    from openpyxl import load_workbook

    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = ws.iter_rows(values_only=True)
    header = None
    for raw in rows:
        cells = [str(c).strip().lower() if c is not None else "" for c in raw]
        mapped = {HEADER_ALIASES[c]: i for i, c in enumerate(cells)
                  if c in HEADER_ALIASES}
        if "lat" in mapped and "lon" in mapped:
            header = mapped
            break
    if not header:
        raise ValueError("No header row with latitude and longitude columns "
                         "was found. Expected LOCATIONS, KECAMATAN, LONG, LAT.")

    def cell(raw, key):
        i = header.get(key)
        return raw[i] if i is not None and i < len(raw) else None

    def fnum(v):
        if v is None or v == "":
            return None
        try:
            return float(str(v).replace(",", ".").strip())
        except ValueError:
            return None

    out = []
    for raw in rows:
        if raw is None or not any(c is not None and str(c).strip() for c in raw):
            continue
        lat, lon = fnum(cell(raw, "lat")), fnum(cell(raw, "lon"))
        if lat is None or lon is None:
            continue
        loc, kec = cell(raw, "location"), cell(raw, "kecamatan_input")
        out.append({"location": str(loc).strip() if loc is not None else None,
                    "kecamatan_input": str(kec).strip() if kec is not None else None,
                    "lat": lat, "lon": lon})
    wb.close()
    return out


# ── persistence ──────────────────────────────────────────────────────────
CAND_COLS = ["row_no", "location", "kecamatan_input", "lat", "lon", "join_key",
             "kecamatan", "kabkot", "mc", "profile", "radius_km", "minimarket",
             "atm", "bank_top4", "bank_all", "vlr", "km_im3", "km_3id", "km_hybrid",
             "cvi_local", "score", "rank", "kec_score", "kec_rank", "error"]


def save_batch(con, batch_id, label, source_file, result):
    con.execute("DELETE FROM site_candidate WHERE batch_id=?", (batch_id,))
    con.execute("DELETE FROM site_batch WHERE batch_id=?", (batch_id,))
    con.execute(
        "INSERT INTO site_batch (batch_id,label,source_file,n_rows,n_scored,"
        "params_json,uploaded_utc) VALUES (?,?,?,?,?,?,?)",
        (batch_id, label, source_file, result["n"], result["n_scored"],
         json.dumps(result["params"], default=str), db.utcnow()))
    cols = ["batch_id"] + CAND_COLS + ["gate_fails_json", "parts_json"]
    ph = ",".join("?" * len(cols))
    con.executemany(
        f"INSERT INTO site_candidate ({','.join(cols)}) VALUES ({ph})",
        [tuple([batch_id] + [r.get(c) for c in CAND_COLS]
               + [json.dumps(r.get("gate_fails") or []),
                  json.dumps(r.get("parts") or [])]) for r in result["rows"]])
    con.commit()
    return batch_id


def load_batch(con, batch_id):
    head = con.execute("SELECT * FROM site_batch WHERE batch_id=?",
                       (batch_id,)).fetchone()
    if not head:
        return None
    rows = []
    for r in con.execute("SELECT * FROM site_candidate WHERE batch_id=? "
                         "ORDER BY rank IS NULL, rank, row_no", (batch_id,)):
        d = dict(r)
        d["gate_fails"] = json.loads(d.pop("gate_fails_json") or "[]")
        d["parts"] = json.loads(d.pop("parts_json") or "[]")
        rows.append(d)
    return {"ok": True, "batch": dict(head), "model": MODEL_VERSION,
            "params": json.loads(head["params_json"] or "{}"), "rows": rows}


def list_batches(con):
    try:
        return [dict(r) for r in con.execute(
            "SELECT batch_id,label,source_file,n_rows,n_scored,uploaded_utc "
            "FROM site_batch ORDER BY uploaded_utc DESC")]
    except sqlite3.OperationalError:
        return []


def delete_batch(con, batch_id):
    con.execute("DELETE FROM site_candidate WHERE batch_id=?", (batch_id,))
    con.execute("DELETE FROM site_batch WHERE batch_id=?", (batch_id,))
    con.commit()


# ── CLI ──────────────────────────────────────────────────────────────────
def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(description="Future GAPURA — KSI v2.0")
    ap.add_argument("--strategy", choices=["DENSIFY", "ATTACK"])
    ap.add_argument("--g1-mode", choices=["min", "max"])
    ap.add_argument("--vlr-min", type=float)
    ap.add_argument("--vlr-pct-min", type=float)
    ap.add_argument("--min-km-hybrid", type=float)
    ap.add_argument("--min-km-im3", type=float)
    ap.add_argument("--min-km-3id", type=float)
    ap.add_argument("--split-brands", action="store_true",
                    help="gate IM3 and 3Store separately instead of as one "
                         "interchangeable network")
    ap.add_argument("--scope", choices=["stratum", "territory"])
    ap.add_argument("--top-n", type=int, default=25)
    ap.add_argument("--no-gates", action="store_true")
    ap.add_argument("--xlsx")
    a = ap.parse_args(argv)
    over = {k: v for k, v in
            {"strategy": a.strategy, "g1_mode": a.g1_mode, "vlr_min": a.vlr_min,
             "vlr_pct_min": a.vlr_pct_min, "min_km_im3": a.min_km_im3,
             "min_km_3id": a.min_km_3id, "scope": a.scope,
             "min_km_hybrid": a.min_km_hybrid}.items() if v is not None}
    if a.no_gates:
        over["gates"] = False
    if a.split_brands:
        over["hybrid_own"] = False

    con = db.connect()
    try:
        if a.xlsx:
            res = score_candidates(con, read_sites_xlsx(a.xlsx), over)
            print(res["proxy_note"] + "\n")
            for r in sorted(res["rows"], key=lambda x: (x.get("rank") or 9999)):
                print(f"{str(r.get('rank') or '-'):>3}  "
                      f"{(r.get('score') if r.get('score') is not None else 0):>6.2f}  "
                      f"{(r.get('location') or '')[:30]:<30} "
                      f"{(r.get('kecamatan') or r.get('error') or ''):<20} "
                      f"{r.get('profile') or ''}")
            return 0
        res = rank(con, over)
        print(f"{res['model']}  ·  scope={res['params']['scope']}  "
              f"strategy={res['params']['strategy']}  g1={res['params']['g1_mode']}")
        drop = [k for k, v in res["availability"].items()
                if not k.startswith("_") and not v["ok"]]
        if drop:
            print("UNAVAILABLE terms, weight redistributed: " + ", ".join(drop))
        print("effective KSI weights: " + ", ".join(
            f"{k}={v:.3f}" for k, v in res["ksi_weights"].items()))
        for g in res["gate_stats"]:
            print(f"   {g['label']:<52} {g['passes']:>4}")
        print(f"   {'PASS ALL GATES':<52} {res['n_eligible']:>4} "
              f"of {res['n_total']}\n")
        print(f"{'#':>3} {'KSI':>6} {'kecamatan':<20} {'kabkot':<15} {'prof':<5} "
              f"{'vlr%':>5} {'ms%':>5} {'CVI':>5} {'CPI':>5} {'NET':>5} {'terr':>6}")
        for r in res["rows"][:a.top_n]:
            if r["score"] is None:
                continue
            t = r["terms"]
            print(f"{r['rank']:>3} {r['score']:>6.2f} {r['kecamatan'][:20]:<20} "
                  f"{(r['kabkot'] or '')[:15]:<15} {r['profile']:<5} "
                  f"{r['vlr_pct']:>5.0f} "
                  f"{(r['ms_pct'] if r['ms_pct'] is not None else 0):>5.0f} "
                  f"{t.get('cvi', 0):>5.0f} {t.get('cpi', 0):>5.0f} "
                  f"{t.get('network', 0):>5.0f} {r['score_territory']:>6.2f}")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_main(sys.argv[1:]))
