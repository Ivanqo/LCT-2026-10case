"""Summarize M-078 / M-079 / room-identity checks of run_baseline submissions (before/after Step A)."""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def summarize(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    checks = data["checks"]
    out = {"checks": len(checks), "violation_label": dict(Counter(c.get("violation_label") for c in checks)), "codes": {}}
    for code in ("M-078", "M-079"):
        rows = [c for c in checks if c.get("matrix_code") == code or c.get("parameter_code") == code]
        out["codes"][code] = sorted(
            (str(c.get("location")), c.get("violation_label"), c.get("finding_status_internal") or c.get("finding_status"))
            for c in rows)
    out["room_identity_checks"] = sorted(
        (str(c.get("matrix_code")), str(c.get("location")), c.get("finding_status_internal"))
        for c in checks if "room_identity" in json.dumps(c, ensure_ascii=False))
    return out


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        print(arg)
        print(json.dumps(summarize(Path(arg)), ensure_ascii=False, indent=1))
