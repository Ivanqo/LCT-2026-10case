"""E3: per-run summary of result.json: checks, tiers, protocol statuses, hypotheses, package counters.
Usage: python e3_summarize.py label=path [label=path ...]"""
import collections
import json
import sys

for arg in sys.argv[1:]:
    label, path = arg.split("=", 1)
    r = json.load(open(path, encoding="utf-8"))
    c = r["checks"]
    tiers = collections.Counter(x.get("evidence_tier") for x in c)
    pkg = r.get("package", {})
    print(f"{label}: checks={len(c)} VIOLATION_CANDIDATE={tiers.get('VIOLATION_CANDIDATE', 0)} SUGGESTION={tiers.get('SUGGESTION', 0)} "
          f"free_hyp={len(r.get('free_search_hypotheses') or [])} status={dict(collections.Counter(x.get('protocol_status') for x in c))} "
          f"stages={pkg.get('documents_by_stage')} unverified={len(pkg.get('approval_unverified_files') or [])} quality_issues={len(r.get('quality_issues') or [])}")
