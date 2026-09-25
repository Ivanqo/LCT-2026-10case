"""Regenerates the reviewable trigger-policy table (Phase 10, prompt B, item 3) from the code.

    python evaluation/decision_layer_b/export_trigger_policy.py

Writes `reports/trigger_policy_132.json` (machine-readable) and `reports/trigger_policy_132.md` (review table).
The table is DERIVED FROM THE CATALOG'S TRIGGER WORDING ONLY. It is the single source of truth in
`api_service/app/domain/trigger_policy.py`; this script only renders it.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "api_service"))

from app.domain import trigger_policy as tp  # noqa: E402

OUT = Path(__file__).resolve().parent / "reports"


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = tp.policy_rows()
    counts = tp.kind_counts()
    review = [r["code"] for r in rows if r["needs_review"]]
    (OUT / "trigger_policy_132.json").write_text(json.dumps(
        {"codes": len(rows), "kind_counts": counts, "needs_review": review, "rows": rows}, ensure_ascii=False, indent=1), encoding="utf-8")
    header = [
        "# Политика триггеров: 132 параметра каталога",
        "",
        "Таблица построена **только по тексту колонки «Триггер» каталога** (`parameter_catalog_132.jsonl`); из gold ничего не использовано.",
        "Источник истины — `api_service/app/domain/trigger_policy.py`; этот файл генерируется `export_trigger_policy.py`.",
        "",
        "Семантика: механизм нашёл различающиеся значения → нарушение (`CANDIDATE`) **только если триггер выполнен** (или не может быть решён — recall прежде всего);",
        "иначе `NEGATIVE_VERIFIED` с `delta.reason = change_not_triggering`. `ANY_CHANGE` = `ANY_CHANGE_PCT(0)`; неоднозначные триггеры консервативно сведены к нему/к широкой границе и помечены `needs_review`.",
        "`NOT_EVALUABLE`: триггер не проверяется сравнением значений двух документов (пространственные/реестровые) — generic-механизм воздерживается (`trigger_not_evaluable`).",
        "Пять кодов rule-pack тира (`PZ-009, KR-055, KR-058, IOS4-078, IOS4-079`) в таблице справочно: тир не менялся.",
        "",
        "## Сводка",
        "",
        "| Тип | Кодов |",
        "|---|---|",
    ] + [f"| {kind} | {counts[kind]} |" for kind in tp.TRIGGER_KINDS] + [
        "",
        f"`needs_review`: **{len(review)}** из {len(rows)} — {', '.join(review)}",
        "",
        "## Таблица",
        "",
    ]
    (OUT / "trigger_policy_132.md").write_text("\n".join(header) + tp.render_markdown(rows), encoding="utf-8")
    print(json.dumps({"codes": len(rows), "kind_counts": counts, "needs_review": len(review)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
