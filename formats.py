#!/usr/bin/env python3
"""
The file formats this application takes, as they are actually shipped.

WHY THIS MODULE EXISTS
    `import_local.py` reads the formats the parent application was given in
    August: a desa master keyed on KEL_DES/KEC/KAB_KOT, a kecamatan profile
    whose first column is "KECAMATAN|KABUPATEN", outlet exports as KMZ. The
    files the Regional Head standardised on are not those files. They carry
    the same facts under different headers, and one of them -- the outlet
    mapping -- arrived as a spreadsheet where the old one was a map export.

    Rather than loosen the old importers until they accept anything, which is
    how a desa master ends up half-read and silently short of 300 rows, each
    new format gets its own reader that states exactly which columns it
    expects. A file that does not match is refused by name, not guessed at.

THE REGION FILTER IS THE TERRITORY DEFINITION
    Both workbooks ship the whole Jakarta Raya circle. Which of it belongs to
    this application is a decision, not a property of the file, so it is
    named once -- here -- and reported in every import log.

    Widened 2026-08-25 from Inner Jakarta alone to the whole circle: Outer
    Jakarta (Bogor, Tangerang, Banten, Serang, Cianjur, Sukabumi) and West
    Java were added to the scope. That takes the village profile from 887
    rows to 7,771 and the outlet mapping from 26,342 to 73,699, so every
    figure on every page changes meaning on the day it is switched -- which
    is exactly why it is one constant with a date on it rather than a
    condition buried in three loops.
"""
import json
import os
import sqlite3

import config
import db
import import_local as il

# The regions in scope. Override with PJP_REGIONS in .env as a comma-separated
# list -- "INNER JAKARTA,OUTER JAKARTA" to go back to a narrower cut for a
# month without editing code.
REGIONS = tuple(r.strip().upper() for r in os.getenv(
    "PJP_REGIONS", "INNER JAKARTA,OUTER JAKARTA,WEST JAVA").split(",")
    if r.strip())
REGION = " · ".join(REGIONS)          # what the logs and the page call it


def in_scope(region):
    """One place decides. An empty REGIONS means take everything, which is
    what somebody reaching for that setting almost certainly means."""
    if not REGIONS:
        return True
    return (region or "").strip().upper() in REGIONS


def log(m):
    print(f"  {m}", flush=True)


def _pick_sheet(wb, required):
    """The sheet whose header carries `required`, not simply the first one.

    The two inputs now arrive as ONE workbook -- "DSE PJP into Desa" holds the
    village profile on one sheet and the outlet mapping on the next. Taking
    worksheets[0] would read the profile as an outlet list, fail on the
    missing columns, and report it as the wrong file for the slot."""
    best = None
    for ws in wb.worksheets:
        header, body = il.sheet_rows(ws)
        idx = _index([str(h or "").strip() for h in header])
        if required <= set(idx):
            return ws.title, [str(h or "").strip() for h in header], list(body)
        if best is None and sum(1 for h in header if str(h or "").strip()) >= 4:
            best = (ws.title, [str(h or "").strip() for h in header], list(body))
    if best is None:
        raise ValueError("no sheet in this workbook has a readable header row")
    # No sheet matched, so hand back the most plausible one and let the
    # caller's _need() name the missing columns -- a specific complaint beats
    # "no suitable sheet".
    return best


def _sheet(path, sheet=None, required=None):
    """(worksheet title, header list, body rows). With `required`, the sheet
    that carries those columns; otherwise the first readable one."""
    try:
        import openpyxl
    except ImportError:
        raise RuntimeError("openpyxl is required to read .xlsx files")
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        return _pick_sheet(wb, set(required or ()))
    finally:
        wb.close()


def _sheet_named(wb, required):
    """The first worksheet whose header row carries every `required` column."""
    if not required:
        return wb.worksheets[0]
    for ws in wb.worksheets:
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i > 7:
                break
            idx = _index([str(c or "").strip() for c in row])
            if set(required) <= set(idx):
                return ws
    return wb.worksheets[0]


def _stream_sheet(path, required=None):
    """(workbook, title, header, row generator) — the caller closes the book.

    `_sheet` above materialises the whole sheet before returning, which is
    fine for a village list and wrong for an outlet mapping: 73,700 rows by
    31 columns is 2.3 million cells, and holding them all as Python lists
    costs more than reading them does. This finds the header in the first
    few rows and then hands back a generator, so the import walks the file
    once and keeps one row in memory at a time.
    """
    try:
        import openpyxl
    except ImportError:
        raise RuntimeError("openpyxl is required to read .xlsx files")
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = _sheet_named(wb, required)
    rows = ws.iter_rows(values_only=True)
    buffered, header, hdr_at = [], None, -1
    for i, r in enumerate(rows):
        buffered.append(r)
        filled = sum(1 for c in r if str(c or "").strip())
        if filled >= 4:
            header, hdr_at = [str(c or "").strip() for c in r], i
            break
        if i >= 7:
            break
    if header is None:
        wb.close()
        raise ValueError("no readable header row in the first eight rows")

    def gen():
        for r in buffered[hdr_at + 1:]:
            yield r
        for r in rows:
            yield r

    return wb, ws.title, header, gen()


def _index(header):
    """Header -> column number, upper-cased and with newlines flattened.

    One of the NCP columns is literally "Existing Sites\\n4G" -- the line break
    is inside the cell. Matching on the raw string would work today and break
    the first time somebody widens the column."""
    out = {}
    for i, h in enumerate(header):
        key = " ".join(str(h or "").split()).upper()
        if key and key not in out:
            out[key] = i
    return out


def _get(row, idx, name):
    i = idx.get(name)
    if i is None or i >= len(row):
        return None
    v = row[i]
    if v is None:
        return None
    s = str(v).strip()
    return s if s and s not in ("#N/A", "-", "NULL") else None


def _need(idx, wanted, what):
    missing = [w for w in wanted if w not in idx]
    if missing:
        raise ValueError(
            f"This does not look like {what}. Missing column"
            + ("s: " if len(missing) > 1 else ": ")
            + ", ".join(missing)
            + ".  Columns found: " + ", ".join(sorted(idx)[:12])
            + ("…" if len(idx) > 12 else ""))


# ── 1. NCP village level -> ref_kelurahan + ref_metric ───────────────────
NCP_REQUIRED = ["VILLAGE (DESA)", "DISTRICT (KECAMATAN)", "CITY (KOTA/KAB)",
                "REGION", "MC", "BRANCH"]

# Everything that is a figure about the village rather than a name for it.
# These land in ref_metric under the period the sheet was cut for, so two
# months of NCP can sit side by side instead of overwriting each other.
NCP_METRICS = [
    ("TOTAL POPULATION", "population", "num"),
    ("TOTAL AREA (KM.SQ.)", "area_km2", "num"),
    ("EXISTING SITES 4G", "sites_4g", "num"),
    ("#SITE (IFAN)", "sites_all", "num"),
    ("NCP 4G", "ncp_4g", "num"),
    ("HIGH NCP 4G (≥50%)", "ncp_4g_high", "text"),
    ("PJP IM3", "pjp_im3", "num"),
    ("PJP 3ID", "pjp_3id", "num"),
    ("COVERED PJP", "pjp_covered", "text"),
    ("SITE REMARKS", "site_remarks", "text"),
    ("STAMP", "ncp_stamp", "text"),
]


def import_ncp_village(con, path, period=None, dry=False):
    """The desa profile: one row per village, hierarchy and NCP figures.

    The hierarchy columns land in ref_kelurahan.mc / .branch / .area exactly
    as the sheet ships them. That is the microcluster generation Indosat
    distributes with the data, and it is NOT the one the map joins on --
    `mc36` is, and it is derived from the polygons by remap_mc.py. Both are
    kept so the two can be compared; neither is written over the other.
    """
    title, header, body = _sheet(path, required=NCP_REQUIRED)
    idx = _index(header)
    _need(idx, NCP_REQUIRED, "an NCP village-level workbook")
    base = os.path.basename(path)
    period = period or _period_from(title, base)
    now = db.utcnow()

    rows, metrics, outside, nokey = [], [], 0, 0
    for r in body:
        if not r or all(c is None for c in r):
            continue
        if not in_scope(_get(r, idx, "REGION")):
            outside += 1
            continue
        kel = _get(r, idx, "VILLAGE (DESA)")
        kec = _get(r, idx, "DISTRICT (KECAMATAN)")
        kab = _get(r, idx, "CITY (KOTA/KAB)")
        if not kel or not kec:
            nokey += 1
            continue
        kel_key = f"{kel}|{kec}|{kab or ''}".upper()
        area_km2 = il.as_num(r[idx["TOTAL AREA (KM.SQ.)"]]) \
            if "TOTAL AREA (KM.SQ.)" in idx else None
        pop = il.as_num(r[idx["TOTAL POPULATION"]]) \
            if "TOTAL POPULATION" in idx else None
        rows.append((
            kel_key, _get(r, idx, "ID DESA"), kel, kec, kab,
            _get(r, idx, "PROVINCE"),
            il.norm_key(kel, kec, kab), il.norm_key(kec, kab),
            _get(r, idx, "MC"), _get(r, idx, "BRANCH"), _get(r, idx, "AREA"),
            _get(r, idx, "REGION"), _get(r, idx, "CIRCLE"), None, None,
            None, None,
            int(pop) if pop is not None else None, None, None,
            # ref_kelurahan.area_m2 is square METRES; the sheet is km².
            int(area_km2 * 1_000_000) if area_km2 is not None else None,
            None, None, base, now))

        pairs = []
        for col, metric, kind in NCP_METRICS:
            if col not in idx:
                continue
            raw = r[idx[col]] if idx[col] < len(r) else None
            if raw is None or str(raw).strip() == "":
                continue
            if kind == "num":
                num = il.as_num(raw)
                pairs.append((metric, period, num,
                              None if num is not None else str(raw).strip()))
            else:
                pairs.append((metric, period, None, str(raw).strip()))
        if pairs:
            metrics.append((kel_key, pairs))

    log(f"  {base}[{title}]: {len(rows):,} desa in scope ({REGION}) "
        f"— {outside:,} rows outside it, {nokey} without a name")
    log(f"      period {period!r} — {sum(len(p) for _, p in metrics):,} "
        f"figures on {len(metrics):,} desa")
    if dry or not rows:
        return len(rows)

    con.executemany(
        "INSERT OR REPLACE INTO ref_kelurahan (kel_key,unique_id,kelurahan,"
        "kecamatan,kabkot,prov,join_key,kec_join_key,mc,branch,area,region,"
        "circle,pt,partner,mc35,branch11,population,pop_index,geo_type,"
        "area_m2,lat,lon,source_file,imported_utc) VALUES "
        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    for kel_key, pairs in metrics:
        il.put_metrics(con, "desa", kel_key, pairs, base, now)
    _roll_kecamatan(con, rows, base, now)
    # The desa polygons carry the BPS identifier and the centroid; the
    # workbook does not. Stamp them across where the join_key matches, so the
    # profile and the boundary describe the same desa.
    con.execute(
        "UPDATE ref_kelurahan SET lat = (SELECT f.centroid_lat FROM geo_feature f"
        " WHERE f.layer_key='kelurahan' AND f.join_key = ref_kelurahan.join_key),"
        " lon = (SELECT f.centroid_lon FROM geo_feature f"
        " WHERE f.layer_key='kelurahan' AND f.join_key = ref_kelurahan.join_key)"
        " WHERE lat IS NULL")
    con.commit()
    return len(rows)


def _roll_kecamatan(con, desa_rows, base, now):
    """ref_kecamatan, rolled up from the villages rather than uploaded.

    There is no separate kecamatan file in this format, and inventing a
    second upload for a fact the village sheet already carries would be one
    more thing to keep in step. A kecamatan's microcluster is decided by a
    POPULATION-WEIGHTED majority of its desa, not by a simple count: a
    kecamatan lying across a seam should follow where most of its people are.
    That is the same rule remap_mc.py applies to the polygons, so the two
    answers are comparable.
    """
    import collections
    KEC, KAB, MC, BR, AR, RG, POP = 3, 4, 8, 9, 10, 11, 17
    weight = collections.defaultdict(lambda: collections.Counter())
    meta = {}
    for r in desa_rows:
        key = f"{r[KEC]}|{r[KAB] or ''}".upper()
        weight[key][r[MC]] += (r[POP] or 1)
        meta.setdefault(key, (r[KEC], r[KAB], r[BR], r[AR], r[RG]))
    rows = []
    for key, counts in weight.items():
        kec, kab, branch, area, region = meta[key]
        mc = counts.most_common(1)[0][0] if counts else None
        rows.append((key, kec, kab, il.norm_key(kec, kab), mc, branch, area,
                     region, None, base, now))
    if rows:
        con.executemany(
            "INSERT OR REPLACE INTO ref_kecamatan (kec_key,kecamatan,kabkot,"
            "join_key,mc,sa,area,region,rank,source_file,imported_utc) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
    split = sum(1 for c in weight.values() if len(c) > 1)
    log(f"      rolled up to {len(rows):,} kecamatan"
        + (f" ({split} of them span more than one microcluster and were "
           f"decided by population)" if split else ""))


def _period_from(sheet_title, filename):
    """A period the figures can be filed under. The sheet is called
    "NCP Village Jun'26" and the file "NCP Village Level_Jun2026 Jaya" -- one
    of the two always says which month, and a figure with no period is a
    figure you cannot put a second month beside."""
    import re
    for text in (sheet_title, filename):
        m = re.search(r"([A-Z][a-z]{2})[^0-9]{0,3}((?:20)?\d{2})", text or "")
        if m:
            yy = m.group(2)[-2:]
            return f"{m.group(1)}'{yy}"
    return "unstated"


# ── 2. DSE -> outlet mapping -> a point layer ────────────────────────────
OUTLET_LAYER = "outlet_dse"
OUTLET_REQUIRED = ["DSE CODE", "OUTLET CODE", "LAT", "LONG", "REGION NAME"]

# The spreadsheet's headers, renamed to the field names the KMZ exports used.
# This is the whole trick: the territory model, the group-by control, force
# fit and the popup all already know these names, so a workbook loaded this
# way behaves exactly like the map exports it replaces. Renaming here is one
# table; teaching six downstream modules a second vocabulary is not.
OUTLET_FIELDS = [
    ("DSE CODE", "DSE_CODE"),
    ("DSE MISISDN", "DSE_MISISD"),
    ("OUTLET CODE", "Outlet_Cod"),
    ("OUTLET NAME", "Outlet_Nam"),
    ("OUTLET MSISDN", "OUTLET_MSI"),
    ("OUTLET CATEGORY", "Outlet_Cat"),
    ("MICRO CLUSTER NAME (MC)", "Micro_Clus"),
    ("BRANCH NAME", "Branch_Nam"),
    ("AREA NAME", "Area_Name"),
    ("REGION NAME", "Region_Nam"),
    ("CIRCLE NAME", "Circle_Nam"),
    ("BRAND NAME", "Brand_Name"),
    ("KABUPATEN", "Kabupaten"),
    ("KECAMATAN NAME", "Kecamatan"),
    ("DESA NAME", "Desa_Name"),
    ("PARTNER TERRITORY NAME (PT)", "Partner_Te"),
    ("PARTNER TYPE", "Partner_Ty"),
    ("MPX NAME/PARTNER NAME", "Partner_Nm"),
    ("HYBRID/NON HYBRID", "Hybrid_Non"),
    ("HYBRID PAIRING DSE CODE", "Hybrid_DSE"),
    ("SUPERVISOR CODE", "Supervisor"),
    ("UNIKDSE", "UNIKDSE"),
    ("UNIKID", "UNIKID"),
]


def _read_outlets(body, idx, now):
    """Walk the rows once and return everything the caller needs.

    Separated so the workbook can be closed the moment the last row is read,
    rather than being held open through the database write."""
    rows, seen, outside, nocoord, dupes = [], set(), 0, 0, 0
    offmap = [0]
    dse, brands = set(), {}

    for i, r in enumerate(body):
        if not r or all(c is None for c in r):
            continue
        if not in_scope(_get(r, idx, "REGION NAME")):
            outside += 1
            continue
        lat = il.as_num(r[idx["LAT"]]) if idx["LAT"] < len(r) else None
        lon = il.as_num(r[idx["LONG"]]) if idx["LONG"] < len(r) else None
        if lat is None or lon is None:
            nocoord += 1
            continue
        # Same transposition guard the rest of the app uses: in this envelope
        # latitude is about -6 and longitude about 107, so the numbers settle
        # it and the column headings do not get a vote.
        if abs(lat) > 90 >= abs(lon):
            lat, lon = lon, lat
        # The transposition guard above only catches the obvious swap. Forty
        # rows in this export carry a POSITIVE latitude -- Jakarta mirrored
        # across the equator -- and a handful name a kabupaten in Aceh while
        # claiming OUTER JAKARTA. They are wrong coordinates, not territory,
        # and there is nowhere on this map to honestly draw them.
        if not config.on_map(lat, lon):
            offmap[0] += 1
            continue

        attrs = {}
        for col, field in OUTLET_FIELDS:
            v = _get(r, idx, col)
            if v is not None:
                attrs[field] = v
        code = attrs.get("Outlet_Cod") or f"row{i + 1}"
        brand = attrs.get("Brand_Name") or "?"
        brands[brand] = brands.get(brand, 0) + 1
        if attrs.get("DSE_CODE"):
            dse.add(attrs["DSE_CODE"])

        # An outlet code can repeat across brands -- the same shop carries
        # both IM3 and 3ID and appears once per brand. The key has to carry
        # the brand or the second row silently replaces the first.
        fkey = f"{brand}|{code}"
        if fkey in seen:
            dupes += 1
            fkey = f"{fkey}#{i + 1}"
        seen.add(fkey)
        name = attrs.get("Outlet_Nam") or code
        rows.append((OUTLET_LAYER, fkey, name, il.norm_key(fkey),
                     json.dumps(attrs, ensure_ascii=False),
                     json.dumps({"type": "Point", "coordinates": [lon, lat]}),
                     lon, lat, lon, lat, lat, lon, now))

    return rows, seen, outside, nocoord, dupes, dse, brands, offmap[0]


def import_outlet_dse(con, path, dry=False):
    """One row per outlet, carrying the rep who serves it.

    Every outlet is a point, and the DSE territory model is built from these
    points and nothing else -- there is no published DSE boundary, the shape
    is inferred from where a rep's outlets actually are.
    """
    wb, title, header, body = _stream_sheet(path, OUTLET_REQUIRED)
    base = os.path.basename(path)
    now = db.utcnow()
    try:
        idx = _index(header)
        _need(idx, OUTLET_REQUIRED, "a DSE-to-outlet mapping workbook")
        rows, seen, outside, nocoord, dupes, dse, brands, offmap = _read_outlets(
            body, idx, now)
    finally:
        wb.close()

    log(f"  {base}[{title}]: {len(rows):,} outlets in scope ({REGION}) "
        f"— {outside:,} outside it")
    log(f"      {len(dse):,} DSE · "
        + " · ".join(f"{k} {v:,}" for k, v in sorted(brands.items())))
    if nocoord:
        log(f"      WARN {nocoord:,} rows had no usable coordinates and were "
            f"left out — they cannot be placed or assigned to a territory.")
    if offmap:
        log(f"      WARN {offmap:,} rows had coordinates outside "
            f"{config.HOME_NAME} — wrong sign or wrong place, left out.")
    if dupes:
        log(f"      NOTE {dupes:,} outlet codes repeated within a brand; the "
            f"later ones were kept under a suffixed key rather than dropped.")
    if dry or not rows:
        return len(rows)

    con.execute("DELETE FROM geo_feature WHERE layer_key = ?", (OUTLET_LAYER,))
    con.executemany(
        "INSERT OR REPLACE INTO geo_feature (layer_key,feature_key,name,"
        "join_key,attrs_json,geometry_geojson,minlon,minlat,maxlon,maxlat,"
        "centroid_lat,centroid_lon,imported_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows)
    con.execute(
        "INSERT OR REPLACE INTO geo_layer (layer_key,label,kind,source_file,"
        "feature_count,attr_fields,imported_utc,notes) VALUES (?,?,?,?,?,?,?,?)",
        (OUTLET_LAYER, "Outlet → DSE mapping", "point", base, len(rows),
         ",".join(f for _, f in OUTLET_FIELDS), now,
         f"scope {REGION}; {outside:,} rows outside it were not imported"))
    con.commit()

    # Placing sites against this geography is NOT done here. With 496 reps
    # and 26,342 outlets the coverage model is minutes of work, and minutes
    # of work hidden inside an upload is an upload that looks like it has
    # hung. It is a declared step on the Configuration page instead, which
    # also makes the two uploads order-independent: fill the four slots in
    # any order, then run it once.
    try:
        n_sites = con.execute("SELECT count(*) FROM ref_site").fetchone()[0]
    except sqlite3.OperationalError:
        n_sites = 0
    if n_sites:
        log(f"      {n_sites:,} sites are loaded and can now be placed — run "
            f"\"Place sites against the DSE geography\" on the Configuration "
            f"page.")
    return len(rows)


# ── 3. site locations -> a point layer AND ref_site ──────────────────────
SITE_LAYER = "site_locations"
SITE_REQUIRED = ["NEW SITE ID", "LONGITUDE", "LATITUDE"]
SITE_EXTRA = [("SITE_TYPE", "SITE_TYPE"), ("ADDRESSABLE", "ADDRESSABLE"),
              ("CATEGORY", "CATEGORY")]


def import_site_locations(con, path, dry=False):
    """Sites, as both a map layer and a reference table.

    The parent app could do one or the other: uploading the workbook through
    the web made a point layer and left `ref_site` empty, and filling
    `ref_site` meant running the importer from a command line. They are the
    same 5,698 sites and there is no reason to choose, so this does both from
    one upload.

    The column names move between exports -- "New Site ID" one month,
    "SITE ID" the next -- so each field accepts the spellings that have
    actually been seen rather than one canonical name.
    """
    title, header, body = _sheet(path)
    idx = _index(header)
    alias = {
        "NEW SITE ID": ["NEW SITE ID", "SITE ID", "SITE_ID", "NEW_SITE_ID"],
        "NEW SITE NAME": ["NEW SITE NAME", "SITE NAME", "SITE_NAME",
                          "NEW_SITE_NAME"],
        "LONGITUDE": ["LONGITUDE", "LONG", "LON"],
        "LATITUDE": ["LATITUDE", "LAT"],
    }
    resolved = {}
    for want, options in alias.items():
        for o in options:
            if o in idx:
                resolved[want] = o
                break
    missing = [w for w in SITE_REQUIRED if w not in resolved]
    if missing:
        raise ValueError(
            "This does not look like a site location workbook. No column for "
            + ", ".join(m.title() for m in missing)
            + ".  Columns found: " + ", ".join(sorted(idx)[:12])
            + ("…" if len(idx) > 12 else ""))

    base = os.path.basename(path)
    now = db.utcnow()
    feats, sites, seen, nocoord, swapped = [], [], set(), 0, 0
    offmap = 0
    for i, r in enumerate(body):
        if not r or all(c is None for c in r):
            continue
        sid = _get(r, idx, resolved["NEW SITE ID"])
        if not sid:
            continue
        lat = il.as_num(r[idx[resolved["LATITUDE"]]])
        lon = il.as_num(r[idx[resolved["LONGITUDE"]]])
        if lat is not None and lon is not None and abs(lat) > 90 >= abs(lon):
            lat, lon = lon, lat
        if lat is not None and lon is not None and not config.on_map(lat, lon):
            # Three masts in this reference sit at 0, 0 -- the Gulf of
            # Guinea. A missing coordinate written as a zero is still a
            # missing coordinate, so it is reported as one rather than
            # drawn where the map has no land.
            offmap += 1
            lat = lon = None
            swapped += 1
        if lat is None or lon is None:
            nocoord += 1

        attrs = {"SITE_ID": sid}
        nm = _get(r, idx, resolved.get("NEW SITE NAME", "")) if \
            resolved.get("NEW SITE NAME") else None
        if nm:
            attrs["SITE_NAME"] = nm
        for key in idx:
            if key in (resolved.get("NEW SITE ID"), resolved.get("NEW SITE NAME"),
                       resolved.get("LATITUDE"), resolved.get("LONGITUDE")):
                continue
            v = _get(r, idx, key)
            if v is not None:
                attrs[key.replace(" ", "_")] = v

        fkey = sid if sid not in seen else f"{sid}#{i + 1}"
        seen.add(fkey)
        if lat is not None and lon is not None:
            feats.append((SITE_LAYER, fkey, nm or sid, il.norm_key(fkey),
                          json.dumps(attrs, ensure_ascii=False),
                          json.dumps({"type": "Point", "coordinates": [lon, lat]}),
                          lon, lat, lon, lat, lat, lon, now))
        sites.append((sid, nm, lat, lon, None, None, None, None, None, None,
                      None, json.dumps(attrs, ensure_ascii=False), base, now))

    log(f"  {base}[{title}]: {len(sites):,} sites, {len(feats):,} placed")
    if offmap:
        log(f"      WARN {offmap:,} sites had coordinates outside "
            f"{config.HOME_NAME} (0,0 or wrong sign) and were left unplaced.")
    if swapped:
        log(f"      NOTE {swapped:,} rows had longitude and latitude the wrong "
            f"way round — corrected on import, the workbook is unchanged.")
    if nocoord:
        log(f"      WARN {nocoord:,} sites have no usable coordinates. They "
            f"load into ref_site but cannot be drawn or given a DSE.")
    if dry or not sites:
        return len(sites)

    con.execute("DELETE FROM geo_feature WHERE layer_key = ?", (SITE_LAYER,))
    if feats:
        con.executemany(
            "INSERT OR REPLACE INTO geo_feature (layer_key,feature_key,name,"
            "join_key,attrs_json,geometry_geojson,minlon,minlat,maxlon,maxlat,"
            "centroid_lat,centroid_lon,imported_utc) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", feats)
    con.execute(
        "INSERT OR REPLACE INTO geo_layer (layer_key,label,kind,source_file,"
        "feature_count,attr_fields,imported_utc,notes) VALUES (?,?,?,?,?,?,?,?)",
        (SITE_LAYER, "Site locations", "point", base, len(feats),
         ",".join(sorted(idx)), now, None))
    con.executemany(
        "INSERT OR REPLACE INTO ref_site (site_id,site_name,lat,lon,vlr,"
        "dse_code,dse_assigned,assign_mode,desa,kecamatan,kabkot,attrs_json,"
        "source_file,imported_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", sites)
    con.commit()

    # Assigning each site a rep is a separate, declared step -- see the note
    # in import_outlet_dse. The sites are complete and correct without it;
    # what they lack is a DSE, and that is stated on the page rather than
    # paid for silently on every upload.
    log("      sites loaded. Run \"Place sites against the DSE geography\" on "
        "the Configuration page to give each one a rep.")
    return len(sites)


# ── 4. which boundary file is this, and is it the right one? ─────────────
# Both microcluster generations claim the same layer key in
# import_local.LAYER_RULES, and the 24-MC rule does not merge -- it deletes
# and replaces. So uploading the MCAgus file would swap the primary cut from
# 36 microclusters to 24 without a word, and every figure joined on mc36
# would quietly change meaning. The Regional Head's decision (2026-08-25) is
# that MC-36 is primary. A guard that refuses by name is the cheapest way to
# keep that decision from being undone by a drag and drop.
MC_PRIMARY = "MC36"

BOUNDARY_KINDS = {
    "desa": {"needs": {"KEL_DES"}, "label": "desa / kelurahan polygon"},
    "kecamatan": {"needs": {"KEC", "KABKOT"}, "label": "kecamatan / kota polygon"},
    "mc36": {"needs": {"MC36"}, "label": "microcluster polygon, MC-36 cut"},
    "mcagus": {"needs": {"MC_IOH"}, "label": "microcluster polygon, 24-MC MCAgus cut"},
}


def boundary_kind(path):
    """-> (kind, label, feature count). Reads the file, never the file name.

    An export called `tmpb8_uphst.kmz` tells you nothing, and the placemark
    <name> in these files is the province on one export and the DSE code on
    another, so neither is trusted either."""
    feats, fields, _links = il.parse_kml_file(path)
    up = {str(f).upper() for f in fields}
    # Most specific first: the desa export also carries KEC and would answer
    # to the kecamatan test, which is how 121 polygons get replaced by 887.
    for kind in ("desa", "mc36", "mcagus", "kecamatan"):
        if BOUNDARY_KINDS[kind]["needs"] <= up:
            return kind, BOUNDARY_KINDS[kind]["label"], len(feats)
    return "unknown", "an unrecognised polygon export", len(feats)


def check_boundary(path, allowed):
    """Raise unless the file is one of the kinds this slot accepts."""
    kind, label, n = boundary_kind(path)
    if kind in allowed:
        return kind, label, n
    if kind == "mcagus" and "mc36" in allowed:
        raise ValueError(
            f"This is the 24-microcluster MCAgus cut ({n} polygons). MC-36 is "
            f"the primary cut in this application, and both files claim the "
            f"same layer — loading this one would replace the 36-MC "
            f"boundaries and every figure joined to them. Upload the MC-36 "
            f"export (mc36_all_inner_reff.kmz or equivalent) instead.")
    if kind == "mc36" and "mc36" not in allowed:
        raise ValueError(
            f"This is the microcluster polygon ({n} polygons). It belongs in "
            f"the MC Hierarchy slot, not this one.")
    if kind in ("desa", "kecamatan") and kind not in allowed:
        raise ValueError(
            f"This is the {label} ({n} polygons). It belongs in the Desa "
            f"Polygon & Profiles slot, not this one.")
    raise ValueError(
        f"{label} — this file carries none of the fields that identify a "
        f"boundary layer (KEL_DES, KEC + KABKOT, MC36 or MC_IOH). Check the "
        f"placemark table against the field specification before uploading.")


# ── 5. the KML layers, streamed ──────────────────────────────────────────
# Provinces the Jakarta Raya circle lies inside. The boundary exports are
# national and carry no region column, so this is the only filter they can
# be given at import time -- it is deliberately loose (it keeps some West
# Java kabupaten outside the circle) and the exact cut is made later by the
# join to the territory layer. Set PJP_SCOPE_PROV empty to import everything.
SCOPE_PROV = tuple(p.strip().upper() for p in os.getenv(
    "PJP_SCOPE_PROV", "DKI JAKARTA,JAWA BARAT,BANTEN").split(",") if p.strip())

BATCH = 2000


def import_kml_layer(con, path, spec=None, dry=False):
    """Stream a KML into geo_feature, applying the layer's field model.

    Nothing about this is clever; what makes it different from the importer
    it replaces is that it never holds more than one placemark and one batch
    of rows in memory, so an 815 MB national export loads on the same laptop
    as an 800 KB one."""
    import kmllayers as kl
    import kmlstream as ks

    base = os.path.basename(path)
    if spec is None:
        fields = [f["name"] for f in ks.scan_fields(path, limit=40)]
        spec = kl.classify(fields)
        if spec is None:
            raise ValueError(
                "This KML carries none of the field sets that identify a "
                "layer (KEL_DES / Kec+Kab_kot+Populasi / KAB_KOT+"
                "Kabupaten_Priority / MC IOH+BRANCH+REGION / New Site ID). "
                "Fields seen: " + ", ".join(fields[:12])
                + ("…" if len(fields) > 12 else ""))

    layer_key = spec["layer_key"]
    clip = kl.scoped(spec)
    drop = set(spec.get("drop", ()))
    rename = spec.get("rename", {})
    now = db.utcnow()
    rows, kept, outside, nogeom, seen = [], 0, 0, 0, {}
    offmap = 0

    def flush():
        if rows and not dry:
            con.executemany(
                "INSERT OR REPLACE INTO geo_feature (layer_key,feature_key,"
                "name,join_key,attrs_json,geometry_geojson,minlon,minlat,"
                "maxlon,maxlat,centroid_lat,centroid_lon,imported_utc) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            con.commit()
        rows.clear()

    if not dry:
        con.execute("DELETE FROM geo_feature WHERE layer_key = ?", (layer_key,))
        con.commit()

    field_order = []
    for name, attrs, geom, bbox, cent in ks.placemarks(path):
        # scope, where the file itself can answer it -- and only for the
        # layers that are clipped at all. The national ones count what falls
        # outside the three regions and keep it, so it can be drawn grey.
        rf = spec.get("region_field")
        pf = spec.get("prov_field")
        if clip:
            if rf and not in_scope(attrs.get(rf)):
                outside += 1
                continue
            if not rf and pf and SCOPE_PROV:
                if (attrs.get(pf) or "").strip().upper() not in SCOPE_PROV:
                    outside += 1
                    continue
        out_of_scope = False
        if not clip:
            reg = attrs.get(rf) if rf else None
            prov = (attrs.get(pf) or "").strip().upper() if pf else ""
            out_of_scope = (not in_scope(reg)) if rf else (
                bool(SCOPE_PROV) and prov not in SCOPE_PROV)
            if out_of_scope:
                outside += 1
        if geom is None:
            nogeom += 1
            continue
        # Coordinates decide, not the attribute table. Twenty kecamatan in
        # this export are tagged BANTEN or JAWA BARAT and carry a shape in
        # Central Java; drawn, they scatter borders across the map behind
        # the working area. A province column that disagrees with its own
        # geometry is a broken row.
        if not config.on_map(cent[0], cent[1]):
            offmap += 1
            continue

        a = {}
        for k, v in attrs.items():
            if k in drop:
                continue
            a[rename.get(k, k)] = v
        a.update(kl.derive(spec, attrs))
        # A polygon the territory model does not cover is kept and marked,
        # not dropped. The map reads this to draw it grey.
        if not clip:
            a["_OUTSIDE"] = "1" if out_of_scope else "0"
        for k in a:
            if k not in field_order:
                field_order.append(k)

        label = a.get(spec["name_field"]) or name or f"{layer_key}-{kept + 1}"
        key = label
        if key in seen:
            seen[key] += 1
            key = f"{label}#{seen[label]}"
        else:
            seen[key] = 1
        rows.append((layer_key, key, label, il.norm_key(key),
                     json.dumps(a, ensure_ascii=False),
                     json.dumps(geom, separators=(",", ":")),
                     bbox[0], bbox[1], bbox[2], bbox[3], cent[0], cent[1], now))
        kept += 1
        if len(rows) >= BATCH:
            flush()
    flush()

    if not clip:
        scope_note = f"national, {outside:,} of {kept:,} outside {REGION}"
    elif spec.get("region_field"):
        scope_note = f"region scope {REGION}"
    elif SCOPE_PROV:
        scope_note = f"provinces {', '.join(SCOPE_PROV)}"
    else:
        scope_note = "no scope filter"
    log(f"  {base}: {kept:,} {spec['label']} kept ({scope_note})"
        + (f", {outside:,} outside" if outside and clip else "")
        + (f", {offmap:,} off-map" if offmap else "")
        + (f", {nogeom:,} without geometry" if nogeom else ""))
    if not dry:
        con.execute(
            "INSERT OR REPLACE INTO geo_layer (layer_key,label,kind,"
            "source_file,feature_count,attr_fields,imported_utc,notes) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (layer_key, spec["label"], spec["kind"], base, kept,
             ",".join(field_order), now, scope_note))
        con.commit()
    return kept
