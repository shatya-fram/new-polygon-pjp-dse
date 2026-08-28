#!/usr/bin/env python3
"""
Re-map kecamatan and desa onto the current Indosat microcluster polygons.

    python remap_mc.py --report     measure, write nothing
    python remap_mc.py              write mc36 / branch onto the ref tables

WHY THIS EXISTS
    The MC boundaries are re-cut periodically -- 24 microclusters became 36
    when MC-CIKARANG was split into MC-CIBARUSAH and MC-CIKARANG BARAT. The
    performance data does not follow: `ref_kecamatan.mc` still carries the
    generation that shipped with the kecamatan profile spreadsheet, so after
    a boundary import the map draws 36 shapes and 18 of them can find no
    figures to show. Replacing polygons without re-deriving the mapping is
    how an MC silently goes blank.

HOW THE MAPPING IS DECIDED
    Point-in-polygon at DESA level, then rolled up to kecamatan by a
    population-weighted majority -- not by the kecamatan's own centroid.
    Desa are an order of magnitude finer, so a kecamatan lying across an MC
    seam is decided by where most of its people are rather than by whichever
    side of the line its geometric centre happened to fall. On this data the
    two methods differ for exactly one kecamatan (LIMO, Kota Depok), and the
    desa-weighted answer is the one that agrees with Indosat's own desa
    reference.

    The result is checked against `ref_kelurahan.mc35`, the mapping Indosat
    ships in the desa export. Agreement everywhere except the kecamatan
    affected by a known split is the evidence that the geometry is right;
    the run prints that comparison rather than asking you to take it on
    trust.

    `mc` is never overwritten. The derived value lands in `mc36`, so the
    generation that came with the spreadsheet stays readable beside it.
"""
import argparse
import collections
import json
import sys

import db
import geom

LAYER = "indosat_mc"


def log(m):
    print(f"  {m}", flush=True)


def load_polys(con):
    polys = geom.load_polys(con, LAYER)
    if not polys:
        sys.exit(f"No '{LAYER}' polygons in the database. Import the MC KMZ "
                 f"first:  python import_local.py --kml 'Data Upload/<file>.kmz'")
    return polys, geom.build_index(polys)


def branch_of(polys):
    """Branch and sales area per microcluster, straight off the polygons.

    BRANCH12 is the current 12-branch cut. BRANCH11 lumped all of Kota
    Bekasi under a single branch called "BEKASI"; BRANCH12 splits it into
    NORTH BEKASI and SOUTH BEKASI, which is why a Kota Bekasi microcluster
    had no sales-area territory of its own before. Newer field first, older
    ones as a fallback so an older export still imports."""
    out = {}
    for f in polys:
        a = {k.upper(): v for k, v in (f["attrs"] or {}).items()}
        out[f["feature_key"]] = {
            "branch": (a.get("BRANCH12") or a.get("BRANCH11")
                       or a.get("BRANCH") or "").strip() or None,
            "area": (a.get("AREA_1") or a.get("AREA") or "").strip() or None,
            "partner": (a.get("PARTNER_SE") or a.get("PARTNER")
                        or "").strip() or None,
        }
    return out


def sync_desa(con):
    """Copy the territory fields off the desa polygons onto ref_kelurahan.

    The desa export is the authority for which microcluster and branch a
    desa belongs to -- it is the file the business maintains. Reading them
    from the polygons rather than a second spreadsheet keeps the map and the
    table describing the same cut."""
    cols = {r[1] for r in con.execute("PRAGMA table_info(ref_kelurahan)")}
    for c in ("mc36", "branch12", "area12", "partner12"):
        if c not in cols:
            con.execute(f"ALTER TABLE ref_kelurahan ADD COLUMN {c} TEXT")
            log(f"added ref_kelurahan.{c}")
    n = 0
    for r in con.execute("SELECT join_key, attrs_json FROM geo_feature "
                         "WHERE layer_key='kelurahan'"):
        a = {k.upper(): v for k, v in json.loads(r["attrs_json"] or "{}").items()}
        mc = (a.get("MC36") or "").strip() or None
        if not mc:
            continue
        n += con.execute(
            "UPDATE ref_kelurahan SET mc36=?, branch12=?, area12=?, partner12=? "
            "WHERE join_key=?",
            (mc, (a.get("BRANCH12") or "").strip() or None,
             (a.get("AREA_1") or "").strip() or None,
             (a.get("PARTNER_SE") or "").strip() or None, r["join_key"])).rowcount
    log(f"synced {n} desa from the boundary attributes")
    return n


def assign(con, index):
    """-> (desa rows, kecamatan majority, how many desa fell outside)."""
    desa, per_kec, outside = [], collections.defaultdict(collections.Counter), 0
    for r in con.execute(
            "SELECT kel_key, trim(upper(kecamatan)) kec, trim(upper(kabkot)) kab, "
            "lat, lon, coalesce(population,0) pop, trim(upper(mc35)) m35 "
            "FROM ref_kelurahan"):
        if r["lat"] is None or r["lon"] is None:
            outside += 1
            continue
        hit = geom.locate_indexed(index, r["lat"], r["lon"])
        if not hit:
            outside += 1
            continue
        mc = hit["feature_key"]
        desa.append((r["kel_key"], mc))
        # +1 so a desa with no recorded population still carries a vote.
        per_kec[(r["kec"], r["kab"])][mc] += (r["pop"] or 0) + 1
    return desa, {k: v.most_common(1)[0][0] for k, v in per_kec.items()}, outside


def cross_check(con, kec_map, col):
    """Compare with the mapping Indosat ships in the desa export.

    `col` is whichever generation the desa reference is on. Checking the
    polygons against the CURRENT desa export should agree everywhere -- they
    are two views of one cut, and a disagreement means one of the files is
    stale. Checking against an OLDER one is still worth printing, because
    the differences show exactly which territories were re-drawn."""
    ref = collections.defaultdict(collections.Counter)
    for r in con.execute(
            f"SELECT trim(upper(kecamatan)) kec, trim(upper(kabkot)) kab, "
            f"trim(upper({col})) m FROM ref_kelurahan WHERE {col} IS NOT NULL"):
        ref[(r["kec"], r["kab"])][r["m"]] += 1
    ref1 = {k: v.most_common(1)[0][0] for k, v in ref.items()}
    both = set(kec_map) & set(ref1)
    differ = sorted(k for k in both if kec_map[k] != ref1[k])
    return len(both), differ, ref1


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--report", action="store_true",
                    help="measure and compare, write nothing")
    args = ap.parse_args()

    con = db.connect()
    polys, index = load_polys(con)
    log(f"{len(polys)} microcluster polygons in layer '{LAYER}'")

    desa, kec_map, outside = assign(con, index)
    log(f"{len(desa):,} desa placed inside a microcluster"
        + (f", {outside} outside" if outside else ", none outside"))
    log(f"{len(kec_map)} kecamatan resolved by population-weighted desa majority")

    cols = {r[1] for r in con.execute("PRAGMA table_info(ref_kelurahan)")}
    for col in ("mc36", "mc35"):
        if col not in cols:
            continue
        if not con.execute(f"SELECT 1 FROM ref_kelurahan WHERE {col} "
                           f"IS NOT NULL LIMIT 1").fetchone():
            continue
        n_both, differ, ref1 = cross_check(con, kec_map, col)
        log(f"cross-check against ref_kelurahan.{col}: "
            f"{n_both - len(differ)}/{n_both} agree")
        for k in differ:
            log(f"    {k[0][:22]:<22} {k[1][:14]:<14} "
                f"polygons={kec_map[k]:<20} desa-ref={ref1[k]}")
        if differ:
            log("    (differences are territories re-drawn since that "
                "generation of the desa export)")

    if args.report:
        con.close()
        return 0

    # ── write ────────────────────────────────────────────────────────────
    sync_desa(con)
    for table, col in (("ref_kecamatan", "mc36"), ("ref_kecamatan", "mc36_branch"),
                       ("ref_kecamatan", "mc36_area")):
        cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        if col not in cols:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
            log(f"added {table}.{col}")

    branches = branch_of(polys)
    n_kec = 0
    for (kec, kab), mc in kec_map.items():
        cur = con.execute(
            "UPDATE ref_kecamatan SET mc36=?, mc36_branch=?, mc36_area=? "
            "WHERE trim(upper(kecamatan))=? AND trim(upper(kabkot))=?",
            (mc, (branches.get(mc) or {}).get("branch"),
             (branches.get(mc) or {}).get("area"), kec, kab))
        n_kec += cur.rowcount
    n_desa = 0
    for kel_key, mc in desa:
        n_desa += con.execute("UPDATE ref_kelurahan SET mc36=? WHERE kel_key=?",
                              (mc, kel_key)).rowcount

    # The new microclusters are in no hierarchy spreadsheet yet, so the KMZ
    # is the only record of their branch. Seed ref_mc from it, without
    # touching the rows the spreadsheet already owns.
    n_mc, n_fix = 0, 0
    for f in polys:
        mc = f["feature_key"]
        exists = con.execute(
            "SELECT mc, branch FROM ref_mc WHERE trim(upper(mc))=?",
            (mc,)).fetchone()
        if exists:
            # The polygons are the current authority for the branch of a
            # microcluster inside this territory. A row still carrying the
            # 11-branch value would keep reporting Kota Bekasi as one
            # undivided "BEKASI".
            want = (branches.get(mc) or {}).get("branch")
            if want and (exists["branch"] or "").strip().upper() != want.upper():
                log(f"    ref_mc {mc}: branch {exists['branch']!r} -> {want!r}")
                con.execute("UPDATE ref_mc SET branch=?, area=? WHERE mc=?",
                            (want, (branches.get(mc) or {}).get("area"),
                             exists["mc"]))
                n_fix += 1
            continue
        b = branches.get(mc) or {}
        con.execute("INSERT INTO ref_mc (mc, branch, area, source_file, "
                    "imported_utc) VALUES (?,?,?,?,?)",
                    (mc, b.get("branch"), b.get("area"),
                     "mc36 polygons", db.utcnow()))
        n_mc += 1
    con.commit()
    log(f"wrote mc36 on {n_kec} kecamatan and {n_desa:,} desa; "
        f"ref_mc: {n_mc} added, {n_fix} branch corrections")

    unmapped = [r[0] for r in con.execute(
        "SELECT kec_key FROM ref_kecamatan WHERE mc36 IS NULL")]
    if unmapped:
        log(f"WARNING {len(unmapped)} kecamatan still have no mc36: "
            f"{', '.join(unmapped[:6])}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
