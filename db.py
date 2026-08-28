"""
SQLite layer. One database, four tables, three of them one-per-connection.

    poi_overture   rows pulled from Overture Maps   (natural key: GERS id)
    poi_google     rows pulled from Google Places   (natural key: place ID)
    poi_osm        rows pulled from OSM / Overpass  (natural key: type/id)
    pull_run       an audit row for every ingestion of any connection

The POI tables are never merged. Keeping them apart means you can diff
the connections against each other, re-pull one without touching the others,
and drop a table wholesale when its licence says so — Google caps
non-ID fields at 30 days, and OSM's ODbL share-alike must not leak into a
redistributed derived database.

This database is entirely separate from the IM3 / 3ID store systems — it
reads nothing from them and writes nothing to them.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone

import config

# Columns shared by both POI tables, so exports and the UI can treat them
# uniformly. Source-specific columns are appended per table.
COMMON_COLUMNS = [
    "source", "source_id", "name", "brand_resolved", "category",
    "lat", "lon", "address", "locality", "region", "phone", "website",
    "pull_run_id", "first_seen_utc", "last_seen_utc",
]

OVERTURE_EXTRA = ["brand_name", "brand_qid", "basic_category", "confidence",
                  "operating_status", "dataset", "source_updated"]
GOOGLE_EXTRA = ["primary_type", "types", "business_status", "kelurahan",
                "kecamatan", "kota", "provinsi", "rating", "user_ratings",
                "brand_verified", "maps_uri"]
OSM_EXTRA = ["osm_type", "osm_id", "brand_name", "brand_qid", "target",
             "operator", "network", "ref", "opening_hours", "osm_version",
             "source_updated", "tags_json"]


def connect(path=None):
    con = sqlite3.connect(path or config.DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=ON")
    return con


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ── pull_run bookkeeping ─────────────────────────────────────────────────
def start_run(con, source, params):
    cur = con.execute(
        "INSERT INTO pull_run (source, started_utc, status, params_json) "
        "VALUES (?,?,?,?)",
        (source, utcnow(), "running", json.dumps(params, default=str)))
    con.commit()
    return cur.lastrowid


def finish_run(con, run_id, status, rows_written=0, api_calls=0,
               est_cost_usd=0.0, notes=""):
    con.execute(
        "UPDATE pull_run SET finished_utc=?, status=?, rows_written=?, "
        "api_calls=?, est_cost_usd=?, notes=? WHERE id=?",
        (utcnow(), status, rows_written, api_calls, est_cost_usd, notes, run_id))
    con.commit()


def runs(con, limit=25):
    return [dict(r) for r in con.execute(
        "SELECT * FROM pull_run ORDER BY id DESC LIMIT ?", (limit,))]


# ── upsert ───────────────────────────────────────────────────────────────
def upsert(con, table, rows, run_id):
    """Insert or update on the natural key. Re-pulling never duplicates:
    first_seen_utc is preserved, last_seen_utc advances. That gives you a
    free record of how long a POI has been in the data."""
    if not rows:
        return 0
    now = utcnow()
    cols = [c for c in rows[0].keys()]
    for c in ("pull_run_id", "first_seen_utc", "last_seen_utc"):
        if c not in cols:
            cols.append(c)

    placeholders = ",".join("?" for _ in cols)
    updatable = [c for c in cols if c not in ("source_id", "first_seen_utc")]
    setters = ",".join(f"{c}=excluded.{c}" for c in updatable)
    sql = (f"INSERT INTO {table} ({','.join(cols)}) VALUES ({placeholders}) "
           f"ON CONFLICT(source_id) DO UPDATE SET {setters}")

    payload = []
    for r in rows:
        r = dict(r)
        r["pull_run_id"] = run_id
        r.setdefault("first_seen_utc", now)
        r["last_seen_utc"] = now
        payload.append([r.get(c) for c in cols])

    con.executemany(sql, payload)
    con.commit()
    return len(payload)


# ── reads for the UI / export ────────────────────────────────────────────
def table_columns(con, table):
    return [r["name"] for r in con.execute(f"PRAGMA table_info({table})")]


def query(con, table, category=None, brand=None, name_like=None, limit=5000):
    where, args = [], []
    if category:
        where.append("category = ?")
        args.append(category)
    if brand:
        where.append("brand_resolved = ?")
        args.append(brand)
    if name_like:
        where.append("lower(name) LIKE ?")
        args.append(f"%{name_like.lower()}%")
    sql = f"SELECT * FROM {table}"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY coalesce(brand_resolved, name) LIMIT ?"
    args.append(limit)
    return [dict(r) for r in con.execute(sql, args)]


def counts(con):
    out = {}
    for t in ("poi_overture", "poi_google", "poi_osm", "poi_unified"):
        try:
            out[t] = con.execute(f"SELECT count(*) c FROM {t}").fetchone()["c"]
        except sqlite3.OperationalError:
            out[t] = None          # table not migrated yet
    return out


def db_exists():
    return os.path.exists(config.DB_PATH)
