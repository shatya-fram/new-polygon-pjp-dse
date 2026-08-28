#!/usr/bin/env python3
"""
Export query results as MAP files — GeoJSON, KML/KMZ, GeoPackage, Shapefile.

Every row already carries exact coordinates, so any of these opens directly
in QGIS, ArcGIS, Google Earth or a web map with no further processing.

    python export_map.py --format geojson
    python export_map.py --format kml --table poi_overture --brand Alfamart
    python export_map.py --format geojson --split-by brand
    python export_map.py --all-formats

GeoJSON and KML are written in pure Python — no GDAL, no extra install.
GeoPackage and Shapefile go through DuckDB's spatial extension; if that
isn't available the script says so and suggests GeoJSON instead.

Which format to pick:
  geojson  QGIS, ArcGIS, Leaflet/Mapbox, anything web. The safe default.
  kml      Google Earth, Google My Maps. Folders + styled pins per group.
  kmz      Same as KML, zipped — smaller, and what Earth prefers.
  gpkg     QGIS native. One file, typed columns, no 10-char field limit.
  shp      Legacy GIS. Field names truncate to 10 chars; use only if asked.
"""
import argparse
import json
import os
import sys
import time
import zipfile

import config
import db

TABLES = ["poi_overture", "poi_google", "poi_osm", "poi_unified"]

# CLI names -> actual column names. "brand" is the obvious thing to type;
# the column is brand_resolved. Getting this wrong silently dumps every row
# into one "unassigned" file, so it is aliased rather than left to chance.
FIELD_ALIAS = {
    "brand": "brand_resolved",
    "category": "category",
    "kecamatan": "adm_kecamatan",
    "kelurahan": "adm_kelurahan",
    "kota": "adm_kota",
}
PURE_PYTHON = {"geojson", "kml", "kmz"}
NEEDS_GDAL = {"gpkg", "shp"}

# KML pin colours are aabbggrr, not rrggbb.
KML_STYLES = {
    "overture": ("ovt", "ff5c5c7c"),   # purple
    "google":   ("ggl", "ffe58739"),   # blue
    "osm":      ("osm", "ff6ec46e"),   # green
    "unified":  ("uni", "ff4bd0f0"),   # amber
}


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def fetch(con, table, brand=None, category=None, name=None, kecamatan=None):
    rows = db.query(con, table, category=category, brand=brand,
                    name_like=name, limit=1_000_000)
    if kecamatan:
        rows = [r for r in rows if (r.get("adm_kecamatan") or "") == kecamatan]
    # A row without coordinates cannot be a map feature. Drop it loudly
    # rather than writing a null-geometry feature that breaks QGIS.
    good = [r for r in rows if r.get("lat") is not None and r.get("lon") is not None]
    if len(good) != len(rows):
        log(f"  WARN {len(rows) - len(good):,} rows had no coordinates and were "
            f"skipped (should be zero — check coverage.py)")
    return good


def props(r):
    """Everything except the coordinates becomes a feature property."""
    return {k: v for k, v in r.items() if k not in ("lat", "lon")}


# ── GeoJSON ──────────────────────────────────────────────────────────────
def write_geojson(rows, path, name):
    fc = {
        "type": "FeatureCollection",
        "name": name,
        # WGS84 — what every consumer assumes, stated explicitly anyway.
        "crs": {"type": "name",
                "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": [
            {"type": "Feature",
             "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
             "properties": props(r)}
            for r in rows
        ],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(fc, fh, ensure_ascii=False)
    return len(rows)


# ── KML / KMZ ────────────────────────────────────────────────────────────
def xml_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def kml_description(r):
    """A small HTML table so the Google Earth balloon is readable."""
    skip = {"lat", "lon"}
    cells = "".join(
        f"<tr><td style='color:#666;padding-right:8px'>{xml_escape(k)}</td>"
        f"<td>{xml_escape(v)}</td></tr>"
        for k, v in r.items() if k not in skip and v not in (None, ""))
    return f"<![CDATA[<table style='font:12px sans-serif'>{cells}</table>]]>"


def write_kml(rows, path, name, source, group_by="category", kmz=False):
    style_id, colour = KML_STYLES.get(source, ("ovt", "ff5c5c7c"))
    col = FIELD_ALIAS.get(group_by, group_by)
    groups = {}
    for r in rows:
        groups.setdefault(r.get(col) or "(none)", []).append(r)

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<kml xmlns="http://www.opengis.net/kml/2.2">',
        f"<Document><name>{xml_escape(name)}</name>",
        f'<Style id="{style_id}"><IconStyle><color>{colour}</color><scale>0.9</scale>'
        '<Icon><href>http://maps.google.com/mapfiles/kml/shapes/placemark_circle.png'
        "</href></Icon></IconStyle>"
        f"<LabelStyle><scale>0.7</scale></LabelStyle></Style>",
    ]
    for gname, grows in sorted(groups.items()):
        parts.append(f"<Folder><name>{xml_escape(gname)} "
                     f"({len(grows):,})</name>")
        for r in grows:
            parts.append(
                "<Placemark>"
                f"<name>{xml_escape(r.get('name') or '(no name)')}</name>"
                f"<styleUrl>#{style_id}</styleUrl>"
                f"<description>{kml_description(r)}</description>"
                f"<Point><coordinates>{r['lon']},{r['lat']},0</coordinates></Point>"
                "</Placemark>")
        parts.append("</Folder>")
    parts.append("</Document></kml>")
    doc = "\n".join(parts)

    if kmz:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("doc.kml", doc)
    else:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(doc)
    return len(rows)


# ── GeoPackage / Shapefile (via DuckDB spatial) ──────────────────────────
def write_gdal(rows, path, fmt, layer):
    try:
        import duckdb
    except ImportError:
        sys.exit("duckdb is not installed.")
    con = duckdb.connect()
    try:
        con.execute("INSTALL spatial")
    except Exception:
        pass
    try:
        con.execute("LOAD spatial")
    except Exception:
        sys.exit(
            f"FATAL: writing {fmt} needs DuckDB's `spatial` extension, which\n"
            f"  could not be loaded. Either install it on a machine with open\n"
            f'  egress ( duckdb -c "INSTALL spatial;" ), or use\n'
            f"  --format geojson — QGIS opens that natively and it needs nothing.")

    if not rows:
        log("  no rows — nothing written")
        return 0
    cols = [c for c in rows[0].keys() if c not in ("lat", "lon")]
    coldefs = ", ".join('"{}" VARCHAR'.format(c) for c in cols)
    con.execute(f"CREATE TABLE t ({coldefs}, lat DOUBLE, lon DOUBLE)")
    con.executemany(
        f"INSERT INTO t VALUES ({','.join('?' for _ in cols)},?,?)",
        [[str(r[c]) if r[c] is not None else None for c in cols]
         + [r["lat"], r["lon"]] for r in rows])

    driver = "GPKG" if fmt == "gpkg" else "ESRI Shapefile"
    sel = ", ".join(f'"{c}"' for c in cols)
    con.execute(f"""
        COPY (SELECT {sel}, ST_Point(lon, lat) AS geom FROM t)
        TO '{path}' WITH (FORMAT GDAL, DRIVER '{driver}',
                          LAYER_CREATION_OPTIONS 'LAYER_NAME={layer}',
                          SRS 'EPSG:4326')""")
    con.close()
    return len(rows)


# ── driver ───────────────────────────────────────────────────────────────
def out_path(table, fmt, suffix=""):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tag = f"-{suffix}" if suffix else ""
    return os.path.join(config.EXPORT_DIR, f"{table}{tag}-{stamp}.{fmt}")


def write_one(rows, table, fmt, source, suffix="", group_by="category"):
    path = out_path(table, fmt, suffix)
    label = f"{table}{'/' + suffix if suffix else ''}"
    if fmt == "geojson":
        n = write_geojson(rows, path, label)
    elif fmt in ("kml", "kmz"):
        n = write_kml(rows, path, label, source, group_by, kmz=(fmt == "kmz"))
    else:
        n = write_gdal(rows, path, fmt, table)
    size = os.path.getsize(path) / 1024 if os.path.exists(path) else 0
    log(f"  {label:<34}{n:>7,} pts -> exports/{os.path.basename(path)} "
        f"({size:,.0f} KB)")
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", choices=TABLES, action="append")
    ap.add_argument("--format", choices=sorted(PURE_PYTHON | NEEDS_GDAL),
                    default="geojson")
    ap.add_argument("--all-formats", action="store_true",
                    help="write geojson + kmz for each table")
    ap.add_argument("--split-by", choices=list(FIELD_ALIAS),
                    help="one file per distinct value instead of one combined")
    ap.add_argument("--group-by", default="category", choices=list(FIELD_ALIAS),
                    help="KML folder grouping (default: category)")
    ap.add_argument("--brand"), ap.add_argument("--category")
    ap.add_argument("--kecamatan"), ap.add_argument("--name")
    args = ap.parse_args()

    if not db.db_exists():
        sys.exit(f"No database at {config.DB_PATH} — run `python migrate.py` "
                 f"then a pull first.")

    formats = ["geojson", "kmz"] if args.all_formats else [args.format]
    con = db.connect()
    total = 0
    for table in (args.table or TABLES):
        source = {"poi_overture": "overture", "poi_google": "google",
                  "poi_osm": "osm", "poi_unified": "unified"}[table]
        rows = fetch(con, table, args.brand, args.category, args.name,
                     args.kecamatan)
        if not rows:
            log(f"  {table}: no matching rows — skipped")
            continue
        for fmt in formats:
            if args.split_by:
                col = FIELD_ALIAS[args.split_by]
                if rows and col not in rows[0]:
                    sys.exit(f"FATAL: {table} has no column '{col}'.")
                buckets = {}
                for r in rows:
                    buckets.setdefault(r.get(col) or "unassigned", []).append(r)
                for key, brows in sorted(buckets.items()):
                    slug = "".join(ch if ch.isalnum() else "_"
                                   for ch in str(key)).strip("_").lower()
                    total += write_one(brows, table, fmt, source, slug,
                                       args.group_by)
            else:
                total += write_one(rows, table, fmt, source, "", args.group_by)
    con.close()

    print(f"\n  {total:,} points written to ./exports/")
    print("  GeoJSON: drag into QGIS, or Layer > Add Layer > Add Vector Layer.")
    print("  KMZ: open in Google Earth, or import to Google My Maps.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
