"""Final-round Step 1: before/after table (with Wilson intervals) from two `score_baseline.py` JSON reports, plus the
submission-level diff of two run directories (which checks changed value / label / evidence).

Usage (repo root as CWD):
    python evaluation/phase12/final_s1_compare.py --before before.json --after after.json \
        [--runs-before DIR --runs-after DIR] [--out report_tables.md]
`DIR` = a run directory written by run_baseline.py (<OBJ>.submission.json files).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

SETS = (("A", "A (метки организатора включены; в слепом режиме = B)"), ("B", "B (живой режим)"),
        ("TYU_live", "Тюмень, живой режим"), ("C", "C (SILVER)"), ("D", "D (посевы L1)"))
ROWS = (("recall", "recall"), ("precision_closed", "precision (закр.)"), ("false_positive_rate", "FPR"),
        ("negatives_confirmed", "отриц. подтверждены NO_VIOLATION"))


def _cell(metric: dict | None) -> str:
    if not metric or not metric.get("n"):
        return "—"
    low, high = metric["wilson95"]
    return f"{metric['rate']:.2f} ({metric['k']}/{metric['n']}; [{low:.2f}; {high:.2f}])"


def metrics_table(before: dict, after: dict) -> str:
    lines = ["| набор | метрика (вариант evidence_page) | до | после |", "|---|---|---|---|"]
    for key, title in SETS:
        if key not in before["reports"] or key not in after["reports"]:
            continue
        b = before["reports"][key]["variants"]["evidence_page"]
        a = after["reports"][key]["variants"]["evidence_page"]
        for field, label in ROWS:
            lines.append(f"| {title if field == 'recall' else ''} | {label} | {_cell(b.get(field))} | {_cell(a.get(field))} |")
        outcomes_b, outcomes_a = b.get("outcomes") or {}, a.get("outcomes") or {}
        if outcomes_b != outcomes_a:
            lines.append(f"| | исходы прогнозов | {outcomes_b or '—'} | {outcomes_a or '—'} |")
    d_before, d_after = before["reports"].get("D"), after["reports"].get("D")
    if d_before and d_after:
        for tier in sorted(d_after["by_tier"]):
            lines.append(f"| D / {tier} | recall | {_cell(d_before['by_tier'].get(tier, {}).get('recall'))} | {_cell(d_after['by_tier'][tier]['recall'])} |")
    return "\n".join(lines)


def _checks(path: Path) -> dict[tuple[str, str, str], list[dict]]:
    submission = json.loads(path.read_text(encoding="utf-8"))
    out: dict[tuple[str, str, str], list[dict]] = {}
    for check in submission["checks"]:
        out.setdefault((str(check["parameter_code"]), str(check["location"]), str(check.get("finding_status_internal"))), []).append(check)
    return out


def _signature(check: dict) -> tuple:
    return (check.get("violation_label"), check.get("protocol_status"), check.get("pd_value"), check.get("rd_value"), check.get("id_value"),
            tuple(sorted((e.get("stage"), e.get("file_id"), e.get("page")) for e in check.get("evidence") or [])))


def submission_diff(before_dir: Path, after_dir: Path) -> str:
    lines = ["| объект | проверок до | проверок после | изменились (значение/метка/доказательство) | ключей (код, location) повторяется до | после | байтово равны |",
             "|---|---|---|---|---|---|---|"]
    for path in sorted(before_dir.glob("*.submission.json")):
        other = after_dir / path.name
        if not other.is_file():
            continue
        b, a = _checks(path), _checks(other)
        changed = sum(1 for key in set(b) | set(a) if sorted(map(_signature, b.get(key, []))) != sorted(map(_signature, a.get(key, []))))
        dup = lambda m: sum(1 for rows in m.values() if len(rows) > 1)  # noqa: E731
        same = path.read_bytes() == other.read_bytes()
        lines.append(f"| {path.name.split('.')[0]} | {sum(map(len, b.values()))} | {sum(map(len, a.values()))} | {changed} | {dup(b)} | {dup(a)} | {'да' if same else 'нет'} |")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--before", type=Path, required=True)
    parser.add_argument("--after", type=Path, required=True)
    parser.add_argument("--runs-before", type=Path, nargs="*", default=[])
    parser.add_argument("--runs-after", type=Path, nargs="*", default=[])
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    text = ["## Метрики до / после", "", metrics_table(json.loads(args.before.read_text(encoding="utf-8")),
                                                       json.loads(args.after.read_text(encoding="utf-8"))), ""]
    for before_dir, after_dir in zip(args.runs_before, args.runs_after):
        text += [f"## Выгрузки: {before_dir.name} → {after_dir.name}", "", submission_diff(before_dir, after_dir), ""]
    rendered = "\n".join(text)
    if args.out:
        args.out.write_text(rendered, encoding="utf-8", newline="\n")
    print(rendered)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
