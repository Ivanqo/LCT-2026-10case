"""Compare two outputs of final_a_harness.py (old vs new implementation)."""
from __future__ import annotations

import json
import sys


def main(old_path: str, new_path: str) -> None:
    old = json.load(open(old_path, encoding="utf-8"))
    new = json.load(open(new_path, encoding="utf-8"))
    for code in old["codes"]:
        print(code, "seconds old/new:", old["codes"][code]["seconds"], new["codes"][code]["seconds"])
        ro = {(r["location"], r["stage"]): r for r in old["codes"][code]["observations"]}
        rn = {(r["location"], r["stage"]): r for r in new["codes"][code]["observations"]}
        for key in sorted(set(ro) | set(rn)):
            a, b = ro.get(key), rn.get(key)

            def view(r):
                return None if r is None else (r["value"], r["confidence"], r.get("text_confidence"),
                                               r.get("link_confidence"), r["violation"])
            same = a is not None and b is not None and a["value"] == b["value"] and a["violation"] == b["violation"]
            print(" ", key, view(a), "|", view(b), "" if same else "   <<< DIFF")
    ident = new.get("room_identity") or {}
    print({k: (len(v) if isinstance(v, list) else v) for k, v in ident.items()})
    for issue in ident.get("issues", []):
        print("  ISSUE", issue["code"], issue["reason"], issue["location"], issue.get("text"))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
