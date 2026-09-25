"""Post-unblinding review of the values the mechanism CITED for every committed (COMPARABLE) group.

Verdict-level truth (labels_source.py) answers "is there a violation"; this file answers the different
question "did the mechanism read a genuine value of THIS parameter on the page it cites?". A verdict can
be right for the wrong reason (e.g. PZ-005/006 on LOS3A compare the TOTAL volume with itself and say
NEGATIVE_VERIFIED). Judgements, per stage, made by opening the cited page (cited page/value are read from
the snapshot, never typed here, so they cannot drift):

    VALID                      genuine statement of the parameter for this object on the cited page
    VALID_NON_AUTHORITATIVE    literal, correctly-extracted statement of the parameter, but from a sheet that is not
                               the parameter's authoritative source and (for PZ-022 RD/МЗ) contradicted by it
    WRONG_QUANTITY             a real number, but of a related-yet-different quantity (total instead of underground,
                               fire-compartment volume, СП 118 area, waste tonnage ...)
    WRONG_OBJECT               real value of the parameter, but of another building / another design generation
    NON_VALUE                  not a physical value at all (clause number, row index, date, page number, sheet count)

Run:  python value_review_source.py   (needs mechanism_snapshots/) -> ../silver_labels/new_objects_value_review_pass1.jsonl
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
SNAP_DIR = HERE / "mechanism_snapshots"
OUT = HERE.parent / "silver_labels" / "new_objects_value_review_pass1.jsonl"
JUDGEMENTS = {"VALID", "VALID_NON_AUTHORITATIVE", "WRONG_QUANTITY", "WRONG_OBJECT", "NON_VALUE"}

# (object, code) -> {"PD": (judgement, note), "RD": (judgement, note)}
J: dict[tuple[str, str], dict[str, tuple[str, str]]] = {}


def j(obj: str, code: str, pd: tuple[str, str], rd: tuple[str, str]) -> None:
    J[(obj, code)] = {"PD": pd, "RD": rd}


# ------------------------------------------------------------------ LOS3A
j("LOS3A", "AR-041",
  ("NON_VALUE", "'7.19' is the clause number in 'требований п 7.19 СП 7.13130.2013' (ИОС5.6 smoke-control text), not a door width"),
  ("NON_VALUE", "'04.25' is a revision date ('Зам 17763-25 04.25') in the title block of АСДКиУ, not a door width"))
j("LOS3A", "KR-067",
  ("WRONG_QUANTITY", "ООС.2 p.19 construction-WASTE table (m3 of wall/partition materials, waste mass) -- not the structural concrete/steel consumption of КР"),
  ("NON_VALUE", "'2 / 10' = number of МГН apartments (2) and a sheet number (10) on the АР4 roof sheet"))
j("LOS3A", "PZ-002",
  ("WRONG_QUANTITY", "16 867,90 is 'Площадь помещений здания (по СП 118.13330.2012, прил. Г.5)', a different quantity from 'Площадь здания' 25 036,27 (ПЗ p.14 row 4)"),
  ("VALID", "АР2 ТЭП row 'Площадь здания м² 25036,27'"))
j("LOS3A", "PZ-004",
  ("VALID", "ПЗ ТЭП row 5 'Строительный объем в т.ч. 88264,00 м3'"),
  ("VALID", "АР3 ТЭП 'Строительный объем в т.ч. м³ 88264,00'"))
j("LOS3A", "PZ-005",
  ("WRONG_QUANTITY", "88264,00 is the TOTAL construction volume; the parameter is the underground part (ниже отм. 0.000 = 13962,18, same ТЭП table)"),
  ("WRONG_QUANTITY", "88264,00 is the TOTAL volume on АР0 p.10; underground part is the sub-row 'Ниже отм. 0.000 13962,18'"))
j("LOS3A", "PZ-006",
  ("WRONG_QUANTITY", "88264,00 is the TOTAL volume; the parameter is the above-ground part (выше отм. 0.000 = 74301,82)"),
  ("WRONG_QUANTITY", "88264,00 is the TOTAL volume; above-ground part is 'Выше отм. 0.000 74301,82'"))
j("LOS3A", "PZ-008",
  ("WRONG_OBJECT", "ООС4.РР1 p.14 'Высота здания до верха 10,00 м' belongs to a neighbouring existing building of the shading study, not to the design object (82,8 m)"),
  ("NON_VALUE", "'2.12.' is a list-item number that follows the phrase 'Высота здания: 70,84-82,84 м.' in the ВК1 notes"))
j("LOS3A", "PZ-022",
  ("VALID", "АР p.8 'Степень огнестойкости здания – I'"),
  ("VALID_NON_AUTHORITATIVE", "МЗ (lightning protection) p.11 says 'Степень огнестойкости здания - II': correctly read, but a copy-paste statement that contradicts the RD АР sheets (I) -- the identical text is also in PD ИОС1.1 p.53/59"))
j("LOS3A", "PZ-023",
  ("VALID", "ПЗ p.18 'Класс конструктивной пожарной опасности – С0'"),
  ("VALID_NON_AUTHORITATIVE", "НК2 (external storm sewers) p.40 quotes the building's fire characteristics 'класс конструктивной пожарной опасности – С0' -- correct content, non-authoritative sheet"))
j("LOS3A", "SPZU-024",
  ("NON_VALUE", "'2.10' is the row number of 'Плодородной почвы на участках озеленения' in the ПЗУ1 earth-mass table"),
  ("WRONG_QUANTITY", "'88,00' is the hazardous-soil part of the 'подземных сетей' row of the ГП earth-mass table, not the 'Итого перерабатываемого грунта' total"))

# ------------------------------------------------------------------ ALT79B
j("ALT79B", "KR-067",
  ("NON_VALUE", "'2 / 2' = the numbering of two drawing notes ('1. Ведомость расхода материалов см. на листе 2. 2. План металлических колонн...') on КР p.45, not a consumption figure"),
  ("NON_VALUE", "same note text on КЖ1 p.4: '2 / 2' are note numbers, not consumption values"))
j("ALT79B", "PZ-023",
  ("VALID", "БЭО p.18 'Класс конструктивной пожарной опасности С0' -- but from the OLD 2024 design generation (ЖС-РД-270121); the 2025 PD АР Изм.1 says С0 too"),
  ("VALID", "АР2 p.3 'класса конструктивной пожарной опасности - С0'"))
j("ALT79B", "SPZU-024",
  ("NON_VALUE", "'158.98' is an elevation mark (abs. level of the geotextile line) in the pit section; the excavation volume on the same drawing is '7 940 м³'"),
  ("NON_VALUE", "same drawing on КЖ01 p.4: '158.98' is an elevation mark, the volume is 7 940 м³"))

# ------------------------------------------------------------------ POL17
j("POL17", "AR-041",
  ("NON_VALUE", "'6.10' is the clause number in 'выходов из технических и вспомогательных помещений 6.10 предусмотрена не менее 0,8 м' (ПБ p.26); the width in that sentence is 0,8 m"),
  ("NON_VALUE", "'8.' is a list-item number in the АР1 notes ('...свободному открыванию 8. к коробке'), not a door width"))
j("POL17", "PZ-005",
  ("WRONG_QUANTITY", "40,95 m is 'Предельная высота' (ТЭП row 9) -- a height, not the underground volume 14945,60 m3"),
  ("WRONG_QUANTITY", "88942,6 m3 is the TOTAL volume of the NEW design generation (ВК3 p.4); its underground part is 14616.3 m3"))
j("POL17", "PZ-006",
  ("WRONG_QUANTITY", "40,95 m is 'Предельная высота' -- a height, not the above-ground volume 32428,5 m3"),
  ("WRONG_QUANTITY", "88942,6 m3 is the TOTAL volume (new generation); above-ground part is 74326.3 m3"))
j("POL17", "PZ-022",
  ("VALID", "ОДИ Корр.5 p.8 'Степень огнестойкости здания - II' (OLD design generation; the new-generation PD АР_v5 says I)"),
  ("VALID_NON_AUTHORITATIVE", "РД КМ p.2 'Степень огнестойкости здания - II' -- correctly read, but the RD АР0-АР4/АИ2/ОВ1 sheets say I"))
j("POL17", "PZ-023",
  ("VALID", "АР_v5 p.7 'Класс конструктивной пожарной опасности – С0'"),
  ("VALID", "АР1 p.3 'Класс конструктивной пожарной опасности – С0'"))

# ------------------------------------------------------------------ DOO25
j("DOO25", "PZ-023",
  ("VALID", "АР 2022 p.7 'Класс конструктивной пожарной опасности - С0'"),
  ("VALID", "АР2 p.4 'класс конструктивной пожарной опасности здания - С0'"))


def main() -> None:
    now = datetime.now(timezone.utc).isoformat()
    rows, missing = [], []
    for p in sorted(SNAP_DIR.glob("snapshot_*.json")):
        d = json.loads(p.read_text(encoding="utf-8"))
        obj = d["object_code"]
        for g in d["groups"]:
            if g["comparability_status"] != "COMPARABLE":
                continue
            code = g["parameter_code"]
            judged = J.get((obj, code))
            if not judged:
                missing.append(f"{obj}::{code}")
                continue
            for f in g["fragments"]:
                stage = {"project": "PD", "working": "RD", "as_built": "ID"}.get(f["stage"])
                if stage not in ("PD", "RD"):
                    continue
                judgement, note = judged[stage]
                assert judgement in JUDGEMENTS, (obj, code, stage, judgement)
                rows.append({
                    "check_id": f"OBJ-NEW-{obj}::{code}", "stage": stage, "cited_file_id": f["dataset_file_id"], "cited_file": f["filename"],
                    "cited_page": f["page"], "cited_value": f["extracted_value"], "mechanism_status": g["finding_status"],
                    "judgement": judgement, "note": note, "reviewed_at": now, "annotation_tier": "SILVER",
                    "labelled_blind_to_current_mechanism_output": False,
                })
    if missing:
        print("MISSING value review for committed groups:", missing)
        sys.exit(1)
    with OUT.open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"{len(rows)} stage judgements written to {OUT}")


if __name__ == "__main__":
    main()
