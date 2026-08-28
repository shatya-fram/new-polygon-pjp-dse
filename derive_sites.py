#!/usr/bin/env python3
"""
Place every loaded site against the DSE geography.

    python derive_sites.py

A thin wrapper around import_local.stamp_sites so the Configuration page can
run it the same way it runs remap_mc.py: as a named step with its log shown,
rather than as a hidden cost inside an upload. The work itself is real --
the coverage model is built from every outlet, then every site is tested
against it -- so it is minutes, not seconds, and it is worth watching.
"""
import sys

import db
import formats
import import_local as il


def main():
    con = db.connect()
    try:
        n_out = con.execute(
            "SELECT count(*) FROM geo_feature WHERE layer_key = ?",
            (formats.OUTLET_LAYER,)).fetchone()[0]
        n_site = con.execute("SELECT count(*) FROM ref_site").fetchone()[0]
        if not n_site:
            print("  no sites loaded — nothing to place")
            return 1
        if not n_out:
            print("  no outlet mapping loaded — there is no DSE geography to "
                  "place sites against")
            return 1
        print(f"  placing {n_site:,} sites against {n_out:,} outlets")
        il.stamp_sites(con, outlet_layer=formats.OUTLET_LAYER, field="DSE_CODE")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
