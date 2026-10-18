"""Step B1: tier statistics of live runs (`run_baseline.py` output directories) and a before/after diff.

    python evaluation/phase12/final_b1_analyze.py tiers <runs_dir> [<runs_dir> ...]
    python evaluation/phase12/final_b1_analyze.py diff <before_dir> <after_dir>
    python evaluation/phase12/final_b1_analyze.py layout <runs_dir> <out_dir>   (A_organizer/B_live/new layout for score_baseline)

Pages per object come from the run's own import summary (document manifest) where available, else from the fixed
page counts below; "per 100 pages" is a noise measure, not a quality measure.
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from pathlib import Path
import shutil
import sys

OBJECTS = ("TYU", "NOV", "LOS3A", "ALT79B", "POL17", "IZM12", "OKT103", "DOO25")
MANIFESTS = Path(__file__).resolve().parents[1] / "silver_new_objects_pass1" / "manifests"
OFFICIAL_PAGES = {"TYU": 357, "NOV": 103}      # PDF pages of the public objects (FINAL_QUALITY_PERF_REPORT)


def pages_of(obj: str) -> int | None:
    if obj in OFFICIAL_PAGES:
        return OFFICIAL_PAGES[obj]
    path = MANIFESTS / f"manifest_{obj}.jsonl"
    if not path.is_file():
        return None
    total = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if str(row.get("distribution_status") or "").upper() == "EXCLUDE":
            continue
        total += int(row.get("pdf_pages") or 0)
    return total


def load(run_dir: Path, obj: str) -> dict | None:
    path = run_dir / f"{obj}.submission.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (round(max(0.0, c - h), 3), round(min(1.0, c + h), 3))


def tiers(run_dirs: list[Path]) -> None:
    for run_dir in run_dirs:
        print(f"## {run_dir}")
        print("| объект | страниц | нарушений-кандидатов | предположений | на 100 стр. (предп.) | отсутств. док. | сравн. невозможно | нет различий | время, с |")
        print("|---|---|---|---|---|---|---|---|---|")
        total_v = total_s = total_pages = 0
        for obj in OBJECTS:
            sub = load(run_dir, obj)
            if sub is None:
                continue
            checks = sub["checks"]
            tier = Counter(c.get("evidence_tier") for c in checks if c["violation_label"] == "VIOLATION_PRESENT")
            labels = Counter(c["violation_label"] for c in checks)
            pages = pages_of(obj)
            run = json.loads((run_dir / f"{obj}.run.json").read_text(encoding="utf-8"))
            v, s = tier.get("VIOLATION_CANDIDATE", 0), tier.get("SUGGESTION", 0)
            total_v, total_s, total_pages = total_v + v, total_s + s, total_pages + (pages or 0)
            per100 = round(100 * s / pages, 2) if pages else "—"
            print(f"| {obj} | {pages} | {v} | {s} | {per100} | {labels.get('MISSING_DOCUMENT', 0)} | {labels.get('COMPARISON_IMPOSSIBLE', 0)} | "
                  f"{labels.get('NO_VIOLATION', 0)} | {run.get('run_process_seconds')} |")
        if total_pages:
            print(f"| **всего** | {total_pages} | {total_v} | {total_s} | {round(100 * total_s / total_pages, 2)} | | | | |")
        print()


def keyed(sub: dict) -> dict[tuple, dict]:
    out: dict[tuple, dict] = {}
    for c in sub["checks"]:
        out.setdefault((c["parameter_code"], c["location"], c["violation_label"], c.get("finding_id")), c)
    return out


def diff(before: Path, after: Path) -> None:
    for obj in OBJECTS:
        b, a = load(before, obj), load(after, obj)
        if b is None or a is None:
            continue
        bk = {(c["parameter_code"], c["location"]): c for c in b["checks"] if c["violation_label"] == "VIOLATION_PRESENT"}
        ak = {(c["parameter_code"], c["location"]): c for c in a["checks"] if c["violation_label"] == "VIOLATION_PRESENT"}
        new = sorted(set(ak) - set(bk))
        lost = sorted(set(bk) - set(ak))
        print(f"### {obj}: до {len(bk)} кандидатов, после {len(ak)}; новых {len(new)}, пропавших {len(lost)}")
        for key in new:
            c = ak[key]
            print(f"  + {key[0]} {key[1]!r} [{c.get('evidence_tier')}] {c.get('tier_reasons')} pd={str(c.get('pd_value'))[:40]!r} rd={str(c.get('rd_value'))[:40]!r}")
        for key in lost:
            c = bk[key]
            after_c = next((x for x in a["checks"] if (x["parameter_code"], x["location"]) == key), None)
            print(f"  - {key[0]} {key[1]!r} теперь: {after_c['violation_label'] if after_c else 'нет'} {after_c.get('reason_code') if after_c else ''}")
        print()


def layout(runs: Path, out: Path) -> None:
    for sub, objs in (("A_organizer", ("TYU", "NOV")), ("B_live", ("TYU", "NOV")), ("new", ("LOS3A", "ALT79B", "POL17", "IZM12", "OKT103", "DOO25"))):
        (out / sub).mkdir(parents=True, exist_ok=True)
        for obj in objs:
            src = runs / f"{obj}.submission.json"
            if src.is_file():
                shutil.copy2(src, out / sub / src.name)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    cmd = sys.argv[1]
    if cmd == "tiers":
        tiers([Path(p) for p in sys.argv[2:]])
    elif cmd == "diff":
        diff(Path(sys.argv[2]), Path(sys.argv[3]))
    elif cmd == "layout":
        layout(Path(sys.argv[2]), Path(sys.argv[3]))
