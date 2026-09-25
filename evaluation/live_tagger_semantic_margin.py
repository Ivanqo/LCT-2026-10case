"""CASE10 E1 -- residual determinism risk: semantic scores near the 0.45 threshold, CPU vs GPU.

See evaluation/LIVE_TAGGER_SCALE_DETERMINISM_REPORT.md section 3.1.

step `collect` (project venv, CPU torch): run the live tagger on an object with a
persistent cache and a wrapped scorer that records every (query, row_text, cpu_score).
step `gpu` (a CUDA venv): re-score the same pairs on CPU and CUDA, compare with the
recorded scores and count threshold decision flips.

Usage (repo root):
    venv/Scripts/python.exe evaluation/live_tagger_semantic_margin.py collect LOS3A
    HF_HOME=E:\\case10_llm_pilot\\hf E:/case10_llm_pilot/venv/Scripts/python.exe evaluation/live_tagger_semantic_margin.py gpu LOS3A
Outputs: evaluation/reports/live_tagger_scale/semantic_{pairs,summary,gpu}_<OBJ>.json
"""
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "evaluation" / "reports" / "live_tagger_scale"
OBJECTS = {"LOS3A": "Лосевская, 3А", "DOO25": "Полярная ул. 25_ ДОО220к.9", "OKT103": "Октябрьская 103"}


def collect(obj: str) -> None:
    os.environ.update({
        "HF_HUB_OFFLINE": "1", "CASE10_LIVE_TAGGER_WORKERS": "6", "CASE10_LIVE_TAGGER_CACHE_ENABLED": "1",
        "CASE10_LIVE_TAGGER_CACHE_DIR": str(Path(tempfile.gettempdir()) / "case10_live_tagger_cache_semantic_margin"),
        "CASE10_ORIGINALS_ROOT": str(Path(os.environ.get("CASE10_NEW_OBJECTS_ROOT", r"E:\CASE10_new_objects_extracted")) / OBJECTS[obj]),
    })
    sys.path[:0] = [str(REPO), str(REPO / "api_service")]
    from unittest.mock import patch

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Base
    from app.domain import live_candidate_tagger as t
    from app.domain.official_dataset import _import_document_manifest
    from app.domain.official_evidence import _full_catalog_params
    from app.domain.semantic_similarity import SEMANTIC_MIN_SIMILARITY
    from app.domain.v3_pipeline import MATRIX_VERSION_OFFICIAL, _project_documents, create_process, current_document_versions, list_active_params

    manifest = REPO / "evaluation" / "silver_new_objects_pass1" / "manifests" / f"manifest_{obj}.jsonl"
    object_id = json.loads(manifest.open(encoding="utf-8").readline())["object_id"]
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    _import_document_manifest(db, manifest, project_id=1, organization_id=1, object_ids={object_id})
    process = create_process(db, project_id=1, organization_id=1, object_id=object_id, matrix_version=MATRIX_VERSION_OFFICIAL)
    docs = current_document_versions(_project_documents(db, process))
    params = list_active_params(db, organization_id=1, project_id=1, matrix_version=MATRIX_VERSION_OFFICIAL)
    pairs = []
    real = t.semantic_text_similarity

    def recording(query, row):
        score = real(query, row)
        pairs.append({"query": query, "row": row, "cpu": score})
        return score

    with patch.object(t, "semantic_text_similarity", recording):
        diag = t.tag_live_candidates(db, docs, _full_catalog_params(db, params), budget=t.new_live_tagger_budget())
    scored = [p for p in pairs if p["cpu"] is not None]
    margins = sorted(abs(p["cpu"] - SEMANTIC_MIN_SIMILARITY) for p in scored)
    summary = {"object": obj, "threshold": SEMANTIC_MIN_SIMILARITY, "n_scored": len(scored),
               "rejected": sum(1 for p in scored if p["cpu"] < SEMANTIC_MIN_SIMILARITY),
               "min_margin": margins[0] if margins else None, "margins_below_1e-3": sum(1 for m in margins if m < 1e-3),
               "margins_below_1e-2": sum(1 for m in margins if m < 1e-2), "fragments": diag["fragments_created"]}
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"semantic_pairs_{obj}.json").write_text(json.dumps({**summary, "pairs": pairs}, ensure_ascii=False), encoding="utf-8")
    (OUT / f"semantic_summary_{obj}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary))


def gpu(obj: str) -> None:
    os.environ["HF_HUB_OFFLINE"] = "1"
    import numpy as np
    import torch
    from sentence_transformers import SentenceTransformer

    data = json.loads((OUT / f"semantic_pairs_{obj}.json").read_text(encoding="utf-8"))
    threshold = data["threshold"]
    scored = [p for p in data["pairs"] if p["cpu"] is not None]
    name = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    report = {"object": obj, "n": len(scored), "torch": torch.__version__}
    for device in ("cpu", "cuda"):
        model = SentenceTransformer(name, device=device)
        cache = {}

        def emb(text):
            # one text at a time, exactly like semantic_similarity._embed_cached
            key = " ".join(str(text or "").split())
            if key not in cache:
                cache[key] = model.encode(key, normalize_embeddings=True)
            return cache[key]

        scores = np.array([float((emb(p["query"]) * emb(p["row"])).sum()) for p in scored])
        ref = np.array([p["cpu"] for p in scored])
        diff = np.abs(scores - ref)
        flips = int(((scores < threshold) != (ref < threshold)).sum())
        report[device] = {"max_abs_diff_vs_project_cpu": float(diff.max()), "mean_abs_diff": float(diff.mean()), "decision_flips": flips}
    print(json.dumps(report))
    (OUT / f"semantic_gpu_{obj}.json").write_text(json.dumps(report, indent=1), encoding="utf-8")


if __name__ == "__main__":
    {"collect": collect, "gpu": gpu}[sys.argv[1]](sys.argv[2])
