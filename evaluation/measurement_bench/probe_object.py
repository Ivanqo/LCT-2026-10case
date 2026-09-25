"""Run the labelling probe plan for one object over the ORIGINAL-PDF text cache
(reader.py -- independent of the pipeline).  For every planned (family, code)
attempt it greps the parameter's own semantic patterns (written from the
parameter's meaning, NOT from the catalog string the extractors anchor on, and
before any extractor output was seen) in PD and RD (+ID where cached) and
prints deduplicated hits.  The reviewer then reads the file and writes labels.

  python -m evaluation.measurement_bench.probe_object OBJ [--codes A,B] [--max 7] [--out FILE]
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .common import CORPUS_DIR, OUT_DIR, catalog_by_code
from .reader import load_pages, manifest_rows

PROBES: dict[str, list[str]] = {
    # ENUM
    "PZ-015": [r"категори\w+\s+(надежности\s+)?(электро)?снабжени", r"\b(I|II|III)\s+категори\w+\s+надежности"],
    "PZ-021": [r"класс\w*\s+энерг\w+\s+эффективност", r"класс\s+энергоэффективности"],
    "PZ-022": [r"степен\w+\s+огнестойкост"],
    "PZ-023": [r"класс\w*\s+конструктивной\s+пожарной\s+опасност", r"конструктивн\w+\s+пожарн\w+\s+опасност\w+\s*[-–—]?\s*[СC]"],
    "AR-050": [r"внутренн\w+\s+отделк", r"тип\w*\s+отделк"],
    "KR-056": [r"сталь\s+[СC]\s?\d{3}", r"марк\w+\s+стал", r"класс\w*\s+прочности\s+стал", r"\b[СC](245|255|345|355|390)\b"],
    "KR-057": [r"арматур\w+\s+класса", r"класса\s+[АA]\s?\d{3}", r"\b[АA]\s?(500|400|240|600)\s?[СC]?\b"],
    "PPM-107": [r"класс\w*\s+пожарной\s+опасности\s+(отделоч|материал)", r"\bКМ\s?[0-5]\b"],
    "ZU-124": [r"класс\w*\s+энерг\w+\s+эффективност\w+\s+здани", r"класс\s+энергосбережения"],
    # TABLE_COUNT
    "PZ-010": [r"количество\s+квартир", r"число\s+квартир", r"всего\s+квартир"],
    "PZ-011": [r"однокомнатн", r"двухкомнатн", r"квартирограф", r"студи[ий]"],
    "PZ-012": [r"машино-?мест", r"м/м\b", r"парковочн\w+\s+мест"],
    "SPZU-037": [r"парковочн\w+\s+мест", r"машино-?мест", r"гостев\w+\s+(автостоян|парковк)"],
    "SPZU-038": [r"мест\w*\s+для\s+(МГН|инвалид|маломобильн)", r"(МГН|инвалид)\w*\s+(на\s+)?(машино|парков|мест)"],
    "PPM-110": [r"оповещател", r"СОУЭ"],
    "ODI-120": [r"поручн"],
    "ZU-129": [r"приб(ор|оры)\w*\s+учета"],
    # NUMERIC_TABLE
    "PZ-001": [r"площадь\s+застройки"],
    "PZ-002": [r"общая\s+площадь\s+(здания|объекта)", r"площадь\s+здания"],
    "PZ-003": [r"полезная\s+площадь", r"расчетная\s+площадь"],
    "PZ-004": [r"строительный\s+объ[её]м"],
    "PZ-005": [r"подземн\w+\s+част\w+.{0,40}(м3|м³)", r"ниже\s+отм\.?\s*[+-]?0"],
    "PZ-006": [r"надземн\w+\s+част\w+.{0,40}(м3|м³)", r"выше\s+отм\.?\s*[+-]?0"],
    "PZ-008": [r"высота\s+(здания|объекта)", r"предельная\s+высота"],
    "PZ-019": [r"коэффициент\s+застройки", r"процент\s+застройки"],
    "PZ-020": [r"коэффициент\s+использования\s+территории", r"плотность\s+застройки"],
    "SPZU-024": [r"объ[её]м\w*\s+(грунта|земляных)", r"итого\s+перерабатываем", r"\bвыемк"],
    "SPZU-025": [r"асфальтобетон"],
    "SPZU-026": [r"тротуар", r"плиточн\w+\s+покрыт"],
    "SPZU-027": [r"озеленени\w+.{0,30}(м2|м²|кв\.?\s*м)", r"площадь\s+озеленени", r"газон\w*.{0,30}(м2|м²)"],
    "SPZU-028": [r"(детск|спортивн)\w+\s+площадк\w+.{0,30}(м2|м²)", r"площадь\s+(детск|спортивн)"],
    "SPZU-030": [r"ширин\w+\s+(внутриплощадочн\w+\s+)?(проезд|дорог|автодорог)"],
    "SPZU-031": [r"радиус\w*\s+(поворот|закруглен)"],
    "SPZU-033": [r"уклон\w*\s+(проезд|дорог)", r"продольн\w+\s+уклон"],
    "SPZU-036": [r"ограждени\w+\s+(территори|участк)", r"высот\w+\s+ограждени"],
    "AR-040": [r"ширин\w+\s+(эвакуационн\w+\s+)?коридор"],
    "AR-041": [r"ширин\w+\s+(эвакуационн\w+\s+)?(выход|двер)"],
    "AR-042": [r"высот\w+\s+(путей\s+эвакуации|эвакуационн)", r"высот\w+\s+проем"],
    "AR-047": [r"тамбур\w*.{0,50}(глубин|ширин|размер)", r"глубин\w+\s+тамбур"],
    "AR-049": [r"ограждени\w+\s+(лестниц|балкон|кровл)", r"высот\w+\s+ограждени"],
    "AR-051": [r"\bКЕО\b", r"коэффициент\s+естественной\s+освещенности"],
    "KR-054": [r"шаг\s+колонн", r"сетк\w+\s+колонн", r"шаг\s+осей"],
    "KR-059": [r"толщин\w+\s+(монолитн\w+\s+)?(плит\w*\s+)?перекрыти", r"толщин\w+\s+плит"],
    "KR-060": [r"сечени\w+\s+(колонн|пилон)", r"колонн\w*\s+сечением", r"пилон\w*\s+сечением"],
    "KR-061": [r"толщин\w+\s+(несущих\s+)?(монолитн\w+\s+)?стен"],
    "KR-062": [r"диаметр\w*\s+(продольной\s+|рабочей\s+)?арматур"],
    "KR-064": [r"лифтов\w+\s+шахт\w*.{0,60}\d{3,4}"],
    "KR-065": [r"технологическ\w+\s+проем"],
    "IOS1-069": [r"сечени\w+\s+жил", r"\b(ВВГ|АВВГ|ВВГнг|NYM)\S*\s*\d"],
    "IOS2-071": [r"стояк\w*\s+(В1|Т3|холодн|горяч).{0,30}(Ду|d\s*=|диаметр|\d{2})"],
    "IOS3-074": [r"выпуск\w*.{0,30}(Ду|d\s*=|диаметр)"],
    "IOS4-076": [r"(магистрал|стояк)\w+\s+отоплен"],
    "POS-081": [r"опасн\w+\s+зон\w+.{0,60}(кран|груз)", r"зон\w+\s+(действия|работы)\s+кран"],
    "POS-083": [r"временн\w+\s+(здани|сооружени|бытов)"],
    "POS-084": [r"временн\w+\s+(автодорог|дорог)"],
    "POD-090": [r"опасн\w+\s+зон\w+.{0,50}(обруш|развал|демонтаж)"],
    "POD-093": [r"объ[её]м\w*\s+демонтир", r"демонтируем\w+.{0,40}(м3|м³)"],
    "POD-097": [r"склад\w+\s+(лом|мусор)", r"площадк\w+.{0,30}лом"],
    "PPM-104": [r"ширин\w+\s+эвакуационн\w+\s+проход"],
    "PPM-105": [r"ширин\w+\s+эвакуационн\w+\s+двер"],
    "ODI-116": [r"ширин\w+\s+коридор", r"коридор\w*.{0,40}(МГН|инвалид)"],
    "ODI-117": [r"ширин\w+\s+дверн\w+\s+проем"],
    "ODI-118": [r"высот\w+\s+порог"],
    "ZU-125": [r"толщин\w+\s+(теплоизоляц|утеплител)", r"утеплител\w+.{0,40}\d{2,3}\s*мм"],
    "ZU-128": [r"утеплител\w+.{0,40}(чердач|кровл)", r"толщин\w+.{0,40}(чердач|кровл)"],
    # COMPOUND
    "SPZU-029": [r"\bМАФ\b", r"малы\w+\s+архитектурн\w+\s+форм"],
    "AR-045": [r"уклон\w*\s+кровл"],
    "AR-046": [r"оконн\w+\s+блок", r"ведомость\s+(окон|заполнени)"],
    "AR-048": [r"лестничн\w+\s+марш", r"ступен\w+.{0,30}(мм|шт)"],
    "KR-067": [r"ведомость\s+расхода", r"расход\w*\s+(бетона|стали|материал)", r"общий\s+расход"],
    "IOS1-070": [r"заземлен\w+.{0,40}(Ом|мм)", r"молниезащит"],
    "IOS2-073": [r"насосн\w+\s+станц", r"насос\w*.{0,40}(м3/ч|кВт)"],
    "IOS4-077": [r"радиатор", r"отопительн\w+\s+прибор"],
    "IOS5-080": [r"пожарн\w+\s+сигнализац", r"\bАПС\b"],
    "POS-088": [r"временн\w+\s+(электро|водо)", r"подключен\w+\s+временн"],
    "PPM-108": [r"извещател"],
    "PPM-112": [r"подпор\w+\s+воздух", r"вентилятор\w*.{0,30}(дымоудал|подпор)"],
    "PPM-113": [r"внутренн\w+\s+противопожарн\w+\s+водопровод", r"\bВПВ\b", r"струй"],
    "ODI-119": [r"санузел\w*.{0,40}(МГН|инвалид)", r"универсальн\w+\s+кабин"],
    "ODI-121": [r"парковочн\w+\s+мест\w*.{0,40}(МГН|инвалид)", r"мест\w*\s+для\s+(МГН|инвалид)"],
}


def _norm(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip().lower())


def run(obj: str, codes: list[str] | None, max_hits: int, stages: tuple[str, ...]) -> str:
    plan = json.loads((CORPUS_DIR / "probe_plan.json").read_text(encoding="utf-8"))
    planned: list[tuple[str, str]] = []
    for fam, lst in plan["attempts"][obj].items():
        planned += [(fam, c) for c in lst]
    if codes:
        planned = [(f, c) for f, c in planned if c in codes] + [("EXTRA", c) for c in codes if c not in {c2 for _f, c2 in planned}]
    rows = [r for r in manifest_rows(obj) if r["stage"] in stages]
    cat = catalog_by_code()
    # load every cached page once
    cache: dict[str, list[str]] = {}
    for r in rows:
        try:
            cache[r["file_id"]] = load_pages(r["file_id"])
        except FileNotFoundError:
            pass
    out: list[str] = []
    for fam, code in planned:
        pats = PROBES.get(code)
        out.append(f"\n========== [{obj}] {fam} {code} | {cat[code]['parameter_name']} | unit={cat[code].get('unit')}")
        if not pats:
            out.append("  (no probe pattern defined)")
            continue
        for stage in stages:
            seen: dict[str, list[str]] = {}
            order: list[str] = []
            for r in rows:
                if r["stage"] != stage or r["file_id"] not in cache:
                    continue
                for pno, text in enumerate(cache[r["file_id"]], 1):
                    for ln in text.split("\n"):
                        if any(re.search(p, ln, re.I) for p in pats):
                            key = _norm(ln)
                            if key not in seen:
                                seen[key] = []
                                order.append(key)
                            seen[key].append(f"{r['file_id'].split('-')[-1]}p{pno}")
            out.append(f"  --- {stage}: {len(order)} distinct hit lines")
            shown = 0
            for key in order:
                if shown >= max_hits:
                    out.append(f"     ... +{len(order) - shown} more")
                    break
                locs = seen[key]
                out.append(f"     [{','.join(locs[:3])}{'+%d' % (len(locs) - 3) if len(locs) > 3 else ''}] {key[:170]}")
                shown += 1
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("obj")
    ap.add_argument("--codes")
    ap.add_argument("--max", type=int, default=7)
    ap.add_argument("--stages", default="PD,RD")
    ap.add_argument("--out")
    a = ap.parse_args()
    text = run(a.obj, a.codes.split(",") if a.codes else None, a.max, tuple(a.stages.split(",")))
    out = Path(a.out) if a.out else OUT_DIR / f"probe_{a.obj}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"-> {out} ({len(text)} chars)")


if __name__ == "__main__":
    main()
