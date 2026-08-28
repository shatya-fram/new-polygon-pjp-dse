"""
CONNECTION 3 — OpenStreetMap via the Overpass API.

Free. No key, no account, no bill, ever. What it costs instead is:

  * a fair-use budget on volunteer-run servers — roughly 10k query-seconds
    and ~1 GB of download per IP per day on overpass-api.de, with only a
    couple of concurrent slots. Hammer it and you get throttled, not billed.
  * an ODbL 1.0 licence obligation. Attribution is required, and the
    share-alike clause bites if OSM-derived rows are merged into a database
    you then redistribute. That is exactly why poi_osm is its own table:
    the same containment argument that keeps Google's 30-day cache limit
    off your base layer keeps ODbL off it too.

Attribution string to carry on any published map or export: see ATTRIBUTION.

Why bother when Overture already exists: Overture's places theme carries no
`bus_stop` category at all and its Indonesian rail coverage is thin, while
OSM's Jakarta transit mapping is actively maintained by local mappers —
KRL, MRT Jakarta, LRT Jakarta and Jabodebek, TransJakarta halte, and
amenity=bank / amenity=atm are all well populated. OSM is also the only one
of the three sources that fills brand:wikidata on Indonesian minimarkets.
"""
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import config

ATTRIBUTION = "© OpenStreetMap contributors — ODbL 1.0"

# Tried in order; the first that answers wins. kumi.systems is the roomiest
# public mirror, so it is not last by accident.
ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

# Overpass operators ask for a descriptive UA so they can tell a runaway
# script from a browser. Sending a real one is part of the fair-use bargain.
USER_AGENT = ("API-Location-Pulldown/1.0 (POI research, Jabodetabek; "
              "low-volume one-off pulls)")

# What a thing IS, most defining first. Used to give every row a single
# readable `category` instead of leaving the caller to read raw tag soup.
PRIMARY_TAGS = ["railway", "station", "aeroway", "amenity", "shop",
                "highway", "public_transport", "office", "tourism", "leisure"]


class OverpassError(RuntimeError):
    pass


# ── query building ───────────────────────────────────────────────────────
def build_query(selectors, aoi, timeout=180, use_nwr=True, meta=True):
    """Compose Overpass QL for one target.

    The bbox goes in the global setting rather than on every statement, so
    adding a selector cannot accidentally widen the area. Note Overpass
    orders a bbox south,west,north,east — the opposite of the lon-first
    convention used everywhere else in this project, which is a classic way
    to silently query the wrong hemisphere.
    """
    s, w, n, e = aoi["minlat"], aoi["minlon"], aoi["maxlat"], aoi["maxlon"]
    kinds = ["nwr"] if use_nwr else ["node", "way", "relation"]
    body = "".join(f"  {k}{sel};\n" for sel in selectors for k in kinds)
    # `center` gives ways and relations a single representative point, so a
    # station mapped as a building polygon still lands as one row with
    # coordinates rather than being dropped.
    out = "out meta center;" if meta else "out tags center;"
    return (f"[out:json][timeout:{timeout}]"
            f"[bbox:{s},{w},{n},{e}];\n(\n{body});\n{out}\n")


# ── transport ────────────────────────────────────────────────────────────
def fetch(query, endpoints=None, attempts=3, pause=6.0, http_timeout=300,
          log=print):
    """POST the query, failing over between mirrors and backing off politely.

    Returns (elements, endpoint_used, seconds). Raises OverpassError only
    after every endpoint has been tried `attempts` times — a 429 or 504 from
    one mirror is normal and is not worth surfacing to the user.
    """
    endpoints = list(endpoints or ENDPOINTS)
    t0 = time.time()
    last = None
    for attempt in range(1, attempts + 1):
        for url in endpoints:
            try:
                data = urllib.parse.urlencode({"data": query}).encode()
                req = urllib.request.Request(
                    url, data=data,
                    headers={"User-Agent": USER_AGENT,
                             "Accept": "application/json",
                             "Content-Type":
                                 "application/x-www-form-urlencoded"})
                with urllib.request.urlopen(req, timeout=http_timeout) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                if "elements" not in payload:
                    raise OverpassError(
                        f"no `elements` in response from {url}: "
                        f"{str(payload)[:200]}")
                return payload["elements"], url, time.time() - t0
            except urllib.error.HTTPError as exc:
                last = f"{url} -> HTTP {exc.code}"
                if exc.code in (429, 504):
                    log(f"    {url.split('/')[2]}: busy (HTTP {exc.code}) — "
                        f"trying the next mirror")
                else:
                    log(f"    {url.split('/')[2]}: HTTP {exc.code}")
            except Exception as exc:                     # noqa: BLE001
                last = f"{url} -> {type(exc).__name__}: {exc}"
                log(f"    {url.split('/')[2]}: {type(exc).__name__}")
        if attempt < attempts:
            wait = pause * attempt
            log(f"    all mirrors busy — waiting {wait:.0f}s "
                f"(attempt {attempt}/{attempts})")
            time.sleep(wait)
    raise OverpassError(
        f"every Overpass endpoint failed after {attempts} rounds. "
        f"Last error: {last}. This is a throttle or an outage, never a "
        f"billing problem — wait a few minutes and re-run, or pass "
        f"--endpoint with a mirror of your own.")


# ── element -> row ───────────────────────────────────────────────────────
def primary_tag(tags):
    for k in PRIMARY_TAGS:
        if k in tags:
            return f"{k}={tags[k]}"
    return None


def resolve_brand(tags):
    """brand:wikidata first — it is an identity, not a string that happens
    to look right. OSM fills it on Indonesian minimarkets far more often
    than Overture does, which is the one place a QID actually helps here.

    Falls back to brand, then operator, then name, each walked in
    BRAND_RESOLVE_ORDER so BSI never resolves to Mandiri and a BRILink agent
    is never mistaken for a BRI branch.
    """
    qid = tags.get("brand:wikidata")
    if qid:
        for key in config.BRAND_RESOLVE_ORDER:
            if config.BRANDS[key].get("wikidata") == qid:
                return config.BRANDS[key]["label"]
    for field in ("brand", "operator", "name"):
        text = tags.get(field)
        if not text:
            continue
        low = text.lower()
        for key in config.BRAND_RESOLVE_ORDER:
            if re.search(config.BRANDS[key]["pattern"], low):
                return config.BRANDS[key]["label"]
    return None


def compose_address(tags):
    """OSM addr:* fill in Indonesia is poor — often under 20%. That is fine
    here: enrich_admin.py derives kelurahan/kecamatan/kota from the point
    itself, which is more reliable than any address string anyway."""
    if tags.get("addr:full"):
        return tags["addr:full"]
    street = " ".join(x for x in (tags.get("addr:street"),
                                  tags.get("addr:housenumber")) if x)
    parts = [street, tags.get("addr:suburb"), tags.get("addr:village"),
             tags.get("addr:subdistrict"), tags.get("addr:city"),
             tags.get("addr:postcode")]
    return ", ".join(p for p in parts if p) or None


def element_to_row(el, target_key):
    """None when the element has no usable point — a relation without a
    center, typically. Dropping it is correct; a null-geometry row would
    break every map export downstream."""
    lat, lon = el.get("lat"), el.get("lon")
    if lat is None or lon is None:
        centre = el.get("center") or {}
        lat, lon = centre.get("lat"), centre.get("lon")
    if lat is None or lon is None:
        return None
    tags = el.get("tags") or {}
    name = (tags.get("name") or tags.get("name:id")
            or tags.get("official_name") or tags.get("ref"))
    return {
        # type/id is OSM's stable natural key and survives tag edits.
        "source_id": f"{el.get('type')}/{el.get('id')}",
        "source": "osm",
        "osm_type": el.get("type"),
        "osm_id": el.get("id"),
        "name": name,
        "brand_resolved": resolve_brand(tags),
        "brand_name": tags.get("brand"),
        "brand_qid": tags.get("brand:wikidata"),
        "category": primary_tag(tags),
        "target": target_key,
        "operator": tags.get("operator"),
        "network": tags.get("network"),
        "ref": tags.get("ref"),
        "lat": round(float(lat), 7),
        "lon": round(float(lon), 7),
        "address": compose_address(tags),
        "locality": tags.get("addr:city") or tags.get("addr:suburb"),
        "region": tags.get("addr:province") or tags.get("addr:state"),
        "phone": tags.get("phone") or tags.get("contact:phone"),
        "website": tags.get("website") or tags.get("contact:website"),
        "opening_hours": tags.get("opening_hours"),
        "osm_version": el.get("version"),
        "source_updated": el.get("timestamp"),
        # The full tag dict, kept verbatim. OSM is tag soup by design and
        # discarding it means a re-pull every time a new question comes up.
        "tags_json": json.dumps(tags, ensure_ascii=False, sort_keys=True),
    }


def elements_to_rows(elements, target_key):
    rows, dropped = [], 0
    for el in elements:
        row = element_to_row(el, target_key)
        if row is None:
            dropped += 1
        else:
            rows.append(row)
    return rows, dropped
