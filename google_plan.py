#!/usr/bin/env python3
"""
Estimate the Google Places bill BEFORE spending anything.

    python google_plan.py                     # full plan, all groups
    python google_plan.py --group government
    python google_plan.py --expect industrial=12000

No API key needed, no request made. It reads your kecamatan and kabupaten
polygons out of geo_feature and uses them as the search grid, so the plan
is sized against your actual territory rather than a made-up square grid.

WHY THE GRID MATTERS. Google bills per REQUEST, not per result, and one
Text Search request returns at most 20 places. So the cost is driven by how
many requests you issue, which is (cells x queries) plus one extra request
for every additional 20 results. A grid that is too fine burns requests on
empty cells; too coarse and you hit the 60-result ceiling and lose places
silently. Kecamatan-sized cells are close to right for Jabodetabek.

SKU. The fields you asked for -- name, lat/lon, category, and enough to drop
permanently-closed -- put this on Text Search Pro at $32 per 1,000 requests.
There is no cheaper route: displayName, primaryType and businessStatus are
all Pro-tier fields, in Place Details as well as in Search. The only tier
below Pro for Search is IDs Only, which returns place IDs and nothing else.
"""
import argparse
import math
import sqlite3
import sys

import config
import db
from providers import google_places as gp

PAGE_SIZE = 20          # Text Search max results per request
MAX_PAGES = 3           # Google caps a query at 3 pages / 60 results
BYTES_PER_PLACE = 290   # measured against the LEAN_FIELDS response shape

# Measured on the first real sweeps: about a third of everything fetched is
# the same place returned by an overlapping neighbouring cell. Requests are
# driven by results FETCHED, not by distinct places, so the estimate has to
# carry this or it under-counts by a third before it starts.
REFETCH_FACTOR = 1.7

# Pages consumed per search, fitted to ten real target runs. It is
# deliberately NOT ceil(avg/20): paging is decided per search over a skewed
# distribution, so a dense cell burns three pages while a sparse one burns
# a single request, and rounding the average down loses the dense tail.
# The straight-line fit lands within 10% on eight of the ten observations
# and is biased high, which is the right direction for a budget guard.
PAGES_INTERCEPT = 0.85
PAGES_DIVISOR = 24.0

# group -> targets. `grid` picks the cell size; `queries` is how many
# distinct searches run per cell. `expect` is the number of PLACES we think
# exist in the AOI -- the one number worth arguing about, so it is a CLI
# override and every source is stated.
# group -> targets.
#
# `searches` is the exact list of (textQuery, includedType) pairs that will
# be issued in EVERY cell. It is the single source of truth: pull_google.py
# runs this list verbatim, so the plan and the pull cannot disagree about
# what gets called or what it costs.
#
# Text Search always needs a textQuery -- there is no type-only mode that
# accepts a rectangle -- and `includedType` takes exactly one type, so a
# target needing two types costs two searches per cell. That is why the
# query count, not the place count, drives the bill.
#
# `expect` is the number of PLACES we think exist. It is the only soft
# number here, so every one states its basis and can be overridden.
TARGETS = {
    "commercial": [
        dict(key="alfamart", label="Alfamart", grid="kecamatan", expect=2655,
             searches=[("Alfamart", "convenience_store")],
             basis="MEASURED: 2,655 distinct in the first sweep"),
        dict(key="indomaret", label="Indomaret", grid="kecamatan", expect=2734,
             searches=[("Indomaret", "convenience_store")],
             basis="MEASURED: 2,734 distinct in the first sweep"),
        dict(key="alfamidi", label="Alfamidi", grid="kecamatan", expect=362,
             searches=[("Alfamidi", "convenience_store")],
             basis="MEASURED: 362 distinct in the first sweep"),
        dict(key="stores_other", label="Other minimarket / supermarket / grocery",
             grid="kecamatan", expect=11000,
             searches=[("minimarket", "convenience_store"),
                       ("supermarket", "supermarket"),
                       ("toko kelontong", "grocery_store")],
             basis="MEASURED: 10,983 distinct -- my 6,000 guess was 45% low"),
        dict(key="malls", label="Malls & department stores", grid="kabkot",
             expect=390,
             searches=[("mall", "shopping_mall"),
                       ("department store", "department_store")],
             basis="MEASURED: 387 distinct"),
    ],
    # ── telco retail ─────────────────────────────────────────────────────
    # Added because the competitor service-point layer imported from KMZ is
    # DKI-only: 18 GraPARI, 15 XL Center, 5 Smartfren, every one of them
    # inside Jakarta and not one in Depok, Bekasi or Karawang. That hole
    # sits directly under the CPI term of the siting model, in exactly the
    # kecamatan it ranks highest, so competitor density there reads zero
    # when it is not zero.
    #
    # The operators' own store locators cannot supply this: Telkomsel
    # publishes a static table with addresses but no coordinates, and the
    # XL and Smartfren locators render their lists in JavaScript. Google
    # Places has both the names and the coordinates.
    "telco": [
        dict(key="grapari", label="GraPARI (Telkomsel)", grid="kabkot",
             expect=30,
             searches=[("GraPARI Telkomsel", "cell_phone_store"),
                       ("GraPARI", "cell_phone_store")],
             basis="GROUND TRUTH: 28 GraPARI listed for this AOI on "
                   "telkomsel.com/contact-us/grapari (Jabodetabek + Jawa "
                   "Barat tabs). Use that list to measure recall."),
        dict(key="xlcenter", label="XL Center / XL Axiata", grid="kabkot",
             expect=25,
             searches=[("XL Center", "cell_phone_store"),
                       ("XL Axiata Center", "cell_phone_store")],
             basis="ESTIMATE: 15 already held for DKI alone; the locator "
                   "at xl.co.id is JavaScript-rendered so there is no "
                   "published count to check against."),
        dict(key="smartfren", label="Galeri Smartfren", grid="kabkot",
             expect=25,
             searches=[("Galeri Smartfren", "cell_phone_store"),
                       ("Smartfren", "cell_phone_store")],
             basis="ESTIMATE: 5 held for DKI. smartfren.com/galeri returns "
                   "HTTP 500, so again no published count."),
        dict(key="own_stores", label="Gerai IM3 / 3Store (our own)",
             grid="kabkot", expect=70,
             searches=[("Gerai IM3 Indosat", "cell_phone_store"),
                       ("3Store Tri", "cell_phone_store")],
             basis="ESTIMATE: 45 IM3 + 5 3ID currently held from KMZ. "
                   "Pulling them independently checks whether the KMZ is "
                   "complete -- the G1 gate is only as good as that list."),
    ],
    "government": [
        dict(key="gov_kelurahan", label="Kantor Kelurahan / Desa",
             grid="kecamatan", expect=700,
             searches=[("kantor kelurahan", "local_government_office"),
                       ("kantor desa", "local_government_office")],
             basis="MEASURED: ~700 of the 400-row government sweep"),
        dict(key="gov_kecamatan", label="Kantor Kecamatan", grid="kabkot",
             expect=101,
             searches=[("kantor kecamatan", "local_government_office")],
             basis="MEASURED: 101 returned"),
        # includedType was city_hall here and it returned almost nothing:
        # 22 requests, 2 rows, not one name containing "walikota" or
        # "bupati". Indonesian city and regency halls are filed under
        # local_government_office, and the official spelling is "wali kota"
        # as two words, so both spellings are queried.
        dict(key="gov_kabkot", label="Kantor Wali Kota / Bupati", grid="kabkot",
             expect=73,
             searches=[("kantor wali kota", "local_government_office"),
                       ("kantor bupati", "local_government_office"),
                       ("balai kota", "local_government_office")],
             basis="MEASURED: 73 returned, 11 of them actual halls"),
        dict(key="gov_provinsi", label="Kantor Gubernur / provincial",
             grid="aoi", expect=3,
             searches=[("kantor gubernur", "local_government_office")],
             basis="DKI Jakarta, Jawa Barat, plus satellite offices"),
    ],
    "finance": [
        # `atm` and `bank` are the only Finance types in Table A (the third
        # is `accounting`), both verified against the current type list.
        #
        # ATMs are queried per bank as well as generically. That is not
        # redundancy: one broad "ATM" query per cell hits Google's 60-result
        # ceiling long before it exhausts a dense kecamatan, so the brand
        # queries are how you actually reach the tail. Five cheap one-page
        # searches beat one search that pages out and still truncates.
        dict(key="atms", label="ATMs (all, brand labelled)",
             grid="kecamatan", expect=10133,
             searches=[("ATM", "atm"), ("ATM BCA", "atm"),
                       ("ATM Mandiri", "atm"), ("ATM BNI", "atm"),
                       ("ATM BRI", "atm")],
             basis="MEASURED: 10,133 distinct -- my 8,000 guess was 21% low"),
        dict(key="bank_branches", label="Bank branches (cabang / kantor kas)",
             grid="kecamatan", expect=3651,
             searches=[("bank", "bank"), ("kantor cabang bank", "bank"),
                       ("kantor kas bank", "bank")],
             basis="MEASURED: 3,651 distinct"),
        # Head offices are few and clustered in central Jakarta, so they get
        # the coarse grid -- 22 requests instead of 242 for the same result.
        dict(key="bank_hq", label="Bank kantor pusat / kantor utama",
             grid="kabkot", expect=115,
             searches=[("kantor pusat bank", "bank"),
                       ("kantor utama bank", "bank")],
             basis="MEASURED: 115 distinct"),
        # BRILink agents are warungs with a terminal, not branches. No type
        # fits them, and the brand resolver already orders BRILink ahead of
        # BRI so they are never counted as branches.
        dict(key="brilink", label="Agen BRILink", grid="kecamatan",
             expect=934,
             searches=[("BRILink", None), ("Agen BRILink", None)],
             basis="MEASURED: 934 distinct -- Overture had 12, so Google is 75x here"),
    ],
    "industrial": [
        # Term choice matters more here than anywhere else, because there
        # is no type to fall back on. Indonesian trading names use the local
        # word: a plant is "Pabrik ...", a warehouse is "Gudang ...".
        # "factory" and "warehouse" are not added -- Google's Indonesian
        # index resolves them to the same places, so they mostly buy
        # duplicate requests. "pergudangan" is worth its own query because
        # it names warehouse COMPLEXES, which "gudang" tends to miss.
        # Factories only, one query, one grid. Built to a hard constraint:
        # 121 cells x 1 search = 121 searches, so even if EVERY search pages
        # out to Google's 3-page maximum the run cannot exceed 363 requests.
        # That is the number that matters when the goal is "no charge" --
        # the estimate can be wrong, the ceiling cannot. Adding a second
        # query would put the worst case at 726 and reintroduce the risk.
        # The gap the pabrik sweep leaves. "pabrik" is a NAME query with no
        # type filter, so it finds factories called Pabrik-something and
        # misses ones called "PT Astra Otoparts". This is the mirror image:
        # includedType=manufacturer makes the TYPE the filter and lets the
        # text be near-neutral. Restricted to the 65 belt kecamatan, so the
        # worst case is 195 requests.
        dict(key="factory_typed", label="Factories by type (manufacturer)",
             grid="industrial_belt", expect=1500,
             searches=[("PT", "manufacturer")],
             basis="untested -- probe it with --limit-cells 3 before "
                   "committing the other 62"),
        dict(key="pabrik", label="Pabrik / factories only", grid="kecamatan",
             expect=4000,
             searches=[("pabrik", None)],
             basis="worst case 363 requests -- fits the free remainder with "
                   "room to spare, which is the point"),
        dict(key="industrial", label="Pabrik / gudang / pergudangan",
             grid="kecamatan", expect=6000,
             searches=[("pabrik", None), ("gudang", None),
                       ("pergudangan", None), ("kawasan industri", None)],
             basis="NO Google place type exists for factory or warehouse -- "
                   "text search only, so this is the least certain line"),
        # The estates themselves, by name. High precision and cheap: they
        # are concentrated in Bekasi and Karawang, so 11 kabkot cells cover
        # them. Useful as territory anchors even before the factories.
        # Bekasi names verified against portalkawasanindustri.com's
        # Kemenperin-derived list; Karawang names are widely used but I
        # could not reach a primary source, so treat them as likely rather
        # than confirmed and add any this misses.
        dict(key="industrial_estates", label="Named industrial estates",
             grid="kabkot", expect=120,
             searches=[(n, None) for n in (
                 "Kawasan Industri Jababeka", "MM2100 Industrial Town",
                 "Greenland International Industrial Center GIIC",
                 "Kawasan Industri Lippo Cikarang", "Delta Silicon",
                 "East Jakarta Industrial Park EJIP", "Kawasan Marunda Center",
                 "Kawasan Industri Terpadu Indonesia China KITIC",
                 "Bekasi International Industrial Estate Hyundai",
                 "Kawasan Industri Gobel Cibitung",
                 "Karawang International Industrial City KIIC",
                 "Suryacipta City of Industry", "Kawasan Industri Indotaisei",
                 "Kota Bukit Indah Industrial Park",
                 "Kawasan Industri Pulogadung JIEP",
                 "Kawasan Berikat Nusantara Cakung")],
             basis="16 named estates; the estate record itself, not its "
                   "tenants"),
        dict(key="manufacturer", label="Manufacturer / supplier (typed)",
             grid="kecamatan", expect=1500,
             searches=[("pabrik manufaktur", "manufacturer"),
                       ("supplier", "supplier")],
             basis="the only industrial-adjacent types in Table A"),
    ],
}


def all_targets():
    return {t["key"]: t for g in TARGETS.values() for t in g}


def grid_sizes(con):
    """Cell counts straight from the boundaries you uploaded."""
    out = {"aoi": 1}
    try:
        out["kecamatan"] = con.execute(
            "SELECT count(*) FROM geo_feature WHERE layer_key='kecamatan'"
        ).fetchone()[0] or 1
        out["industrial_belt"] = 65
        out["kabkot"] = con.execute(
            "SELECT count(DISTINCT kabkot) FROM ref_kecamatan "
            "WHERE kabkot IS NOT NULL").fetchone()[0] or 1
        out["mc"] = con.execute(
            "SELECT count(*) FROM geo_feature WHERE layer_key='indosat_mc'"
        ).fetchone()[0] or 1
    except sqlite3.OperationalError:
        out.update(kecamatan=121, kabkot=11, mc=24)
    return out


def plan_one(t, cells):
    """Requests = searches x pages-per-search.

    The first version of this pooled all expected places and only charged
    for paging once the total exceeded searches x 20. That is wrong, and it
    under-estimated a real sweep by 2.5x: paging happens PER SEARCH, so a
    dense cell burns its full three pages while a sparse one uses a single
    request, and the sparse cells cannot lend their unused pages to anybody.

    Charging every search for the pages its own share of the results needs
    reproduces the observed runs to within about 20%, biased high -- which
    is the right direction for something guarding a budget."""
    searches = cells * len(t["searches"])
    if not searches:
        return dict(cells=cells, searches=0, requests=0, capped=False,
                    reachable=0, per_request=0, lo=0, hi=0)
    fetched = t["expect"] * REFETCH_FACTOR
    per_search = fetched / searches
    pages = max(1.0, min(float(MAX_PAGES),
                         PAGES_INTERCEPT + per_search / PAGES_DIVISOR))
    requests = int(round(searches * pages))
    reachable = searches * PAGE_SIZE * MAX_PAGES
    return dict(cells=cells, searches=searches, requests=requests,
                capped=fetched > reachable, reachable=int(reachable),
                per_request=t["expect"] / requests,
                lo=searches, hi=searches * MAX_PAGES)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--group", action="append", choices=sorted(TARGETS))
    ap.add_argument("--expect", action="append", default=[],
                    metavar="key=N", help="override an expected place count")
    ap.add_argument("--price", type=float, default=32.00,
                    help="Text Search Pro per 1,000 requests")
    ap.add_argument("--free", type=int, default=5000,
                    help="free Pro requests per month (0 to ignore)")
    args = ap.parse_args()

    override = {}
    for spec in args.expect:
        k, _, v = spec.partition("=")
        try:
            override[k.strip()] = int(v)
        except ValueError:
            sys.exit(f"--expect wants key=NUMBER, got {spec!r}")

    con = db.connect() if db.db_exists() else None
    cells = grid_sizes(con) if con else {"aoi": 1, "kecamatan": 121,
                                         "kabkot": 11, "mc": 24}
    if con:
        con.close()

    print(f"\n  Grid from your own boundaries: "
          + "  ".join(f"{k}={v}" for k, v in sorted(cells.items())))
    print(f"  SKU: Text Search Pro @ ${args.price:.2f} / 1,000 requests "
          f"(one request returns up to {PAGE_SIZE} places)")
    print(f"  Field mask: {', '.join(f.replace('places.','') for f in gp.LEAN_FIELDS)}")

    groups = args.group or sorted(TARGETS)
    print(f"\n  {'target':<34}{'cells':>6}{'reqs':>8}{'range':>12}"
          f"{'places':>9}{'cost':>9}{'MB':>7}")
    print("  " + "-" * 82)
    tot_req = tot_places = tot_bytes = 0
    warnings = []
    for g in groups:
        for t in TARGETS[g]:
            t = dict(t)
            if t["key"] in override:
                t["expect"] = override[t["key"]]
            p = plan_one(t, cells[t["grid"]])
            cost = p["requests"] * args.price / 1000.0
            mb = t["expect"] * BYTES_PER_PLACE / 1e6
            tot_req += p["requests"]
            tot_places += t["expect"]
            tot_bytes += t["expect"] * BYTES_PER_PLACE
            flag = " !" if p["capped"] else ""
            print(f"  {t['label'][:33]:<34}{p['cells']:>6}{p['requests']:>8,}"
                  f"{p['lo']:>6,}-{p['hi']:<5,}{t['expect']:>9,}"
                  f"{cost:>9.2f}{mb:>7.1f}{flag}")
            if p["capped"]:
                warnings.append(
                    f"{t['key']}: expected {t['expect']:,} places but the "
                    f"{t['grid']} grid can only reach {p['reachable']:,} "
                    f"(3-page cap per search). Use a finer grid or split the "
                    f"query, or you will silently lose the remainder.")
    print("  " + "-" * 82)
    gross = tot_req * args.price / 1000.0
    billable = max(0, tot_req - args.free)
    net = billable * args.price / 1000.0
    print(f"  {'TOTAL':<34}{'':>6}{tot_req:>8,}{'':>12}{tot_places:>9,}"
          f"{gross:>9.2f}{tot_bytes/1e6:>7.1f}")
    print(f"\n  'range' is the floor (every search one page) and the ceiling\n"
          f"  (every search paging out to Google's 3-page limit).")

    print(f"\n  Gross cost                 ${gross:,.2f}")
    if args.free:
        print(f"  Less free Pro tier         -{min(tot_req, args.free):,} "
              f"requests/month (${min(tot_req, args.free)*args.price/1000:,.2f})")
        print(f"  NET COST                   ${net:,.2f}"
              + ("   <- the whole sweep fits inside one month's free tier"
                 if net == 0 else ""))
    print(f"  Data transferred           {tot_bytes/1e6:,.1f} MB uncompressed, "
          f"~{tot_bytes/1e6/3.5:,.1f} MB gzipped")
    print(f"  Places retrieved           {tot_places:,}")

    if warnings:
        print("\n  WARNINGS")
        for w in warnings:
            print(f"    - {w}")

    print("\n  Assumptions behind the place counts (the only soft numbers here):")
    for g in groups:
        for t in TARGETS[g]:
            e = override.get(t["key"], t["expect"])
            print(f"    {t['key']:<16}{e:>7,}  {t['basis']}")
    print("\n  Override any of them:  --expect industrial=12000\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
