"""Render the bench outputs (OUT_DIR/*.json) as markdown tables -> evaluation/reports/measurement_bench/tables.md.
Used to compose MEASUREMENT_BENCH_REPORT.md without hand-copying numbers.

  python -m evaluation.measurement_bench.render_tables
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from typing import Any

from .common import CORPUS_DIR, OUT_DIR, REPORT_DIR, read_jsonl
from .location_convention import observed_conventions


def _r(x: dict | None) -> str:
    if not x or x.get("n") in (0, None):
        return "—"
    lo, hi = x["wilson95"]
    return f"{x['k']}/{x['n']} = {100 * x['rate']:.0f}% [{100 * lo:.0f}; {100 * hi:.0f}]"


def _load(name: str) -> Any:
    p = OUT_DIR / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def corpus_tables() -> list[str]:
    m = json.loads((CORPUS_DIR / "corpus_manifest.json").read_text(encoding="utf-8"))
    s = m["summary"]
    L = ["## Корпус", "", f"Заморожен {m['frozen_at']} (sha256 `{m['corpus_sha256'][:16]}…`), троек: **{s['n_triples']}**, holdout {s['by_split'].get('holdout')} ({100 * s['holdout_share']:.1f}%), dev {s['by_split'].get('dev')}.", "",
         "| объект | split | чистота holdout | троек PD | RD | ID |", "|---|---|---|---|---|---|"]
    rows = read_jsonl(CORPUS_DIR / "value_corpus_v1.jsonl")
    for obj in sorted({r["obj"] for r in rows}):
        rr = [r for r in rows if r["obj"] == obj]
        L.append(f"| {obj} | {rr[0]['split']} | {rr[0]['cleanliness']} | {sum(r['stage'] == 'PD' for r in rr)} | {sum(r['stage'] == 'RD' for r in rr)} | {sum(r['stage'] == 'ID' for r in rr)} |")
    L += ["", "| семейство | троек | из них holdout | SET_ANY (несколько допустимых значений) |", "|---|---|---|---|"]
    for fam in ("ENUM", "NUMERIC_TABLE", "COMPOUND", "TABLE_COUNT"):
        rr = [r for r in rows if r["family"] == fam]
        L.append(f"| {fam} | {len(rr)} | {sum(r['split'] == 'holdout' for r in rr)} | {sum(r['truth_kind'] == 'SET_ANY' for r in rr)} |")
    L += ["", f"Провенанс: {s['by_provenance']}; выбрано по выводу системы: {s['selected_by_system_output']}.", "",
          f"Журнал попыток без скалярной истины (знаменатель «что можно найти чтением»): {s['probe_log_by_status']} (из них по плану {s['probe_log_planned_by_status']}).", ""]
    return L


def metric1_tables() -> list[str]:
    d = _load("metric1_summary.json")
    if not d:
        return []
    S = d["summary"]
    L = ["## Метрика 1 — точность извлечения по семействам", "", "Значение верно = значение экстрактора для (объект, параметр, стадия) совпало с допустимым по разметке; в скобках 95% интервал Уилсона.", "",
         "| выборка | семейство | n | значение верно | значение и страница верны |", "|---|---|---|---|---|"]
    for split, label in (("holdout", "holdout"), ("holdout_clean_only", "holdout, чистые объекты (OKT103, IZM12)"), ("dev", "dev"), ("all", "все")):
        for fam in ("all_families", "ENUM", "NUMERIC_TABLE", "COMPOUND", "TABLE_COUNT"):
            b = S[split].get(fam)
            if b:
                L.append(f"| {label} | {fam} | {b['n']} | {_r(b['value_correct'])} | {_r(b['value_and_page_correct'])} |")
    b = S["holdout_system_independent_selection"]["all_families"]
    L += ["", f"Holdout без троек, выбранных по выводу механизма (PASS1_MECHANISM_SELECTED исключены): n={b['n']}, значение верно {_r(b['value_correct'])}.", "",
          "### Таксономия ошибок (holdout)", "", "| семейство | CORRECT | NOT_TAGGED | NO_VALUE | WRONG_VALUE | WRONG_ROW | CITATION_NUMBER | ORDINAL | GATED |", "|---|---|---|---|---|---|---|---|---|"]
    for fam in ("all_families", "ENUM", "NUMERIC_TABLE", "COMPOUND", "TABLE_COUNT"):
        b = S["holdout"][fam]
        tax = b["taxonomy"]
        L.append(f"| {fam} | {b['value_correct']['k']} | " + " | ".join(str(tax.get(c, 0)) for c in ("NOT_TAGGED", "NO_VALUE", "WRONG_VALUE", "WRONG_ROW", "CITATION_NUMBER", "ORDINAL", "GATED")) + " |")
    L += ["", "### Подпричины (holdout, все семейства)", "", "| класс/подпричина | троек |", "|---|---|"]
    for k, v in sorted(S["holdout"]["all_families"]["taxonomy_sub"].items(), key=lambda kv: -kv[1]):
        L.append(f"| {k} | {v} |")
    L += ["", "### По объектам", "", "| объект | n | значение верно | из них NOT_TAGGED |", "|---|---|---|---|"]
    for o, b in S["by_object"].items():
        L.append(f"| {o} | {b['n']} | {_r(b['value_correct'])} | {b['taxonomy'].get('NOT_TAGGED', 0)} |")
    L += ["", "### По стадии (holdout)", "", "| стадия | n | значение верно |", "|---|---|---|"]
    for s, b in S["by_stage_holdout"].items():
        if b["n"]:
            L.append(f"| {s} | {b['n']} | {_r(b['value_correct'])} |")
    L += ["", f"«Ошибки выбора»: в {S['selection_errors_holdout']} holdout-случаях экстрактор в изоляции читает верное значение с истинной страницы, а конвейер выдал другое (выбор кандидата)."]
    res = (_load("metric1_results.json") or {}).get("results", [])
    if res:
        hold = [r for r in res if r["split"] == "holdout"]
        iso = sum(1 for r in hold if r.get("isolated_correct"))
        L += ["", f"Потолок при идеальной локализации: на истинной странице собственный поиск экстрактора читает верное значение в {iso} из {len(hold)} holdout-троек "
                  f"({100 * iso / max(1, len(hold)):.0f}%); остальные — экстрактор не находит якорь/значение и на верной странице (словарь формулировок, разбор строки/столбца)."]
        from collections import defaultdict

        by: dict = defaultdict(dict)
        for r in res:
            by[(r["obj"], r["param_code"])][r["stage"]] = r
        L += ["", "### Пары (объект, параметр) с истиной и на ПД, и на РД: найдены ли обе стороны верно", "",
              "Именно эта доля — первый множитель итогового recall: пару нельзя сравнить, если хотя бы одна сторона извлечена неверно.", "",
              "| выборка | пар с истиной на ПД и РД | обе стороны верны | только ПД верна | только РД верна |", "|---|---|---|---|---|"]
        for name, pred in (("holdout", lambda v: v["PD"]["split"] == "holdout"), ("dev", lambda v: v["PD"]["split"] == "dev"), ("все", lambda v: True)):
            pairs = [v for v in by.values() if "PD" in v and "RD" in v and pred(v)]
            n = len(pairs)
            both = sum(1 for v in pairs if v["PD"]["status"] == "CORRECT" and v["RD"]["status"] == "CORRECT")
            pd_only = sum(1 for v in pairs if v["PD"]["status"] == "CORRECT" and v["RD"]["status"] != "CORRECT")
            rd_only = sum(1 for v in pairs if v["RD"]["status"] == "CORRECT" and v["PD"]["status"] != "CORRECT")
            from .metric1 import rate

            L.append(f"| {name} | {n} | {_r(rate(both, n))} | {pd_only} | {rd_only} |")
    return L + [""]


def seed_tables() -> list[str]:
    l1 = _load("l1_results.json")
    if not l1:
        return []
    L = ["## Метрика 2 — посевы нарушений", "", f"Посев: seed `{20260924}`, хэш списка кейсов `{l1.get('seed_manifest_sha256', '')[:16]}…`; кейсов: {len(l1['rows'])}.", ""]
    for key, title in (("summary", "Экстракторы + исходная функция сравнения (не зависит от слоя решения B)"), ("summary_protocol", "Протокольный уровень (create_official_evidence_groups дерева кода в замере)")):
        S = l1.get(key)
        if not S:
            L += [f"### {title}", "", f"недоступно: {l1.get('protocol_unavailable')}", ""]
            continue
        L += [f"### {title}", "", "| срез | пар | must-catch | recall (метка) | recall (метка+evidence) | recall (метка+evidence+location) | must-not-catch | FP-rate |", "|---|---|---|---|---|---|---|---|"]
        for name, b in S.items():
            if name == "layer":
                continue
            L.append(f"| {name} | {b['n_pairs']} | {b['n_must_catch']} | {_r(b['recall_label_only'])} | {_r(b['recall_label_and_evidence'])} | {_r(b['recall_label_evidence_location'])} | {b['n_must_not_catch']} | {_r(b['fp_rate'])} |")
        L.append("")
    l2 = _load("l2_results.json")
    if l2:
        s = l2["summary"]
        L += ["### L2 — реальная правка PDF", "", f"Попыток {s['attempted']}, правка подтверждена (новый токен извлекается на старом месте) {s['verified_edits']} ({100 * (s['verified_rate'] or 0):.0f}%); порог хрупкости {100 * s['fragility_threshold']:.0f}% → "
              f"{'**L2 признан хрупким и НЕ выполнен**' if s['L2_declared_fragile'] else 'L2 выполнен'}. Совпадение метки с L1 на подтверждённых правках: {s['agreement_with_L1_on_verified']}.", ""]
        if s.get("unverified"):
            L += ["Неподтверждённые правки (примеры):", ""] + [f"- {u['case_id']}: rotation={u['rotation']}, слова после правки {u['words_after']}" for u in s["unverified"][:8]] + [""]
        if s.get("layer_summary"):
            b = s["layer_summary"]["all"]
            L += [f"L2 (подтверждённые): must-catch {b['n_must_catch']}, recall {_r(b['recall_label_only'])}; must-not-catch {b['n_must_not_catch']}, FP {_r(b['fp_rate'])}.", ""]
    return L


def metric3_tables() -> list[str]:
    d = _load("metric3_results.json")
    if not d:
        return []
    S = d["summary"]
    L = ["## Метрика 3 — реальные расхождения (annotations.jsonl)", "", f"Код-фингерпринт `{d.get('code_fingerprint')}`.", "",
         f"Находок всего {S['findings_total']}, доступно на диске {S['available']}, not_available {S['not_available']}. Поймано на точной размеченной странице: "
         f"{_r(S['recall_exact_page_over_available'])} (из доступных), {_r(S['recall_exact_page_over_all_findings'])} (из всех). Причины промахов: {S['missed_by_reason']}. "
         f"FP на отрицательной паре POL17-N01 (VIOLATION_PRESENT на страницах пары): {S['negative_pair_false_positive_on_pair']}.", "",
         "| находка | природа | доступность | вердикт | причина промаха | слов на аннотированных страницах | VIOLATION_PRESENT на точной странице | инцидентные в том же документе | что ещё мешает |", "|---|---|---|---|---|---|---|---|---|"]
    for c in d["cases"]:
        words = ",".join(str(ap.get("words_on_page")) for ap in c["annotated_pages"]) if c["availability"] == "available" else "—"
        inc = ", ".join(x["parameter_code"] for x in c.get("incidental_same_document_positives") or []) or "—"
        L.append(f"| {c['finding_id']} | {c['nature']} | {c['availability']} | {c.get('verdict', '—')} | {c.get('miss_reason', '—')} | {words} | {c.get('violation_present_exact_page', '—')} | {inc} | {'; '.join(c.get('also_blocked_by') or []) or '—'} |")
    L += ["", f"### Все VIOLATION_PRESENT на шести доступных объектах ({S['violation_present_total_on_available_objects']}; на аннотированных страницах расхождений: {S['violation_present_on_annotated_discrepancy_pages']})", "",
          "Не размечено стендом как истинное/ложное «нарушение»; значения сверены с замороженным корпусом (статусы метрики 1). "
          f"Итог адъюдикации: {S['positives_adjudication']}; доля GENUINE среди адъюдицируемых {_r(S['positives_precision_on_adjudicable'])}.", "",
          "| объект | код | значения | статус | адъюдикация | стороны (статус метрики 1) |", "|---|---|---|---|---|---|"]
    for p in d["unlabelled_positives"]:
        sides = ", ".join(f"{k}: {(v or {}).get('status', 'нет тройки')}" for k, v in p["adjudication"]["sides"].items())
        L.append(f"| {p['object']} | {p['parameter_code']} | {p['values']} | {p['protocol_status']} | {p['adjudication']['verdict']} | {sides} |")
    return L + [""]


def conclusions_tables() -> list[str]:
    m1 = _load("metric1_summary.json")
    mx = json.loads((REPORT_DIR / "critical_recall_matrix.json").read_text(encoding="utf-8")) if (REPORT_DIR / "critical_recall_matrix.json").exists() else None
    L = ["## Выводы: топ-10 критических типов и топ-5 классов ошибок извлечения", ""]
    if mx:
        L += ["### Топ-10 критических типов по (вес × худший recall)", "", "| # | код | параметр | статус | механизм | вес | e2e recall (нижняя гр.) | приоритет |", "|---|---|---|---|---|---|---|---|"]
        for i, x in enumerate(mx["top10_by_weight_x_worst_recall"], 1):
            L.append(f"| {i} | {x['code']} | {x['name']} | {x['status']} | {x['mechanism']} | {x['weight']:.4f} | {x['e2e_recall_lower_bound']:.3f} | {x['priority']:.5f} |")
        L += [""]
    if m1:
        b = m1["summary"]["holdout"]["all_families"]
        fails = sorted(b["taxonomy_sub"].items(), key=lambda kv: -kv[1])[:5]
        n = b["n"]
        L += ["### Топ-5 классов ошибок извлечения (holdout, по подпричинам)", "", "| # | класс/подпричина | троек | доля от n |", "|---|---|---|---|"]
        for i, (k, v) in enumerate(fails, 1):
            L.append(f"| {i} | {k} | {v} | {100 * v / n:.0f}% |")
        L += [""]
    return L


def location_tables() -> list[str]:
    o = observed_conventions()
    L = ["## Конвенции location по public_gold_checks.jsonl", "", f"Источник: `{o['source']}` ({o['n_rows']} строк)."]
    for t, v in o["location_types_seen"].items():
        L.append(f"- `{t}`: {v['n']} строк; примеры: " + ", ".join(f"{e['parameter_code']}→«{e['location']}»" for e in v["examples"][:5]))
    L += [f"- не встречается в публичном gold: {o['location_types_not_seen_in_public_gold']} (конвенция «SITE» подтверждена только текстом задания/комментарием кода про закрытый gold).", ""]
    return L


def main() -> int:
    parts: list[str] = ["# Таблицы стенда измерений (сгенерировано автоматически)", ""]
    for f in (corpus_tables, metric1_tables, seed_tables, metric3_tables, conclusions_tables, location_tables):
        try:
            parts += f()
        except Exception as exc:  # noqa: BLE001
            parts += [f"(ошибка в {f.__name__}: {exc})", ""]
    out = REPORT_DIR / "measurement_bench"
    out.mkdir(parents=True, exist_ok=True)
    (out / "tables.md").write_text("\n".join(parts), encoding="utf-8")
    print(out / "tables.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
