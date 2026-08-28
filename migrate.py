#!/usr/bin/env python3
"""
Idempotent schema migration. Safe to run on every start — never hand-edit
the SQLite file, always change it here.

    python migrate.py
"""
import sqlite3
import sys

import config
import db

SCHEMA = {
    "pull_run": """
        CREATE TABLE IF NOT EXISTS pull_run (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            source        TEXT    NOT NULL,       -- overture|google|osm
            started_utc   TEXT    NOT NULL,
            finished_utc  TEXT,
            status        TEXT    NOT NULL,       -- running|ok|failed
            params_json   TEXT,
            rows_written  INTEGER DEFAULT 0,
            api_calls     INTEGER DEFAULT 0,
            est_cost_usd  REAL    DEFAULT 0,
            notes         TEXT
        )""",
    "poi_overture": """
        CREATE TABLE IF NOT EXISTS poi_overture (
            source_id        TEXT PRIMARY KEY,     -- Overture GERS id
            source           TEXT NOT NULL DEFAULT 'overture',
            name             TEXT,
            brand_resolved   TEXT,
            brand_name       TEXT,
            brand_qid        TEXT,
            category         TEXT,
            basic_category   TEXT,
            confidence       REAL,
            operating_status TEXT,
            lat              REAL,
            lon              REAL,
            address          TEXT,
            locality         TEXT,
            region           TEXT,
            phone            TEXT,
            website          TEXT,
            dataset          TEXT,
            source_updated   TEXT,
            mc_ioh          TEXT,
            adm_kelurahan    TEXT,
            adm_kecamatan    TEXT,
            adm_kota         TEXT,
            adm_provinsi     TEXT,
            adm_source       TEXT,
            pull_run_id      INTEGER REFERENCES pull_run(id),
            first_seen_utc   TEXT,
            last_seen_utc    TEXT
        )""",
    "poi_google": """
        CREATE TABLE IF NOT EXISTS poi_google (
            source_id       TEXT PRIMARY KEY,      -- Google Place ID
            source          TEXT NOT NULL DEFAULT 'google',
            target          TEXT,
            name            TEXT,
            brand_resolved  TEXT,
            brand_verified  INTEGER,
            category        TEXT,
            primary_type    TEXT,
            types           TEXT,
            business_status TEXT,
            lat             REAL,
            lon             REAL,
            address         TEXT,
            kelurahan       TEXT,
            kecamatan       TEXT,
            kota            TEXT,
            provinsi        TEXT,
            locality        TEXT,
            region          TEXT,
            phone           TEXT,
            website         TEXT,
            rating          REAL,
            user_ratings    INTEGER,
            maps_uri        TEXT,
            mc_ioh          TEXT,
            adm_kelurahan   TEXT,
            adm_kecamatan   TEXT,
            adm_kota        TEXT,
            adm_provinsi    TEXT,
            adm_source      TEXT,
            pull_run_id     INTEGER REFERENCES pull_run(id),
            first_seen_utc  TEXT,
            last_seen_utc   TEXT
        )""",
    "poi_osm": """
        CREATE TABLE IF NOT EXISTS poi_osm (
            source_id       TEXT PRIMARY KEY,      -- 'node/123' | 'way/456'
            source          TEXT NOT NULL DEFAULT 'osm',
            osm_type        TEXT,
            osm_id          INTEGER,
            name            TEXT,
            brand_resolved  TEXT,
            brand_name      TEXT,
            brand_qid       TEXT,
            category        TEXT,
            target          TEXT,
            operator        TEXT,
            network         TEXT,
            ref             TEXT,
            lat             REAL,
            lon             REAL,
            address         TEXT,
            locality        TEXT,
            region          TEXT,
            phone           TEXT,
            website         TEXT,
            opening_hours   TEXT,
            osm_version     INTEGER,
            source_updated  TEXT,
            tags_json       TEXT,
            mc_ioh          TEXT,
            adm_kelurahan   TEXT,
            adm_kecamatan   TEXT,
            adm_kota        TEXT,
            adm_provinsi    TEXT,
            adm_source      TEXT,
            pull_run_id     INTEGER REFERENCES pull_run(id),
            first_seen_utc  TEXT,
            last_seen_utc   TEXT
        )""",
    # ── cross-source de-duplication ─────────────────────────────────
    # These two say which rows are the same real-world place. They stay
    # separate from the POI tables on purpose: a matching decision is an
    # opinion with a threshold behind it, and opinions do not belong inside
    # the record of what each source actually said.
    "poi_match": """
        CREATE TABLE IF NOT EXISTS poi_match (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            a_table      TEXT NOT NULL,
            a_id         TEXT NOT NULL,
            b_table      TEXT NOT NULL,
            b_id         TEXT NOT NULL,
            distance_m   REAL,
            name_sim     REAL,
            score        REAL,
            verdict      TEXT,
            decided_utc  TEXT,
            pull_run_id  INTEGER REFERENCES pull_run(id)
        )""",
    "poi_cluster": """
        CREATE TABLE IF NOT EXISTS poi_cluster (
            cluster_id   INTEGER NOT NULL,
            table_name   TEXT NOT NULL,
            source_id    TEXT NOT NULL,
            PRIMARY KEY (table_name, source_id)
        )""",
    # ── locally uploaded reference data ─────────────────────────────
    # Boundaries and internal business figures live here, kept apart from
    # the API-sourced POI tables. These are yours: no licence ceiling, no
    # 30-day cache limit, no share-alike.
    "geo_layer": """
        CREATE TABLE IF NOT EXISTS geo_layer (
            layer_key      TEXT PRIMARY KEY,
            label          TEXT,
            kind           TEXT,
            source_file    TEXT,
            feature_count  INTEGER,
            attr_fields    TEXT,
            imported_utc   TEXT,
            notes          TEXT
        )""",
    "geo_feature": """
        CREATE TABLE IF NOT EXISTS geo_feature (
            layer_key         TEXT NOT NULL,
            feature_key       TEXT NOT NULL,
            name              TEXT,
            join_key          TEXT,
            attrs_json        TEXT,
            geometry_geojson  TEXT,
            minlon REAL, minlat REAL, maxlon REAL, maxlat REAL,
            centroid_lat REAL, centroid_lon REAL,
            imported_utc      TEXT,
            PRIMARY KEY (layer_key, feature_key)
        )""",
    # ── sample uploads ───────────────────────────────────────────────
    # Somebody else's file, kept apart from the application's own data on
    # purpose. A sample is a demarcation someone wants to look at against
    # the real layers -- it is not a new version of them. Separate tables
    # mean an upload can never overwrite a permanent layer, and deleting a
    # sample can never take one with it.
    "sample_set": """
        CREATE TABLE IF NOT EXISTS sample_set (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            label         TEXT    NOT NULL,
            uploader      TEXT    NOT NULL,
            branch        TEXT    NOT NULL,
            region        TEXT    NOT NULL,
            note          TEXT,
            source_file   TEXT,           -- as stored in the upload folder
            original_name TEXT,           -- as the uploader called it
            sheet         TEXT,
            rows_read     INTEGER DEFAULT 0,
            rows_kept     INTEGER DEFAULT 0,
            no_coords     INTEGER DEFAULT 0,
            off_map       INTEGER DEFAULT 0,
            dse_count     INTEGER DEFAULT 0,
            minlon REAL, minlat REAL, maxlon REAL, maxlat REAL,
            uploaded_utc  TEXT    NOT NULL
        )""",
    "sample_outlet": """
        CREATE TABLE IF NOT EXISTS sample_outlet (
            set_id        INTEGER NOT NULL,
            row_no        INTEGER NOT NULL,
            dse           TEXT,
            outlet_code   TEXT,
            outlet_name   TEXT,
            attrs_json    TEXT,
            lat           REAL,
            lon           REAL,
            PRIMARY KEY (set_id, row_no)
        )""",
    "ref_mc": """
        CREATE TABLE IF NOT EXISTS ref_mc (
            mc           TEXT PRIMARY KEY,
            branch       TEXT,
            area         TEXT,
            region       TEXT,
            circle       TEXT,
            source_file  TEXT,
            imported_utc TEXT
        )""",
    "ref_kecamatan": """
        CREATE TABLE IF NOT EXISTS ref_kecamatan (
            kec_key      TEXT PRIMARY KEY,
            kecamatan    TEXT,
            kabkot       TEXT,
            join_key     TEXT,
            mc           TEXT,
            sa           TEXT,
            area         TEXT,
            region       TEXT,
            rank         INTEGER,
            source_file  TEXT,
            imported_utc TEXT
        )""",
    "ref_kelurahan": """
        CREATE TABLE IF NOT EXISTS ref_kelurahan (
            kel_key      TEXT PRIMARY KEY,   -- upper-case "KEL|KEC|KABKOT"
            unique_id    TEXT,               -- BPS Unique_ID, e.g. 31_1_1_1001
            kelurahan    TEXT,
            kecamatan    TEXT,
            kabkot       TEXT,
            prov         TEXT,
            join_key     TEXT,               -- norm_key(kel, kec, kabkot)
            kec_join_key TEXT,               -- norm_key(kec, kabkot) -> ref_kecamatan
            -- Primary Indosat mapping: the 24-MC / 9-branch structure, the
            -- same generation as the indosat_mc polygon layer. mc35/branch11
            -- are the finer re-split, carried but not joined on.
            mc           TEXT,
            branch       TEXT,
            area         TEXT,
            region       TEXT,
            circle       TEXT,
            pt           TEXT,
            partner      TEXT,
            mc35         TEXT,
            branch11     TEXT,
            population   INTEGER,
            pop_index    INTEGER,
            geo_type     TEXT,
            area_m2      REAL,
            lat          REAL,
            lon          REAL,
            source_file  TEXT,
            imported_utc TEXT
        )""",
    # dse_code is whatever the upload carried; dse_assigned is what the
    # coverage polygons say. Kept apart on purpose: when the two disagree that
    # is a territory finding, and one merged column is how it disappears.
    "ref_site": """
        CREATE TABLE IF NOT EXISTS ref_site (
            site_id      TEXT PRIMARY KEY,
            site_name    TEXT,
            lat          REAL,
            lon          REAL,
            vlr          REAL,
            dse_code     TEXT,
            dse_assigned TEXT,
            assign_mode  TEXT,
            desa         TEXT,
            kecamatan    TEXT,
            kabkot       TEXT,
            attrs_json   TEXT,
            source_file  TEXT,
            imported_utc TEXT
        )""",
    "ref_store": """
        CREATE TABLE IF NOT EXISTS ref_store (
            store_key    TEXT PRIMARY KEY,
            store_code   TEXT,
            store_name   TEXT,
            store_type   TEXT,
            rso_name     TEXT,
            address      TEXT,
            lat          REAL,
            lon          REAL,
            source_file  TEXT,
            imported_utc TEXT
        )""",
    "ref_agent": """
        CREATE TABLE IF NOT EXISTS ref_agent (
            nik_agent    TEXT PRIMARY KEY,
            agent_name   TEXT,
            position     TEXT,
            store_code   TEXT,
            store_name   TEXT,
            nik_rso      TEXT,
            rso_name     TEXT,
            source_file  TEXT,
            imported_utc TEXT
        )""",
    # Metrics are stored long, not wide, and deliberately. The source
    # headers carry the period in the column name ("VLR SUBS MTD 1708"),
    # so a wide table would need a schema migration every single refresh.
    # Long format absorbs a new month as rows.
    "ref_metric": """
        CREATE TABLE IF NOT EXISTS ref_metric (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type  TEXT NOT NULL,
            entity_key   TEXT NOT NULL,
            metric       TEXT NOT NULL,
            period       TEXT,
            value_num    REAL,
            value_text   TEXT,
            source_file  TEXT,
            imported_utc TEXT
        )""",
    "ref_service_point": """
        CREATE TABLE IF NOT EXISTS ref_service_point (
            sp_key       TEXT PRIMARY KEY,
            operator     TEXT,
            operator_raw TEXT,
            sp_class     TEXT,
            name         TEXT,
            sp_type      TEXT,
            address      TEXT,
            lat          REAL,
            lon          REAL,
            mc_ioh       TEXT,
            adm_kecamatan TEXT,
            adm_kota     TEXT,
            attrs_json   TEXT,
            source_file  TEXT,
            imported_utc TEXT
        )""",
    # The chosen record for each cluster. Computed in Python because the
    # choice depends on the POI's class, and the class maps live in
    # reconcile.py -- expressing that in a SQL view would duplicate them.
    "poi_cluster_pick": """
        CREATE TABLE IF NOT EXISTS poi_cluster_pick (
            cluster_id   INTEGER PRIMARY KEY,
            table_name   TEXT NOT NULL,
            source_id    TEXT NOT NULL,
            cls          TEXT,
            n_members    INTEGER,
            reason       TEXT
        )""",

    # ── Future GAPURA ────────────────────────────────────────────────────
    # Uploaded candidate sites, kept so a scored batch survives a page
    # refresh and can be re-exported without the spreadsheet being re-sent.
    # The scores are stored with the parameters that produced them, because
    # a score is meaningless without the gate settings it was run under.
    "site_batch": """
        CREATE TABLE IF NOT EXISTS site_batch (
            batch_id     TEXT PRIMARY KEY,
            label        TEXT,
            source_file  TEXT,
            n_rows       INTEGER,
            n_scored     INTEGER,
            params_json  TEXT,
            uploaded_utc TEXT
        )""",
    "site_candidate": """
        CREATE TABLE IF NOT EXISTS site_candidate (
            batch_id        TEXT NOT NULL,
            row_no          INTEGER NOT NULL,
            location        TEXT,
            kecamatan_input TEXT,
            lat             REAL,
            lon             REAL,
            join_key        TEXT,
            kecamatan       TEXT,
            kabkot          TEXT,
            mc              TEXT,
            profile         TEXT,
            radius_km       REAL,
            minimarket      INTEGER,
            atm             INTEGER,
            bank_top4       INTEGER,
            bank_all        INTEGER,
            cvi_local       REAL,
            vlr             REAL,
            km_im3          REAL,
            km_3id          REAL,
            km_hybrid       REAL,
            score           REAL,
            rank            INTEGER,
            kec_score       REAL,
            kec_rank        INTEGER,
            gate_fails_json TEXT,
            parts_json      TEXT,
            error           TEXT,
            PRIMARY KEY (batch_id, row_no)
        )""",
}

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_sample_outlet_set "
    "ON sample_outlet (set_id)",
    "CREATE INDEX IF NOT EXISTS ix_ovt_cat   ON poi_overture(category)",
    "CREATE INDEX IF NOT EXISTS ix_ovt_brand ON poi_overture(brand_resolved)",
    "CREATE INDEX IF NOT EXISTS ix_ovt_geo   ON poi_overture(lat, lon)",
    "CREATE INDEX IF NOT EXISTS ix_ggl_cat   ON poi_google(category)",
    "CREATE INDEX IF NOT EXISTS ix_ggl_brand ON poi_google(brand_resolved)",
    "CREATE INDEX IF NOT EXISTS ix_ggl_geo   ON poi_google(lat, lon)",
    "CREATE INDEX IF NOT EXISTS ix_run_src   ON pull_run(source, started_utc)",
    "CREATE INDEX IF NOT EXISTS ix_match_a   ON poi_match(a_table, a_id)",
    "CREATE INDEX IF NOT EXISTS ix_match_b   ON poi_match(b_table, b_id)",
    "CREATE INDEX IF NOT EXISTS ix_match_v   ON poi_match(verdict, score)",
    "CREATE INDEX IF NOT EXISTS ix_clu_id    ON poi_cluster(cluster_id)",
    "CREATE INDEX IF NOT EXISTS ix_pick_src  ON poi_cluster_pick(table_name, source_id)",
    "CREATE INDEX IF NOT EXISTS ix_geo_layer ON geo_feature(layer_key)",
    "CREATE INDEX IF NOT EXISTS ix_geo_join  ON geo_feature(join_key)",
    "CREATE INDEX IF NOT EXISTS ix_geo_bbox  ON geo_feature(minlon, minlat, maxlon, maxlat)",
    "CREATE INDEX IF NOT EXISTS ix_kec_join  ON ref_kecamatan(join_key)",
    "CREATE INDEX IF NOT EXISTS ix_kec_mc    ON ref_kecamatan(mc)",
    "CREATE INDEX IF NOT EXISTS ix_kel_join ON ref_kelurahan(join_key)",
    "CREATE INDEX IF NOT EXISTS ix_kel_kec  ON ref_kelurahan(kec_join_key)",
    "CREATE INDEX IF NOT EXISTS ix_kel_mc   ON ref_kelurahan(mc)",
    "CREATE INDEX IF NOT EXISTS ix_site_dse  ON ref_site(dse_assigned)",
    "CREATE INDEX IF NOT EXISTS ix_site_geo  ON ref_site(lat, lon)",
    "CREATE INDEX IF NOT EXISTS ix_sp_op    ON ref_service_point(operator)",
    "CREATE INDEX IF NOT EXISTS ix_sp_geo   ON ref_service_point(lat, lon)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_metric_u ON ref_metric(entity_type, entity_key, metric, coalesce(period,''))",
    "CREATE INDEX IF NOT EXISTS ix_metric_e  ON ref_metric(entity_type, entity_key)",
    "CREATE INDEX IF NOT EXISTS ix_ovt_adm   ON poi_overture(adm_kecamatan)",
    "CREATE INDEX IF NOT EXISTS ix_ggl_adm   ON poi_google(adm_kecamatan)",
    "CREATE INDEX IF NOT EXISTS ix_osm_cat   ON poi_osm(category)",
    "CREATE INDEX IF NOT EXISTS ix_osm_brand ON poi_osm(brand_resolved)",
    "CREATE INDEX IF NOT EXISTS ix_osm_geo   ON poi_osm(lat, lon)",
    "CREATE INDEX IF NOT EXISTS ix_osm_tgt   ON poi_osm(target)",
    "CREATE INDEX IF NOT EXISTS ix_osm_adm   ON poi_osm(adm_kecamatan)",
    "CREATE INDEX IF NOT EXISTS ix_cand_batch ON site_candidate(batch_id, score)",
]


def existing_columns(con, table):
    try:
        return {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}
    except sqlite3.OperationalError:
        return set()


def add_missing_columns(con, table, ddl):
    """CREATE TABLE IF NOT EXISTS does nothing for a table that already
    exists, so new columns have to be added explicitly."""
    have = existing_columns(con, table)
    if not have:
        return []
    # Table-level constraints are not columns. Parsing them as one produced
    # `ALTER TABLE ... ADD COLUMN PRIMARY KEY (a, b)`, which SQLite rejects
    # with a bare syntax error the moment a table with a composite key is
    # migrated a second time.
    NOT_A_COLUMN = ("PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT")
    wanted = {}
    for line in ddl.splitlines():
        line = line.strip()
        if (not line or line.upper().startswith(("CREATE", "(", ")"))
                or line.startswith("--")):
            continue
        # Strip the trailing comment FIRST. Splitting on commas before doing
        # so turns a comma inside a comment into a column separator, and the
        # next word becomes a phantom column -- which ALTER TABLE then chokes
        # on with a syntax error pointing at a word that is not in the schema.
        line = line.split("--")[0].strip()
        # One line may carry several definitions ("a REAL, b REAL").
        for frag in line.split(","):
            frag = frag.strip().rstrip(",")
            if not frag or frag in (")", "("):
                continue
            parts = frag.split()
            if len(parts) < 2 or parts[0].upper() in NOT_A_COLUMN:
                continue
            if not parts[0].isidentifier():
                continue
            wanted[parts[0]] = " ".join(parts[1:]).strip()
    added = []
    for col, spec in wanted.items():
        if col in have:
            continue
        # SQLite can't add a column with a non-constant default or a PK.
        spec = spec.replace("PRIMARY KEY", "").strip()
        con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {spec}")
        added.append(col)
    return added


def main():
    print(f"database: {config.DB_PATH}")
    con = db.connect()
    created, altered = [], []
    for table, ddl in SCHEMA.items():
        before = existing_columns(con, table)
        con.execute(ddl)
        if not before:
            created.append(table)
        else:
            cols = add_missing_columns(con, table, ddl)
            if cols:
                altered.append((table, cols))
    for ix in INDEXES:
        con.execute(ix)
    con.commit()

    for t in created:
        print(f"  created table  {t}")
    for t, cols in altered:
        print(f"  added columns  {t}: {', '.join(cols)}")
    if not created and not altered:
        print("  schema already current — nothing to do")

    c = db.counts(con)
    print("  rows: " + "  ".join(
        f"{t}={0 if c.get(t) is None else c[t]:,}" for t in sorted(c)))
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
