"""
Configuration for API Location Pulldown.

This project is deliberately self-contained: its own venv, its own SQLite
file, its own .env. It reads nothing from — and writes nothing to — the
IM3 / 3ID store-location systems.
"""
import os
import sys


# ── the ground under the polygons ────────────────────────────────────────
# CARTO still serves its public basemap without a key, but anonymously it is
# rate limited per referrer, and over that limit it answers with a valid PNG
# reading "API KEY REQUIRED" rather than with an error. Nothing in the
# browser can tell that from a map, so the default here is a provider that
# needs no key at all; set BASEMAP_PROVIDER=carto with CARTO_API_KEY to have
# the other one back. The reader can switch either way from the map itself.
BASEMAP_PROVIDER = (os.environ.get("BASEMAP_PROVIDER") or "gray").strip()
CARTO_API_KEY = (os.environ.get("CARTO_API_KEY") or "").strip()


def basemap_cfg():
    """What the page needs to build its tile layer."""
    return {"provider": BASEMAP_PROVIDER, "carto_key": CARTO_API_KEY}


from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

# ── paths ────────────────────────────────────────────────────────────────
DATA_DIR    = os.path.join(BASE_DIR, "data")
EXPORT_DIR  = os.path.join(BASE_DIR, "exports")
DB_PATH     = os.getenv("DB_PATH", os.path.join(DATA_DIR, "poi_pulldown.db"))
OVERTURE_CACHE = os.path.join(DATA_DIR, "overture_aoi.parquet")
# A HARDENED INSTANCE RUNS ON A READ-ONLY FILESYSTEM BY DESIGN
# systemd's ProtectSystem=strict makes everything read-only except the one
# ReadWritePaths, and on the shared server that is data/ alone. exports/ is
# written by the CSV and map-export routes, every one of which PUBLIC_MODE
# already refuses -- so the public instance will never use the directory it
# was dying at import to create.
#
# Failing to start over a directory this instance cannot use, and does not
# need, is the tail wagging the dog. A read-only filesystem is noted and
# passed over; a genuinely missing directory that we COULD have made is
# still an error worth seeing.
for _d in (DATA_DIR, EXPORT_DIR):
    try:
        os.makedirs(_d, exist_ok=True)
    except OSError as _exc:                                       # EROFS, EACCES
        if not os.path.isdir(_d):
            sys.stderr.write("config: %s is not writable (%s) — features "
                             "that write there are unavailable\n"
                             % (_d, _exc.strerror))

# ── connection 1: Overture (no key, public parquet on S3) ────────────────
OVERTURE_RELEASE = os.getenv("OVERTURE_RELEASE", "2026-07-22.0").strip()
OVERTURE_S3_BASE = os.getenv("OVERTURE_S3_BASE",
                             "s3://overturemaps-us-west-2/release").strip()
OVERTURE_MIN_CONFIDENCE = float(os.getenv("OVERTURE_MIN_CONFIDENCE", "0.4"))
DUCKDB_MEMORY_LIMIT = os.getenv("DUCKDB_MEMORY_LIMIT", "4GB")
DUCKDB_THREADS = int(os.getenv("DUCKDB_THREADS", "4"))

# ── connection 2: Google Places API (New) — optional ─────────────────────
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "").strip()

# ── query guardrails ─────────────────────────────────────────────────────
DEFAULT_RADIUS_M = int(os.getenv("DEFAULT_RADIUS_M", "1500"))
MAX_RADIUS_M     = int(os.getenv("MAX_RADIUS_M", "25000"))
MAX_RESULTS      = int(os.getenv("MAX_RESULTS", "2000"))

# ── AOI: DKI Jakarta + Depok + Bekasi (kota & kab) + Karawang ────────────
# Kepulauan Seribu excluded; pass --with-seribu to pull_overture.py for it.
AOI = {"minlon": 106.60, "minlat": -6.65, "maxlon": 107.70, "maxlat": -5.85}
AOI_SERIBU_MAXLAT = -5.15
AOI_NAME = "Jabodetabek-Karawang"

# ── HOME: the three provinces the territory model covers ─────────────────
# The Indosat territory and kabupaten layers are imported nationally, so a
# microcluster in Sulawesi or a kabupaten in Papua is in the database on
# purpose -- it is how an outlet or a site sitting outside the patch becomes
# visible instead of being silently dropped at import. But the map is a
# Jakarta Raya + West Java map: fitting it to everything loaded would zoom
# out to the whole archipelago and make the working area a smudge. So every
# automatic fit is clamped to this box, and reaching the national polygons
# is a deliberate pan or a filter, never the default view.
HOME_BOUNDS = {"minlon": float(os.getenv("HOME_MINLON", "105.00")),
               "minlat": float(os.getenv("HOME_MINLAT", "-7.90")),
               "maxlon": float(os.getenv("HOME_MAXLON", "108.95")),
               "maxlat": float(os.getenv("HOME_MAXLAT", "-5.10"))}
HOME_NAME = os.getenv("HOME_NAME", "DKI Jakarta · Banten · Jawa Barat")

# ── sample overlays ──────────────────────────────────────────────────────
# A sample is somebody's own demarcation workbook, previewed over the
# application's layers. The page reads it in the browser and draws it there;
# nothing is sent anywhere, which is what makes it safe to offer on a public
# host and what makes it instant -- a 2.5 MB workbook parses in about a
# quarter of a second with no upload at all.
#
# KEEPING one on the server is a separate, deliberate act, and it is off
# unless this instance is told otherwise. On a shared host "keep" means
# "publish to everyone who opens the page", which is not what someone
# previewing their own working file is asking for.
SAMPLE_STORE = os.getenv("SAMPLE_STORE", "local").strip().lower()
# The PIN that authorises deleting a kept sample. It is a guard against a
# careless click, NOT access control: anyone who can read this file or reach
# the endpoint can delete. Set SAMPLE_PIN before hosting this anywhere real.
SAMPLE_PIN = os.getenv("SAMPLE_PIN", "080809")
SAMPLE_MAX_ROWS = int(os.getenv("SAMPLE_MAX_ROWS", "60000"))
# Building a model is heavier than keeping a list, but a real demarcation
# export is 73,699 rows, so the ceiling has to clear that with room rather
# than refusing the file the feature exists for.
SAMPLE_MODEL_MAX = int(os.getenv("SAMPLE_MODEL_MAX", "150000"))


# ── public mode ──────────────────────────────────────────────────────────
# Set PUBLIC_MODE=1 on a shared server. It is a posture, not a feature: the
# application stops accepting anything that changes what is stored, and says
# so on the pages that offer it. Reading, drawing and previewing your own
# workbook in the browser all still work, because none of them write.
#
# The guard that enforces this lives in app.py and is fail-closed: it refuses
# every method that is not a read unless the path is on a short allowlist, so
# an endpoint added later is refused by default rather than exposed by
# default. Marking each of the thirteen write endpoints by hand would have
# been one forgotten decorator away from the opposite.
PUBLIC_MODE = os.getenv("PUBLIC_MODE", "").strip().lower() in ("1", "true", "yes", "on")


# ── workspace-only mode ──────────────────────────────────────────────────
# PUBLIC_MODE stops the application CHANGING anything. It says nothing about
# what it will SHOW, and on a shared server that is the wider hole of the
# two: Configuration, Distribution, Preview Polygon, Polygon Samples, Layer
# Model, Territory and Data Files would all still render, along with some
# sixty API routes, every one of them reading a database the visitor was
# never meant to browse.
#
# WORKSPACE_ONLY answers the other half. One page is published -- Map
# Workspace -- and it needs exactly ten paths to work. Everything else
# returns 404 rather than 403, because a refusal still confirms that the
# thing exists; a 404 says only that this server does not serve it.
#
# The allowlist is enumerated, not prefixed. `/api/territory/` as a prefix
# would also admit poi.geojson and service-points.geojson, which is how an
# allowlist quietly becomes a denylist.
WORKSPACE_ONLY = os.getenv("WORKSPACE_ONLY", "").strip().lower() \
    in ("1", "true", "yes", "on")

WORKSPACE_PAGE = "/map-workspace"
WORKSPACE_READS = frozenset({
    WORKSPACE_PAGE,
    "/healthz",
    # the four boundary layers, by name
    "/api/territory/kelurahan.geojson",
    "/api/territory/kecamatan.geojson",
    "/api/territory/indosat_mc.geojson",
    "/api/territory/kabkot.geojson",
    # the territory tree in the rail
    "/api/territory/hierarchy",
    # desa area, population and site counts, and the two profile lookups
    "/api/samples/area-stats",
    "/api/samples/desa-profile",
    "/api/samples/area-profile",
    # borders computed from points the browser sends; nothing is kept
    "/api/samples/model.geojson",
})


def sample_store_allowed(remote_addr):
    """Can this caller keep a sample on the server?

    "local" -- the default -- means only somebody at the machine the
    application is running on. "on" allows it from anywhere, which is a
    choice to be made deliberately when hosting; "off" refuses it entirely
    and leaves preview-only.

    THE REVERSE-PROXY TRAP, WHICH THIS USED TO WALK INTO
    Behind nginx on the same host, EVERY visitor arrives from 127.0.0.1 --
    the proxy's own address, not theirs. A "localhost only" test written
    against remote_addr therefore says yes to the entire internet the moment
    the application is put behind a proxy, which is exactly when it most
    needs to say no. So public mode decides first and the address test never
    runs there."""
    if PUBLIC_MODE or SAMPLE_STORE == "off":
        return False
    if SAMPLE_STORE == "on":
        return True
    return (remote_addr or "") in ("127.0.0.1", "::1", "localhost")


def on_map(lat, lon):
    """Is this coordinate inside the area this application covers?

    Applied to every feature at import and again when a layer is cached, so
    nothing outside Inner Jakarta, Outer Jakarta and West Java can be drawn
    -- neither a national polygon nor a row whose coordinates contradict its
    own province. Both exist in the source exports: 40 outlets carry a
    positive latitude (Jakarta mirrored into the South China Sea), three
    masts sit at 0,0, and twenty kecamatan tagged BANTEN or JAWA BARAT
    carry a shape in Central Java. None of them are territory; all of them
    used to draw."""
    if lat is None or lon is None:
        return False
    h = HOME_BOUNDS
    return (h["minlat"] <= lat <= h["maxlat"]
            and h["minlon"] <= lon <= h["maxlon"])

DEFAULT_LAT   = float(os.getenv("DEFAULT_LAT", "-6.1872609"))
DEFAULT_LON   = float(os.getenv("DEFAULT_LON", "106.8153029"))
DEFAULT_PLACE = os.getenv("DEFAULT_PLACE", "Pasar Tanah Abang, Jakarta Pusat")

NOMINATIM_UA = os.getenv(
    "NOMINATIM_UA", "api-location-pulldown/1.0 (contact: you@example.com)")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-only-change-me")


def secret_key_ok():
    """A placeholder secret on a public server is a signed-cookie forgery
    waiting to happen. app.py refuses to start rather than run with one."""
    return not (PUBLIC_MODE and (SECRET_KEY in ("", "dev-only-change-me")
                                 or len(SECRET_KEY) < 24))

# ── target registry: one entry, two dialects ─────────────────────────────
CATEGORIES = {
    "bank":              {"label": "Bank (branch)", "group": "Finance",
                          "google": ["bank"],
                          "overture": ["banks", "bank_credit_union"]},
    "atm":               {"label": "ATM", "group": "Finance",
                          "google": ["atm"], "overture": ["atms"]},
    "bus_station":       {"label": "Bus terminal", "group": "Transit",
                          "google": ["bus_station", "transit_depot"],
                          "overture": ["bus_station", "bus_service"]},
    "bus_stop":          {"label": "Bus stop / halte", "group": "Transit",
                          "google": ["bus_stop"], "overture": [],
                          "note": "No Overture category. Google or OSM only."},
    "train_station":     {"label": "Train station (KA/KRL)", "group": "Transit",
                          "google": ["train_station"],
                          "overture": ["train_station", "trains"]},
    "metro_lrt":         {"label": "MRT / LRT / subway", "group": "Transit",
                          "google": ["subway_station", "light_rail_station"],
                          "overture": ["metro_station",
                                       "light_rail_and_subway_stations"]},
    "convenience_store": {"label": "Minimarket", "group": "Retail",
                          "google": ["convenience_store"],
                          "overture": ["convenience_store"]},
    "supermarket":       {"label": "Supermarket", "group": "Retail",
                          "google": ["supermarket", "grocery_store"],
                          "overture": ["supermarket", "grocery_store"]},
    "market":            {"label": "Pasar / market", "group": "Retail",
                          "google": ["market"],
                          "overture": ["public_market", "bazaars",
                                       "market_stall", "farmers_market"]},
    "shopping_mall":     {"label": "Shopping mall", "group": "Retail",
                          "google": ["shopping_mall"],
                          "overture": ["shopping_center"]},
    "gas_station":       {"label": "SPBU", "group": "Other",
                          "google": ["gas_station"],
                          "overture": ["gas_station"]},
    "university":        {"label": "Campus", "group": "Other",
                          "google": ["university"],
                          "overture": ["college_university"]},
    "hospital":          {"label": "Hospital", "group": "Other",
                          "google": ["hospital"], "overture": ["hospital"]},
}

BRANDS = {
    "alfamart":  {"label": "Alfamart",  "group": "Minimarket",
                  "wikidata": "Q23745600", "pattern": r"alfa\s?mart"},
    "indomaret": {"label": "Indomaret", "group": "Minimarket",
                  "wikidata": "Q4262825", "pattern": r"indo\s?maret"},
    "alfamidi":  {"label": "Alfamidi",  "group": "Minimarket",
                  "wikidata": None, "pattern": r"alfa\s?midi"},
    "lawson":    {"label": "Lawson",    "group": "Minimarket",
                  "wikidata": None, "pattern": r"(^|\W)lawson(\W|$)"},
    "bca":       {"label": "BCA", "group": "Bank", "wikidata": "Q806626",
                  "pattern": r"(^|\W)(bca|bank central asia)(\W|$)"},
    "mandiri":   {"label": "Bank Mandiri", "group": "Bank",
                  "wikidata": "Q806639", "pattern": r"(^|\W)mandiri(\W|$)"},
    "bni":       {"label": "BNI", "group": "Bank", "wikidata": "Q2882611",
                  "pattern": r"(^|\W)(bni|bank negara indonesia)(\W|$)"},
    "bri":       {"label": "BRI", "group": "Bank", "wikidata": "Q623042",
                  "pattern": r"(^|\W)(bri|bank rakyat indonesia)(\W|$)"},
    "bsi":       {"label": "BSI (Bank Syariah Indonesia)", "group": "Bank",
                  "wikidata": None,
                  "pattern": r"(bank syariah indonesia|(^|\W)bsi(\W|$))"},
    "brilink":   {"label": "Agen BRILink", "group": "Bank agent",
                  "wikidata": None, "pattern": r"bri\s?link"},
}

# Broader patterns would swallow narrower ones — BSI before Mandiri/BNI/BRI,
# BRILink before BRI. Covered by tests/test_pulls.py.
BRAND_RESOLVE_ORDER = ["bsi", "brilink", "alfamidi", "alfamart", "indomaret",
                       "lawson", "bca", "mandiri", "bni", "bri"]


def category_group_map():
    out = {}
    for key, cfg in CATEGORIES.items():
        out.setdefault(cfg["group"], []).append(
            {"key": key, "label": cfg["label"], "note": cfg.get("note")})
    return out


def brand_group_map():
    out = {}
    for key, cfg in BRANDS.items():
        out.setdefault(cfg["group"], []).append(
            {"key": key, "label": cfg["label"]})
    return out

# ── operators & map styling ──────────────────────────────────────────────
# One source of truth for colour: the importer classifies with it, the map
# legend renders from it, and the two therefore cannot drift apart.
# `icon` names a file under static/img/operators/. The app uses the real
# logo when that file is present and falls back to the coloured dot when it
# is not, so the map never breaks over a missing asset — and the colour
# stays the source of truth for the ring around each logo, which is what
# keeps a 20px mark readable against a dark basemap.
OPERATORS = {
    "IM3":   {"label": "IM3 (Indosat)",   "color": "#FFD400",    # yellow
              "icon": "im3.png"},
    "3ID":   {"label": "3ID (Tri)",       "color": "#FF2FBF",    # magenta
              "icon": "3id.png"},
    "TSEL":  {"label": "Telkomsel",       "color": "#E4002B",    # red
              "icon": "tsel.png"},
    "XL":    {"label": "XL / AXIS",       "color": "#2B7FFF",    # blue
              "icon": "xl.png"},
    "SF":    {"label": "Smartfren",       "color": "#00B85C",    # green
              "icon": "smartfren.png"},
    # Not an operator: independent phone/pulsa counters ("X CELL"), which
    # arrive in the same export but are a different class of place. Giving
    # them their own colour stops 77 outlets masquerading as unclassified
    # competitor service points.
    "IPP":   {"label": "IPP outlet (counter)", "color": "#FF8A3D"},  # orange
    "OTHER": {"label": "Other / unknown", "color": "#8B93A7"},
}

# Order matters. "Indosat Ooredoo Hutchison" contains 'hutchison', which
# belongs to 3ID, so IM3 has to be tested first or every IOH point would be
# filed under the wrong brand.
OPERATOR_PATTERNS = [
    ("IM3",  r"indosat|\bim3\b|ooredoo|\bioh\b"),
    ("TSEL", r"telkomsel|\btsel\b|grapari|gerai\s*halo"),
    ("XL",   r"\bxl\b|axiata|\baxis\b"),
    ("SF",   r"smartfren|\bsf\b|smart\s*telecom"),
    ("3ID",  r"\b3id\b|\btri\b|\b3\s?store\b|hutchison|\b3\s?kios\b"),
]

# Boundary layers are meant to be viewed *on top of each other*, so they get
# near-zero fill and visually distinct strokes rather than solid colours.
BOUNDARY_STYLE = {
    # `defer` keeps a layer out of the initial page load. Kelurahan is 887
    # polygons against kecamatan's 121 — around 5 MB of geometry — so it is
    # fetched the first time the box is ticked, not on every page open.
    "kelurahan":  {"label": "Desa / Kelurahan border", "color": "#6EC46E",
                   "weight": 0.8, "dash": None,    "fill": 0.03,
                   "defer": True},
    "kecamatan":  {"label": "Kecamatan border", "color": "#35D0E0",
                   "weight": 1.4, "dash": None,    "fill": 0.04},
    "indosat_mc": {"label": "Indosat MC border", "color": "#B388FF",
                   "weight": 2.6, "dash": "7 5",   "fill": 0.05},
}

# Fields the Desa layer can be tinted by on the Distribution page, in the
# order they appear in the pulldown. `kind` decides the ramp: "cat" gives
# each distinct value its own colour, "num" a sequential scale, "band" a
# pre-cut numeric scale with fixed break points.
KELURAHAN_THEMES = [
    {"field": "geo_type",   "label": "Geo type",            "kind": "cat"},
    {"field": "population", "label": "Population",          "kind": "band",
     "breaks": [2500, 5000, 10000, 20000, 40000]},
    {"field": "branch",     "label": "Branch (9)",          "kind": "cat"},
    {"field": "mc",         "label": "Microcluster (24)",   "kind": "cat"},
    {"field": "mc35",       "label": "Microcluster (35)",   "kind": "cat"},
    {"field": "branch11",   "label": "Branch (11)",         "kind": "cat"},
    {"field": "partner",    "label": "Partner / AF",        "kind": "cat"},
    {"field": "kabkot",     "label": "Kota / Kabupaten",    "kind": "cat"},
    {"field": "pop_density","label": "Population per km2",  "kind": "band",
     "breaks": [2500, 7500, 15000, 30000, 60000]},
]


# ── local layer folder ───────────────────────────────────────────────────
# Where the Distribution page looks for boundary and overlay files, so the
# recurring ones do not have to be dragged onto the page every session. Any
# .kml/.kmz dropped in here is offered for one-click import; the role is
# guessed from the filename and can be overridden in the UI.
LAYER_DIR = os.path.join(BASE_DIR, "Data Upload")

# filename fragment -> (role, legend label, colour). First match wins.
LAYER_ROLES = [
    ("mc36",      ("mc",      "Indosat MC border",       "#B388FF")),
    ("mc_",       ("mc",      "Indosat MC border",       "#B388FF")),
    ("site",      ("site",    "Sites",                   "#35d0e0")),
    ("desa",      ("desa",    "Desa / kelurahan border", "#6ec46e")),
    ("kel",       ("desa",    "Desa / kelurahan border", "#6ec46e")),
    ("kec",       ("kec",     "Kecamatan border",        "#4b96f3")),
    ("city",      ("kec",     "Kecamatan border",        "#4b96f3")),
    ("outlet",    ("outlet",  "DSE to outlet mapping",   "#ff8a3d")),
    ("dse",       ("outlet",  "DSE to outlet mapping",   "#ff8a3d")),
]


def layer_role(filename):
    low = os.path.basename(filename).lower()
    for frag, role in LAYER_ROLES:
        if frag in low:
            return role
    return ("other", os.path.basename(filename), "#8b93a7")

# ── point classes on the map ─────────────────────────────────────────────
# One registry for the dots: the endpoint classifies with it and the legend
# renders from it, so the two cannot drift. Colours are a validated
# categorical set for a dark ground -- every adjacent pair clears the
# colour-vision separation floor -- but colour is never the only channel:
# each dot carries a hover label and the legend names and counts every class.
POINT_CLASSES = [
    ("outlet",     "IOH outlet",       "#3987e5"),
    ("site",       "Network site",     "#c98500"),
    ("atm",        "ATM",              "#9085e9"),
    ("bank",       "Bank",             "#199e70"),
    ("minimarket", "Minimarket",       "#d95926"),
    ("transit",    "Transit",          "#d55181"),
    ("pasar",      "Pasar / market",   "#e66767"),
    ("poi_other",  "Other POI",        "#7c8598"),
]
POINT_COLOUR = {k: c for k, _l, c in POINT_CLASSES}
POINT_LABEL = {k: l for k, l, _c in POINT_CLASSES}

# category -> class. Matched on the lower-cased category, longest first, so
# "bank_or_credit_union" is not caught by the "bank" rule before its own.
POI_CLASS_RULES = [
    ("atm",        ("atm", "amenity=atm")),
    ("bank",       ("bank", "bank_or_credit_union", "amenity=bank",
                    "financial_service", "credit_union", "money_transfer")),
    ("minimarket", ("convenience_store", "shop=convenience", "supermarket",
                    "grocery_store", "asian_grocery_store", "shop=supermarket",
                    "warehouse_club_store")),
    ("transit",    ("highway=bus_stop", "bus_station", "highway=platform",
                    "train_station", "railway=station", "public_transport",
                    "transit_station", "subway_station")),
    ("pasar",      ("market", "marketplace", "amenity=marketplace",
                    "farmers_market", "shop=mall", "shopping_mall")),
]


def poi_class(category):
    c = (category or "").strip().lower()
    if not c:
        return "poi_other"
    for cls, keys in POI_CLASS_RULES:
        if c in keys:
            return cls
    for cls, keys in POI_CLASS_RULES:
        if any(k in c for k in keys):
            return cls
    return "poi_other"
