from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
from typing import Any

from sqlalchemy.orm import Session

from ..db.models import GoldDraftItem, MLRetrainingLog
TRAINING_LABELS = {"CONFIRMED_VIOLATION", "NEGATIVE_VERIFIED"}
FORBIDDEN_TRAINING_LABELS = {"CANDIDATE", "SUSPICION", "MISSING_EVIDENCE", "CLARIFICATION_REQUIRED", "NOT_COMPARABLE"}


def build_training_release(
    db: Session,
    *,
    project_id: int,
    organization_id: int,
    dataset_version: str = "case10-inspector-gold-draft-v1",
    min_confirmed_violations: int = 100,
    candidate_metrics: dict[str, Any] | None = None,
    baseline_metrics: dict[str, Any] | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    rows = (
        db.query(GoldDraftItem)
        .filter(
            GoldDraftItem.project_id == int(project_id),
            GoldDraftItem.organization_id == int(organization_id),
        )
        .order_by(GoldDraftItem.object_id.asc(), GoldDraftItem.id.asc())
        .all()
    )

    included: list[dict[str, Any]] = []
    excluded = Counter()
    for row in rows:
        payload = row.payload_json if isinstance(row.payload_json, dict) else {}
        label = str(row.label or "")
        object_id = str(row.object_id or payload.get("object_id") or "")
        if str(payload.get("split") or "").upper() == "TEST_HIDDEN":
            excluded["hidden_or_organizer_only"] += 1
            continue
        if label in FORBIDDEN_TRAINING_LABELS or label not in TRAINING_LABELS:
            excluded[f"forbidden_label:{label or 'EMPTY'}"] += 1
            continue
        fragments = payload.get("fragments") if isinstance(payload.get("fragments"), list) else []
        if not _has_complete_evidence_card(fragments):
            excluded["missing_complete_evidence_card"] += 1
            continue
        included.append(_release_item(row, payload, fragments, label, object_id))

    splits = _object_level_splits(included)
    split_hashes = {name: _hash_json(items) for name, items in splits.items()}
    labels = Counter(item["label"] for item in included)
    objects = sorted({item["object_id"] for item in included if item.get("object_id")})
    gate_reasons = []
    if labels["CONFIRMED_VIOLATION"] < int(min_confirmed_violations):
        gate_reasons.append(
            f"confirmed_violations_below_minimum:{labels['CONFIRMED_VIOLATION']}<{int(min_confirmed_violations)}"
        )
    if len(objects) < 3:
        gate_reasons.append("not_enough_objects_for_object_level_train_validation_test_split")
    for split_name in ("train", "validation", "test"):
        if not splits[split_name]:
            gate_reasons.append(f"empty_split:{split_name}")
    model_gate = model_acceptance_gate(candidate_metrics or {}, baseline_metrics or {})
    gate_reasons.extend(model_gate["reasons"])

    gate_status = "READY_FOR_REVIEW" if not gate_reasons else "BLOCKED"
    approval_status = "PENDING_APPROVAL" if gate_status == "READY_FOR_REVIEW" else "BLOCKED"
    metrics = {
        "precision": (candidate_metrics or {}).get("finding_precision"),
        "recall": (candidate_metrics or {}).get("finding_recall"),
        "f1": (candidate_metrics or {}).get("finding_f1"),
        "false_positive_rate": (candidate_metrics or {}).get("false_positive_rate"),
        "release_items": len(included),
        "confirmed_violations": labels["CONFIRMED_VIOLATION"],
        "negative_verified": labels["NEGATIVE_VERIFIED"],
        "objects": len(objects),
    }
    per_category_metrics = (candidate_metrics or {}).get("per_category_metrics") or {}
    release_payload = {
        "dataset_version": dataset_version,
        "items": included,
        "splits": {name: [item["id"] for item in items] for name, items in splits.items()},
        "excluded_counts": dict(excluded),
        "gate_reasons": gate_reasons,
        "no_auto_publish": True,
    }
    log = MLRetrainingLog(
        project_id=int(project_id),
        organization_id=int(organization_id),
        dataset_version=dataset_version,
        matrix_version=_common_value(included, "matrix_version"),
        model_version=_common_value(included, "model_version"),
        candidate_model_version=None,
        split_hashes=split_hashes,
        metrics_json=metrics,
        per_category_metrics=per_category_metrics,
        approval_status=approval_status,
        gate_status=gate_status,
        gate_reasons=gate_reasons,
        approved_by_user_id=None,
        baseline_metrics_json=baseline_metrics or {},
        release_payload_json=release_payload,
        rollback_to_model_version=model_gate.get("rollback_to_model_version"),
    )
    db.add(log)
    db.flush()
    result = training_log_to_dict(log)
    result["release"] = release_payload
    result["excluded_counts"] = dict(excluded)
    result["model_gate"] = model_gate
    result["created_by_user_id"] = user_id
    db.commit()
    return result


def training_log_to_dict(row: MLRetrainingLog) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "project_id": int(row.project_id),
        "organization_id": int(row.organization_id),
        "dataset_version": row.dataset_version,
        "matrix_version": row.matrix_version,
        "model_version": row.model_version,
        "candidate_model_version": row.candidate_model_version,
        "split_hashes": row.split_hashes or {},
        "metrics": row.metrics_json or {},
        "per_category_metrics": row.per_category_metrics or {},
        "approval_status": row.approval_status,
        "gate_status": row.gate_status,
        "gate_reasons": row.gate_reasons or [],
        "approved_by": row.approved_by_user_id,
        "approved_at": row.approved_at,
        "rollback_to_model_version": row.rollback_to_model_version,
        "created_at": row.created_at,
        "no_auto_publish": row.approved_at is None,
    }


def model_acceptance_gate(candidate_metrics: dict[str, Any], baseline_metrics: dict[str, Any]) -> dict[str, Any]:
    """Block release on aggregate regression (existing behaviour) OR per-category regression.

    ТЗ 9.4 requires no recall/FPR regression "по любой обязательной категории"
    (in any mandatory category), not just on average -- an aggregate that looks
    flat can still hide one category collapsing while another compensates.
    The aggregate checks below are unchanged; the per-category loop is an
    additional, independent gate over `per_category_metrics` (already computed
    by `evaluation.metrics._evaluate_findings`), not a replacement.
    """
    reasons: list[str] = []
    rollback_to = baseline_metrics.get("model_version")
    if not candidate_metrics:
        return {"passed": None, "reasons": [], "rollback_to_model_version": rollback_to}
    baseline_f1 = _float_or_none(baseline_metrics.get("finding_f1"))
    candidate_f1 = _float_or_none(candidate_metrics.get("finding_f1"))
    if baseline_f1 is not None and candidate_f1 is not None and baseline_f1 - candidate_f1 > 0.05:
        reasons.append("f1_degradation_gt_5pp")
    baseline_recall = _float_or_none(baseline_metrics.get("finding_recall"))
    candidate_recall = _float_or_none(candidate_metrics.get("finding_recall"))
    if baseline_recall is not None and candidate_recall is not None and baseline_recall - candidate_recall > 0.02:
        reasons.append("recall_degradation_gt_2pp")
    baseline_fpr = _float_or_none(baseline_metrics.get("false_positive_rate"))
    candidate_fpr = _float_or_none(candidate_metrics.get("false_positive_rate"))
    if baseline_fpr is not None and candidate_fpr is not None and candidate_fpr - baseline_fpr > 0.02:
        reasons.append("fpr_growth_gt_2pp")

    baseline_categories = baseline_metrics.get("per_category_metrics") or {}
    candidate_categories = candidate_metrics.get("per_category_metrics") or {}
    for category in sorted(set(baseline_categories) & set(candidate_categories)):
        baseline_category = baseline_categories.get(category) or {}
        candidate_category = candidate_categories.get(category) or {}
        category_baseline_recall = _float_or_none(baseline_category.get("finding_recall"))
        category_candidate_recall = _float_or_none(candidate_category.get("finding_recall"))
        if (
            category_baseline_recall is not None
            and category_candidate_recall is not None
            and category_baseline_recall - category_candidate_recall > 0.02
        ):
            reasons.append(f"recall_degradation_gt_2pp:category={category}")
        category_baseline_fpr = _float_or_none(baseline_category.get("finding_false_positive_rate"))
        category_candidate_fpr = _float_or_none(candidate_category.get("finding_false_positive_rate"))
        if (
            category_baseline_fpr is not None
            and category_candidate_fpr is not None
            and category_candidate_fpr - category_baseline_fpr > 0.02
        ):
            reasons.append(f"fpr_growth_gt_2pp:category={category}")

    return {"passed": not reasons, "reasons": reasons, "rollback_to_model_version": rollback_to if reasons else None}


def _release_item(row: GoldDraftItem, payload: dict[str, Any], fragments: list[dict[str, Any]], label: str, object_id: str) -> dict[str, Any]:
    return {
        "id": f"gold-draft-{int(row.id)}",
        "gold_draft_item_id": int(row.id),
        "evidence_group_id": int(row.evidence_group_id),
        "decision_id": int(row.decision_id) if row.decision_id is not None else None,
        "object_id": object_id,
        "label": label,
        "parameter_code": ((payload.get("parameter") or {}).get("code") if isinstance(payload.get("parameter"), dict) else None),
        "finding_status": payload.get("finding_status"),
        "matrix_version": payload.get("matrix_version"),
        "model_version": payload.get("model_version"),
        "dataset_version": payload.get("dataset_version"),
        "fragments": [_fragment_card(fragment) for fragment in fragments],
    }


def _fragment_card(fragment: dict[str, Any]) -> dict[str, Any]:
    return {
        "file_id": fragment.get("file_id"),
        "stage": fragment.get("stage"),
        "page": fragment.get("page"),
        "bbox": fragment.get("bbox_normalized") or fragment.get("bbox"),
        "extracted_value": fragment.get("extracted_value"),
        "role": fragment.get("role"),
    }


def _has_complete_evidence_card(fragments: list[dict[str, Any]]) -> bool:
    if not fragments:
        return False
    for fragment in fragments:
        bbox = fragment.get("bbox_normalized") or fragment.get("bbox")
        if not fragment.get("file_id") or fragment.get("page") is None:
            return False
        if not isinstance(bbox, list) or len(bbox) != 4:
            return False
    return True


def _object_level_splits(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    by_object: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        by_object[str(item.get("object_id") or "")].append(item)
    output = {"train": [], "validation": [], "test": []}
    for object_id in sorted(by_object):
        digest = int(hashlib.sha256(object_id.encode("utf-8")).hexdigest()[:8], 16)
        split = ("train", "validation", "test")[digest % 3]
        output[split].extend(sorted(by_object[object_id], key=lambda row: row["id"]))
    return output


def _hash_json(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _common_value(items: list[dict[str, Any]], key: str) -> str | None:
    values = sorted({str(item.get(key)) for item in items if item.get(key)})
    return values[0] if len(values) == 1 else None


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
