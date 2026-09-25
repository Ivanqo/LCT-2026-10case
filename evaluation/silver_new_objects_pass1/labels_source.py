"""Source of the manual SILVER labels for the 6 new objects (pass 1).

Method (same 5-status scheme as silver_labels/violation_review_pass1.jsonl):
    VIOLATION_PRESENT / NO_VIOLATION / NEEDS_REVIEW / UNLABELED / EXCLUDED
with an exact file_id + page reference for every stage that was compared.

Blind protocol: every record below was written from the ORIGINAL PDFs (via
probe.py, never via the pipeline/DB) BEFORE any mechanism_snapshots/*.json was
opened. Ground truth is defined by the parameter's own catalog trigger
(parameter_catalog_132.jsonl), not by "any number differs":
    NO_VIOLATION         both stages' values found and the trigger is not met
    VIOLATION_PRESENT    both stages' values found and the trigger is met
    NEEDS_REVIEW         reviewer could not establish a like-for-like PD/RD pair
                         from the corpus (baseline absent / multi-instance /
                         mixed design generations) -- NOT counted in metrics
`pd_rd_documented_discrepancy` records, independently of the verdict, whether
the reviewer saw two *different* numbers for the same row label across stages
(explained or not) -- used only for the secondary "lenient" basis.

Run:  python labels_source.py   -> writes ../silver_labels/new_objects_review_pass1.jsonl
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "silver_labels" / "new_objects_review_pass1.jsonl"

CATALOG = {
    "PZ-001": ("Площадь застройки", "м²", "Расхождение контуров здания на генплане с данными БТИ или РД > 0."),
    "PZ-002": ("Общая площадь здания", "м²", "Дельта общей площади между ПД и РД (или ИД) > 1%."),
    "PZ-004": ("Строительный объем (Общий)", "м³", "Изменение внешних объемных габаритов здания в РД без корректировки ПД."),
    "PZ-005": ("Строительный объем (Подземный)", "м³", "Изменение глубины заложения или объема паркинга/подвала в РД."),
    "PZ-006": ("Строительный объем (Надземный)", "м³", "Изменение общего числа этажей (включая технический) в РД."),
    "PZ-008": ("Высота здания", "м", "Увеличение высоты здания в РД (риск нарушения ограничений приаэродромных зон)."),
    "PZ-010": ("Количество квартир", "шт.", "Изменение общего количества жилых единиц (перепланировка в РД)."),
    "PZ-011": ("Квартирография", "шт.", "Изменение пропорций (например, замена 3-комнатной на две студии)."),
    "PZ-012": ("Количество машино-мест (подземных)", "шт.", "Сокращение количества машино-мест в РД (нарушение местных нормативов)."),
    "PZ-022": ("Степень огнестойкости здания", "Степень", "Снижение степени огнестойкости (например, с I на II) в РД."),
    "PZ-023": ("Класс конструктивной пожарной опасности", "Класс (С0, С1)", "Снижение класса пожарной опасности (например, с С0 на С1) в РД."),
    "SPZU-024": ("Объем грунта (выемка/насыпь)", "м³", "Дельта объемов выемки и обратной засыпки между стадиями > 5%."),
    "AR-041": ("Ширина эвакуационных выходов (дверей)", "м", "Ширина дверного полотна на путях эвакуации в РД/ИД < 0.9 м."),
    "KR-067": ("Ведомости расхода материалов (бетон, сталь)", "м³ / т", "Итоговое расхождение объемов материалов между ПД, РД и ИД > 2%."),
}

_MANIFEST_CACHE: dict[str, dict] = {}


def _file(file_id: str) -> str:
    if not _MANIFEST_CACHE:
        for p in (HERE / "manifests").glob("manifest_*.jsonl"):
            for line in p.read_text(encoding="utf-8").splitlines():
                r = json.loads(line)
                _MANIFEST_CACHE[r["file_id"]] = r
    return _MANIFEST_CACHE[file_id]["relative_path"]


def R(file_id: str, page: int, locator: str, quote: str = "") -> dict:
    return {"file_id": file_id, "file": _file(file_id), "page": page, "locator": locator, "quote": quote}


ORIG19 = {
    "LOS3A": {"AR-041", "KR-067", "PZ-002", "PZ-004", "PZ-005", "PZ-006", "PZ-008", "PZ-022", "PZ-023", "SPZU-024"},
    "ALT79B": {"KR-067", "PZ-023", "SPZU-024"},
    "POL17": {"AR-041", "PZ-005", "PZ-006", "PZ-022", "PZ-023"},
    "DOO25": {"PZ-023"},
}


def rec(obj: str, code: str, verdict: str, *, confidence: str, pd_value=None, rd_value=None, id_value=None,
        relation: str, delta_pct=None, pd_refs=(), rd_refs=(), id_note: str, discrepancy, reasoning: str,
        stratum: str | None = None, amendment: str | None = None) -> dict:
    name, unit, trigger = CATALOG[code]
    in19 = code in ORIG19.get(obj, set())
    return {
        "check_id": f"OBJ-NEW-{obj}::{code}",
        "object_id": f"OBJ-NEW-{obj}",
        "object_code": obj,
        "parameter_code": code,
        "parameter_name": name,
        "unit": unit,
        "catalog_trigger": trigger,
        "verdict": verdict,
        "verdict_confidence": confidence,
        "pd_value": pd_value,
        "rd_value": rd_value,
        "id_value": id_value,
        "value_relation": relation,
        "delta_percent_rd_vs_pd": delta_pct,
        "pd_reference": list(pd_refs),
        "rd_reference": list(rd_refs),
        "id_reference": id_note,
        "pd_rd_documented_discrepancy": discrepancy,
        "reasoning": reasoning,
        "stratum": stratum or ("COMPARABLE_19_TAGGER_SESSION" if in19 else "TEP_TABLE_EXTENSION"),
        "in_original_19": in19,
        "labelled_blind_to_current_mechanism_output": True,
        "annotation_tier": "SILVER",
        "annotator": "claude-single-annotator (same model family as the system under test)",
        "second_review_status": "PENDING",
        "training_eligible": False,
        "reasoning_amendment_after_unblinding": amendment,
    }


LOS3A_PD_TEP = "LOS3A-000003"   # 1. 01-01-00-02-ПЗ.pdf, PDF p.14-15 = ТЭП table
LOS3A_RD_TEP = "LOS3A-000069"   # 19-0322-ОК-1_Н-1-АР2 ... PDF p.11 = "Общие данные" ТЭП table
NO_ID_LOS3A = ("LOS3A has one ID file (! ИД АР.pdf, 3956 pp, LOS3A-000001, text layer present): a full-text search for "
               "25036|88264|1076,49|74301|13962|3196,44|83,54|площадь здания|строительный объем|степень огнестойкости|"
               "конструктивной пожарной found 0 hits -- no building-level value at the as-built stage.")

LABELS: list[dict] = []

# ------------------------------------------------------------------ LOS3A
LABELS += [
    rec("LOS3A", "PZ-002", "NO_VIOLATION", confidence="HIGH", pd_value="25036.27", rd_value="25036.27", relation="EQUAL", delta_pct=0.0,
        pd_refs=[R(LOS3A_PD_TEP, 14, "ТЭП table row 4", "Площадь здания (по СП 54.13330.2016, прил. А.1.2) 25036,27 м2")],
        rd_refs=[R(LOS3A_RD_TEP, 11, "ТЭП table row 6", "Площадь здания м² 25036,27"), R("LOS3A-000070", 10, "same ТЭП (section 2 set)", "Площадь здания м² 25036,27")],
        id_note=NO_ID_LOS3A, discrepancy=False,
        reasoning="Identical figure in the PD ПЗ ТЭП table and in the RD АР2 'Общие данные' ТЭП table (also repeated in the section-2 АР2 set). "
                  "Note PD ПЗ p.15 row 14 'Площадь помещений здания 16 867,90' (СП 118.13330.2012 Г.5) is a DIFFERENT quantity, not the parameter."),
    rec("LOS3A", "PZ-004", "NO_VIOLATION", confidence="HIGH", pd_value="88264.00", rd_value="88264.00", relation="EQUAL", delta_pct=0.0,
        pd_refs=[R(LOS3A_PD_TEP, 14, "ТЭП table row 5", "Строительный объем в т.ч. 88264,00 м3")],
        rd_refs=[R(LOS3A_RD_TEP, 11, "ТЭП table row 14", "Строительный объем в т.ч. м³ 88264,00")],
        id_note=NO_ID_LOS3A, discrepancy=False,
        reasoning="Same total construction volume in PD ПЗ and RD АР2 ТЭП tables."),
    rec("LOS3A", "PZ-005", "NO_VIOLATION", confidence="HIGH", pd_value="13962.18", rd_value="13962.18", relation="EQUAL", delta_pct=0.0,
        pd_refs=[R(LOS3A_PD_TEP, 14, "ТЭП table row 5, sub-row 'ниже отм. 0.000'", "ниже отм. 0.000 13962,18 м3")],
        rd_refs=[R(LOS3A_RD_TEP, 11, "ТЭП table row 14, sub-row 'Ниже отм. 0.000'", "Ниже отм. 0.000 м³ 13962,18")],
        id_note=NO_ID_LOS3A, discrepancy=False,
        reasoning="Underground volume (below 0.000) identical in PD and RD. The TOTAL (88264.00) is not the parameter -- checks that compare the total with itself are a value error."),
    rec("LOS3A", "PZ-006", "NO_VIOLATION", confidence="HIGH", pd_value="74301.82", rd_value="74301.82", relation="EQUAL", delta_pct=0.0,
        pd_refs=[R(LOS3A_PD_TEP, 14, "ТЭП table row 5, sub-row 'выше отм. 0.000'", "выше отм. 0.000 74301,82 м3")],
        rd_refs=[R(LOS3A_RD_TEP, 11, "ТЭП table row 14, sub-row 'Выше отм. 0.000'", "Выше отм. 0.000 м³ 74301,82")],
        id_note=NO_ID_LOS3A, discrepancy=False,
        reasoning="Above-ground volume identical in PD and RD."),
    rec("LOS3A", "PZ-008", "NO_VIOLATION", confidence="MEDIUM", pd_value="82.8", rd_value="83.54", relation="DIFFERENT_DEFINITION_TOP_MARK_UNCHANGED",
        delta_pct=round((83.54 - 82.8) / 82.8 * 100, 2),
        pd_refs=[R(LOS3A_PD_TEP, 14, "ТЭП table row 6", "Высота + 82,8 м"), R("LOS3A-000007", 7, "ПЗУ1 table", "Предельная высота (м) 85 / Высота + 82,8 м"),
                 R("LOS3A-000009", 13, "АР text", "высота от наименьшей точки на участке (абс. 151,65) до верхнего парапета 83,890 м (абс. 235,54); ГПЗУ max 85,00 м")],
        rd_refs=[R(LOS3A_RD_TEP, 11, "ТЭП table row 16", "Высота объекта м 83,54"),
                 R("LOS3A-000076", 9, "АР5 facades", "верх +82.800 (same top mark as PD)")],
        id_note=NO_ID_LOS3A, discrepancy=True,
        reasoning="The two ТЭП rows differ (PD 82,8 vs RD 83,54, +0.9%), BUT the physical top mark is the same in both stages: PD facades/АР p.33-35 "
                  "and RD АР5 p.9-16 both show +82.800; PD АР p.13 gives abs. 235,54 for the parapet and RD КЖ2.1.2 p.5 fixes 0.000 = abs. +152,74 "
                  "(152,74 + 82,8 = 235,54). 83,54 = 235,54 - 152,00, i.e. consistent with a height measured from a 152,00 planning mark on the RD ГП "
                  "(INFERENCE -- the RD does not state its reference level). PD itself carries three different 'heights' (82,8 / 82,81 in ПБ.1 p.9 / 83,89 in АР p.13). "
                  "Trigger is a real increase of the building height: none is evidenced -> NO_VIOLATION, medium confidence; the documented ТЭП-row discrepancy is kept in the secondary flag."),
    rec("LOS3A", "PZ-022", "NO_VIOLATION", confidence="HIGH", pd_value="I", rd_value="I", relation="EQUAL",
        pd_refs=[R(LOS3A_PD_TEP, 17, "ПЗ text", "Степень огнестойкости I (первая)"), R("LOS3A-000009", 8, "АР text", "Степень огнестойкости здания – I"),
                 R("LOS3A-000047", 129, "ПБ.1 summary table", "Жилая часть: Степень огнестойкости I")],
        rd_refs=[R(LOS3A_RD_TEP, 11, "Общие указания", "Объект проектирования относится к 1 степени огнестойкости"), R("LOS3A-000072", 10, "АР0 Общие данные", "1 степени огнестойкости")],
        id_note=NO_ID_LOS3A, discrepancy=False,
        reasoning="Both stages state degree I for the building. Noise that is NOT a discrepancy: lightning-protection boilerplate 'Степень огнестойкости здания - II' "
                  "appears in BOTH stages (PD ИОС1.1 p.53/59; RD МЗ p.11/14), 'КМ 0/КМ 1' in RD are finishing-material flammability classes, PD ПБ.1 p.17/19 'V' belongs to the ТП/site structures."),
    rec("LOS3A", "PZ-023", "NO_VIOLATION", confidence="HIGH", pd_value="С0", rd_value="С0", relation="EQUAL",
        pd_refs=[R(LOS3A_PD_TEP, 18, "ПЗ text", "Класс конструктивной пожарной опасности – С0"), R("LOS3A-000009", 8, "АР text", "Класс конструктивной пожарной опасности – С0"),
                 R("LOS3A-000047", 129, "ПБ.1 summary table", "Класс конструктивной опасности C0")],
        rd_refs=[R(LOS3A_RD_TEP, 11, "Общие указания", "Класс конструктивной пожарной опасности - СО (Cyrillic О = C0)"), R("LOS3A-000072", 10, "АР0", "Класс конструктивной пожарной опасности - СО")],
        id_note=NO_ID_LOS3A, discrepancy=False,
        reasoning="C0 in both stages. RD text writes 'СО' with a Cyrillic letter O instead of digit 0 -- an encoding variant, not a different class."),
    rec("LOS3A", "SPZU-024", "VIOLATION_PRESENT", confidence="MEDIUM", pd_value="site: насыпь 393 / выемка 1491; parking: 2088 / 2088",
        rd_value="site: насыпь 380 / выемка 1350; parking: 1993 / 1993; +extra-landscaping table 144 / 144", relation="DIFFERENT_TRIGGER_MET",
        delta_pct=round((1350 - 1491) / 1491 * 100, 2),
        pd_refs=[R("LOS3A-000007", 22, "ПЗУ1 лист 6 'План земляных масс', bottom-left table row 7 'Итого перерабатываемого грунта'",
                   "393 | 1491 (site); parking table 2088 | 2088")],
        rd_refs=[R("LOS3A-000050", 13, "ГП лист 4 'План земляных масс', bottom-left table row 7", "380 | 1350 (site); parking 1993 | 1993; additional-landscaping fragment 144 | 144")],
        id_note=NO_ID_LOS3A, discrepancy=True,
        reasoning="Both stages carry the same-structure earth-mass balance sheet (verified by rendering both pages at 3x). Site excavation ('выемка') total falls 1491 -> 1350 (-9.5% > 5% trigger); "
                  "site fill 393 -> 380 (-3.3%); parking balance 2088 -> 1993 (-4.55%, below 5%). The parameter has several totals; the site 'Итого перерабатываемого грунта' выемка is used as the "
                  "primary quantity -> trigger met. Medium confidence: whether the RD change is an approved update is not knowable from the corpus; the parking table alone would not meet the trigger. "
                  "Evidence crops (3x zoom of the four total rows): evaluation/silver_new_objects_pass1/evidence/spzu024_*.png."),
    rec("LOS3A", "AR-041", "NEEDS_REVIEW", confidence="HIGH", relation="INDETERMINATE",
        pd_refs=[R("LOS3A-000009", 10, "АР text", "Ширина дверей на путях эвакуации – не менее 1,2 м в свету"), R("LOS3A-000009", 11, "АР text", "Ширина проемов в свету не менее 0,9м"),
                 R("LOS3A-000047", 53, "ПБ.1 text", "ширина эвакуационных выходов не менее 0,8 м"), R("LOS3A-000047", 132, "ПБ.1 evacuation tables", "Ширина двери зон безопасности МГН 0,9")],
        rd_refs=[R(LOS3A_RD_TEP, 11, "Общие указания", "Ширина проемов в свету не менее 0,9м")],
        id_note=NO_ID_LOS3A, discrepancy=None,
        reasoning="Multi-instance parameter (dozens of doors of different widths per floor). PD states several different minimum widths (0,8 / 0,9 / 1,2 m) in different contexts and RD "
                  "repeats the 0,9 m sentence; the trigger (any evacuation door leaf < 0,9 m in RD) needs a door-by-door audit of the RD door schedules against the evacuation plan, "
                  "which was not done. A single scalar PD-vs-RD comparison is ill-posed here."),
    rec("LOS3A", "KR-067", "NEEDS_REVIEW", confidence="HIGH", relation="INDETERMINATE",
        pd_refs=[], rd_refs=[R("LOS3A-000083", 4, "КЖ2.1.3 per-set steel schedule", "Общий расход 47,9 34,9 82,8 136,5 515,2 21427,8 4629,6 26709 26791,9 (this set only)")],
        id_note=NO_ID_LOS3A, discrepancy=None,
        reasoning="No PD baseline exists: text-layer searches of all 47 PD documents (incl. КР.1, 85 pp, 0 pages without text) for consumption / 'ведомость расхода' / mass-of-metal / "
                  "'общий расход' found no structural material-consumption summary (only utility-network bills of quantities). RD carries per-drawing-set steel schedules (kg) without a "
                  "building total. No like-for-like PD/RD pair can be formed. (Caveat: 14 PD documents have >=15% pages without a text layer.)"),
    # extension: same two ТЭП pages, checks decidable without any new search
    rec("LOS3A", "PZ-001", "NO_VIOLATION", confidence="HIGH", pd_value="1076.49", rd_value="1076.49", relation="EQUAL", delta_pct=0.0,
        pd_refs=[R(LOS3A_PD_TEP, 14, "ТЭП table row 2", "Площадь застройки (по СП 54.13330.2022 прил. А.1.1. с учетом выступающих частей...) 1076,49 м2")],
        rd_refs=[R(LOS3A_RD_TEP, 11, "ТЭП table row 2", "Площадь застройки (по СП 54.13330.2022 прил. А.1.1. с учетом выступающих частей балконов) м² 1076,49")],
        id_note=NO_ID_LOS3A, discrepancy=False, reasoning="Identical footprint area in PD and RD (also 1061,49 without balconies in both)."),
    rec("LOS3A", "PZ-010", "NO_VIOLATION", confidence="HIGH", pd_value="231", rd_value="231", relation="EQUAL", delta_pct=0.0,
        pd_refs=[R(LOS3A_PD_TEP, 15, "ТЭП table row 25", "Количество квартир, в т.ч.: 231")],
        rd_refs=[R(LOS3A_RD_TEP, 11, "ТЭП table row 10", "Количество квартир, в т.ч.: Шт. 231")],
        id_note=NO_ID_LOS3A, discrepancy=False, reasoning="Apartment count identical."),
    rec("LOS3A", "PZ-011", "NO_VIOLATION", confidence="HIGH", pd_value="1к 44 / 2к 150 / 3к 37", rd_value="1к 44 / 2к 150 / 3к 37", relation="EQUAL", delta_pct=0.0,
        pd_refs=[R(LOS3A_PD_TEP, 15, "ТЭП table row 25", "однокомнатных 44; двухкомнатных 150; трехкомнатных 37")],
        rd_refs=[R(LOS3A_RD_TEP, 11, "ТЭП table row 10", "Однокомнатные 44; Двухкомнатные 150; Трехкомнатные 37")],
        id_note=NO_ID_LOS3A, discrepancy=False, reasoning="Apartment-type mix identical (also 2 + 4 МГН units in both)."),
    rec("LOS3A", "PZ-012", "NO_VIOLATION", confidence="HIGH", pd_value="72", rd_value="72", relation="EQUAL", delta_pct=0.0,
        pd_refs=[R(LOS3A_PD_TEP, 15, "ТЭП table row 30", "Кол-во машиномест в подземной автостоянке 72 шт.")],
        rd_refs=[R(LOS3A_RD_TEP, 11, "ТЭП table row 15", "Кол-во машиномест в подземной автостоянке Шт. 72")],
        id_note=NO_ID_LOS3A, discrepancy=False, reasoning="Underground parking places identical."),
]

# ------------------------------------------------------------------ ALT79B
NO_ID_ALT = "ALT79B has no ID stage documents at all (68 PD + 10 RD files)."
LABELS += [
    rec("ALT79B", "PZ-023", "NO_VIOLATION", confidence="HIGH", pd_value="С0", rd_value="С0", relation="EQUAL_ON_LIKE_FOR_LIKE_ROWS",
        pd_refs=[R("ALT79B-000015", 9, "АР Изм.1 text", "Класс конструктивной пожарной опасности - С0"), R("ALT79B-000066", 7, "ПБ text", "степень огнестойкости и класс конструктивной пожарной опасности не ниже II, С0"),
                 R("ALT79B-000004", 4, "ОДИ text", "класс конструктивной пожарной опасности-С0")],
        rd_refs=[R("ALT79B-000076", 3, "АР1 Общие данные", "III степени огнестойкости, класса конструктивной пожарной опасности - С0"), R("ALT79B-000077", 3, "АР2 Общие данные", "...конструктивной пожарной опасности - С0")],
        id_note=NO_ID_ALT, discrepancy=True,
        reasoning="RD (АР1/АР2) = С0. PD 2025 series (П-2025-04.266): АР Изм.1, ПБ, ОДИ, ТБЭО = С0, but PD КР (27.04.26) p.6/20/27/28 and ПОС Изм.1 p.13 say С1 (PD is internally inconsistent; "
                  "the 2024 ЖС-РД-270121 PD generation says II/С0). Trigger is a REDUCTION of the class (С0 -> С1): RD С0 is never lower than any PD row -> NO_VIOLATION. "
                  "The PD-internal С1 rows create a documented numeric difference against RD (kept in the secondary flag) but not a trigger event."),
    rec("ALT79B", "SPZU-024", "NO_VIOLATION", confidence="MEDIUM", pd_value="7940 (excavation pit)", rd_value="7940 (excavation pit)", relation="EQUAL", delta_pct=0.0,
        pd_refs=[R("ALT79B-000019", 35, "КР (27.04.26) section drawing", "Объем выемки грунта усреднен - 7 940 м³")],
        rd_refs=[R("ALT79B-000069", 4, "КЖ01 section drawing", "Объем выемки грунта усреднен - 7 940 м³"), R("ALT79B-000070", 4, "КЖ02 same drawing", "Объем выемки грунта усреднен - 7 940 м³")],
        id_note=NO_ID_ALT, discrepancy=False,
        reasoning="The only excavation volume stated in RD is the pit volume 7 940 m3; the same figure is in PD КР. Like-for-like pair equal -> no trigger. "
                  "Scope caveat: RD has no earth-mass plan (no ГП set in the RD folder), and PD СПОЗУ (ALT79B-000012 p.18) gives a different quantity, the site planning balance "
                  "(насыпь 2187.9 / выемка 5123.0) -- comparing it with 7 940 would be a scope error, not a violation."),
    rec("ALT79B", "KR-067", "NEEDS_REVIEW", confidence="MEDIUM", relation="INDETERMINATE",
        pd_refs=[R("ALT79B-000019", 69, "КР ЛМ-1 steel table", "Всего масса металла, т ... 14,794")],
        rd_refs=[R("ALT79B-000072", 12, "КМ ЛМ-1 steel table", "Всего масса металла, т ... 14,791")],
        id_note=NO_ID_ALT, discrepancy=False,
        reasoning="Only per-element/per-set tables exist (one same-element pair found: ЛМ-1 steel 14,794 t PD vs 14,791 t RD = 0.02%, within the 2% trigger). No building-level PD or RD "
                  "consumption total was located; the trigger (total material discrepancy > 2%) cannot be evaluated from the corpus without summing dozens of sheet sets. "
                  "PD folder also mixes two design generations (2024 ЖС-РД-270121 vs 2025 П-2025-04.266)."),
]

# ------------------------------------------------------------------ POL17
NO_ID_POL = ("POL17 ID (26 files) searched (text layer) for конструктивной пожарной / степень огнестойкости / ширина двери -> 0 hits. ID folder 'Проект организации строительства' "
             "files (POL17-000001/9/22 p.1030) are a copy of the PD ПОС text, not as-built data.")
POL_GEN_NOTE = ("PD folder mixes two design generations: OLD (11+1 floors, 47374.10 m3, degree II) in ПЗ2/ПБ Корр.5/КР1/ИОС*, and NEW (20-13 floors, 'Изм.2 077-25 02.2025', degree I) in АР_v5 "
                "(POL17-000031 p.6-7); RD (АР0-3, ВК3, НС) follows the NEW generation.")
LABELS += [
    rec("POL17", "PZ-023", "NO_VIOLATION", confidence="HIGH", pd_value="С0", rd_value="С0", relation="EQUAL",
        pd_refs=[R("POL17-000031", 7, "АР_v5 text (new generation)", "Класс конструктивной пожарной опасности – С0 (ст.31 №123-ФЗ)"), R("POL17-000068", 10, "ПБ Корр.5", "класс конструктивной пожарной опасности - С0"),
                 R("POL17-000071", 38, "ПЗ2", "Класс конструктивной пожарной опасности – С0")],
        rd_refs=[R("POL17-000095", 3, "АР0 Общие данные", "Класс конструктивной пожарной опасности – С0 (ст.31 №123-ФЗ)"), R("POL17-000096", 3, "АР1", "..С0"), R("POL17-000097", 3, "АР2", "..С0")],
        id_note=NO_ID_POL, discrepancy=False, reasoning="C0 in both design generations of the PD and in RD."),
    rec("POL17", "PZ-022", "NO_VIOLATION", confidence="HIGH", pd_value="I (new-generation АР_v5) / II (old-generation ПБ, ПЗ2, КР1, ОДИ, ТБЭ)", rd_value="I", relation="EQUAL_ON_NEW_GENERATION_INCREASE_ON_OLD",
        pd_refs=[R("POL17-000031", 7, "АР_v5 text", "Степень огнестойкости – I (ст.30 №123-ФЗ)"), R("POL17-000068", 10, "ПБ Корр.5", "степень огнестойкости здания - II"), R("POL17-000071", 38, "ПЗ2", "Фактическая степень огнестойкости – II")],
        rd_refs=[R("POL17-000095", 3, "АР0", "Степень огнестойкости здания - I (ст.30 №123-ФЗ)"), R("POL17-000096", 3, "АР1", "..I"), R("POL17-000097", 3, "АР2", "..I")],
        id_note=NO_ID_POL, discrepancy=True,
        reasoning=POL_GEN_NOTE + " Against the NEW-generation PD the degree is equal (I = I); against the OLD generation it changes II -> I, an INCREASE of fire resistance, whereas the trigger is a "
                  "REDUCTION -> NO_VIOLATION either way. The II-vs-I numeric difference is a real documented discrepancy (secondary flag) but not a trigger event.",
        amendment="Evidence added after seeing mechanism output (verdict unchanged): RD is itself inconsistent -- РД КМ (POL17-000124 p.2) says 'Степень огнестойкости здания - II' "
                  "while АР0-АР4/АИ2/ОВ1 say I. Like-for-like pairs are still equal: PD АР_v5 I = RD АР I; PD КР1 II = RD КМ II. My first RD grep was capped at 40 hits and missed КМ."),
    rec("POL17", "PZ-005", "NEEDS_REVIEW", confidence="MEDIUM", pd_value="14945.60 (old-generation PD)", rd_value="14616.3 (RD ВК3, new generation)", relation="INDETERMINATE_MIXED_GENERATIONS",
        delta_pct=round((14616.3 - 14945.6) / 14945.6 * 100, 2),
        pd_refs=[R("POL17-000071", 10, "ПЗ2 ТЭП table row 7", "подземной части 14945,60м³"), R("POL17-000039", 6, "ИОС2.1 Корр.6", "подземной части 14945,6 м³")],
        rd_refs=[R("POL17-000102", 4, "ВК3 Общие указания (only RD restatement found)", "подземной части 14616.3 м³")],
        id_note=NO_ID_POL, discrepancy=True,
        reasoning=POL_GEN_NOTE + " The only RD volume statement is in the ВК3 general notes (14616.3 underground / 74326.3 above / 88942.6 total) and describes the NEW generation; the only PD ТЭП table "
                  "(14945.60 / 32428.5 / 47374.10) describes the OLD one. No new-generation PD volume was located. Cross-generation comparison is not like-for-like and which PD generation is the "
                  "approved baseline cannot be decided from the corpus -> NEEDS_REVIEW (a like-for-like reviewer would still see a large documented numeric difference)."),
    rec("POL17", "PZ-006", "NEEDS_REVIEW", confidence="MEDIUM", pd_value="32428.5 (old-generation PD)", rd_value="74326.3 (RD ВК3, new generation)", relation="INDETERMINATE_MIXED_GENERATIONS",
        delta_pct=round((74326.3 - 32428.5) / 32428.5 * 100, 2),
        pd_refs=[R("POL17-000071", 10, "ПЗ2 ТЭП table row 7", "наземной части 32428,5м³"), R("POL17-000039", 6, "ИОС2.1 Корр.6", "надземной части 32428,5 м³")],
        rd_refs=[R("POL17-000102", 4, "ВК3 Общие указания (only RD restatement found)", "надземной части 74326.3 м³")],
        id_note=NO_ID_POL, discrepancy=True,
        reasoning="Same situation as PZ-005: old-generation PD 32428.5 vs new-generation RD 74326.3 (+129%); NEW-generation PD ТЭП not located -> baseline conflict, NEEDS_REVIEW."),
    rec("POL17", "AR-041", "NEEDS_REVIEW", confidence="HIGH", relation="INDETERMINATE",
        pd_refs=[R("POL17-000031", 26, "АР_v5 plan legend", "мин 900 - ширина проема 'в свету'")],
        rd_refs=[R("POL17-000093", 14, "АИ2 legend", "ширина проема 'в свету' мин 900")],
        id_note=NO_ID_POL, discrepancy=None,
        reasoning="Multi-instance parameter (door widths appear only as per-door dimension annotations and a legend 'мин 900 в свету' on the plans); no scalar PD/RD value exists and the "
                  "door-by-door evacuation audit was not performed -> NEEDS_REVIEW."),
]

# ------------------------------------------------------------------ DOO25
NO_ID_DOO = ("DOO25 ID has 779 files, ~72% of DOO25 PDFs have no text layer (see case10_ocr_fallback_hardening); the ID stage was NOT searched -- a limitation, not evidence of absence.")
LABELS += [
    rec("DOO25", "PZ-023", "NO_VIOLATION", confidence="HIGH", pd_value="С0", rd_value="С0", relation="EQUAL",
        pd_refs=[R("DOO25-000793", 7, "АР 2022 text", "Класс конструктивной пожарной опасности - С0"), R("DOO25-000840", 21, "АР Корр1 2025", "Класс конструктивной пожарной опасности - С0"),
                 R("DOO25-000875", 8, "АР Корр2 2025", "Класс конструктивной пожарной опасности - С0")],
        rd_refs=[R("DOO25-000898", 4, "АР2 Общие данные", "класс конструктивной пожарной опасности здания - С0"), R("DOO25-001734", 5, "АР2 Изм2 (к 15.09.2025)", "...С0")],
        id_note=NO_ID_DOO, discrepancy=False, reasoning="C0 in all three PD generations (2022, Корр1, Корр2) and in both RD АР2 sets; degree II in both as well."),
    rec("DOO25", "PZ-001", "NO_VIOLATION", confidence="HIGH", pd_value="1331.4", rd_value="1331.4", relation="EQUAL", delta_pct=0.0,
        pd_refs=[R("DOO25-000791", 12, "ПЗ ТЭП table row 3", "Площадь застройки ДОО м2 1 331,4"), R("DOO25-000792", 8, "ПЗУ ТЭП row 2", "Площадь застройки ДОО м2 1331,4")],
        rd_refs=[R("DOO25-000944", 5, "ГП2 ТЭП", "Площадь застройки корп. 9 м2 1331,4"), R("DOO25-000946", 9, "ПЗУ изм.1 ТЭП row 2", "Площадь застройки ДОО м2 1331,4")],
        id_note=NO_ID_DOO, discrepancy=False, reasoning="Identical footprint area in PD ПЗ/ПЗУ and RD ГП2/ПЗУ."),
]


def main() -> None:
    now = datetime.now(timezone.utc).isoformat()
    seen = set()
    with OUT.open("w", encoding="utf-8") as f:
        for r in LABELS:
            assert r["check_id"] not in seen, r["check_id"]
            seen.add(r["check_id"])
            r["reviewed_at"] = now
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    from collections import Counter
    print(len(LABELS), dict(Counter(r["verdict"] for r in LABELS)), "original-19 covered:",
          sum(r["in_original_19"] for r in LABELS), "of 19")


if __name__ == "__main__":
    main()
