import json, sys, collections
def load(p): return json.load(open(p, encoding="utf-8"))
def tup(c): return (c.get("matrix_code") or c.get("parameter_code"), c.get("location"), c.get("violation_label"), c.get("protocol_status"), c.get("finding_status_internal"), c.get("evidence_tier"))
a, b = load(sys.argv[1]), load(sys.argv[2])
ca, cb = a["checks"], b["checks"]
ta, tb = collections.Counter(map(tup, ca)), collections.Counter(map(tup, cb))
print("checks", len(ca), len(cb), "tuple multiset equal:", ta == tb)
for k, v in (ta - tb).items(): print("  only A", k, v)
for k, v in (tb - ta).items(): print("  only B", k, v)
# full-checks equality
fa = sorted(json.dumps(c, sort_keys=True, ensure_ascii=False) for c in ca)
fb = sorted(json.dumps(c, sort_keys=True, ensure_ascii=False) for c in cb)
print("full checks equal:", fa == fb)
for k in a:
    if k in ("checks", "package"): continue
    if a.get(k) != b.get(k): print("  differs top-level:", k)
pa, pb = a.get("package", {}), b.get("package", {})
for k in sorted(set(pa) | set(pb)):
    if pa.get(k) != pb.get(k): print("  package differs:", k, str(pa.get(k))[:100], "|", str(pb.get(k))[:100])
tiers = collections.Counter(c.get("evidence_tier") for c in cb); print("tiers B", dict(tiers))
