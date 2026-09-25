"""Metric 3 -- real discrepancies (organiser's domain_violation_reannotation_20260817/annotations.jsonl).

For every annotated finding the REAL pipeline (`run_process`, the code tree in use) is run on the object and, per finding, we
record: does ANY check (a) cite the annotated (file_id, page) exactly, (b) cite the same document on other pages, and with
which parameter_code / location / label / protocol_status; for misses the cause (NO_MECHANISM: no catalog parameter can express
the finding; plus the text-layer state of the annotated pages).  Only an exact-page VIOLATION_PRESENT counts as a catch: a
VIOLATION_PRESENT in the same (often 100+ page) document on another parameter is reported as `incidental`, not as a catch.
Objects that are not on disk (УНДМС, Полярная 16, СОШ 25) are reported `not_available` -- never skipped silently.
POL17-N01 (negative pair) is the false-positive probe: FP = a VIOLATION_PRESENT citing one of the pair's pages.

Every VIOLATION_PRESENT emitted on the six available objects is also listed (`unlabelled_positives`) and, where the frozen value
corpus has both compared triples, adjudicated with the Metric-1 statuses (GENUINE_CANDIDATE / VALUE_ERROR / NOT_ADJUDICABLE).

  python -m evaluation.measurement_bench.metric3            # run the pipeline (minutes) and evaluate
  python -m evaluation.measurement_bench.metric3 --reeval   # evaluate the stored runs only
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter, defaultdict
from typing import Any

from .common import ANNOTATIONS_JSONL, OUT_DIR, OBJECTS, read_jsonl, utcnow_iso
from .location_convention import location_ok
from .metric1 import rate
from .pipeline_env import code_fingerprint, object_root, open_db, patch_bytes, require_frozen_corpus

OBJECT_BY_FINDING = {
    "ALT79B-V01": "ALT79B", "UNDMS-V01": None, "IZM12-V01": "IZM12", "LOS3A-V01": "LOS3A", "OKT103-V01": "OKT103",
    "POL16-V01": None, "DOO25-V01": "DOO25", "SOSH25-V01": None, "POL17-N01": "POL17",
}
# What the finding IS, judged from the organiser's own `expected`/`actual` text (not from any pipeline output):
#   AREA_EXPLICATION  room functions / floor areas / exposition tables
#   DRAWING_GEOMETRY  door position, pile repair scheme, armoured sheet, deviations vs tolerances on an executive scheme
FINDING_NATURE = {
    "ALT79B-V01": ("AREA_EXPLICATION", "функции помещений и итоговые площади этажей (экспликация)"),
    "UNDMS-V01": ("DRAWING_GEOMETRY", "исключение бронированного листа из состава покрытия"),
    "IZM12-V01": ("DRAWING_GEOMETRY", "локальная схема ремонта свай в ИД"),
    "LOS3A-V01": ("DRAWING_GEOMETRY", "перенос двери санузла кв. 2.4.1"),
    "OKT103-V01": ("DRAWING_GEOMETRY", "геометрические отклонения исполнительной схемы больше допусков"),
    "POL16-V01": ("AREA_EXPLICATION", "площади квартир (экспликация)"),
    "DOO25-V01": ("AREA_EXPLICATION", "площади помещений пищеблока 135-150"),
    "SOSH25-V01": ("AREA_EXPLICATION", "новое помещение 1.109 и итог площади 1-го этажа"),
    "POL17-N01": ("NEGATIVE_PAIR", "отрицательная контрольная пара"),
}
# nearest catalog parameters and why they do not express the finding (from the catalog names, not from pipeline output)
CLOSEST_CATALOG = {
    "AREA_EXPLICATION": (["PZ-002", "PZ-003"], "whole-building totals only; no catalog parameter for per-floor / per-room area or room function"),
    "DRAWING_GEOMETRY": ([], "a change of position / scheme / composition or a deviation-vs-tolerance on a drawing: no scalar catalog parameter to compare"),
}
ADJUDICATED_OK = {"CORRECT"}
ADJUDICATED_WRONG = {"WRONG_VALUE", "WRONG_ROW", "ORDINAL", "CITATION_NUMBER", "GATED", "NOT_TAGGED", "NO_VALUE"}
STAGE_OF_FRAGMENT = {"project": "PD", "working": "RD", "as_built": "ID"}


def run_object(obj: str) -> dict:
    from app.domain.v3_pipeline import MATRIX_VERSION_OFFICIAL, create_process, latest_protocol, protocol_to_dict, run_process
    from evaluation.exporter import protocol_to_submission

    patch_bytes(obj)
    db, engine = open_db(obj, fresh=False)
    t0 = time.monotonic()
    process = create_process(db, project_id=1, organization_id=1, object_id=OBJECTS[obj]["object_id"], matrix_version=MATRIX_VERSION_OFFICIAL)
    run_process(db, process_id=process.id)
    protocol = latest_protocol(db, project_id=1, organization_id=1, process_id=process.id)
    pd = protocol_to_dict(protocol)
    submission = protocol_to_submission(pd, code_style="legacy")  # keyed by internal codes below (groups, location_ok)
    groups = pd["payload"]["findings"]
    out = {
        "object": obj, "seconds": round(time.monotonic() - t0, 1), "n_checks": len(submission["checks"]),
        "labels": dict(Counter(c["violation_label"] for c in submission["checks"])),
        "checks": submission["checks"],
        "groups": [{"parameter_code": (g.get("parameter") or {}).get("code"), "finding_status": g.get("finding_status"),
                    "model_version": g.get("model_version"), "delta": {k: (g.get("delta") or {}).get(k) for k in ("reason", "location", "comparison_result", "values", "source")},
                    "fragments": [{k: f.get(k) for k in ("stage", "file_id", "page", "extracted_value", "extractor")} for f in g.get("fragments") or []]} for g in groups],
    }
    db.close()
    return out


def _words_on_page(obj: str, file_id: str, page: int) -> int | None:
    """Text-layer probe of the ORIGINAL pdf page (independent of the pipeline): number of words fitz finds there."""
    try:
        import fitz

        from .reader import manifest_rows

        row = next(m for m in manifest_rows(obj) if m["file_id"] == file_id)
        with fitz.open(object_root(obj) / row["relative_path"].replace("/", "\\")) as doc:
            return len(doc[page - 1].get_text("words"))
    except Exception:  # noqa: BLE001 - diagnostics only
        return None


def _adjudicate_positive(obj: str, code: str, stages: list[str], m1: list[dict]) -> dict:
    """Judge an emitted VIOLATION_PRESENT against the frozen value corpus (Metric-1 statuses of the same triples): both compared
    values correct and the corpus values differ -> GENUINE_CANDIDATE; one side wrong -> VALUE_ERROR; no corpus triple -> NOT_ADJUDICABLE."""
    sides: dict[str, Any] = {}
    for st in stages:
        row = next((x for x in m1 if x["obj"] == obj and x["param_code"] == code and x["stage"] == st), None)
        sides[st] = None if row is None else {"status": row["status"], "sub": row.get("sub"), "true_norm": row.get("true_norm")}
    if any(v is not None and v["status"] in ADJUDICATED_WRONG for v in sides.values()):
        verdict = "VALUE_ERROR"                       # one wrong side is enough: the emitted comparison is not between the truth values
    elif not sides or any(v is None for v in sides.values()):
        verdict = "NOT_ADJUDICABLE"
    elif all(v["status"] in ADJUDICATED_OK for v in sides.values()):
        verdict = "GENUINE_CANDIDATE" if len({v["true_norm"] for v in sides.values()}) > 1 else "TRUE_VALUES_EQUAL"
    else:
        verdict = "NOT_ADJUDICABLE"
    return {"verdict": verdict, "sides": sides}


def evaluate(annotations: list[dict], runs: dict[str, dict], ext: dict[str, dict], m1: list[dict] | None = None) -> dict:
    from .critical_recall_matrix import ANNOTATION_MAP

    m1 = m1 or []
    by_finding: dict[str, list[dict]] = defaultdict(list)
    for a in annotations:
        by_finding[a["finding_id"]].append(a)
    cases = []
    for fid, rows in sorted(by_finding.items()):
        obj = OBJECT_BY_FINDING.get(fid)
        nature, why = FINDING_NATURE[fid]
        case: dict[str, Any] = {"finding_id": fid, "status_in_annotations": rows[0]["status"], "nature": nature, "nature_note": why,
                                "expected": rows[0]["expected"], "actual": rows[0]["actual"],
                                "annotated_pages": [{"stage": r["stage"], "file_id": r["source_file_id"], "page": r["page"]} for r in rows]}
        if obj is None:
            case.update(availability="not_available", note="object documents are not on disk (not downloaded); reported, not skipped")
            cases.append(case)
            continue
        case["availability"] = "available"
        for ap in case["annotated_pages"]:
            ap["words_on_page"] = _words_on_page(obj, ap["file_id"], ap["page"])
        run = runs.get(obj)
        if run is None or "error" in run:
            case.update(run_error=(run or {}).get("error", "no run"))
            cases.append(case)
            continue
        exact_pairs = {(r["source_file_id"], r["page"]) for r in rows}
        docs = {r["source_file_id"] for r in rows}
        hits = []
        for c in run["checks"]:
            ev = {(e["file_id"], e["pdf_page_number"]) for e in c["evidence"]}
            exact = sorted(ev & exact_pairs)
            same_doc = sorted({e["file_id"] for e in c["evidence"]} & docs)
            if exact or same_doc:
                hits.append({"parameter_code": c["parameter_code"], "location": c["location"], "label": c["violation_label"], "protocol_status": c["protocol_status"],
                             "cited_exact_pages": [list(x) for x in exact], "cited_same_document": same_doc,
                             "cited_pages_all": sorted([e["file_id"], e["pdf_page_number"]] for e in c["evidence"]),
                             "location_matches_site_convention": location_ok(c["parameter_code"], c["location"])[0]})
        viol = [h for h in hits if h["label"] == "VIOLATION_PRESENT"]
        exact_viol = [h for h in viol if h["cited_exact_pages"]]
        incidental = [h for h in viol if not h["cited_exact_pages"] and h["cited_same_document"]]
        case["checks_touching_annotated_pages"] = len(hits)
        case["violation_present_exact_page"] = len(exact_viol)
        case["violation_present_same_document_other_pages"] = len(incidental)
        case["incidental_same_document_positives"] = [{"parameter_code": h["parameter_code"], "cited_pages": h["cited_pages_all"]} for h in incidental]
        case["hits"] = hits[:12]
        e = ext.get(obj)
        if e is not None:
            tagged = defaultdict(list)
            for f in e["fragments"]:
                if (f["file_id"], f["page"]) in exact_pairs:
                    tagged[f"{f['file_id']}:{f['page']}"].append(f["code"])
            obs_on = []
            for code, st in e["observations"].items():
                for stage, o in st.items():
                    if (o["file_id"], o["page"]) in exact_pairs:
                        obs_on.append({"code": code, "stage": stage, "value": o.get("value") or o.get("values") or o.get("row_count")})
            case["tagged_codes_on_annotated_pages"] = {k: sorted(set(v)) for k, v in tagged.items()}
            case["observations_on_annotated_pages"] = obs_on
        if nature == "NEGATIVE_PAIR":
            case["false_positive_on_pair"] = bool(exact_viol)
            case["verdict"] = "FALSE_POSITIVE_ON_PAIR" if exact_viol else "NO_FALSE_POSITIVE_ON_PAIR"
        elif exact_viol:
            case["verdict"] = "CAUGHT_EXACT_PAGE"
        else:
            nearest, note = CLOSEST_CATALOG[nature]
            no_text = [ap for ap in case["annotated_pages"] if ap.get("words_on_page") == 0]
            case.update(verdict="MISSED", miss_reason="NO_MECHANISM", miss_reason_detail=note,
                        closest_catalog_params=ANNOTATION_MAP.get(fid, nearest),
                        also_blocked_by=[f"NO_TEXT_LAYER on {len(no_text)} annotated page(s): OCR would be needed"] if no_text else [])
        cases.append(case)
    positives = []
    for obj, run in runs.items():
        if "error" in run:
            continue
        annotated = {(r["source_file_id"], r["page"]) for r in annotations if OBJECT_BY_FINDING.get(r["finding_id"]) == obj and r["status"] != "negative"}
        pair_pages = {(r["source_file_id"], r["page"]) for r in annotations if OBJECT_BY_FINDING.get(r["finding_id"]) == obj and r["status"] == "negative"}
        groups = {g["parameter_code"]: g for g in run["groups"]}
        for c in run["checks"]:
            if c["violation_label"] != "VIOLATION_PRESENT":
                continue
            g = groups.get(c["parameter_code"], {})
            stages = sorted({STAGE_OF_FRAGMENT.get(f["stage"], f["stage"]) for f in g.get("fragments") or []}, key=["PD", "RD", "ID"].index)
            cited = {(e["file_id"], e["pdf_page_number"]) for e in c["evidence"]}
            positives.append({"object": obj, "parameter_code": c["parameter_code"], "location": c["location"], "protocol_status": c["protocol_status"],
                              "values": (g.get("delta") or {}).get("values"), "cited_pages": sorted([f, p] for f, p in cited),
                              "on_annotated_discrepancy_page": bool(cited & annotated), "on_negative_pair_page": bool(cited & pair_pages),
                              "adjudication": _adjudicate_positive(obj, c["parameter_code"], stages, m1)})
    adj = Counter(p["adjudication"]["verdict"] for p in positives)
    findings = [c for c in cases if c["nature"] != "NEGATIVE_PAIR"]
    avail = [c for c in findings if c["availability"] == "available"]
    exact = [c for c in avail if c.get("verdict") == "CAUGHT_EXACT_PAGE"]
    adjudicable = adj.get("GENUINE_CANDIDATE", 0) + adj.get("VALUE_ERROR", 0) + adj.get("TRUE_VALUES_EQUAL", 0)
    return {
        "cases": cases,
        "unlabelled_positives": positives,
        "summary": {
            "findings_total": len(findings), "available": len(avail), "not_available": sum(1 for c in findings if c["availability"] == "not_available"),
            "caught_exact_page": len(exact),
            "recall_exact_page_over_available": rate(len(exact), len(avail)),
            "recall_exact_page_over_all_findings": rate(len(exact), len(findings)),
            "missed_by_reason": dict(Counter(c.get("miss_reason") for c in avail if c.get("verdict") == "MISSED")),
            "same_document_incidental_findings": [c["finding_id"] for c in avail if c.get("violation_present_same_document_other_pages")],
            "negative_pair_false_positive_on_pair": next((c.get("false_positive_on_pair") for c in cases if c["nature"] == "NEGATIVE_PAIR"), None),
            "violation_present_total_on_available_objects": len(positives),
            "violation_present_on_annotated_discrepancy_pages": sum(1 for p in positives if p["on_annotated_discrepancy_page"]),
            "positives_adjudication": dict(adj),
            "positives_precision_on_adjudicable": rate(adj.get("GENUINE_CANDIDATE", 0), adjudicable) if adjudicable else None,
        },
    }


def main() -> int:
    manifest = require_frozen_corpus()
    reeval = "--reeval" in sys.argv
    annotations = read_jsonl(ANNOTATIONS_JSONL)
    needed = sorted({OBJECT_BY_FINDING[a["finding_id"]] for a in annotations if OBJECT_BY_FINDING.get(a["finding_id"])})
    runs: dict[str, dict] = {}
    ext: dict[str, dict] = {}
    for obj in needed:
        p = OUT_DIR / f"extract_{obj}.json"
        if p.exists():
            ext[obj] = json.loads(p.read_text(encoding="utf-8"))
    if reeval:
        runs = json.loads((OUT_DIR / "metric3_runs_full.json").read_text(encoding="utf-8"))
    else:
        for obj in needed:
            try:
                runs[obj] = run_object(obj)
                print(obj, runs[obj]["labels"], runs[obj]["seconds"], flush=True)
            except Exception as exc:  # noqa: BLE001
                runs[obj] = {"error": f"{type(exc).__name__}: {exc}"}
                print(obj, "RUN_ERROR", runs[obj]["error"], flush=True)
        (OUT_DIR / "metric3_runs_full.json").write_text(json.dumps(runs, ensure_ascii=False, default=str), encoding="utf-8")
    m1 = json.loads((OUT_DIR / "metric1_results.json").read_text(encoding="utf-8"))["results"]
    result = evaluate(annotations, runs, ext, m1)
    result.update(generated_at=utcnow_iso(), corpus_sha256=manifest["corpus_sha256"], code_fingerprint=code_fingerprint()["combined"],
                  runs={o: {k: v for k, v in r.items() if k not in ("checks", "groups")} for o, r in runs.items()})
    (OUT_DIR / "metric3_results.json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(json.dumps(result["summary"], ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
