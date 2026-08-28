#!/usr/bin/env python3
"""
Tag every point with the territory it actually falls in.

    python enrich_territory.py --report      # measure, write nothing
    python enrich_territory.py               # write adm_* and mc_ioh
    python enrich_territory.py --table poi_google

Point-in-polygon against the boundaries you uploaded, so a row is assigned
to a kecamatan and an Indosat MC because its coordinates are inside them --
not because a string matched.

This is also the honest answer to "why is Purwakarta in my results". Search
cells are rectangles drawn around irregular polygons, so they always spill
into neighbouring territory. Rather than throw those rows away at fetch
time, they are kept and left with a NULL kecamatan: `adm_kecamatan IS NULL`
becomes a precise, queryable definition of "outside my patch".
"""
import argparse
import sys
import time

import config
import db
import geom

TABLES = ["poi_overture", "poi_google", "poi_osm", "ref_service_point"]


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", action="append", choices=TABLES)
    ap.add_argument("--report", action="store_true",
                    help="measure coverage, write nothing")
    args = ap.parse_args()

    if not db.db_exists():
        sys.exit("No database — run migrate.py first.")
    con = db.connect()

    kec = geom.load_polys(con, "kecamatan")
    mc = geom.load_polys(con, "indosat_mc")
    if not kec and not mc:
        sys.exit("No boundaries loaded. Run `python import_local.py --all` "
                 "first — this script has nothing to test points against.")
    log(f"boundaries: {len(kec)} kecamatan · {len(mc)} Indosat MC")
    kec_ix = geom.build_index(kec)
    mc_ix = geom.build_index(mc)

    run_id = None if args.report else db.start_run(
        con, "enrich", {"tables": args.table or TABLES})

    grand_in = grand_out = 0
    for table in (args.table or TABLES):
        cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
        if not cols:
            continue
        rows = list(con.execute(
            f"SELECT {'source_id' if 'source_id' in cols else 'sp_key'} AS k,"
            f" lat, lon FROM {table} WHERE lat IS NOT NULL"))
        if not rows:
            log(f"  {table:<20} empty — skipped")
            continue

        t0 = time.time()
        updates, inside, outside, no_mc = [], 0, 0, 0
        for r in rows:
            k = geom.locate_indexed(kec_ix, r["lat"], r["lon"])
            m = geom.locate_indexed(mc_ix, r["lat"], r["lon"])
            if k:
                inside += 1
            else:
                outside += 1
            if not m:
                no_mc += 1
            a = k["attrs"] if k else {}
            updates.append((
                (k["name"] if k else None),
                (a.get("KABKOT") if k else None),
                (m["feature_key"] if m else None),
                "geo_feature:kecamatan+indosat_mc",
                r["k"]))

        pct = inside / len(rows) * 100
        log(f"  {table:<20}{len(rows):>7,} pts   in-territory {inside:>7,} "
            f"({pct:5.1f}%)   outside {outside:>6,}   no MC {no_mc:>6,}"
            f"   {time.time()-t0:.1f}s")
        grand_in += inside
        grand_out += outside

        if not args.report:
            key = "source_id" if "source_id" in cols else "sp_key"
            src = "adm_source" if "adm_source" in cols else None
            sql = (f"UPDATE {table} SET adm_kecamatan=?, adm_kota=?, "
                   f"mc_ioh=?" + (", adm_source=?" if src else ", ?=?")
                   + f" WHERE {key}=?")
            if src:
                con.executemany(
                    f"UPDATE {table} SET adm_kecamatan=?, adm_kota=?, "
                    f"mc_ioh=?, adm_source=? WHERE {key}=?", updates)
            else:
                con.executemany(
                    f"UPDATE {table} SET adm_kecamatan=?, adm_kota=?, "
                    f"mc_ioh=? WHERE {key}=?",
                    [(u[0], u[1], u[2], u[4]) for u in updates])
            con.commit()

    total = grand_in + grand_out
    print(f"\n  {grand_in:,} of {total:,} points sit inside a kecamatan "
          f"you loaded ({grand_in/total*100:.1f}%)" if total else "  nothing to do")
    if grand_out:
        print(f"  {grand_out:,} fall outside. That is expected, not a fault: "
              f"the AOI rectangle\n  and the search cells both overrun the "
              f"territory boundary. Filter them with\n"
              f"    WHERE adm_kecamatan IS NOT NULL")
    if args.report:
        print("\n  --report: nothing written.")
    else:
        db.finish_run(con, run_id, "ok", rows_written=total, api_calls=0,
                      est_cost_usd=0.0,
                      notes=f"inside={grand_in} outside={grand_out}")
        print("\n  Written to adm_kecamatan, adm_kota, mc_ioh.")
        print("  Join ref_mc on mc_ioh for branch / area / region, or "
              "ref_metric for VLR and revenue.")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
