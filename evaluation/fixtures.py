from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from .errors import LeakageGuardError


HIDDEN_SPLITS = {"TEST_HIDDEN"}
HIDDEN_OBJECT_IDS = {"OBJ-RECHNIKOV-7-7"}
PUBLIC_OBJECT_IDS = ("OBJ-TYUMENSKAYA-5-GOLD-SEED", "OBJ-NOVOSLOBODSKAYA")
PUBLIC_TRAIN_VISIBILITY = "PUBLIC_TRAIN_LABEL"


def load_gold_checks_jsonl(
    path: str | Path,
    *,
    purpose: str = "evaluation",
    allow_hidden_labels: bool = False,
    allow_organizer_only: bool = False,
    include_blocked: bool = False,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for row in _read_jsonl(Path(path)):
        decision = leakage_decision(
            row,
            purpose=purpose,
            allow_hidden_labels=allow_hidden_labels,
            allow_organizer_only=allow_organizer_only,
        )
        if decision["allowed"]:
            rows.append(row | {"leakage_guard": decision})
        else:
            blocked.append({"check_id": row.get("check_id"), "object_id": row.get("object_id"), "reason": decision["reasons"]})
            # Blocked labels are never returned, even for diagnostic requests.
    return {
        "rows": rows,
        "blocked": blocked,
        "summary": {
            "allowed": len(rows) if not include_blocked else sum(1 for row in rows if row.get("leakage_guard", {}).get("allowed")),
            "blocked": len(blocked),
            "purpose": purpose,
            "allow_hidden_labels": allow_hidden_labels,
            "allow_organizer_only": allow_organizer_only,
        },
    }


def leakage_decision(
    row: dict[str, Any],
    *,
    purpose: str,
    allow_hidden_labels: bool = False,
    allow_organizer_only: bool = False,
) -> dict[str, Any]:
    normalized_purpose = str(purpose or "evaluation").strip().lower()
    split = str(row.get("split") or "").upper()
    visibility = str(row.get("visibility") or "").upper()
    object_id = str(row.get("object_id") or "")
    hidden = split in HIDDEN_SPLITS or object_id in HIDDEN_OBJECT_IDS
    organizer_only = visibility == "ORGANIZER_ONLY"
    reasons: list[str] = []

    if normalized_purpose == "training":
        if hidden:
            reasons.append("hidden_labels_forbidden_for_training")
        if organizer_only:
            reasons.append("organizer_only_forbidden_for_training")
        if visibility != PUBLIC_TRAIN_VISIBILITY or split != "TRAIN_PUBLIC":
            reasons.append("not_public_train_label")
    elif normalized_purpose == "evaluation":
        if hidden and not allow_hidden_labels:
            reasons.append("hidden_labels_blocked")
        if organizer_only and hidden and not allow_organizer_only:
            reasons.append("hidden_organizer_only_blocked")
    elif normalized_purpose in {"submission", "inference"}:
        if row.get("violation_label") is not None:
            reasons.append("labels_not_allowed_for_submission_or_inference")
    else:
        reasons.append(f"unknown_purpose:{normalized_purpose}")

    return {
        "allowed": not reasons,
        "purpose": normalized_purpose,
        "split": split,
        "visibility": visibility,
        "hidden": hidden,
        "organizer_only": organizer_only,
        "reasons": reasons,
    }


def gold_checks_to_evaluation_fixture(checks: Iterable[dict[str, Any]], *, total_params: int = 132) -> dict[str, Any]:
    findings: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    document_links: list[dict[str, Any]] = []
    object_splits: dict[str, str] = {}
    split_conflicts: dict[str, list[str]] = {}
    for row in checks:
        object_id = str(row.get("object_id") or "")
        split = str(row.get("split") or "")
        if object_id and split:
            previous = object_splits.get(object_id)
            if previous and previous != split:
                split_conflicts.setdefault(object_id, sorted({previous, split}))
            else:
                object_splits[object_id] = split
        if str(row.get("matrix_scope") or "MATRIX").upper() != "MATRIX":
            continue
        check_id = str(row.get("check_id") or "")
        if not check_id:
            continue
        parameter_code = str(row.get("parameter_code") or "")
        category = parameter_code.split("-", 1)[0] if "-" in parameter_code else parameter_code or "default"
        findings.append(
            {
                "id": check_id,
                "parameter_code": parameter_code,
                "object_id": row.get("object_id"),
                "location": str(row.get("location") or ""),
                "category": category,
                "is_violation": str(row.get("violation_label") or "").upper() == "VIOLATION_PRESENT",
                "status": _gold_status(row),
                "protocol_status": row.get("protocol_status"),
                "comparison_result": _gold_comparison_result(row),
            }
        )
        for index, fragment in enumerate(row.get("evidence") or []):
            if not isinstance(fragment, dict):
                continue
            evidence.append(
                {
                    "id": f"{check_id}:E{index + 1}",
                    "finding_id": check_id,
                    "file_id": fragment.get("file_id"),
                    "stage": fragment.get("stage"),
                    "page": fragment.get("pdf_page_number"),
                    "bbox": fragment.get("bbox_normalized") or fragment.get("bbox"),
                }
            )
            if fragment.get("file_id"):
                document_links.append(
                    {
                        "id": f"{check_id}:E{index + 1}",
                        "finding_id": check_id,
                        "target_document_id": fragment.get("file_id"),
                        "stage": fragment.get("stage"),
                    }
                )
    return {
        "total_params": total_params,
        "ocr": [],
        "key_fields": [],
        "document_links": document_links,
        "findings": findings,
        "evidence": evidence,
        "object_splits": object_splits,
        "object_split_conflicts": split_conflicts,
        "metric_denominator_reasons": _metric_denominator_reasons(),
    }


def build_public_metric_fixture(data_dir: str | Path, *, total_params: int = 132) -> dict[str, Any]:
    """Build a public/train-safe fixture without inventing OCR or key-field gold."""

    root = Path(data_dir)
    checks = []
    gold_path = root / "public_gold_checks.jsonl"
    if gold_path.exists():
        checks = load_gold_checks_jsonl(gold_path, purpose="evaluation")["rows"]
    fixture = gold_checks_to_evaluation_fixture(checks, total_params=total_params)
    fixture["diagnostics"] = public_metric_diagnostics(root)
    return fixture


def public_metric_diagnostics(data_dir: str | Path) -> dict[str, Any]:
    root = Path(data_dir)
    document_field_candidates = 0
    matrix_field_candidates = 0
    ocr_reference_rows = 0
    key_field_gold_rows = 0
    if (root / "annotations.jsonl").exists():
        for row in _read_jsonl(root / "annotations.jsonl"):
            annotation_type = str(row.get("annotation_type") or "")
            status = str(row.get("status") or "")
            if annotation_type == "DOCUMENT_FIELD" and status == "AUTO_FIELD_CANDIDATE":
                document_field_candidates += 1
            if annotation_type == "MATRIX_FIELD" and status == "AUTO_FIELD_CANDIDATE":
                matrix_field_candidates += 1
            if annotation_type == "OCR_REFERENCE_TEXT" and status.startswith("FINAL"):
                ocr_reference_rows += 1
            if annotation_type == "DOCUMENT_FIELD" and status.startswith("FINAL"):
                key_field_gold_rows += 1
    return {
        "page_index_rows": _count_jsonl(root / "page_index.jsonl"),
        "annotation_rows": _count_jsonl(root / "annotations.jsonl"),
        "document_field_candidates": document_field_candidates,
        "matrix_field_candidates": matrix_field_candidates,
        "ocr_reference_rows": ocr_reference_rows,
        "key_field_gold_rows": key_field_gold_rows,
        "note": "Machine-assisted annotation candidates are coverage diagnostics only; they are not OCR/key-field gold.",
    }


def write_evaluation_fixture(checks: Iterable[dict[str, Any]], output_path: str | Path, *, total_params: int = 132) -> dict[str, Any]:
    fixture = gold_checks_to_evaluation_fixture(checks, total_params=total_params)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return fixture


def _read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            payload = json.loads(line)
            if isinstance(payload, dict):
                yield payload


def _count_jsonl(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as fh:
        return sum(1 for line in fh if line.strip())


def _metric_denominator_reasons() -> dict[str, str]:
    return {
        "ocr_character_accuracy": "No public/train-safe human OCR reference text is provided; page_index only marks text_source/needs_ocr.",
        "key_field_exact_match": "Public DOCUMENT_FIELD annotations are MACHINE_ASSISTED_PREANNOTATION_NEEDS_EXPERT_ACCEPTANCE, not final key-field gold.",
        "document_linking_accuracy": "Computed from public gold evidence file_id rows when predictions export matching evidence/document_links.",
        "localization_completeness": "Computed from public gold evidence file_id/page rows; bbox denominator remains null unless gold bbox is present.",
    }


def _gold_status(row: dict[str, Any]) -> str:
    label = str(row.get("violation_label") or "").upper()
    if label == "VIOLATION_PRESENT":
        return "CONFIRMED_VIOLATION"
    if label == "NO_VIOLATION":
        return "NEGATIVE_VERIFIED"
    if label == "MISSING_DOCUMENT":
        return "MISSING_EVIDENCE"
    return "NOT_COMPARABLE"


def _gold_comparison_result(row: dict[str, Any]) -> str | None:
    label = str(row.get("violation_label") or "").upper()
    raw = str(row.get("comparison_result") or "").upper().replace(" ", "_")
    if label == "VIOLATION_PRESENT" or raw in {
        "TRIGGERED",
        "MISSING_DESIGN_ELEMENT",
        "CONFIGURATION_MISMATCH",
        "VALUE_MISMATCH",
    }:
        return "TRIGGERED"
    if label == "NO_VIOLATION" or raw in {"NON_TRIGGERING", "MATCH", "NO_CHANGE"}:
        return "NON_TRIGGERING"
    if label in {"MISSING_DOCUMENT", "COMPARISON_IMPOSSIBLE"}:
        return label
    return raw or None
