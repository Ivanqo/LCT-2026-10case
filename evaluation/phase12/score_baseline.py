"""Phase 12 / P0: score the baseline runs with grader_sim on sets A, B, C, D.

Inputs: run directories written by `run_baseline.py` (organizer / live / new) and a `seed_l1` l1_results.json that
stores the seeded pair and the protocol check per case (set D). Outputs: a JSON with every report, a generated markdown
with the tables (both under evaluation/phase12/), and copies of the scored submissions (baseline_runs/).

Usage (repo root as CWD):
    python evaluation/phase12/score_baseline.py --runs E:/case10_phase12/runs --seeds E:/case10_phase12/bench_work/out/l1_results.json
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
for _path in (REPO_ROOT, REPO_ROOT / "api_service"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from evaluation import grader_sim as gs  # noqa: E402
from evaluation.phase12 import gold_sets  # noqa: E402

HERE = Path(__file__).resolve().parent
OFFICIAL = {"TYU": "OBJ-TYUMENSKAYA-5-GOLD-SEED", "NOV": "OBJ-NOVOSLOBODSKAYA", "RECH": "OBJ-RECHNIKOV-7-7"}
NEW = ("LOS3A", "ALT79B", "POL17", "DOO25", "OKT103", "IZM12")
OBJECT_IDS = {**OFFICIAL, **{code: gold_sets.new_object_id(code) for code in NEW}}


def _preds(run_dir: Path, objects) -> list[dict]:
    out = []
    for obj in objects:
        path = run_dir / f"{obj}.submission.json"
        if path.is_file():
            out.extend(gs.load_submission(path))
    return out


def _timings(runs: Path) -> list[dict]:
    rows = []
    for mode in ("A_organizer", "B_live", "new"):
        for path in sorted((runs / mode).glob("*.run.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            rows.append({"run": mode, "object": data["object"], "import_seconds": data["import_seconds"],
                         "run_process_seconds": data["run_process_seconds"], "finding_status": data["finding_status"],
                         "violation_label": data["violation_label"], "coverage": data.get("live_tagger_coverage")})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=Path, required=True)
    parser.add_argument("--seeds", type=Path)
    parser.add_argument("--out-json", type=Path, default=HERE / "baseline_metrics.json")
    parser.add_argument("--out-md", type=Path, default=HERE / "baseline_tables.md")
    parser.add_argument("--copy-runs", type=Path, default=HERE / "baseline_runs")
    args = parser.parse_args()

    registry = gold_sets.registry()
    organizer = _preds(args.runs / "A_organizer", OFFICIAL)
    live = _preds(args.runs / "B_live", OFFICIAL)
    new = _preds(args.runs / "new", NEW)
    gold_a = gold_sets.set_a()
    gold_c, c_stats = gold_sets.set_c()
    tyu = [g for g in gold_a if g["object_id"] == OFFICIAL["TYU"]]
    reports = {
        "A": gs.evaluate(gold_a, organizer + new, registry=registry),
        "A_objects_on_disk": gs.evaluate([g for g in gold_a if g["available"]], organizer + new, registry=registry),
        "B": gs.evaluate(gold_a, live + new, registry=registry),
        "B_objects_on_disk": gs.evaluate([g for g in gold_a if g["available"]], live + new, registry=registry),
        "TYU_with_organizer_annotations": gs.evaluate(tyu, organizer, registry=registry),
        "TYU_live": gs.evaluate(tyu, live, registry=registry),
        "C": gs.evaluate(gold_c, live + new, registry=registry),
    }
    notes = {"C_label_stats": c_stats}
    if args.seeds and args.seeds.is_file():
        gold_d, pred_d, d_stats = gold_sets.set_d(args.seeds, OBJECT_IDS)
        reports["D"] = gs.evaluate(gold_d, pred_d)
        notes["D_stats"] = d_stats
    timings = _timings(args.runs)
    args.out_json.write_text(json.dumps({"reports": reports, "notes": notes, "timings": timings,
                                         "assumed_domain_codes": {k: {"codes": v[0], "type": v[1]} for k, v in gold_sets.ASSUMED_DOMAIN_CODES.items()}},
                                        ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
    md = ["# Phase 12 baseline — generated tables", "", f"_Сгенерировано `evaluation/phase12/score_baseline.py` из `{args.runs.as_posix()}`; не править вручную._", ""]
    for name, report in reports.items():
        sample = report["sample"]
        md.append(gs.render_summary(report, f"{name} — gold {sample['gold']} (+{sample['gold_positive']} / −{sample['gold_negative']}), "
                                             f"прогнозов {sample['predictions']} (VIOLATION_PRESENT {sample['predictions_positive']})"))
        if sample["objects_without_predictions"]:
            md.append(f"Объекты без прогона: {', '.join(sample['objects_without_predictions'])}.")
        primary = report["variants"][gs.PRIMARY_VARIANT]
        md.append(f"Найдено: {', '.join(primary['found']) or '—'}; пропущено: {', '.join(primary['missed']) or '—'}; "
                  f"отрицательные с ложным срабатыванием: {', '.join(primary['negatives_flagged']) or '—'}; исходы прогнозов: {primary['outcomes'] or '—'}.")
        md += ["", "| срез (tier) | +/− | recall | precision закр. | FPR |", "|---|---|---|---|---|"]
        md += [f"| {k} | {v['n_pos']}/{v['n_neg']} | {gs._fmt(v['recall'])} | {gs._fmt(v['precision_closed'])} | {gs._fmt(v['false_positive_rate'])} |" for k, v in report["by_tier"].items()]
        md += ["", "| раздел | +/− | recall | FPR |", "|---|---|---|---|"]
        md += [f"| {k} | {v['n_pos']}/{v['n_neg']} | {gs._fmt(v['recall'])} | {gs._fmt(v['false_positive_rate'])} |" for k, v in report["by_section"].items()]
        md += ["", "| тип | +/− | recall | FPR |", "|---|---|---|---|"]
        md += [f"| {k} | {v['n_pos']}/{v['n_neg']} | {gs._fmt(v['recall'])} | {gs._fmt(v['false_positive_rate'])} |" for k, v in report["by_type"].items()]
        md.append("")
    md += ["## Время", "", "| прогон | объект | импорт, с | run_process, с | статусы групп |", "|---|---|---|---|---|"]
    md += [f"| {t['run']} | {t['object']} | {t['import_seconds']} | {t['run_process_seconds']} | {t['finding_status']} |" for t in timings]
    md += ["", f"Метки C: {json.dumps(c_stats, ensure_ascii=False)}", f"Посевы D: {json.dumps(notes.get('D_stats'), ensure_ascii=False)}", ""]
    args.out_md.write_text("\n".join(md), encoding="utf-8", newline="\n")

    if args.copy_runs:
        for mode in ("A_organizer", "B_live", "new"):
            target = args.copy_runs / mode
            target.mkdir(parents=True, exist_ok=True)
            for path in (args.runs / mode).glob("*.json"):
                shutil.copy2(path, target / path.name)
    _write_example(args.runs / "A_organizer" / "TYU.submission.json", HERE / "example_submission_gold11.json")
    print(args.out_md.read_text(encoding="utf-8"))
    return 0


def _write_example(source: Path, target: Path) -> None:
    """A short, real 1.1 export: every VIOLATION_PRESENT check of the Tyumen run plus one check of each other label."""
    if not source.is_file():
        return
    submission = json.loads(source.read_text(encoding="utf-8"))
    picked, seen = [], set()
    for check in submission["checks"]:
        label = check["violation_label"]
        if label == "VIOLATION_PRESENT" or label not in seen:
            picked.append(check)
            seen.add(label)
    example = {"object_id": submission["object_id"], "checks": picked, "export_conventions": submission.get("export_conventions"),
               "_note": f"Excerpt of {source.name} (baseline run, organizer annotations on): all VIOLATION_PRESENT checks + one per other label. "
                        "Keys beyond submission_schema.json are the GOLD 1.1 fields."}
    target.write_text(json.dumps(example, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
