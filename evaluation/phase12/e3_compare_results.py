"""E3: compare two result.json files: `checks` (full, order-insensitive) and everything outside `checks`.
Usage: python e3_compare_results.py A.json B.json   (A = reference, B = new)"""
import collections
import json
import sys


def load(p):
    return json.load(open(p, encoding="utf-8"))


def canon(c):
    return json.dumps(c, sort_keys=True, ensure_ascii=False)


a, b = load(sys.argv[1]), load(sys.argv[2])
fa, fb = collections.Counter(map(canon, a["checks"])), collections.Counter(map(canon, b["checks"]))
equal = fa == fb
print(f"checks A={len(a['checks'])} B={len(b['checks'])} equal={equal}")
if not equal:
    for k, v in list((fa - fb).items())[:5]:
        print("  only A:", k[:300])
    for k, v in list((fb - fa).items())[:5]:
        print("  only B:", k[:300])
ka, kb = set(a), set(b)
print("top-level keys only A:", sorted(ka - kb), "only B:", sorted(kb - ka))
for k in sorted(ka & kb - {"checks", "package"}):
    if a[k] != b[k]:
        va, vb = a[k], b[k]
        if isinstance(va, list) and isinstance(vb, list):
            print(f"  differs: {k} len {len(va)} vs {len(vb)}")
        else:
            print(f"  differs: {k}")
pa, pb = a.get("package", {}), b.get("package", {})
for k in sorted(set(pa) | set(pb)):
    if pa.get(k) != pb.get(k):
        print("  package differs:", k, "|", str(pa.get(k))[:60], "|", str(pb.get(k))[:60])
print("free_search_hypotheses:", type(b.get("free_search_hypotheses")).__name__, len(b.get("free_search_hypotheses") or []))
tiers = collections.Counter(c.get("evidence_tier") or c.get("finding_status_internal") for c in b["checks"])
print("tiers B:", dict(tiers))
sys.exit(0 if equal else 1)
