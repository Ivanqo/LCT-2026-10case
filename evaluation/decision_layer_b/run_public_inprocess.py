"""In-process run of the REAL v3 pipeline on the two PUBLIC official objects (no HTTP, no Docker).

Purpose (Phase 10, prompt B): a before/after comparison harness. Run once against a frozen copy of the
code (`--code-root <copy>`) and once against the working tree; `compare_runs.py` then diffs the two
`submission.json` files check by check, so "rule-pack output is byte-identical" is an actual
byte comparison and not a claim.

Object selection belongs to this evaluation-only runner. The runtime importer accepts those IDs explicitly and
never imports annotation or gold-label rows; a hidden object named in `--objects` runs as a blind measurement.

Usage:
    python run_public_inprocess.py --out <dir> [--code-root <dir containing api_service/ and evaluation/>]
                                   [--objects OBJ-TYUMENSKAYA-5-GOLD-SEED OBJ-NOVOSLOBODSKAYA]
Run it with the repository root as the CWD (dataset roots are discovered from the CWD).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--code-root", type=Path, default=REPO_ROOT)
    parser.add_argument("--objects", nargs="*")
    args = parser.parse_args()

    code_root = args.code_root.resolve()
    sys.path.insert(0, str(code_root))
    sys.path.insert(0, str(code_root / "api_service"))
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ["CASE10_LLM_VERIFIER_ENABLED"] = "0"

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Base
    from app.domain.official_dataset import MATRIX_VERSION_OFFICIAL, import_official_dataset
    from evaluation.fixtures import PUBLIC_OBJECT_IDS
    from app.domain.v3_pipeline import create_process, latest_protocol, protocol_to_dict, run_process
    from evaluation.exporter import protocol_to_submission

    args.out.mkdir(parents=True, exist_ok=True)
    objects = args.objects or list(PUBLIC_OBJECT_IDS)
    summary = []
    for object_id in objects:
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(engine)
        db = sessionmaker(bind=engine)()
        started = time.monotonic()
        # a hidden object is imported WITHOUT any label (blind measurement run, see smoke_hidden_blind.py): documents
        # and pages only; the runtime importer has no annotation or gold-label import options.
        import_official_dataset(db, project_id=1, organization_id=1, object_ids=[object_id])
        process = create_process(db, project_id=1, organization_id=1, object_id=object_id, matrix_version=MATRIX_VERSION_OFFICIAL)
        run_process(db, process_id=process.id)
        protocol = latest_protocol(db, project_id=1, organization_id=1, process_id=process.id)
        protocol_dict = protocol_to_dict(protocol)
        submission = protocol_to_submission(protocol_dict)
        payload = protocol_dict["payload"]
        findings = payload["findings"]
        (args.out / f"{object_id}.submission.json").write_text(json.dumps(submission, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        (args.out / f"{object_id}.groups.json").write_text(json.dumps([
            {
                "parameter_code": (g.get("parameter") or {}).get("code"),
                "group_key": g.get("group_key"),
                "finding_status": g.get("finding_status"),
                "comparability_status": g.get("comparability_status"),
                "model_version": g.get("model_version"),
                "expected": g.get("expected"), "actual": g.get("actual"),
                "delta": g.get("delta"),
                "fragments": [{k: f.get(k) for k in ("stage", "file_id", "page", "extracted_value", "role", "extractor")} for f in g.get("fragments") or []],
            }
            for g in sorted(findings, key=lambda x: ((x.get("parameter") or {}).get("code") or "", x.get("group_key") or ""))
        ], ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
        (args.out / f"{object_id}.annex2.json").write_text(json.dumps(payload.get("annex_2"), ensure_ascii=False, indent=1, sort_keys=True, default=str), encoding="utf-8")
        row = {
            "object_id": object_id,
            "seconds": round(time.monotonic() - started, 1),
            "statuses": dict(Counter(g["finding_status"] for g in findings)),
            "labels": dict(Counter(c["violation_label"] for c in submission["checks"])),
            "checks": len(submission["checks"]),
        }
        summary.append(row)
        print(json.dumps(row, ensure_ascii=False), flush=True)
        db.close()
    (args.out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
