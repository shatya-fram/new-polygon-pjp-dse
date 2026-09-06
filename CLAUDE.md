# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# NEW POLYGON PJP DSE — notes for Claude

A sibling of API Location Pulldown, not a fork of it. Read `README.md` first;
this file is only the things that are easy to get wrong.

## Commands

```bash
./start.sh                  # migrate, serve on 5002, open a browser
./start.sh --no-open        # same without the browser (= make serve)
./stop.sh                   # stop whatever start.sh started
./setup.sh                  # venv + deps + DuckDB extensions + migrate; re-runnable
make migrate                # schema only; idempotent, safe any time
make test                   # tests/test_pulls.py — see below
```

`start.sh` **restarts rather than refuses**: a previous copy of this app on the
port is stopped first, but a stranger on the port makes it step to the next
free one and say so. Flags: `--port=`, `--no-open`, `--no-migrate`,
`--strict-port`. The chosen port lands in `.server-port` so `./stop.sh` finds a
moved server.

**Always `./.venv/bin/python`, never bare `python`.** On this machine bare
`python` resolves to the *parent app's* venv at
`API Location Pulldown/.venv/bin/python` — running this app's scripts under it
is exactly the cross-contamination the first ground rule forbids, and it fails
quietly rather than loudly.

```bash
./.venv/bin/python migrate.py           # into an empty data/ directory
./.venv/bin/python remap_mc.py          # derivation 1 — seconds
./.venv/bin/python derive_sites.py      # derivation 2 — two to four minutes
./.venv/bin/python prebuild_maps.py     # rebuild every mapcache file + the rollup cache
./.venv/bin/python purge_offmap.py      # report features outside HOME_BOUNDS; --apply removes
./.venv/bin/python mcprofile.py "MC-JAKARTA PUSAT"   # who works a microcluster
node --check static/js/map_workspace.js # after EVERY edit to the browser code
```

Publishing (details in `DEPLOY.md`, which is the plan; these are the two
commands it turns on):

```bash
./.venv/bin/python make_public_db.py    # scrubbed copy -> data/public/; --dry reports only
./deploy/build_release.sh               # -> dist/pjp-dse-<stamp>.tar.gz
                                        # refuses a phone field, a .env, a .kml,
                                        # or an outlet_dse / site_locations map cache
```

The `Makefile` is inherited from 5001 and most of its targets
(`pull-overture`, `pull-osm`, `reconcile`, `enrich`, `export`, `map`, …) drive
the POI pipeline, which no page here uses. `migrate`, `serve`, `start`,
`stop`, `test` are the ones that mean anything in this app.

## Ground rules

- **This app is 5002. The parent app is 5001.** Separate database, separate
  layer folder, separate venv. Never make one read the other's files — the
  whole point of the copy is that an experiment here cannot damage what is
  running there.
- **Modules copied from the parent are copies, not forks.** `import_local.py`,
  `territory_rollup.py`, `dse_coverage.py`, `force_fit.py`, `filestore.py`,
  `geom.py`, `territory_api.py` should stay byte-identical to 5001 apart from
  `territory_api.MENUS`. Fix a bug in the parent and copy the file across; do
  not diverge quietly, or the next copy silently reverts the fix.
- **New behaviour goes in `configuration.py`.** That is the only module written
  for this app, and it should stay the only one.
- **No new colours.** `static/css/configuration.css` uses tokens from
  `style.css` and nothing else, which is what makes the theme switch work on
  the new pages for free. The parent app collapsed two hundred scattered hex
  literals onto those tokens to make a light theme possible at all — do not
  start the drift again.

## The one list that matters

`configuration.LAYERS` is the single statement of what this application needs.
The Configuration page, the Layer Model page and the readiness of every route
are all rendered from it. Adding an input means adding one entry there — not
touching three templates.

Each entry declares its own **import mode**, and that is the fix this app
exists to carry: the parent chose the importer from the file extension, so a
reference workbook uploaded through the web UI was read as a table of places.
Here the slot decides. Never re-introduce a guess from the file name.

`configuration.DERIVATIONS` is the same idea for steps that are computed rather
than uploaded. There are two — `remap_mc.py` (`mc36`) and `derive_sites.py`
(`site_dse`). Both run as a subprocess on purpose: the script owns its own
argument parsing and its own exits, and a long derivation that fails should not
take the web process with it.

## Traps

- **`ref_kecamatan.mc36` does not exist until `remap_mc.py` has run.** It is the
  script that adds the column. So a status check for it must treat
  `sqlite3.OperationalError` as "has never run", not as a broken database.
  `/api/rollup` fails with `no such column: mc36` when this is skipped — that
  error means the derivation, not a bug in the rollup.
- **Import a desa export missing `MC36` and it is classified as a kecamatan
  layer**, overwriting 121 polygons with 887. `import_local.LAYER_RULES` tests
  the desa rules before the kecamatan one for exactly this reason. The boundary
  field specification in `Data Templates/` is there to be checked before an
  upload, not after.
- **A parse failure keeps the file.** It is nearly always the wrong file for
  the slot, and deleting it hides which one it was. Do not "tidy up" by
  removing it on error.
- **`territory_api._poly_cache` lives for the life of the process.** Any new
  import path must clear it (`configuration._clear_caches`) or the upload looks
  like it failed until a restart.
- **SQLite cannot be created on the Cowork device mount.** `migrate.py` run
  through `device_bash` against `~/mnt/...` fails with `disk I/O error` — that
  is the bridge's filesystem, not the app. To smoke-test, copy the app to the
  VM's own disk, run it there, and copy only the source files back.

## Verifying a change

`tests/test_pulls.py` is inherited from 5001 and covers the POI pipeline, not
the new pages. It is a **flat script**, not pytest — a linear sequence of
`check(label, cond)` calls printing `[PASS]`/`[FAIL]`; there is no way to run
one test, run the file and narrow with grep on the `section()` headings:

```bash
make test                                            # or ./.venv/bin/python tests/test_pulls.py
./.venv/bin/python tests/test_pulls.py | grep -B3 FAIL
```

**The clean baseline here is `75 passed, 1 failed`.** The failure is
`[FAIL] / renders`, and it is expected: the copied test asserts the parent's
`/` returns 200, while here `/` is a deliberate 302 to `/configuration`. Do not
"fix" it by changing the redirect. Any *other* failure is real.

The new pages have no test suite. The check that catches most things:

```bash
./.venv/bin/python migrate.py                # into an empty data/ directory
./start.sh --no-open                          # then load every menu item
curl -s localhost:5002/api/configuration/status   # honest about what is missing
curl -s localhost:5002/api/layer-model            # 0 unclaimed layers
```

Load every menu item literally. `territory_api.MENUS` is the list, and it is
six now, not the four the README still describes — `/configuration`,
`/distribution`, `/preview-polygon`, `/polygon-samples`, `/map-workspace`,
`/layer-model` — plus `/data-files` and `/territory`, which are routed but not
in the menu. These pages are rendered from `configuration.LAYERS` by key, so a
renamed or dropped key raises a `KeyError` at request time and nothing at
import time; only a request finds it.

`unclaimed` being non-zero means a layer was imported that no declared input
claims — usually a file that went into the wrong slot. That number is the
cheapest signal that the role matching in `configuration.status()` has drifted.

## The four slots and the formats behind them (2026-08-25)

`configuration.SLOTS` names what somebody uploads against; `configuration.LAYERS`
lists the PARTS inside each slot. Desa Polygon & Profiles holds three parts
because three different files make it up and they arrive on different days —
a slot that could only say "incomplete" would be no help.

`formats.py` is the only module that knows what the current files look like.
It exists because loosening `import_local.py` until it accepted everything is
how a desa master ends up half-read and silently 300 rows short. Rules:

- **The slot picks the reader, never the file name — and for polygons, never
  the extension either.** Three boundary exports are `.kmz` and only their
  fields tell them apart. `formats.boundary_kind()` reads the file;
  `check_boundary()` refuses the wrong one with a sentence saying which slot
  it belongs in.
- **`REGION = "INNER JAKARTA"` is the territory definition.** Both workbooks
  span far more. The filter is named once, in `formats.py`, and reported in
  every import log. Change it there or not at all.
- **Column names are translated to the KMZ vocabulary on import**
  (`OUTLET_FIELDS`). Six downstream modules already know `DSE_CODE` and
  `Micro_Clus`; teaching them a second vocabulary would have been the
  expensive way round.
- **MC-36 is primary** (`formats.MC_PRIMARY`). Both cuts claim `indosat_mc`
  and the 24-MC rule replaces rather than merges, so the guard is the only
  thing standing between a drag-and-drop and a silent change of meaning in
  every figure joined on mc36.

## Do not put slow work inside an upload

`stamp_sites` at this volume is two to four minutes — `dse_coverage.build` is
fast (0.3 s for 496 reps) but `assign` is ~1.3 minutes for 5,698 sites and the
desa/kecamatan lookup adds more. It was briefly wired into the outlet import
and made a 6-second upload look like a hung one. It is now `derive_sites.py`,
a declared step beside `remap_mc.py`. Anything else that grows with the data
belongs there too, not in a request handler.

## Numbers worth remembering — the INNER JAKARTA slice

Loading all four slots from the real files gives 887 desa · 121 kecamatan ·
36 MC · 5,698 sites · 26,342 outlets · 496 DSE · 9,757 figures at period
Jun'26. The outlet import is ~6 s. **430 of 887 desa** disagree between the
profile's `mc` and the derived `mc36` — that is a finding the page reports,
not a bug to fix.

## Map Workspace — one map, four views (2026-08-27)

`/map-workspace` is the page this application is now mainly about. Cloud
boundaries underneath, the user's own DSE-to-outlet workbook read over them in
the browser, cleared on sign-out. `templates/map_workspace.html` +
`static/js/map_workspace.js` (~185 KB) + `static/css/workspace.css`.

**One `L.canvas()` renderer for every layer, and `reorder()` after every add.**
Leaflet gives each *pane* its own `<canvas>`, and a canvas is opaque to the
mouse: the topmost one swallows every hover and click beneath it. Put layers in
separate panes and the moment local outlets draw, no boundary can be hovered
again — which is exactly the bug reported as "mouseover not working". Depth
comes from draw order plus `reorder()` re-asserting it, never from panes. Any
new layer must take `renderer: RENDER` and be followed by `reorder()`.

**The territory endpoint emits lower-cased property names.** `kec`, `kab_kot`,
`kel_des`, `mc281`, `jumlah_pen` — not `KEC`, not `kecamatan`. Reading
`p.kecamatan` returns `undefined` on *every* desa, which does not throw; it
silently makes every `desa|kecamatan` key half-empty, so nothing joins and the
page reports "7,761 desa with no outlet" while looking healthy. Go through the
`featName / featKec / featKab / featMc / featPop` accessors. Never index a
property directly.

**A polygon dissolve, not a raster.** `dissolve.py` cancels edges that appear
twice and stitches the rest. Force fit used to draw a 600 m grid, which gave
serrated overlapping borders and put outlets under two patches at once.
Holes are classified **by containment, not by winding** — the KML has mixed
winding, and a winding test subtracts valid clockwise outer rings, which read
as 49% coverage on a rep whose true figure was 100%.

**Two drawer paths, and they look alike but are not.** `drawerTerritory`
opens for a DRAWN BORDER and is titled "Area profile · <mode> border · N km²".
`drawerDse` is the fallback when a rep is picked from the rail roster and no
border feature matches the code — titled "DSE profile · from your local file".
The usual cause of the second is that the territory filter moved after the
borders were built. Read the title before debugging an empty panel.

**Only `covered km²` and `% covered` need a polygon.** Desa area, masts and
people are facts about the DESA and come from `byDesaS`, which the browser
already holds. `drawerDse` used to dash all six measured columns, which made
a working panel look broken and made the two meaningful dashes meaningless.

## PJP review — "out of boundaries" (2026-08-27)

Drawer tab **PJP review**, beside Desa covered and Outlets. For the selected
rep it measures every outlet against **the gap to that rep's own nearest
outlet** — not to a centroid. A round is a chain of stops; what costs the day
is the gap you must cross to reach one.

- **urban 1.5 km · rural 4 km.** Beyond it the flag reads exactly
  `out of boundaries`, and a nearby rep is recommended to bind it to. The
  binding itself happens elsewhere; this page only recommends.
- **Urban/rural comes from desa population density, cut at 1,500 per km².**
  It cannot come from `ref_kelurahan.geo_type` — that column is **NULL on all
  7,761 rows**, so `force_fit.stratum_of` currently calls every desa in the
  circle rural. Density is read in the browser from `/api/samples/area-stats`,
  which already sends km² and population per desa, so this needed no new
  endpoint.
- The **flag is not gated on the 50-outlet threshold**; only the review
  headline is. Gating it would have shown nothing for DSEKEBAY168, the case
  the feature was built for, which carries 36 outlets.

Calibrated against the August workbook — 73,661 outlets with coordinates:

| desa density | median gap | p95 | over 1.5 km | over 4 km |
|---|---|---|---|---|
| under 500/km² | 0.14 km | 2.79 km | 12.0% | 2.5% |
| 500 – 1,500 | 0.13 km | 1.68 km | 6.0% | 1.1% |
| 1,500 – 5,000 | 0.13 km | 1.01 km | 2.6% | 0.6% |
| 5,000 and over | 0.08 km | 0.52 km | 0.9% | 0.2% |

Those thresholds flag **1,224 outlets, 1.66%** — a review list, not a flood.
Across the 653 reps carrying over 50 outlets: 443 flags, 357 with a receiver,
86 with no nearer rep within 20 km, median 2.31 km of detour removed per move.

**The whole-file version lives on the dashboard** — `rebScanAll()` runs the
same rule across every rep and fills the *Outlets out of boundaries* cell,
with a CSV carrying desa, kecamatan, city, microcluster, region, area, sales
area, supervisor and the recommended DSE. It shares `rebStratum()` and
`nearestOf()` with the per-rep tab rather than copying the rule, because two
copies of a threshold drift. It yields every 40 reps: 73,661 outlets is a few
million distance calculations and doing them in one go locks the tab.

**Above `REB_BAD_KM` (50 km) it is a bad coordinate, not a stray.** The circle
is about 350 km wide, so an outlet 352 km from its own nearest sibling is a
sign error or a misplaced decimal — one sits in Lampung, across the Sunda
Strait. Eleven rows qualify. Left in the main count they top the table, and a
summary whose three worst rows are obvious rubbish is a summary nobody
trusts, so they are counted apart and flagged `check the coordinate`. That is
the same family as the latitude −67 row, and the reason Map Workspace still
wants the home-bounds filter Polygon Samples already has.

## Outlet Code is not a key (2026-08-27)

Dashboard cell **Outlet identity check**. The finding it exists to state:

- **32 outlet codes appear under two DSE. All 32 are cross-brand collisions.**
  The two rows sit a median of **80 km** apart — nothing closer than 3.9 km,
  worst pair 223 km — under different names, in different branches. 3ID and
  IM3 number their outlets independently.
- **Same brand + same code under two reps: zero.** There is no double
  coverage in this file.
- `Brand + Outlet Code` is unique across all 73,699 rows, and so is `UNIKID`.
  Anything that joins, dedupes or counts on Outlet Code alone silently merges
  those 32 pairs.

The panel therefore splits four ways — one outlet two reps / both brands same
shop / same ID two shops / the same row twice — because a bare "32" sends
somebody hunting demarcation errors that do not exist. 500 m is the cut
between "one shop recorded twice" and "two shops sharing a number".

## Publishing to a shared server (2026-08-27)

The plan lives in the deployment artifact; these are the parts that constrain
the code.

**Phase 1 is done (2026-08-28).** The four gaps below are closed in code;
the numbered list is kept because it says what each change is defending
against, which the diff does not.

**Only boundaries may be hosted.** Desa, kecamatan, microcluster (and kabkot,
pending confirmation), plus per-desa population, area and site count from
`ref_metric`. Site locations and the DSE-to-outlet mapping are read from the
user's desktop and never leave the browser. Four gaps stand between that rule
and what would ship today:

1. `make_public_db.py` strips phone numbers and sample tables but **not the
   `outlet_dse` (73,659) or `site_locations` (15,919) layers**.
2. `data/mapcache/` is gitignored and travels by rsync, so it bypasses every
   check that reads the repo. It holds `outlet_dse.geojson.gz` at 4.0 MB, and
   `build_release.sh` excludes only the `*.points.*` variants.
3. `PUBLIC_MODE` blocks *writes*, not *pages*. Nine pages and ~60 API routes
   would answer. Map Workspace needs six endpoints. A `WORKSPACE_ONLY` posture
   is wanted, fail-closed in the same `before_request`.
4. Dropping the mast layer **silently zeroes the site count in every tooltip** —
   `/api/samples/area-stats` computes it by point-in-polygon over
   `site_locations`. Read `sites_all` from `ref_metric` instead.

How each was closed:

1. `make_public_db.DROP_LAYERS` / `DROP_METRICS` and `drop_private()`;
   `check()` now refuses a copy holding either layer and prints what
   survived, so a clean result reads as a fact rather than an absence.
2. `build_release.sh` excludes `outlet_dse*` and `site_locations*` from the
   map cache **by layer name**, and refuses to package a tree containing
   either. Both `rsync` commands in DEPLOY.md carry the same exclusions plus
   `--delete`, so a layer excluded today is also removed from a server an
   earlier build already put it on.
3. **`PUBLIC_WRITE_ALLOW` must list `/api/samples/area-profile` as well as
   `/api/samples/model.geojson`.** Both POST a geometry this server drew and
   keep nothing; the write guard refused the first one, and the symptom was
   not an error — the ledger under the map read "Nothing to show yet" for
   every border clicked on a public instance, with the tiles beside it
   correctly filled from the model. A refusal that looks like an empty
   result is the worst kind. The refusal now names the path and the table
   prints it instead of an empty state.
   `config.WORKSPACE_ONLY` + `config.WORKSPACE_READS` (ten paths,
   enumerated) + `app._workspace_only_guard`, which runs **after** the write
   guard and returns **404, not 403** — a refusal still confirms a path
   exists. `MENUS` narrows to the one page, because a nav offering seven
   pages of which six 404 reads as a broken application rather than a
   deliberately narrow one.
4. `configuration._desa_sites()` reads `sites_all` from `ref_metric`,
   matching all 7,761 desa and totalling 15,921 against 15,919 masts — the
   two extra being masts the geometry pass could not place. `_sites_in()` is
   gone. **The meaning changed and the response says so:** `sites` for a
   drawn border is now the masts in the *desa the border covers*, and the
   payload carries `sites_basis` so the page can label it rather than imply
   a precision the data no longer supports.

**Site locations are a local file now, with their own reader (2026-08-28).**
`SITE_FIELDS` reads `SITE ID / Site Name / Long / Lat / Site Type`, and it is
a SEPARATE vocabulary from the outlet `FIELDS` on purpose: "Site Name" would
match the outlet reader's `name` list and "Site Type" its `category` list, so
a site file read through the outlet vocabulary is not rejected — it is
quietly mis-read, which is worse. Only lat/lon are required (`SITE_NEED`).

`SITES` is its own top-level variable, not a property of `LOCAL`, so a site
file can be loaded **without** an outlet workbook and there is never a second
copy to disagree with. Dots are coloured by site type, with hues assigned in
order of first appearance and a legend under the rail button — hard-coding
"2G / 4G / 5G" would be a list somebody has to keep in step with the network.

Two bugs found while doing it, both worth remembering:

- **A site file only reached `readSites` when the outlet reader THREW.** A
  file that merely produced no outlet rows was reported "no usable rows" and
  never offered to the reader that wanted it. Try sites whenever outlets
  yield nothing, not only on an exception.
- The tooltip now shows the cloud reference count for the desa **and** how
  many of your own sites fall in it. Different questions; both are shown
  rather than one replacing the other.

**The cloud Site locations layer is gone from Map Workspace.** Site files are
local and optional, like the outlet workbook. Watch for dangling references
when removing a layer: `reorder()` still called `front(siteLayer)`, which
`node --check` passes and which throws a ReferenceError under `"use strict"`
at the first redraw.

**The Hetzner box is shared** — Postpaid, Frontliner and Merchandiser run on
it, and port 80 already answers. This application arrives as a tenant: add
files, take free numbers, edit nothing anyone else depends on. Never claim
nginx `default_server` (on a bare IP that is what answers, so taking it
redirects another app's traffic); `reload` nginx, never `restart`;
`certbot certonly --webroot`, never `--nginx`, which rewrites every server
block it finds. `deploy/server_survey.sh` is read-only and supplies the port,
unit name and user — run it before writing the unit file.

## Verifying a change to the browser code

`static/js/map_workspace.js` has no test suite and a syntax error in it fails
silently — the IIFE simply never runs and the page renders empty chrome.

```bash
node --check static/js/map_workspace.js     # after every edit, without exception
```

For anything that computes over the workbook — the PJP review, the identity
check — replay the same rule in Python against
`Data Upload/DSE PJP into Desa -JAYA vShare.xlsx` and compare counts. That is
how both features above were verified; it is stronger than clicking, and it
works when the app is not running.

## Numbers worth remembering — the full JAYA circle load

The figures under "The four slots" are the INNER JAKARTA slice. Loading the
whole circle from `Data Upload/` gives:

| | |
|---|---|
| desa / kelurahan | 7,761 |
| kecamatan polygons | 721 features, **719 distinct names** |
| microclusters | 825 |
| kota / kabupaten | 41 |
| site locations | 15,919 |
| outlet_dse features | 73,659 |
| workbook rows | 73,699 · 73,661 with usable coordinates |
| DSE CODE | 1,501 |
| UNIKDSE — actual people | **1,347** · 154 carry both 3ID and IM3 |
| distinct outlet codes | 73,667 |
| `ref_metric` | 85,371 rows, all `entity_type='desa'` |

**36 kecamatan named on desa rows have no polygon in the kecamatan layer** —
754 names on the desa rows against 719 in the layer, TANAH ABANG among the
missing. Any kecamatan-level rollup taken from polygons under-counts those.
`ref_kecamatan` has 825 rows and does not share the gap, which is why the
microcluster totals are rolled up through it server-side rather than summed
from the desa labels.

Two data findings still open, both flagged and neither fixed:

- **Cikampek and Kota Baru are filed under INNER JAKARTA → INNER JAKARTA EAST
  → NORTH KARAWANG** in `ref_kecamatan`, not West Java. Probably wrong.
- An outlet carries **latitude −67.0661**. Polygon Samples has a home-bounds
  filter; Map Workspace does not.

## Maps are served from disk, not built per request (2026-08-26)

`mapcache.py` writes each boundary layer once into a simplified, rounded,
gzipped GeoJSON under `data/mapcache/`. `/api/territory/<layer>.geojson`
checks `is_fresh()` and streams the file; the 7,761-round-trip rebuild only
happens when the stamp is stale. Consequences worth holding:

- **Nothing may compute from a mapcache file.** Simplification is
  per-polygon, not topological, so neighbours can part by up to the
  tolerance (11 m for desa). Areas, containment and every figure come from
  `geo_feature`. The files are for drawing.
- **Any code path that changes a layer must `mapcache.drop()` it** —
  `configuration.py`, `purge_offmap.py` and `prebuild_maps.py` all do. A
  layer re-imported without a drop keeps serving the old shapes and the page
  looks like the upload silently failed.
- After changing *how* the files are built (tolerance, precision, the props
  attached by `territory_api._bulk_props`), run `prebuild_maps.py` — nothing
  invalidates on a code change, only on a data change. It also warms the
  roll-up cache (`base_rows.json.gz`, `desa_placement.json.gz`), which is the
  half-minute the Preview Polygon page otherwise pays on each restart.

## Polygon Samples is a second store, not a layer (samples.py)

Somebody else's demarcation, drawn *on top of* the application's layers and
never in place of them: its own tables (`sample_set`, `sample_outlet`), its
own upload, its own delete. Deleting every sample cannot touch a permanent
layer, and a sample cannot overwrite one. Keep that separation — it is the
reason a branch's working file can be looked at at all.

- Name, branch and region are required **before** the file is read. On a
  shared instance "whose file is this?" is the first question asked of an
  overlay, so it is data, not metadata offered afterwards.
- `config.sample_store_allowed()` is where the reverse-proxy trap is handled:
  behind nginx every visitor arrives from `127.0.0.1`, so a "localhost only"
  test on `remote_addr` says yes to the internet exactly when it must say no.
  `PUBLIC_MODE` therefore decides first and the address test never runs there.
  `SAMPLE_STORE` is `local` (default) / `on` / `off`.
- `make_public_db.DROP_TABLES` drops both sample tables. Samples do not travel.

## Big KML, and the registry that says what a field is for

- **`kmlstream.py` walks a KML placemark at a time** with `iterparse` +
  `clear()`. `import_local.parse_kml_file` builds a DOM of the whole
  document, which is fine at 1 MB and fatal at the 815 MB national desa
  export — the process dies and it reads as a hung upload. New readers for
  large exports go through `kmlstream`, not `ET.fromstring`. It also treats
  the literal string `NULL` (and `#N/A`, `-`, `N/A`) as blank, once, so no
  consumer downstream ever sees a place called NULL.
- **`kmllayers.LAYERS` is one registry with three consumers** — the popup
  card (`preview`, five fields is the budget), the filter list (`filters`,
  low-cardinality classifications only) and `derive` (computed on import and
  stored beside the real fields so it filters and colours like the rest).
  Add a field in one place; a field added to a template alone appears in the
  popup and nowhere else. Layers are matched **on their fields, not their
  file names** — same rule as the boundary KMZs.

## Adding a route, on an application that is published

Two `before_request` guards in `app.py`, in this order, both fail-closed:

1. `_public_mode_guard` — under `PUBLIC_MODE`, anything that is not GET /
   HEAD / OPTIONS is refused 403 unless its path is in
   `app.PUBLIC_WRITE_ALLOW` (two paths, both compute-and-forget).
2. `_workspace_only_guard` — under `WORKSPACE_ONLY`, anything not in
   `config.WORKSPACE_READS` (or `/static/`) returns **404, not 403**.

So a new endpoint is invisible on a published instance until somebody adds
it on purpose, which is the intent — do not "fix" a 404 on a public box by
loosening a guard. `WORKSPACE_READS` is **enumerated, never prefixed**: a
prefix like `/api/territory/` would also admit `poi.geojson` and
`service-points.geojson`, which is how an allowlist becomes a denylist. A new
page also needs an entry in `territory_api.MENUS`, and `MENUS` is filtered
down to the workspace entry when `WORKSPACE_ONLY` is set.

## Environment (`.env`, git-ignored; `deploy/env.public.example` for a server)

`PUBLIC_MODE`, `WORKSPACE_ONLY`, `SECRET_KEY` (`app.py` refuses to start a
`PUBLIC_MODE` instance whose key is the placeholder or under 24 characters —
`config.secret_key_ok()`),
`SAMPLE_STORE` / `SAMPLE_PIN` / `SAMPLE_MAX_ROWS` / `SAMPLE_MODEL_MAX`,
`MAX_UPLOAD_MB` (1024 by default — these workbooks are large),
`HOME_MINLON`/`MINLAT`/`MAXLON`/`MAXLAT` (what `config.on_map()` and
`purge_offmap.py` test against), `DB_PATH`, `BEHIND_PROXY` (defaults ON
under `PUBLIC_MODE`, so `ProxyFix` trusts one hop of `X-Forwarded-*`),
`BASEMAP_PROVIDER` / `CARTO_API_KEY` (tile source; `gray` by default), and
the gunicorn knobs `BIND` / `WEB_WORKERS` / `WEB_TIMEOUT` / `LOG_LEVEL`. `GOOGLE_API_KEY` belongs to the
inherited POI pullers and must not exist on a published host.

## The server as it actually landed (2026-08-28)

`DEPLOY.md` is the procedure; these are the settled facts a change has to
respect, and the reasons are in the files themselves — `deploy/gunicorn.conf.py`
and `deploy/pjp-dse.service` are written as arguments, not as configuration.

| | |
|---|---|
| host | shared Hetzner box, 2 cores · 3.7 GB · eight other applications |
| runs as | systemd `pjp-dse`, user `pjp`, `WorkingDirectory=/opt/pjp-dse` |
| server | **gunicorn**, `deploy/gunicorn.conf.py`, `127.0.0.1:8096` |
| workers | **2 sync**, fixed — not derived from cores; `max_requests=400` |
| ceiling | `MemoryHigh=450M` / `MemoryMax=600M`, `CPUQuota=50%` |
| nginx | `pjp.gamextopia.id` → upstream `pjp_dse`; never `default_server` |
| code | `git pull` from `origin` via `deploy/update.sh` |
| data | `rsync` from the Mac, separately — git never carries `data/` |

- **`app.run` is the laptop's server only.** `./start.sh` (port 5002, debug on)
  is for development; nothing on the box should reach it. Port numbers differ
  by design — 5002 local, 8096 behind nginx — so any health check, proxy pass
  or firewall rule has to say which one it means.
- **The unit runs under `ProtectSystem=strict` with `ReadWritePaths=/opt/pjp-dse/data`.**
  Everything else is read-only at runtime, which is why `config.py` *notes*
  an unwritable `exports/` and carries on instead of dying at import. Any new
  module that creates a directory at import time must do the same, or the
  published instance stops booting over a directory it will never use.
- **A code deploy must not be able to touch the data, and a data refresh must
  not be able to ship half-finished code.** That is why they travel by
  different routes; keep them apart.

Two things in `deploy/` still point at the pre-8096 shape and will bite on
the server — neither is a design decision, both are leftovers:

- `deploy/update.sh:52` health-checks `localhost:5002`. Under systemd the app
  is on 8096, so the check fails and, under `set -e`, a successful update
  exits non-zero right after printing `up`.
- `deploy/update.sh` defaults `BRANCH=main`; this repository's branch is
  **`master`** (`origin` = `git@github.com:shatya-fram/new-polygon-pjp-dse.git`),
  so `git fetch origin main` fails before anything else runs.

`DEPLOY.md` §4 and §9 quote 5002 in the same way — read them as "the app's
port", and check `deploy/gunicorn.conf.py` for what it actually is.

## UNIKDSE is the grouping key (2026-09-06)

Asked for on 6 September: *"use UNIKDSE for the grouping of polygon as final
grouping instead of the DSE ID, DSECODE, DSE."* Everything that groups the
workbook now groups on the **person**, not on the code a brand gave them.

A demarkasi export lists 3ID and IM3 separately and gives the same rep a
different DSE CODE under each — `CVSCJU013` on one side, `1-163544572541`
on the other. Grouping on the code drew that rep as **two territories**.
In the August Jaya file:

| | |
|---|---|
| distinct DSE CODE | 1,501 |
| distinct UNIKDSE | **1,347** |
| people carrying two codes | 154 (one 3ID, one IM3 — never more) |
| DSE CODE mapping to two UNIKDSE | **0** — the merge is clean, many-to-one |

`readOutlets` now writes the person into **`r.dse`** and keeps the brand code
in **`r.dsecode`**. Nothing downstream had to change: the roster, the DSE
filter, the border build (`points: [r.dse, lat, lon]`), the PJP review, the
boundary scan, every profile and every export were already keyed on `r.dse`,
so they all regrouped at once. `repKey()` is now simply `r.dse`.

**`UNIKID` was in the `unikdse` alias list and is gone from it.** `UNIKID` is
`Brand + Outlet Code` — **73,699 distinct values, one per row**. While the
field was decoration that alias cost nothing; as the grouping key it would
have made one "rep" per outlet and drawn 73,699 territories the size of a
dot. A file with no UNIKDSE column but a UNIKID one is exactly the case that
would have hit it.

**The two key columns are resolved by name, not by header order.**
`resolveKeys()` runs after the generic header matcher. The matcher walks
columns left to right and gives each to the first field that claims it, so a
file with UNIKDSE standing left of DSE CODE would have handed the code slot
to the person column and left the brand code unread. With no code column at
all, the person column satisfies `NEED` on its own.

**The guard.** A person key merges codes; it cannot produce more groups than
the codes it merges. If it does, that column is not a person — `readOutlets`
falls back to the brand code, sets `LOCAL.basis = "code"`, and every panel
that shows a rep count says which key it counted (`keyLabel()`). Same
fallback when the column is absent or blank on every row.

What moves, measured by replaying the rule in Python over the same 73,699
rows (`urban 1.5 km · rural 4 km`, nearest **sibling**, >50 km set aside):

| grouped by | groups | flagged | urban | rural | groups affected |
|---|---|---|---|---|---|
| DSE CODE | 1,501 | 1,214 | 943 | 271 | 647 |
| **UNIKDSE** | **1,347** | **1,133** | 869 | 264 | 599 |

**81 flags disappear** because a rep's 3ID outlet is now a sibling of their
own IM3 outlet — the gap was never a real detour, only an artefact of the
two brands being counted as two people. (Replay figures: the app measures
inside the current slice and joins density its own way, so treat these as
the size of the move, not as the panel's exact output.)

**The identity check is untouched: still 32.** Every one of the 32 outlet
codes under two groups is two *different* people, so merging brands does not
absorb a single one.

## When UNIKDSE holds a rayon (2026-09-06)

`Recap Polygon SA Inner 6 Sept.xlsx` — 13,487 rows, no `DSE CODE` column at
all — has **UNIKDSE filled in by hand on 56% of its rows, with the rayon**:
`R1`, `R2`, `R 4 DT`, `R 13 DB`, `RAYON 1` … `RAYON 32`, plus `0` and `#N/A`
from a failed lookup. Grouped on that, `R1` is one "DSE" holding **224
outlets across two branches and five microclusters, 16.8 km corner to
corner**, mixing IM3 (191) and 3ID (33). A real rep in the same file holds a
median of 48 within a few km.

| what UNIKDSE holds | values | rows | outlets/group median · max |
|---|---|---|---|
| a rep code (`DSE…`, `CSO…`, `1-…`, `2613…`) | 109 | 5,970 | 48 · 133 |
| a rayon label | 72 | 7,472 | 102 · 224 |
| `0` / `#N/A` | 2 | 45 | — |

**The rep is recoverable from the same file, and it is not KETERANGAN.**
`KETERANGAN` looked promising — filled on 3,528 of the broken rows — but its
58 values never appear as a UNIKDSE anywhere, and they are `CSOBKSS-05`
shaped: the CSO, a supervisor. **`Hybrid Pairing DSE Code` is the rep.** On a
hybrid outlet the paired rep is the same human, 41 of its values also appear
as a UNIKDSE on other rows in the file, and the geometry settles it:

| rayon rows keyed by | groups | outlets/group median | spread km median · p90 · max |
|---|---|---|---|
| the rayon label | 74 | 102 | 4.6 · 14.5 · 29.2 |
| KETERANGAN | 58 | 61 | 5.3 · 9.5 · 15.3 |
| **pairing DSE code** | **93** | **54** | **4.1 · 5.6 · 7.1** |

So `readOutlets` resolves the key in this order — **UNIKDSE · pairing DSE
code (brand suffix dropped) · brand code · the label, tagged**:

- `personOf()` rejects a value that is empty, junk (`0`, `#N/A`, `-`,
  `NULL`, `#REF!`) or matches `/^(R|RAYON)\s*\d+(?:[\s\-]*[A-Z]{1,3})?$/i`.
  That pattern is deliberately narrow: `DSEKRWG06`, `2613051097` and
  `1-28825212763` all pass it.
- `dropBrand()` turns `26130531273ID` into `2613053127`, so the two
  spellings of one rep land in the same group.
- A row that answers none of them keeps its label under a **`RAYON · `
  prefix** — visible in the roster, the polygon, the profile and both CSVs,
  so it can never be read as a person. All 2,601 such rows are NON HYBRID,
  which is why no pairing exists for them: nothing in this file names their
  rep, and only a corrected export can.

Every panel says where the keys came from (`keyNote()`), and both exports
carry a **Key from** column.

Result on the recap file — verified by replaying the same regexes in node
over all 13,487 rows, and confirmed to change nothing on the August master:

| | recap, before | recap, after | August master |
|---|---|---|---|
| groups | 183 | **231** | 1,347 (unchanged) |
| outlets/group median | 74 | **54** | 53 |
| largest group | **224** | 134 | 149 |
| keys from UNIKDSE / pairing / rayon | — | 5,970 / 4,916 / 2,601 | 73,699 / 0 / 0 |

**The old `groups > codes` guard is gone.** It assumed a person key only ever
merges codes; repairing a rayon row SPLITS one label into the several reps
under it, so more groups than raw values is now the wanted outcome. What
replaced it catches the case that actually matters — a column that
identifies the outlet rather than the rep (`UNIKID`, one value per row):
if the key produces close to one group per row, it is not a person.
