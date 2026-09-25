"""Dump the COMPARABLE groups of a mechanism snapshot to a UTF-8 text file (console-safe)."""
import io, json, sys
from collections import Counter
snap_dir, out_path, *objs = sys.argv[1:]
out = io.open(out_path, "w", encoding="utf-8")
for obj in objs:
    d = json.load(open(f"{snap_dir}/snapshot_{obj}.json", encoding="utf-8"))
    out.write(f"===== {obj}: groups={len(d['groups'])} status={dict(Counter((g['comparability_status'], g['finding_status']) for g in d['groups']))} "
              f"live_fragments={d['live_fragments']} run_seconds={d['run_seconds']} coverage={d.get('tagger_coverage')}\n")
    for g in d["groups"]:
        if g["comparability_status"] != "COMPARABLE":
            continue
        dl = g["delta"] or {}
        out.write(f"\n-- {g['parameter_code']} {g['finding_status']} model={g['model_version']} exp={g['expected_value']!r} act={g['actual_value']!r} conf={g['confidence']}\n")
        out.write(f"   delta: loc={dl.get('location')!r} values={dl.get('values')} missing={dl.get('missing_stages')} cmp={dl.get('comparison_result')}\n")
        for f in g["fragments"]:
            out.write(f"   [{f['stage']}/{f['role']}] {f['dataset_file_id']} p.{f['page']} val={f['extracted_value']!r} | {f['filename']}\n        ctx: {(f['context'] or '')[:240]!r}\n")
out.close()
