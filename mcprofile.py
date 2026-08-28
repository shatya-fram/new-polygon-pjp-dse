#!/usr/bin/env python3
"""Who works a microcluster, and what their patch looks like.

    python mcprofile.py "MC-JAKARTA PUSAT"

WHY THE KECAMATAN JOIN AND NOT THE OUTLET'S OWN COLUMN
    Every outlet row carries a `Micro_Clus`, and it agrees with the
    kecamatan-to-MC mapping 37,667 times and disagrees 35,972 times -- but
    almost every disagreement is a naming convention, not a fact:
    "CS CIREBON TIMUR" against "MC-CIREBON TIMUR". Rather than guess at a
    prefix rule that would quietly mis-file the cases where the two really
    do differ, membership is resolved the same way the territory map
    resolves it -- kecamatan -> MC through ref_kecamatan -- so the list of
    DSE in a microcluster and the polygons drawn for that microcluster can
    never disagree with each other.

    The outlet's own label is still returned, as `mc_label`, so a row that
    is filed differently in the source is visible rather than overridden.
"""
import collections
import json
import sys

import config
import db

OUTLET_LAYER = "outlet_dse"


def _hier(con):
    """(kecamatan, kabupaten) -> (mc, branch, area, region)."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(ref_kecamatan)")}
    mc = "mc36" if "mc36" in cols else "mc"
    br = ("mc36_branch" if "mc36_branch" in cols
          else ("sa" if "sa" in cols else "branch"))
    ar = "mc36_area" if "mc36_area" in cols else "area"
    hier, kab_of = {}, {}
    for r in con.execute(
            "SELECT upper(trim(kecamatan)) k, upper(trim(kabkot)) kb, "
            f"{mc} mc, {br} br, {ar} ar, region FROM ref_kecamatan"):
        row = (r["mc"], r["br"], r["ar"], r["region"])
        hier[(r["k"], r["kb"])] = row
        hier.setdefault((r["k"], ""), row)
        kab_of.setdefault(r["k"], r["kb"])
    return hier, kab_of


def _blank():
    return {"outlets": 0, "desa": set(), "kecamatan": set(),
            "brands": collections.Counter(),
            "categories": collections.Counter(),
            "partners": collections.Counter(),
            "hybrid": collections.Counter(),
            "minlat": None, "minlon": None, "maxlat": None, "maxlon": None,
            "mc_label": collections.Counter(), "msisdn": None}


def _grow(box, lat, lon):
    if lat is None or lon is None:
        return
    box["minlat"] = lat if box["minlat"] is None else min(box["minlat"], lat)
    box["maxlat"] = lat if box["maxlat"] is None else max(box["maxlat"], lat)
    box["minlon"] = lon if box["minlon"] is None else min(box["minlon"], lon)
    box["maxlon"] = lon if box["maxlon"] is None else max(box["maxlon"], lon)


def rosters(con):
    """{MC: {"dse": {code: profile}, ...}} for every microcluster.

    One pass over the outlet layer -- 73,659 rows in about a third of a
    second, which is cheap enough that it is not worth a cache that could
    go stale against a re-upload."""
    hier, _kab = _hier(con)
    out = collections.defaultdict(
        lambda: {"dse": collections.defaultdict(_blank),
                 "branch": None, "area": None, "region": None,
                 "kecamatan": set(), "outlets": 0, "unplaced": 0})

    for r in con.execute(
            "SELECT attrs_json, centroid_lat, centroid_lon FROM geo_feature "
            "WHERE layer_key = ?", (OUTLET_LAYER,)):
        a = json.loads(r["attrs_json"] or "{}")
        lat, lon = r["centroid_lat"], r["centroid_lon"]
        if not config.on_map(lat, lon):
            continue
        kec = (a.get("Kecamatan") or "").strip().upper()
        kab = (a.get("Kabupaten") or "").strip().upper()
        h = hier.get((kec, kab)) or hier.get((kec, ""))
        if not h or not h[0]:
            continue
        mc = h[0].strip()
        code = str(a.get("DSE_CODE") or "").strip()
        m = out[mc]
        m["branch"], m["area"], m["region"] = h[1], h[2], h[3]
        m["kecamatan"].add(kec)
        m["outlets"] += 1
        if not code:
            m["unplaced"] += 1
            continue
        d = m["dse"][code]
        d["outlets"] += 1
        if a.get("Desa_Name"):
            # Keyed on desa AND kecamatan. Desa names repeat constantly --
            # there are several MEKARSARI and several SUKAMAJU inside this
            # region alone -- so counting on the name by itself would merge
            # unrelated villages and inflate every figure derived from it.
            d["desa"].add((str(a["Desa_Name"]).strip().upper(), kec))
        d["kecamatan"].add(kec)
        d["brands"][a.get("Brand_Name") or "?"] += 1
        d["categories"][a.get("Outlet_Cat") or "?"] += 1
        if a.get("Partner_Te"):
            d["partners"][str(a["Partner_Te"]).strip()] += 1
        d["hybrid"][a.get("Hybrid_Non") or "?"] += 1
        if a.get("Micro_Clus"):
            d["mc_label"][str(a["Micro_Clus"]).strip()] += 1
        if not d["msisdn"] and a.get("DSE_MISISD"):
            d["msisdn"] = str(a["DSE_MISISD"]).strip()
        _grow(d, lat, lon)
    return out


def mc_bounds(con, mc):
    """The microcluster's own polygon envelope, for the zoom.

    Read from the territory layer rather than from where its outlets happen
    to be: a rep's patch can sit in one corner of a microcluster, and zooming
    to the outlets would frame the corner and call it the MC."""
    key = str(mc).strip().upper()
    lo_lat = lo_lon = hi_lat = hi_lon = None
    for r in con.execute(
            "SELECT attrs_json, minlon, minlat, maxlon, maxlat FROM geo_feature "
            "WHERE layer_key = 'indosat_mc'"):
        a = json.loads(r["attrs_json"] or "{}")
        name = (a.get("MC IOH") or a.get("MC36") or a.get("MC") or "")
        if str(name).strip().upper() != key:
            continue
        if r["minlon"] is None:
            continue
        lo_lat = r["minlat"] if lo_lat is None else min(lo_lat, r["minlat"])
        lo_lon = r["minlon"] if lo_lon is None else min(lo_lon, r["minlon"])
        hi_lat = r["maxlat"] if hi_lat is None else max(hi_lat, r["maxlat"])
        hi_lon = r["maxlon"] if hi_lon is None else max(hi_lon, r["maxlon"])
    if lo_lat is None:
        return None
    return [[lo_lat, lo_lon], [hi_lat, hi_lon]]


def _top(counter, n=3):
    return [{"value": k, "count": v} for k, v in counter.most_common(n)]


def profile(con, mc, sites_per_desa=None):
    """One microcluster, ready to render. -> dict or None."""
    key = str(mc).strip().upper()
    all_mc = rosters(con)
    match = next((k for k in all_mc if k.strip().upper() == key), None)
    if match is None:
        return None
    m = all_mc[match]

    # Population and desa count come from the reference table, so the MC
    # header reports the whole microcluster and not merely the parts of it
    # a rep has been given outlets in.
    pop = desa_n = 0
    kec_set = m["kecamatan"]
    for r in con.execute(
            "SELECT upper(trim(kecamatan)) k, coalesce(population,0) p "
            "FROM ref_kelurahan"):
        if r["k"] in kec_set:
            desa_n += 1
            pop += r["p"] or 0

    rows = []
    total = max(1, sum(d["outlets"] for d in m["dse"].values()))
    for code, d in m["dse"].items():
        b = ([[d["minlat"], d["minlon"]], [d["maxlat"], d["maxlon"]]]
             if d["minlat"] is not None else None)
        label = d["mc_label"].most_common(1)
        rows.append({
            "dse": code,
            "msisdn": d["msisdn"],
            "outlets": d["outlets"],
            "share": round(d["outlets"] * 100.0 / total, 1),
            "desa": len(d["desa"]),
            "kecamatan": len(d["kecamatan"]),
            "kecamatan_names": sorted(d["kecamatan"])[:6],
            "sites": (sum(sites_per_desa.get(x, 0) for x in d["desa"])
                      if sites_per_desa else None),
            "brands": _top(d["brands"]),
            "categories": _top(d["categories"]),
            "partner": (d["partners"].most_common(1)[0][0]
                        if d["partners"] else None),
            "hybrid": (d["hybrid"].most_common(1)[0][0]
                       if d["hybrid"] else None),
            "mc_label": label[0][0] if label else None,
            "outlets_per_desa": (round(d["outlets"] / len(d["desa"]), 1)
                                 if d["desa"] else None),
            "bounds": b,
        })
    rows.sort(key=lambda r: -r["outlets"])
    return {
        "mc": match, "branch": m["branch"], "area": m["area"],
        "region": m["region"],
        "kecamatan": sorted(kec_set),
        "totals": {"dse": len(rows), "outlets": m["outlets"],
                   "unassigned": m["unplaced"], "kecamatan": len(kec_set),
                   "desa": desa_n, "population": int(pop)},
        "dse": rows,
    }


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    con = db.connect()
    d = profile(con, sys.argv[1])
    con.close()
    if not d:
        print("no such microcluster")
        return 1
    t = d["totals"]
    print(f"{d['mc']} - {d['branch']} > {d['area']} > {d['region']}")
    print(f"  {t['dse']} DSE - {t['outlets']:,} outlets - "
          f"{t['kecamatan']} kecamatan - {t['desa']:,} desa - "
          f"{t['population']:,} people")
    print(f"  {'DSE':<20}{'outlets':>8}{'share':>7}{'desa':>6}  brand / partner")
    for r in d["dse"]:
        brand = ", ".join(b["value"] for b in r["brands"])
        print(f"  {r['dse']:<20}{r['outlets']:>8}{r['share']:>6}%"
              f"{r['desa']:>6}  {brand} - {r['partner'] or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
