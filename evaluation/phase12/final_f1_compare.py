"""F1: export one set of finished protocols with the exporter at HEAD (before) and with the working tree (after).

    python evaluation/phase12/final_f1_compare.py <runs_dir_with_*.protocol.json> <out_dir> [deploy/demo/expected]

Writes <out_dir>/before/<OBJ>.submission.json and <out_dir>/after/<OBJ>.submission.json (same layout as run_baseline,
so final_b1_analyze.py `layout` and score_baseline.py take them), prints counts, shared (code, location) pairs and
whether two exports of the same protocol are byte-identical. The comparison is on the very same protocol, so run-to-run
noise is excluded by construction.
"""
from __future__ import annotations

import importlib
import json
import random
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for path in (REPO, REPO / "api_service"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def head_exporter():
    """The exporter as committed at HEAD, imported as package `evaluation_head` (its relative imports resolve there)."""
    root = Path(tempfile.mkdtemp(prefix="f1_head_"))
    package = root / "evaluation_head"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    for name in ("exporter.py", "errors.py"):
        source = subprocess.run(["git", "show", f"HEAD:evaluation/{name}"], cwd=REPO, capture_output=True, check=True).stdout
        (package / name).write_bytes(source)
    sys.path.insert(0, str(root))
    return importlib.import_module("evaluation_head.exporter")


def shared_pairs(submission: dict) -> list[tuple[str, str, int]]:
    counts = Counter((c["parameter_code"], c["location"]) for c in submission["checks"])
    return [(code, location, n) for (code, location), n in counts.items() if n > 1]


def tier_items(submission: dict) -> Counter:
    """(matrix code, place as the protocol names it, tier) of every tiered check: what demo.py `verify` compares."""
    return Counter((str(c.get("matrix_code") or c["parameter_code"]), str(c.get("location_base") or c["location"]), str(c["evidence_tier"]))
                   for c in submission["checks"] if c.get("evidence_tier"))


def main() -> int:
    runs, out = Path(sys.argv[1]), Path(sys.argv[2])
    expected_dir = Path(sys.argv[3]) if len(sys.argv) > 3 else None
    from evaluation import exporter as after_module
    before_module = head_exporter()
    print("| объект | checks до | checks после | общих (код, место) до | после | сняты (свёрнуты) | с уточнённым местом | байт-идентично | перестановка (3 раза) |")
    print("|---|---|---|---|---|---|---|---|---|")
    for path in sorted(runs.glob("*.protocol.json")):
        obj = path.name.split(".")[0]
        protocol = json.loads(path.read_text(encoding="utf-8"))
        before = before_module.protocol_to_submission(protocol)
        after = after_module.protocol_to_submission(protocol)
        again = after_module.protocol_to_submission(json.loads(path.read_text(encoding="utf-8")))
        blobs = {}
        for label, submission in (("before", before), ("after", after)):
            target = out / label
            target.mkdir(parents=True, exist_ok=True)
            data = json.dumps(submission, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8")
            (target / f"{obj}.submission.json").write_bytes(data)
            blobs[label] = data
        identical = json.dumps(after, sort_keys=True) == json.dumps(again, sort_keys=True)
        canon = sorted(json.dumps(c, sort_keys=True, ensure_ascii=False) for c in after["checks"])
        rng = random.Random(20261018)
        permuted_ok = True
        for _ in range(3):
            shuffled = json.loads(path.read_text(encoding="utf-8"))
            payload = shuffled["payload"] if isinstance(shuffled.get("payload"), dict) else shuffled
            rng.shuffle(payload["findings"])
            permuted = after_module.protocol_to_submission(shuffled)
            permuted_ok = permuted_ok and sorted(json.dumps(c, sort_keys=True, ensure_ascii=False) for c in permuted["checks"]) == canon
        qualified = sum(1 for c in after["checks"] if c.get("location_qualifier"))
        if expected_dir and (expected_dir / f"{obj.lower()}.json").is_file():
            want = Counter(tuple(item) for item in json.loads((expected_dir / f"{obj.lower()}.json").read_text(encoding="utf-8"))["items"])
            sys.stderr.write(f"{obj}: expected {sum(want.values())} | before == expected: {tier_items(before) == want} | "
                             f"after == expected: {tier_items(after) == want} | after == before: {tier_items(after) == tier_items(before)}\n")
        print(f"| {obj} | {len(before['checks'])} | {len(after['checks'])} | {len(shared_pairs(before))} | {len(shared_pairs(after))} | "
              f"{len(before['checks']) - len(after['checks'])} | {qualified} | {'да' if identical else 'НЕТ'} | {'да' if permuted_ok else 'НЕТ'} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
