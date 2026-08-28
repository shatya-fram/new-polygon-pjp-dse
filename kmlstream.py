#!/usr/bin/env python3
"""
Read a KML placemark at a time, whatever the file weighs.

WHY
    `import_local.parse_kml_file` calls ET.fromstring on the whole document.
    That was fine for a 1 MB KMZ of 887 desa. The exports now in use are
    815 MB of uncompressed XML for a national desa layer, and a DOM of that
    needs several gigabytes of RAM -- the process dies, and the failure looks
    like the upload hanging rather than like a file that was never going to
    fit.

    iterparse with an explicit clear() walks the same file in constant
    memory. Nothing else about the format changes: the same <ExtendedData>,
    the same <Polygon>, the same coordinates.

WHAT COUNTS AS BLANK
    These exports write the four-letter string NULL where a value is absent,
    and pad others with spaces (`  JAKARTA RAYA  `). Both are stripped here,
    once, so that every consumer downstream sees an empty value as empty
    rather than as a place called NULL.
"""
import json
import xml.etree.ElementTree as ET

KML_NS = "http://earth.google.com/kml/2.2"
OGC_NS = "http://www.opengis.net/kml/2.2"
BLANKS = {"", "NULL", "null", "#N/A", "-", "N/A"}


def _tag(e):
    """Local name, so both KML namespaces read the same."""
    t = e.tag
    return t.rsplit("}", 1)[-1] if "}" in t else t


def clean(v):
    v = " ".join((v or "").split())
    return "" if v in BLANKS else v


def _coords(text):
    """KML gives lon,lat[,alt] triples separated by whitespace."""
    out = []
    for tok in (text or "").split():
        bits = tok.split(",")
        if len(bits) >= 2:
            try:
                out.append((float(bits[0]), float(bits[1])))
            except ValueError:
                continue
    return out


def _rings(poly):
    outer, inner = [], []
    for el in poly.iter():
        t = _tag(el)
        if t == "outerBoundaryIs":
            for c in el.iter():
                if _tag(c) == "coordinates":
                    r = _coords(c.text)
                    if len(r) >= 4:
                        outer.append(r)
        elif t == "innerBoundaryIs":
            for c in el.iter():
                if _tag(c) == "coordinates":
                    r = _coords(c.text)
                    if len(r) >= 4:
                        inner.append(r)
    return outer, inner


def geometry_of(pm):
    """-> (geojson dict, bbox, centroid) or (None, None, None).

    Multi-part polygons are kept as MultiPolygon rather than reduced to the
    largest part: an island district that loses its islands is a different
    district."""
    parts = []
    points = []
    for el in pm.iter():
        t = _tag(el)
        if t == "Polygon":
            outer, inner = _rings(el)
            for o in outer:
                parts.append([[list(p) for p in o]]
                             + [[list(p) for p in h] for h in inner])
        elif t == "Point":
            for c in el.iter():
                if _tag(c) == "coordinates":
                    points.extend(_coords(c.text))
    if parts:
        geom = ({"type": "Polygon", "coordinates": parts[0]} if len(parts) == 1
                else {"type": "MultiPolygon", "coordinates": parts})
        flat = [p for part in parts for ring in part for p in ring]
    elif points:
        geom = {"type": "Point", "coordinates": list(points[0])}
        flat = points[:1]
    else:
        return None, None, None
    xs = [p[0] for p in flat]
    ys = [p[1] for p in flat]
    bbox = (min(xs), min(ys), max(xs), max(ys))
    centroid = ((min(ys) + max(ys)) / 2.0, (min(xs) + max(xs)) / 2.0)
    return geom, bbox, centroid


def attrs_of(pm):
    """<ExtendedData><Data name=…><value>…  Only that form is read.

    The <description> block in these files repeats the same pairs as HTML.
    Parsing it as a fallback would double every value and quietly disagree
    with ExtendedData wherever the export truncated a name."""
    out = {}
    for el in pm.iter():
        if _tag(el) != "Data":
            continue
        key = clean(el.get("name"))
        if not key:
            continue
        val = ""
        for c in el:
            if _tag(c) == "value":
                val = clean("".join(c.itertext()))
        if val:
            out[key] = val
    return out


def placemarks(path, want_geometry=True, limit=None):
    """Yield (name, attrs, geometry, bbox, centroid), freeing each as we go.

    The element is removed from its PARENT once it has been handed over.
    Calling clear() alone is the well-known half-fix: the element empties but
    the parent still holds it, so a "streaming" parse quietly rebuilds the
    whole document in memory one placemark at a time. On an 815 MB file that
    is the difference between 60 MB of RAM and none left."""
    ctx = ET.iterparse(path, events=("start", "end"))
    stack, n = [], 0
    for event, el in ctx:
        if event == "start":
            stack.append(el)
            continue
        if stack and stack[-1] is el:
            stack.pop()
        if _tag(el) != "Placemark":
            continue
        name = ""
        for c in el:
            if _tag(c) == "name":
                name = clean("".join(c.itertext()))
                break
        attrs = attrs_of(el)
        if want_geometry:
            geom, bbox, cent = geometry_of(el)
        else:
            geom = bbox = cent = None
        yield name, attrs, geom, bbox, cent
        n += 1
        el.clear()
        if stack:
            try:
                stack[-1].remove(el)
            except ValueError:
                pass
        if limit and n >= limit:
            return


def scan_fields(path, limit=200):
    """Field names and a couple of examples, without reading the whole file."""
    seen, ex, n = [], {}, 0
    for _name, attrs, _g, _b, _c in placemarks(path, want_geometry=False,
                                               limit=limit):
        n += 1
        for k, v in attrs.items():
            if k not in ex:
                seen.append(k)
                ex[k] = []
            if len(ex[k]) < 3 and v not in ex[k]:
                ex[k].append(v)
        if n >= limit:
            break
    return [{"name": k, "examples": ex[k]} for k in seen]
