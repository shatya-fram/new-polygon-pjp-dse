#!/usr/bin/env python3
"""
Fill the admin hierarchy by SPATIAL JOIN, not by parsing address strings.

Every POI row has exact coordinates (both sources are point geometry), so
the reliable way to get kelurahan / kecamatan / kota / provinsi is to ask
which boundary polygon contains the point. That works even when the address
field is empty — which, for Overture in Indonesia, it very often is.

    python enrich_admin.py --boundaries batas_desa.geojson
    python enrich_admin.py --boundaries adm4.gpkg --table poi_google
    python enrich_admin.py --boundaries adm.shp --inspect   # list field names

Results land in the adm_* columns, kept separate from the API-reported
locality/kecamatan fields so you can always tell derived from reported.

Where to get boundaries (all free):
  - Indonesia Geospasial / BIG "Batas Administrasi Desa" (RBI) — authoritative
  - GADM v4.1  https://gadm.org/download_country.html  (level 4 = desa)
  - Overture `divisions` theme
"""
import argparse
import os
import sys
import time

import duckdb

import config
import db

# Field-name candidates, most-specific first. BPS/RBI uses WADM*; GADM uses
# NAME_n. Anything else, pass --field-* explicitly.
CANDIDATES = {
    "kelurahan": ["WADMKD", "NAMOBJ", "NAME_4", "kelurahan", "desa",
                  "village", "nm_kelurahan", "nmdesa"],
    "kecamatan": ["WADMKC", "NAME_3", "kecamatan", "district", "nm_kecamatan"],
    "kota":      ["WADMKK", "NAME_2", "kabkot", "kota", "kabupaten",
                  "regency", "nm_kabkota"],
    "provinsi":  ["WADMPR", "NAME_1", "provinsi", "province", "nm_provinsi"],
}

TABLES = ["poi_overture", "poi_google", "poi_osm"]


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def duck():
    """Return a DuckDB connection with spatial loaded, or None if the
    extension is unavailable (blocked proxy, offline laptop). The caller
    then falls back to the pure-Python join, which needs GeoJSON."""
    con = duckdb.connect()
    try:
        con.execute("INSTALL spatial")
    except Exception:
        pass
    try:
        con.execute("LOAD spatial")
        return con
    except Exception:
        con.close()
        return None


# ── pure-Python fallback ─────────────────────────────────────────────────
# Ray-casting point-in-polygon with a bbox prefilter. Handles GeoJSON only
# (shapefile/gpkg parsing needs the spatial extension). Fast enough in
# practice: the bbox check eliminates all but 1-3 candidate polygons per
# point, so 50k points over a few thousand kelurahan runs in seconds.

def load_geojson(path):
    import json
    with open(path, encoding="utf-8") as fh:
        gj = json.load(fh)
    feats = gj.get("features", []) if gj.get("type") == "FeatureCollection" \
        else [gj]
    out = []
    for f in feats:
        geom, props = f.get("geometry") or {}, f.get("properties") or {}
        if not geom:
            continue
        gtype = geom.get("type")
        if gtype == "Polygon":
            polys = [geom["coordinates"]]
        elif gtype == "MultiPolygon":
            polys = geom["coordinates"]
        else:
            continue
        for rings in polys:
            xs = [p[0] for p in rings[0]]
            ys = [p[1] for p in rings[0]]
            out.append({"rings": rings, "props": props,
                        "bbox": (min(xs), min(ys), max(xs), max(ys))})
    return out


def in_ring(x, y, ring):
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i][0], ring[i][1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > y) != (yj > y)) and \
                (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-15) + xi):
            inside = not inside
        j = i
    return inside


def locate(x, y, polys):
    for p in polys:
        x0, y0, x1, y1 = p["bbox"]
        if not (x0 <= x <= x1 and y0 <= y <= y1):
            continue
        rings = p["rings"]
        if not in_ring(x, y, rings[0]):
            continue
        # interior rings are holes
        if any(in_ring(x, y, h) for h in rings[1:]):
            continue
        return p["props"]
    return None


def enrich_python(sql_con, table, path, fields):
    polys = load_geojson(path)
    rows = sql_con.execute(
        f"SELECT source_id, lat, lon FROM {table} "
        f"WHERE lat IS NOT NULL AND lon IS NOT NULL").fetchall()
    if not rows:
        log(f"  {table}: no rows with coordinates — skipping")
        return 0, 0
    levels = list(fields.keys())
    setters = ", ".join(f"adm_{lv} = ?" for lv in levels)
    payload, hit = [], 0
    for sid, lat, lon in rows:
        props = locate(lon, lat, polys)
        vals = [props.get(fields[lv]) if props else None for lv in levels]
        if props:
            hit += 1
        payload.append(vals + ["spatial_join(py)", sid])
    sql_con.executemany(
        f"UPDATE {table} SET {setters}, adm_source = ? WHERE source_id = ?",
        payload)
    sql_con.commit()
    log(f"  {table}: {len(rows):,} points · {hit:,} matched a polygon "
        f"({100 * hit / max(len(rows), 1):.1f}%)")
    return len(rows), hit


def boundary_fields(dcon, path):
    if dcon is not None:
        return [r[0] for r in dcon.execute(
            f"DESCRIBE SELECT * FROM ST_Read('{path}') LIMIT 0").fetchall()]
    if not path.lower().endswith((".geojson", ".json")):
        sys.exit("FATAL: the DuckDB `spatial` extension is unavailable, so only\n"
                 "  GeoJSON can be read. Either convert your boundary file to\n"
                 "  GeoJSON, or install the extension on a machine with open\n"
                 '  egress:  duckdb -c "INSTALL spatial;"')
    polys = load_geojson(path)
    if not polys:
        sys.exit(f"FATAL: no polygon features found in {path}")
    return list(polys[0]["props"].keys())


def resolve_fields(available, overrides):
    """Match wanted levels against what the file actually has, case-insensitively."""
    lower = {c.lower(): c for c in available}
    out = {}
    for level, names in CANDIDATES.items():
        if overrides.get(level):
            if overrides[level] not in available:
                sys.exit(f"FATAL: --field-{level} '{overrides[level]}' is not a "
                         f"column in the boundary file.\n  Available: "
                         f"{', '.join(available)}")
            out[level] = overrides[level]
            continue
        for cand in names:
            if cand.lower() in lower:
                out[level] = lower[cand.lower()]
                break
    return out


def enrich(dcon, sql_con, table, path, fields, batch=50000):
    rows = sql_con.execute(
        f"SELECT source_id, lat, lon FROM {table} "
        f"WHERE lat IS NOT NULL AND lon IS NOT NULL").fetchall()
    if not rows:
        log(f"  {table}: no rows with coordinates — skipping")
        return 0, 0

    dcon.execute("CREATE OR REPLACE TABLE pts (source_id VARCHAR, "
                 "lat DOUBLE, lon DOUBLE)")
    dcon.executemany("INSERT INTO pts VALUES (?,?,?)", rows)

    sel = ", ".join(f'b."{col}" AS {level}' for level, col in fields.items())
    # ST_Contains on the polygon side, point built from the POI coords. The
    # spatial extension uses an R-tree here, so this stays fast at 100k+ pts.
    q = f"""
      SELECT p.source_id, {sel}
      FROM pts p
      LEFT JOIN ST_Read('{path}') b
        ON ST_Contains(b.geom, ST_Point(p.lon, p.lat))
    """
    matched = dcon.execute(q).fetchall()

    levels = list(fields.keys())
    setters = ", ".join(f"adm_{lv} = ?" for lv in levels)
    payload, hit = [], 0
    for rec in matched:
        sid, vals = rec[0], rec[1:]
        if any(v is not None for v in vals):
            hit += 1
        payload.append(list(vals) + ["spatial_join", sid])

    sql_con.executemany(
        f"UPDATE {table} SET {setters}, adm_source = ? WHERE source_id = ?",
        payload)
    sql_con.commit()
    log(f"  {table}: {len(rows):,} points · {hit:,} matched a polygon "
        f"({100*hit/max(len(rows),1):.1f}%)")
    return len(rows), hit


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--boundaries", required=True,
                    help="GeoJSON / Shapefile / GeoPackage of admin polygons")
    ap.add_argument("--table", choices=TABLES, action="append")
    ap.add_argument("--inspect", action="store_true",
                    help="print the boundary file's field names and exit")
    for lv in CANDIDATES:
        ap.add_argument(f"--field-{lv}", dest=f"f_{lv}",
                        help=f"column holding the {lv} name")
    args = ap.parse_args()

    if not os.path.exists(args.boundaries):
        sys.exit(f"FATAL: boundary file not found: {args.boundaries}")
    if not db.db_exists():
        sys.exit(f"FATAL: no database at {config.DB_PATH} — run migrate.py "
                 f"and a pull first.")

    dcon = duck()
    if dcon is None:
        log("NOTE  DuckDB `spatial` unavailable — using the pure-Python "
            "point-in-polygon join (GeoJSON only).")
    available = boundary_fields(dcon, args.boundaries)

    if args.inspect:
        print(f"\nFields in {os.path.basename(args.boundaries)}:")
        for c in available:
            print(f"  {c}")
        guessed = resolve_fields(available, {})
        print("\nAuto-detected mapping:")
        for lv in CANDIDATES:
            print(f"  {lv:<10} -> {guessed.get(lv) or '(none — pass --field-' + lv + ')'}")
        return 0

    fields = resolve_fields(available, {lv: getattr(args, f"f_{lv}")
                                        for lv in CANDIDATES})
    if not fields:
        sys.exit("FATAL: could not identify any admin-level column.\n"
                 "  Run with --inspect to see the field names, then pass\n"
                 "  --field-kelurahan / --field-kecamatan / etc. explicitly.")

    log(f"boundary file: {os.path.basename(args.boundaries)}")
    for lv, col in fields.items():
        log(f"  {lv:<10} <- {col}")
    missing = [lv for lv in CANDIDATES if lv not in fields]
    if missing:
        log(f"  not available in this file: {', '.join(missing)}")

    sql_con = db.connect()
    run_id = db.start_run(sql_con, "enrich_admin",
                          {"boundaries": args.boundaries, "fields": fields})
    total = hits = 0
    try:
        for t in (args.table or TABLES):
            if dcon is not None:
                n, h = enrich(dcon, sql_con, t, args.boundaries, fields)
            else:
                n, h = enrich_python(sql_con, t, args.boundaries, fields)
            total += n
            hits += h
        db.finish_run(sql_con, run_id, "ok", rows_written=hits,
                      notes=f"spatial join, {len(fields)} levels")
    except Exception as exc:
        db.finish_run(sql_con, run_id, "failed", notes=str(exc)[:500])
        raise

    print(f"\n  {hits:,} of {total:,} points assigned an admin polygon "
          f"({100*hits/max(total,1):.1f}%)")
    if hits < total:
        print("  Unmatched points sit outside the boundary file's coverage,\n"
              "  or fall in a sliver gap. Check adm_kecamatan IS NULL to see them.")
    print("  API cost: $0.00 — this is a local geometric join.")
    sql_con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
