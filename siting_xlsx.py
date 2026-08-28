"""
Future GAPURA -> Excel.

WHY THE NUMBERS ARE VALUES AND NOT FORMULAS
    The inputs to KSI are 56,170 deduplicated POIs, 121 boundary polygons and
    the kecamatan profile metrics. None of that is in the workbook, so there
    is nothing for a cell formula to recompute from -- a `=` in front of a
    score would be decoration pretending to be a model. The scores are
    therefore written as values, and the Model sheet records exactly which
    model version and which parameters produced them so a reader can tell
    where each number came from.

    The Summary sheet IS formula-driven, because its source rows genuinely
    are in the workbook: it counts and averages over the Ranked Kecamatan
    sheet, so it updates if you filter, edit or extend that sheet.

    Formula cells carry no cached value until Excel or LibreOffice opens the
    file and recalculates -- they will look blank in a quick previewer. Every
    figure on the Summary sheet also appears as a value elsewhere, so nothing
    is only available through a formula.
"""
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

import siting

FONT = "Arial"
HEAD_FILL = PatternFill("solid", fgColor="1F2A44")
HEAD_FONT = Font(name=FONT, size=10, bold=True, color="FFFFFF")
BODY = Font(name=FONT, size=10)
BODY_DIM = Font(name=FONT, size=10, color="8A8F9C")
BOLD = Font(name=FONT, size=10, bold=True)
TITLE = Font(name=FONT, size=13, bold=True)
NOTE = Font(name=FONT, size=9, color="5A6070", italic=True)
THIN = Side(style="thin", color="D6DAE3")
BOX = Border(bottom=THIN)

# Light tints of the map's magenta-to-yellow ramp, chosen so black text
# stays readable on paper as well as on screen.
BANDS = [(80, "FFF3C4"), (60, "FFE2C9"), (40, "FBD5DF"), (20, "E9D9EF"),
         (0, "EDEDF2")]


def band_fill(score):
    if score is None:
        return None
    for lo, rgb in BANDS:
        if score >= lo:
            return PatternFill("solid", fgColor=rgb)
    return None


def _head(ws, headers, row=1):
    for c, h in enumerate(headers, 1):
        cell = ws.cell(row=row, column=c, value=h)
        cell.font = HEAD_FONT
        cell.fill = HEAD_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center",
                                   wrap_text=True)
    ws.row_dimensions[row].height = 30
    ws.freeze_panes = ws.cell(row=row + 1, column=1)


def _widths(ws, widths):
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


# ── sheet 1: ranked kecamatan ────────────────────────────────────────────
KEC_COLS = [
    ("rank", "Rank", 7, "0"),
    ("rank_in_profile", "Rank in profile", 9, "0"),
    ("kecamatan", "Kecamatan", 22, None),
    ("kabkot", "Kabupaten / Kota", 18, None),
    ("mc", "Microcluster", 18, None),
    ("sa", "Sales area", 18, None),
    ("profile", "Profile", 9, None),
    ("score", "KSI (within stratum)", 11, "0.0"),
    ("score_territory", "KSI (territory)", 11, "0.0"),
    ("_eligible", "Passes gates", 11, None),
    ("_gates", "Gate failures", 44, None),
    ("vlr", "VLR subs", 12, "#,##0"),
    ("vlr_pct", "VLR percentile", 10, "0.0"),
    ("ms_ioh", "Market share IOH", 11, "0.0%"),
    ("ms_pct", "Share percentile", 10, "0.0"),
    ("_cvi", "CVI", 8, "0.0"),
    ("_cpi", "CPI", 8, "0.0"),
    ("_net", "Network", 9, "0.0"),
    ("_finance", "Finance sub-score", 10, "0.0"),
    ("_minimarket_pct", "Minimarket density pct", 11, "0.0"),
    ("_pasar_pct", "Pasar density pct", 11, "0.0"),
    ("_comp_pct", "Competitor density pct", 11, "0.0"),
    ("_gap_pct", "RSoV gap pct", 10, "0.0"),
    ("_sov_pct", "Retail SoV pct", 10, "0.0"),
    ("area_km2", "Area km²", 10, "#,##0.0"),
    ("minimarket_n", "Minimarket", 10, "#,##0"),
    ("atm_n", "ATM", 8, "#,##0"),
    ("bank_n", "Bank", 8, "#,##0"),
    ("bank_top4_n", "Top-4 bank", 10, "#,##0"),
    ("pasar_n", "Pasar", 8, "#,##0"),
    ("mall_n", "Mall", 8, "#,##0"),
    ("agent_n", "Agen BRILink", 10, "#,##0"),
    ("own_stores", "Own stores", 10, "0"),
    ("comp_stores", "Competitor stores", 10, "0"),
    ("ipp_stores", "IPP outlets", 10, "0"),
    ("site_count", "Network sites", 10, "#,##0"),
    ("vlr_per_site", "VLR per site", 11, "#,##0"),
    ("km_hybrid", "km to nearest own counter", 12, "0.0"),
    ("km_im3", "km to IM3", 10, "0.0"),
    ("km_3id", "km to 3Store", 10, "0.0"),
    ("lat", "Latitude", 11, "0.000000"),
    ("lon", "Longitude", 11, "0.000000"),
]


def _kec_value(r, key):
    if not key.startswith("_"):
        return r.get(key)
    t, sub = r.get("terms") or {}, r.get("sub") or {}
    return {
        "_eligible": "yes" if r.get("score") is not None else "no",
        "_gates": "; ".join(r.get("gate_fails") or []),
        "_cvi": t.get("cvi"), "_cpi": t.get("cpi"), "_net": t.get("network"),
        "_finance": sub.get("finance"),
        "_minimarket_pct": sub.get("minimarket"),
        "_pasar_pct": sub.get("pasar"), "_comp_pct": sub.get("comp_den"),
        "_gap_pct": sub.get("rsov_gap"), "_sov_pct": sub.get("retail_sov"),
    }.get(key)


def sheet_kecamatan(wb, res):
    ws = wb.create_sheet("Ranked Kecamatan")
    _head(ws, [c[1] for c in KEC_COLS])
    _widths(ws, [c[2] for c in KEC_COLS])
    for i, r in enumerate(res["rows"], start=2):
        eligible = r.get("score") is not None
        for c, (key, _lab, _w, fmt) in enumerate(KEC_COLS, 1):
            cell = ws.cell(row=i, column=c, value=_kec_value(r, key))
            cell.font = BODY if eligible else BODY_DIM
            cell.border = BOX
            if fmt:
                cell.number_format = fmt
        if eligible:
            fill = band_fill(r["score"])
            if fill:
                ws.cell(row=i, column=8).fill = fill
            ws.cell(row=i, column=8).font = BOLD
    ws.auto_filter.ref = f"A1:{get_column_letter(len(KEC_COLS))}{len(res['rows']) + 1}"
    return ws


# ── sheet 2: candidate sites ─────────────────────────────────────────────
CAND_COLS = [
    ("rank", "Rank", 7, "0"),
    ("row_no", "Spreadsheet row", 9, "0"),
    ("location", "Location", 34, None),
    ("kecamatan_input", "Kecamatan (as typed)", 20, None),
    ("kecamatan", "Kecamatan (from geometry)", 22, None),
    ("_match", "Match", 9, None),
    ("kabkot", "Kabupaten / Kota", 18, None),
    ("mc", "Microcluster", 18, None),
    ("profile", "Profile", 9, None),
    ("score", "Stage-2 proxy score", 12, "0.0"),
    ("kec_score", "Stage-1 KSI of kecamatan", 12, "0.0"),
    ("cvi_local", "CVI in catchment", 11, "0.0"),
    ("radius_km", "Catchment radius km", 10, "0.0"),
    ("minimarket", "Minimarket in catchment", 11, "#,##0"),
    ("atm", "ATM in catchment", 11, "#,##0"),
    ("bank_all", "Bank in catchment", 11, "#,##0"),
    ("bank_top4", "Top-4 bank in catchment", 11, "#,##0"),
    ("vlr", "VLR of kecamatan", 12, "#,##0"),
    ("km_hybrid", "km to nearest own counter", 12, "0.0"),
    ("km_im3", "km to IM3", 10, "0.0"),
    ("km_3id", "km to 3Store", 10, "0.0"),
    ("_gates", "Gate failures", 44, None),
    ("error", "Problem", 30, None),
    ("lat", "Latitude", 11, "0.000000"),
    ("lon", "Longitude", 11, "0.000000"),
]


def sheet_candidates(wb, batch):
    ws = wb.create_sheet("Candidate Sites")
    ws["A1"] = "Stage-2 SAI is not implemented — this is a proxy"
    ws["A1"].font = TITLE
    ws["A2"] = siting.score_candidates.__doc__ and ""
    ws["A2"] = ("Sections 2-4 of 03-SCORING-MODEL.md define SAI over H3 cells "
                "with travel-time isochrones, day/night population, rent, "
                "betweenness centrality and a coverage-quality index. None of "
                "those are in this database. The score below is 50% the "
                "Stage-1 KSI of the containing kecamatan and 50% the CVI "
                "terms measured inside the catchment radius, each "
                "percentile-ranked against the same catchment measured at "
                "every kecamatan centroid of the same profile. It is a "
                "screening aid, not a business case.")
    ws["A2"].font = NOTE
    ws["A2"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells("A2:H2")
    ws.row_dimensions[2].height = 58
    _head(ws, [c[1] for c in CAND_COLS], row=4)
    _widths(ws, [c[2] for c in CAND_COLS])
    for i, r in enumerate(batch["rows"], start=5):
        scored = r.get("score") is not None
        for c, (key, _lab, _w, fmt) in enumerate(CAND_COLS, 1):
            if key == "_match":
                m = r.get("kecamatan_matches_input")
                v = "—" if m is None else ("yes" if m else "MISMATCH")
            elif key == "_gates":
                v = "; ".join(r.get("gate_fails") or [])
            else:
                v = r.get(key)
            cell = ws.cell(row=i, column=c, value=v)
            cell.font = BODY if scored else BODY_DIM
            cell.border = BOX
            if fmt:
                cell.number_format = fmt
        if scored:
            fill = band_fill(r["score"])
            if fill:
                ws.cell(row=i, column=10).fill = fill
            ws.cell(row=i, column=10).font = BOLD
    ws.auto_filter.ref = f"A4:{get_column_letter(len(CAND_COLS))}{len(batch['rows']) + 4}"
    return ws


# ── sheet 3: the model, its parameters and what is missing ───────────────
def _kv(ws, row, key, value, note=""):
    ws.cell(row=row, column=1, value=key).font = BODY
    c = ws.cell(row=row, column=2, value=value)
    c.font = BOLD
    if note:
        n = ws.cell(row=row, column=3, value=note)
        n.font = NOTE
        n.alignment = Alignment(wrap_text=True, vertical="top")
    return row + 1


def sheet_model(wb, res):
    ws = wb.create_sheet("Model and Gates", 0)
    _widths(ws, [34, 18, 96])
    p = res["params"]
    ws["A1"] = "Future GAPURA — Stage 1 kecamatan screening"
    ws["A1"].font = TITLE
    r = 3
    r = _kv(ws, r, "Model", res["model"],
            "Transcribed from the document named below. Weights, sub-weights "
            "and gates are the document's, not the analyst's.")
    r = _kv(ws, r, "Specification", res["doc"])
    r = _kv(ws, r, "Kecamatan scored", res["n_total"])
    r = _kv(ws, r, "Passing every gate", res["n_eligible"])
    r += 1

    ws.cell(row=r, column=1, value="PARAMETERS AS RUN").font = BOLD
    r += 1
    r = _kv(ws, r, "Strategy (gate G3)", p["strategy"],
            "DENSIFY scores market share as-is; ATTACK inverts it, favouring "
            "kecamatan where our share is low.")
    r = _kv(ws, r, "Percentile basis", p["scope"],
            "'stratum' ranks urban and rural kecamatan separately, per §1 of "
            "the document. 'territory' ranks all of them together.")
    r = _kv(ws, r, "Hybrid IM3 / 3Store",
            "on" if p.get("hybrid_own") else "off",
            "On: G1 measures to the nearest counter of either brand, because "
            "a Gerai serves 3ID customers and a 3Store serves IM3 customers. "
            "Off: each brand is gated separately against its own threshold.")
    r = _kv(ws, r, "G1 direction", p["g1_mode"],
            "'min' = at least N km from an existing counter, to avoid "
            "cannibalising it (the brief). 'max' = at most N km, staying "
            "within reach of the estate (the document as written). The two "
            "select opposite kecamatan.")
    r = _kv(ws, r, "G1 threshold, hybrid (km)", p["min_km_hybrid"])
    r = _kv(ws, r, "G1 threshold, IM3 (km)", p["min_km_im3"],
            "Used only when hybrid is off.")
    r = _kv(ws, r, "G1 threshold, 3Store (km)", p["min_km_3id"],
            "Used only when hybrid is off.")
    r = _kv(ws, r, "G2 VLR floor", p["vlr_min"], "Absolute subscriber floor.")
    r = _kv(ws, r, "G2 VLR percentile floor", p["vlr_pct_min"],
            "The document's own G2. Percentiles for gates are computed "
            "territory-wide even when scoring uses strata.")
    r = _kv(ws, r, "G3 share percentile floor", p["ms_pct_min"], "DENSIFY.")
    r = _kv(ws, r, "G3 share percentile ceiling", p["ms_pct_max"], "ATTACK.")
    r = _kv(ws, r, "Gates applied", "yes" if p["gates"] else "no (reported only)")
    r = _kv(ws, r, "IPP counted as own stores",
            "yes" if p.get("own_includes_ipp") else "no",
            "Affects retail share of voice inside CPI. Never affects G1, "
            "which is about a branded counter a customer can walk into.")
    r = _kv(ws, r, "Top banks counted", ", ".join(p["top_banks"]))
    r = _kv(ws, r, "Catchment radius urban / rural (km)",
            f"{p['radius_urban_km']} / {p['radius_rural_km']}",
            "Used only for uploaded candidate sites.")
    r += 1

    ws.cell(row=r, column=1, value="KSI WEIGHTS").font = BOLD
    r += 1
    for col, lab in ((1, "Term"), (2, "As run"), (3, "As specified")):
        c = ws.cell(row=r, column=col, value=lab)
        c.font = HEAD_FONT
        c.fill = HEAD_FILL
    r += 1
    for term, spec in res["ksi_weights_spec"].items():
        ws.cell(row=r, column=1, value=siting.TERM_LABELS.get(term, term)).font = BODY
        run = res["ksi_weights"].get(term)
        c = ws.cell(row=r, column=2, value=run if run is not None else "dropped")
        c.font = BOLD
        c.number_format = "0.000"
        s = ws.cell(row=r, column=3, value=spec)
        s.font = BODY
        s.number_format = "0.00"
        r += 1
    for term, spec in res["cvi_weights_spec"].items():
        ws.cell(row=r, column=1, value="   inside CVI · "
                + siting.TERM_LABELS.get(term, term)).font = BODY
        run = res["cvi_weights"].get(term)
        c = ws.cell(row=r, column=2, value=run if run is not None else "dropped")
        c.font = BODY
        c.number_format = "0.000"
        s = ws.cell(row=r, column=3, value=spec)
        s.font = BODY
        s.number_format = "0.00"
        r += 1
    r += 1

    ws.cell(row=r, column=1, value="INPUTS THIS DATABASE CANNOT SUPPLY").font = BOLD
    r += 1
    ws.cell(row=r, column=3, value=(
        "A term with no data is dropped and its weight redistributed across "
        "the terms that remain — never scored as zero, because a zero and a "
        "missing measurement rank kecamatan very differently and only one of "
        "them is honest. Every row below changes the model you actually got.")
    ).font = NOTE
    ws.cell(row=r, column=3).alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[r].height = 46
    r += 1
    av = res["availability"]
    for key, info in list(av.items()) + list(av.get("_cvi_sub", {}).items()) \
            + list(av.get("_gates", {}).items()):
        if key.startswith("_") or not isinstance(info, dict) or info.get("ok"):
            continue
        ws.cell(row=r, column=1, value=key).font = BOLD
        ws.cell(row=r, column=2, value="MISSING").font = Font(
            name=FONT, size=10, bold=True, color="B3462F")
        c = ws.cell(row=r, column=3, value=info.get("why"))
        c.font = BODY
        c.alignment = Alignment(wrap_text=True, vertical="top")
        ws.row_dimensions[r].height = 30
        r += 1
    r += 1

    ws.cell(row=r, column=1, value="GATE AUDIT").font = BOLD
    r += 1
    for col, lab in ((1, "Gate"), (2, "Kecamatan passing on its own")):
        c = ws.cell(row=r, column=col, value=lab)
        c.font = HEAD_FONT
        c.fill = HEAD_FILL
    r += 1
    for g in res["gate_stats"]:
        ws.cell(row=r, column=1, value=g["label"]).font = BODY if g["active"] else BODY_DIM
        c = ws.cell(row=r, column=2, value=g["passes"])
        c.font = BODY if g["active"] else BODY_DIM
        r += 1
    ws.cell(row=r, column=1, value="PASS EVERY GATE").font = BOLD
    ws.cell(row=r, column=2, value=res["n_eligible"]).font = BOLD
    ws.cell(row=r, column=3, value=(
        "Each row above counts the kecamatan that would pass that gate with "
        "the others switched off. A small intersection is a finding about the "
        "territory, not a fault in the model: the kecamatan holding the "
        "subscribers are the ones that already have a counter.")).font = NOTE
    ws.cell(row=r, column=3).alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[r].height = 46
    r += 2
    ws.cell(row=r, column=1, value=(
        "Scores on the other sheets are values, not formulas: their inputs "
        "(56,170 deduplicated POIs and 121 boundary polygons) are not in this "
        "workbook, so a formula would have nothing to recompute from. The "
        "Summary sheet does use formulas, because its source rows are here.")
    ).font = NOTE
    ws.cell(row=r, column=1).alignment = Alignment(wrap_text=True, vertical="top")
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=3)
    ws.row_dimensions[r].height = 44
    return ws


# ── sheet 4: summary, driven by formulas over sheet 1 ────────────────────
def sheet_summary(wb, res):
    ws = wb.create_sheet("Summary")
    _widths(ws, [34, 16, 16, 60])
    n = len(res["rows"])
    last = n + 1
    K = "'Ranked Kecamatan'"
    ws["A1"] = "Summary"
    ws["A1"].font = TITLE
    ws["D1"] = ("These cells count and average over the Ranked Kecamatan "
                "sheet, so they follow any edit you make there. They are "
                "blank until Excel or LibreOffice opens the file and "
                "recalculates — openpyxl cannot cache formula results.")
    ws["D1"].font = NOTE
    ws["D1"].alignment = Alignment(wrap_text=True, vertical="top")
    ws.row_dimensions[1].height = 48

    rows = [
        ("Kecamatan scored", f"=COUNTA({K}!C2:C{last})", "0"),
        ("Passing every gate", f'=COUNTIF({K}!J2:J{last},"yes")', "0"),
        ("Urban kecamatan", f'=COUNTIF({K}!G2:G{last},"urban")', "0"),
        ("Rural kecamatan", f'=COUNTIF({K}!G2:G{last},"rural")', "0"),
        ("Highest KSI", f"=MAX({K}!H2:H{last})", "0.0"),
        ("Mean KSI, eligible only",
         f'=IFERROR(AVERAGEIF({K}!J2:J{last},"yes",{K}!H2:H{last}),0)', "0.0"),
        ("Mean KSI, urban",
         f'=IFERROR(AVERAGEIF({K}!G2:G{last},"urban",{K}!H2:H{last}),0)', "0.0"),
        ("Mean KSI, rural",
         f'=IFERROR(AVERAGEIF({K}!G2:G{last},"rural",{K}!H2:H{last}),0)', "0.0"),
        ("Total VLR subscribers", f"=SUM({K}!L2:L{last})", "#,##0"),
        ("Total minimarket", f"=SUM({K}!Z2:Z{last})", "#,##0"),
        ("Total ATM", f"=SUM({K}!AA2:AA{last})", "#,##0"),
        ("Total bank branches", f"=SUM({K}!AB2:AB{last})", "#,##0"),
        ("Total top-4 bank branches", f"=SUM({K}!AC2:AC{last})", "#,##0"),
        ("Own service points in territory", f"=SUM({K}!AG2:AG{last})", "0"),
        ("Competitor service points in territory", f"=SUM({K}!AH2:AH{last})", "0"),
    ]
    r = 3
    for label, formula, fmt in rows:
        ws.cell(row=r, column=1, value=label).font = BODY
        c = ws.cell(row=r, column=2, value=formula)
        c.font = BOLD
        c.number_format = fmt
        c.border = BOX
        r += 1

    r += 1
    ws.cell(row=r, column=1, value="TOP 10 BY KSI").font = BOLD
    r += 1
    for col, lab in ((1, "Kecamatan"), (2, "Kabupaten / Kota"), (3, "KSI"),
                     (4, "Why it ranks where it does")):
        c = ws.cell(row=r, column=col, value=lab)
        c.font = HEAD_FONT
        c.fill = HEAD_FILL
    r += 1
    top = [x for x in res["rows"] if x.get("score") is not None][:10]
    for x in top:
        ws.cell(row=r, column=1, value=x["kecamatan"]).font = BODY
        ws.cell(row=r, column=2, value=x["kabkot"]).font = BODY
        c = ws.cell(row=r, column=3, value=x["score"])
        c.font = BOLD
        c.number_format = "0.0"
        fill = band_fill(x["score"])
        if fill:
            c.fill = fill
        parts = ", ".join(f"{p['label']} {p['pct']:.0f}"
                          for p in (x.get("parts") or [])[:3])
        ws.cell(row=r, column=4, value=parts).font = BODY
        r += 1
    if not top:
        ws.cell(row=r, column=1, value="No kecamatan passed every gate. "
                "The Model and Gates sheet shows which gate removed them.").font = NOTE
    return ws


# ── entry point ──────────────────────────────────────────────────────────
def build(res, batch=None):
    wb = Workbook()
    wb.remove(wb.active)
    sheet_model(wb, res)
    sheet_kecamatan(wb, res)
    if batch and batch.get("rows"):
        sheet_candidates(wb, batch)
    sheet_summary(wb, res)
    wb.active = 0
    return wb
