"""Step A check: the fixed Tyumen sheets (S2 report) through the ventilation room comparison.

Runs `_extract_ventilation_observations` directly on the fixed pages (PD F0171 p.88/p.104, RD F0201
p.17/p.18/p.20; the file ids and pages are those of evaluation/phase12/S2_REPORT.md and are read from
the command line, not from the code under test) and prints, per code, the PD/RD observations with
their confidence fields and the room-identity report. Works against the old and the new
implementation: pass the code tree through PYTHONPATH.

    python evaluation/phase12/final_a_harness.py --pd F0171:88,104 --rd F0201:17,18,20 --out result.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--object", default="OBJ-TYUMENSKAYA-5-GOLD-SEED")
    parser.add_argument("--pd", required=True)
    parser.add_argument("--rd", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db.models import Base, DocumentVersion
    from app.domain.official_dataset import import_official_dataset
    from app.domain import official_rule_packs as packs
    from app.domain.dataset_sources import extract_original_pages

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    t0 = time.monotonic()
    import_official_dataset(db, project_id=1, organization_id=1, object_ids=[args.object])
    documents = {int(d.id): d for d in db.query(DocumentVersion).all()}
    by_file = {str(d.dataset_file_id): d for d in documents.values()}

    wanted = {}
    for spec in (args.pd, args.rd):
        file_id, pages = spec.split(":")
        wanted[file_id] = [int(p) for p in pages.split(",")]
    snapshots, candidates = {}, {}
    for file_id, pages in wanted.items():
        document = by_file[file_id]
        for page_number, snapshot in extract_original_pages(document, set(pages)).items():
            snapshots[(int(document.id), int(page_number))] = snapshot
        for code in ("IOS4-078", "IOS4-079"):
            candidates.setdefault((int(document.id), code), {}).update({p: [] for p in pages})
    result = {"setup_seconds": round(time.monotonic() - t0, 1), "codes": {}}
    report = packs.new_room_identity_report() if hasattr(packs, "new_room_identity_report") else None
    for code in ("IOS4-078", "IOS4-079"):
        t1 = time.monotonic()
        if report is not None:
            observations = packs._extract_ventilation_observations(code, documents, candidates, snapshots, report)
        else:
            observations = packs._extract_ventilation_observations(code, documents, candidates, snapshots)
        rows = []
        for obs in observations:
            rows.append({
                "location": obs.location, "stage": obs.stage, "value": obs.normalized_value,
                "confidence": obs.confidence, "text_confidence": getattr(obs, "text_confidence", None),
                "link_confidence": getattr(obs, "link_confidence", None),
                "file": getattr(obs.document, "dataset_file_id", None), "page": obs.page,
                "violation": (packs.rule_is_violation(code, next((o.normalized_value for o in observations
                              if o.stage == "PD" and o.location == obs.location), ""), obs.normalized_value)
                              if obs.stage == "RD" else None),
            })
        result["codes"][code] = {"seconds": round(time.monotonic() - t1, 1),
                                 "observations": sorted(rows, key=lambda r: (r["location"], r["stage"]))}
    if report is not None:
        result["room_identity"] = json.loads(json.dumps(report, default=str))
    Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print("written", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
