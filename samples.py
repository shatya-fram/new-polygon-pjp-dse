#!/usr/bin/env python3
"""
SAMPLE UPLOADS — somebody else's demarcation, shown against ours.

WHAT THIS IS FOR
    A branch has a working file: this DSE takes these outlets, that one takes
    those. They want to see it on the map, over the territory the application
    already knows, before anybody agrees to make it the territory. That is a
    different act from configuring the application, so it is a different
    store: separate tables, its own upload, its own delete.

    Nothing here can overwrite a permanent layer, and deleting every sample
    cannot touch one. A sample is drawn ON TOP of the application's layers,
    never in place of them.

WHO UPLOADED IT IS PART OF THE DATA
    Once this is on a shared server, "whose file is this and which branch
    does it describe?" is the first question anyone will ask of an overlay
    they did not upload. Name, branch and region are required before the
    file is read, not offered afterwards.

THE FORMAT
    The demarcation exports carry one row per outlet with LONG and LAT, and
    they spell their columns differently from the application's own outlet
    layer -- "DSE CODE" against "DSE_CODE", "Desa Name" against "Desa_Name".
    Both spellings are accepted. The sheet is chosen by which one carries the
    required columns, because these workbooks also contain Summary and Maps
    sheets that would otherwise be read as an outlet list.
"""
import collections
import json
import os
import sqlite3
import sys

import config
import db

# What a row has to have before it can be drawn. Everything else is context.
REQUIRED = ("dse", "lat", "lon")

# Column name -> field. Lower-cased and stripped of spaces, punctuation and
# parenthetical notes before matching, so "Micro Cluster Name (MC)",
# "MICRO_CLUSTER_NAME" and "Micro Cluster Name" all land in the same place.
FIELDS = {
    "dse":          ("dsecode", "dse", "dseid", "unikdse", "dse_code"),
    "dse_msisdn":   ("dsemisisdn", "dsemsisdn", "dse_misisd"),
    "outlet_code":  ("outletcode", "outlet_cod", "outletcod"),
    "outlet_name":  ("outletname", "outlet_nam", "outletnam"),
    "outlet_msisdn": ("outletmsisdn", "outlet_msi"),
    "lat":          ("lat", "latitude", "latnew", "y"),
    "lon":          ("long", "lon", "longitude", "longnew", "x"),
    "brand":        ("brandname", "brand_name", "brand"),
    "region":       ("regionname", "region_nam", "region"),
    "area":         ("areaname", "area_name", "area"),
    "branch":       ("branchname", "branch_nam", "branch"),
    "mc":           ("microclustername", "microclus", "micro_clus", "mc"),
    "partner":      ("partnerterritoryname", "partner_te", "partnerterritory"),
    "partner_type": ("partnertype", "partner_ty"),
    "partner_name": ("mpxnamepartnername", "partnernm", "partner_nm"),
    "supervisor":   ("supervisorcode", "supervisor"),
    "category":     ("outletcategory", "outlet_cat"),
    "kabupaten":    ("kabupaten", "kabkot", "kotakabupaten"),
    "kecamatan":    ("kecamatanname", "kecamatan"),
    "desa":         ("desaname", "desa_name", "desa", "kelurahanname",
                     "keluarahanname", "kelurahan"),
    "hybrid":       ("hybridnonhybrid", "hybrid_non", "hybrid"),
    "schedule":     ("jadwalkunjungan", "jadwalkunjunganf4f8f12", "pjp"),
    "pairing":      ("pairingoutletcode", "pairing_ou", "outletpairing"),
    "unikid":       ("unikid",),
    "remarks":      ("remarks", "remark"),
}

# Context worth keeping on every row, in the order a popup should read them.
KEEP = ("brand", "category", "mc", "branch", "area", "region", "kabupaten",
        "kecamatan", "desa", "partner", "partner_type", "partner_name",
        "supervisor", "hybrid", "schedule", "pairing", "outlet_msisdn",
        "dse_msisdn", "unikid", "remarks")

LABEL = {
    "dse": "DSE", "outlet_code": "Outlet code", "outlet_name": "Outlet",
    "brand": "Brand", "category": "Category", "mc": "Microcluster",
    "branch": "Branch", "area": "Area", "region": "Region",
    "kabupaten": "Kabupaten", "kecamatan": "Kecamatan", "desa": "Desa",
    "partner": "Partner territory", "partner_type": "Partner type",
    "partner_name": "Partner", "supervisor": "Supervisor",
    "hybrid": "Hybrid", "schedule": "Visit schedule (PJP)",
    "pairing": "Pairing outlet", "outlet_msisdn": "Outlet MSISDN",
    "dse_msisdn": "DSE MSISDN", "unikid": "Unique id", "remarks": "Remarks",
}


def _norm(name):
    """A column heading reduced to letters and digits, lower case."""
    s = str(name or "").lower()
    if "(" in s:                       # "Jadwal Kunjungan (F4,F8,F12)"
        s = s.split("(")[0]
    return "".join(c for c in s if c.isalnum())


def _map_header(header):
    """-> {field: column index}. First match wins, so a sheet with both
    "DSE CODE" and "UNIKDSE" uses the one named first in FIELDS."""
    seen = {}
    for i, cell in enumerate(header):
        n = _norm(cell)
        if not n:
            continue
        for field, names in FIELDS.items():
            if field in seen:
                continue
            if n in names:
                seen[field] = i
                break
    return seen


def _num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).strip().replace(",", ".")
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _txt(v):
    if v is None:
        return None
    t = str(v).strip()
    return t or None


def read_workbook(path, max_rows=200000):
    """Find the outlet sheet and read it. -> (sheet_name, idx, rows).

    Raises ValueError naming what was missing, because "could not read the
    file" is not something anyone can act on."""
    import openpyxl
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    best = None
    tried = []
    try:
        for ws in wb.worksheets:
            # The header is not always row 1 -- these exports carry a title
            # row above it often enough to be worth looking a few rows down.
            for probe in range(0, 6):
                header = None
                for i, row in enumerate(ws.iter_rows(min_row=probe + 1,
                                                     max_row=probe + 1,
                                                     values_only=True)):
                    header = row
                if not header:
                    break
                idx = _map_header(header)
                have = [f for f in REQUIRED if f in idx]
                tried.append((ws.title, probe + 1, len(idx), len(have)))
                if len(have) == len(REQUIRED):
                    best = (ws.title, probe + 1, idx)
                    break
            if best:
                break
        if not best:
            near = sorted(tried, key=lambda t: -t[3])[:1]
            hint = ""
            if near:
                hint = (f" The closest was sheet '{near[0][0]}' row "
                        f"{near[0][1]}, which matched {near[0][3]} of "
                        f"{len(REQUIRED)}.")
            raise ValueError(
                "No sheet in this workbook has the columns a DSE-to-outlet "
                "sample needs: a DSE code, a longitude and a latitude. "
                "Accepted headings include DSE CODE / UNIKDSE, LONG / "
                "LONGITUDE and LAT / LATITUDE." + hint)

        sheet, hrow, idx = best
        ws = wb[sheet]
        rows = []
        for n, row in enumerate(ws.iter_rows(min_row=hrow + 1,
                                             values_only=True)):
            if n >= max_rows:
                break
            if not row or all(c is None for c in row):
                continue
            rows.append(row)
        return sheet, idx, rows
    finally:
        wb.close()


def parse(path):
    """-> (records, stats). A record is ready to insert."""
    sheet, idx, raw = read_workbook(path)

    def cell(row, field):
        i = idx.get(field)
        if i is None or i >= len(row):
            return None
        return row[i]

    out, stats = [], collections.Counter()
    dse = set()
    box = [None, None, None, None]
    for n, row in enumerate(raw):
        stats["read"] += 1
        code = _txt(cell(row, "dse"))
        if not code:
            stats["no_dse"] += 1
            continue
        lat, lon = _num(cell(row, "lat")), _num(cell(row, "lon"))
        # Same transposition guard the rest of the application uses: here
        # latitude is about -6 and longitude about 107, so the numbers
        # settle it and the column headings do not get a vote.
        if lat is not None and lon is not None and abs(lat) > 90 >= abs(lon):
            lat, lon = lon, lat
        if lat is None or lon is None:
            stats["no_coords"] += 1
            continue
        if not config.on_map(lat, lon):
            # Outside the three regions is outside this map. Counted and
            # reported rather than dropped in silence.
            stats["off_map"] += 1
            continue
        attrs = {}
        for f in KEEP:
            v = _txt(cell(row, f))
            if v is not None:
                attrs[f] = v
        dse.add(code)
        box[0] = lon if box[0] is None else min(box[0], lon)
        box[1] = lat if box[1] is None else min(box[1], lat)
        box[2] = lon if box[2] is None else max(box[2], lon)
        box[3] = lat if box[3] is None else max(box[3], lat)
        out.append((len(out), code,
                    _txt(cell(row, "outlet_code")),
                    _txt(cell(row, "outlet_name")),
                    json.dumps(attrs, ensure_ascii=False), lat, lon))
    stats["kept"] = len(out)
    stats["dse"] = len(dse)
    return out, {"sheet": sheet, "stats": stats, "bbox": box}


# ── the browser-parsed path ──────────────────────────────────────────────
# The page reads the workbook itself and posts the rows it found. Nothing is
# re-parsed here: two parsers for one format is two ways to disagree about
# what a file said. The server's job is to check and to keep.
def from_browser(rows):
    """[{dse, outlet_code, outlet_name, lat, lon, attrs}] -> (records, info)."""
    out = []
    st = collections.Counter()
    dse = set()
    box = [None, None, None, None]
    for r in rows or []:
        st["read"] += 1
        code = _txt(r.get("dse"))
        if not code:
            st["no_dse"] += 1
            continue
        lat, lon = _num(r.get("lat")), _num(r.get("lon"))
        if lat is not None and lon is not None and abs(lat) > 90 >= abs(lon):
            lat, lon = lon, lat
        if lat is None or lon is None:
            st["no_coords"] += 1
            continue
        if not config.on_map(lat, lon):
            st["off_map"] += 1
            continue
        attrs = {k: _txt(v) for k, v in (r.get("attrs") or {}).items()
                 if _txt(v) is not None and k in KEEP}
        dse.add(code)
        box[0] = lon if box[0] is None else min(box[0], lon)
        box[1] = lat if box[1] is None else min(box[1], lat)
        box[2] = lon if box[2] is None else max(box[2], lon)
        box[3] = lat if box[3] is None else max(box[3], lat)
        out.append((len(out), code, _txt(r.get("outlet_code")),
                    _txt(r.get("outlet_name")),
                    json.dumps(attrs, ensure_ascii=False), lat, lon))
    st["kept"] = len(out)
    st["dse"] = len(dse)
    return out, {"sheet": _txt(rows and rows[0].get("_sheet")) or "browser",
                 "stats": st, "bbox": box}


def save(con, meta, records, info):
    """Write one sample set. -> set id."""
    st = info["stats"]
    b = info["bbox"]
    cur = con.execute(
        "INSERT INTO sample_set (label, uploader, branch, region, note, "
        "source_file, original_name, sheet, rows_read, rows_kept, no_coords, "
        "off_map, dse_count, minlon, minlat, maxlon, maxlat, uploaded_utc) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (meta["label"], meta["uploader"], meta["branch"], meta["region"],
         meta.get("note"), meta.get("source_file"), meta.get("original_name"),
         info["sheet"], st["read"], st["kept"], st["no_coords"],
         st["off_map"], st["dse"], b[0], b[1], b[2], b[3], db.utcnow()))
    sid = cur.lastrowid
    con.executemany(
        "INSERT INTO sample_outlet (set_id, row_no, dse, outlet_code, "
        "outlet_name, attrs_json, lat, lon) VALUES (?,?,?,?,?,?,?,?)",
        [(sid,) + r for r in records])
    con.commit()
    return sid


def listing(con):
    """Every sample set, newest first."""
    try:
        rows = con.execute(
            "SELECT * FROM sample_set ORDER BY id DESC").fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]


def one(con, sid):
    try:
        r = con.execute("SELECT * FROM sample_set WHERE id=?", (sid,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(r) if r else None


def points(con, sid):
    """A sample set as GeoJSON, ready to draw."""
    feats = []
    for r in con.execute(
            "SELECT row_no, dse, outlet_code, outlet_name, attrs_json, "
            "lat, lon FROM sample_outlet WHERE set_id=? ORDER BY row_no",
            (sid,)):
        props = {"row": r["row_no"], "dse": r["dse"],
                 "outlet_code": r["outlet_code"],
                 "outlet_name": r["outlet_name"]}
        try:
            props.update(json.loads(r["attrs_json"] or "{}"))
        except (TypeError, ValueError):
            pass
        feats.append({"type": "Feature", "properties": props,
                      "geometry": {"type": "Point",
                                   "coordinates": [r["lon"], r["lat"]]}})
    return {"type": "FeatureCollection", "name": f"sample:{sid}",
            "features": feats}


def roster(con, sid):
    """Who is in this sample and how much they hold -- the same shape of
    answer the microcluster roster gives, so the two read alike."""
    per = collections.defaultdict(
        lambda: {"outlets": 0, "desa": set(), "kecamatan": set(),
                 "minlat": None, "minlon": None, "maxlat": None,
                 "maxlon": None})
    for r in con.execute(
            "SELECT dse, attrs_json, lat, lon FROM sample_outlet "
            "WHERE set_id=?", (sid,)):
        d = per[r["dse"]]
        d["outlets"] += 1
        try:
            a = json.loads(r["attrs_json"] or "{}")
        except (TypeError, ValueError):
            a = {}
        if a.get("desa"):
            d["desa"].add((a["desa"].upper(), (a.get("kecamatan") or "").upper()))
        if a.get("kecamatan"):
            d["kecamatan"].add(a["kecamatan"].upper())
        for k, v, fn in (("minlat", r["lat"], min), ("maxlat", r["lat"], max),
                         ("minlon", r["lon"], min), ("maxlon", r["lon"], max)):
            d[k] = v if d[k] is None else fn(d[k], v)
    total = max(1, sum(v["outlets"] for v in per.values()))
    rows = [{
        "dse": k, "outlets": v["outlets"],
        "share": round(v["outlets"] * 100.0 / total, 1),
        "desa": len(v["desa"]), "kecamatan": len(v["kecamatan"]),
        "bounds": ([[v["minlat"], v["minlon"]], [v["maxlat"], v["maxlon"]]]
                   if v["minlat"] is not None else None),
    } for k, v in per.items()]
    rows.sort(key=lambda r: -r["outlets"])
    return rows


def delete(con, sid):
    """Remove a sample set and its rows. -> the stored file name, if any.

    The uploaded file is returned rather than deleted here: whether it is
    also removed from disk is the caller's decision, and the caller is the
    only one that knows the folder is writable."""
    row = one(con, sid)
    if not row:
        return None
    con.execute("DELETE FROM sample_outlet WHERE set_id=?", (sid,))
    con.execute("DELETE FROM sample_set WHERE id=?", (sid,))
    con.commit()
    return row.get("source_file")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    recs, info = parse(sys.argv[1])
    st = info["stats"]
    print(f"sheet '{info['sheet']}' — {st['read']:,} rows read, "
          f"{st['kept']:,} kept, {st['dse']:,} DSE")
    for k in ("no_dse", "no_coords", "off_map"):
        if st[k]:
            print(f"  {st[k]:,} {k.replace('_', ' ')}")
    print("  bbox:", info["bbox"])
    if recs:
        print("  first:", recs[0][1], recs[0][3], recs[0][5], recs[0][6])
    return 0


if __name__ == "__main__":
    sys.exit(main())
