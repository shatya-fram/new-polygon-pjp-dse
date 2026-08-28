#!/usr/bin/env python3
"""
What does Overture ACTUALLY have in the cached AOI slice?

Reads data/overture_aoi.parquet (no network) and reports every category
present, so target lists can be built from facts instead of guesses.

    python inspect_categories.py                 # overview
    python inspect_categories.py --grep bank     # anything matching a word
    python inspect_categories.py --tree transit  # hierarchy for a theme
"""
import argparse, os, sys
import duckdb, config

CACHE = config.OVERTURE_CACHE

def duck():
    con = duckdb.connect()
    for e in ("spatial", "httpfs"):
        try: con.execute(f"LOAD {e}")
        except Exception: pass
    return con

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--grep", action="append", help="substring to search for")
    ap.add_argument("--top", type=int, default=40)
    ap.add_argument("--min-conf", type=float, default=0.0)
    a = ap.parse_args()
    if not os.path.exists(CACHE):
        sys.exit(f"No cache at {CACHE} — run pull_overture.py first.")
    con = duck()
    n = con.execute(f"SELECT count(*) FROM read_parquet('{CACHE}')").fetchone()[0]
    print(f"cache: {os.path.basename(CACHE)}  ·  {n:,} places in AOI\n")

    terms = a.grep or ["bank", "atm", "financial", "credit",
                       "station", "transit", "rail", "train", "metro",
                       "subway", "bus", "terminal", "halte", "airport"]
    print(f"{'category':38}{'basic_category':30}{'rows':>8}  {'conf≥.4':>8}")
    print("-" * 86)
    for t in terms:
        rows = con.execute(f"""
            SELECT coalesce(category,'(null)') c,
                   coalesce(basic_category,'(null)') b,
                   count(*) n,
                   sum(CASE WHEN coalesce(confidence,0)>=0.4 THEN 1 ELSE 0 END) n4
            FROM read_parquet('{CACHE}')
            WHERE lower(coalesce(category,'')) LIKE '%{t}%'
               OR lower(coalesce(basic_category,'')) LIKE '%{t}%'
            GROUP BY 1,2 ORDER BY n DESC""").fetchall()
        if rows:
            print(f"~ matching '{t}'")
            for c, b, cnt, c4 in rows[:a.top]:
                print(f"  {c:36}{b:30}{cnt:>8,}  {c4:>8,}")
    print()
    print("top 25 categories overall in the AOI")
    print("-" * 86)
    for c, b, cnt in con.execute(f"""
            SELECT coalesce(category,'(null)'), coalesce(basic_category,'(null)'),
                   count(*) n FROM read_parquet('{CACHE}')
            GROUP BY 1,2 ORDER BY n DESC LIMIT 25""").fetchall():
        print(f"  {c:36}{b:30}{cnt:>8,}")

    print("\nname-based probe (categories can be wrong; names rarely are)")
    print("-" * 86)
    for label, pat in [("Bank (any)", "bank "), ("BCA", "bca"), ("Mandiri", "mandiri"),
                       ("BNI", "bni"), ("BRI", "bri"), ("ATM", "atm"),
                       ("Stasiun", "stasiun"), ("MRT", "mrt"), ("LRT", "lrt"),
                       ("Halte", "halte"), ("Terminal", "terminal")]:
        r = con.execute(f"""SELECT count(*) FROM read_parquet('{CACHE}')
            WHERE lower(coalesce(name,'')) LIKE '%{pat}%'""").fetchone()[0]
        ex = con.execute(f"""SELECT name, coalesce(category,'(null)')
            FROM read_parquet('{CACHE}')
            WHERE lower(coalesce(name,'')) LIKE '%{pat}%' LIMIT 2""").fetchall()
        s = "  |  ".join(f"{x[0][:30]} [{x[1]}]" for x in ex)
        print(f"  {label:12}{r:>7,}   {s}")
    con.close()

if __name__ == "__main__":
    main()
