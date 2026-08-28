#!/usr/bin/env python3
"""
Rebuild the prebuilt map files for every loaded layer.

    python prebuild_maps.py

Normally each is built when its layer is imported. Run this after pulling new
code that changes how they are built, or if a build was interrupted.
"""
import sys

import time

import db
import mapcache
import territory_api
import territory_rollup as tr


def main():
    con = db.connect()
    try:
        layers = [r["layer_key"] for r in con.execute(
            "SELECT layer_key FROM geo_layer ORDER BY feature_count DESC")]
        if not layers:
            print("  nothing loaded")
            return 1
        for lk in layers:
            mapcache.drop(lk)
            _p, n, b = mapcache.build(
                con, lk, props_for=territory_api._bulk_props(con, lk))
            print(f"  {lk:<18} {n:>8,} features  ->  {b / 1e6:>6.2f} MB")

        # The roll-up's own cache. Placing every outlet and mast inside a
        # desa polygon is the half-minute the Preview Polygon page used to
        # wait for on each restart; warming it here makes that a step you
        # can see rather than a delay the page wears.
        t = time.time()
        tr.forget()
        rows, _layers = tr.base_rows(con)
        tr.desa_points(con)
        print(f"  {'territory rollup':<18} {len(rows):>8,} kecamatan  ->  "
              f"{time.time() - t:>6.1f} s (cached)")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
