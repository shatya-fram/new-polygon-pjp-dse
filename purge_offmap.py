#!/usr/bin/env python3
"""Remove features whose coordinates fall outside the area this app covers.

Rows loaded before the import gate existed: national polygons, outlets with
a positive latitude, masts at 0,0, kecamatan whose shape disagrees with the
province they name. Reports what it removes, per layer, before doing it.
"""
import sys
import config, db, mapcache


def main(apply_it):
    con = db.connect()
    doomed = {}
    for r in con.execute("SELECT layer_key, feature_key, name, minlon, minlat,"
                         " maxlon, maxlat FROM geo_feature"):
        if r["minlon"] is None:
            continue
        lat = (r["minlat"] + r["maxlat"]) / 2.0
        lon = (r["minlon"] + r["maxlon"]) / 2.0
        if not config.on_map(lat, lon):
            doomed.setdefault(r["layer_key"], []).append(
                (r["feature_key"], r["name"], round(lat, 3), round(lon, 3)))
    if not doomed:
        print("  nothing off-map")
        return 0
    for lk, rows in sorted(doomed.items(), key=lambda kv: -len(kv[1])):
        total = con.execute("SELECT count(*) FROM geo_feature WHERE layer_key=?",
                            (lk,)).fetchone()[0]
        print(f"  {lk:<16} {len(rows):>6,} off-map of {total:,}")
        for fk, nm, la, lo in rows[:3]:
            print(f"        {nm}  @ {la}, {lo}")
    if not apply_it:
        print("\n  dry run — pass --apply to remove them")
        return 0
    for lk, rows in doomed.items():
        con.executemany("DELETE FROM geo_feature WHERE layer_key=? AND "
                        "feature_key=?", [(lk, fk) for fk, _n, _a, _o in rows])
        n = con.execute("SELECT count(*) FROM geo_feature WHERE layer_key=?",
                        (lk,)).fetchone()[0]
        con.execute("UPDATE geo_layer SET feature_count=? WHERE layer_key=?",
                    (n, lk))
        mapcache.drop(lk)
    con.commit()
    con.close()
    print("\n  removed; caches dropped — run prebuild_maps.py")
    return 0


if __name__ == "__main__":
    sys.exit(main("--apply" in sys.argv))
