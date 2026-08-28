#!/usr/bin/env python3
"""
Cross-source de-duplication.

The three connections describe the same city, so the same Alfamart appears
in poi_overture and poi_osm as two rows with nothing in common: Overture
uses GERS ids, OSM uses type/id, Google uses place ids. There is no shared
key to join on, so "the same shop" has to be *decided*, not looked up.

This script decides it and writes the decision down. It does NOT merge the
tables:

    poi_match     one row per candidate pair, with the evidence and verdict
    poi_cluster   which cluster each source row belongs to (union-find)
    poi_unified   a VIEW: one row per cluster, best source wins

Leaving the raw tables untouched matters for three reasons. Licences stay
separable, so ODbL-derived rows remain identifiable and strippable. A bad
threshold becomes a re-run rather than data loss. And every verdict stays
auditable — you can ask why two things were called the same and get an
answer with a distance and a name score attached.

    python reconcile.py --report          # decide nothing, just measure
    python reconcile.py                   # build matches, clusters, view
    python reconcile.py --within          # also collapse same-table dupes
    python reconcile.py --prefer poi_osm,poi_overture,poi_google
    python reconcile.py --max-metres 120 --match-at 0.75

Two passes run in order:

  1. EXACT. Overture rows whose own source record points at OSM
     ('OpenStreetMap' plus a record id like n123/w456). That is not a
     guess — it is Overture naming the OSM object it ingested. Needs the
     `source_record_id` column, which only exists if the parquet cache was
     built after that column was added; the script says so if it is absent.

  2. FUZZY. Distance plus name similarity, with hard vetoes on conflicting
     brand and conflicting class, then greedy one-to-one assignment so
     three OSM ATMs cannot all claim the same Overture ATM.
"""
import argparse
import csv
import math
import os
import re
import sqlite3
import sys
import time
from difflib import SequenceMatcher

import config
import db

TABLES = ["poi_overture", "poi_google", "poi_osm"]
DEFAULT_PRIORITY = ["poi_overture", "poi_google", "poi_osm"]

# Which source wins, PER CLASS. A single global order cannot be right when
# Google carries 15x the ATMs and 75x the BRILink agents while OSM is the
# only source that maps halte at all -- a global "Overture first" rule
# throws away the better record across most of retail and finance.
#
# Every line here is a measurement from this AOI, not a preference:
#   retail    Google 13,638 vs Overture 3,076   (3.2x on Alfamart alone)
#   atm       Google  8,639 vs Overture   553
#   bank      Google  4,829 vs Overture 3,202
#   gov       Google    430 vs Overture     0
#   industry  Google  1,648 vs Overture     0
#   rail/bus  OSM tags beat Overture's name-rescue; Overture has no
#             bus_stop category at all, so halte only exist in OSM
CLASS_PRIORITY = {
    "retail":   ["poi_google", "poi_overture", "poi_osm"],
    "mall":     ["poi_google", "poi_overture", "poi_osm"],
    "bank":     ["poi_google", "poi_overture", "poi_osm"],
    "atm":      ["poi_google", "poi_overture", "poi_osm"],
    # BRILink agents: Google 897 in territory against Overture's 12.
    "agent":    ["poi_google", "poi_overture", "poi_osm"],
    "gov":      ["poi_google", "poi_overture", "poi_osm"],
    "industry": ["poi_google", "poi_overture", "poi_osm"],
    "rail":     ["poi_osm", "poi_overture", "poi_google"],
    "bus":      ["poi_osm", "poi_overture", "poi_google"],
    "fuel":     ["poi_osm", "poi_overture", "poi_google"],
}
EXACT_PAIR = {"poi_overture", "poi_osm"}

# ── class: what kind of thing is this, coarsely ──────────────────────────
# Two rows of different class are never the same object, however close they
# sit. The ATM in a bank's lobby is a real, separate POI — folding it into
# the branch would quietly delete a visit target from a PJP route.
CLASS_BY_OVERTURE_CAT = {
    "bank_or_credit_union": "bank", "bank": "bank", "banks": "bank",
    "credit_union": "bank", "bank_credit_union": "bank",
    "commercial_bank": "bank", "savings_bank": "bank",
    "atm": "atm", "atms": "atm", "automated_teller_machine": "atm",
    "train_station": "rail", "trains": "rail", "metro_station": "rail",
    "light_rail_and_subway_stations": "rail", "railway_service": "rail",
    "subway_station": "rail",
    "bus_station": "bus", "bus_service": "bus", "bus_stop": "bus",
    "transit_station": "bus", "public_transportation": "bus",
    "convenience_store": "retail", "supermarket": "retail",
    "grocery_store": "retail",
    "gas_station": "fuel",
    # Sparse in Overture here, but mapping them means the catalogue reports
    # a real zero rather than "this source cannot express the class".
    "shopping_mall": "mall", "department_store": "mall",
    "local_government_office": "gov", "government_office": "gov",
    "manufacturer": "industry", "supplier": "industry",
}
CLASS_BY_GOOGLE_EXTRA = {
    "local_government_office": "gov", "city_hall": "gov",
    "government_office": "gov", "courthouse": "gov", "post_office": "gov",
    "manufacturer": "industry", "supplier": "industry",
    "wholesaler": "industry", "storage": "industry",
    "shopping_mall": "mall", "department_store": "mall",
    "asian_grocery_store": "retail", "discount_supermarket": "retail",
    "hypermarket": "retail", "store": "retail",
}
# The pull that fetched a Google row states its intent far more reliably
# than primary_type does: a BRILink agent is a warung and Google types it
# convenience_store, but target='brilink' says exactly what was sought.
# Checked before primary_type for that reason. Without this, 1,977 clusters
# came back unclassed -- 1,361 factories and 406 BRILink agents -- and fell
# through to the default order, handing BRILink to the source with 12
# records over the one with 897.
CLASS_BY_GOOGLE_TARGET = {
    "alfamart": "retail", "indomaret": "retail", "alfamidi": "retail",
    "stores_other": "retail", "malls": "mall",
    "atms": "atm", "bank_branches": "bank", "bank_hq": "bank",
    "brilink": "agent",
    "gov_kelurahan": "gov", "gov_kecamatan": "gov", "gov_kabkot": "gov",
    "gov_provinsi": "gov",
    "pabrik": "industry", "factory_typed": "industry",
    "industrial": "industry", "manufacturer": "industry",
    "industrial_estates": "industry",
}
CLASS_BY_OSM_TARGET = {
    "rail_stations": "rail", "bus_stops": "bus", "bus_terminals": "bus",
    "bank_branches": "bank", "atms": "atm", "minimarkets": "retail",
    "fuel_stations": "fuel",
}
CLASS_BY_OSM_TAG = {
    "amenity=bank": "bank", "amenity=atm": "atm", "amenity=fuel": "fuel",
    "amenity=bus_station": "bus", "highway=bus_stop": "bus",
    "public_transport=platform": "bus", "public_transport=station": "bus",
    "railway=station": "rail", "railway=halt": "rail",
    "station=subway": "rail", "station=light_rail": "rail",
    "shop=convenience": "retail", "shop=supermarket": "retail",
    "shop=mall": "mall", "shop=department_store": "mall",
    "amenity=townhall": "gov", "office=government": "gov",
    "man_made=works": "industry", "landuse=industrial": "industry",
}
CLASS_BY_GOOGLE_TYPE = {
    "bank": "bank", "atm": "atm", "train_station": "rail",
    "subway_station": "rail", "light_rail_station": "rail",
    "transit_station": "rail", "bus_station": "bus", "bus_stop": "bus",
    "convenience_store": "retail", "supermarket": "retail",
    "grocery_store": "retail", "gas_station": "fuel",
}

# Words that say nothing about *which* branch this is. Stripping them stops
# "Bank BCA KCP Sudirman" and "BCA Sudirman" from looking like strangers.
NOISE_TOKENS = {
    "pt", "cv", "tbk", "persero", "the", "and", "dan", "di",
    "kantor", "cabang", "pembantu", "kas", "unit", "pusat",
    "kcp", "kcu", "kck", "kc", "kk", "cab",
    "atm", "cdm", "setor", "tunai", "anjungan",
    "stasiun", "station", "st", "halte", "terminal", "shelter",
    "jl", "jalan", "no", "nomor", "raya", "km",
    "toko", "minimarket", "swalayan",
}
SPLIT_RX = re.compile(r"[^a-z0-9]+")


def norm_tokens(name):
    if not name:
        return []
    low = str(name).lower().replace("&", " dan ")
    parts = [p for p in SPLIT_RX.split(low) if p]
    keep = [p for p in parts if p not in NOISE_TOKENS]
    # If a name is *entirely* noise ("ATM Center"), keep the raw tokens
    # rather than comparing two empty sets and calling them identical.
    return keep or parts


def name_similarity(a, b):
    """None when either side is unnamed — that is missing evidence, not
    evidence of difference, and the caller treats it accordingly."""
    ta, tb = norm_tokens(a), norm_tokens(b)
    if not ta or not tb:
        return None
    sa, sb = set(ta), set(tb)
    jaccard = len(sa & sb) / len(sa | sb)
    ratio = SequenceMatcher(None, " ".join(ta), " ".join(tb)).ratio()
    # max, not mean: these two fail in opposite directions. Jaccard ignores
    # word order but punishes extra words hard; the ratio does the reverse.
    # Either one being convinced is enough.
    return max(jaccard, ratio)


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * r * math.asin(math.sqrt(a))


def klass(table, row):
    if table == "poi_osm":
        return (CLASS_BY_OSM_TARGET.get(row.get("target"))
                or CLASS_BY_OSM_TAG.get(row.get("category")))
    if table == "poi_google":
        return (CLASS_BY_GOOGLE_TARGET.get(row.get("target"))
                or CLASS_BY_GOOGLE_TYPE.get(row.get("category"))
                or CLASS_BY_GOOGLE_EXTRA.get(row.get("category")))
    return CLASS_BY_OVERTURE_CAT.get(row.get("category"))


# ── loading ──────────────────────────────────────────────────────────────
def load(con, table):
    try:
        cols = {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.OperationalError:
        return []
    if not cols:
        return []
    want = ["source_id", "name", "brand_resolved", "category", "lat", "lon"]
    want += [c for c in ("target", "dataset", "source_record_id") if c in cols]
    return [dict(r) for r in con.execute(
        f"SELECT {', '.join(want)} FROM {table} "
        f"WHERE lat IS NOT NULL AND lon IS NOT NULL")]


# ── pass 1: exact link via Overture's own OSM source record ──────────────
OSM_REC_RX = re.compile(r"^([nwr])(\d+)$")
OSM_KIND = {"n": "node", "w": "way", "r": "relation"}


def exact_links(ovt_rows, osm_rows):
    """Overture keeps the id of the record it ingested. Where that record is
    an OSM object the join is exact, and no similarity maths is needed."""
    by_osm_id = {r["source_id"]: r for r in osm_rows}
    out, have_col = [], False
    for a in ovt_rows:
        if "source_record_id" not in a:
            continue
        have_col = True
        rec = a.get("source_record_id")
        if not rec:
            continue
        dataset = (a.get("dataset") or "").lower()
        if "openstreetmap" not in dataset and "osm" not in dataset:
            continue
        m = OSM_REC_RX.match(str(rec).strip())
        if not m:
            continue
        b = by_osm_id.get(f"{OSM_KIND[m.group(1)]}/{m.group(2)}")
        if b:
            out.append((a, b, haversine_m(a["lat"], a["lon"],
                                          b["lat"], b["lon"])))
    return out, have_col


# ── pass 2: fuzzy ────────────────────────────────────────────────────────
def index_by_cell(rows, cell_deg):
    grid = {}
    for r in rows:
        k = (int(math.floor(r["lat"] / cell_deg)),
             int(math.floor(r["lon"] / cell_deg)))
        grid.setdefault(k, []).append(r)
    return grid


def score(a, b, table_a, table_b, max_m):
    """Returns (verdict, score, distance_m, name_sim). verdict is 'no' for a
    hard veto, 'review' for a forced-manual case, or None to mean 'let the
    score decide'. Vetoes come first and are absolute."""
    dist = haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
    if dist > max_m:
        return "no", 0.0, dist, None

    ba, bb = a.get("brand_resolved"), b.get("brand_resolved")
    if ba and bb and ba != bb:
        return "no", 0.0, dist, None            # Alfamart is not Indomaret

    ka, kb = klass(table_a, a), klass(table_b, b)
    if ka and kb and ka != kb:
        return "no", 0.0, dist, None            # an ATM is not its branch

    sim = name_similarity(a.get("name"), b.get("name"))
    if sim is None:
        # Unnamed on one side — common for OSM ATMs and bus stops. Only
        # near-coincident points of the same known class qualify, and they
        # go to review rather than being merged on distance alone.
        if dist <= 25 and ka and kb and ka == kb:
            return "review", 0.50, dist, None
        return "no", 0.0, dist, None

    # Name carries more weight than distance, and deliberately so. Two
    # sources rarely agree on where a shop's centroid is — 30-60 m apart is
    # ordinary — but they do agree on what it is called. Weighting distance
    # higher made "Stasiun Manggarai" 60 m from "Stasiun Manggarai" score
    # below the merge line, which is plainly the wrong answer. Distance now
    # decides only when the name is weak, which is what it is good for.
    ds = 1.0 - (dist / max_m)
    same_brand = bool(ba and bb and ba == bb)
    sc = 0.35 * ds + 0.55 * sim + 0.10 * (1.0 if same_brand else 0.0)
    return None, sc, dist, sim


def fuzzy_pairs(rows_a, rows_b, table_a, table_b, max_m, match_at, review_at,
                skip_a=(), skip_b=(), same_table=False):
    cell = max_m / 111_320.0
    grid = index_by_cell([r for r in rows_b if r["source_id"] not in skip_b],
                         cell)
    pairs = []
    for a in rows_a:
        if a["source_id"] in skip_a:
            continue
        ci = int(math.floor(a["lat"] / cell))
        cj = int(math.floor(a["lon"] / cell))
        for di in (-1, 0, 1):
            for dj in (-1, 0, 1):
                for b in grid.get((ci + di, cj + dj), ()):
                    if same_table and a["source_id"] >= b["source_id"]:
                        continue        # self, and each pair only once
                    verdict, sc, dist, sim = score(a, b, table_a, table_b,
                                                   max_m)
                    if verdict == "no":
                        continue
                    if verdict is None:
                        verdict = ("match" if sc >= match_at
                                   else "review" if sc >= review_at else "no")
                    if verdict == "no":
                        continue
                    pairs.append({"a": a, "b": b, "score": sc, "dist": dist,
                                  "sim": sim, "verdict": verdict})
    # Greedy one-to-one: the best evidence claims its partner first. Without
    # this a row with four near neighbours matches all four, and the cluster
    # swallows genuinely distinct shops.
    pairs.sort(key=lambda p: -p["score"])
    used_a, used_b, accepted, review = set(), set(), [], []
    for p in pairs:
        ia, ib = p["a"]["source_id"], p["b"]["source_id"]
        if p["verdict"] == "match":
            if ia in used_a or ib in used_b:
                continue
            used_a.add(ia)
            used_b.add(ib)
            accepted.append(p)
        else:
            review.append(p)
    review = [p for p in review
              if p["a"]["source_id"] not in used_a
              and p["b"]["source_id"] not in used_b]
    return accepted, review


# ── union-find ───────────────────────────────────────────────────────────
class Union:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, x, y):
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            self.parent[ry] = rx


# ── the unified view ─────────────────────────────────────────────────────
VIEW_COLS = ["name", "brand_resolved", "category", "lat", "lon", "address",
             "locality", "region", "adm_kelurahan", "adm_kecamatan",
             "adm_kota", "adm_provinsi"]


def choose(members, fallback_priority):
    """One winner per cluster, by the priority for that POI's class.

    `members` is [(table, row)]. Returns (table, row, cls, reason).

    Class is taken from whichever member declares one -- a cluster is by
    definition one real place, so any member's class describes all of them.
    Where nothing declares a class, the caller's --prefer order applies.
    """
    cls = next((klass(t, r) for t, r in members if klass(t, r)), None)
    order = CLASS_PRIORITY.get(cls) or list(fallback_priority)
    order = order + [t for t in TABLES if t not in order]
    rank = {t: i for i, t in enumerate(order)}
    # Tie-break within a source on having a name at all, then on the id, so
    # the same input always produces the same winner.
    table, row = min(members,
                     key=lambda m: (rank.get(m[0], 99),
                                    0 if (m[1].get("name") or "").strip() else 1,
                                    str(m[1].get("source_id"))))
    reason = (f"class={cls or 'unknown'} -> {order[0].replace('poi_', '')}"
              if len(members) > 1 else "sole source")
    return table, row, cls, reason


VIEW_COLS = ["name", "brand_resolved", "category", "lat", "lon", "address",
             "locality", "region", "adm_kelurahan", "adm_kecamatan",
             "adm_kota", "adm_provinsi", "mc_ioh"]


def write_picks(con, cluster_of, rows, priority):
    """Materialise the winner per cluster into poi_cluster_pick."""
    by_cluster = {}
    for (table, sid), cid in cluster_of.items():
        by_cluster.setdefault(cid, []).append((table, sid))
    index = {(t, r["source_id"]): r for t in TABLES for r in rows[t]}

    picks = []
    for cid, members in by_cluster.items():
        full = [(t, index[(t, sid)]) for t, sid in members if (t, sid) in index]
        if not full:
            continue
        table, row, cls, reason = choose(full, priority)
        picks.append((cid, table, row["source_id"], cls, len(full), reason))
    con.execute("DELETE FROM poi_cluster_pick")
    con.executemany(
        "INSERT INTO poi_cluster_pick (cluster_id,table_name,source_id,cls,"
        "n_members,reason) VALUES (?,?,?,?,?,?)", picks)
    con.commit()
    return picks


def build_view(con):
    """poi_unified is now a thin join onto poi_cluster_pick. The choosing
    happens in Python, where the class maps already live; duplicating them
    in SQL would guarantee the two drift apart."""
    parts = []
    for t in TABLES:
        try:
            cols = {r["name"] for r in con.execute(f"PRAGMA table_info({t})")}
        except sqlite3.OperationalError:
            continue
        if not cols:
            continue
        sel = ", ".join(c if c in cols else f"NULL AS {c}" for c in VIEW_COLS)
        parts.append(f"SELECT '{t}' AS tbl, source_id, {sel} FROM {t}")
    if not parts:
        return False
    con.execute("DROP VIEW IF EXISTS poi_unified")
    con.execute(f"""
        CREATE VIEW poi_unified AS
        WITH allrows AS ({' UNION ALL '.join(parts)})
        SELECT p.cluster_id,
               replace(p.table_name, 'poi_', '') AS source,
               p.source_id, {', '.join(VIEW_COLS)},
               p.cls AS poi_class, p.reason,
               -- n_members counts every raw record folded in, including
               -- same-source duplicates found by --within. n_sources counts
               -- DISTINCT sources, which is the corroboration signal. They
               -- are different numbers and conflating them overstates how
               -- many places two sources actually agreed on.
               p.n_members,
               (SELECT count(DISTINCT c2.table_name) FROM poi_cluster c2
                 WHERE c2.cluster_id = p.cluster_id) AS n_sources,
               (SELECT group_concat(DISTINCT replace(c3.table_name,'poi_',''))
                  FROM poi_cluster c3
                 WHERE c3.cluster_id = p.cluster_id) AS sources_seen
        FROM poi_cluster_pick p
        JOIN allrows a
          ON a.tbl = p.table_name AND a.source_id = p.source_id""")
    con.commit()
    return True


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-metres", type=float, default=150.0,
                    help="never pair rows further apart than this")
    ap.add_argument("--match-at", type=float, default=0.72,
                    help="score at or above which a pair is merged")
    ap.add_argument("--review-at", type=float, default=0.55,
                    help="score at or above which a pair is flagged instead")
    ap.add_argument("--prefer", default=",".join(DEFAULT_PRIORITY),
                    help="source priority for poi_unified, best first")
    ap.add_argument("--within", action="store_true",
                    help="also collapse duplicates inside a single table")
    ap.add_argument("--report", action="store_true",
                    help="measure and print, write nothing")
    ap.add_argument("--no-exact", action="store_true",
                    help="skip the Overture->OSM record-id pass")
    args = ap.parse_args()

    priority = [t.strip() for t in args.prefer.split(",") if t.strip()]
    bad = [t for t in priority if t not in TABLES]
    if bad:
        sys.exit(f"--prefer: unknown table(s) {bad}. Pick from {TABLES}.")
    priority += [t for t in TABLES if t not in priority]

    if not db.db_exists():
        sys.exit("No database — run migrate.py and at least one pull first.")
    con = db.connect()

    rows = {t: load(con, t) for t in TABLES}
    for t in TABLES:
        log(f"{t:<14}{len(rows[t]):>8,} rows with coordinates")
    live = [t for t in TABLES if rows[t]]
    if len(live) < 2 and not args.within:
        sys.exit("Need two populated tables to de-duplicate across sources, "
                 "or pass --within to look inside one.")

    run_id = None
    if not args.report:
        run_id = db.start_run(con, "reconcile", {
            "max_metres": args.max_metres, "match_at": args.match_at,
            "review_at": args.review_at, "prefer": priority,
            "within": args.within})

    uf = Union()
    all_matches, all_review = [], []
    exact_skip = {t: set() for t in TABLES}

    # pass 1 — exact
    exact_n = 0
    if not args.no_exact and rows["poi_overture"] and rows["poi_osm"]:
        links, have_col = exact_links(rows["poi_overture"], rows["poi_osm"])
        if not have_col:
            log("NOTE  poi_overture has no source_record_id column, so the "
                "exact Overture->OSM link is unavailable. Re-run "
                "`pull_overture.py --all --refresh` to populate it, then "
                "reconcile again for higher precision.")
        for a, b, dist in links:
            all_matches.append({"a": a, "b": b, "ta": "poi_overture",
                                "tb": "poi_osm", "score": 1.0, "dist": dist,
                                "sim": None, "verdict": "exact"})
            uf.union(("poi_overture", a["source_id"]),
                     ("poi_osm", b["source_id"]))
            exact_skip["poi_overture"].add(a["source_id"])
            exact_skip["poi_osm"].add(b["source_id"])
        exact_n = len(links)
        if exact_n:
            log(f"exact link via Overture's OSM source record: "
                f"{exact_n:,} pairs")

    # pass 2 — fuzzy, over every table pair
    for i, ta in enumerate(live):
        for tb in live[i + 1:]:
            # Only skip rows already linked *within this same pair*. A row
            # exact-linked to OSM must still be free to match Google.
            ex = {ta, tb} == EXACT_PAIR
            acc, rev = fuzzy_pairs(
                rows[ta], rows[tb], ta, tb, args.max_metres, args.match_at,
                args.review_at,
                skip_a=exact_skip[ta] if ex else (),
                skip_b=exact_skip[tb] if ex else ())
            for p in acc:
                p.update(ta=ta, tb=tb, verdict="match")
                uf.union((ta, p["a"]["source_id"]), (tb, p["b"]["source_id"]))
            for p in rev:
                p.update(ta=ta, tb=tb)
            all_matches.extend(acc)
            all_review.extend(rev)
            log(f"{ta.replace('poi_', ''):>9} <-> {tb.replace('poi_', ''):<9}"
                f"{len(acc):>7,} merged   {len(rev):>6,} for review")

    # optional pass 3 — duplicates inside one source
    if args.within:
        for t in live:
            acc, rev = fuzzy_pairs(rows[t], rows[t], t, t, args.max_metres,
                                   args.match_at, args.review_at,
                                   same_table=True)
            for p in acc:
                p.update(ta=t, tb=t, verdict="match")
                uf.union((t, p["a"]["source_id"]), (t, p["b"]["source_id"]))
            for p in rev:
                p.update(ta=t, tb=t)
            all_matches.extend(acc)
            all_review.extend(rev)
            log(f"{t.replace('poi_', ''):>9} internal {len(acc):>7,} merged"
                f"   {len(rev):>6,} for review")

    # clusters: every row gets one, matched or not
    cluster_of, roots, next_id = {}, {}, 1
    for t in TABLES:
        for r in rows[t]:
            key = (t, r["source_id"])
            root = uf.find(key) if key in uf.parent else key
            if root not in roots:
                roots[root] = next_id
                next_id += 1
            cluster_of[key] = roots[root]

    total_rows = sum(len(rows[t]) for t in TABLES)
    n_clusters = len(roots)
    dupes = total_rows - n_clusters
    pct = (dupes / total_rows * 100) if total_rows else 0.0

    print(f"\n  {total_rows:,} source rows  ->  {n_clusters:,} distinct places")
    print(f"  {dupes:,} duplicate rows collapsed ({pct:.1f}% of the raw total)")
    print(f"  {exact_n:,} by exact id, "
          f"{len(all_matches) - exact_n:,} by distance + name")
    print(f"  {len(all_review):,} borderline pairs flagged, not merged")

    if args.report:
        print("\n  --report: nothing written. Drop it to build poi_match, "
              "poi_cluster and poi_unified.")
        con.close()
        return 0

    con.execute("DELETE FROM poi_match")
    con.execute("DELETE FROM poi_cluster")
    con.executemany(
        "INSERT INTO poi_match (a_table,a_id,b_table,b_id,distance_m,"
        "name_sim,score,verdict,decided_utc,pull_run_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        [(p["ta"], p["a"]["source_id"], p["tb"], p["b"]["source_id"],
          round(p["dist"], 1), p["sim"], round(p["score"], 4), p["verdict"],
          db.utcnow(), run_id) for p in all_matches + all_review])
    con.executemany(
        "INSERT INTO poi_cluster (cluster_id,table_name,source_id) "
        "VALUES (?,?,?)",
        [(cid, t, sid) for (t, sid), cid in cluster_of.items()])
    con.commit()

    write_picks(con, cluster_of, rows, priority)
    if build_view(con):
        n = con.execute("SELECT count(*) FROM poi_unified").fetchone()[0]
        multi = con.execute(
            "SELECT count(*) FROM poi_unified WHERE n_sources > 1"
        ).fetchone()[0]
        dupes = con.execute(
            "SELECT count(*) FROM poi_unified WHERE n_members > 1").fetchone()[0]
        print(f"\n  poi_unified: {n:,} rows "
              f"({multi:,} corroborated by more than one SOURCE; "
              f"{dupes:,} folded in more than one raw record)")
        print(f"  raw tables are untouched — {total_rows:,} source rows remain "
              f"queryable in full")
        print("\n  winner by class:")
        for r in con.execute(
                "SELECT coalesce(cls,'(unclassed)') c, table_name, count(*) n "
                "FROM poi_cluster_pick GROUP BY 1,2 ORDER BY 1, n DESC"):
            print(f"    {r[0]:<12}{r[1].replace('poi_',''):<10}{r[2]:>8,}")

    if all_review:
        path = os.path.join(
            config.EXPORT_DIR,
            f"review-pairs-{time.strftime('%Y%m%d-%H%M%S')}.csv")
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["score", "distance_m", "name_sim", "a_table", "a_id",
                        "a_name", "a_category", "b_table", "b_id", "b_name",
                        "b_category"])
            for p in sorted(all_review, key=lambda x: -x["score"]):
                w.writerow([round(p["score"], 4), round(p["dist"], 1),
                            None if p["sim"] is None else round(p["sim"], 3),
                            p["ta"], p["a"]["source_id"], p["a"].get("name"),
                            p["a"].get("category"), p["tb"],
                            p["b"]["source_id"], p["b"].get("name"),
                            p["b"].get("category")])
        print(f"  review queue -> exports/{os.path.basename(path)}")
        print("  These are the pairs the rules would not call either way. "
              "Skim the top of the file; if most are real duplicates, lower "
              "--match-at and re-run.")

    db.finish_run(con, run_id, "ok", rows_written=len(cluster_of),
                  api_calls=0, est_cost_usd=0.0,
                  notes=f"clusters={n_clusters} merged={dupes}")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
