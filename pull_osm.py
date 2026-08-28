#!/usr/bin/env python3
"""
CONNECTION 3 — OpenStreetMap via Overpass.

Same shape as the other two connections: its own targets, its own table,
its own pull_run rows. It reads nothing from poi_overture or poi_google and
writes nothing to them.

    python pull_osm.py --list
    python pull_osm.py --all --dry-run        # prints the QL, calls nothing
    python pull_osm.py --target rail_stations
    python pull_osm.py --all

Free and keyless. The only budget is fair use on a volunteer-run endpoint,
so targets are queried one at a time with a pause between them rather than
as one enormous union. See providers/osm_overpass.py for the licence note —
OSM is ODbL, which is share-alike, unlike Overture's CDLA-Permissive.
"""
import argparse
import sys
import time

import config
import db
from providers import osm_overpass as osm

# target -> tag selectors. Each selector is one Overpass filter expression,
# applied to nodes, ways and relations alike. Selectors within a target are
# unioned, so overlap between them is harmless — the upsert dedupes on the
# OSM type/id key.
#
# These are tag *facts*, not name guesses. That is the whole reason to add
# OSM alongside Overture: a station is railway=station because a mapper
# said so, not because its name happened to start with "Stasiun".
TARGETS = {
    "rail_stations": {
        "label": "Rail: KRL / MRT / LRT / main stations",
        "selectors": [
            '["railway"="station"]',
            '["railway"="halt"]',                    # smaller KRL stops
            '["station"="subway"]',
            '["station"="light_rail"]',
            '["public_transport"="station"]["train"="yes"]',
            '["public_transport"="station"]["subway"="yes"]',
            '["public_transport"="station"]["light_rail"="yes"]',
        ],
        "note": "the gap Overture cannot close — MRT/LRT are thin there",
    },
    "bus_stops": {
        "label": "Halte / bus stops (incl TransJakarta)",
        "selectors": [
            '["highway"="bus_stop"]',
            '["public_transport"="platform"]["bus"="yes"]',
        ],
        "note": "Overture has no bus_stop category at all",
    },
    "bus_terminals": {
        "label": "Bus terminals",
        "selectors": [
            '["amenity"="bus_station"]',
            '["public_transport"="station"]["bus"="yes"]',
        ],
        "note": "",
    },
    "bank_branches": {
        "label": "Bank branches (all banks, brand labelled)",
        "selectors": ['["amenity"="bank"]'],
        "note": "amenity=bank is unambiguous — no name-veto list needed",
    },
    "atms": {
        "label": "ATMs (all banks, brand labelled)",
        "selectors": ['["amenity"="atm"]', '["amenity"="bank"]["atm"="yes"]'],
        "note": "",
    },
    "minimarkets": {
        "label": "Minimarkets & supermarkets",
        "selectors": ['["shop"="convenience"]', '["shop"="supermarket"]'],
        "note": "brand:wikidata is well filled here — better than Overture",
    },
    "fuel_stations": {
        "label": "SPBU / fuel stations",
        "selectors": ['["amenity"="fuel"]'],
        "note": "",
    },
    "marketplaces": {
        "label": "Pasar / traditional marketplaces",
        "selectors": ['["amenity"="marketplace"]', '["shop"="marketplace"]'],
        "note": "the CVI pasar term needs these — Google and Overture between "
                "them found 54 in 121 kecamatan, too sparse to rank, and a "
                "name match on 'Pasar ' mostly returns bus stops named after "
                "the market rather than the market",
    },
}


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", action="append", choices=sorted(TARGETS),
                    help="repeatable; omit with --all")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the Overpass QL and exit — no request sent")
    ap.add_argument("--endpoint", action="append",
                    help="override the mirror list; repeatable")
    ap.add_argument("--sleep", type=float, default=4.0,
                    help="seconds between targets (fair use; default 4)")
    ap.add_argument("--timeout", type=int, default=180,
                    help="Overpass server-side timeout in seconds")
    ap.add_argument("--no-nwr", action="store_true",
                    help="expand nwr to node/way/relation for old mirrors")
    ap.add_argument("--with-seribu", action="store_true")
    args = ap.parse_args()

    if args.list:
        print(f"{'key':<16}{'label':<44}{'selectors'}")
        for k, t in sorted(TARGETS.items()):
            print(f"{k:<16}{t['label']:<44}{len(t['selectors'])}")
            if t.get("note"):
                print(f"{'':<16}  -> {t['note']}")
        return 0

    targets = sorted(TARGETS) if args.all else (args.target or [])
    if not targets:
        ap.error("pick --all or at least one --target (see --list)")

    aoi = dict(config.AOI)
    if args.with_seribu:
        aoi["maxlat"] = config.AOI_SERIBU_MAXLAT

    queries = {k: osm.build_query(TARGETS[k]["selectors"], aoi,
                                  timeout=args.timeout,
                                  use_nwr=not args.no_nwr)
               for k in targets}

    if args.dry_run:
        for k in targets:
            print(f"\n── {k}  ::  {TARGETS[k]['label']}")
            print(queries[k])
        print(f"  {len(targets)} request(s) would be sent to "
              f"{(args.endpoint or osm.ENDPOINTS)[0]}")
        print("  Cost: $0.00 — Overpass is free and keyless. The only limit "
              "is fair use.")
        return 0

    sql_con = db.connect()
    run_id = db.start_run(sql_con, "osm", {
        "targets": targets, "aoi": aoi, "endpoints": args.endpoint,
        "sleep": args.sleep, "timeout": args.timeout})
    log(f"pull_run #{run_id} — connection: OSM / OVERPASS")
    log(f"attribution required on any output: {osm.ATTRIBUTION}")

    total = calls = 0
    try:
        for i, key in enumerate(targets):
            t = TARGETS[key]
            log(f"  {t['label']} …")
            elements, used, secs = osm.fetch(
                queries[key], endpoints=args.endpoint, log=print)
            calls += 1
            rows, dropped = osm.elements_to_rows(elements, key)
            n = db.upsert(sql_con, "poi_osm", rows, run_id)
            total += n
            extra = f", {dropped} without a point dropped" if dropped else ""
            log(f"    {n:>7,} rows   ({len(elements):,} elements{extra}) "
                f"in {secs:,.0f}s via {used.split('/')[2]}")
            # Politeness, not superstition: back-to-back heavy queries from
            # one IP is exactly what gets an address throttled.
            if i < len(targets) - 1 and args.sleep > 0:
                time.sleep(args.sleep)
        db.finish_run(sql_con, run_id, "ok", rows_written=total,
                      api_calls=calls, est_cost_usd=0.0,
                      notes=f"targets={','.join(targets)}")
    except SystemExit:
        db.finish_run(sql_con, run_id, "failed", notes="see console")
        raise
    except Exception as exc:                              # noqa: BLE001
        db.finish_run(sql_con, run_id, "failed", api_calls=calls,
                      notes=str(exc)[:500])
        print(f"\nFATAL: {exc}", file=sys.stderr)
        sql_con.close()
        return 1

    c = db.counts(sql_con)
    print(f"\n  {total:,} rows upserted   ·   poi_osm now holds "
          f"{c.get('poi_osm') or 0:,}")
    print(f"  Cost: $0.00 — no key, no account, nothing billable.")
    print(f"  Licence: {osm.ATTRIBUTION}. Share-alike applies to derived "
          f"databases you redistribute; that is why this table stands alone.")
    print(f"  Next: python enrich_admin.py --boundaries "
          f"data/boundaries/<file>.geojson")
    sql_con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
