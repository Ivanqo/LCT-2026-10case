"""F1: inventory of duplicate (parameter_code, location) pairs in the exported submission of finished protocols.

    python evaluation/phase12/final_f1_inventory.py [--check] <protocol.json> [...]    (protocol dict as the API stores it;
    --check: exit code 1 if any (code, location) is still shared)

Classes per duplicated pair: SAME (identical status+values -> pure duplicate), STATUS (different protocol_status/label),
VALUES (same status, different values). Service location = the object id (no entity): quality / clarification / file rows."""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for path in (REPO, REPO / "api_service"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from evaluation.exporter import protocol_to_submission  # noqa: E402


def duplicates(submission: dict) -> dict:
    object_id = submission.get("object_id")
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for check in submission["checks"]:
        groups[(check["parameter_code"], check["location"])].append(check)
    rows = []
    for (code, location), items in groups.items():
        if len(items) < 2:
            continue
        statuses = sorted({(c.get("protocol_status"), c.get("violation_label")) for c in items}, key=str)
        values = sorted({json.dumps([c.get("pd_value"), c.get("rd_value"), c.get("id_value")], ensure_ascii=False, sort_keys=True) for c in items})
        kind = "STATUS" if len(statuses) > 1 else ("VALUES" if len(values) > 1 else "SAME")
        rows.append({"code": code, "location": location, "n": len(items), "service": location == object_id, "kind": kind,
                     "statuses": [list(s) for s in statuses], "values": values,
                     "tiers": sorted({str(c.get("output_tier") or c.get("evidence_tier") or "") for c in items})})
    return {"object_id": object_id, "checks": len(submission["checks"]), "duplicate_pairs": len(rows), "rows": rows}


def summary(report: dict) -> dict:
    rows = report["rows"]
    return {"checks": report["checks"], "pairs": len(rows),
            "service": sum(r["service"] for r in rows), "non_service": sum(not r["service"] for r in rows),
            "by_kind": dict(Counter(r["kind"] for r in rows)),
            "by_kind_non_service": dict(Counter(r["kind"] for r in rows if not r["service"]))}


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--check"]
    failed = False
    for arg in args:
        protocol = json.loads(Path(arg).read_text(encoding="utf-8"))
        report = duplicates(protocol_to_submission(protocol))
        failed = failed or bool(report["rows"])
        print(Path(arg).name, json.dumps(summary(report), ensure_ascii=False))
        for row in sorted(report["rows"], key=lambda r: (r["service"], r["kind"], r["code"], r["location"])):
            print("  ", "SVC" if row["service"] else "   ", row["kind"], row["code"], repr(row["location"])[:50], row["n"], row["statuses"], [v[:60] for v in row["values"]])
    if "--check" in sys.argv[1:] and failed:
        sys.exit(1)
