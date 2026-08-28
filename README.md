# NEW POLYGON PJP DSE

A second local application, beside — never inside — API Location Pulldown.

    ./start.sh                 http://127.0.0.1:5002
    ./start.sh --port=5003     somewhere else
    ./stop.sh                  stop whatever start.sh started

The application on **5001 is untouched**. This one has its own database
(`data/poi_pulldown.db`), its own layer folder (`Data Upload/`) and its own
`.venv`. Nothing here reads or writes the other, and the two can run at the
same time.

## Why it exists

Four pages, not six. Two of them are the pages this work is actually about —
Distribution Polygon and Preview Polygon, copied whole from 5001 — and two are
new:

| menu | what it is |
|---|---|
| **Configuration** | Load the base data layers. States what the application needs, reports what is loaded against it, and says which page can work. |
| **Distribution Polygon** | Outlets, the DSE territory models, and the boundaries beneath them. Unchanged from 5001. |
| **Preview Polygon** | Territory summary by area, branch and microcluster. Unchanged from 5001. |
| **Layer Model** | The stack as it actually stands, in draw order. |

`/` redirects to Configuration. An application whose first act is to ask for
data should open on the screen that takes it, not on an empty table.

## The first run, in order

```
./start.sh              # builds the .venv the first time, then serves on 5002
```

Then, on the Configuration page, fill the four slots. They can be filled in
any order; the two derived steps at the bottom of the page are run once the
files they need are in.

| slot | file | what it produces |
|---|---|---|
| **Desa Polygon & Profiles** | `Desa_kel_inner2308.kmz` | 887 desa polygons |
| | `City BorderKec_kabAgus26_inner.kmz` | 121 kecamatan polygons |
| | `NCP Village Level_<month> Jaya.xlsx` | 887 desa + 121 kecamatan rolled up + the month's figures |
| **DSE & Outlet Mapping** | `DSE Mapping to Outlet <circle>.xlsx` | 26,342 outlets, 496 DSE |
| **Site Locations** | `Site Locations.xlsx` | 5,698 sites, as a map layer and in `ref_site` |
| **MC Hierarchy** | `mc36_all_inner_reff.kmz` | 36 microcluster polygons |

then run, in this order:

1. **MC-36 mapping onto kecamatan and desa** — seconds.
2. **Place sites against the DSE geography** — two to four minutes at this
   volume. It is a step you start rather than a cost hidden inside an upload,
   because an upload that silently takes four minutes looks like one that has
   hung.

Every slot links to its own field specification in `Data Templates/`.

### The formats, as they actually ship

These are the files the Regional Head standardised on (2026-08-25), and none
of them matched what the parent app could read:

- **NCP village level** keys on `Village (Desa)` / `District (Kecamatan)` /
  `City (Kota/Kab)`, not `KEL_DES` / `KEC` / `KAB_KOT`. Only rows whose
  `REGION` is `INNER JAKARTA` are loaded — 887 of 7,771 — and **that filter is
  the territory definition**, not a convenience. Its figures (population,
  area, NCP 4G, PJP IM3, PJP 3ID, coverage stamp) land in `ref_metric` under
  the period read from the sheet name, so `Jun'26` sits beside `Jul'26`
  instead of overwriting it. Kecamatan are rolled up from the villages by a
  population-weighted majority — there is no separate kecamatan upload.
- **Outlet → DSE** now arrives as a spreadsheet, not a KMZ. 73,699 rows across
  three regions; the 26,342 in Inner Jakarta are kept. Columns are renamed on
  import to the field names the old KMZ exports used — `DSE_CODE`,
  `Outlet_Cod`, `Micro_Clus` — so everything downstream behaves exactly as it
  did. The old branch-by-branch KMZs still load in the same slot.
- **Site locations** fills the map layer *and* `ref_site` from one upload. On
  the parent app those were two different jobs and only one of them had a
  button, which is why `ref_site` sat empty for a week.

### MC-36 is primary, and the guard says so

Both microcluster generations claim the same layer key, and the 24-MC rule
replaces rather than merges. Uploading `Indosat MC BorderAREASA_MCAgus26` —
the 24-MC MCAgus cut — would therefore swap the primary cut without a word
and change the meaning of every figure joined on it. The MC Hierarchy slot
reads the fields before importing and **refuses that file by name**.

The two cuts disagree, and the page says so rather than reconciling them:
**430 of 887 desa** sit in a different microcluster than the NCP profile's own
`MC` column claims. Both values are kept — `mc` as shipped, `mc36` as derived
from the polygons — and the derived card carries the count.

## Ready means ready for a page

There is no single "ready" flag, because the Distribution map is perfectly
usable with no reference figures at all and saying otherwise would stop
somebody working. Each page names what it cannot do without:

| page | cannot work without | thinner without |
|---|---|---|
| Distribution Polygon | outlet → DSE mapping | the boundary layers, sites |
| Preview Polygon | outlet → DSE mapping, village profile, the MC-36 derivation | the boundary layers, sites |
| Layer Model | nothing | everything |

A page whose inputs are missing says so on the Configuration page, with the
missing input named.

## What was verified

Every slot was filled from the real files against a database created from
nothing:

    887 desa polygons · 121 kecamatan polygons · 36 microcluster polygons
    887 desa profiles from 7,771 rows, 9,757 figures under period Jun'26
    121 kecamatan rolled up — exactly matching the polygon count
    26,342 outlets from 73,699 rows · 496 DSE · IM3 12,926 / 3ID 13,416
    5,698 sites, in the map layer and in ref_site
    MC-36 derived onto 121 kecamatan and 887 desa
    the 24-MC file refused by the MC Hierarchy slot, by name
    site placement verified on a 300-site subset: 295 by polygon, 5 by
      nearest outlet, 0 unplaced

## Layout

```
app.py                  routes; / redirects to /configuration
configuration.py        the Configuration and Layer Model pages and their API
territory_api.py        everything else — copied from 5001, only MENUS changed
templates/
  configuration.html    the requirement, the upload panel, the file list
  layer_model.html      the stack, the draw order, the rules
  distribution.html     copied unchanged
  preview.html          copied unchanged
static/css/configuration.css   new — not one new colour, all tokens
static/js/configuration.js     new
static/js/layer_model.js       new
Data Templates/         the four template and specification workbooks
Data Upload/            where uploaded files land
data/                   the database, created on first run
```

Everything else — `import_local.py`, `dse_coverage.py`, `force_fit.py`,
`territory_rollup.py`, `filestore.py`, `geom.py`, the POI connectors — is
copied from 5001 unchanged, so a fix there can be brought across by copying the
file.

## What is deliberately not here

The Overview, Retail Gapura and Future GAPURA pages. Their routes still exist
because they share modules with the pages that stayed, but they are not in the
menu and nothing links to them. The POI connectors are copied and work, but no
POI has been pulled into this database — the Distribution page's catalogue will
be empty until `python pull_osm.py --all` or one of its siblings is run.
