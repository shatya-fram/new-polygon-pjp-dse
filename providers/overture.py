"""
Overture Maps Places provider — live DuckDB query over the public S3 parquet.

No download step: httpfs + parquet predicate pushdown means only the row
groups whose bbox overlaps the AOI are fetched. Expect ~10-40s for a first
query (extension load + metadata), faster afterwards as DuckDB caches
metadata for the session.

Schema note: Overture is mid-migration from `categories` (deprecated,
removal slated for Sept 2026) to `basic_category` + `taxonomy`. This module
probes the release's actual columns once and builds the SQL accordingly, so
it keeps working on either side of that cut.
"""
import threading

import duckdb

import config
from config import BRAND_RESOLVE_ORDER, BRANDS, CATEGORIES

_conn = None
_lock = threading.Lock()
_columns_cache = {}
_has_spatial = False


class OvertureError(RuntimeError):
    pass


def _dataset_path():
    return (f"{config.OVERTURE_S3_BASE}/{config.OVERTURE_RELEASE}"
            f"/theme=places/type=place/*")


def _try_extension(con, name):
    """Return True if the extension is usable. INSTALL may legitimately fail
    (already installed, or no route to extensions.duckdb.org behind a
    corporate proxy) — what matters is whether LOAD works afterwards."""
    try:
        con.execute(f"INSTALL {name}")
    except Exception:
        pass
    try:
        con.execute(f"LOAD {name}")
        return True
    except Exception:
        return False


def _connect():
    global _conn, _has_spatial
    if _conn is not None:
        return _conn
    con = duckdb.connect(database=":memory:")

    # spatial is optional — without it we read coordinates off the bbox
    # struct instead of decoding WKB (see _geom_exprs).
    _has_spatial = _try_extension(con, "spatial")

    # httpfs is mandatory: no s3:// without it.
    if not _try_extension(con, "httpfs"):
        raise OvertureError(
            "DuckDB could not load the `httpfs` extension, so s3:// is "
            "unreachable. This is almost always a blocked route to "
            "extensions.duckdb.org. Fix: run `duckdb -c \"INSTALL httpfs; "
            "INSTALL spatial;\"` once on a machine with open egress, or set "
            "OVERTURE_S3_BASE to a locally downloaded copy of the release."
        )

    for stmt in (
        "SET s3_region='us-west-2'",
        f"SET memory_limit='{config.DUCKDB_MEMORY_LIMIT}'",
        f"SET threads={config.DUCKDB_THREADS}",
    ):
        try:
            con.execute(stmt)
        except Exception as exc:
            raise OvertureError(f"DuckDB setup failed on `{stmt}`: {exc}")

    _conn = con
    return _conn


def _geom_exprs():
    """(lon_expr, lat_expr, precision_note).

    Overture places are points, so bbox.xmin/ymin already hold the exact
    coordinate — just at float32 precision (~1 m here). Decoding the WKB via
    the spatial extension is exact, so prefer it when available."""
    if _has_spatial:
        # Newer DuckDB decodes GeoParquet natively -> column is already
        # GEOMETRY; older builds hand back a WKB BLOB. Probe, don't assume.
        try:
            rows = _conn.execute(
                f"DESCRIBE SELECT geometry FROM "
                f"read_parquet('{_dataset_path()}') LIMIT 0").fetchall()
            coltype = str(rows[0][1]).upper()
        except Exception:
            coltype = "BLOB"
        if coltype.startswith("GEOMETRY"):
            return "ST_X(geometry)", "ST_Y(geometry)", None
        return ("ST_X(ST_GeomFromWKB(geometry))",
                "ST_Y(ST_GeomFromWKB(geometry))", None)
    return ("bbox.xmin", "bbox.ymin",
            "spatial extension unavailable — coordinates read from bbox "
            "(float32, ~1 m precision)")


def columns():
    """Top-level column names in the current release (cached)."""
    key = config.OVERTURE_RELEASE
    if key in _columns_cache:
        return _columns_cache[key]
    con = _connect()
    with _lock:
        try:
            rows = con.execute(
                f"DESCRIBE SELECT * FROM read_parquet('{_dataset_path()}', "
                f"hive_partitioning=1) LIMIT 0"
            ).fetchall()
        except Exception as exc:
            raise OvertureError(
                f"Could not read Overture release '{config.OVERTURE_RELEASE}'. "
                f"Check OVERTURE_RELEASE in .env against "
                f"https://docs.overturemaps.org/release-notes/ — a wrong "
                f"release string is the usual cause. Underlying error: {exc}"
            )
    cols = {r[0] for r in rows}
    _columns_cache[key] = cols
    return cols


def _category_expr():
    """`taxonomy` on new releases, `categories` on old ones."""
    cols = columns()
    if "taxonomy" in cols:
        return "taxonomy.primary", "taxonomy.hierarchy"
    if "categories" in cols:
        return "categories.primary", "NULL"
    raise OvertureError("Release exposes neither `taxonomy` nor `categories`.")


def _overture_categories(category_keys):
    cats = []
    for key in category_keys:
        for c in CATEGORIES.get(key, {}).get("overture", []):
            if c not in cats:
                cats.append(c)
    return cats


def _brand_case_sql():
    """SQL CASE that resolves a brand label, in the order that stops
    BSI being eaten by MANDIRI and BRILink by BRI."""
    parts = []
    for key in BRAND_RESOLVE_ORDER:
        cfg = BRANDS[key]
        label = cfg["label"].replace("'", "''")
        pat = cfg["pattern"].replace("'", "''")
        if cfg.get("wikidata"):
            parts.append(f"WHEN brand.wikidata = '{cfg['wikidata']}' THEN '{label}'")
        parts.append(
            f"WHEN regexp_matches(lower(coalesce(brand.names.primary, '')), "
            f"'{pat}') THEN '{label}'"
        )
        parts.append(
            f"WHEN regexp_matches(lower(coalesce(names.primary, '')), "
            f"'{pat}') THEN '{label}'"
        )
    return "CASE " + " ".join(parts) + " ELSE NULL END"


def search(lat, lon, radius_m, category_keys, brand_keys=None, name_query="",
           max_results=None, min_confidence=None):
    """Return (rows, meta)."""
    brand_keys = brand_keys or []
    max_results = max_results or config.MAX_RESULTS
    min_confidence = (config.OVERTURE_MIN_CONFIDENCE
                      if min_confidence is None else min_confidence)

    cols = columns()          # also forces _connect(), which sets _has_spatial
    cat_expr, hier_expr = _category_expr()
    basic_expr = "basic_category" if "basic_category" in cols else "NULL"
    lon_expr, lat_expr, geom_note = _geom_exprs()

    # Degree padding for the bbox prune. Longitude degrees shrink with
    # latitude; near the equator (Indonesia) the difference is small, but
    # compute it properly anyway.
    from math import cos, radians

    dlat = radius_m / 111_320.0
    dlon = radius_m / (111_320.0 * max(cos(radians(lat)), 0.01))
    minlon, maxlon = lon - dlon, lon + dlon
    minlat, maxlat = lat - dlat, lat + dlat

    where = [
        f"bbox.xmin BETWEEN {minlon} AND {maxlon}",
        f"bbox.ymin BETWEEN {minlat} AND {maxlat}",
        f"coalesce(confidence, 0) >= {min_confidence}",
    ]
    if "operating_status" in cols:
        where.append("coalesce(operating_status, 'open') = 'open'")

    cats = _overture_categories(category_keys)
    if cats:
        quoted = ", ".join(f"'{c}'" for c in cats)
        cat_clause = f"{cat_expr} IN ({quoted})"
        if basic_expr != "NULL":
            cat_clause += f" OR {basic_expr} IN ({quoted})"
        where.append(f"({cat_clause})")

    if brand_keys:
        ors = []
        for key in brand_keys:
            cfg = BRANDS.get(key)
            if not cfg:
                continue
            pat = cfg["pattern"].replace("'", "''")
            if cfg.get("wikidata"):
                ors.append(f"brand.wikidata = '{cfg['wikidata']}'")
            ors.append(
                f"regexp_matches(lower(coalesce(brand.names.primary, '')), '{pat}')")
            ors.append(
                f"regexp_matches(lower(coalesce(names.primary, '')), '{pat}')")
        if ors:
            where.append("(" + " OR ".join(ors) + ")")

    if name_query.strip():
        safe = name_query.strip().lower().replace("'", "''")
        where.append(f"lower(coalesce(names.primary, '')) LIKE '%{safe}%'")

    sql = f"""
    WITH src AS (
      SELECT
        id,
        names.primary                       AS name,
        brand.names.primary                 AS brand_name,
        brand.wikidata                      AS brand_qid,
        {cat_expr}                          AS category,
        {basic_expr}                        AS basic_category,
        {hier_expr}                         AS hierarchy,
        confidence,
        {"operating_status" if "operating_status" in cols else "NULL"} AS status,
        {_brand_case_sql()}                 AS brand_resolved,
        {lon_expr}                          AS lon,
        {lat_expr}                          AS lat,
        CASE WHEN len(addresses) > 0 THEN addresses[1].freeform END AS address,
        CASE WHEN len(addresses) > 0 THEN addresses[1].locality END AS locality,
        CASE WHEN len(addresses) > 0 THEN addresses[1].region   END AS region,
        CASE WHEN len(websites)  > 0 THEN websites[1]           END AS website,
        CASE WHEN len(phones)    > 0 THEN phones[1]             END AS phone,
        CASE WHEN len(sources)   > 0 THEN sources[1].dataset    END AS dataset,
        CASE WHEN len(sources)   > 0 THEN sources[1].update_time END AS updated
      FROM read_parquet('{_dataset_path()}', hive_partitioning=1)
      WHERE {' AND '.join(where)}
    )
    SELECT * FROM (
      SELECT *,
        round(2 * 6371000 * asin(sqrt(
          pow(sin(radians(lat - {lat}) / 2), 2) +
          cos(radians({lat})) * cos(radians(lat)) *
          pow(sin(radians(lon - {lon}) / 2), 2)))) AS distance_m
      FROM src
    )
    WHERE distance_m <= {radius_m}
    ORDER BY distance_m
    LIMIT {max_results}
    """

    con = _connect()
    with _lock:
        try:
            cur = con.execute(sql)
            colnames = [d[0] for d in cur.description]
            records = cur.fetchall()
        except Exception as exc:
            raise OvertureError(f"Overture query failed: {exc}")

    rows = []
    for rec in records:
        d = dict(zip(colnames, rec))
        d["source"] = "overture"
        # Overture matched on real brand fields / the name itself, so the
        # brand attribution is always evidence-backed (cf. Google cheap mode).
        d["brand_verified"] = True
        # bbox coords are float32; trim the binary-representation noise so
        # exports don't carry 12 meaningless decimal places.
        for k in ("lat", "lon"):
            if d.get(k) is not None:
                d[k] = round(float(d[k]), 7)
        d["types"] = ", ".join(d.pop("hierarchy") or []) if d.get("hierarchy") else None
        d["kelurahan"] = None
        d["kecamatan"] = None
        d["kota"] = d.get("locality")
        d["provinsi"] = d.get("region")
        d["rating"] = None
        d["user_ratings"] = None
        d["url"] = d.get("website")
        if d.get("updated") is not None:
            d["updated"] = str(d["updated"])
        rows.append(d)

    meta = {
        "provider": "overture",
        "release": config.OVERTURE_RELEASE,
        "category_field": cat_expr,
        "overture_categories": cats,
        "min_confidence": min_confidence,
        "truncated": len(rows) >= max_results,
    }
    if geom_note:
        meta["warning"] = geom_note
    return rows, meta


def brand_audit(country="ID", category_keys=None, limit=60):
    """Discovery query: what brand strings / QIDs actually exist in this
    release? Run this before trusting any brand filter."""
    cat_expr, _ = _category_expr()
    cats = _overture_categories(
        category_keys or ["convenience_store", "supermarket", "bank", "atm"])
    quoted = ", ".join(f"'{c}'" for c in cats) or "''"
    sql = f"""
    SELECT
      brand.names.primary AS brand_name,
      brand.wikidata      AS brand_qid,
      {cat_expr}          AS category,
      count(*)            AS n
    FROM read_parquet('{_dataset_path()}', hive_partitioning=1)
    WHERE bbox.xmin BETWEEN 95 AND 141
      AND bbox.ymin BETWEEN -11 AND 6
      AND {cat_expr} IN ({quoted})
      AND brand.names.primary IS NOT NULL
    GROUP BY 1, 2, 3
    ORDER BY n DESC
    LIMIT {limit}
    """
    con = _connect()
    with _lock:
        cur = con.execute(sql)
        cols_ = [d[0] for d in cur.description]
        return [dict(zip(cols_, r)) for r in cur.fetchall()]
