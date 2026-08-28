"""
The catalogue: one place that defines what can be selected, and what each
selection means in each source.

Everything the three menus offer — the source list, the category dropdown,
the insight counters — is described here and nowhere else. The per-source
SQL is *derived* from the class maps in reconcile.py rather than restated,
because a second copy of "which Overture categories are banks" would drift
from the first within a week.
"""
import sqlite3

import reconcile as rc

# ── sources ──────────────────────────────────────────────────────────────
SOURCES = {
    "unified":  {"table": "poi_unified",  "label": "Unified (deduplicated)",
                 "color": "#f0d04b", "raw": False,
                 "note": "one row per real place, winner chosen per category"},
    "overture": {"table": "poi_overture", "label": "Overture (raw)",
                 "color": "#7c5cff", "raw": True,
                 "note": "CDLA-Permissive · free · every record as pulled"},
    "google":   {"table": "poi_google",   "label": "Google (raw)",
                 "color": "#4b96f3", "raw": True,
                 "note": "Place IDs storable; other fields 30-day cache"},
    "osm":      {"table": "poi_osm",      "label": "OSM (raw)",
                 "color": "#6ec46e", "raw": True,
                 "note": "ODbL share-alike · © OpenStreetMap contributors"},
}
DEFAULT_SOURCE = "unified"


def table_for(source):
    return SOURCES.get(source, SOURCES[DEFAULT_SOURCE])["table"]


# ── classes → per-source SQL, derived from the reconciler ────────────────
def _in(col, values):
    if not values:
        return None
    quoted = ", ".join("'" + str(v).replace("'", "''") + "'" for v in values)
    return f"{col} IN ({quoted})"


def _or(*parts):
    parts = [p for p in parts if p]
    if not parts:
        return None
    return "(" + " OR ".join(parts) + ")"


def class_sql(cls, source):
    """The WHERE fragment selecting one POI class out of one source, or None
    when that source cannot express the class at all."""
    table = table_for(source)
    if table == "poi_unified":
        return f"poi_class = '{cls}'"
    if table == "poi_overture":
        return _in("category",
                   [k for k, v in rc.CLASS_BY_OVERTURE_CAT.items() if v == cls])
    if table == "poi_google":
        types = {**rc.CLASS_BY_GOOGLE_TYPE, **rc.CLASS_BY_GOOGLE_EXTRA}
        return _or(
            _in("primary_type", [k for k, v in types.items() if v == cls]),
            _in("target",
                [k for k, v in rc.CLASS_BY_GOOGLE_TARGET.items() if v == cls]))
    if table == "poi_osm":
        return _or(
            _in("category",
                [k for k, v in rc.CLASS_BY_OSM_TAG.items() if v == cls]),
            _in("target",
                [k for k, v in rc.CLASS_BY_OSM_TARGET.items() if v == cls]))
    return None


# ── insight counters ─────────────────────────────────────────────────────
# Brand buckets count on brand_resolved, which is the one brand field that
# means the same thing in all three tables. Class buckets go through
# class_sql above. A bucket a source cannot express is reported as
# unavailable rather than as zero — those are different facts.
BUCKETS = [
    {"key": "alfamart",  "label": "Alfamart",        "brand": "Alfamart"},
    {"key": "indomaret", "label": "Indomaret",       "brand": "Indomaret"},
    {"key": "alfamidi",  "label": "Alfamidi",        "brand": "Alfamidi"},
    {"key": "retail",    "label": "All minimarket",  "cls": "retail"},
    {"key": "bank",      "label": "Bank branches",   "cls": "bank"},
    {"key": "atm",       "label": "ATMs",            "cls": "atm"},
    {"key": "agent",     "label": "Agen BRILink",    "brand": "Agen BRILink"},
    {"key": "rail",      "label": "Rail stations",   "cls": "rail"},
    {"key": "bus",       "label": "Bus & halte",     "cls": "bus"},
    {"key": "mall",      "label": "Malls",           "cls": "mall"},
    {"key": "gov",       "label": "Gov offices",     "cls": "gov"},
    {"key": "industry",  "label": "Factories",       "cls": "industry"},
    {"key": "fuel",      "label": "SPBU",            "cls": "fuel"},
]
BUCKET_BY_KEY = {b["key"]: b for b in BUCKETS}


def bucket_sql(bucket, source):
    if bucket.get("brand"):
        b = bucket["brand"].replace("'", "''")
        return f"brand_resolved = '{b}'"
    if bucket.get("cls"):
        return class_sql(bucket["cls"], source)
    return None


# ── live facets ──────────────────────────────────────────────────────────
def _cols(con, table):
    try:
        return {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.OperationalError:
        return set()


def facets(con, source, in_territory=True):
    """The dropdown contents for one source, counted from that source only.

    This is the fix for the category list showing Overture's taxonomy while
    Google was selected: the options now come from the table being read, so
    an option that returns nothing cannot appear.
    """
    table = table_for(source)
    cols = _cols(con, table)
    if not cols:
        return {"ok": False, "error": f"{table} does not exist yet",
                "categories": [], "brands": [], "classes": []}
    where = " WHERE adm_kecamatan IS NOT NULL" if (
        in_territory and "adm_kecamatan" in cols) else ""
    catcol = "category"

    def group(col):
        if col not in cols:
            return []
        w = where or " WHERE 1=1"
        return [{"value": r[0], "count": r[1]} for r in con.execute(
            f"SELECT {col}, count(*) n FROM {table}{w} AND {col} IS NOT NULL "
            f"AND {col} <> '' GROUP BY 1 ORDER BY n DESC LIMIT 300")]

    return {
        "ok": True,
        "source": source,
        "table": table,
        "categories": group(catcol),
        "brands": group("brand_resolved"),
        "classes": group("poi_class") if "poi_class" in cols else [],
        "kota": group("adm_kota"),
        "mc": group("mc_ioh"),
    }


def counts(con, source, extra_where=None, in_territory=True):
    """One number per bucket for the source in view."""
    table = table_for(source)
    cols = _cols(con, table)
    out = []
    if not cols:
        return out
    base = []
    if in_territory and "adm_kecamatan" in cols:
        base.append("adm_kecamatan IS NOT NULL")
    if extra_where:
        base.append(extra_where)
    for b in BUCKETS:
        sql = bucket_sql(b, source)
        if not sql:
            out.append({**{k: b[k] for k in ("key", "label")},
                        "count": None, "available": False})
            continue
        clause = " AND ".join(base + [sql]) or "1=1"
        try:
            n = con.execute(
                f"SELECT count(*) FROM {table} WHERE {clause}").fetchone()[0]
        except sqlite3.OperationalError:
            n, sql = None, None
        out.append({**{k: b[k] for k in ("key", "label")},
                    "count": n, "available": n is not None})
    return out
