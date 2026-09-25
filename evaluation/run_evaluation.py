from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .fixtures import gold_checks_to_evaluation_fixture, load_gold_checks_jsonl
from .metrics import evaluate_case10


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate CASE10 V3 predictions against a gold JSON fixture.")
    parser.add_argument("--gold", type=Path, help="Path to gold JSON.")
    parser.add_argument("--gold-checks-jsonl", type=Path, help="Path to CASE10 gold checks JSONL.")
    parser.add_argument("--predictions", required=True, type=Path, help="Path to predictions JSON.")
    parser.add_argument("--output", type=Path, help="Optional output JSON path.")
    parser.add_argument("--bbox-iou-threshold", type=float, default=0.5, help="IoU threshold for evidence localization accuracy.")
    parser.add_argument("--purpose", choices=["evaluation", "training"], default="evaluation", help="Leakage guard purpose for JSONL gold.")
    parser.add_argument("--allow-hidden-labels", action="store_true", help="Allow hidden organizer labels for a local locked evaluation run.")
    parser.add_argument("--allow-organizer-only", action="store_true", help="Allow organizer-only labels where the selected purpose permits it.")
    parser.add_argument("--object-id", action="append", help="Evaluate only these object IDs (repeatable).")
    args = parser.parse_args()

    if not args.gold and not args.gold_checks_jsonl:
        parser.error("one of --gold or --gold-checks-jsonl is required")
    gold = _read_json(args.gold) if args.gold else _read_gold_checks(args)
    predictions = _read_json(args.predictions)
    result = evaluate_case10(gold, predictions, bbox_iou_threshold=args.bbox_iou_threshold)
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return data


def _read_gold_checks(args: argparse.Namespace) -> dict[str, Any]:
    loaded = load_gold_checks_jsonl(
        args.gold_checks_jsonl,
        purpose=args.purpose,
        allow_hidden_labels=args.allow_hidden_labels,
        allow_organizer_only=args.allow_organizer_only,
    )
    rows = [row for row in loaded["rows"] if not args.object_id or row.get("object_id") in args.object_id]
    objects = {row.get("object_id") for row in rows}
    return gold_checks_to_evaluation_fixture(rows, total_params=132 * len(objects))


if __name__ == "__main__":
    raise SystemExit(main())
