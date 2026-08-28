#!/usr/bin/env python3
"""
Build the database copy that goes to a shared server.

    python3 make_public_db.py                 -> data/public/poi_pulldown.db
    python3 make_public_db.py --dry            report only, write nothing

WHAT IT REMOVES AND WHY
    The outlet layer carries DSE_MISISDN and OUTLET_MSI -- mobile numbers
    for a rep and for the shopkeeper. The map never sends them, but the
    popup detail endpoint returns the whole attribute row, so on a reachable
    server they are one click away for anyone who gets in. Partner and
    company names stay: they are commercial, not personal.

    Nothing is deleted from your own database. This writes a separate copy
    and leaves the original untouched, because the local instance is where
    the work is done and it needs the full data.

WHAT ELSE IT DOES
    Drops the sample tables -- somebody's working file is not something to
    publish by accident -- and VACUUMs, which is worth doing: the working
    database is 382 MB and most of that is churn.
"""
import json
import os
import shutil
import sqlite3
import sys

import config

# Fields removed from every attribute row, matched case-insensitively and
# ignoring separators, so DSE_MISISDN / "DSE MISISDN" / dse_misisd all go.
STRIP = (
    "dsemisisdn", "dsemsisdn", "dsemisisd",
    "outletmsisdn", "outletmsi",
    "msisdn", "phone", "nohp", "nomorhp", "handphone", "mobile",
)
# Sample uploads are somebody else's working file. They do not travel.
DROP_TABLES = ("sample_outlet", "sample_set")

# ── LAYERS THAT MAY NOT TRAVEL ───────────────────────────────────────────
# The rule for the shared server is that it holds geography and nothing
# else. The outlet-to-DSE mapping and the mast locations are read from the
# user's own desktop in the browser and are never uploaded, so a copy of
# them sitting on the server would be a second and staler source for data
# this application deliberately does not host.
#
# Stripping phone numbers was never enough on its own. An outlet row with
# its phone number removed is still a named shop at a coordinate filed
# under a named rep, and there are 73,659 of them.
DROP_LAYERS = ("outlet_dse", "site_locations")

# Per-desa figures from the NCP village file. Population, area and the site
# counts are territory reference and stay -- the desa profile needs them and
# they describe the ground, not the business. The pjp_* rows are counts of
# DSE coverage per desa, which is the business, so they go.
DROP_METRICS = ("pjp_3id", "pjp_im3", "pjp_covered")


def drop_private(con, quiet=False):
    """Remove the layers and metrics that must not reach a shared server."""
    gone = {}
    for lk in DROP_LAYERS:
        n = con.execute("SELECT count(*) FROM geo_feature WHERE layer_key = ?",
                        (lk,)).fetchone()[0]
        if n:
            con.execute("DELETE FROM geo_feature WHERE layer_key = ?", (lk,))
            gone[lk] = n
        try:
            con.execute("DELETE FROM geo_layer WHERE layer_key = ?", (lk,))
        except sqlite3.OperationalError:
            pass
    try:
        for m in DROP_METRICS:
            n = con.execute("SELECT count(*) FROM ref_metric WHERE metric = ?",
                            (m,)).fetchone()[0]
            if n:
                con.execute("DELETE FROM ref_metric WHERE metric = ?", (m,))
                gone["ref_metric." + m] = n
    except sqlite3.OperationalError:
        pass
    if not quiet:
        for k, n in gone.items():
            print(f"  dropped {n:>8,}  {k}")
    return gone


def _norm(k):
    return "".join(c for c in str(k).lower() if c.isalnum())


def scrub_attrs(raw):
    """-> (json, removed_field_names) or (raw, ()) when nothing matched."""
    try:
        a = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return raw, ()
    if not isinstance(a, dict):
        return raw, ()
    gone = [k for k in a if _norm(k) in STRIP]
    if not gone:
        return raw, ()
    for k in gone:
        a.pop(k, None)
    return json.dumps(a, ensure_ascii=False), tuple(gone)


def main(argv):
    dry = "--dry" in argv
    src = config.DB_PATH
    if not os.path.exists(src):
        print(f"no database at {src}")
        return 1
    out_dir = os.path.join(os.path.dirname(src), "public")
    dst = os.path.join(out_dir, os.path.basename(src))

    if dry:
        con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        seen, rows = {}, 0
        for r in con.execute("SELECT layer_key, attrs_json FROM geo_feature"):
            _new, gone = scrub_attrs(r["attrs_json"])
            if gone:
                rows += 1
                for g in gone:
                    seen[(r["layer_key"], g)] = seen.get((r["layer_key"], g), 0) + 1
        con.close()
        if not seen:
            print("  nothing to strip")
        for (lk, f), n in sorted(seen.items(), key=lambda kv: -kv[1]):
            print(f"  {lk:<16} {f:<14} {n:>8,} rows")
        con2 = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
        print()
        for lk in DROP_LAYERS:
            n = con2.execute(
                "SELECT count(*) FROM geo_feature WHERE layer_key = ?",
                (lk,)).fetchone()[0]
            print(f"  would drop {n:>8,}  layer {lk}")
        try:
            for m in DROP_METRICS:
                n = con2.execute(
                    "SELECT count(*) FROM ref_metric WHERE metric = ?",
                    (m,)).fetchone()[0]
                print(f"  would drop {n:>8,}  ref_metric.{m}")
        except sqlite3.OperationalError:
            pass
        con2.close()
        print(f"\n  {rows:,} rows would be rewritten. Nothing written "
              f"(--dry).")
        return 0

    os.makedirs(out_dir, exist_ok=True)
    print(f"  copying {os.path.getsize(src) / 1e6:.0f} MB …")
    shutil.copy2(src, dst)

    con = sqlite3.connect(dst)
    con.row_factory = sqlite3.Row

    # DROP FIRST, THEN SCRUB WHAT IS LEFT.
    # The other way round rewrote 73,659 outlet rows to remove their phone
    # numbers and then deleted all 73,659 of them -- work done on rows that
    # were never going to travel, and a report that read as though stripping
    # phone numbers was the thing protecting them. It is not. Not shipping
    # the rows is. The scrub is defence for whatever remains.
    drop_private(con)

    for t in DROP_TABLES:
        try:
            n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            con.execute(f"DELETE FROM {t}")
            if n:
                print(f"  dropped {n:,} rows from {t}")
        except sqlite3.OperationalError:
            pass

    todo, seen = [], {}
    for r in con.execute("SELECT rowid, layer_key, attrs_json FROM geo_feature"):
        new, gone = scrub_attrs(r["attrs_json"])
        if gone:
            todo.append((new, r["rowid"]))
            for g in gone:
                seen[(r["layer_key"], g)] = seen.get((r["layer_key"], g), 0) + 1
    con.executemany("UPDATE geo_feature SET attrs_json=? WHERE rowid=?", todo)

    # attr_fields is the layer's advertised column list; a field that is gone
    # should not still be advertised.
    for r in con.execute("SELECT layer_key, attr_fields FROM geo_layer"):
        if not r["attr_fields"]:
            continue
        keep = [f for f in r["attr_fields"].split(",")
                if _norm(f) not in STRIP]
        if len(keep) != len(r["attr_fields"].split(",")):
            con.execute("UPDATE geo_layer SET attr_fields=? WHERE layer_key=?",
                        (",".join(keep), r["layer_key"]))
    con.commit()
    print(f"  rewrote {len(todo):,} attribute rows")
    for (lk, f), n in sorted(seen.items(), key=lambda kv: -kv[1]):
        print(f"    {lk:<16} {f:<14} {n:>8,}")
    print("  vacuuming …")
    con.execute("VACUUM")
    con.close()

    a, b = os.path.getsize(src), os.path.getsize(dst)
    print(f"\n  {dst}")
    print(f"  {a / 1e6:.0f} MB  ->  {b / 1e6:.0f} MB")
    print("\n  Verify before you ship it:")
    print(f"    python3 make_public_db.py --check {dst}")
    return 0


def check(path):
    """Prove the copy is clean rather than trusting that it is."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    bad = {}
    for r in con.execute("SELECT layer_key, attrs_json FROM geo_feature"):
        try:
            a = json.loads(r["attrs_json"] or "{}")
        except (TypeError, ValueError):
            continue
        for k in a:
            if _norm(k) in STRIP:
                bad[(r["layer_key"], k)] = bad.get((r["layer_key"], k), 0) + 1
    for t in DROP_TABLES:
        try:
            n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            if n:
                bad[("(table)", t)] = n
        except sqlite3.OperationalError:
            pass

    # THE CHECK THAT MATTERS MOST
    # A phone number is one field on a row. These are whole layers -- 73,659
    # named shops at coordinates, filed under named reps, and 15,919 masts.
    # This is the assertion that the copy holds geography and nothing else,
    # and build_release.sh refuses to package a database that fails it.
    for lk in DROP_LAYERS:
        n = con.execute("SELECT count(*) FROM geo_feature WHERE layer_key = ?",
                        (lk,)).fetchone()[0]
        if n:
            bad[("(layer)", lk)] = n
    try:
        for m in DROP_METRICS:
            n = con.execute("SELECT count(*) FROM ref_metric WHERE metric = ?",
                            (m,)).fetchone()[0]
            if n:
                bad[("(metric)", m)] = n
    except sqlite3.OperationalError:
        pass

    # And say what DID survive, so a clean result is readable as a fact
    # rather than as an absence.
    kept = con.execute(
        "SELECT layer_key, count(*) n FROM geo_feature "
        "GROUP BY 1 ORDER BY n DESC").fetchall()
    con.close()
    if not bad:
        print(f"  clean — {path}")
        print("  holds only:")
        for r in kept:
            print(f"    {r['layer_key']:<16} {r['n']:>8,}")
        return 0
    for (lk, f), n in bad.items():
        print(f"  STILL PRESENT  {lk:<16} {f:<14} {n:>8,}")
    return 1


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(check(sys.argv[sys.argv.index("--check") + 1]))
    sys.exit(main(sys.argv[1:]))
