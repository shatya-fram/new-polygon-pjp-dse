"""
Google Places API (New) provider.

Two request shapes, picked automatically:

  searchNearby  — pure category query (no text). Exhaustive-ish within the
                  circle, max 20 results per call.
  searchText    — anything with a brand or name term, because Nearby Search
                  has no text parameter at all. Max 20/page, 60 total.

Field mask is deliberately pinned to Essentials + Pro. Adding rating /
opening hours / phone bumps every call to the Enterprise SKU — see
ENTERPRISE_FIELDS below if you decide you want that.
"""
import re
import time

import requests

import config
from config import BRAND_RESOLVE_ORDER, BRANDS, CATEGORIES

_NEARBY = "https://places.googleapis.com/v1/places:searchNearby"
_TEXT = "https://places.googleapis.com/v1/places:searchText"
_DETAILS = "https://places.googleapis.com/v1/places/"

# List price per 1,000 requests, 0-100k band, USD. Used only to show an
# estimate in the UI — check your own billing account before budgeting.
# Verified against developers.google.com/maps/billing-and-pricing/pricing,
# August 2026. The IDs-Only SKU was carrying 2.83 here, which is wrong and
# was making the two-step route look more expensive than it is: Google now
# lists Text Search Essentials (IDs Only) at an unlimited free cap.
#
# Free monthly caps per SKU tier: Essentials 10,000, Pro 5,000,
# Enterprise 1,000. They are per SKU, not shared, and they reset monthly.
FREE_CALLS_PER_MONTH = {"Essentials": 10_000, "Pro": 5_000, "Enterprise": 1_000}

PRICE_PER_1K = {
    "Text Search Essentials (IDs Only)": 0.00,
    "Text Search Pro": 32.00,
    "Nearby Search Pro": 32.00,
    "Nearby Search Enterprise": 35.00,
    "Place Details Essentials": 5.00,
    "Place Details Pro": 17.00,
}

# Essentials + Pro only.
#
# Worth knowing before trying to economise on this list: displayName,
# primaryType and businessStatus are ALL Pro-tier fields, for Place Details
# as well as for Search. location and types are Essentials. So the moment
# you need the name of a place, or need to know whether it is permanently
# closed, you are paying Pro rates and no field-mask trimming avoids it.
# Trimming still cuts the bytes on the wire -- see LEAN_FIELDS.
BASE_FIELDS = [
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.location",
    "places.primaryType",
    "places.types",
    "places.businessStatus",
    "places.addressComponents",
    "places.pureServiceAreaBusiness",
    "places.googleMapsUri",
]
# Uncomment-by-config if you accept the Enterprise SKU price.
ENTERPRISE_FIELDS = [
    "places.rating",
    "places.userRatingCount",
    "places.internationalPhoneNumber",
]

# The minimum that satisfies "location, lat/lon, name, category, and skip
# the permanently closed". Same Pro SKU as BASE_FIELDS, roughly 40% fewer
# bytes because the address blocks are gone.
LEAN_FIELDS = [
    "places.id",
    "places.displayName",
    "places.location",
    "places.primaryType",
    "places.types",
    "places.businessStatus",
]

# Two-step mode: Text Search returns nothing but IDs (the "IDs Only" SKU),
# then Place Details fills in coordinates + address at the Essentials SKU.
#
# READ THIS BEFORE ENABLING IT. Google bills per REQUEST, not per result.
# One Text Search request returns up to 20 places; one Place Details request
# returns exactly one. So the comparison depends entirely on how many usable
# hits a search page yields (n):
#
#   Text Search Pro      $0.032 / n                 per place
#   IDs Only + Details   ($0.00283 / n) + $0.005    per place
#
# Break-even is n = 6 (verified in test_google_places.py):
#
#   n = 20 (full page)   $0.0016 vs $0.0051  -> Text Search Pro, 3.2x cheaper
#   n = 6                roughly equal
#   n = 2 (sparse)       $0.0160 vs $0.0064  -> two-step, 2.5x cheaper
#
# So: bulk enumeration of a dense brand (Alfamart in Jakarta) -> leave this
# OFF. Sparse lookups, or picking one hit out of a page -> turn it ON.
# It also loses `displayName` (a Pro field on Place Details), so brand
# matches come back unverified — see brand_verified on the row.
IDS_ONLY_FIELDS = ["places.id"]
DETAILS_ESSENTIALS_FIELDS = [
    "id", "location", "formattedAddress", "addressComponents", "types",
]


class GoogleError(RuntimeError):
    pass


def available():
    return bool(config.GOOGLE_API_KEY)


def search(lat, lon, radius_m, category_keys, brand_keys=None,
           name_query="", max_results=60, include_enterprise=False,
           cheap_mode=False):
    """Return (rows, meta). Never raises for 'no results' — only for config
    or transport failures."""
    if not available():
        raise GoogleError("GOOGLE_API_KEY is not set.")

    brand_keys = brand_keys or []
    included_types = _google_types(category_keys)
    text_terms = _text_terms(brand_keys, name_query)

    fields = list(BASE_FIELDS)
    if include_enterprise:
        fields += ENTERPRISE_FIELDS

    billed = {}          # SKU name -> call count
    brand_verified = True

    if text_terms and cheap_mode and not include_enterprise:
        # ── two-step: IDs Only -> Details Essentials ──────────────────
        ids, text_calls = _search_text_ids(
            lat, lon, radius_m, included_types, text_terms, max_results)
        raw, detail_calls = _details_batch(ids)
        billed["Text Search Essentials (IDs Only)"] = text_calls
        billed["Place Details Essentials"] = detail_calls
        mode = "searchText(IDs) + details"
        # No displayName came back, so the brand can't be confirmed from the
        # response — it's inherited from the query term.
        brand_verified = False
    elif text_terms:
        raw, text_calls = _search_text(
            lat, lon, radius_m, included_types, text_terms, fields, max_results)
        billed["Text Search Pro"] = text_calls
        mode = "searchText"
    else:
        raw, near_calls = _search_nearby(
            lat, lon, radius_m, included_types, fields, max_results)
        billed["Nearby Search Enterprise" if include_enterprise
               else "Nearby Search Pro"] = near_calls
        mode = "searchNearby"

    rows = [_normalise(p, lat, lon) for p in raw]

    if not brand_verified:
        # Attribute the searched brand, but mark it unverified so nobody
        # mistakes it for a confirmed name match downstream.
        inherited = ", ".join(BRANDS[b]["label"] for b in brand_keys) or None
        for r in rows:
            r["brand_resolved"] = inherited
            r["brand_verified"] = False
    else:
        for r in rows:
            r["brand_verified"] = True
        # Google has no brand field, so brand filtering is a post-filter on
        # the display name. Do it here, not in the caller, so both providers
        # return an already-brand-resolved row.
        if brand_keys:
            rows = [r for r in rows if r["brand_resolved"] in
                    {BRANDS[b]["label"] for b in brand_keys}]
        if name_query:
            nq = name_query.lower()
            rows = [r for r in rows if nq in (r["name"] or "").lower()]

    rows.sort(key=lambda r: (r["distance_m"] is None, r["distance_m"]))

    total_calls = sum(billed.values())
    est_cost = sum(PRICE_PER_1K.get(sku, 0) * n / 1000.0
                   for sku, n in billed.items())
    # Cost per place is the number that actually compares across modes,
    # because Text Search returns up to 20 places per billed request while
    # Place Details returns one.
    per_place = round(est_cost / len(rows), 5) if rows else None
    meta = {
        "provider": "google",
        "mode": mode,
        "api_calls": total_calls,
        "included_types": included_types,
        "text_terms": text_terms,
        "truncated": len(raw) >= max_results,
        "sku": " + ".join(billed.keys()),
        "billed_calls": billed,
        "est_cost_usd": round(est_cost, 4),
        "est_cost_per_place_usd": per_place,
        "brand_verified": brand_verified,
    }
    if not brand_verified:
        meta["warning"] = (
            "Two-step mode: no displayName returned, so brand matches are "
            "inherited from the query, not verified. Note this mode is only "
            "cheaper when you need details for a few of the hits — for bulk "
            "enumeration Text Search Pro costs ~3x less per place.")
    return rows, meta


# ------------------------------------------------------------------ helpers
def _google_types(category_keys):
    types = []
    for key in category_keys:
        for t in CATEGORIES.get(key, {}).get("google", []):
            if t not in types:
                types.append(t)
    return types[:50]  # API cap


def _text_terms(brand_keys, name_query):
    terms = [BRANDS[b]["label"] for b in brand_keys if b in BRANDS]
    if name_query.strip():
        terms.append(name_query.strip())
    return terms


# Setup problems all arrive as a 403 with prose in the body. They are the
# most common first-run failure and every one of them is fixable in the
# console in under a minute, so they get translated instead of raised raw.
SETUP_HINTS = [
    (r"has not been used in project|SERVICE_DISABLED|API is disabled|it is disabled",
     "Places API (New) is not enabled on this Google Cloud project.\n"
     "    Enable it, wait ~2 minutes for it to propagate, then re-run:\n"
     "      https://console.cloud.google.com/apis/library/places.googleapis.com\n"
     "    Make sure it is 'Places API (New)', not the legacy 'Places API' —\n"
     "    this app uses New-style field masks and SKUs."),
    (r"API key not valid|API_KEY_INVALID|Invalid API key",
     "The API key was rejected.\n"
     "    Check GOOGLE_API_KEY in .env for stray quotes, spaces or a line\n"
     "    break, and that the key belongs to the same project."),
    (r"billing|BILLING_DISABLED",
     "Billing is not enabled on the project.\n"
     "    Maps Platform serves nothing without a billing account attached,\n"
     "    even for usage that lands inside the free monthly tier."),
    (r"REQUEST_DENIED|PERMISSION_DENIED|not authorized|restricted",
     "The key is restricted away from this API.\n"
     "    Credentials -> your key -> API restrictions -> allow\n"
     "    'Places API (New)'."),
    (r"RESOURCE_EXHAUSTED|Quota exceeded|quota",
     "Quota exhausted.\n"
     "    Cloud Console -> APIs & Services -> Places API (New) -> Quotas."),
]


def explain_error(message):
    """A human next action for a Google error, or None if we have nothing
    useful to add beyond what Google already said."""
    for pattern, hint in SETUP_HINTS:
        if re.search(pattern, message or "", re.I):
            return hint
    return None


def _headers(fields):
    return {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": config.GOOGLE_API_KEY,
        "X-Goog-FieldMask": ",".join(fields),
    }


# Codes worth retrying. Everything else is a real answer, right or wrong.
TRANSIENT_STATUS = {429, 500, 502, 503, 504}
HTTP_TIMEOUT = 60          # was 30; a busy cell can genuinely take longer


def _post(url, body, fields, attempts=4, backoff=2.0, log=print):
    """One request, with retries for the things that are not Google's answer.

    A single 30-second read timeout used to kill an 800-request sweep two
    thirds of the way through. Transport failures are not results, so they
    are retried with exponential backoff rather than propagated.

    Be aware of the honest caveat: a timeout on this side does not prove
    Google never served the request, so a retry can be billed twice. That
    is still far cheaper than re-running the whole target.
    """
    last = None
    for n in range(1, attempts + 1):
        try:
            r = requests.post(url, json=body, headers=_headers(fields),
                              timeout=HTTP_TIMEOUT)
        except (requests.exceptions.Timeout,
                requests.exceptions.ConnectionError) as exc:
            last = f"{type(exc).__name__}: {exc}"
            if n < attempts:
                wait = backoff * (2 ** (n - 1))
                log(f"    network hiccup ({type(exc).__name__}) — retry "
                    f"{n}/{attempts - 1} in {wait:.0f}s")
                time.sleep(wait)
                continue
            err = GoogleError(
                f"Google Places unreachable after {attempts} attempts: {last}")
            err.status = None
            err.hint = ("A transport failure, not a Google rejection.\n"
                        "    Check the connection and re-run the same "
                        "--target: the upsert keys on place id, so already\n"
                        "    fetched places cost nothing to see again.")
            raise err from exc

        if r.status_code in TRANSIENT_STATUS and n < attempts:
            wait = backoff * (2 ** (n - 1))
            log(f"    HTTP {r.status_code} from Google — retry "
                f"{n}/{attempts - 1} in {wait:.0f}s")
            time.sleep(wait)
            continue

        if r.status_code >= 400:
            try:
                msg = r.json().get("error", {}).get("message", r.text)
            except Exception:
                msg = r.text
            err = GoogleError(f"Google Places {r.status_code}: {msg}")
            err.status = r.status_code
            err.hint = explain_error(msg)
            raise err
        return r.json()


def _search_nearby(lat, lon, radius_m, included_types, fields, max_results):
    body = {
        "locationRestriction": {
            "circle": {
                "center": {"latitude": lat, "longitude": lon},
                "radius": float(min(radius_m, 50000)),
            }
        },
        "maxResultCount": 20,
        "languageCode": "id",
        "regionCode": "ID",
    }
    if included_types:
        body["includedPrimaryTypes"] = included_types
    payload = _post(_NEARBY, body, fields)
    # Nearby Search has no paging — 20 is the hard ceiling per call.
    return payload.get("places", [])[:max_results], 1


def _search_text(lat, lon, radius_m, included_types, text_terms, fields,
                 max_results):
    """One query per text term (brands are OR-ed by running them separately),
    paged up to 60 each, de-duplicated on place id."""
    seen, out, calls = set(), [], 0
    page_fields = fields + ["nextPageToken"]

    for term in text_terms:
        token = None
        for _ in range(3):  # 3 pages x 20 = 60, the API maximum
            body = {
                "textQuery": term,
                "locationBias": {
                    "circle": {
                        "center": {"latitude": lat, "longitude": lon},
                        "radius": float(min(radius_m, 50000)),
                    }
                },
                "pageSize": 20,
                "languageCode": "id",
                "regionCode": "ID",
            }
            # Text Search takes a single includedType, not a list.
            if len(included_types) == 1:
                body["includedType"] = included_types[0]
            if token:
                body["pageToken"] = token
            payload = _post(_TEXT, body, page_fields)
            calls += 1
            for p in payload.get("places", []):
                if p.get("id") and p["id"] not in seen:
                    seen.add(p["id"])
                    out.append(p)
            token = payload.get("nextPageToken")
            if not token or len(out) >= max_results:
                break
        if len(out) >= max_results:
            break

    # When several types were requested, Text Search couldn't filter by all
    # of them — enforce it here.
    if len(included_types) > 1:
        wanted = set(included_types)
        out = [p for p in out if wanted & set(p.get("types") or [])]

    return out[:max_results], calls


def _search_text_ids(lat, lon, radius_m, included_types, text_terms,
                     max_results):
    """Same paging as _search_text, but asking only for place IDs so the call
    bills at the 'Text Search Essentials (IDs Only)' SKU."""
    seen, calls = [], 0
    page_fields = IDS_ONLY_FIELDS + ["nextPageToken"]
    for term in text_terms:
        token = None
        for _ in range(3):
            body = {
                "textQuery": term,
                "locationBias": {
                    "circle": {
                        "center": {"latitude": lat, "longitude": lon},
                        "radius": float(min(radius_m, 50000)),
                    }
                },
                "pageSize": 20,
                "languageCode": "id",
                "regionCode": "ID",
            }
            if len(included_types) == 1:
                body["includedType"] = included_types[0]
            if token:
                body["pageToken"] = token
            payload = _post(_TEXT, body, page_fields)
            calls += 1
            for p in payload.get("places", []):
                if p.get("id") and p["id"] not in seen:
                    seen.append(p["id"])
            token = payload.get("nextPageToken")
            if not token or len(seen) >= max_results:
                break
        if len(seen) >= max_results:
            break
    return seen[:max_results], calls


def _details_batch(place_ids):
    """One Place Details call per ID at the Essentials SKU. This is the half
    of cheap mode that actually costs something — n IDs means n calls."""
    out, calls = [], 0
    for pid in place_ids:
        r = requests.get(
            _DETAILS + pid,
            headers={
                "X-Goog-Api-Key": config.GOOGLE_API_KEY,
                "X-Goog-FieldMask": ",".join(DETAILS_ESSENTIALS_FIELDS),
            },
            timeout=30,
        )
        calls += 1
        if r.status_code >= 400:
            continue  # a single dead ID shouldn't sink the whole query
        out.append(r.json())
    return out, calls


def _admin(components, wanted):
    for c in components or []:
        if wanted in (c.get("types") or []):
            return c.get("longText")
    return None


def resolve_brand(name):
    """Shared brand resolver — order matters (BSI before Mandiri, BRILink
    before BRI)."""
    low = (name or "").lower()
    for key in BRAND_RESOLVE_ORDER:
        if re.search(BRANDS[key]["pattern"], low):
            return BRANDS[key]["label"]
    return None


def _normalise(p, origin_lat, origin_lon):
    loc = p.get("location") or {}
    lat, lon = loc.get("latitude"), loc.get("longitude")
    comps = p.get("addressComponents")
    name = (p.get("displayName") or {}).get("text")
    return {
        "source": "google",
        "id": p.get("id"),
        "name": name,
        "brand_resolved": resolve_brand(name),
        "category": p.get("primaryType"),
        "types": ", ".join(p.get("types") or []),
        "status": p.get("businessStatus"),
        "confidence": None,          # Google has no confidence score
        "lat": lat,
        "lon": lon,
        "distance_m": _haversine(origin_lat, origin_lon, lat, lon),
        "address": p.get("formattedAddress"),
        "kelurahan": _admin(comps, "administrative_area_level_4"),
        "kecamatan": _admin(comps, "administrative_area_level_3"),
        "kota": _admin(comps, "administrative_area_level_2"),
        "provinsi": _admin(comps, "administrative_area_level_1"),
        "rating": p.get("rating"),
        "user_ratings": p.get("userRatingCount"),
        "phone": p.get("internationalPhoneNumber"),
        "url": p.get("googleMapsUri"),
    }


def _haversine(lat1, lon1, lat2, lon2):
    from math import asin, cos, radians, sin, sqrt

    if None in (lat1, lon1, lat2, lon2):
        return None
    r = 6371000.0
    dlat, dlon = radians(lat2 - lat1), radians(lon2 - lon1)
    a = (sin(dlat / 2) ** 2
         + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2)
    return round(2 * r * asin(sqrt(a)))

def search_rect(bbox, searches, lean=True, max_pages=3, skip_closed=True,
                max_calls=None):
    """Text Search confined to a rectangle -- one cell of the territory grid.

    `bbox` is (minlon, minlat, maxlon, maxlat), matching geo_feature.
    `searches` is a list of (textQuery, includedType|None) pairs, run in
    order; results are de-duplicated on place id across the whole cell.

    Rectangle rather than circle on purpose: a circle inscribed in a
    kecamatan misses the corners and a circle that covers them spills into
    the neighbours, so you either lose places or pay to fetch the same ones
    from two cells. `locationRestriction` is a hard boundary, not a bias.

    Returns (rows, meta). Every call is billed at Text Search Pro whether it
    returns 20 places or none, so meta reports calls as well as rows.
    """
    minlon, minlat, maxlon, maxlat = bbox
    rect = {"low": {"latitude": minlat, "longitude": minlon},
            "high": {"latitude": maxlat, "longitude": maxlon}}
    clat, clon = (minlat + maxlat) / 2.0, (minlon + maxlon) / 2.0
    fields = list(LEAN_FIELDS if lean else BASE_FIELDS)
    page_fields = fields + ["nextPageToken"]

    seen, out, calls, truncated, dropped = set(), [], 0, False, 0
    for text, itype in searches:
        token = None
        for page in range(max_pages):
            if max_calls is not None and calls >= max_calls:
                truncated = True
                break
            body = {
                "textQuery": text,
                "locationRestriction": {"rectangle": rect},
                "pageSize": 20,
                "languageCode": "id",
                "regionCode": "ID",
            }
            if itype:
                body["includedType"] = itype          # exactly one, not a list
            if token:
                body["pageToken"] = token
            payload = _post(_TEXT, body, page_fields)
            calls += 1
            places = payload.get("places", []) or []
            for p in places:
                pid = p.get("id")
                if not pid or pid in seen:
                    continue
                if skip_closed and p.get("businessStatus") == "CLOSED_PERMANENTLY":
                    dropped += 1
                    continue
                seen.add(pid)
                out.append(_normalise(p, clat, clon))
            token = payload.get("nextPageToken")
            if not token:
                break
        else:
            # Ran out of pages with a token still pending: Google caps a
            # query at 60 results, so there are more places in this cell
            # than this search can reach. Say so -- silence here looks
            # exactly like complete coverage.
            if token:
                truncated = True
        if max_calls is not None and calls >= max_calls:
            break

    sku = "Text Search Pro"
    return out, {
        "api_calls": calls,
        "sku": sku,
        "est_cost_usd": round(PRICE_PER_1K[sku] * calls / 1000.0, 4),
        "closed_dropped": dropped,
        "truncated": truncated,
    }

