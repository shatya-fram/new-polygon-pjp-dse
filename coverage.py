#!/usr/bin/env python3
"""
Field-coverage report — measure what you actually have before trusting it.

    python coverage.py                       # both tables, to console
    python coverage.py --table poi_overture
    python coverage.py --by-category
    python coverage.py --format xlsx         # also write to exports/

Coordinates should always be 100% (both sources are point geometry). The
numbers worth watching are `address`, and after running enrich_admin.py,
`adm_kecamatan`.
"""
import argparse
import csv
import os
import sqlite3
import sys
import time

import config
import db

TABLES = ["poi_overture", "poi_google", "poi_osm", "poi_unified"]

# (column, label) — only reported if the column exists in that table.
FIELDS = [
    ("lat",            "coordinates"),
    ("name",           "name"),
    ("brand_resolved", "brand resolved"),
    ("category",       "category"),
    ("address",        "address (raw)"),
    ("locality",       "locality (API)"),
    ("kecamatan",      "kecamatan (API)"),
    ("adm_kelurahan",  "kelurahan (spatial)"),
    ("adm_kecamatan",  "kecamatan (spatial)"),
    ("adm_kota",       "kota (spatial)"),
    ("adm_provinsi",   "provinsi (spatial)"),
    ("confidence",     "confidence"),
    ("rating",         "rating"),
    ("phone",          "phone"),
]


def bar(pct, width=22):
    filled = int(round(pct / 100 * width))
    return "█" * filled + "·" * (width - filled)


def flag(col, pct):
    if col == "lat":
        return "  <-- MUST be 100%" if pct < 100 else ""
    if pct == 0:
        return "  (empty)"
    if pct < 40:
        return "  (sparse)"
    return ""


def report_table(con, table, by_category=False):
    cols = set(db.table_columns(con, table))
    if not cols:
        return None
    total = con.execute(f"SELECT count(*) c FROM {table}").fetchone()["c"]
    if not total:
        return {"table": table, "total": 0, "rows": []}

    present = [(c, lbl) for c, lbl in FIELDS if c in cols]
    exprs = ", ".join(f"count({c}) AS n_{c}" for c, _ in present)
    r = con.execute(f"SELECT {exprs} FROM {table}").fetchone()

    out = []
    for c, lbl in present:
        n = r[f"n_{c}"]
        out.append({"table": table, "scope": "ALL", "field": c, "label": lbl,
                    "filled": n, "total": total,
                    "pct": round(100 * n / total, 1)})

    if by_category:
        for row in con.execute(
                f"SELECT category, count(*) c FROM {table} "
                f"WHERE category IS NOT NULL GROUP BY 1 ORDER BY c DESC"):
            cat, ctot = row["category"], row["c"]
            rr = con.execute(
                f"SELECT {exprs} FROM {table} WHERE category = ?",
                (cat,)).fetchone()
            for c, lbl in present:
                n = rr[f"n_{c}"]
                out.append({"table": table, "scope": cat, "field": c,
                            "label": lbl, "filled": n, "total": ctot,
                            "pct": round(100 * n / ctot, 1)})
    return {"table": table, "total": total, "rows": out}


def print_report(res):
    print(f"\n{'=' * 66}")
    print(f"{res['table']}   {res['total']:,} rows")
    print("=" * 66)
    if not res["total"]:
        print("  (empty — run a pull first)")
        return
    scope = None
    for r in res["rows"]:
        if r["scope"] != scope:
            scope = r["scope"]
            if scope != "ALL":
                print(f"\n  -- {scope}  ({r['total']:,} rows)")
        pad = "  " if scope == "ALL" else "     "
        print(f"{pad}{r['label']:<22}{r['pct']:>6.1f}%  {bar(r['pct'])}"
              f"  {r['filled']:>7,}{flag(r['field'], r['pct'])}")


def advice(all_rows):
    tips = []
    for r in all_rows:
        if r["scope"] != "ALL":
            continue
        if r["field"] == "lat" and r["pct"] < 100:
            tips.append(f"{r['table']}: {r['total'] - r['filled']:,} rows have no "
                        f"coordinates — that should not happen; investigate.")
        if r["field"] == "address" and r["pct"] < 60:
            tips.append(f"{r['table']}: address is only {r['pct']}% filled. Use "
                        f"enrich_admin.py to derive the admin hierarchy from "
                        f"coordinates instead of parsing address strings.")
        if r["field"] == "adm_kecamatan" and r["pct"] == 0:
            tips.append(f"{r['table']}: no spatial admin data yet. Run "
                        f"enrich_admin.py --boundaries <file>.")
    if tips:
        print(f"\n{'=' * 66}\nWhat to do about it\n{'=' * 66}")
        for t in tips:
            print(f"  - {t}")


def write_file(rows, fmt):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(config.EXPORT_DIR, f"coverage-{stamp}.{fmt}")
    if fmt == "csv":
        with open(path, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
    else:
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill
            from openpyxl.utils import get_column_letter
        except ImportError:
            sys.exit("openpyxl not installed. Run: pip install openpyxl")
        wb = Workbook()
        ws = wb.active
        ws.title = "coverage"
        headers = list(rows[0].keys())
        ws.append(headers)
        for i in range(1, len(headers) + 1):
            c = ws.cell(row=1, column=i)
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = PatternFill("solid", fgColor="1F3864")
        for r in rows:
            ws.append([r[h] for h in headers])
        for i, h in enumerate(headers, 1):
            ws.column_dimensions[get_column_letter(i)].width = max(
                12, min(len(h) + 4, 30))
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = ws.dimensions
        wb.save(path)
    print(f"\n  written -> exports/{os.path.basename(path)}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", choices=TABLES, action="append")
    ap.add_argument("--by-category", action="store_true")
    ap.add_argument("--format", choices=["csv", "xlsx"])
    args = ap.parse_args()

    if not db.db_exists():
        sys.exit(f"No database at {config.DB_PATH} — run `python migrate.py` "
                 f"then a pull first.")

    con = db.connect()
    collected = []
    for t in (args.table or TABLES):
        try:
            res = report_table(con, t, args.by_category)
        except sqlite3.OperationalError as exc:
            print(f"  {t}: {exc}")
            continue
        if res:
            print_report(res)
            collected += res["rows"]
    advice(collected)
    if args.format and collected:
        write_file(collected, args.format)
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
