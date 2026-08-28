#!/usr/bin/env python3
"""
Import locally-held reference data — boundaries and internal business
figures — into the same database as the API pulls, but into its own tables.

    python import_local.py --inspect            # read everything, write nothing
    python import_local.py --all                # import the whole folder
    python import_local.py --file "Data Upload/Hierarchy mc_reff_2026-07-01.xlsx"

Handles two shapes:

  KMZ / KML  ->  geo_layer + geo_feature
      Polygon boundaries: administrative kecamatan, Indosat microclusters,
      anything else you export from Google Earth or ArcGIS. Geometry is
      stored as GeoJSON text with a bounding box, so point-in-polygon works
      with no GIS extension installed.

  XLSX       ->  ref_mc / ref_kecamatan / ref_store / ref_agent / ref_metric
      The file's own header row decides which table it lands in, so you do
      not have to remember a flag per file.

Three things this handles that bite every hand-rolled importer:

  * These KMZ files carry no ExtendedData. The attributes are buried in an
    HTML table inside <description>, the way ArcGIS and Google Earth export
    them. They get scraped back out into real fields.

  * Kecamatan is keyed differently in each file — "BABELANBEKASI" in the
    KMZ, "BABELAN|BEKASI" in the spreadsheet. Both are reduced to the same
    `join_key` so the boundary and the profile actually join.

  * Metrics go into ref_metric long, not wide. The source headers carry the
    period in the column name ("VLR SUBS MTD 1708"), so a wide table would
    need a schema migration every refresh. Rows absorb a new month for free.
"""
import argparse
import html
import json
import os
import re
import sqlite3
import sys
import time
import hashlib
import urllib.error
import urllib.request
import zipfile
from xml.etree import ElementTree as ET

import config
import db
import geom

KML_NS = {"k": "http://www.opengis.net/kml/2.2"}
DEFAULT_DIR = os.path.join(config.BASE_DIR, "Data Upload")

# Indonesia's envelope, used only to catch transposed coordinates.
ID_LAT = (-11.5, 6.5)
ID_LON = (94.5, 141.5)


def log(m):
    print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)


MYMAPS_RX = re.compile(r"[?&]mid=([A-Za-z0-9_\-]+)")


def normalise_link(href):
    """Google My Maps serves a NetworkLink stub by default. Asking the same
    map id with forcekml=1 returns the actual placemarks instead of another
    pointer, which is the difference between 0 features and all of them."""
    href = (href or "").strip()
    m = MYMAPS_RX.search(href)
    if m and "google.com/maps/d" in href:
        return f"https://www.google.com/maps/d/kml?mid={m.group(1)}&forcekml=1"
    return href


def fetch_url(url, timeout=90):
    req = urllib.request.Request(
        url, headers={"User-Agent": "API-Location-Pulldown/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# An IPP folder, or a name like "DRYNA CELL" / "59 Cell", is an independent
# counter, not an operator-branded service point. They are a different class
# of place and must not be counted as competitor presence.
OUTLET_FOLDER_RX = re.compile(r"\bipp\b|konter|\bcounter\b|pulsa", re.I)
OUTLET_NAME_RX = re.compile(r"\bcell\b|\bcel\b|\bponsel\b|\bselular\b"
                            r"|\bseluler\b|konter", re.I)


def classify_point(operator_raw, folder, name, sp_type):
    """-> (operator_code, sp_class). Folder wins: in a My Maps export it is
    the curator's own grouping, which beats guessing from a trading name."""
    if OUTLET_FOLDER_RX.search(folder or ""):
        return "IPP", "outlet"
    code = operator_of(operator_raw, folder, name, sp_type)
    if code == "OTHER" and OUTLET_NAME_RX.search(name or ""):
        return "IPP", "outlet"
    return code, ("operator" if code != "OTHER" else "unknown")


def stable_key(name, lat, lon):
    """Content-derived, so the same point imported from the file and from
    the URL collapses onto one row instead of being stored twice."""
    blob = f"{(name or '').strip().lower()}|{round(float(lat), 6)}|" \
           f"{round(float(lon), 6)}"
    return "sp:" + hashlib.sha1(blob.encode("utf-8")).hexdigest()[:20]


def operator_of(*texts):
    """Canonical operator code from whatever the source calls it. Returns
    'OTHER' rather than None so an unclassified point still renders on the
    map -- an invisible competitor is worse than a grey dot."""
    blob = " ".join(str(t) for t in texts if t).lower()
    if not blob.strip():
        return "OTHER"
    for code, pat in config.OPERATOR_PATTERNS:
        if re.search(pat, blob):
            return code
    return "OTHER"


def norm_key(*parts):
    """A join key that survives the formatting differences between files:
    'BABELAN|BEKASI' and 'BABELANBEKASI' both reduce to BABELANBEKASI."""
    joined = "".join(str(p or "") for p in parts)
    return re.sub(r"[^A-Z0-9]", "", joined.upper())


# ── KML / KMZ ────────────────────────────────────────────────────────────
def kml_bytes(data):
    """A .kmz is a zip, a .kml is XML, and a download can be either without
    telling you which. Sniff the zip magic rather than trusting the name."""
    if data[:2] == b"PK":
        import io
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".kml")]
            if not names:
                raise ValueError("no .kml inside the archive")
            return z.read(names[0])
    return data


def read_kml(path):
    with open(path, "rb") as fh:
        return kml_bytes(fh.read())


def coords(text):
    out = []
    for tok in (text or "").split():
        bits = tok.split(",")
        if len(bits) >= 2:
            try:
                out.append([float(bits[0]), float(bits[1])])
            except ValueError:
                continue
    return out


def polygon_rings(poly):
    outer = poly.find("k:outerBoundaryIs/k:LinearRing/k:coordinates", KML_NS)
    if outer is None:
        return None
    rings = [coords(outer.text)]
    for inner in poly.findall("k:innerBoundaryIs/k:LinearRing/k:coordinates",
                              KML_NS):
        ring = coords(inner.text)
        if ring:
            rings.append(ring)
    return rings if rings[0] else None


def geometry_of(pm):
    polys, pts, lines = [], [], []
    for el in pm.iter():
        tag = el.tag.split("}")[-1]
        if tag == "Polygon":
            rings = polygon_rings(el)
            if rings:
                polys.append(rings)
        elif tag == "Point":
            c = el.find("k:coordinates", KML_NS)
            got = coords(c.text) if c is not None else []
            if got:
                pts.append(got[0])
        elif tag == "LineString":
            c = el.find("k:coordinates", KML_NS)
            got = coords(c.text) if c is not None else []
            if got:
                lines.append(got)
    if polys:
        return ({"type": "Polygon", "coordinates": polys[0]} if len(polys) == 1
                else {"type": "MultiPolygon", "coordinates": polys})
    if lines:
        return ({"type": "LineString", "coordinates": lines[0]}
                if len(lines) == 1
                else {"type": "MultiLineString", "coordinates": lines})
    if pts:
        return ({"type": "Point", "coordinates": pts[0]} if len(pts) == 1
                else {"type": "MultiPoint", "coordinates": pts})
    return None


def scrape_description(desc):
    """ArcGIS and Google Earth write attributes as an HTML table inside
    <description> rather than as ExtendedData. Pull them back out.

    Parsed row by row, not as a flat list of cells. The flat version dropped
    empty cells before pairing, so a single blank value — Desa_Name in the
    outlet export — shifted every field after it onto the wrong key. A <tr>
    holding exactly two cells is a key/value pair; anything else (the title
    row, spacers) is skipped.
    """
    out = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", desc or "", re.S | re.I):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)
        cells = [re.sub(r"<[^>]+>", "", html.unescape(c)).strip()
                 for c in cells]
        if len(cells) == 2 and cells[0]:
            out[cells[0]] = cells[1]
    return out


def attrs_of(pm):
    out = {}
    for sd in pm.findall(".//k:SimpleData", KML_NS):
        if sd.get("name"):
            out[sd.get("name")] = (sd.text or "").strip()
    for d in pm.findall(".//k:Data", KML_NS):
        if d.get("name"):
            out[d.get("name")] = (d.findtext("k:value", "", KML_NS) or "").strip()
    if not out:
        out = scrape_description(pm.findtext("k:description", "", KML_NS))
    return out


# Rules are (attribute signature, layer_key, label, key field, name field,
# join fields, merge_parts). The key field must be UNIQUE within the layer
# unless merge_parts is set, and the name field need not be. Keying kecamatan
# on KEC alone silently lost a polygon, because two kabupaten can each
# contain a kecamatan of the same name -- KECKAB is the field that actually
# distinguishes them.
#
# merge_parts says the key names an ENTITY rather than a shape, so several
# placemarks sharing it are pieces of one thing and must be combined. The
# MC36_SA12 export ships 39 placemarks for 36 microclusters because
# MC-EAST CIKAMPEK, MC-PANGKALAN and MC-PEBAYURAN are each split by
# territory they do not cover. Storing those as "MC-PANGKALAN#12" would put
# a made-up microcluster on the map and leave the real one missing a limb.
LAYER_RULES = [
    # MC36 is the current generation: the MC35 cut with MC-CIKARANG split
    # into MC-CIBARUSAH and MC-CIKARANG BARAT. BRANCH12 splits the single
    # "BEKASI" branch into NORTH and SOUTH BEKASI. It replaces the older
    # microcluster layer rather than sitting beside it, so it claims the
    # same layer_key.
    ({"MC36"}, "indosat_mc", "Indosat microcluster boundaries (MC36 / SA12)",
     "MC36", "MC36", ("MC36",), True),
    ({"MC_IOH"}, "indosat_mc", "Indosat microcluster boundaries",
     "MC_IOH", "MC_IOH", ("MC_IOH",), False),
    # The 2308 desa export carries no UNIQUE_ID, so it is keyed on the
    # composite that is actually unique across all 887 rows. Tested before
    # the older desa rule because it also carries KEL_DES.
    # KAB_KOT is renamed to KABKOT by slim_kelurahan_attrs before the key is
    # taken, so the join fields must name it as it will be, not as it
    # arrives. No key field: a desa name repeats across kecamatan -- 63 of
    # the 887 collide -- so the key is built from the join fields, which is
    # the combination that is actually unique.
    ({"KEL_DES", "KEC", "KAB_KOT", "MC36"}, "kelurahan",
     "Administrative desa / kelurahan boundaries (MC36 / SA12)",
     None, "KEL_DES", ("KEL_DES", "KEC", "KABKOT"), False),
    # Desa/kelurahan must be tested before kecamatan: the desa export carries
    # KEC as well, and would otherwise be swallowed by the kecamatan rule and
    # overwrite 121 kecamatan polygons with 887 desa ones.
    ({"KEL_DES", "UNIQUE_ID"}, "kelurahan",
     "Administrative desa / kelurahan boundaries",
     "UNIQUE_ID", "KEL_DES", ("KEL_DES", "KEC", "KABKOT"), False),
    ({"KEC", "KABKOT"}, "kecamatan", "Administrative kecamatan boundaries",
     "KECKAB", "KEC", ("KEC", "KABKOT"), False),
]

# The desa export is the product of a spatial join and carries every source
# field twice — Kel_des and Kel_des_1, Populasi_2 and Populasi_3, and a pair
# of Unique_ID_ / Unique_ID1 that are not identifiers at all but stray
# coordinates. Storing all 43 keys per polygon triples attrs_json and puts
# junk in the popup, so the duplicates are dropped on import.
KEL_DROP_SUFFIX = ("_1", "_2", "_3")
KEL_DROP_EXACT = {"UNIQUE_ID_", "UNIQUE_ID1", "KABKEC_200", "KABKEC_201",
                  "KABKEC_202", "KABKEC_203", "FID"}
KEL_RENAME = {"POPULASI_2": "POPULATION", "INDEX_POPU": "POP_INDEX",
              "LUAS_DESA": "AREA_M2", "LONG_MUK": "LON", "LAT_MUK": "LAT",
              "KAB_KOT": "KABKOT", "KEL_DES": "KEL_DES", "PARTNER_AF": "PARTNER"}


def slim_kelurahan_attrs(attrs):
    """Keep one clean copy of each field, upper-cased and renamed."""
    out = {}
    for k, v in attrs.items():
        ku = k.upper()
        if ku in KEL_DROP_EXACT or ku.endswith(KEL_DROP_SUFFIX):
            if ku not in ("POPULASI_2",):      # Populasi_2 is the real one
                continue
        if v is None or str(v).strip() == "":
            continue
        out[KEL_RENAME.get(ku, ku)] = str(v).strip()
    return out


def classify_layer(fields, path):
    """Most specific rule wins, not first-listed.

    Signatures overlap by nature -- the desa export carries MC36 as well as
    KEL_DES, so a rule needing only {MC36} matches it too. Relying on the
    order rules happen to be written in is how the 2308 desa file was
    classified as the microcluster layer and overwrote it: 887 desa polygons
    merged into 36 shapes that looked plausible enough to miss. Ranking by
    how many fields a rule demands makes the specific rule win wherever both
    apply, and stops the next overlapping export doing the same thing."""
    have = {f.upper() for f in fields}
    for need, key, label, keyfield, namefield, joinfields, merge in sorted(
            LAYER_RULES, key=lambda r: -len(r[0])):
        if need <= have:
            return key, label, keyfield, namefield, joinfields, merge
    slug = re.sub(r"[^a-z0-9]+", "_",
                  os.path.splitext(os.path.basename(path))[0].lower()).strip("_")
    return slug, os.path.basename(path), None, None, (), False


def _placemark(pm, folders):
    attrs = attrs_of(pm)
    geom = geometry_of(pm)
    if geom is None:
        return None
    return attrs, geom, folders


def parse_kml_bytes(data):
    """Returns (features, attribute fields, networklink hrefs).

    Placemarks are walked folder by folder rather than with a flat
    `.//Placemark`, because in a My Maps export the folder IS the operator
    name — throwing that away is throwing away the classification."""
    root = ET.fromstring(data)
    feats, fields, links = [], set(), []
    for ln in root.findall(".//k:NetworkLink", KML_NS):
        href = ln.findtext("k:Link/k:href", None, KML_NS) or \
            ln.findtext("k:Url/k:href", None, KML_NS)
        if href:
            links.append(href)

    def walk(node, path):
        for child in list(node):
            tag = child.tag.split("}")[-1]
            if tag in ("Document", "Folder"):
                nm = (child.findtext("k:name", "", KML_NS) or "").strip()
                walk(child, path + ([nm] if nm else []))
            elif tag == "Placemark":
                got = _placemark(child, list(path))
                if got:
                    _collect(got, child, feats, fields)

    walk(root, [])
    return feats, sorted(fields), links


def _collect(got, pm, feats, fields):
    attrs, geom, folders = got
    fields.update(attrs)
    if True:
        flat = []
        def walk_coords(x):
            if (isinstance(x, list) and len(x) == 2
                    and all(isinstance(v, (int, float)) for v in x)):
                flat.append(x)
            elif isinstance(x, list):
                for y in x:
                    walk_coords(y)
        walk_coords(geom["coordinates"])
        if not flat:
            return
        lons = [p[0] for p in flat]
        lats = [p[1] for p in flat]
        feats.append({
            "name": (pm.findtext("k:name", "", KML_NS) or "").strip() or None,
            "attrs": attrs, "geom": geom, "folders": folders,
            "folder": " / ".join(folders) or None,
            "minlon": min(lons), "maxlon": max(lons),
            "minlat": min(lats), "maxlat": max(lats),
            "clat": sum(lats) / len(lats), "clon": sum(lons) / len(lons),
        })


def parse_kml_file(path):
    return parse_kml_bytes(read_kml(path))


SP_FILE_RX = re.compile(r"service\s*point|operator|competit|\bsp\b", re.I)
OP_FIELDS = ("OPERATOR", "OPR", "BRAND", "PROVIDER", "TELCO", "OPERATOR_NAME")


def import_service_points(con, path, feats, dry=False):
    """Competitor and own service points, coloured by operator on the map.
    Kept in their own table rather than as a geo_feature layer because they
    are points with an operator, not boundaries."""
    base = os.path.basename(path)
    now = db.utcnow()
    rows, tally, seen = [], {}, set()
    for i, f in enumerate(feats):
        a = {k.upper(): v for k, v in f["attrs"].items()}
        raw = next((a[k] for k in OP_FIELDS if a.get(k)), None)
        op, klass = classify_point(raw, f.get("folder"), f["name"],
                                   a.get("TYPE"))
        tally[op] = tally.get(op, 0) + 1
        key = stable_key(f["name"], f["clat"], f["clon"])
        if key in seen:
            continue                    # same point listed twice in the map
        seen.add(key)
        rows.append((key, op, klass, raw or f.get("folder"), f["name"],
                     a.get("TYPE") or f.get("folder"),
                     a.get("ADDRESS") or a.get("ALAMAT"),
                     f["clat"], f["clon"],
                     json.dumps(f["attrs"], ensure_ascii=False), base, now))
    dropped = len(feats) - len(rows)
    log(f"  {base}: {len(rows):,} service points  "
        + "  ".join(f"{k}={v:,}" for k, v in sorted(tally.items())))
    if dropped:
        log(f"      {dropped:,} duplicate placemark(s) in the source "
            f"collapsed onto the same point")
    if tally.get("IPP"):
        log(f"      {tally['IPP']:,} are IPP outlets (independent counters), "
            f"classed separately from operator service points")
    if tally.get("OTHER"):
        log(f"      NOTE {tally['OTHER']:,} could not be attributed -- they "
            f"render grey. Add an OPERATOR field to the export, or extend "
            f"config.OPERATOR_PATTERNS.")
    if not dry and rows:
        # Legacy rows were keyed by source_file+index, so importing the same
        # map from the file and from its URL stored it twice. Content keys
        # fix that going forward; this clears what the old scheme left.
        n_old = con.execute(
            "DELETE FROM ref_service_point "
            "WHERE sp_key NOT LIKE 'sp:%' AND sp_key NOT LIKE 'store:%'"
        ).rowcount
        if n_old:
            log(f"      removed {n_old:,} row(s) written by the old "
                f"duplicate-prone key scheme")
        con.executemany(
            "INSERT OR REPLACE INTO ref_service_point (sp_key,operator,"
            "sp_class,operator_raw,name,sp_type,address,lat,lon,attrs_json,"
            "source_file,imported_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
    return len(rows)


def seed_service_points_from_stores(con, dry=False):
    """Your own gerai are service points too. Seeding them from ref_store
    means the IM3 layer is populated even before a competitor export
    arrives, and it is idempotent -- the key is derived, not generated."""
    now = db.utcnow()
    rows = [(f"store:{r[0]}", "IM3", "operator", r[3], r[1], r[2], r[4],
             r[5], r[6], None, "ref_store", now)
            for r in con.execute(
                "SELECT store_key, store_name, store_type, rso_name, address,"
                " lat, lon FROM ref_store WHERE lat IS NOT NULL")]
    if rows and not dry:
        con.executemany(
            "INSERT OR REPLACE INTO ref_service_point (sp_key,operator,"
            "sp_class,operator_raw,name,sp_type,address,lat,lon,attrs_json,"
            "source_file,imported_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
    if rows:
        log(f"  seeded {len(rows):,} IM3 service points from ref_store")
    return len(rows)


# geo_feature row layout, so combine_part reads as something other than
# a column of magic numbers.
_G_GEOM, _G_MINLON, _G_MINLAT, _G_MAXLON, _G_MAXLAT, _G_CLAT, _G_CLON = \
    5, 6, 7, 8, 9, 10, 11


def combine_part(row, part):
    """Fold another placemark's shape into an already-stored territory.

    The bounding box grows to cover both, and the centroid is re-taken as
    the middle of that box. A part-weighted centroid would be better, but
    the stored centroid is only used to put a label somewhere sensible and
    to test which territory a point falls in -- and that test walks the
    rings, never the centroid."""
    geom = json.loads(row[_G_GEOM])
    add = part["geom"]
    polys = (geom["coordinates"] if geom["type"] == "MultiPolygon"
             else [geom["coordinates"]])
    polys += (add["coordinates"] if add["type"] == "MultiPolygon"
              else [add["coordinates"]])
    row[_G_GEOM] = json.dumps({"type": "MultiPolygon", "coordinates": polys})
    row[_G_MINLON] = min(row[_G_MINLON], part["minlon"])
    row[_G_MINLAT] = min(row[_G_MINLAT], part["minlat"])
    row[_G_MAXLON] = max(row[_G_MAXLON], part["maxlon"])
    row[_G_MAXLAT] = max(row[_G_MAXLAT], part["maxlat"])
    row[_G_CLAT] = (row[_G_MINLAT] + row[_G_MAXLAT]) / 2.0
    row[_G_CLON] = (row[_G_MINLON] + row[_G_MAXLON]) / 2.0


def import_kml(con, path, override_key=None, dry=False, follow=True,
               _depth=0):
    feats, fields, links = parse_kml_file(path)
    base0 = os.path.basename(path)
    if not feats and links and follow and _depth < 2:
        # A KMZ holding only a NetworkLink is a pointer, not data. Google
        # Earth resolves it on open, which is why the file looks fine there
        # and imports as zero features here.
        for href in links[:4]:
            url = normalise_link(href)
            log(f"  {base0}: no placemarks in the file — it is a NetworkLink.")
            log(f"      following -> {url}")
            try:
                blob = fetch_url(url)
            except urllib.error.HTTPError as exc:
                log(f"      HTTP {exc.code} — the map is probably not shared "
                    f"publicly. In My Maps: Share -> 'Anyone with the link', "
                    f"or use Download KML and save the file here.")
                continue
            except Exception as exc:                    # noqa: BLE001
                log(f"      fetch failed ({type(exc).__name__}: {exc}). "
                    f"No internet, or the link has expired.")
                continue
            try:
                f2, fl2, _ = parse_kml_bytes(kml_bytes(blob))
            except Exception as exc:                    # noqa: BLE001
                log(f"      the link returned something unparseable: {exc}")
                continue
            log(f"      got {len(f2):,} placemarks "
                f"({len(blob)/1024:,.0f} KB)")
            feats.extend(f2)
            fields = sorted(set(fields) | set(fl2))
        if feats:
            folders = sorted({f.get("folder") for f in feats if f.get("folder")})
            if folders:
                log(f"      folders: {', '.join(folders[:10])}")
    # Points + an operator-ish filename or field means service points, not
    # a boundary layer. Route them to the table that knows about operators.
    if feats and all(f["geom"]["type"] in ("Point", "MultiPoint")
                     for f in feats):
        upper = {k.upper() for f in feats for k in f["attrs"]}
        if SP_FILE_RX.search(os.path.basename(path)) or (set(OP_FIELDS) & upper):
            return import_service_points(con, path, feats, dry=dry)

    key, label, keyfield, namefield, joinfields, merge_parts = \
        classify_layer(fields, path)
    if override_key:
        key = override_key
    base = os.path.basename(path)
    if not feats:
        log(f"  {base}: 0 placemarks with geometry — nothing to import. "
            f"Re-export it from Google Earth with the features included.")
        return 0
    log(f"  {base}: {len(feats):,} features -> layer '{key}'  "
        f"fields={fields or '(none)'}")
    if dry:
        for f in feats[:3]:
            log(f"      e.g. {f['name']!r} {f['attrs']}")
        return len(feats)

    now = db.utcnow()
    rows, seen, merged = [], {}, 0
    for i, f in enumerate(feats):
        if key == "kelurahan":
            f["attrs"] = slim_kelurahan_attrs(f["attrs"])
        a = {k.upper(): v for k, v in f["attrs"].items()}
        jkey = norm_key(*[a.get(j.upper()) for j in joinfields]) \
            if joinfields else None
        # A layer with no single unique field is keyed on the combination
        # that is unique. Falling through to the display name instead put 63
        # desa under names like 'SUKASARI#374' -- stable only until the
        # export is regenerated and the FIDs move.
        fkey = ((a.get(keyfield.upper()) if keyfield else None)
                or (jkey if (not keyfield and joinfields) else None)
                or (a.get(namefield.upper()) if namefield else None)
                or f["name"] or f"{key}-{i}")
        fkey = str(fkey).strip()
        if fkey in seen:
            if merge_parts:
                # Pieces of one territory, not two territories with the same
                # name. Fold this shape into the one already stored.
                combine_part(rows[seen[fkey]], f)
                merged += 1
                continue
            # Never let a key collision silently drop a polygon. Disambiguate
            # loudly instead — a missing boundary is invisible downstream.
            alt = f"{fkey}#{a.get('FID') or i}"
            log(f"      WARN duplicate key {fkey!r} -> stored as {alt!r}")
            fkey = alt
        if jkey is None:
            jkey = norm_key(fkey)
        display = (a.get(namefield.upper()) if namefield else None) or f["name"]
        # The vertex mean is not inside a bent or coastal shape. Store a
        # point that is, so everything downstream that asks "where is this
        # area" gets an answer within it.
        import geom as _geom
        clat, clon = _geom.inside_or_representative(
            _geom.rings_of(f["geom"]), f["clat"], f["clon"])
        f["clat"], f["clon"] = clat, clon
        seen[fkey] = len(rows)
        rows.append([key, fkey, display, jkey,
                     json.dumps(f["attrs"], ensure_ascii=False),
                     json.dumps(f["geom"]),
                     f["minlon"], f["minlat"], f["maxlon"], f["maxlat"],
                     f["clat"], f["clon"], now])
    if merged:
        log(f"      merged {merged} extra polygon(s) into the territory they "
            f"belong to -> {len(rows)} features")
    rows = [tuple(r) for r in rows]
    con.execute("DELETE FROM geo_feature WHERE layer_key = ?", (key,))
    con.executemany(
        "INSERT OR REPLACE INTO geo_feature (layer_key,feature_key,name,"
        "join_key,attrs_json,geometry_geojson,minlon,minlat,maxlon,maxlat,"
        "centroid_lat,centroid_lon,imported_utc) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    con.execute(
        "INSERT OR REPLACE INTO geo_layer (layer_key,label,kind,source_file,"
        "feature_count,attr_fields,imported_utc,notes) VALUES (?,?,?,?,?,?,?,?)",
        (key, label, "polygon", base, len(rows), ",".join(fields), now, None))
    con.commit()
    return len(rows)


# ── point tables (xlsx / csv) ────────────────────────────────────────────
# A site list arrives as a spreadsheet at least as often as a KML, and the
# column names are never the same twice. Match them by meaning rather than by
# position, keep every other column as an attribute, and the same importer
# handles a site export, an outlet export or anything else that is a list of
# places with coordinates.
LAT_NAMES = {"lat", "latitude", "lat_muk", "y", "lintang", "site_lat",
             "latitud", "lat_site", "coord_lat"}
LON_NAMES = {"lon", "long", "longitude", "lng", "x", "bujur", "site_long",
             "site_lon", "longitud", "lon_site", "coord_long", "coord_lon"}
NAME_NAMES = ["site_name", "sitename", "nama_site", "site_id", "siteid",
              "site", "name", "nama", "location", "lokasi", "id"]


def _norm_header(h):
    return re.sub(r"[^a-z0-9]+", "_", str(h or "").strip().lower()).strip("_")


def _table_rows(path):
    """(header, rows) from an xlsx/xlsm/csv, header found not assumed."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        import csv as _csv
        with open(path, newline="", encoding="utf-8-sig") as fh:
            grid = [r for r in _csv.reader(fh)]
    else:
        from openpyxl import load_workbook
        wb = load_workbook(path, read_only=True, data_only=True)
        grid = [list(r) for r in wb[wb.sheetnames[0]].iter_rows(values_only=True)]
        wb.close()
    if not grid:
        return [], []
    # The header is the first row that carries both a latitude and a
    # longitude column; exports often start with a title or a blank line.
    for i, row in enumerate(grid[:12]):
        norm = [_norm_header(c) for c in row]
        if any(n in LAT_NAMES for n in norm) and any(n in LON_NAMES for n in norm):
            return norm, grid[i + 1:]
    best = max(range(min(6, len(grid))),
               key=lambda i: sum(1 for c in grid[i] if str(c or "").strip()))
    return [_norm_header(c) for c in grid[best]], grid[best + 1:]


def _as_float(v):
    if v is None or v == "":
        return None
    try:
        return float(str(v).replace(",", ".").strip())
    except (TypeError, ValueError):
        return None


def import_points_table(con, path, override_key=None, label=None, dry=False):
    """Import a spreadsheet of places into geo_feature as points."""
    header, body = _table_rows(path)
    base = os.path.basename(path)
    if not header:
        log(f"  {base}: no readable rows")
        return 0
    lat_i = next((i for i, h in enumerate(header) if h in LAT_NAMES), None)
    lon_i = next((i for i, h in enumerate(header) if h in LON_NAMES), None)
    if lat_i is None or lon_i is None:
        raise ValueError(
            "No latitude/longitude columns found. Expected something like "
            "LAT / LONG (also accepted: latitude, longitude, x, y, bujur, "
            f"lintang). Columns seen: {', '.join(h for h in header if h) or '(none)'}")
    name_i = next((i for n in NAME_NAMES for i, h in enumerate(header) if h == n),
                  None)

    key = override_key or re.sub(r"[^a-z0-9]+", "_",
                                 os.path.splitext(base)[0].lower()).strip("_")
    now = db.utcnow()
    rows, seen, skipped, swapped = [], set(), 0, 0
    for i, r in enumerate(body):
        if r is None or not any(str(c or "").strip() for c in r):
            continue
        lat = _as_float(r[lat_i]) if lat_i < len(r) else None
        lon = _as_float(r[lon_i]) if lon_i < len(r) else None
        if lat is None or lon is None:
            skipped += 1
            continue
        # Columns labelled the wrong way round are unambiguous here:
        # latitude in this AOI is about -6 and longitude about 107.
        if abs(lat) > 90 >= abs(lon):
            lat, lon = lon, lat
            swapped += 1
        attrs = {}
        for j, h in enumerate(header):
            if not h or j in (lat_i, lon_i) or j >= len(r):
                continue
            v = r[j]
            if v is None or str(v).strip() == "":
                continue
            attrs[h.upper()] = str(v).strip()
        nm = (str(r[name_i]).strip() if name_i is not None and name_i < len(r)
              and r[name_i] is not None else None) or f"{key}-{i + 1}"
        fkey = nm if nm not in seen else f"{nm}#{i + 1}"
        seen.add(fkey)
        rows.append((key, fkey, nm, norm_key(fkey),
                     json.dumps(attrs, ensure_ascii=False),
                     json.dumps({"type": "Point", "coordinates": [lon, lat]}),
                     lon, lat, lon, lat, lat, lon, now))
    log(f"  {base}: {len(rows):,} points -> layer '{key}'"
        + (f"  ({skipped} rows without coordinates)" if skipped else "")
        + (f"  ({swapped} lat/long pairs were the wrong way round)" if swapped else ""))
    if dry or not rows:
        return len(rows)
    con.execute("DELETE FROM geo_feature WHERE layer_key = ?", (key,))
    con.executemany(
        "INSERT OR REPLACE INTO geo_feature (layer_key,feature_key,name,"
        "join_key,attrs_json,geometry_geojson,minlon,minlat,maxlon,maxlat,"
        "centroid_lat,centroid_lon,imported_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows)
    con.execute(
        "INSERT OR REPLACE INTO geo_layer (layer_key,label,kind,source_file,"
        "feature_count,attr_fields,imported_utc,notes) VALUES (?,?,?,?,?,?,?,?)",
        (key, label or base, "point", base, len(rows),
         ",".join(h.upper() for h in header if h), now, None))
    con.commit()
    return len(rows)


# ── XLSX ─────────────────────────────────────────────────────────────────
def sheet_rows(ws, max_scan=8):
    """Returns (header, rows). Real-world exports often have a blank first
    row, so the header is found rather than assumed."""
    grid = [list(r) for r in ws.iter_rows(values_only=True)]
    best, best_n = 0, -1
    for i, row in enumerate(grid[:max_scan]):
        n = sum(1 for c in row if isinstance(c, str) and c.strip())
        if n > best_n:
            best, best_n = i, n
    header = [str(c).strip() if c is not None else "" for c in grid[best]]
    body = [r for r in grid[best + 1:] if any(c is not None for c in r)]
    return header, body


def as_num(v):
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


PERIOD_RX = re.compile(r"\s+(MTD\s+\d+|\d{2,6})$", re.I)


def split_metric(header):
    """'PREPAID REVENUE NETT MTD 1708' -> ('prepaid_revenue_nett','MTD 1708')"""
    h = header.strip()
    m = PERIOD_RX.search(h)
    period = None
    if m:
        period = re.sub(r"\s+", " ", m.group(1).strip()).upper()
        h = h[:m.start()]
    return re.sub(r"[^a-z0-9]+", "_", h.lower()).strip("_"), period


def put_metrics(con, entity_type, entity_key, pairs, src, now):
    con.executemany(
        "INSERT INTO ref_metric (entity_type,entity_key,metric,period,"
        "value_num,value_text,source_file,imported_utc) "
        "VALUES (?,?,?,?,?,?,?,?) "
        "ON CONFLICT(entity_type,entity_key,metric,coalesce(period,'')) "
        "DO UPDATE SET value_num=excluded.value_num, "
        "value_text=excluded.value_text, imported_utc=excluded.imported_utc",
        [(entity_type, entity_key, m, p, n, t, src, now)
         for m, p, n, t in pairs])


KEC_IDS = {"KECAMATAN", "MC", "SA", "AREA", "REGION", "RANK"}


def stamp_sites(con, outlet_layer=None, field="DSE_CODE"):
    """Place every loaded site: desa/kecamatan by boundary, DSE by coverage.

    The site file is not required to carry a DSE, so the assignment is
    geographic: whichever inferred DSE coverage polygon contains the site.
    Where the file DOES carry one, both are kept -- `dse_code` as supplied and
    `dse_assigned` as computed -- because a site whose paperwork says one rep
    and whose position says another is exactly the thing a territory review
    exists to find.

    Sites outside every coverage polygon fall back to the nearest outlet's
    DSE, marked `nearest` rather than `polygon`, so a reader can tell a
    confident placement from a best guess.
    """
    import dse_coverage

    rows = [dict(r) for r in con.execute(
        "SELECT site_id, lat, lon FROM ref_site "
        "WHERE lat IS NOT NULL AND lon IS NOT NULL")]
    if not rows:
        return 0

    if outlet_layer is None:
        pick = con.execute(
            "SELECT layer_key FROM geo_layer WHERE layer_key NOT IN "
            "('kecamatan','indosat_mc','kelurahan') ORDER BY feature_count DESC"
        ).fetchone()
        outlet_layer = pick[0] if pick else None

    groups, pts = {}, []
    if outlet_layer:
        for r in con.execute(
                "SELECT attrs_json, centroid_lat, centroid_lon FROM geo_feature"
                " WHERE layer_key = ? AND centroid_lat IS NOT NULL",
                (outlet_layer,)):
            a = {k.upper(): v for k, v in
                 json.loads(r["attrs_json"] or "{}").items()}
            key = a.get(field.upper())
            if key is None or str(key).strip() == "":
                continue
            key = str(key).strip()
            groups.setdefault(key, []).append(
                (r["centroid_lat"], r["centroid_lon"]))
            pts.append((r["centroid_lat"], r["centroid_lon"], key))
    feats = dse_coverage.build(groups)[0] if groups else []

    kel = geom.build_index(geom.load_polys(con, "kelurahan"))
    kec = geom.build_index(geom.load_polys(con, "kecamatan"))

    updates, by_poly, by_near, unplaced = [], 0, 0, 0
    for r in rows:
        lat, lon = r["lat"], r["lon"]
        dse = dse_coverage.assign(feats, lat, lon) if feats else None
        mode = "polygon" if dse else None
        if not dse and pts:
            dse = min(pts, key=lambda p: (p[0] - lat) ** 2
                      + (p[1] - lon) ** 2)[2]
            mode = "nearest"
        if mode == "polygon":
            by_poly += 1
        elif mode == "nearest":
            by_near += 1
        else:
            unplaced += 1
        d = geom.locate_indexed(kel, lat, lon)
        k = geom.locate_indexed(kec, lat, lon)
        updates.append((dse, mode,
                        d["name"] if d else None,
                        (k["name"] if k else
                         (d["attrs"].get("KEC") if d else None)),
                        d["attrs"].get("KABKOT") if d else None,
                        r["site_id"]))
    con.executemany(
        "UPDATE ref_site SET dse_assigned=?, assign_mode=?, desa=?, "
        "kecamatan=?, kabkot=? WHERE site_id=?", updates)
    con.commit()
    log(f"      placed {by_poly:,} sites inside a DSE coverage polygon, "
        f"{by_near:,} by nearest outlet, {unplaced:,} unplaced")
    mism = con.execute(
        "SELECT count(*) FROM ref_site WHERE dse_code IS NOT NULL "
        "AND dse_assigned IS NOT NULL AND dse_code <> dse_assigned"
    ).fetchone()[0]
    if mism:
        log(f"      {mism:,} sites sit in a different rep's coverage than "
            f"their own DSE column says. Not corrected — that is the finding.")
    return len(updates)


def import_xlsx(con, path, dry=False):
    try:
        import openpyxl
    except ImportError:
        sys.exit("openpyxl is required for .xlsx — pip install openpyxl")
    base = os.path.basename(path)
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    now = db.utcnow()
    total = 0
    for ws in wb.worksheets:
        header, body = sheet_rows(ws)
        up = [h.upper() for h in header]
        hs = set(up)
        idx = {h: i for i, h in enumerate(up)}

        if ("SITE ID" in hs or "SITE_ID" in hs) and (
                "LAT" in hs or "LATITUDE" in hs or "SITE LAT" in hs):
            # Site master. Site ID / Site Name / Long / Lat / VLR, with an
            # optional DSE column. Where the file carries no DSE the site is
            # placed by geography instead -- see stamp_sites() below.
            sid_k = "SITE ID" if "SITE ID" in idx else "SITE_ID"
            latk = next(k for k in ("LAT", "LATITUDE", "SITE LAT") if k in idx)
            lonk = next((k for k in ("LONG", "LON", "LONGITUDE", "SITE LONG")
                         if k in idx), None)
            vlrk = next((k for k in ("VLR", "SITE VLR", "VLR SUBS",
                                     "SITE_VLR") if k in idx), None)
            dsek = next((k for k in ("DSE CODE", "DSE_CODE", "DSE ID", "DSE")
                         if k in idx), None)
            namek = next((k for k in ("SITE NAME", "SITE_NAME", "NAME")
                          if k in idx), None)
            g = lambda r, k: (str(r[idx[k]]).strip()
                              if k and k in idx and idx[k] < len(r)
                              and r[idx[k]] is not None
                              and str(r[idx[k]]).strip() not in ("", "#N/A")
                              else None)
            rows, swapped, nocoord = [], 0, 0
            for r in body:
                sid = g(r, sid_k)
                if not sid:
                    continue
                a = as_num(r[idx[lonk]]) if lonk and lonk in idx else None
                b = as_num(r[idx[latk]]) if latk in idx else None
                lat, lon = b, a
                # Same transposition guard the store loader uses: trust the
                # numbers over the headers, Indonesia's envelope separates them.
                if (a is not None and b is not None
                        and ID_LAT[0] <= a <= ID_LAT[1]
                        and ID_LON[0] <= b <= ID_LON[1]):
                    lat, lon = a, b
                    swapped += 1
                if lat is None or lon is None:
                    nocoord += 1
                extra = {header[i]: r[i] for i in range(min(len(header), len(r)))
                         if header[i] and r[i] is not None
                         and header[i].upper() not in
                         {sid_k, latk, lonk or "", vlrk or "", dsek or "",
                          namek or ""}}
                rows.append((sid, g(r, namek), lat, lon,
                             as_num(r[idx[vlrk]]) if vlrk and vlrk in idx else None,
                             g(r, dsek), None, None, None, None, None,
                             json.dumps(extra, ensure_ascii=False, default=str),
                             base, now))
            if swapped:
                log(f"      NOTE {swapped:,} rows had Long/Lat transposed — "
                    f"corrected on import, the spreadsheet is unchanged.")
            if nocoord:
                log(f"      WARN {nocoord:,} sites have no usable coordinates "
                    f"— they load, but cannot be placed on the map or "
                    f"assigned to a DSE.")
            log(f"  {base}[{ws.title}]: {len(rows):,} sites")
            if not dry and rows:
                con.executemany(
                    "INSERT OR REPLACE INTO ref_site (site_id,site_name,lat,"
                    "lon,vlr,dse_code,dse_assigned,assign_mode,desa,kecamatan,"
                    "kabkot,attrs_json,source_file,imported_utc) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
                con.commit()
                stamp_sites(con)
            total += len(rows)

        elif {"KEL_DES", "KEC", "KAB_KOT"} <= hs:
            # Desa profile. The sheet spans the whole of West Java and Banten;
            # only the rows carrying an Indosat branch are inside this region,
            # and importing the other ~7,000 would put desa on the map that no
            # one here owns. The filter IS the territory definition.
            g = lambda r, k: (str(r[idx[k]]).strip()
                              if k in idx and idx[k] < len(r)
                              and r[idx[k]] is not None
                              and str(r[idx[k]]).strip() not in ("", "#N/A")
                              else None)
            krows, skipped, dupes = [], 0, {}
            for r in body:
                kel, kec, kab = g(r, "KEL_DES"), g(r, "KEC"), g(r, "KAB_KOT")
                if not kel or not kec:
                    continue
                # MCAgus is the 24-MC generation, the same one the indosat_mc
                # polygons were cut from. MC35 is the finer re-split, carried
                # as a field so the two can be compared but never joined on.
                mc = g(r, "MCAGUS") or g(r, "MC")
                branch_ = g(r, "BRANCHAGUS") or g(r, "BRANCH")
                if not branch_:
                    skipped += 1
                    continue
                kel_key = f"{kel}|{kec}|{kab or ''}".upper()
                if kel_key in dupes:
                    log(f"      WARN duplicate desa key {kel_key!r} — "
                        f"the later row wins")
                dupes[kel_key] = True
                pop = as_num(r[idx["POPULASI_2"]]) if "POPULASI_2" in idx else None
                pidx = as_num(r[idx["INDEX_POPU"]]) if "INDEX_POPU" in idx else None
                luas = as_num(r[idx["LUAS_DESA"]]) if "LUAS_DESA" in idx else None
                lon = as_num(r[idx["LONG_MUK"]]) if "LONG_MUK" in idx else None
                lat = as_num(r[idx["LAT_MUK"]]) if "LAT_MUK" in idx else None
                krows.append((
                    kel_key, None, kel, kec, kab, g(r, "PROV"),
                    norm_key(kel, kec, kab), norm_key(kec, kab),
                    mc, branch_, g(r, "AREA"), g(r, "REGION"), g(r, "CIRCLE"),
                    g(r, "PT"),
                    g(r, "PARTNER AGUST") or g(r, "PARTNER_AF") or g(r, "PARTNER"),
                    g(r, "MC35"), g(r, "BRANCH11"),
                    int(pop) if pop is not None else None,
                    int(pidx) if pidx is not None else None,
                    g(r, "GEO_TYPE"), luas, lat, lon, base, now))
            log(f"  {base}[{ws.title}]: {len(krows):,} desa inside the region "
                f"({skipped:,} rows outside it, no branch assigned)")
            if not dry and krows:
                con.executemany(
                    "INSERT OR REPLACE INTO ref_kelurahan (kel_key,unique_id,"
                    "kelurahan,kecamatan,kabkot,prov,join_key,kec_join_key,"
                    "mc,branch,area,region,circle,pt,partner,mc35,branch11,"
                    "population,pop_index,geo_type,area_m2,lat,lon,"
                    "source_file,imported_utc) VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    krows)
                # Stamp the BPS Unique_ID from whichever polygons are loaded,
                # so the profile and the boundary share one identifier.
                con.execute(
                    "UPDATE ref_kelurahan SET unique_id = (SELECT f.feature_key"
                    " FROM geo_feature f WHERE f.layer_key='kelurahan'"
                    "   AND f.join_key = ref_kelurahan.join_key)"
                    " WHERE unique_id IS NULL")
                # AREA / REGION / CIRCLE / PT are in the KMZ but not in the
                # spreadsheet. Backfill rather than leave the hierarchy
                # half-populated — a NULL region reads as "unknown", which
                # is a different claim from "not in this column".
                for col, key in (("area", "AREA"), ("region", "REGION"),
                                 ("circle", "CIRCLE"), ("pt", "PT")):
                    con.execute(
                        f"UPDATE ref_kelurahan SET {col} = (SELECT"
                        f" json_extract(f.attrs_json, '$.{key}') FROM"
                        f" geo_feature f WHERE f.layer_key='kelurahan'"
                        f"   AND f.join_key = ref_kelurahan.join_key)"
                        f" WHERE {col} IS NULL")
            total += len(krows)

        elif {"CIRCLE", "REGION", "AREA", "BRANCH", "MC"} <= hs:
            rows = []
            for r in body:
                mc = (r[idx["MC"]] or "")
                if not str(mc).strip():
                    continue
                g = lambda k: (str(r[idx[k]]).strip()
                               if idx[k] < len(r) and r[idx[k]] is not None
                               else None)
                rows.append((str(mc).strip(), g("BRANCH"), g("AREA"),
                             g("REGION"), g("CIRCLE"), base, now))
            log(f"  {base}[{ws.title}]: {len(rows):,} MC hierarchy rows")
            if not dry and rows:
                con.executemany(
                    "INSERT OR REPLACE INTO ref_mc (mc,branch,area,region,"
                    "circle,source_file,imported_utc) VALUES (?,?,?,?,?,?,?)",
                    rows)
            total += len(rows)

        elif "KECAMATAN" in hs and "MC" in hs:
            krows, nmetric = [], 0
            for r in body:
                raw = r[idx["KECAMATAN"]]
                if not raw:
                    continue
                # The export ends with a provenance footer in the first
                # column -- "Applied filters: dt_id is 1 March, 2026 ..." --
                # which parsed into a kecamatan named after the filter, with
                # a NULL kabupaten and twenty empty metrics behind it. It
                # joined to no polygon so it never reached the map, but it
                # sat in every count of "how many kecamatan do we have".
                if str(raw).strip().lower().startswith("applied filter") \
                        or "\n" in str(raw):
                    log(f"      skipped a footer row: {str(raw)[:40]!r}")
                    continue
                # "KARAWANG TIMUR|KARAWANG" -> kecamatan | kabupaten
                bits = [b.strip() for b in str(raw).split("|")]
                kec = bits[0]
                kab = bits[1] if len(bits) > 1 else None
                kec_key = f"{kec}|{kab or ''}".upper()
                g = lambda k: (str(r[idx[k]]).strip()
                               if k in idx and idx[k] < len(r)
                               and r[idx[k]] is not None else None)
                krows.append((kec_key, kec, kab, norm_key(kec, kab), g("MC"),
                              g("SA"), g("AREA"), g("REGION"),
                              int(as_num(r[idx["RANK"]]) or 0)
                              if "RANK" in idx else None, base, now))
                pairs = []
                for i, h in enumerate(header):
                    if not h or h.upper() in KEC_IDS or i >= len(r):
                        continue
                    metric, period = split_metric(h)
                    if not metric:
                        continue
                    num = as_num(r[i])
                    pairs.append((metric, period, num,
                                  None if num is not None
                                  else (str(r[i]) if r[i] is not None else None)))
                if pairs and not dry:
                    put_metrics(con, "kecamatan", kec_key, pairs, base, now)
                nmetric += len(pairs)
            log(f"  {base}[{ws.title}]: {len(krows):,} kecamatan, "
                f"{nmetric:,} metric values")
            if not dry and krows:
                con.executemany(
                    "INSERT OR REPLACE INTO ref_kecamatan (kec_key,kecamatan,"
                    "kabkot,join_key,mc,sa,area,region,rank,source_file,"
                    "imported_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?)", krows)
            total += len(krows)

        elif "STORE NAME" in hs and ("LONGITUDE" in hs or "LATITUDE" in hs):
            rows, swapped = [], 0
            for r in body:
                name = r[idx["STORE NAME"]]
                if not name:
                    continue
                a = as_num(r[idx["LONGITUDE"]]) if "LONGITUDE" in idx else None
                b = as_num(r[idx["LATITUDE"]]) if "LATITUDE" in idx else None
                lat, lon = b, a
                # The header says Longitude but the value is a latitude.
                # Trust the numbers, not the label — Indonesia's envelope
                # makes the two unambiguous.
                if (a is not None and b is not None
                        and ID_LAT[0] <= a <= ID_LAT[1]
                        and ID_LON[0] <= b <= ID_LON[1]):
                    lat, lon = a, b
                    swapped += 1
                g = lambda k: (str(r[idx[k]]).strip()
                               if k in idx and idx[k] < len(r)
                               and r[idx[k]] is not None else None)
                rows.append((str(name).strip(), g("STORE CODE"),
                             str(name).strip(), g("TYPE"), g("RSO NAME"),
                             g("ADDRESS"), lat, lon, base, now))
            if swapped:
                log(f"      NOTE {swapped:,}/{len(rows):,} rows had "
                    f"Longitude/Latitude transposed in the source file — "
                    f"corrected on import, the spreadsheet is unchanged.")
            log(f"  {base}[{ws.title}]: {len(rows):,} IOH stores / gerai")
            if not dry and rows:
                con.executemany(
                    "INSERT OR REPLACE INTO ref_store (store_key,store_code,"
                    "store_name,store_type,rso_name,address,lat,lon,"
                    "source_file,imported_utc) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    rows)
            total += len(rows)

        elif ({"OPERATOR"} & hs) and ("LATITUDE" in hs or "LAT" in hs):
            latk = "LATITUDE" if "LATITUDE" in idx else "LAT"
            lonk = "LONGITUDE" if "LONGITUDE" in idx else "LON"
            rows, tally = [], {}
            for i, r in enumerate(body):
                g = lambda k: (str(r[idx[k]]).strip()
                               if k in idx and idx[k] < len(r)
                               and r[idx[k]] is not None else None)
                a = as_num(r[idx[lonk]]) if lonk in idx else None
                b = as_num(r[idx[latk]]) if latk in idx else None
                lat, lon = b, a
                if (a is not None and b is not None
                        and ID_LAT[0] <= a <= ID_LAT[1]
                        and ID_LON[0] <= b <= ID_LON[1]):
                    lat, lon = a, b
                nm = g("NAME") or g("SITE NAME")
                op, klass = classify_point(g("OPERATOR"), None, nm, g("TYPE"))
                tally[op] = tally.get(op, 0) + 1
                if lat is None or lon is None:
                    continue
                rows.append((stable_key(nm, lat, lon), op, klass,
                             g("OPERATOR"), nm, g("TYPE"),
                             g("ADDRESS"), lat, lon, None, base, now))
            log(f"  {base}[{ws.title}]: {len(rows):,} service points  "
                + "  ".join(f"{k}={v:,}" for k, v in sorted(tally.items())))
            if not dry and rows:
                con.executemany(
                    "INSERT OR REPLACE INTO ref_service_point (sp_key,"
                    "operator,sp_class,operator_raw,name,sp_type,address,"
                    "lat,lon,attrs_json,source_file,imported_utc) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
            total += len(rows)

        elif "NIK AGENT" in hs:
            rows = []
            for r in body:
                nik = r[idx["NIK AGENT"]]
                if not nik:
                    continue
                g = lambda k: (str(r[idx[k]]).strip()
                               if k in idx and idx[k] < len(r)
                               and r[idx[k]] is not None else None)
                rows.append((str(nik).strip(), g("NAME"), g("POSITION"),
                             g("STORE CODE"), g("STORE NAME"), g("NIK RSO"),
                             g("RSO NAME"), base, now))
            log(f"  {base}[{ws.title}]: {len(rows):,} agents")
            if not dry and rows:
                con.executemany(
                    "INSERT OR REPLACE INTO ref_agent (nik_agent,agent_name,"
                    "position,store_code,store_name,nik_rso,rso_name,"
                    "source_file,imported_utc) VALUES (?,?,?,?,?,?,?,?,?)",
                    rows)
            total += len(rows)
        else:
            log(f"  {base}[{ws.title}]: no recognised header "
                f"({', '.join(h for h in header[:5] if h) or 'blank'}…) "
                f"— skipped")
    wb.close()
    if not dry:
        con.commit()
    return total


# ── driver ───────────────────────────────────────────────────────────────
def discover(folder):
    if not os.path.isdir(folder):
        return []
    out = []
    for n in sorted(os.listdir(folder)):
        if n.startswith("~$") or n.startswith("."):
            continue          # Excel lock files
        if n.lower().endswith((".kmz", ".kml", ".xlsx")):
            out.append(os.path.join(folder, n))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=DEFAULT_DIR)
    ap.add_argument("--file", action="append")
    ap.add_argument("--layer", help="override the layer key for a KML/KMZ")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--inspect", action="store_true",
                    help="read and report, write nothing")
    ap.add_argument("--no-follow-links", action="store_true",
                    help="do not resolve NetworkLinks to remote KML")
    ap.add_argument("--url", action="append",
                    help="import straight from a KML/KMZ URL "
                         "(a Google My Maps link works)")
    args = ap.parse_args()

    files = args.file or ([] if args.url else discover(args.dir))
    if not files and not args.url:
        sys.exit(f"No .kmz/.kml/.xlsx found in {args.dir}")
    if not (args.all or args.file or args.inspect or args.url):
        ap.error("pick --all, --inspect, --url, or one or more --file")

    if not db.db_exists():
        sys.exit("No database — run `python migrate.py` first.")
    con = db.connect()
    run_id = None if args.inspect else db.start_run(
        con, "local", {"files": [os.path.basename(f) for f in files]})

    log(f"{'inspecting' if args.inspect else 'importing'} "
        f"{len(files)} file(s) from {args.dir}")
    total = 0
    for url in (args.url or []):
        real = normalise_link(url)
        log(f"  fetching {real}")
        try:
            feats, fields, _ = parse_kml_bytes(kml_bytes(fetch_url(real)))
        except Exception as exc:                        # noqa: BLE001
            log(f"    failed: {type(exc).__name__}: {exc}")
            continue
        folders = sorted({f.get("folder") for f in feats if f.get("folder")})
        log(f"    {len(feats):,} placemarks; folders: "
            f"{', '.join(folders[:10]) or '(none)'}")
        total += import_service_points(con, real, feats, dry=args.inspect)
    for path in files:
        try:
            if path.lower().endswith((".kmz", ".kml")):
                total += import_kml(con, path, args.layer, dry=args.inspect,
                                    follow=not args.no_follow_links)
            else:
                total += import_xlsx(con, path, dry=args.inspect)
        except Exception as exc:                       # noqa: BLE001
            log(f"  {os.path.basename(path)}: FAILED — {type(exc).__name__}: {exc}")

    seed_service_points_from_stores(con, dry=args.inspect)

    if args.inspect:
        print("\n  --inspect: nothing written.")
        con.close()
        return 0

    db.finish_run(con, run_id, "ok", rows_written=total, api_calls=0,
                  est_cost_usd=0.0, notes=f"files={len(files)}")
    print()
    for t in ("geo_layer", "geo_feature", "ref_mc", "ref_kecamatan",
              "ref_kelurahan", "ref_site", "ref_store", "ref_agent",
              "ref_service_point", "ref_metric"):
        n = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        print(f"  {t:<16}{n:>8,}")
    unmatched = con.execute("""
        SELECT count(*) FROM ref_kecamatan k
         WHERE NOT EXISTS (SELECT 1 FROM geo_feature g
                            WHERE g.layer_key='kecamatan'
                              AND g.join_key = k.join_key)""").fetchone()[0]
    if unmatched:
        print(f"\n  {unmatched:,} kecamatan in the profile have no matching "
              f"boundary polygon.\n  That is a coverage gap, not a bug — the "
              f"profile covers more area than the KMZ.")
    try:
        kel_unmatched = con.execute("""
            SELECT count(*) FROM ref_kelurahan k
             WHERE NOT EXISTS (SELECT 1 FROM geo_feature g
                                WHERE g.layer_key='kelurahan'
                                  AND g.join_key = k.join_key)""").fetchone()[0]
        poly_unmatched = con.execute("""
            SELECT count(*) FROM geo_feature g
             WHERE g.layer_key='kelurahan'
               AND NOT EXISTS (SELECT 1 FROM ref_kelurahan k
                                WHERE k.join_key = g.join_key)""").fetchone()[0]
    except sqlite3.OperationalError:
        kel_unmatched = poly_unmatched = 0
    if kel_unmatched or poly_unmatched:
        print(f"\n  desa join: {kel_unmatched:,} profile rows without a "
              f"polygon, {poly_unmatched:,} polygons without a profile row.\n"
              f"  Both should be 0 — anything else means the name spelling "
              f"drifted between the KMZ and the spreadsheet.")
    print("\n  Next: python enrich_territory.py   "
          "(tag every POI with its MC and kecamatan)")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
