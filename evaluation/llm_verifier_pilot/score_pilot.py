"""Score baseline pick_best_candidate vs LLM verifier against the manual labels.
Outcome per pool: correct iff the pick is in `strict` (or, when `strict` is
empty, iff the system abstained -- None). Same rule for both systems."""
import glob
import json
import math
import sys
from pathlib import Path

S = Path(__file__).parent
pools = [json.loads(l) for l in open(S / "candidate_pools.jsonl", encoding="utf-8")]
labels = {}
for f in glob.glob(str(S / "labels_*.json")):
    labels.update({k: v for k, v in json.load(open(f, encoding="utf-8")).items() if not k.startswith("_")})
verdicts = {}
for name in (sys.argv[1:] or ["llm_fwd.jsonl", "llm_rev.jsonl"]):
    vpath = S / name
    if vpath.exists():
        for line in open(vpath, encoding="utf-8"):
            r = json.loads(line)
            verdicts.setdefault(r["pool_index"], {}).update(r)


def wilson(k, n, z=1.96):
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (c - h, c + h)


def mcnemar_exact(b, c):
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    p = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * p)


def ok(choice, correct):
    return (choice is None) if not correct else (choice in correct)


def llm_choice(v, key="forward"):
    if v is None or key not in v:
        return "NO_CALL"
    parsed = v[key]["parsed"]
    return "UNUSABLE" if parsed is None else parsed["choice"]


rows = []
for i, p in enumerate(pools):
    key = f"{p['object']}|{p['code']}|{p['stage']}|{p['mechanism']}"
    lab = labels[key]
    assert lab["n"] == len(p["candidates"]), key
    v = verdicts.get(i)
    lc = llm_choice(v)
    lc_eff = None if lc in ("UNUSABLE", "NO_CALL") else lc  # unusable reply == no signal == abstain
    rc = llm_choice(v, "reversed")
    rows.append({
        "i": i, "key": key, "n": len(p["candidates"]), "strict": lab["strict"], "lenient": lab["lenient"],
        "base": p["baseline_choice"], "llm": lc, "llm_eff": lc_eff, "rev": rc,
        "values": [c["value"] for c in p["candidates"]],
        "latency": v["forward"]["latency"] if v and "forward" in v else None, "tokens": v["forward"]["prompt_tokens"] if v and "forward" in v else None,
        "reason": (v["forward"]["parsed"] or {}).get("reason") if v and "forward" in v and v["forward"]["parsed"] else None,
    })


def report(sel, title, label_key="strict", out=sys.stdout):
    n = len(sel)
    b = sum(ok(r["base"], r[label_key]) for r in sel)
    l = sum(ok(r["llm_eff"], r[label_key]) for r in sel) if verdicts else None
    print(f"\n== {title} (n={n}, labels={label_key})", file=out)
    lo, hi = wilson(b, n)
    print(f"  baseline pick_best_candidate: {b}/{n} = {b/n:.3f} [{lo:.3f}, {hi:.3f}]", file=out)
    if verdicts:
        lo, hi = wilson(l, n)
        print(f"  LLM verifier               : {l}/{n} = {l/n:.3f} [{lo:.3f}, {hi:.3f}]", file=out)
        bw = sum(ok(r["base"], r[label_key]) and not ok(r["llm_eff"], r[label_key]) for r in sel)
        lw = sum(ok(r["llm_eff"], r[label_key]) and not ok(r["base"], r[label_key]) for r in sel)
        print(f"  discordant: baseline-only-right={bw}, llm-only-right={lw}, McNemar exact p={mcnemar_exact(bw, lw):.4f}", file=out)


with open(S / "score_report.txt", "w", encoding="utf-8") as out:
    report(rows, "ALL pools", out=out)
    report(rows, "ALL pools", "lenient", out=out)
    report([r for r in rows if r["strict"]], "ANSWERABLE pools (a correct candidate exists)", out=out)
    report([r for r in rows if not r["strict"]], "UNANSWERABLE pools (correct answer = none)", out=out)
    report([r for r in rows if r["n"] > 1], "multi-candidate pools", out=out)
    num = [r for r in rows if "|numeric" in r["key"]]
    report(num, "numeric mechanism only", out=out)
    named = [r for r in rows if r["key"].split("|")[0] == "LOS3A" and r["key"].split("|")[1] in ("PZ-001", "PZ-002", "PZ-004")]
    print("\n== Named LOS3A cases", file=out)
    for r in named:
        print(f"  {r['key']}: strict={r['strict']} baseline={r['base']}({r['values'][r['base']] if r['base'] is not None else None})"
              f" llm={r['llm']}({r['values'][r['llm']] if isinstance(r['llm'], int) else '-'})"
              f" rev={r['rev']} reason={r['reason']}", file=out)
    if verdicts:
        lat = sorted(r["latency"] for r in rows if r["latency"] is not None)
        tok = sorted(r["tokens"] for r in rows if r["tokens"] is not None)
        print(f"\n== latency: n={len(lat)} mean={sum(lat)/len(lat):.2f}s p50={lat[len(lat)//2]:.2f}s p90={lat[int(len(lat)*.9)]:.2f}s max={lat[-1]:.2f}s", file=out)
        print(f"== prompt tokens: mean={sum(tok)/len(tok):.0f} max={tok[-1]}", file=out)
        unusable = sum(r["llm"] == "UNUSABLE" for r in rows)
        print(f"== unusable replies: {unusable}/{len(rows)}", file=out)
        multi = [r for r in rows if r["n"] > 1 and r["rev"] != "NO_CALL"]
        agree = sum(r["llm"] == r["rev"] for r in multi)
        print(f"== order-reversal consistency (multi-candidate): {agree}/{len(multi)}", file=out)
        # position bias: how often LLM picks index 0 vs how often index 0 is correct
        print(f"== LLM picked index 0: {sum(r['llm'] == 0 for r in rows)}; null: {sum(r['llm'] is None for r in rows)}", file=out)
    if verdicts:
        # Alternative integration policies, same labels, same pools.
        def policy_report(name, pick):
            for lk in ("strict", "lenient"):
                k = sum(ok(pick(r), r[lk]) for r in rows)
                lo, hi = wilson(k, len(rows))
                bw = sum(ok(r["base"], r[lk]) and not ok(pick(r), r[lk]) for r in rows)
                lw = sum(ok(pick(r), r[lk]) and not ok(r["base"], r[lk]) for r in rows)
                print(f"  {name:48s} [{lk:7s}] {k}/{len(rows)} = {k/len(rows):.3f} [{lo:.3f}, {hi:.3f}]  vs baseline: lost={bw} gained={lw} p={mcnemar_exact(bw, lw):.4f}", file=out)
        print("\n== integration policies", file=out)
        policy_report("pure LLM (null = abstain)", lambda r: r["llm_eff"])
        policy_report("LLM pick if non-null, else keep baseline", lambda r: r["llm_eff"] if r["llm_eff"] is not None else r["base"])
        policy_report("LLM only when >=2 candidates, else baseline", lambda r: r["llm_eff"] if r["n"] > 1 else r["base"])
        # POST-HOC (chosen after seeing the forward-pass regressions -- exploratory, not the pre-registered comparison):
        policy_report("POST-HOC: never override a baseline abstention", lambda r: None if r["base"] is None else r["llm_eff"])
        policy_report("POST-HOC: LLM only if >=2 distinct values, never override abstention",
                      lambda r: (None if r["base"] is None else (r["llm_eff"] if len(set(r["values"])) > 1 else r["base"])))
        policy_report("order-consistent LLM pick, else baseline",
                      lambda r: r["llm_eff"] if (r["llm_eff"] is not None and r["rev"] == r["llm"]) else r["base"])
        # answerable-only accuracy of a non-null pick (precision of a pick)
        picks = [r for r in rows if r["llm_eff"] is not None]
        bpicks = [r for r in rows if r["base"] is not None]
        kp = sum(r["llm_eff"] in r["strict"] for r in picks)
        kb = sum(r["base"] in r["strict"] for r in bpicks)
        print(f"\n== precision of a non-null pick: LLM {kp}/{len(picks)} = {kp/max(1,len(picks)):.3f} {wilson(kp, len(picks))}; baseline {kb}/{len(bpicks)} = {kb/max(1,len(bpicks)):.3f} {wilson(kb, len(bpicks))}", file=out)
        ans = [r for r in rows if r["strict"]]
        ka = sum(r["llm_eff"] in r["strict"] for r in ans)
        kb2 = sum(r["base"] in r["strict"] for r in ans)
        print(f"== recall on answerable pools: LLM {ka}/{len(ans)}; baseline {kb2}/{len(ans)}", file=out)
    print("\n== per-pool", file=out)
    for r in rows:
        mark_b = "OK " if ok(r["base"], r["strict"]) else "BAD"
        mark_l = ("OK " if ok(r["llm_eff"], r["strict"]) else "BAD") if verdicts else "   "
        print(f"  [{r['i']:3d}] {r['key']:34s} n={r['n']:2d} strict={r['strict']} base={r['base']} {mark_b} llm={r['llm']} rev={r['rev']} {mark_l} | {r['reason']}", file=out)
print(open(S / "score_report.txt", encoding="utf-8").read()[:3000])
