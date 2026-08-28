#!/usr/bin/env python3
"""
The local data-file store behind the Data Files menu.

Every recurring input -- the microcluster and administrative boundaries, the
DSE-to-outlet export, the site list -- lives as a file in one folder on this
machine, and this module is the only thing that writes to it. Uploading,
listing, importing, unloading and removing all go through here so the folder
and the database cannot drift apart.

ROLES
    A file's role decides how it is imported and what it is called on screen.
    It is guessed from the filename and can be overridden at upload time,
    because a file named `export_final_v3.kmz` tells you nothing.

DELETING
    Removing a file and removing its data are separate acts, and the UI keeps
    them separate:

        unload  drop the imported rows, leave the file
        delete  remove the file, and its rows if asked

    Where the filesystem refuses removal -- a synced or read-only folder, and
    some sandboxes -- the file is moved into `_trash/` instead of failing.
    The caller is told which of the two happened rather than being left to
    assume the file is gone.
"""
import os
import re
import shutil
import time

import config

ROLES = {
    "mc":      {"label": "Indosat MC polygon",     "colour": "#B388FF",
                "kinds": (".kml", ".kmz")},
    "kec":     {"label": "Kecamatan polygon",      "colour": "#4b96f3",
                "kinds": (".kml", ".kmz")},
    "desa":    {"label": "Desa / kelurahan polygon", "colour": "#6ec46e",
                "kinds": (".kml", ".kmz")},
    "outlet":  {"label": "DSE to outlet mapping",  "colour": "#ff8a3d",
                "kinds": (".kml", ".kmz")},
    "site":    {"label": "Site location",          "colour": "#35d0e0",
                "kinds": (".kml", ".kmz", ".xlsx", ".xlsm", ".csv")},
    "service": {"label": "Operator service points", "colour": "#f0d04b",
                "kinds": (".kml", ".kmz")},
    "other":   {"label": "Other layer",            "colour": "#8b93a7",
                "kinds": (".kml", ".kmz", ".xlsx", ".xlsm", ".csv")},
}
ORDER = ["mc", "kec", "desa", "outlet", "site", "service", "other"]

# Longest, most specific fragments first: a file called
# "site_dse_mapping.kmz" is a DSE export, not a site list.
GUESS = [
    ("dse", "outlet"), ("outlet", "outlet"),
    ("site", "site"), ("bts", "site"), ("tower", "site"),
    ("service point", "service"), ("service_point", "service"),
    ("mc36", "mc"), ("mc_", "mc"), ("microcluster", "mc"),
    ("desa", "desa"), ("kel", "desa"),
    ("kec", "kec"), ("city border", "kec"), ("kabupaten", "kec"),
]

# ── WHICH DATA BELONGS ON WHICH PROJECT'S MAP ──────────────────────────────
# The two projects ask different questions and must not borrow each other's
# overlays. Retail Gapura and Future GAPURA are about where a point of
# presence should be, so they see operator service points and never the trade
# channel. Distribution Polygon is about who covers what on the ground, so it
# sees DSE, outlets and sites and never the Gapura layers.
#
# The boundaries -- microcluster, kecamatan, desa/kelurahan -- are the frame
# both are drawn on, so they belong to neither and appear in both. Anything
# whose role could not be guessed stays visible everywhere rather than
# quietly disappearing from the only page that would have shown it.
BASELINE_ROLES = ("mc", "kec", "desa")
DOMAIN_ROLES = {
    "gapura":       BASELINE_ROLES + ("service", "other"),
    "distribution": BASELINE_ROLES + ("outlet", "site", "other"),
    "baseline":     BASELINE_ROLES,
}

# Layers that predate the file store, whose key is not a filename.
BUILTIN_ROLE = {"kecamatan": "kec", "kelurahan": "desa", "desa": "desa",
                "indosat_mc": "mc", "mc": "mc", "microcluster": "mc"}


def layer_role(layer_key, source_file=None):
    """The role of an imported layer, from the file it came from.

    Both the file name and the layer key get a say, because one of them is
    often useless: an outlet export that arrived as `tmpb8_uphst.kmz` is
    still recognisable as `outlet_to_dse3id_bks`, and vice versa.
    """
    if layer_key in BUILTIN_ROLE:
        return BUILTIN_ROLE[layer_key]
    for candidate in (source_file, layer_key):
        if candidate:
            role = guess_role(candidate)
            if role != "other":
                return role
    return "other"


def in_domain(domain, layer_key, source_file=None):
    """True if this layer may be shown on that project's map.

    An unknown domain filters nothing, so a caller that does not care, or a
    new page that has not been told about domains yet, still sees everything.
    """
    roles = DOMAIN_ROLES.get((domain or "").strip().lower())
    if not roles:
        return True
    return layer_role(layer_key, source_file) in roles


ALLOWED = (".kml", ".kmz", ".xlsx", ".xlsm", ".csv")
SAFE_NAME = re.compile(r"^[A-Za-z0-9 ._()\-]+$")


def guess_role(filename):
    low = os.path.basename(filename).lower()
    for frag, role in GUESS:
        if frag in low:
            return role
    return "other"


def folder():
    d = config.LAYER_DIR
    os.makedirs(d, exist_ok=True)
    return d


def trash():
    d = os.path.join(folder(), "_trash")
    os.makedirs(d, exist_ok=True)
    return d


def safe_path(name):
    """Resolve a caller-supplied name to a path inside the folder, or raise.

    Rejects anything with a separator or an unexpected character rather than
    trying to sanitise it -- a name that needs cleaning is a name that should
    not be trusted."""
    name = (name or "").strip()
    if not name or name.startswith(".") or os.path.basename(name) != name:
        raise ValueError("bad file name")
    if not SAFE_NAME.match(name):
        raise ValueError("file name has characters that are not allowed")
    if not name.lower().endswith(ALLOWED):
        raise ValueError(f"only {', '.join(ALLOWED)} files are handled")
    p = os.path.realpath(os.path.join(folder(), name))
    if os.path.dirname(p) != os.path.realpath(folder()):
        raise ValueError("path escapes the data folder")
    return p


def unique_name(name):
    """Never silently overwrite: a second upload of the same name becomes
    `name (2).kmz`, so the previous generation is still there to compare."""
    base, ext = os.path.splitext(name)
    p = os.path.join(folder(), name)
    i = 2
    while os.path.exists(p):
        name = f"{base} ({i}){ext}"
        p = os.path.join(folder(), name)
        i += 1
    return name


def listing(con=None):
    """Every data file, with whether it is imported and what it produced."""
    loaded = {}
    if con is not None:
        try:
            for r in con.execute(
                    "SELECT layer_key,label,kind,source_file,feature_count,"
                    "imported_utc FROM geo_layer"):
                loaded.setdefault(r["source_file"], []).append(dict(r))
        except Exception:                                     # noqa: BLE001
            loaded = {}
    out = []
    d = folder()
    for n in sorted(os.listdir(d)):
        if n.startswith((".", "~$")) or not n.lower().endswith(ALLOWED):
            continue
        full = os.path.join(d, n)
        if not os.path.isfile(full):
            continue
        st = os.stat(full)
        role = guess_role(n)
        layers = loaded.get(n, [])
        out.append({
            "file": n,
            "role": role,
            "role_label": ROLES[role]["label"],
            "colour": ROLES[role]["colour"],
            "ext": os.path.splitext(n)[1].lower(),
            "bytes": st.st_size,
            "modified": time.strftime("%Y-%m-%d %H:%M",
                                      time.localtime(st.st_mtime)),
            "loaded": bool(layers),
            "layers": layers,
            "features": sum(l.get("feature_count") or 0 for l in layers),
        })
    return out


def save_upload(fileobj, filename):
    name = unique_name(os.path.basename(filename))
    safe_path(name)                       # validates before anything is written
    path = os.path.join(folder(), name)
    fileobj.save(path)
    return name, path


def remove(name):
    """-> ('deleted'|'trashed', path). Never raises for a read-only folder."""
    p = safe_path(name)
    if not os.path.isfile(p):
        raise FileNotFoundError(name)
    try:
        os.remove(p)
        return "deleted", p
    except OSError:
        # A synced, locked or sandboxed folder refuses removal. Moving the
        # file out of the way is the same outcome for everything that reads
        # this folder, and it is recoverable.
        dest = os.path.join(trash(), f"{int(time.time())}-{os.path.basename(p)}")
        shutil.move(p, dest)
        return "trashed", dest


def unload(con, layer_key):
    """Drop an imported layer's rows, leaving the file alone."""
    n = con.execute("SELECT count(*) FROM geo_feature WHERE layer_key=?",
                    (layer_key,)).fetchone()[0]
    con.execute("DELETE FROM geo_feature WHERE layer_key=?", (layer_key,))
    con.execute("DELETE FROM geo_layer WHERE layer_key=?", (layer_key,))
    con.commit()
    return n
