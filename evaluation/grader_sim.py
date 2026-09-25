"""Grader simulator for ТЗ 1.1 (§14.3 and sheet «МЕТРИКИ» of Матрица_параметров_редакция1.1.xlsx).

The official metric script is not published (expert session §33); this is our reading of the sheet, made explicit so
every Phase 12 change is measured the same way.

Unit of scoring: an evidence group. A gold item says "on object X, parameter P at location L there is (POSITIVE) / there
is verified no (NEGATIVE) discrepancy, shown on these pages [with these rectangles]". A prediction is a `checks[]` item of
our submission; it is positive when `violation_label == VIOLATION_PRESENT` (CANDIDATE or CONFIRMED_VIOLATION).

Match (variants, all reported):
  key_only                     object + code + location (STRICT location); diagnostic, no evidence required
  evidence_page   (PRIMARY)    key + evidence: for every stage of the gold evidence, one of its (file_id, page) is cited
  evidence_page_loose_location as PRIMARY, location LOOSE (room "012" ~ "12", element-name containment)
  evidence_strict              PRIMARY + the cited bbox has IoU >= 0.50 with a gold rectangle of that page; gold items
                               without rectangles are excluded from this variant (reported as not evaluable)
Codes match in either convention (M-078 == IOS4-078 via the matrix 1.1 table); FREE-* codes match literally.

Page-pair gold (the 9 cases of the organizer's domain re-annotation carry pages and rectangles but no parameter code):
  key_only = the page pair only (for every annotated stage one annotated page is cited, same object);
  evidence_page = page pair + our ASSUMED code mapping (`assumed_codes`, documented with the gold set) -- an assumption,
  not organizer data; evidence_strict = + IoU.

Precision, two flavours (the hidden test's labelling density is unknown):
  closed  FP = positive predictions that touch a LABELLED key (a gold negative's key, or a gold positive's key/pages
          with a wrong code or wrong evidence); positives on unlabelled keys are ignored;
  open    FP = every positive prediction on the gold set's objects that is not a TP.
FPR = gold negatives flagged by a positive prediction on their key (or their pages) / gold negatives.
Localization completeness = among gold positives found at key level, the share whose evidence also matches (page /
strict). Document linkage = cited evidence whose file belongs to the object in the registry, with the registry's stage;
code + revision are checked only where the registry carries them. Every rate comes with its sample size and a 95%
Wilson interval. `world` separates independent runs on the same object (one per seeded case in set D).
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import math
from pathlib import Path
import sys
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
for _path in (REPO_ROOT, REPO_ROOT / "api_service"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from app.domain import matrix_v11  # noqa: E402
from evaluation.measurement_bench.location_convention import match_level  # noqa: E402

POSITIVE = "POSITIVE"
NEGATIVE = "NEGATIVE"
MATCH_KEY = "KEY"
MATCH_PAGE_PAIR = "PAGE_PAIR"
KEY_ONLY = "key_only"
EVIDENCE_PAGE = "evidence_page"
EVIDENCE_PAGE_LOOSE = "evidence_page_loose_location"
EVIDENCE_STRICT = "evidence_strict"
VARIANTS = (KEY_ONLY, EVIDENCE_PAGE, EVIDENCE_PAGE_LOOSE, EVIDENCE_STRICT)
PRIMARY_VARIANT = EVIDENCE_PAGE
IOU_THRESHOLD = 0.5
VERDICT_LABELS = {"VIOLATION_PRESENT", "NO_VIOLATION"}


# ------------------------------------------------------------------ statistics


def wilson(k: int, n: int, z: float = 1.96) -> dict[str, Any]:
    if n <= 0:
        return {"k": k, "n": n, "rate": None, "wilson95": None}
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return {"k": k, "n": n, "rate": round(p, 4), "wilson95": [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]}


def _f1(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None:
        return None
    return round(2 * precision * recall / (precision + recall), 4) if precision + recall else 0.0


def iou(a: Iterable[float], b: Iterable[float]) -> float:
    ax0, ay0, ax1, ay1 = a
    bx0, by0, bx1, by1 = b
    inter = max(0.0, min(ax1, bx1) - max(ax0, bx0)) * max(0.0, min(ay1, by1) - max(ay0, by0))
    union = (ax1 - ax0) * (ay1 - ay0) + (bx1 - bx0) * (by1 - by0) - inter
    return inter / union if union > 0 else 0.0


# ------------------------------------------------------------------ inputs


def gold_item(
    *,
    gold_id: str,
    object_id: str,
    label: str,
    code: str | None = None,
    location: str | None = None,
    evidence: Iterable[dict[str, Any]] = (),
    match_mode: str = MATCH_KEY,
    assumed_codes: Iterable[str] = (),
    set_name: str = "",
    tier: str = "",
    section: str | None = None,
    violation_type: str | None = None,
    critical: bool | None = None,
    score_eligible: bool = True,
    world: str | None = None,
    source_status: str | None = None,
    available: bool = True,
) -> dict[str, Any]:
    """One gold evidence group in the simulator's normalized shape (JSON-serialisable)."""
    if label not in (POSITIVE, NEGATIVE):
        raise ValueError(f"label must be {POSITIVE}/{NEGATIVE}, got {label!r}")
    assumed = tuple(assumed_codes)
    row = matrix_v11.lookup(code) if code else (matrix_v11.lookup(assumed[0]) if assumed else None)
    return {
        "gold_id": gold_id, "set": set_name, "tier": tier, "object_id": object_id, "world": world or object_id,
        "label": label, "code": code, "code_key": matrix_v11.canonical_code(code) if code else None,
        "location": location, "match_mode": match_mode,
        "assumed_codes": list(assumed), "assumed_code_keys": sorted({matrix_v11.canonical_code(c) for c in assumed}),
        "evidence": [_gold_evidence(item) for item in evidence],
        "section": section or (row or {}).get("pd_section") or ("FREE_SEARCH" if code else "NO_CODE"),
        "violation_type": violation_type,
        "critical": bool(critical) if critical is not None else bool(row and row["review_priority"] == "HIGH"),
        "score_eligible": bool(score_eligible), "source_status": source_status, "available": bool(available),
    }


def _gold_evidence(item: dict[str, Any]) -> dict[str, Any]:
    rects = [list(map(float, rect)) for rect in (item.get("rects") or []) if rect and len(rect) == 4]
    return {"stage": str(item.get("stage") or "").upper(), "file_id": str(item["file_id"]), "page": int(item["page"]), "rects": rects}


def normalize_prediction(check: dict[str, Any], *, object_id: str | None = None, world: str | None = None) -> dict[str, Any]:
    """A submission check (either code style) in the simulator's shape."""
    object_id = str(object_id or check.get("object_id") or "")
    code = check.get("parameter_code_legacy") or check.get("parameter_code")
    evidence = []
    for item in check.get("evidence") or []:
        page = item.get("pdf_page_number") or item.get("page")
        if not item.get("file_id") or not page:
            continue
        bbox = item.get("bbox_norm")
        evidence.append({
            "stage": str(item.get("stage") or "").upper(), "file_id": str(item["file_id"]), "page": int(page),
            "bbox": [float(v) for v in bbox] if isinstance(bbox, (list, tuple)) and len(bbox) == 4 else None,
            "document_code": item.get("document_code"), "revision": item.get("revision"),
        })
    return {
        "world": world or object_id, "object_id": object_id, "code": check.get("parameter_code"),
        "code_key": matrix_v11.canonical_code(code), "location": check.get("location"),
        "label": check.get("violation_label"), "positive": check.get("violation_label") == "VIOLATION_PRESENT",
        "evidence": evidence,
    }


def load_submission(path: str | Path, *, world: str | None = None) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    object_id = payload.get("object_id")
    return [normalize_prediction(check, object_id=object_id, world=world) for check in payload.get("checks") or []]


# ------------------------------------------------------------------ matching


def _key_match(p: dict[str, Any], g: dict[str, Any], *, loose: bool = False) -> bool:
    if p["world"] != g["world"] or p["code_key"] != g["code_key"]:
        return False
    if g["location"] is None:
        return True
    level = match_level(g["location"], p["location"])
    return level == "STRICT" or (loose and level == "LOOSE")


def _cited(p: dict[str, Any]) -> dict[tuple[str, int], list[list[float] | None]]:
    cited: dict[tuple[str, int], list[list[float] | None]] = defaultdict(list)
    for item in p["evidence"]:
        cited[(item["file_id"], item["page"])].append(item["bbox"])
    return cited


def strict_evaluable(g: dict[str, Any]) -> bool:
    by_stage: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in g["evidence"]:
        by_stage[row["stage"]].append(row)
    return bool(by_stage) and all(any(row["rects"] for row in rows) for rows in by_stage.values())


def _evidence_match(p: dict[str, Any], g: dict[str, Any], *, strict: bool) -> bool | None:
    """True/False, or None when the gold cannot certify it (no evidence; no rectangles for the strict check)."""
    if not g["evidence"] or (strict and not strict_evaluable(g)):
        return None
    cited = _cited(p)
    by_stage: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in g["evidence"]:
        by_stage[row["stage"]].append(row)
    for rows in by_stage.values():
        hit = False
        for row in rows:
            boxes = cited.get((row["file_id"], row["page"]))
            if boxes is None:
                continue
            if not strict or any(box and iou(box, rect) >= IOU_THRESHOLD for box in boxes for rect in row["rects"]):
                hit = True
                break
        if not hit:
            return False
    return True


def _cites_gold_page(p: dict[str, Any], g: dict[str, Any]) -> bool:
    pages = {(row["file_id"], row["page"]) for row in g["evidence"]}
    return p["world"] == g["world"] and any((item["file_id"], item["page"]) in pages for item in p["evidence"])


def touches(p: dict[str, Any], g: dict[str, Any]) -> bool:
    """The prediction is about this gold item's labelled key (KEY) or its annotated pages (PAGE_PAIR)."""
    return _cites_gold_page(p, g) if g["match_mode"] == MATCH_PAGE_PAIR else _key_match(p, g)


def identity_match(p: dict[str, Any], g: dict[str, Any]) -> bool:
    """Key-level match used by localization completeness: KEY = object+code+location; PAGE_PAIR = object + assumed code."""
    if g["match_mode"] == MATCH_PAGE_PAIR:
        return p["world"] == g["world"] and p["code_key"] in g["assumed_code_keys"]
    return _key_match(p, g)


def criterion(variant: str, p: dict[str, Any], g: dict[str, Any]) -> bool | None:
    strict = variant == EVIDENCE_STRICT
    if g["match_mode"] == MATCH_PAGE_PAIR:
        if p["world"] != g["world"]:
            return False
        pages = _evidence_match(p, g, strict=strict)
        if variant == KEY_ONLY or pages is not True:
            return pages
        return p["code_key"] in g["assumed_code_keys"]
    if not _key_match(p, g, loose=variant == EVIDENCE_PAGE_LOOSE):
        return False
    if variant == KEY_ONLY:
        return True
    return _evidence_match(p, g, strict=strict)


# ------------------------------------------------------------------ scoring


def score_variant(golds: list[dict[str, Any]], preds: list[dict[str, Any]], variant: str = PRIMARY_VARIANT) -> dict[str, Any]:
    positives = [g for g in golds if g["label"] == POSITIVE]
    negatives = [g for g in golds if g["label"] == NEGATIVE]
    worlds = {g["world"] for g in golds}
    pos_preds = [p for p in preds if p["positive"] and p["world"] in worlds]
    evaluable = [g for g in positives if variant != EVIDENCE_STRICT or strict_evaluable(g)]
    evaluable_ids = {g["gold_id"] for g in evaluable}
    found: set[str] = set()
    outcome = Counter()
    for p in pos_preds:
        results = [(criterion(variant, p, g), g) for g in positives]
        hits = [g for result, g in results if result is True]
        if hits:
            outcome["tp"] += 1
            found.update(g["gold_id"] for g in hits)
        elif any(result is None and touches(p, g) for result, g in results):
            outcome["not_evaluable"] += 1
        elif any(touches(p, g) for g in positives):
            outcome["fp_labelled_positive_key"] += 1           # right key/pages, wrong evidence or code
        elif any(touches(p, g) for g in negatives):
            outcome["fp_labelled_negative_key"] += 1
        else:
            outcome["fp_unlabelled"] += 1
    fp_closed = outcome["fp_labelled_positive_key"] + outcome["fp_labelled_negative_key"]
    recall = wilson(len(found & evaluable_ids), len(evaluable))
    precision_closed = wilson(outcome["tp"], outcome["tp"] + fp_closed)
    precision_open = wilson(outcome["tp"], outcome["tp"] + fp_closed + outcome["fp_unlabelled"])
    flagged = [g for g in negatives if any(p["positive"] and touches(p, g) for p in preds)]
    critical = [g for g in evaluable if g["critical"]]
    return {
        "variant": variant,
        "n_gold_positive": len(positives), "n_gold_positive_evaluable": len(evaluable), "n_gold_negative": len(negatives),
        "n_pred_positive": len(pos_preds), "outcomes": dict(outcome),
        "recall": recall, "precision_closed": precision_closed, "precision_open": precision_open,
        "f1_closed": _f1(precision_closed["rate"], recall["rate"]), "f1_open": _f1(precision_open["rate"], recall["rate"]),
        "false_positive_rate": wilson(len(flagged), len(negatives)),
        "critical_recall": wilson(sum(g["gold_id"] in found for g in critical), len(critical)),
        "found": sorted(found), "missed": sorted(g["gold_id"] for g in evaluable if g["gold_id"] not in found),
        "negatives_flagged": sorted(g["gold_id"] for g in flagged),
    }


def localization_completeness(golds: list[dict[str, Any]], preds: list[dict[str, Any]]) -> dict[str, Any]:
    positives = [g for g in golds if g["label"] == POSITIVE]
    pos_preds = [p for p in preds if p["positive"]]
    at_key = [g for g in positives if any(identity_match(p, g) for p in pos_preds)]
    page_ok = [g for g in at_key if any(identity_match(p, g) and _evidence_match(p, g, strict=False) is True for p in pos_preds)]
    strict_pool = [g for g in at_key if strict_evaluable(g)]
    strict_ok = [g for g in strict_pool if any(identity_match(p, g) and _evidence_match(p, g, strict=True) is True for p in pos_preds)]
    return {"n_found_at_key": len(at_key), "file_page": wilson(len(page_ok), len(at_key)),
            "file_page_iou50": wilson(len(strict_ok), len(strict_pool)), "strict_not_evaluable": len(at_key) - len(strict_pool)}


def document_linkage(preds: list[dict[str, Any]], registry: dict[str, dict[str, dict[str, Any]]] | None) -> dict[str, Any]:
    """Cited evidence of verdict checks (VIOLATION_PRESENT / NO_VIOLATION) against the file registry:
    `registry[object_id][file_id] = {"stage", "excluded", "document_code"?, "revision"?}`."""
    if not registry:
        return {"available": False}
    n = exact = excluded = verifiable = code_rev_exact = 0
    for p in preds:
        if p["label"] not in VERDICT_LABELS:
            continue
        files = registry.get(p["object_id"], {})
        for item in p["evidence"]:
            n += 1
            entry = files.get(item["file_id"])
            if entry is None:
                continue
            stage = str(entry.get("stage") or "").upper()
            stage_ok = stage == item["stage"] or (stage == "RD_ID_MIXED" and item["stage"] in {"RD", "ID"})
            if entry.get("excluded"):
                excluded += 1
            exact += int(stage_ok and not entry.get("excluded"))
            if entry.get("document_code") and entry.get("revision"):
                verifiable += 1
                code_rev_exact += int(stage_ok and entry["document_code"] == item["document_code"] and str(entry["revision"]) == str(item["revision"]))
    return {"available": True, "n_cited_evidence": n, "object_stage_exact": wilson(exact, n), "cites_excluded_file": excluded,
            "code_revision_verifiable": verifiable, "code_revision_exact": wilson(code_rev_exact, verifiable)}


def _slices(golds: list[dict[str, Any]], preds: list[dict[str, Any]], field: str) -> dict[str, Any]:
    out = {}
    for value in sorted({str(g.get(field)) for g in golds}):
        subset = [g for g in golds if str(g.get(field)) == value]
        s = score_variant(subset, preds, PRIMARY_VARIANT)
        out[value] = {"n_pos": s["n_gold_positive"], "n_neg": s["n_gold_negative"], "recall": s["recall"],
                      "precision_closed": s["precision_closed"], "f1_closed": s["f1_closed"], "false_positive_rate": s["false_positive_rate"]}
    return out


def evaluate(golds: list[dict[str, Any]], preds: list[dict[str, Any]], *, registry: dict | None = None) -> dict[str, Any]:
    worlds = {g["world"] for g in golds}
    in_scope = [p for p in preds if p["world"] in worlds]
    return {
        "sample": {
            "gold": len(golds), "gold_positive": sum(g["label"] == POSITIVE for g in golds), "gold_negative": sum(g["label"] == NEGATIVE for g in golds),
            "objects": len({g["object_id"] for g in golds}), "worlds": len(worlds),
            "objects_without_predictions": sorted({g["object_id"] for g in golds} - {p["object_id"] for p in in_scope}),
            "predictions": len(in_scope), "predictions_positive": sum(p["positive"] for p in in_scope),
            "prediction_labels": dict(Counter(p["label"] for p in in_scope)),
        },
        "variants": {variant: score_variant(golds, in_scope, variant) for variant in VARIANTS},
        "localization_completeness": localization_completeness(golds, in_scope),
        "document_linkage": document_linkage(in_scope, registry),
        "by_tier": _slices(golds, in_scope, "tier"),
        "by_section": _slices(golds, in_scope, "section"),
        "by_type": _slices(golds, in_scope, "violation_type"),
    }


# ------------------------------------------------------------------ CLI


def _fmt(metric: dict[str, Any] | None) -> str:
    if not metric or metric.get("rate") is None:
        return f"— (n={metric.get('n', 0) if metric else 0})"
    lo, hi = metric["wilson95"]
    return f"{metric['rate']:.2f} ({metric['k']}/{metric['n']}; [{lo:.2f}; {hi:.2f}])"


def render_summary(report: dict[str, Any], title: str = "") -> str:
    lines = [f"### {title}" if title else "", "", "| вариант | recall | precision (закр.) | precision (откр.) | F1 закр./откр. | FPR |", "|---|---|---|---|---|---|"]
    for variant, s in report["variants"].items():
        lines.append(f"| {variant} | {_fmt(s['recall'])} | {_fmt(s['precision_closed'])} | {_fmt(s['precision_open'])} | "
                     f"{s['f1_closed'] if s['f1_closed'] is not None else '—'} / {s['f1_open'] if s['f1_open'] is not None else '—'} | {_fmt(s['false_positive_rate'])} |")
    loc = report["localization_completeness"]
    lines += ["", f"Полнота локализации: file+page {_fmt(loc['file_page'])}; +IoU≥0,5 {_fmt(loc['file_page_iou50'])} (без прямоугольников в gold: {loc['strict_not_evaluable']})."]
    link = report["document_linkage"]
    if link.get("available"):
        lines.append(f"Связка документов: объект+стадия {_fmt(link['object_stage_exact'])}; шифр+редакция проверяемы у {link['code_revision_verifiable']} из {link['n_cited_evidence']}; ссылок на исключённые файлы {link['cites_excluded_file']}.")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--gold", type=Path, required=True, help="JSONL of gold items (gold_item shape)")
    parser.add_argument("--pred", type=Path, nargs="+", required=True, help="submission.json files")
    parser.add_argument("--registry", type=Path, help="JSON {object_id: {file_id: {stage, excluded, document_code, revision}}}")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    golds = [json.loads(line) for line in args.gold.read_text(encoding="utf-8").splitlines() if line.strip()]
    preds = [p for path in args.pred for p in load_submission(path)]
    registry = json.loads(args.registry.read_text(encoding="utf-8")) if args.registry else None
    report = evaluate(golds, preds, registry=registry)
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    print(render_summary(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
