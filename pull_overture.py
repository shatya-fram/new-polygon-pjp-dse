#!/usr/bin/env python3
"""
CONNECTION 1 — Overture Maps.

Pulls the AOI slice from the public Overture parquet on S3 into a local
cache, then loads the requested targets into the poi_overture table.
No API key. No billable request. Never touches poi_google.

    python pull_overture.py --list
    python pull_overture.py --target alfamart
    python pull_overture.py --all
    python pull_overture.py --all --refresh     # re-download the cache
"""
import argparse
import os
import sys
import time

import duckdb

import config
import db

S3 = config.OVERTURE_S3_BASE
CACHE = config.OVERTURE_CACHE

# target -> spec. Read a spec as: a row qualifies if its CATEGORY is one of
# `cats` OR its NAME matches `name_any` (and does not match `name_not`).
#
# Category and name are ORed, never ANDed. Overture's Indonesian places are
# unevenly categorised -- a station can arrive with no taxonomy at all -- so a
# category-only filter silently under-counts. `name_not` exists because
# "Bank Sampah" is a waste depot and "Stasiun Pengisian" is a petrol station.
#
# `brands` labels rows; it only *filters* them when brand_required is True.
# For banks and ATMs it deliberately does not: brand.wikidata is empty on
# every Indonesian row in this AOI and brand.names.primary is usually null,
# so requiring a brand match discarded the whole category.
#
# `min_conf` of 0.0 pins the target to full recall regardless of the global
# floor -- infrastructure POIs often carry low Overture confidence. The
# confidence value is stored on every row, so tightening later is a filter,
# not a re-pull. Passing --min-confidence on the CLI overrides these pins.
TARGETS = {
    "rail_stations": {
        "label": "Rail: KRL / MRT / LRT / main stations",
        "cats": ["train_station", "trains", "metro_station",
                 "light_rail_and_subway_stations", "railway_service",
                 "subway_station", "transit_station", "public_transportation"],
        "name_any": (r"^stasiun\b|\bstasiun\b|\bkrl\b|\bmrt\b|\blrt\b|"
                     r"commuter line|\bcommuterline\b"),
        # Jakarta names a lot of property after the line it sits near:
        # "LRT City Sentul" and "Apartment LRT Gateway Park" are housing
        # estates, "Depo MRT Lebak Bulus" is a train shed, "Proyek MRT
        # Fatmawati" is a building site. None of them is a station.
        "name_not": (r"stasiun pengisian|stasiun radio|stasiun tv|"
                     r"stasiun pompa|stasiun bumi|\bspbu\b|pemadam|"
                     r"\bdepo\b|\bdepot\b|proyek|apartemen|apartment|"
                     r"lrt city|residence|perumahan|\bruko\b"),
        "brands": None, "brand_required": False, "min_conf": 0.0,
    },
    "bus_terminals": {
        "label": "Bus terminals, halte & BRT",
        "cats": ["bus_station", "bus_service", "bus_stop", "transit_station",
                 "public_transportation"],
        "name_any": (r"^terminal\b|terminal bus|terminal bis|\bhalte\b|"
                     r"transjakarta|trans jakarta|busway|\bbrt\b"),
        "name_not": (r"terminal peti kemas|terminal kargo|terminal bandara|"
                     r"terminal 1|terminal 2|terminal 3|terminal penumpang|"
                     r"apartemen|apartment|perumahan|\bruko\b"),
        "brands": None, "brand_required": False, "min_conf": 0.0,
    },
    "bank_branches": {
        "label": "Bank branches (all banks, brand labelled)",
        # Codes confirmed against the real Jabodetabek slice, not guessed:
        # bank_or_credit_union (2,971 rows), bank (593), credit_union (515).
        # `financial_service` used to be in this list and dragged in ~1,980
        # tax consultants, insurers and leasing agents through
        # basic_category. Removed -- a category that broad is not a bank.
        "cats": ["bank_or_credit_union", "bank", "banks", "credit_union",
                 "bank_credit_union", "commercial_bank", "savings_bank"],
        # "kantor cabang" is generic Indonesian for any branch office. It
        # matched 596 rows with no bank in the name at all -- BPJS offices,
        # insurers, tax consultants -- so it is gone. The bank-specific
        # abbreviations stay, and BPR/BPD are added.
        "name_any": (r"^bank\b|\bbank\b|kantor kas|"
                     r"\bkcp\b|\bkcu\b|\bkck\b|\bbpr\b|\bbpd\b"),
        "name_not": (r"bank sampah|bank darah|bank soal|bank mini|bank data|"
                     r"bank asi|bank sperma|food bank|bank tanah|\batm\b|"
                     r"\bbpjs\b|konsultan pajak|kantor pajak|asuransi|leasing"),
        "brands": ["bca", "mandiri", "bni", "bri"],
        "brand_required": False, "min_conf": 0.0,
    },
    "atms": {
        "label": "ATMs (all banks, brand labelled)",
        "cats": ["atms", "atm", "automated_teller_machine"],
        "name_any": r"^atm\b|\batm\b|anjungan tunai",
        "name_not": r"atm sampah",
        "brands": ["bca", "mandiri", "bni", "bri"],
        "brand_required": False, "min_conf": 0.0,
    },
    "alfamart": {
        "label": "Alfamart",
        "cats": ["convenience_store", "supermarket", "grocery_store"],
        "name_any": None, "name_not": None,
        "brands": ["alfamart"], "brand_required": True, "min_conf": None,
    },
    "indomaret": {
        "label": "Indomaret",
        "cats": ["convenience_store", "supermarket", "grocery_store"],
        "name_any": None, "name_not": None,
        "brands": ["indomaret"], "brand_required": True, "min_conf": None,
    },
    "alfamidi": {
        "label": "Alfamidi",
        "cats": ["convenience_store", "supermarket", "grocery_store"],
        "name_any": None, "name_not": None,
        "brands": ["alfamidi"], "brand_required": True, "min_conf": None,
    },
    "minimarket_all": {
        "label": "All minimarkets (unbranded included)",
        "cats": ["convenience_store"],
        "name_any": None, "name_not": None,
        "brands": None, "brand_required": False, "min_conf": None,
    },
}


_has_spatial = False


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def duck():
    global _has_spatial
    con = duckdb.connect()
    for ext in ("spatial", "httpfs"):
        try:
            con.execute(f"INSTALL {ext}")
        except Exception:
            pass
        try:
            con.execute(f"LOAD {ext}")
            if ext == "spatial":
                _has_spatial = True
        except Exception:
            if ext == "httpfs":
                sys.exit(
                    "FATAL: DuckDB could not load `httpfs`, so s3:// is\n"
                    "  unreachable. Run once on a machine with open egress:\n"
                    '    duckdb -c "INSTALL httpfs; INSTALL spatial;"')
            log("WARN  spatial extension missing — coordinates come from the "
                "bbox struct (~1 m precision) instead of decoded WKB")
    con.execute("SET s3_region='us-west-2'")
    con.execute(f"SET memory_limit='{config.DUCKDB_MEMORY_LIMIT}'")
    con.execute(f"SET threads={config.DUCKDB_THREADS}")
    return con


def brand_case(qid_col="brand_qid", brand_col="brand_name", name_col="name"):
    """Order matters: BSI before Mandiri/BNI/BRI, BRILink before BRI."""
    parts = []
    for key in config.BRAND_RESOLVE_ORDER:
        cfg = config.BRANDS[key]
        label = cfg["label"].replace("'", "''")
        pat = cfg["pattern"].replace("'", "''")
        if cfg.get("wikidata"):
            parts.append(f"WHEN {qid_col} = '{cfg['wikidata']}' THEN '{label}'")
        parts.append(f"WHEN regexp_matches(lower(coalesce({brand_col},'')),"
                     f"'{pat}') THEN '{label}'")
        parts.append(f"WHEN regexp_matches(lower(coalesce({name_col},'')),"
                     f"'{pat}') THEN '{label}'")
    return "CASE " + " ".join(parts) + " ELSE NULL END"


def geom_exprs(con, src):
    """Pick the right way to read coordinates out of the parquet.

    DuckDB >= ~1.2 with `spatial` loaded decodes GeoParquet natively, so the
    `geometry` column arrives already typed GEOMETRY. Older builds hand back
    a WKB BLOB, which needs ST_GeomFromWKB. Passing a GEOMETRY to
    ST_GeomFromWKB is a binder error, so probe the column type instead of
    assuming either one."""
    if not _has_spatial:
        return "bbox.xmin", "bbox.ymin", "bbox struct (no spatial ext, ~1 m)"
    try:
        rows = con.execute(
            f"DESCRIBE SELECT geometry FROM read_parquet('{src}') LIMIT 0"
        ).fetchall()
        coltype = str(rows[0][1]).upper()
    except Exception:
        coltype = "BLOB"
    if coltype.startswith("GEOMETRY"):
        return "ST_X(geometry)", "ST_Y(geometry)", "native GEOMETRY"
    return ("ST_X(ST_GeomFromWKB(geometry))",
            "ST_Y(ST_GeomFromWKB(geometry))", "WKB blob")


def download_cache(con, aoi, refresh=False):
    if os.path.exists(CACHE) and not refresh:
        n = con.execute(
            f"SELECT count(*) FROM read_parquet('{CACHE}')").fetchone()[0]
        log(f"cache hit: {os.path.basename(CACHE)} ({n:,} rows) — "
            f"use --refresh to re-pull")
        return n
    if refresh and os.path.exists(CACHE):
        os.remove(CACHE)

    src = f"{S3}/{config.OVERTURE_RELEASE}/theme=places/type=place/*"
    lon, lat, how = geom_exprs(con, src)
    log(f"  geometry read as: {how}")
    log(f"downloading AOI slice from Overture {config.OVERTURE_RELEASE} …")
    log("  first run also fetches parquet metadata — allow 2–15 min")
    t0 = time.time()
    try:
        con.execute(f"""
          COPY (
            SELECT id, names.primary AS name,
                   brand.names.primary AS brand_name,
                   brand.wikidata      AS brand_qid,
                   taxonomy.primary    AS category,
                   basic_category, confidence, operating_status,
                   {lon} AS lon, {lat} AS lat,
                   CASE WHEN len(addresses)>0 THEN addresses[1].freeform END AS address,
                   CASE WHEN len(addresses)>0 THEN addresses[1].locality END AS locality,
                   CASE WHEN len(addresses)>0 THEN addresses[1].region   END AS region,
                   CASE WHEN len(phones)   >0 THEN phones[1]   END AS phone,
                   CASE WHEN len(websites) >0 THEN websites[1] END AS website,
                   CASE WHEN len(sources)  >0 THEN sources[1].dataset     END AS dataset,
                   CASE WHEN len(sources)  >0 THEN sources[1].update_time END AS source_updated
            FROM read_parquet('{src}', hive_partitioning=1)
            WHERE bbox.xmin BETWEEN {aoi['minlon']} AND {aoi['maxlon']}
              AND bbox.ymin BETWEEN {aoi['minlat']} AND {aoi['maxlat']}
          ) TO '{CACHE}' (FORMAT PARQUET, COMPRESSION ZSTD)""")
    except Exception as exc:
        msg = str(exc)
        hint = ("A wrong OVERTURE_RELEASE is the usual cause — check\n"
                "  https://docs.overturemaps.org/release-notes/")
        if "st_geomfromwkb" in msg.lower() or "Binder Error" in msg:
            hint = ("This is a SQL/type mismatch, not a bad release string.\n"
                    "  Report it — the geometry-column probe should have\n"
                    "  handled it.")
        elif "HTTP" in msg or "404" in msg:
            hint = ("Looks like the release path is unreachable. Check\n"
                    "  OVERTURE_RELEASE in .env against\n"
                    "  https://docs.overturemaps.org/release-notes/")
        sys.exit(f"FATAL: download failed.\n  {msg}\n  {hint}")
    n = con.execute(f"SELECT count(*) FROM read_parquet('{CACHE}')").fetchone()[0]
    log(f"cached {n:,} places -> {os.path.basename(CACHE)} "
        f"({os.path.getsize(CACHE)/1e6:,.1f} MB) in {time.time()-t0:,.0f}s")
    return n


def _q(pat):
    return pat.replace("'", "''")


def cat_clause(t):
    cats = t.get("cats") or []
    if not cats:
        return None
    quoted = ", ".join(f"'{c}'" for c in cats)
    return f"(category IN ({quoted}) OR basic_category IN ({quoted}))"


def name_clause(t):
    """Name rescue -- ORed with the category test, vetoed by name_not."""
    if not t.get("name_any"):
        return None
    low = "lower(coalesce(name,''))"
    nm = f"regexp_matches({low},'{_q(t['name_any'])}')"
    if t.get("name_not"):
        nm = f"({nm} AND NOT regexp_matches({low},'{_q(t['name_not'])}'))"
    return nm


def brand_clause(brands):
    ors = []
    for b in brands:
        cfg = config.BRANDS[b]
        pat = _q(cfg["pattern"])
        if cfg.get("wikidata"):
            ors.append(f"brand_qid = '{cfg['wikidata']}'")
        ors.append(f"regexp_matches(lower(coalesce(brand_name,'')),'{pat}')")
        ors.append(f"regexp_matches(lower(coalesce(name,'')),'{pat}')")
    return "(" + " OR ".join(ors) + ")"


def target_where(key, min_conf_default, force_conf=False):
    t = TARGETS[key]
    match = [c for c in (cat_clause(t), name_clause(t)) if c]
    if not match:
        sys.exit(f"target {key} defines neither cats nor name_any")
    pin = t.get("min_conf")
    mc = min_conf_default if (force_conf or pin is None) else pin
    where = ["(" + " OR ".join(match) + ")",
             f"coalesce(confidence,0) >= {mc}",
             "coalesce(operating_status,'open') = 'open'"]
    if t.get("brand_required") and t.get("brands"):
        where.append(brand_clause(t["brands"]))
    return t, where, mc


SELECT_COLS = """
        SELECT id AS source_id, 'overture' AS source, name,
               {brandcase} AS brand_resolved,
               brand_name, brand_qid, category, basic_category, confidence,
               operating_status,
               round(lat, 7) AS lat, round(lon, 7) AS lon,
               address, locality, region, phone, website,
               dataset, CAST(source_updated AS VARCHAR) AS source_updated
        FROM read_parquet('{cache}')
        WHERE {where}"""


def fetch_target(dcon, key, min_conf_default, force_conf=False):
    t, where, _mc = target_where(key, min_conf_default, force_conf)
    cur = dcon.execute(SELECT_COLS.format(
        brandcase=brand_case(), cache=CACHE, where=" AND ".join(where)))
    cols = [d[0] for d in cur.description]
    return t["label"], [dict(zip(cols, r)) for r in cur.fetchall()]


def _count(dcon, where):
    return dcon.execute(
        f"SELECT count(*) FROM read_parquet('{CACHE}') WHERE {where}"
    ).fetchone()[0]


def explain_target(dcon, key, min_conf_default, force_conf=False):
    """Show which clause is eating the rows, and which categories the
    matching rows are actually filed under.

    A target that returns nothing is far more often a filter problem than a
    coverage problem, and the two look identical from the outside. This
    prints the funnel so you can tell them apart before editing any codes."""
    t, where, mc = target_where(key, min_conf_default, force_conf)
    cc, nc = cat_clause(t), name_clause(t)
    both = "(" + " OR ".join(c for c in (cc, nc) if c) + ")"
    conf_sql = both + f" AND coalesce(confidence,0) >= {mc}"
    kept_sql = " AND ".join(where)

    print(f"\n-- {key}  ::  {t['label']}")
    brandmode = ("REQUIRED (rows without a brand match are dropped)"
                 if t.get("brand_required") else
                 "label only (nothing is dropped)" if t.get("brands") else "none")
    print(f"   confidence floor {mc}   ·   brand filter: {brandmode}")
    if cc:
        print(f"   category match only              {_count(dcon, cc):>9,}")
    if nc:
        print(f"   name match only                  {_count(dcon, nc):>9,}")
    print(f"   either (category OR name)        {_count(dcon, both):>9,}")
    print(f"   after confidence floor           {_count(dcon, conf_sql):>9,}")
    print(f"   after operating_status + brand   {_count(dcon, kept_sql):>9,}"
          f"   <- written")
    if t.get("brands"):
        bc = brand_clause(t["brands"])
        print(f"   of those, brand-identifiable     "
              f"{_count(dcon, kept_sql + ' AND ' + bc):>9,}")
    rows = dcon.execute(f'''
        SELECT coalesce(category, '(no category)') AS c, count(*) AS n
        FROM read_parquet('{CACHE}') WHERE {both}
        GROUP BY 1 ORDER BY n DESC LIMIT 12''').fetchall()
    if rows:
        print("   categories those rows are actually filed under:")
        for c, n in rows:
            print(f"      {c:<46}{n:>9,}")
    else:
        print("   nothing matched at all -- widen cats/name_any, or the AOI "
              "cache is stale (--refresh)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", action="append", choices=sorted(TARGETS),
                    help="repeatable; omit with --all")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--with-seribu", action="store_true")
    ap.add_argument("--min-confidence", type=float, default=None,
                    help="overrides every per-target confidence pin")
    ap.add_argument("--explain", action="store_true",
                    help="print the match funnel per target and write nothing")
    args = ap.parse_args()

    force_conf = args.min_confidence is not None
    min_conf_default = (args.min_confidence if force_conf
                        else config.OVERTURE_MIN_CONFIDENCE)

    if args.list:
        print(f"{'key':<16}{'label':<44}{'brand':<12}{'conf floor'}")
        for k, t in sorted(TARGETS.items()):
            bf = ("required" if t.get("brand_required")
                  else "label only" if t.get("brands") else "-")
            pin = t.get("min_conf")
            mc = min_conf_default if (force_conf or pin is None) else pin
            print(f"{k:<16}{t['label']:<44}{bf:<12}{mc}")
        return 0

    targets = sorted(TARGETS) if args.all else (args.target or [])
    if not targets:
        ap.error("pick --all or at least one --target (see --list)")

    aoi = dict(config.AOI)
    if args.with_seribu:
        aoi["maxlat"] = config.AOI_SERIBU_MAXLAT

    sql_con = db.connect()
    run_id = db.start_run(sql_con, "overture", {
        "targets": targets, "release": config.OVERTURE_RELEASE,
        "aoi": aoi, "min_confidence": min_conf_default,
        "refresh": args.refresh})
    log(f"pull_run #{run_id} — connection: OVERTURE")

    total = 0
    try:
        dcon = duck()
        download_cache(dcon, aoi, args.refresh)
        if args.explain:
            log("explain mode — reading the cache, writing nothing")
            for key in targets:
                explain_target(dcon, key, min_conf_default, force_conf)
            db.finish_run(sql_con, run_id, "ok", rows_written=0, api_calls=0,
                          est_cost_usd=0.0, notes="explain only")
            print("\n  Nothing written. Drop --explain to load these targets.")
            sql_con.close()
            return 0

        log("loading targets into poi_overture (no network from here) …")
        for key in targets:
            label, rows = fetch_target(dcon, key, min_conf_default, force_conf)
            n = db.upsert(sql_con, "poi_overture", rows, run_id)
            total += n
            log(f"  {label:<38}{n:>8,} rows")
        db.finish_run(sql_con, run_id, "ok", rows_written=total,
                      api_calls=0, est_cost_usd=0.0,
                      notes=f"targets={','.join(targets)}")
    except SystemExit:
        db.finish_run(sql_con, run_id, "failed", notes="see console")
        raise
    except Exception as exc:
        db.finish_run(sql_con, run_id, "failed", notes=str(exc)[:500])
        raise

    c = db.counts(sql_con)
    print(f"\n  {total:,} rows upserted   ·   poi_overture now holds "
          f"{c['poi_overture']:,}")
    print(f"  API cost: $0.00 — no key used, no billable request made.")
    sql_con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
