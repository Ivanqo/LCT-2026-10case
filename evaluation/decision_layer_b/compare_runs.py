"""Before/after comparison of two `run_public_inprocess.py` outputs (Phase 10, prompt B acceptance).

Checks
  1. RULE-PACK OUTPUT IS BYTE-IDENTICAL: every submission check of a rule-pack code (official_rule_packs.
     SUPPORTED_RULE_CODES) and every evidence group written by the rule-pack tier (`model_version ==
     official-rule-packs-v2`) is compared as canonical JSON text; any difference fails the run.
  2. EVERYTHING ELSE that changed is listed, check by check, with the before/after label and the reason the new
     layer gave -- nothing changes silently.

Usage:  python compare_runs.py <before_dir> <after_dir> [--json out.json]
Exit code 1 when a rule-pack row differs.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

RULE_PACK_CODES = {"PZ-009", "KR-055", "KR-058", "IOS4-078", "IOS4-079"}
RULE_PACK_MODEL = "official-rule-packs-v2"


def _canon(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _checks(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["checks"]


def _groups(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))


def _key(check: dict, seen: Counter) -> tuple:
    base = (check["parameter_code"], check["location"])
    seen[base] += 1
    return (*base, seen[base])


def compare_object(before_dir: Path, after_dir: Path, object_id: str) -> dict:
    b_checks = _checks(before_dir / f"{object_id}.submission.json")
    a_checks = _checks(after_dir / f"{object_id}.submission.json")
    b_seen: Counter = Counter()
    a_seen: Counter = Counter()
    before = {_key(c, b_seen): c for c in b_checks}
    after = {_key(c, a_seen): c for c in a_checks}
    rule_diff = []
    changed = []
    identical = 0
    for key in sorted(set(before) | set(after), key=lambda k: (k[0], k[1], k[2])):
        b, a = before.get(key), after.get(key)
        if b is not None and a is not None and _canon(b) == _canon(a):
            identical += 1
            continue
        code = key[0]
        row = {"parameter_code": code, "location": key[1], "before": (b or {}).get("violation_label"), "after": (a or {}).get("violation_label"),
               "before_status": (b or {}).get("protocol_status"), "after_status": (a or {}).get("protocol_status")}
        if code in RULE_PACK_CODES:
            rule_diff.append({**row, "before_row": b, "after_row": a})
        else:
            changed.append(row)
    b_groups = [g for g in _groups(before_dir / f"{object_id}.groups.json") if g["model_version"] == RULE_PACK_MODEL]
    a_groups = [g for g in _groups(after_dir / f"{object_id}.groups.json") if g["model_version"] == RULE_PACK_MODEL]
    group_identical = _canon(b_groups) == _canon(a_groups)
    a_reasons = {(g["parameter_code"], g["group_key"]): (g.get("delta") or {}).get("reason") for g in _groups(after_dir / f"{object_id}.groups.json")}
    for row in changed:
        row["reason"] = a_reasons.get((row["parameter_code"], "generic:anchor")) or a_reasons.get((row["parameter_code"], "annotation"))
    return {
        "object_id": object_id,
        "checks_before": len(b_checks), "checks_after": len(a_checks), "identical_checks": identical,
        "rule_pack_rows_differ": rule_diff, "rule_pack_groups_byte_identical": group_identical,
        "rule_pack_groups": len(b_groups),
        "changed_non_rule_pack": changed,
        "label_transitions": dict(Counter(f"{r['before']} -> {r['after']}" for r in changed)),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args()
    objects = sorted(p.name.removesuffix(".submission.json") for p in args.before.glob("*.submission.json"))
    report = [compare_object(args.before, args.after, obj) for obj in objects]
    failed = any(r["rule_pack_rows_differ"] or not r["rule_pack_groups_byte_identical"] for r in report)
    for r in report:
        print(json.dumps({k: v for k, v in r.items() if k not in {"changed_non_rule_pack", "rule_pack_rows_differ"}}, ensure_ascii=False))
        for row in r["changed_non_rule_pack"]:
            print("   changed:", json.dumps(row, ensure_ascii=False))
        for row in r["rule_pack_rows_differ"]:
            print("   RULE-PACK DIFF:", json.dumps(row, ensure_ascii=False)[:600])
    print("RULE-PACK BYTE-IDENTICAL:", "NO" if failed else "YES")
    if args.json:
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
