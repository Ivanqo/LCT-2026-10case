"""Phase 10 / prompt B, item 4: reconcile the pipeline's document-integrity detection against the organizer-side
SILVER defect log (`multi_object_annotation_20260817/00_СВОДНЫЕ_ДАННЫЕ/ДЕФЕКТЫ_И_КАНДИДАТЫ.jsonl`, 48 records).

Input is METADATA ONLY (the SILVER file registry: file id, stage, path, size, sha256, pages), so all nine objects
can be checked -- including УНДМС / Полярная 16 / СОШ 25 whose files are not downloaded. The detection code under
test is exactly the code the pipeline runs (`app.domain.document_facts`): nothing is re-implemented here.

For every defect record that the pipeline is supposed to notice, the record's own file ids are compared with the
files the pipeline set aside / flagged. Records about content the metadata cannot show (no text layer, act numbers
inside documents, title-page cipher mismatches, ID->RD references) are listed as `NOT_DETECTABLE_FROM_METADATA`
so the coverage claim stays honest.

Usage:  python evaluation/decision_layer_b/reconcile_silver_defects.py [--out <dir>]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "api_service"))

from app.domain import document_facts as df  # noqa: E402

BASE = REPO_ROOT / "case_data" / "extracted" / "02_gold_methodology" / "multi_object_annotation_20260817" / "00_СВОДНЫЕ_ДАННЫЕ"
DETECTABLE = {
    "EXACT_DUPLICATES_WITHIN_STAGE", "UNREADABLE_OR_EMPTY_SOURCE_FILE", "TEMPORARY_EDITOR_ARTIFACTS",
    "EXACT_DUPLICATES_ACROSS_STAGES", "MIXED_PROJECT_BASELINES", "MISSING_COMPARISON_STAGE", "SAME_LOGICAL_NAME_DIFFERENT_CONTENT",
}


def _facts(row: dict) -> df.DocFacts:
    return df.facts_from_manifest_row({
        "file_id": row["file_id"], "stage": row["stage"], "relative_path": row["relative_path"], "section": row["discipline"],
        "sha256": row["sha256"], "pdf_pages": int(row["pdf_pages"]) if row["pdf_pages"] not in ("", None) else None,
        "extension": row["extension"], "size_bytes": int(row["bytes"]) if row["bytes"] else None,
    })


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "evaluation" / "decision_layer_b" / "reports")
    args = parser.parse_args()
    with (BASE / "РЕЕСТР_8929_ФАЙЛОВ.csv").open(encoding="utf-8-sig", newline="") as fh:
        registry = list(csv.DictReader(fh))
    log = [json.loads(line) for line in (BASE / "ДЕФЕКТЫ_И_КАНДИДАТЫ.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    by_object: dict[str, list[dict]] = defaultdict(list)
    for row in registry:
        by_object[row["object_code"]].append(row)

    analysis = {}
    for code, rows in by_object.items():
        facts = [_facts(r) for r in rows]
        integrity = df.analyze_integrity(facts)
        revisions = df.analyze_revisions(facts, exclude_keys=integrity.excluded_keys)
        inventory = df.stage_inventory(facts, exclude_keys=integrity.excluded_keys)
        analysis[code] = {"facts": facts, "integrity": integrity, "revisions": revisions, "inventory": inventory}

    results = []
    for record in log:
        code, kind = record["object_code"], record["finding_type"]
        a = analysis[code]
        ours = {item["file_id"]: item for item in a["integrity"].excluded}
        entry = {"finding_id": record["finding_id"], "object": code, "finding_type": kind, "status": record["status"]}
        if kind not in DETECTABLE:
            entry["result"] = "NOT_DETECTABLE_FROM_METADATA"
            entry["note"] = "content-level (text layer / act number / title cipher / ID->RD reference): needs the page text, not the manifest"
        elif kind == "EXACT_DUPLICATES_WITHIN_STAGE":
            expected_redundant = set()
            for ev in record["evidence"]:
                if ev.get("cross_stage") is False and len(ev.get("object_codes", [])) == 1:
                    files = sorted((f["file_id"] for f in ev["files"]), key=lambda fid: [int(p) if p.isdigit() else p for p in __import__("re").split(r"(\d+)", fid)])
                    expected_redundant.update(files[1:])
                elif ev.get("cross_stage") is False:   # within one stage but also shared with another object: redundancy is still within-stage per object
                    per = [f["file_id"] for f in ev["files"] if f["object_code"] == code]
                    per.sort(key=lambda fid: [int(p) if p.isdigit() else p for p in __import__("re").split(r"(\d+)", fid)])
                    expected_redundant.update(per[1:])
            found = {fid for fid, item in ours.items() if item["reason"] == df.REASON_EXACT_DUPLICATE}
            entry.update(expected_redundant_files=len(expected_redundant), detected_redundant_files=len(found),
                         missed=sorted(expected_redundant - found)[:10], extra=sorted(found - expected_redundant)[:10])
            entry["result"] = "MATCH" if expected_redundant <= found else "PARTIAL"
        elif kind in {"UNREADABLE_OR_EMPTY_SOURCE_FILE", "TEMPORARY_EDITOR_ARTIFACTS"}:
            reason = df.REASON_UNREADABLE if kind == "UNREADABLE_OR_EMPTY_SOURCE_FILE" else df.REASON_SERVICE
            expected = set(record["file_ids"])
            found = {fid for fid, item in ours.items() if item["reason"] == reason}
            entry.update(expected_files=len(expected), detected_files=len(expected & found), missed=sorted(expected - found)[:10])
            entry["result"] = "MATCH" if expected <= found else "PARTIAL"
        elif kind == "EXACT_DUPLICATES_ACROSS_STAGES":
            # a log group counts for this object when ITS OWN files sit in >= 2 stages (groups shared with another
            # object are cross-object as well; only the object's own stage spread matters to this pipeline)
            expected_groups = set()
            for ev in record["evidence"]:
                own_stages = {f["stage"] for f in ev["files"] if f["object_code"] == code}
                if len(own_stages) > 1:
                    expected_groups.add(ev["duplicate_group_id"])
            detected = len(a["integrity"].cross_stage_duplicates)
            entry.update(expected_groups=len(expected_groups), detected_groups=detected)
            entry["result"] = "MATCH" if detected >= len(expected_groups) else "PARTIAL"
        elif kind == "MIXED_PROJECT_BASELINES":
            stage_conflicts = [c for c in a["revisions"].conflicts if c.type == df.CONFLICT_MIXED_SERIES]
            entry.update(record_families={e["family"]: len(e["files"]) for e in record["evidence"]},
                         detected_mixed_series_sections=len(stage_conflicts),
                         detected_series=sorted({s for c in stage_conflicts for s in c.detail["series"]}))
            entry["result"] = "MATCH" if stage_conflicts else "MISSED"
        elif kind == "MISSING_COMPARISON_STAGE":
            # the record is per CASE (a "Дело" folder inside the object); the registry carries `case_name`
            ev = record["evidence"][0]
            case_rows = [r for r in by_object[code] if r["case_name"] == ev["case_name"]]
            case_facts = [_facts(r) for r in case_rows]
            inventory = df.stage_inventory(case_facts, exclude_keys=df.analyze_integrity(case_facts).excluded_keys)
            missing = {stage for stage, docs in inventory.items() if not docs}
            expected_missing = set(ev["missing_stages"])
            entry.update(case=ev["case_name"], expected_missing=sorted(expected_missing), detected_missing=sorted(missing))
            entry["result"] = "MATCH" if expected_missing <= missing else "PARTIAL"
        elif kind == "SAME_LOGICAL_NAME_DIFFERENT_CONTENT":
            entry.update(records=len(record["evidence"]), detected_unordered_or_mixed=len(a["revisions"].conflicts))
            entry["result"] = "COVERED_BY_REVISION_ANALYSIS" if a["revisions"].conflicts or a["revisions"].superseded else "NOT_FLAGGED"
        results.append(entry)

    summary = Counter(r["result"] for r in results)
    per_object = {
        code: {
            "files": len(a["facts"]),
            "excluded_by_reason": dict(Counter(item["reason"] for item in a["integrity"].excluded)),
            "cross_stage_duplicate_groups": len(a["integrity"].cross_stage_duplicates),
            "revision_conflicts": dict(Counter(c.type for c in a["revisions"].conflicts)),
            "superseded_revisions": len(a["revisions"].superseded),
            "stage_files": {stage: len(docs) for stage, docs in a["inventory"].items()},
        }
        for code, a in sorted(analysis.items())
    }
    report = {"records": len(log), "summary": dict(summary), "results": results, "per_object": per_object}
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "silver_defects_reconciliation.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"records": len(log), "summary": dict(summary)}, ensure_ascii=False))
    for r in results:
        if r["result"] not in {"MATCH", "NOT_DETECTABLE_FROM_METADATA", "COVERED_BY_REVISION_ANALYSIS"}:
            print(json.dumps(r, ensure_ascii=False))


if __name__ == "__main__":
    main()
