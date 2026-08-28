#!/usr/bin/env python3
"""
CONNECTION 2 — Google Places API (New).

Independent of pull_overture.py: its own tiling, its own table
(poi_google), its own cost accounting. Requires GOOGLE_API_KEY in .env;
without one it exits cleanly rather than half-running.

    python pull_google.py --category bus_stop --center -6.1872,106.8153 --radius 2000
    python pull_google.py --category bank --brand bca --radius 5000
    python pull_google.py --category bus_stop --tile-aoi --tile-km 2

Cost control: the field mask is pinned to Essentials + Pro. --with-rating
promotes every call in the run to the Enterprise SKU — the run summary
always prints which SKU was billed and the estimated spend.
"""
import argparse
import json
import sqlite3
import sys
import time

import time

import config
import db
import google_plan
from providers import google_places as gp

MAX_RADIUS = 50000


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


def parse_center(text):
    try:
        lat, lon = (float(x) for x in text.split(","))
        return lat, lon
    except Exception:
        raise SystemExit(f"--center must be 'lat,lon' — got {text!r}")


def tiles_for_aoi(aoi, tile_km):
    """Uniform grid over the AOI. Each tile becomes one circle query whose
    radius covers the tile's half-diagonal, so the grid has no gaps."""
    from math import cos, radians, sqrt
    dlat = tile_km / 110.574
    mid = (aoi["minlat"] + aoi["maxlat"]) / 2
    dlon = tile_km / (111.320 * max(cos(radians(mid)), 0.01))
    radius = sqrt(2) / 2 * tile_km * 1000

    out, lat = [], aoi["minlat"] + dlat / 2
    while lat < aoi["maxlat"] + dlat / 2:
        lon = aoi["minlon"] + dlon / 2
        while lon < aoi["maxlon"] + dlon / 2:
            out.append((round(lat, 6), round(lon, 6), min(radius, MAX_RADIUS)))
            lon += dlon
        lat += dlat
    return out


# Where Indonesia's Jabodetabek-area manufacturing actually is. Confirmed
# by the pabrik sweep: 1,076 of 2,100 confident industrial places landed in
# these three, from 65 of the 121 kecamatan.
INDUSTRIAL_KABKOT = {"BEKASI", "KOTA BEKASI", "KARAWANG"}


def cells_for(grid):
    """Search cells straight from the boundaries you uploaded, so a pull is
    scoped to the same territory model as everything else in the app."""
    if grid == "aoi":
        a = config.AOI
        return [("AOI", (a["minlon"], a["minlat"], a["maxlon"], a["maxlat"]))]
    con = db.connect()
    try:
        rows = list(con.execute(
            "SELECT feature_key, name, attrs_json, minlon, minlat, maxlon,"
            " maxlat FROM geo_feature WHERE layer_key = ?",
            ("kecamatan" if grid in ("kecamatan", "kabkot",
                                     "industrial_belt") else grid,)))
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()
    if not rows:
        sys.exit(f"No '{grid}' boundaries loaded. Run "
                 f"`python import_local.py --all` first — the grid comes "
                 f"from geo_feature, not from a made-up square mesh.")
    if grid == "industrial_belt":
        # Only the kabupaten that actually hold the factories. 65 cells
        # instead of 121, which is what makes a typed sweep affordable
        # inside what is left of the free tier.
        return [(r["name"] or r["feature_key"],
                 (r["minlon"], r["minlat"], r["maxlon"], r["maxlat"]))
                for r in rows
                if (json.loads(r["attrs_json"] or "{}").get("KABKOT") or ""
                    ).strip().upper() in INDUSTRIAL_KABKOT]

    if grid != "kabkot":
        return [(r["name"] or r["feature_key"],
                 (r["minlon"], r["minlat"], r["maxlon"], r["maxlat"]))
                for r in rows]
    # kabkot cells are the union of their kecamatan bounding boxes
    agg = {}
    for r in rows:
        kab = (json.loads(r["attrs_json"] or "{}").get("KABKOT")
               or "UNKNOWN").strip()
        b = agg.get(kab)
        agg[kab] = ((min(b[0], r["minlon"]), min(b[1], r["minlat"]),
                     max(b[2], r["maxlon"]), max(b[3], r["maxlat"]))
                    if b else (r["minlon"], r["minlat"], r["maxlon"],
                               r["maxlat"]))
    return sorted(agg.items())


def to_row(r, target=None):
    return {
        "source_id": r["id"], "source": "google", "target": target,
        "name": r["name"],
        "brand_resolved": r.get("brand_resolved"),
        "brand_verified": 1 if r.get("brand_verified") else 0,
        "category": r.get("category"), "primary_type": r.get("category"),
        "types": r.get("types"), "business_status": r.get("status"),
        "lat": r.get("lat"), "lon": r.get("lon"),
        "address": r.get("address"),
        "kelurahan": r.get("kelurahan"), "kecamatan": r.get("kecamatan"),
        "kota": r.get("kota"), "provinsi": r.get("provinsi"),
        "locality": r.get("kota"), "region": r.get("provinsi"),
        "phone": r.get("phone"), "website": None,
        "rating": r.get("rating"), "user_ratings": r.get("user_ratings"),
        "maps_uri": r.get("url"),
    }


FREE_PRO_PER_MONTH = 5000


def month_to_date_requests(con):
    """Requests already billed this calendar month, from pull_run.

    --max-requests only ever guarded a single run, which is no use when the
    free tier is a monthly pool spent across many. This reads what has
    actually been sent so the guard can reason about the pool, not the run.
    Approximate by design: it counts what this app sent, not what the Cloud
    Console says, so treat it as a floor and check billing for the truth."""
    row = con.execute(
        "SELECT coalesce(sum(api_calls), 0) FROM pull_run "
        "WHERE source='google' AND started_utc >= ?",
        (time.strftime("%Y-%m-01"),)).fetchone()
    return int(row[0] or 0)


def preflight():
    """Spend ONE request to prove the key, the enabled API and the billing
    account all work. Finding a setup problem on request 1 instead of on
    request 276 is the whole point."""
    log("preflight: one Text Search request against a small box in Jakarta")
    box = (106.80, -6.20, 106.83, -6.17)
    try:
        rows, meta = gp.search_rect(
            box, [("kantor kelurahan", "local_government_office")],
            max_pages=1, max_calls=1)
    except gp.GoogleError as exc:
        print(f"\n  FAILED: {exc}")
        if getattr(exc, "hint", None):
            print(f"\n  What to do:\n    {exc.hint}\n")
        else:
            print("\n  No specific hint for this one — the message above is "
                  "Google's own.\n")
        return 1
    except Exception as exc:                              # noqa: BLE001
        print(f"\n  FAILED before Google answered: {type(exc).__name__}: {exc}")
        print("  Usually no internet, or a proxy blocking places.googleapis.com.\n")
        return 1
    print(f"\n  OK — {len(rows)} place(s) returned, {meta['api_calls']} request "
          f"billed at {meta['sku']} (~${meta['est_cost_usd']:.4f}).")
    for r in rows[:3]:
        print(f"    {r['name']}  ({r['category']})  "
              f"{r['lat']:.5f}, {r['lon']:.5f}")
    print("\n  Key, API and billing are all working. Safe to run a group.\n")
    return 0


def run_targets(args, keys):
    """Run the planned targets: the same (textQuery, includedType) list the
    estimator priced, over the same cells it counted."""
    reg = google_plan.all_targets()
    plan = []
    for key in keys:
        t = reg[key]
        cells = cells_for(t["grid"])
        if args.limit_cells:
            cells = cells[:args.limit_cells]
        plan.append((t, cells))

    est = sum(google_plan.plan_one(t, len(c))["requests"] for t, c in plan)
    hi = sum(google_plan.plan_one(t, len(c))["hi"] for t, c in plan)
    con0 = db.connect()
    spent = month_to_date_requests(con0)
    con0.close()
    left = FREE_PRO_PER_MONTH - spent
    log(f"{len(plan)} target(s) · {est:,} estimated requests "
        f"(worst case {hi:,}) · ~${est * 32.0 / 1000:,.2f} list price")
    log(f"month to date: {spent:,} of {FREE_PRO_PER_MONTH:,} free Pro "
        f"requests used, {left:,} left")
    # Guard on the WORST CASE, not the estimate. Guarding on the estimate
    # is what let a 1,232-request plan spend 2,251 of 2,818 remaining: the
    # estimate can be wrong, the 3-pages-per-search ceiling cannot.
    if hi > left:
        over_est = max(0, est - left)
        print(f"\n  BUDGET WARNING")
        print(f"    estimate   {est:,} requests"
              + (f"  (${over_est*32/1000:,.2f} over the free tier)"
                 if over_est else "  (fits)"))
        print(f"    worst case {hi:,} requests  "
              f"(${max(0, hi-left)*32/1000:,.2f} over)")
        print(f"    remaining  {left:,} free requests this month")
        print(f"  Pass --max-requests {min(left, hi)} to cap the run at what "
              f"is free, or accept the overage deliberately.")
    # An explicit --max-requests is an instruction to CAP, not a tripwire.
    # Refusing there contradicted the advice printed a few lines above, and
    # made the honest "spend only what is free" path impossible.
    if args.max_requests is None:
        if est > 4500:
            print(f"\nREFUSING: the plan needs ~{est:,} requests, over the "
                  f"4,500 default ceiling.\n"
                  f"  That is usually a mistyped grid. Run fewer targets, or "
                  f"pass --max-requests deliberately.")
            return 1
        args.max_requests = 4500
    elif est > args.max_requests:
        log(f"capping at {args.max_requests:,} requests — the plan wants "
            f"~{est:,}, so the last targets will be left incomplete. "
            f"Re-run next month to finish; the upsert keys on place id, so "
            f"nothing already fetched is paid for twice in the data.")

    if args.dry_run:
        print("\n  DRY RUN — no API call made, nothing billed.\n")
        for t, cells in plan:
            p = google_plan.plan_one(t, len(cells))
            print(f"  {t['label'][:40]:<42}{len(cells):>4} cells x "
                  f"{len(t['searches'])} search = {p['requests']:>5,} requests")
            for text, itype in t["searches"]:
                print(f"      textQuery={text!r:<26} includedType={itype}")
            print(f"      e.g. cell {cells[0][0]!r} bbox={tuple(round(v,4) for v in cells[0][1])}")
        print(f"\n  Total ~{est:,} requests · ~${est*32.0/1000:,.2f} gross · "
              f"$0.00 if inside the 5,000/month free Pro tier\n")
        return 0

    sql_con = db.connect()
    run_id = db.start_run(sql_con, "google", {
        "targets": list(keys), "estimated_requests": est,
        "max_requests": args.max_requests, "sku": "Text Search Pro",
        "lean_fields": not args.full_fields})
    log(f"pull_run #{run_id} — connection: GOOGLE (planned targets)")

    total, calls, cost, closed = 0, 0, 0.0, 0
    seen_ids, refetched = set(), 0
    try:
        for t, cells in plan:
            got, tcalls, tnew = 0, 0, 0
            for name, bbox in cells:
                if calls >= args.max_requests:
                    log(f"  STOPPING at the {args.max_requests:,}-request "
                        f"ceiling — {t['key']} is incomplete")
                    break
                rows, meta = gp.search_rect(
                    bbox, t["searches"], lean=not args.full_fields,
                    max_calls=args.max_requests - calls)
                calls += meta["api_calls"]
                tcalls += meta["api_calls"]
                cost += meta["est_cost_usd"]
                closed += meta["closed_dropped"]
                # Cell bounding boxes overlap -- a bbox around an irregular
                # kecamatan covers slices of its neighbours -- so the same
                # office comes back from several cells. The upsert keys on
                # place id so the data is fine, but the re-fetches are paid
                # for, and that is worth measuring rather than hiding.
                fresh = [r for r in rows if r["id"] not in seen_ids]
                refetched += len(rows) - len(fresh)
                seen_ids.update(r["id"] for r in rows)
                tnew += len(fresh)
                got += db.upsert(sql_con, "poi_google",
                                 [to_row(r, t["key"]) for r in rows], run_id)
                if meta["truncated"]:
                    log(f"    WARN {t['key']} / {name}: hit the 60-result cap "
                        f"— more places exist here than this cell can return")
            total += tnew
            log(f"  {t['label'][:40]:<42}{tnew:>7,} new  {got:>6,} fetched"
                f"  {tcalls:>5,} requests")
        db.finish_run(sql_con, run_id, "ok", rows_written=total,
                      api_calls=calls, est_cost_usd=round(cost, 4),
                      notes=f"targets={','.join(keys)}")
    except gp.GoogleError as exc:
        # A setup problem is not a crash worth a stack trace. Record the run
        # as failed, say what to do, and leave the database untouched.
        db.finish_run(sql_con, run_id, "failed", rows_written=total,
                      api_calls=calls, est_cost_usd=round(cost, 4),
                      notes=str(exc)[:500])
        sql_con.close()
        print(f"\n  STOPPED: {exc}")
        if getattr(exc, "hint", None):
            print(f"\n  What to do:\n    {exc.hint}")
        print(f"\n  {total:,} rows were written before this, {calls:,} "
              f"request(s) attempted. Re-running is safe — the upsert keys "
              f"on place id, so nothing duplicates.\n")
        return 1
    except Exception as exc:
        db.finish_run(sql_con, run_id, "failed", rows_written=total,
                      api_calls=calls, est_cost_usd=round(cost, 4),
                      notes=str(exc)[:500])
        raise

    c = db.counts(sql_con)
    print(f"\n  {total:,} distinct places new to this run   ·   poi_google "
          f"now holds {c['poi_google']:,}")
    if refetched:
        print(f"  {refetched:,} results were the same place returned by more "
              f"than one cell ({refetched/(refetched+total)*100:.0f}% of "
              f"everything fetched).\n  Cell bounding boxes overlap by "
              f"nature; the data is unaffected, the requests are not.")
    print(f"  {calls:,} requests · Text Search Pro · list price "
          f"${cost:,.2f} (the 5,000/month free tier may absorb it entirely)")
    if closed:
        print(f"  {closed:,} permanently-closed places dropped on the way in")
    print(f"  Licence: only Place IDs may be stored beyond 30 days. "
          f"poi_google is separate for exactly that reason.")
    sql_con.close()
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", action="append", choices=sorted(config.CATEGORIES),
                    help="repeatable")
    ap.add_argument("--brand", action="append", choices=sorted(config.BRANDS),
                    help="repeatable; switches the request to Text Search")
    ap.add_argument("--name", default="", help="free-text name filter")
    ap.add_argument("--center", help="lat,lon — single-circle mode")
    ap.add_argument("--radius", type=int, default=config.DEFAULT_RADIUS_M)
    ap.add_argument("--tile-aoi", action="store_true",
                    help="sweep the whole AOI as a grid instead of one circle")
    ap.add_argument("--tile-km", type=float, default=2.0)
    ap.add_argument("--with-rating", action="store_true",
                    help="add rating/phone — promotes the run to Enterprise SKU")
    ap.add_argument("--max-tiles", type=int, default=400,
                    help="hard stop so a mistyped grid can't run up a bill")
    ap.add_argument("--target", action="append",
                    choices=sorted(google_plan.all_targets()),
                    help="a planned target from google_plan.py; repeatable")
    ap.add_argument("--group", action="append",
                    choices=sorted(google_plan.TARGETS),
                    help="commercial | government | industrial")
    ap.add_argument("--all-targets", action="store_true")
    ap.add_argument("--limit-cells", type=int,
                    help="run only the first N cells — probe a sweep for a "
                         "handful of requests before committing to it")
    ap.add_argument("--max-requests", type=int, default=None,
                    help="cap billable requests for this run. Set it and the "
                         "run stops at the cap with the target marked "
                         "incomplete; leave it and a plan over 4,500 is "
                         "refused outright as a likely mistake.")
    ap.add_argument("--full-fields", action="store_true",
                    help="request addresses too (same Pro SKU, ~40%% more bytes)")
    ap.add_argument("--check", action="store_true",
                    help="spend ONE request to verify key + API + billing")
    ap.add_argument("--dry-run", action="store_true",
                    help="show the tile plan and estimated cost, call nothing")
    args = ap.parse_args()

    # A dry run makes no request, so it must work without a key — planning
    # the spend before you have a billing account is the normal order.
    if not gp.available() and not args.dry_run:
        print("GOOGLE_API_KEY is not set in .env — nothing to do.\n"
              "  This connection is optional; pull_overture.py works without "
              "it,\n  and `--dry-run` plans the sweep with no key at all.")
        return 1
    if args.check:
        return preflight()

    picked = list(args.target or [])
    for g in (args.group or []):
        picked += [t["key"] for t in google_plan.TARGETS[g]]
    if args.all_targets:
        picked = list(google_plan.all_targets())
    if picked:
        return run_targets(args, dict.fromkeys(picked))

    if not args.category and not args.brand and not args.name:
        ap.error("give --target/--group/--all-targets, or one of "
                 "--category / --brand / --name")

    if args.tile_aoi:
        plan = tiles_for_aoi(config.AOI, args.tile_km)
        if len(plan) > args.max_tiles:
            print(f"REFUSING: {len(plan):,} tiles exceeds --max-tiles "
                  f"({args.max_tiles}).\n"
                  f"  Raise --tile-km or --max-tiles deliberately.")
            return 1
    else:
        lat, lon = parse_center(args.center) if args.center else (
            config.DEFAULT_LAT, config.DEFAULT_LON)
        plan = [(lat, lon, min(args.radius, MAX_RADIUS))]

    sku = "Enterprise" if args.with_rating else "Essentials+Pro"
    per_1k = 35.0 if args.with_rating else 32.0
    log(f"pull plan: {len(plan):,} request group(s) · SKU {sku} · "
        f"list price ${per_1k}/1k requests")

    if args.dry_run:
        est = len(plan) * per_1k / 1000
        print(f"\n  DRY RUN — no API call made.\n"
              f"  {len(plan):,} tiles · rough list cost ${est:,.2f} "
              f"(before the monthly free tier)\n")
        for lat, lon, rad in plan[:5]:
            print(f"    {lat:.5f}, {lon:.5f}  r={rad:,.0f} m")
        if len(plan) > 5:
            print(f"    … and {len(plan) - 5:,} more")
        return 0

    sql_con = db.connect()
    run_id = db.start_run(sql_con, "google", {
        "categories": args.category, "brands": args.brand,
        "name": args.name, "tiles": len(plan), "tile_km": args.tile_km,
        "with_rating": args.with_rating, "sku": sku})
    log(f"pull_run #{run_id} — connection: GOOGLE")

    total, calls, cost, seen = 0, 0, 0.0, set()
    try:
        for i, (lat, lon, rad) in enumerate(plan, 1):
            rows, meta = gp.search(
                lat, lon, int(rad), args.category or [],
                brand_keys=args.brand or [], name_query=args.name,
                include_enterprise=args.with_rating)
            calls += meta.get("api_calls", 0)
            cost += meta.get("est_cost_usd", 0.0)
            fresh = [to_row(r) for r in rows if r["id"] not in seen]
            seen.update(r["id"] for r in rows)
            total += db.upsert(sql_con, "poi_google", fresh, run_id)
            if args.tile_aoi and (i % 10 == 0 or i == len(plan)):
                log(f"  tile {i:,}/{len(plan):,} · {total:,} rows · "
                    f"{calls:,} calls · ~${cost:,.2f}")
            if meta.get("truncated"):
                log(f"  WARN tile {i} hit the result cap — shrink --tile-km "
                    f"or results are being silently dropped")
        db.finish_run(sql_con, run_id, "ok", rows_written=total,
                      api_calls=calls, est_cost_usd=round(cost, 4),
                      notes=f"sku={sku}")
    except Exception as exc:
        db.finish_run(sql_con, run_id, "failed", rows_written=total,
                      api_calls=calls, est_cost_usd=round(cost, 4),
                      notes=str(exc)[:500])
        raise

    c = db.counts(sql_con)
    print(f"\n  {total:,} rows upserted   ·   poi_google now holds "
          f"{c['poi_google']:,}")
    print(f"  {calls:,} API calls · SKU {sku} · estimated list cost "
          f"${cost:,.2f}")
    print(f"  (the monthly free tier may absorb this — check Cloud Console)")
    sql_con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
