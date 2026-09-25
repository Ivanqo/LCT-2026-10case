"""Metric-1 extraction run for one object (or all): real live tagger + the four
real collectors (numeric / enum / compound / table-count), decision layer NOT
involved.  Writes OUT_DIR/extract_<OBJ>.json (tagger fragments, doc coverage,
per-(param, stage) observations, candidate pools) and keeps the persistent DB.

  python -m evaluation.measurement_bench.run_extraction OBJ [OBJ ...]   (default: all eight)

Refuses to run unless the value corpus is frozen (see pipeline_env.require_frozen_corpus)
and stamps every output with the corpus hash, the code fingerprint and start time.
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict

from .common import OBJECTS, OUT_DIR, utcnow_iso
from .pipeline_env import code_fingerprint, require_frozen_corpus, setup


def obs_row(obs, family: str) -> dict:
    row = {
        "file_id": obs.document.dataset_file_id, "page": obs.page, "extractor": obs.extractor, "context": obs.context,
        "confidence": obs.confidence, "bbox_pdf": obs.bbox_pdf, "location": obs.location,
        "document_stage": obs.document.dataset_stage, "discipline": obs.document.discipline,
    }
    if family == "NUMERIC_TABLE":
        row.update(value=obs.value, norm=obs.normalized_value)
    elif family == "ENUM":
        row.update(value=obs.value, canonical=obs.canonical_value, enum_family=obs.family)
    elif family == "COMPOUND":
        row.update(values=list(obs.raw_values), norms=[str(x) for x in obs.normalized_values])
    else:
        row.update(row_count=obs.row_count)
    gf = getattr(obs, "gate_facts", None)
    if gf:
        row["gate_facts"] = {k: (v if isinstance(v, (str, int, float, bool, type(None), list, dict)) else str(v)) for k, v in gf.items()}
    return row


def run_object(obj: str) -> dict:
    manifest = require_frozen_corpus()
    t0 = time.monotonic()
    started = utcnow_iso()
    db, engine, object_id, process, docs, params = setup(obj, fresh=True)
    from collections import defaultdict as dd

    from app.db.models import SourceFragment
    from app.domain.cross_stage_localization import narrow_candidate_fragments
    from app.domain.generic_compound_extraction import collect_compound_observations, new_compound_budget
    from app.domain.generic_enum_extraction import collect_enum_observations, new_enum_budget
    from app.domain.generic_matrix_extraction import collect_generic_observations, eligible_params, new_generic_budget
    from app.domain.generic_table_row_count import collect_table_count_observations, new_table_count_budget
    from app.domain.live_candidate_tagger import live_tagger_coverage, new_live_tagger_budget, tag_live_candidates
    from app.domain.official_evidence import STAGE_CODES, inference_annotation
    from app.domain.official_rule_packs import SUPPORTED_RULE_CODES

    catalog = [p for p in params]
    diagnostics = tag_live_candidates(db, docs, catalog, budget=new_live_tagger_budget())
    db.commit()
    t_tag = time.monotonic() - t0

    by_id = {d.id: d for d in docs}
    fragments = db.query(SourceFragment).filter(SourceFragment.document_version_id.in_(by_id)).all()
    by_code = dd(list)
    for f in fragments:
        if inference_annotation(f):
            by_code[(f.metadata_json or {}).get("code")].append(f)
    frag_rows = [
        {"code": (f.metadata_json or {}).get("code"), "file_id": by_id[f.document_version_id].dataset_file_id, "page": f.page,
         "confidence": f.confidence, "semantic_score": (f.metadata_json or {}).get("semantic_score"), "text": (f.text or "")[:160]}
        for f in fragments if inference_annotation(f)
    ]

    t1 = time.monotonic()
    gen_cand = eligible_params(catalog, excluded_codes=SUPPORTED_RULE_CODES)
    numeric = collect_generic_observations(gen_cand, by_code, by_id, stage_codes=STAGE_CODES, budget=new_generic_budget())
    enum = collect_enum_observations(catalog, by_code, by_id, stage_codes=STAGE_CODES, budget=new_enum_budget(), excluded_codes=SUPPORTED_RULE_CODES)
    comp = collect_compound_observations(catalog, by_code, by_id, stage_codes=STAGE_CODES, budget=new_compound_budget(), excluded_codes=SUPPORTED_RULE_CODES)
    cnt = collect_table_count_observations(catalog, by_code, by_id, stage_codes=STAGE_CODES, budget=new_table_count_budget(), excluded_codes=SUPPORTED_RULE_CODES)
    t_col = time.monotonic() - t1

    code_by_id = {int(p.id): str(p.code) for p in params}
    observations: dict[str, dict[str, dict]] = defaultdict(dict)
    for family, result in (("NUMERIC_TABLE", numeric), ("ENUM", enum), ("COMPOUND", comp), ("TABLE_COUNT", cnt)):
        for pid, stages in result.items():
            for stage, obs in stages.items():
                observations[code_by_id[int(pid)]][stage] = {"family": family, **obs_row(obs, family)}

    # rendered-candidate window (top PAGES_PER_STAGE after discipline narrowing) per (code, stage): the pages the
    # collectors could ever look at -> distinguishes "tagged but never rendered" (page cap) from "rendered, no value".
    rendered: dict[str, dict[str, list]] = defaultdict(dict)
    pd_doc = {code: st["PD"]["file_id"] for code, st in observations.items() if "PD" in st}
    doc_by_fid = {d.dataset_file_id: d for d in docs}
    for code, frs in by_code.items():
        for stage in ("PD", "RD", "ID"):
            sfr = [f for f in frs if (by_id[f.document_version_id].dataset_stage or "") == stage]
            if not sfr:
                continue
            ref = doc_by_fid.get(pd_doc.get(code)) if stage != "PD" else None
            picked = narrow_candidate_fragments(sfr, by_id, reference_document=ref, limit=15)
            rendered[code][stage] = [[by_id[f.document_version_id].dataset_file_id, f.page] for f in picked]

    coverage = live_tagger_coverage(docs)
    doc_rows = []
    for d in docs:
        meta = d.dataset_metadata or {}
        doc_rows.append({"file_id": d.dataset_file_id, "stage": d.dataset_stage, "discipline": d.discipline, "marker": meta.get("live_tagger_scan"),
                         "pages": (meta.get("document_manifest") or {}).get("pdf_pages")})
    result = {
        "object": obj, "object_id": object_id, "started_at": started, "finished_at": utcnow_iso(),
        "seconds_tagging": round(t_tag, 1), "seconds_collectors": round(t_col, 1),
        "corpus_sha256": manifest["corpus_sha256"], "corpus_frozen_at": manifest["frozen_at"],
        "code_fingerprint": code_fingerprint(),
        "tagger_diagnostics": {k: v for k, v in diagnostics.items() if isinstance(v, (int, float, str, dict, list))},
        "tagger_coverage": coverage, "n_live_fragments": len(frag_rows), "documents": doc_rows,
        "fragments": frag_rows, "observations": observations, "rendered_window": rendered,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"extract_{obj}.json"
    out.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"object": obj, "fragments": len(frag_rows), "observed_codes": len(observations), "tag_s": round(t_tag, 1), "collect_s": round(t_col, 1), "out": str(out)}, ensure_ascii=False), flush=True)
    db.close()
    return result


def main() -> None:
    objs = [a for a in sys.argv[1:] if not a.startswith("-")] or list(OBJECTS)
    for o in objs:
        run_object(o)


if __name__ == "__main__":
    main()
