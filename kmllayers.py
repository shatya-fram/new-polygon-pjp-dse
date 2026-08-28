#!/usr/bin/env python3
"""
The five KML layers, the fields they carry, and what each field is FOR.

ONE REGISTRY, THREE CONSUMERS
    The map card, the filter list and the derived measures are all read from
    the same entry. Before this, "which fields matter" lived in whoever last
    edited a template -- so a field added to an export appeared in the popup
    and nowhere else, or in a filter that nothing could draw.

    preview   what a click shows, in order. Five is the budget; a card that
              lists thirty attributes is a table, and nobody reads it.
    filters   what can be picked from. Low-cardinality classifications only:
              a filter on 82,000 desa names is a search box, not a filter.
    derive    computed on import and stored beside the real fields, so they
              filter, colour and group exactly like the rest. Density and
              youth share are the two figures that actually rank a place,
              and neither is in any file.

WHY MATCH ON FIELDS AND NOT ON FILE NAMES
    The same trap as the boundary KMZs: an export called `Border Desa.kml`
    is only a desa layer until somebody re-exports it. The fields inside are
    what it is.
"""

# Age bands, as the KML truncates them. Twenty columns of five-year bands are
# the richest thing in the desa file and unreadable as twenty columns; they
# exist here so the two shares below can be computed from them.
AGE = {
    "0_4": "Usia_0_4_t", "5_9": "Usia_5_9_t", "10_14": "Usia_10_14",
    "15_19": "Usia_15_19", "20_24": "Usia_20_24", "25_29": "Usia_25_29",
    "30_34": "Usia_30_34", "35_39": "Usia_35_39", "40_44": "Usia_40_44",
    "45_49": "Usia_45_49", "50_54": "Usia_50_54", "55_59": "Usia_55_59",
    "60_64": "Usia_60_64", "65_69": "Usia_65_69", "70_74": "Usia_70_74",
    "75_up": "Usia_75_th",
}
# The age bands stay in the file and are still imported as attributes; what
# was removed (2026-08-26, Regional Head) is any DERIVED share built on them,
# and the sex ratio with it. They were my suggestion, not a requirement, and
# a card carrying a figure nobody asked to see is a card people stop reading.
YOUTH = []
WORKING = []

LAYERS = [
 {"key": "desa", "layer_key": "kelurahan", "kind": "polygon",
  "label": "Desa / kelurahan boundary",
  "source": "Border Desa.kml",
  "match": ({"KEL_DES"}, {"MC260", "MC281", "Jumlah_Pen"}),
  "name_field": "KEL_DES",
  "prov_field": "PROV",
  "join": ["NO_PROV", "NO_KAB", "NO_KEC", "NO_DES"],
  "preview": [("KEL_DES", "Desa"), ("KEC", "Kecamatan"),
              ("Jumlah_Pen", "Population"), ("_DENSITY", "People per km²"),
              ("MC260", "Microcluster (260)")],
  "context": ["KAB_KOT", "PROV", "MC281", "Luas_Area_"],
  "filters": ["PROV", "KAB_KOT", "MC260", "MC281"],
  "derive": ["_DENSITY"],
  "drop": ["REG", "KEY"]},

 {"key": "kecamatan", "layer_key": "kecamatan", "kind": "polygon",
  "label": "Kecamatan boundary",
  "source": "Border Kecamatan.kml",
  "match": ({"Kec", "Kab_kot", "Populasi"}, set()),
  "name_field": "Kec",
  "prov_field": "Prov",
  "join": [],
  "preview": [("Kec", "Kecamatan"), ("Kab_kot", "Kota / kabupaten"),
              ("Populasi", "Population"), ("Prov", "Province")],
  "context": ["Laki_Laki", "Perempuan"],
  "filters": ["Prov", "Kab_kot"],
  "derive": [],
  # Every code column is zero in all 6,830 rows, and Kel_des holds a single
  # desa name on a kecamatan row -- showing it invites the reader to think
  # they are looking at a village.
  "drop": ["No_prov", "No_kab", "No_kec", "No_des", "Top_500_FWA", "Kel_des"]},

 {"key": "kabkot", "layer_key": "kabkot", "kind": "polygon",
  "label": "Kabupaten / kota boundary",
  "source": "Border KabKot.kml",
  "match": ({"KAB_KOT", "Kabupaten_Priority"}, set()),
  "name_field": "KAB_KOT",
  "prov_field": "PROV",
  "join": ["NO_PROV", "NO_KAB"],
  "preview": [("KAB_KOT", "Kota / kabupaten"),
              ("Kabupaten_Priority", "Priority"),
              ("Attack_3ID", "3ID play"), ("Jumlah_Penduduk", "Population"),
              ("_DENSITY", "People per km²")],
  "context": ["PROV", "Luas_Area_Km2", "IM3_3ID_irisan",
              "New Kab Tagging_New Kab Tagging V2",
              "New Kab Tagging_New Kab Tagging_1"],
  "filters": ["PROV", "Kabupaten_Priority", "Attack_3ID", "IM3_3ID_irisan",
              "New Kab Tagging_New Kab Tagging V2",
              "New Kab Tagging_New Kab Tagging_1"],
  "derive": ["_DENSITY"],
  # KEC on this layer holds coverage bands ("75% - 80%"), not a kecamatan
  # name. Renamed rather than dropped: the value is real, the label is not.
  "rename": {"KEC": "COVERAGE_BAND"},
  "drop": ["Code_Unique_Kec", "NO_KEC", "NO_DES", "_8_Blue_Kab"]},

 {"key": "territory", "layer_key": "indosat_mc", "kind": "polygon",
  "label": "Indosat internal territory",
  "source": "Teritory Border Aug.kml",
  "match": ({"MC IOH", "BRANCH", "REGION"}, set()),
  "name_field": "MC IOH",
  "prov_field": "PROV",
  "region_field": "REGION",
  "circle_field": "CIRCLE",
  "join": ["NO_PROV", "NO_KAB", "NO_KEC"],
  "preview": [("MC IOH", "Microcluster"), ("_HIERARCHY", "Branch › Area › Region"),
              ("URBAN/RURA", "Urban / rural"), ("Jumlah_Pen", "Population"),
              ("_DENSITY", "People per km²")],
  "context": ["KECAMATAN", "KABKOT", "MC 3ID", "PT IM3", "PT 3ID",
              "PARTNER IM", "PARTNER 3I", "MPC/MIM3", "MP3/3KIOSK",
              "Smartbeat", "Luas_Area_"],
  "filters": ["CIRCLE", "REGION", "AREA", "BRANCH", "MC IOH", "URBAN/RURA",
              "MPC/MIM3", "MP3/3KIOSK", "Smartbeat"],
  "derive": ["_DENSITY", "_HIERARCHY"],
  # Four more spellings of the same composite key invite a join on the wrong
  # one; KEC duplicates KECAMATAN.
  "drop": ["Code_Uniqu", "KECKAB", "KEC_UNIQUE", "KEC ID", "KABKOT ID", "KEC"]},

 {"key": "outlet", "layer_key": "outlet_dse", "kind": "point",
  "label": "Outlet → DSE mapping (PJP into desa)",
  "source": "DSE PJP into Desa <circle> vShare.xlsx  [sheet: OUTLET TO DSE]",
  "match": ({"DSE_CODE", "Outlet_Cod"}, {"Micro_Clus", "Desa_Name"}),
  "name_field": "Outlet_Nam",
  "region_field": "Region_Nam",
  "circle_field": "Circle_Nam",
  "join": [],
  # Uploaded BY LOCATION: every row carries LONG/LAT, so the layer draws as
  # points and the desa it falls in is a variable on the row, not a separate
  # upload. That is what makes "PJP into desa" answerable on the map.
  "preview": [("Outlet_Nam", "Outlet"), ("DSE_CODE", "DSE"),
              ("Desa_Name", "Desa"), ("Micro_Clus", "Microcluster"),
              ("Outlet_Cat", "Category")],
  "context": ["Outlet_Cod", "Brand_Name", "Kecamatan", "Kabupaten",
              "Branch_Nam", "Area_Name", "Region_Nam", "Partner_Te",
              "Partner_Ty", "Hybrid_Non"],
  "filters": ["Region_Nam", "Area_Name", "Branch_Nam", "Micro_Clus",
              "Brand_Name", "Outlet_Cat", "Partner_Ty", "Hybrid_Non",
              "Kabupaten", "Desa_Name"],
  "derive": [],
  "drop": []},

 {"key": "sites", "layer_key": "site_locations", "kind": "point",
  "label": "Site locations (SnD reference)",
  "source": "SnD Site Reference Aug.kml",
  "match": ({"New Site ID"}, {"Site Type", "New Site Name"}),
  "name_field": "New Site ID",
  "region_field": "Region New",
  "circle_field": "Circle New",
  "join": [],
  "preview": [("New Site ID", "Site"), ("Site Type", "Type"),
              ("KECAMATAN", "Kecamatan"),
              ("ADDRESSABLE (August'26)", "Addressable"),
              ("CATEGORY (August'26)", "Category")],
  "context": ["New Site Name", "BRANCH", "Area", "Region New",
              "KEC RURAL/URBAN", "ISLAND"],
  "filters": ["Region New", "Area", "BRANCH", "Site Type",
              "KEC RURAL/URBAN", "ADDRESSABLE (August'26)",
              "CATEGORY (August'26)"],
  "derive": [],
  "drop": ["Java/Outside Java", "KECAMATAN UNIK"]},
]

# ── how far each layer is imported ───────────────────────────────────────
# Every layer is clipped to the three regions. The national exports were
# imported whole for a while so that territory outside the patch could be
# seen; it turned out to be noise on a map of Jakarta -- 6,253 microclusters
# and 468 kabupaten drawn across the archipelago behind the working area --
# so scope is once again a hard boundary rather than a shading. Setting a
# key here would import that layer whole again.
NATIONAL = ()


def scoped(spec):
    """Should this layer's import apply the scope filter?"""
    return (spec or {}).get("key") not in NATIONAL


BY_KEY = {ly["key"]: ly for ly in LAYERS}
BY_LAYER = {ly["layer_key"]: ly for ly in LAYERS}


def slim_fields(layer_key):
    """The attribute columns a map layer actually needs on the client.

    The outlet export carries ~50 MB of attributes across 73k rows; shipping
    all of it is what made the browser hang before it drew a single point.
    Preview, filter and context fields are what the map, the tooltip and the
    grouping controls read -- the rest is fetched per feature when a popup
    is actually opened."""
    spec = BY_LAYER.get(layer_key)
    if not spec:
        return None
    keep = {"feature_key", "name"}
    keep.update(f for f, _ in spec.get("preview", []))
    keep.update(spec.get("filters", []) or [])
    keep.update(spec.get("context", []) or [])
    for f in ("name_field", "region_field", "prov_field", "circle_field"):
        if spec.get(f):
            keep.add(spec[f])
    keep.update(DERIVED_LABEL)
    return keep

DERIVED_LABEL = {
    "_DENSITY": "People per km²",
    "_HIERARCHY": "Branch › Area › Region",
}


def num(attrs, key):
    try:
        return float(str(attrs.get(key, "")).replace(",", ""))
    except (TypeError, ValueError):
        return None


def derive(spec, attrs):
    """Compute the declared derived fields. A term that cannot be computed is
    left out entirely rather than written as zero -- a desa with no population
    is not a desa with a density of nought."""
    out = {}
    for d in spec.get("derive", ()):
        if d == "_DENSITY":
            pop = num(attrs, "Jumlah_Pen") or num(attrs, "Jumlah_Penduduk") \
                or num(attrs, "Populasi")
            area = num(attrs, "Luas_Area_") or num(attrs, "Luas_Area_Km2")
            if pop is not None and area:
                out[d] = str(round(pop / area))
        elif d == "_HIERARCHY":
            bits = [attrs.get("BRANCH"), attrs.get("AREA"), attrs.get("REGION")]
            bits = [b for b in bits if b]
            if bits:
                out[d] = " › ".join(bits)
    return out


def classify(fields):
    """-> the layer spec whose required fields this file carries, or None."""
    have = set(fields)
    best, best_n = None, -1
    for spec in LAYERS:
        need, helps = spec["match"]
        if need <= have:
            n = len(need) + len(helps & have)
            if n > best_n:
                best, best_n = spec, n
    return best
