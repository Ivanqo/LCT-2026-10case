"""Hand-labelled value corpus (SILVER: single annotator = same model family as
the system under test; second review PENDING; training_eligible = false).

Every entry was read from the ORIGINAL PDFs through `reader.py` / the
`probe_object.py` plan (independent of the pipeline) BEFORE any extractor was
run on the corpus.  `verify_labels.py` re-checks each cited (file, page)
against the text cache (value token + line fragment present) so a typo cannot
silently become ground truth.

Vocabulary
  V(...)  one (object, parameter, stage) triple with a ground-truth value
  P(...)  a documented search attempt that produced NO scalar truth
          (ABSENT / NOT_SCALAR / NOT_APPLICABLE / CONFLICT) -- kept so the
          denominator of "what a reader could find" is public (anti-selection)
  prov    PLAN      drawn by the seeded probe plan (probe_plan.json)
          TEP_CORE  supplement: high-presence TEP-table parameters chosen by me
          PASS1     re-used from the blind SILVER verdict rows of pass 1
                    (`selected_by_system_output` = True for the 19 mechanism-
                    selected groups, False for the 5 TEP-extension rows)
          FORENSIC  LOS3A forensic cases (PZ-001/002/004 fixes)
  any_of  the parameter legitimately has several values in the document; a
          reading is correct if it equals any of them (truth_kind SET_ANY)
  sup     values found in a SUPERSEDED design generation / conflicting place
          (used only to classify wrong readings, never to accept them)
"""
from __future__ import annotations

from typing import Any

LABELS: list[dict[str, Any]] = []
PROBE_LOG: list[dict[str, Any]] = []
_seen: set[tuple[str, str, str]] = set()


def _fid(obj: str, file_no: int | str) -> str:
    return f"{obj}-{int(file_no):06d}" if isinstance(file_no, int) else str(file_no)


def V(
    obj: str, code: str, stage: str, value: Any, file_no: int | str, page: int, line: str, *,
    alt: tuple = (), sup: tuple = (), any_of: tuple = (), conf: str = "HIGH", prov: str = "", note: str = "",
    selected_by_system: bool = False,
) -> None:
    # `prov` / `selected_by_system` are advisory only: the builder recomputes the
    # provenance mechanically (in the probe plan -> PLAN; in the 19 pass-1
    # mechanism-selected groups -> PASS1_MECHANISM; otherwise SUPPLEMENT), so a
    # hand-typed tag cannot misstate how a triple was selected.
    key = (obj, code, stage)
    assert key not in _seen, f"duplicate label {key}"
    _seen.add(key)
    LABELS.append({
        "obj": obj, "code": code, "stage": stage, "value": value, "file_id": _fid(obj, file_no), "page": page, "line": line,
        "alt": [{"file_id": _fid(obj, f), "page": p, "value": v} for f, p, v in alt],
        "sup": [{"file_id": _fid(obj, f), "page": p, "value": v, "note": n} for f, p, v, n in sup],
        "any_of": [{"file_id": _fid(obj, f), "page": p, "value": v, "note": n} for f, p, v, n in any_of],
        "conf": conf, "supplement_kind": prov, "note": note,
    })


def P(obj: str, code: str, status: str, note: str, *, stages: str = "PD,RD", sup: tuple = (), prov: str = "") -> None:
    """status: ABSENT | NOT_SCALAR | NOT_APPLICABLE | CONFLICT | EMPTY_PLACEHOLDER"""
    PROBE_LOG.append({
        "obj": obj, "code": code, "status": status, "stages": stages.split(","), "note": note, "prov": prov,
        "sup": [{"file_id": _fid(obj, f), "page": p, "value": v, "note": n} for f, p, v, n in sup],
    })


# ==================================================================== ALT79B
# Two design generations live in the PD folder: the OLD 2024 series
# (ЖС-РД-270121-*: 2 floors, 34754 m3, degree II) and the NEW 2025 series
# (П-2025-04.266-*: 37076 m3, degree III) that the RD (АР1/АР2, КЖ, КМ) follows.
# Truth = the NEW generation; old-generation values are `sup`.
_A = "ALT79B"
V(_A, "PZ-001", "PD", "3009.4", 1, 10, "Площадь застройки объекта м2 3009.4", alt=((15, 10, "3009,4"), (12, 11, "3009,40")),
  sup=((2, 16, "2909,5", "old 2024 generation, после реконструкции"), (13, 12, "2909,5", "old 2024 ПЗУ")), prov="TEP_CORE")
P(_A, "PZ-001", "ABSENT", "RD АР1/АР2/КЖ/КМ/ВК/ОВ carry no TEP table; grep 'площадь застройки' over RD = 0 hits", stages="RD", prov="TEP_CORE")
V(_A, "PZ-002", "PD", "5825.8", 1, 10, "Общая площадь объекта, в т.ч.: м2 5825.8", alt=((15, 10, "5825.8"),), prov="TEP_CORE")
P(_A, "PZ-002", "ABSENT", "no total area in RD text", stages="RD", prov="TEP_CORE")
V(_A, "PZ-004", "PD", "37076.0", 1, 10, "Общий строительный объем зданий, в т.ч.: м3 37 076,0", alt=((15, 10, "37076,0"),),
  sup=((2, 16, "34754", "old 2024 generation"), (61, 63, "44454,00", "old 2024 ПОС")))
P(_A, "PZ-004", "ABSENT", "no building volume in RD text", stages="RD")
V(_A, "PZ-005", "PD", "0", 1, 10, "- подземная часть м3 0", note="true zero (no basement); a genuine 0, not an unfilled placeholder", prov="TEP_CORE")
P(_A, "PZ-005", "ABSENT", "no underground volume in RD", stages="RD", prov="TEP_CORE")
V(_A, "PZ-006", "PD", "37076.0", 1, 10, "- наземная часть м3 37 076,0", note="label is 'наземная часть' (not 'надземная')")
P(_A, "PZ-006", "ABSENT", "no above-ground volume in RD", stages="RD")
P(_A, "PZ-019", "ABSENT", "current (2025) PD has no coverage %, only OLD-generation 'Процент застройки (после реконструкции) 31'", stages="PD,RD",
  sup=((2, 16, "31", "old 2024 generation"),))
P(_A, "PZ-020", "ABSENT", "only OLD 'плотность застройки 6,2 тыс.кв.м/га' (different quantity/unit)")
V(_A, "PZ-022", "PD", "III", 15, 10, "Степень огнестойкости здания – III;", alt=((4, 4, "III"), (7, 4, "III"), (19, 6, "III"), (19, 20, "III")),
  sup=((10, 10, "II", "old 2024 БЭО"), (22, 27, "II", "old 2024 КР"), (23, 15, "II", "old КР")))
V(_A, "PZ-022", "RD", "III", 76, 3, "3. Проектируемое здание относится ко III степени огнестойкости", alt=((77, 3, "III"),))
V(_A, "PZ-023", "PD", "C0", 15, 9, "Класс конструктивной пожарной опасности - С0;", alt=((4, 4, "С0"),), conf="MEDIUM",
  sup=((19, 6, "С1", "PD КР contradicts АР inside the same generation"), (19, 20, "С1", "PD КР")),
  note="PD is internally inconsistent (АР/ОДИ С0 vs КР С1); truth taken from АР (architect's sheet)")
V(_A, "PZ-023", "RD", "C0", 77, 3, "конструктивной пожарной опасности - С0.", alt=((76, 3, "С0"),))
V(_A, "KR-057", "PD", "A500C", 19, 19, "ростверки - монолитные железобетонные из бетона В25 F150 W8, арматура кл. А500С по ГОСТ",
  alt=((19, 25, "А500С"),), note="Cyrillic 'А' and 'С' in print; canonical A500C")
V(_A, "KR-057", "RD", "A500C", 69, 11, "ГОСТ 34028-2016 12 А500С l=1100", alt=((69, 3, "А500С"),))
V(_A, "KR-056", "PD", "C345", 19, 19, "навес запроектирован из стальных колонн 25К1 из стали марки С345", any_of=((19, 19, "С345", "frame steel"),),
  conf="MEDIUM", note="steel grade of the main frame; secondary elements may differ")
V(_A, "KR-056", "RD", "C345", 72, 2, "4.1. Материал стальных элементов - сталь С 345.", any_of=((72, 2, "С345", "КМ"), (69, 2, "С245", "КЖ01 foundation embedded parts")),
  conf="MEDIUM", note="two RD sets state different grades for different element groups (КМ С345, КЖ01 закладные С245)")
V(_A, "SPZU-024", "PD", "7940", 19, 35, "Объем выемки грунта усреднен - 7 940 м³", any_of=((19, 35, "7 940", "pit volume"), (12, 18, "5123.0", "СПОЗУ site balance выемка"), (12, 18, "2187.9", "СПОЗУ site balance насыпь")),
  prov="PASS1", conf="MEDIUM", note="pass-1 verdict row: like-for-like pit volume; the parameter has several totals", selected_by_system=True)
V(_A, "SPZU-024", "RD", "7940", 69, 4, "Объем выемки грунта усреднен - 7 940 м³", alt=((70, 4, "7 940"),), prov="PASS1", conf="MEDIUM", selected_by_system=True)
P(_A, "PZ-003", "NOT_SCALAR", "'расчетная площадь' in ЭЭ is a heat-loss design area (5559,7) not the ТЭП usable area", prov="TEP_CORE", stages="PD,RD")
P(_A, "PZ-008", "CONFLICT", "PD states three heights: предельная +16.580, по капитальной части +15.720, верхняя отм. +16.400; RD has none", stages="PD,RD", prov="TEP_CORE")
P(_A, "SPZU-037", "CONFLICT", "СПОЗУ p12 says 108 м/м (АС1 41 + АС2 39 + АС3 28) but ОДИ p7 says 64 машиномест", stages="PD,RD")
P(_A, "SPZU-038", "CONFLICT", "МГН spaces: ОДИ 7+3 (calc) / 4 (designed); СПОЗУ 4; old ОДИ 8", stages="PD,RD")
P(_A, "PZ-010", "NOT_APPLICABLE", "commercial building, no apartments", stages="PD,RD")
P(_A, "PZ-011", "NOT_APPLICABLE", "commercial building, no apartments", stages="PD,RD")
P(_A, "PZ-012", "NOT_APPLICABLE", "no underground parking (подземная часть 0)", stages="PD,RD")
P(_A, "PPM-110", "ABSENT", "no countable СОУЭ specification table in text layer (72 prose hits only)")
P(_A, "ODI-120", "ABSENT", "no handrail specification table (9 prose requirement hits)")
P(_A, "ZU-129", "ABSENT", "no metering-device specification table (135 prose hits)")
P(_A, "PZ-021", "ABSENT", "ПЗ p11: 'класс энергоэффективности: не классифицируется' -- text, not a class letter")
P(_A, "ZU-124", "ABSENT", "only OLD-generation ЭЭ 'класс энергосбережения С'; current ПЗ says 'не классифицируется'",
  sup=((8, 56, "C", "old 2024 ЭЭ"),))
P(_A, "PPM-107", "NOT_SCALAR", "КМ1..КМ4 assigned per room type in ПБ (several classes)")
P(_A, "AR-050", "NOT_SCALAR", "finishing described per room type; no single class")
P(_A, "KR-054", "ABSENT", "no column-grid step text in PD/RD text layer")
P(_A, "KR-062", "ABSENT", "no 'диаметр рабочей арматуры колонн' statement")
P(_A, "PPM-105", "ABSENT", "no 'ширина эвакуационных дверей' scalar")
P(_A, "POD-093", "ABSENT", "no demolition volume list in text")
P(_A, "IOS1-069", "NOT_SCALAR", "cable schedule lists dozens of cable sections")
P(_A, "KR-065", "ABSENT", "no technological-opening statement")
P(_A, "POS-084", "NOT_SCALAR", "'временные дороги - 1905 м²' is an area; width not stated")
P(_A, "IOS3-074", "ABSENT", "no diameter of sewer outlets in text")
P(_A, "AR-048", "NOT_SCALAR", "several stair flights; not a scalar (count, mm) pair")
P(_A, "PPM-112", "NOT_SCALAR", "fan capacities per system, prose only")
P(_A, "SPZU-029", "ABSENT", "only OLD-generation ПЗУ has a MAF statement; current СПОЗУ none")
P(_A, "PPM-113", "NOT_SCALAR", "fire-hydrant flow per system, prose only")
P(_A, "AR-045", "ABSENT", "only 'уклон кровли 1,7%' (one number) -- no % / ° pair")
P(_A, "KR-067", "NOT_SCALAR", "steel consumption per element/per set only (pass 1: no building total)")


# ==================================================================== POL17
# PD folder mixes two design generations: OLD (11+1 floors, 100 apartments,
# 47374.1 m3, degree II: ПЗ2/ПБ/КР1/ИОС*/ЭЭ) and NEW (20/13 floors, 248
# apartments, degree I: АР_v5 = POL17-000031).  RD (АР0-4, ВК3, НС, НВ.3, КЖ*)
# follows the NEW generation, but no NEW-generation PD TEP table was found:
# for PZ-001/002/004/005/006/010 the current PD value is ABSENT and the old
# values are recorded as `sup`.
_P = "POL17"
V(_P, "PZ-004", "RD", "88942.6", 102, 4, "Строительный объём здания 88942,6 м³,", alt=((127, 2, "88942,6"),))
P(_P, "PZ-004", "ABSENT", "current-generation PD has no TEP table; only OLD ПЗ2 p10 'строительный объём 47374,10 м³'", stages="PD",
  sup=((71, 10, "47374.10", "old generation ПЗ2 TEP"), (39, 6, "47374.1", "old ИОС2.1")))
V(_P, "PZ-005", "RD", "14616.3", 102, 4, "― подземной части 14616.3 м³;", alt=((127, 2, "14616,3"), (128, 4, "14616.3")))
P(_P, "PZ-005", "ABSENT", "current PD TEP absent", stages="PD", sup=((71, 10, "14945.60", "old generation ПЗ2"), (39, 6, "14945.6", "old ИОС2.1")))
V(_P, "PZ-006", "RD", "74326.3", 102, 4, "― надземной части 74326.3 м³.", alt=((127, 2, "74326,3"), (128, 4, "74326.3")))
P(_P, "PZ-006", "ABSENT", "current PD TEP absent", stages="PD", sup=((71, 10, "32428.5", "old generation ПЗ2"), (39, 6, "32428,5", "old ИОС2.1")))
V(_P, "PZ-010", "RD", "248", 102, 4, "количество квартир – 248 кв.;", alt=((127, 2, "248"), (128, 4, "248")))
P(_P, "PZ-010", "ABSENT", "current PD has no apartment count in text layer; OLD ПЗ2 p10 'количество квартир, 100шт.'", stages="PD",
  sup=((71, 10, "100", "old generation ПЗ2"), (65, 18, "100", "old ООС")), prov="")
V(_P, "PZ-012", "PD", "38", 31, 8, "38 машино-мест хранения общей площадью 503,5 м2", note="АР_v5 (current generation), underground car park")
V(_P, "PZ-012", "RD", "38", 93, 5, "лоток водоотвода паркинга на 38 м/м", alt=((95, 6, "38"), (104, 9, "38")))
V(_P, "PZ-022", "PD", "I", 31, 7, "Степень огнестойкости – I (ст.30 №123-ФЗ);", sup=((68, 10, "II", "old ПБ Корр.5"), (71, 38, "II", "old ПЗ2"), (64, 8, "II", "old ОДИ")))
V(_P, "PZ-022", "RD", "I", 95, 3, "3. Степень огнестойкости здания - I (ст.30 №123-ФЗ).", alt=((96, 3, "I"), (97, 3, "I")))
V(_P, "PZ-023", "PD", "C0", 31, 7, "Класс конструктивной пожарной опасности – С0 (ст.31 №123-ФЗ)", alt=((68, 10, "С0"), (71, 38, "С0")))
V(_P, "PZ-023", "RD", "C0", 95, 3, "класс конструктивной пожарной опасности – С0 (ст.31 №123-ФЗ).", alt=((96, 3, "С0"),))
V(_P, "PZ-008", "PD", "68.41", 31, 10, "Предельная высота здания – 68,41 м (70,00 м. – по ГПЗУ).", sup=((71, 10, "40.95", "old generation ПЗ2 'предельная высота 40,95 м'"),),
  note="70,00 is the GPZU limit, not the design value")
P(_P, "PZ-008", "ABSENT", "no building-height statement in RD text", stages="RD")
V(_P, "KR-057", "RD", "A500C", 111, 2, "6.Характеристика здания : · Ø20 А500С - 970 мм;", alt=((111, 8, "А500С"),))
P(_P, "KR-057", "CONFLICT", "PD КР1/АР_МГЭ (old generation) 'арматуры класса А500С и А240'; no current-generation PD КР", stages="PD")
V(_P, "KR-056", "RD", "C245", 114, 5, "50 t=180 С245 ГОСТ27772-2015", any_of=((124, 2, "С235", "КМ 'сталь С 235'"), (125, 4, "С245", "КМ1")), conf="MEDIUM",
  note="different RD sets state С245 (КЖ0.3.1, КМ1) and С235 (КМ)")
P(_P, "KR-056", "ABSENT", "PD: no steel grade statement", stages="PD")
V(_P, "SPZU-024", "RD", "28108", 105, 7, "8. Итого перерабатываемого грунта 28108 28108", note="насыпь = выемка = 28108 (ГП1 'Ведомость объемов земляных масс')")
P(_P, "SPZU-024", "ABSENT", "no earth-mass sheet found in PD text (old-generation ПЗУ has none either)", stages="PD")
P(_P, "PZ-001", "ABSENT", "current PD TEP absent; OLD ПЗ2 p10 'площадь застройки 943,0м2'", stages="PD,RD", prov="TEP_CORE", sup=((71, 10, "943.0", "old generation ПЗ2"),))
P(_P, "PZ-002", "ABSENT", "current PD TEP absent; OLD ПЗ2 'общая площадь здания 11691,90м2' (был 11744,50)", stages="PD,RD", sup=((71, 10, "11691.90", "old ПЗ2"), (72, 14, "11744.50", "old ПЗ2 МГЭ")))
P(_P, "PZ-003", "NOT_SCALAR", "'расчетная площадь' hits are irrigation/asphalt design areas")
P(_P, "PZ-019", "ABSENT", "'процент застройки' only as 'не установлен' in ГПЗУ table")
P(_P, "SPZU-025", "NOT_SCALAR", "asphalt areas per element in ООС/ПОС tables; no single total")
P(_P, "SPZU-033", "ABSENT", "one ОДИ requirement text without a value")
P(_P, "POS-084", "NOT_SCALAR", "'временная дорога м² 1112' is an area; no width")
P(_P, "ZU-128", "ABSENT", "no attic/roof insulation thickness statement (only mass tables)")
P(_P, "AR-047", "NOT_SCALAR", "vestibule areas per room in АР plans; no single depth")
P(_P, "POD-093", "ABSENT", "no demolition volumes")
P(_P, "IOS2-071", "ABSENT", "no riser diameters in text")
P(_P, "PZ-011", "NOT_SCALAR", "apartment mix (1к 112 / 2к 105 / 3к 31) is a triple, not a row count", stages="RD")
P(_P, "PZ-011", "NOT_SCALAR", "old PD: per-floor mix only", stages="PD")
P(_P, "SPZU-037", "NOT_SCALAR", "open-parking count not stated as a scalar in text", stages="PD,RD")
P(_P, "SPZU-038", "CONFLICT", "'3 м/мест для инвалидов-колясочников' only in OLD ОДИ Корр.5; ПБ/ОДИ old-generation", stages="PD,RD")
P(_P, "PPM-110", "NOT_SCALAR", "prose/wiring hits only")
P(_P, "ODI-120", "NOT_SCALAR", "handrail requirement prose")
P(_P, "ZU-129", "ABSENT", "no metering-device specification table in text")
P(_P, "AR-050", "NOT_SCALAR", "finishing per room type")
P(_P, "PPM-107", "NOT_SCALAR", "КМ0..КМ5 assigned per room type")
P(_P, "ZU-124", "ABSENT", "class 'А+' only in old-generation ЭЭ изм.4 (100 apartments); no current statement", sup=((79, 46, "A+", "old-generation ЭЭ"),))
P(_P, "PZ-021", "ABSENT", "class letter only in old-generation ЭЭ", sup=((79, 51, "A+", "old-generation ЭЭ"),))
P(_P, "AR-048", "NOT_SCALAR", "stair flights per staircase")
P(_P, "AR-046", "NOT_SCALAR", "window schedule per type")
P(_P, "PPM-108", "NOT_SCALAR", "detector details per room")
P(_P, "ODI-121", "CONFLICT", "МГН parking only in OLD ОДИ Корр.5")
P(_P, "IOS5-080", "NOT_SCALAR", "prose")
P(_P, "IOS1-070", "NOT_SCALAR", "prose")
