"""
Offline verification. No API key, no S3, no network, nothing billed.

    python tests/test_pulls.py

Covers: schema migration (including re-run idempotence), upsert semantics,
brand-resolver ordering, Overture target matching, the Overpass query
builder and element parser, the Google tile planner, and every Flask route.
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

TMP = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(TMP, "test.db")
os.environ["GOOGLE_API_KEY"] = ""

import duckdb  # noqa: E402

import config  # noqa: E402
import db  # noqa: E402
import migrate  # noqa: E402
import pull_overture as po  # noqa: E402

config.DB_PATH = os.environ["DB_PATH"]

PASSED = FAILED = 0


def check(label, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
    else:
        FAILED += 1
    print(f"  [{'PASS' if cond else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))
    return bool(cond)


def section(t):
    print(f"\n{t}")


# ── schema ───────────────────────────────────────────────────────────────
section("migration")
migrate.main()
con = db.connect()
tables = {r["name"] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table'")}
check("poi_overture created", "poi_overture" in tables)
check("poi_google created", "poi_google" in tables)
check("pull_run created", "pull_run" in tables)
check("the two POI tables are separate objects",
      "poi_overture" in tables and "poi_google" in tables
      and "poi" not in tables)
con.close()

migrate.main()   # must be safe to re-run
con = db.connect()
check("migrate.py is idempotent (re-run adds nothing)",
      len({r["name"] for r in con.execute(
          "SELECT name FROM sqlite_master WHERE type='table'")}) == len(tables))

# ── upsert ───────────────────────────────────────────────────────────────
section("upsert semantics")
run1 = db.start_run(con, "overture", {"t": "test"})
rows = [
    {"source_id": "ovt-1", "source": "overture", "name": "Alfamart A",
     "brand_resolved": "Alfamart", "category": "convenience_store",
     "confidence": 0.9, "lat": -6.2, "lon": 106.8},
    {"source_id": "ovt-2", "source": "overture", "name": "Indomaret B",
     "brand_resolved": "Indomaret", "category": "convenience_store",
     "confidence": 0.8, "lat": -6.3, "lon": 106.9},
]
db.upsert(con, "poi_overture", rows, run1)
check("2 rows inserted", db.counts(con)["poi_overture"] == 2)
first_seen = con.execute(
    "SELECT first_seen_utc FROM poi_overture WHERE source_id='ovt-1'"
).fetchone()[0]

run2 = db.start_run(con, "overture", {"t": "test2"})
rows[0]["name"] = "Alfamart A (renamed)"
db.upsert(con, "poi_overture", rows, run2)
check("re-pull does not duplicate", db.counts(con)["poi_overture"] == 2)
r = con.execute("SELECT * FROM poi_overture WHERE source_id='ovt-1'").fetchone()
check("changed field is updated", r["name"] == "Alfamart A (renamed)")
check("first_seen_utc is preserved", r["first_seen_utc"] == first_seen)
check("pull_run_id advances", r["pull_run_id"] == run2)

db.finish_run(con, run2, "ok", rows_written=2, api_calls=0, est_cost_usd=0.0)
check("run recorded as ok", db.runs(con)[0]["status"] == "ok")
check("Overture run costs nothing", db.runs(con)[0]["est_cost_usd"] == 0.0)

# ── the two connections stay apart ───────────────────────────────────────
section("connection isolation")
rung = db.start_run(con, "google", {"t": "g"})
db.upsert(con, "poi_google", [
    {"source_id": "ChIJ-1", "source": "google", "name": "Alfamart A",
     "brand_resolved": "Alfamart", "category": "convenience_store",
     "lat": -6.2, "lon": 106.8, "brand_verified": 1}], rung)
c = db.counts(con)
check("google row lands only in poi_google",
      c["poi_google"] == 1 and c["poi_overture"] == 2)
check("same real-world POI keeps a separate row per source",
      con.execute("SELECT count(*) FROM poi_overture WHERE name LIKE 'Alfamart%'"
                  ).fetchone()[0] == 1
      and con.execute("SELECT count(*) FROM poi_google WHERE name LIKE 'Alfamart%'"
                      ).fetchone()[0] == 1)
check("no table references IM3 / 3ID",
      not any("im3" in t.lower() or "3id" in t.lower() for t in tables))

# ── brand resolver ───────────────────────────────────────────────────────
section("brand resolver ordering")
d = duckdb.connect()
names = ["Alfamart Kebon Jeruk", "Alfamidi Bekasi", "Indomaret Point",
         "Bank BCA KCP", "Bank Mandiri KC", "Bank Syariah Indonesia KCP",
         "Agen BRILink Pak Budi", "Bank BRI Unit", "Warung Bu Ani"]
d.execute("CREATE TABLE t (name VARCHAR, brand_name VARCHAR, brand_qid VARCHAR)")
d.executemany("INSERT INTO t VALUES (?,NULL,NULL)", [(n,) for n in names])
lab = dict(d.execute(f"SELECT name, {po.brand_case()} FROM t").fetchall())
check("BSI not labelled Mandiri",
      lab["Bank Syariah Indonesia KCP"] == "BSI (Bank Syariah Indonesia)",
      str(lab["Bank Syariah Indonesia KCP"]))
check("BRILink not labelled BRI",
      lab["Agen BRILink Pak Budi"] == "Agen BRILink")
check("plain BRI still resolves", lab["Bank BRI Unit"] == "BRI")
check("Alfamidi not labelled Alfamart", lab["Alfamidi Bekasi"] == "Alfamidi")
check("Alfamart resolves", lab["Alfamart Kebon Jeruk"] == "Alfamart")
check("unbranded stays null", lab["Warung Bu Ani"] is None)
d.close()

# ── Google tile planner (no calls made) ──────────────────────────────────
section("Google tile planner")
import pull_google as pg  # noqa: E402

tiles = pg.tiles_for_aoi(config.AOI, 2.0)
check("AOI is fully covered by tiles", len(tiles) > 100, f"{len(tiles):,} tiles")
lats = [t[0] for t in tiles]
lons = [t[1] for t in tiles]
check("tiles span the AOI latitude range",
      min(lats) <= config.AOI["minlat"] + 0.02
      and max(lats) >= config.AOI["maxlat"] - 0.02)
check("tiles span the AOI longitude range",
      min(lons) <= config.AOI["minlon"] + 0.02
      and max(lons) >= config.AOI["maxlon"] - 0.02)
check("radius covers the tile half-diagonal (no gaps)",
      abs(tiles[0][2] - (2.0 * 1000 * (2 ** 0.5) / 2)) < 1,
      f"{tiles[0][2]:.0f} m")
check("bigger tiles mean fewer of them",
      len(pg.tiles_for_aoi(config.AOI, 5.0)) < len(tiles))
check("google connection reports itself unavailable without a key",
      not __import__("providers.google_places", fromlist=["x"]).available())

# ── Flask routes ─────────────────────────────────────────────────────────
section("web UI routes")
con.close()
import app as webapp  # noqa: E402

cl = webapp.app.test_client()
check("/healthz ok", cl.get("/healthz").get_json()["ok"] is True)
r = cl.get("/")
check("/ renders", r.status_code == 200 and len(r.data) > 1000)
j = cl.get("/api/rows?source=overture").get_json()
check("/api/rows returns overture rows", j["ok"] and j["count"] == 2)
j = cl.get("/api/rows?source=google").get_json()
check("/api/rows returns google rows separately", j["ok"] and j["count"] == 1)
j = cl.get("/api/rows?source=overture&brand=Indomaret").get_json()
check("brand filter works", j["count"] == 1)
j = cl.get("/api/facets?source=overture").get_json()
check("facets list only values present in the table",
      set(j["brands"]) == {"Alfamart", "Indomaret"}, str(j["brands"]))
j = cl.get("/api/runs").get_json()
check("/api/runs lists both connections",
      {x["source"] for x in j["runs"]} == {"overture", "google"})
csv_body = cl.get("/api/export?source=overture").data.decode()
check("/api/export streams CSV with a header",
      csv_body.startswith("source_id") and csv_body.count("\n") >= 3)
check("/api/rows rejects an unknown source",
      cl.get("/api/rows?source=nope").status_code == 400)

# The web-routes section closes `con` so Flask opens its own handle against
# the same file. Every section after it needs a live one again — without this
# the suite dies on a closed database before the OSM checks ever run.
con = db.connect()

# ── target matching: name rescue + vetoes ────────────────────────────────
# These guard the two failure modes that actually bit: a mandatory brand
# filter emptying a whole category, and a name rescue that happily swallows
# "Bank Sampah" (a waste depot) as a bank branch.
section("Overture target matching")
d = duckdb.connect()
d.execute("CREATE TABLE t (name VARCHAR, category VARCHAR, basic_category VARCHAR)")


def matches(target_key, names):
    """Return the subset of `names` that target_key would keep on name alone."""
    t = po.TARGETS[target_key]
    clause = po.name_clause(t)
    if clause is None:
        return set()
    d.execute("DELETE FROM t")
    d.executemany("INSERT INTO t VALUES (?,NULL,NULL)", [(n,) for n in names])
    return {r[0] for r in d.execute(f"SELECT name FROM t WHERE {clause}").fetchall()}


rail = matches("rail_stations", [
    "Stasiun Manggarai", "MRT Lebak Bulus Grab", "LRT Velodrome",
    "Stasiun KRL Depok Baru", "Stasiun Pengisian Bahan Bakar Umum",
    "Warung Kopi Stasiun", "Apotek K24"])
check("rail rescue finds Stasiun / MRT / LRT / KRL by name",
      {"Stasiun Manggarai", "MRT Lebak Bulus Grab", "LRT Velodrome",
       "Stasiun KRL Depok Baru"} <= rail, str(sorted(rail)))
check("rail rescue vetoes SPBU (Stasiun Pengisian)",
      "Stasiun Pengisian Bahan Bakar Umum" not in rail)
check("rail rescue ignores unrelated POIs", "Apotek K24" not in rail)

bus = matches("bus_terminals", [
    "Terminal Kampung Rambutan", "Halte Transjakarta Harmoni",
    "Terminal 3 Bandara Soekarno-Hatta", "Terminal Peti Kemas Koja",
    "Toko Bangunan Jaya"])
check("bus rescue finds terminals and halte",
      {"Terminal Kampung Rambutan", "Halte Transjakarta Harmoni"} <= bus,
      str(sorted(bus)))
check("bus rescue vetoes airport and container terminals",
      "Terminal 3 Bandara Soekarno-Hatta" not in bus
      and "Terminal Peti Kemas Koja" not in bus)

bank = matches("bank_branches", [
    "Bank BCA KCP Sudirman", "Bank Mandiri Kantor Kas Cikini",
    "Bank Sampah Melati", "Bank Darah PMI", "ATM BNI Blok M",
    "Rumah Makan Padang"])
check("bank rescue finds branches by name",
      {"Bank BCA KCP Sudirman", "Bank Mandiri Kantor Kas Cikini"} <= bank,
      str(sorted(bank)))
check("bank rescue vetoes Bank Sampah / Bank Darah",
      "Bank Sampah Melati" not in bank and "Bank Darah PMI" not in bank)
check("bank rescue does not swallow ATMs", "ATM BNI Blok M" not in bank)

atm = matches("atms", ["ATM BCA Center", "Anjungan Tunai Mandiri Grogol",
                       "Universitas Atma Jaya", "Atmosphere Cafe"])
check("ATM rescue finds ATMs", {"ATM BCA Center"} <= atm, str(sorted(atm)))
check("ATM rescue does not match Atma Jaya / Atmosphere",
      "Universitas Atma Jaya" not in atm and "Atmosphere Cafe" not in atm)
d.close()

# The bug this guards: requiring a brand match on banks/ATMs emptied the
# category outright, because Indonesian Overture rows carry no brand.wikidata
# and usually no brand.names.primary either.
check("banks and ATMs label brands, never require them",
      po.TARGETS["bank_branches"]["brand_required"] is False
      and po.TARGETS["atms"]["brand_required"] is False)
check("minimarket targets still require their brand",
      all(po.TARGETS[k]["brand_required"] for k in
          ("alfamart", "indomaret", "alfamidi")))
check("infrastructure targets are pinned to full recall",
      all(po.TARGETS[k]["min_conf"] == 0.0 for k in
          ("rail_stations", "bus_terminals", "bank_branches", "atms")))

_t, _where, _mc = po.target_where("bank_branches", 0.75)
check("per-target confidence pin beats the global default", _mc == 0.0)
_t, _where, _mc = po.target_where("bank_branches", 0.75, force_conf=True)
check("--min-confidence overrides the pin", _mc == 0.75)
check("category and name are ORed, not ANDed",
      " OR " in _where[0] and "regexp_matches" in _where[0])

# ── connection 3: OSM / Overpass (no network touched) ────────────────────
# Every assertion here runs against a hand-built element list. If any of
# this ever starts needing the internet, the test is wrong, not the network.
section("OSM / Overpass connection")
import pull_osm as pos  # noqa: E402
from providers import osm_overpass as osm  # noqa: E402

q = osm.build_query(pos.TARGETS["rail_stations"]["selectors"], config.AOI)
check("query carries the AOI bbox in Overpass order (S,W,N,E)",
      f"[bbox:{config.AOI['minlat']},{config.AOI['minlon']},"
      f"{config.AOI['maxlat']},{config.AOI['maxlon']}]" in q, q.splitlines()[0])
check("query asks for JSON and centres for ways/relations",
      "[out:json]" in q and "out meta center;" in q)
check("every selector is queried as nwr",
      q.count("nwr[") == len(pos.TARGETS["rail_stations"]["selectors"]))
q2 = osm.build_query(['["amenity"="bank"]'], config.AOI, use_nwr=False)
check("--no-nwr expands to node/way/relation",
      q2.count("node[") == 1 and q2.count("way[") == 1
      and q2.count("relation[") == 1)
check("every OSM target declares at least one selector",
      all(t["selectors"] for t in pos.TARGETS.values()))

ELEMENTS = [
    {"type": "node", "id": 1, "lat": -6.2, "lon": 106.8,
     "timestamp": "2026-01-02T03:04:05Z", "version": 7,
     "tags": {"railway": "station", "name": "Stasiun Manggarai",
              "operator": "KAI Commuter", "public_transport": "station"}},
    {"type": "way", "id": 2, "center": {"lat": -6.3, "lon": 106.9},
     "tags": {"shop": "convenience", "name": "Alfamart Cikini",
              "brand": "Alfamart", "brand:wikidata": "Q23745600"}},
    {"type": "node", "id": 3, "lat": -6.1, "lon": 106.7,
     "tags": {"amenity": "atm", "operator": "Bank Central Asia"}},
    # A relation with no centre has no usable point and must be dropped,
    # not written as a null-geometry row.
    {"type": "relation", "id": 4, "tags": {"amenity": "bank"}},
]
rows, dropped = osm.elements_to_rows(ELEMENTS, "rail_stations")
by_id = {r["source_id"]: r for r in rows}
check("elements without a point are dropped, not written",
      dropped == 1 and len(rows) == 3, f"dropped={dropped}")
check("source_id is OSM type/id", "node/1" in by_id and "way/2" in by_id)
check("a way is placed by its centre",
      by_id["way/2"]["lat"] == -6.3 and by_id["way/2"]["lon"] == 106.9)
check("category comes from the defining tag",
      by_id["node/1"]["category"] == "railway=station",
      by_id["node/1"]["category"])
check("brand:wikidata resolves the brand by identity, not string luck",
      by_id["way/2"]["brand_resolved"] == "Alfamart")
check("brand falls back to operator when there is no name",
      by_id["node/3"]["brand_resolved"] == "BCA",
      str(by_id["node/3"]["brand_resolved"]))
check("raw tags are kept verbatim",
      "KAI Commuter" in by_id["node/1"]["tags_json"])
check("OSM edit timestamp is carried through",
      by_id["node/1"]["source_updated"] == "2026-01-02T03:04:05Z")

rung3 = db.start_run(con, "osm", {"targets": ["rail_stations"]})
n = db.upsert(con, "poi_osm", rows, rung3)
check("OSM rows land in poi_osm", n == 3)
c = db.counts(con)
check("an OSM pull leaves the other two tables untouched",
      c["poi_osm"] == 3 and c["poi_overture"] == 2 and c["poi_google"] == 1,
      str(c))
db.upsert(con, "poi_osm", rows, rung3)
check("re-pulling the same elements does not duplicate",
      db.counts(con)["poi_osm"] == 3)
check("OSM is free — the run records no cost",
      con.execute("SELECT est_cost_usd FROM pull_run WHERE source='osm'"
                  ).fetchone()[0] in (0, 0.0, None))
check("attribution string is available for exports",
      "OpenStreetMap" in osm.ATTRIBUTION and "ODbL" in osm.ATTRIBUTION)
check("more than one Overpass mirror is configured",
      len(osm.ENDPOINTS) >= 2)

# ── Overture precision guards (driven by the real Jabodetabek slice) ─────
section("Overture precision")
# matches() below needs the DuckDB scratch table the target-matching section
# built and then closed. Reopen it here rather than leaving the last section
# of the suite unreachable.
d = duckdb.connect()
d.execute("CREATE TABLE t (name VARCHAR, category VARCHAR, basic_category VARCHAR)")
_bank = po.TARGETS["bank_branches"]
check("financial_service is not treated as a bank category",
      "financial_service" not in _bank["cats"])
check("the real Overture bank codes are covered",
      {"bank_or_credit_union", "bank"} <= set(_bank["cats"]))
check("generic 'kantor cabang' no longer counts as a bank",
      "kantor cabang" not in _bank["name_any"])
_noise = matches("bank_branches", [
    "BPJS Ketenagakerjaan Kantor Cabang Cilincing",
    "Kantor Pajak Jakarta Tanah Abang Tiga",
    "Asuransi Syariah Bumiputera Indonesia",
    "Bank BTN Rawamangun"])
check("BPJS / tax / insurance offices no longer match as banks",
      _noise == {"Bank BTN Rawamangun"}, str(sorted(_noise)))
_prop = matches("rail_stations", [
    "Stasiun MRT Bendungan Hilir", "Depo MRT Lebak Bulus",
    "Proyek MRT Fatmawati", "LRT City Sentul",
    "Apartment LRT Gateway Park", "Stasiun LRT Palmerah"])
check("depots, building sites and LRT-branded housing are not stations",
      _prop == {"Stasiun MRT Bendungan Hilir", "Stasiun LRT Palmerah"},
      str(sorted(_prop)))

shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{PASSED} passed, {FAILED} failed")
sys.exit(0 if FAILED == 0 else 1)
