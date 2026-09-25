from __future__ import annotations

from typing import Any
import hashlib
import json
from pathlib import Path
from app.domain import matrix_v11
from .fixtures import LeakageGuardError


SUBMISSION_VIOLATION_LABELS = {"VIOLATION_PRESENT", "NO_VIOLATION", "MISSING_DOCUMENT", "COMPARISON_IMPOSSIBLE"}
SUBMISSION_PROTOCOL_STATUSES = {
    "OK",
    "WARNING",
    "CRITICAL",
    "ID_MISSING",
    "RD_MISSING",
    "PD_MISSING",
    "COMPARISON_IMPOSSIBLE",
}
DATASET_STAGE_BY_INTERNAL = {"project": "PD", "working": "RD", "as_built": "ID"}
# `delta.reason` values that mean "a required document is absent" (-> MISSING_DOCUMENT + X_MISSING status).
# `missing_discipline_document`: the stage has files, but none of the section the catalog names (Phase 10, B).
MISSING_DOCUMENT_REASONS = {"missing_stage", "missing_discipline_document"}

# GOLD schema 1.1 (sheet «СХЕМА GOLD» of Матрица_параметров_редакция1.1.xlsx): enums of the fields added to every
# check on top of submission_schema.json. An internal abstention status is not a finding status there -- it is
# carried by completeness_status, and finding_status is null (the internal status stays in finding_status_internal).
GOLD11_FINDING_STATUSES = {"NEGATIVE_VERIFIED", "CANDIDATE", "CONFIRMED_VIOLATION", "SUSPICION"}
GOLD11_COMPLETENESS_BY_STATUS = {
    "MISSING_EVIDENCE": "MISSING_EVIDENCE",
    "NOT_APPLICABLE": "NOT_APPLICABLE",
    "NOT_COMPARABLE": "NOT_COMPARABLE",
    "LOW_QUALITY": "NOT_COMPARABLE",
    "CLARIFICATION_REQUIRED": "CLARIFICATION_REQUIRED",
}
GOLD11_REVIEW_PRIORITIES = {"HIGH", "MEDIUM", "LOW"}
STAGE_ORDER = ("PD", "RD", "ID")
EXPORT_BBOX_FORMAT = "xyxy_top_left_origin_normalized"
VERSION_CONTEXT_KEYS = ("dataset_version", "matrix_version", "model_version", "input_manifest_hash")


def protocol_to_evaluation_predictions(protocol: dict[str, Any]) -> dict[str, Any]:
    payload = protocol.get("payload") if isinstance(protocol.get("payload"), dict) else protocol
    findings = payload.get("findings") if isinstance(payload, dict) else []
    out_findings: list[dict[str, Any]] = []
    out_evidence: list[dict[str, Any]] = []
    out_document_links: list[dict[str, Any]] = []
    for group in findings or []:
        if not isinstance(group, dict):
            continue
        _guard_prediction(group)
        if str((group.get("delta") or {}).get("matrix_scope") or "MATRIX").upper() != "MATRIX":
            continue
        prediction = evidence_group_to_prediction(group)
        out_findings.append(prediction["finding"])
        out_evidence.extend(prediction["evidence"])
        out_document_links.extend(prediction["document_links"])
    return {
        "total_params": 132 if str(payload.get("matrix_version") or "").startswith("official-132") else len(out_findings),
        "findings": out_findings,
        "evidence": out_evidence,
        "document_links": out_document_links,
        "object_id": payload.get("object_id"),
        "provenance": "independent_runtime",
    }


def protocol_to_submission(protocol: dict[str, Any], *, include_suspicions: bool = False, code_style: str | None = None) -> dict[str, Any]:
    """`code_style` overrides CASE10_PARAMETER_CODE_STYLE (tools keyed by internal codes pass "legacy")."""
    payload = protocol.get("payload") if isinstance(protocol.get("payload"), dict) else protocol
    object_id = payload.get("object_id") or protocol.get("object_id")
    context = {"object_id": object_id}
    context.update({key: payload.get(key) or protocol.get(key) for key in VERSION_CONTEXT_KEYS})
    style = code_style or matrix_v11.export_code_style()
    checks: list[dict[str, Any]] = []
    duplicate_of, dropped_files = _integrity_maps(payload)
    for group in payload.get("findings") or []:
        if not isinstance(group, dict):
            continue
        _guard_prediction(group)
        delta = group.get("delta") if isinstance(group.get("delta"), dict) else {}
        if str(delta.get("matrix_scope") or "MATRIX").upper() != "MATRIX" and not include_suspicions:
            continue
        item = evidence_group_to_submission_check(_canonical_evidence(group, duplicate_of, dropped_files), context=context, code_style=style)
        if item.get("parameter_code"):
            checks.append(item)
    return {
        "object_id": object_id,
        "checks": checks,
        # extra top-level key (the schema allows it): how to read the GOLD 1.1 fields of every check
        "export_conventions": {
            "parameter_code_style": style,
            "matrix_table_version": matrix_v11.table_metadata().get("matrix_version"),
            "bbox_format": EXPORT_BBOX_FORMAT,
            **{key: context.get(key) for key in VERSION_CONTEXT_KEYS},
        },
    }


def _integrity_maps(payload: dict[str, Any]) -> tuple[dict[str, str], set[str]]:
    """({duplicate file id -> canonical file id}, {files that must never be cited}) from the protocol's document
    analysis (Phase 10, prompt B, item 4): a byte duplicate is ONE document, cited once under its canonical id;
    an unreadable/empty/service file is not a document at all."""
    analysis = payload.get("document_analysis") if isinstance(payload.get("document_analysis"), dict) else {}
    excluded = (analysis.get("integrity") or {}).get("excluded") or []
    duplicate_of = {
        str(item["file_id"]): str(item["duplicate_of"])
        for item in excluded if item.get("reason") == "EXACT_DUPLICATE_WITHIN_STAGE" and item.get("file_id") and item.get("duplicate_of")
    }
    dropped = {str(item["file_id"]) for item in excluded if item.get("reason") != "EXACT_DUPLICATE_WITHIN_STAGE" and item.get("file_id")}
    return duplicate_of, dropped


def _canonical_evidence(group: dict[str, Any], duplicate_of: dict[str, str], dropped: set[str]) -> dict[str, Any]:
    """The same group with its evidence rewritten to canonical files: duplicates -> the canonical copy, files that are
    not documents removed, identical (stage, file, page) citations collapsed. A group with nothing to rewrite is
    returned untouched (same object), so a package without duplicates exports byte-identically."""
    fragments = group.get("fragments") or []
    if not (duplicate_of or dropped) or not any(isinstance(f, dict) and str(f.get("file_id")) in (duplicate_of.keys() | dropped) for f in fragments):
        return group
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, Any]] = set()
    for fragment in fragments:
        if not isinstance(fragment, dict):
            out.append(fragment)
            continue
        file_id = str(fragment.get("file_id") or "")
        if file_id in dropped:
            continue
        if file_id in duplicate_of:
            fragment = {**fragment, "file_id": duplicate_of[file_id]}
        key = (fragment.get("stage"), fragment.get("file_id"), fragment.get("page"))
        if key in seen:
            continue
        seen.add(key)
        out.append(fragment)
    return {**group, "fragments": out}


def evidence_group_to_prediction(group: dict[str, Any]) -> dict[str, Any]:
    parameter = group.get("parameter") if isinstance(group.get("parameter"), dict) else {}
    delta = group.get("delta") if isinstance(group.get("delta"), dict) else {}
    values = delta.get("values") if isinstance(delta.get("values"), dict) else {}
    check_id = str(group.get("id"))
    parameter_code = str(delta.get("parameter_code") or parameter.get("code") or "")
    category = parameter_code.split("-", 1)[0] if "-" in parameter_code else parameter_code or "default"
    evidence_rows = []
    document_links = []
    for index, fragment in enumerate(group.get("fragments") or []):
        if not isinstance(fragment, dict):
            continue
        row_id = f"{check_id}:E{index + 1}"
        stage = _dataset_stage(fragment.get("stage"), fragment.get("dataset_stage"))
        evidence_rows.append(
            {
                "id": row_id,
                "finding_id": check_id,
                "file_id": fragment.get("file_id"),
                "stage": stage,
                "page": fragment.get("page"),
                "bbox": fragment.get("bbox_normalized") or fragment.get("bbox"),
            }
        )
        if fragment.get("file_id"):
            document_links.append(
                {
                    "id": row_id,
                    "finding_id": check_id,
                    "target_document_id": fragment.get("file_id"),
                    "stage": stage,
                }
            )
    finding = {
        "id": check_id,
        "group_id": group.get("id"),
        "parameter_code": parameter_code,
        "object_id": group.get("object_id"),
        "location": str(delta.get("location", group.get("entity_name")) or ""),
        "category": category,
        "status": group.get("finding_status"),
        "protocol_status": _protocol_status_for_group(group),
        "comparison_result": _comparison_result_for_group(group),
    }
    if values:
        finding["expected_value"] = values.get("PD")
        finding["actual_value"] = values.get("ID") or values.get("RD")
        finding["normalized_value"] = _normalized_value_summary(values)
    return {
        "finding": finding,
        "evidence": evidence_rows,
        "document_links": document_links,
    }


def evidence_group_to_submission_check(
    group: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
    code_style: str | None = None,
) -> dict[str, Any]:
    """One `checks[]` item: the submission_schema.json fields plus the GOLD 1.1 fields (extra keys, the schema
    stays valid). `context` carries protocol-level versions (dataset/matrix/model, input_manifest_hash)."""
    parameter = group.get("parameter") if isinstance(group.get("parameter"), dict) else {}
    delta = group.get("delta") if isinstance(group.get("delta"), dict) else {}
    internal_code = str(delta.get("parameter_code") or parameter.get("code") or "")
    fragments = []
    for fragment in group.get("fragments") or []:
        if not isinstance(fragment, dict):
            continue
        file_id = fragment.get("file_id")
        page = _optional_int(fragment.get("page"))
        if not file_id or not page:
            continue
        stage = _dataset_stage(fragment.get("stage"), fragment.get("dataset_stage"))
        fragments.append(
            {
                "stage": stage,
                "file_id": str(file_id),
                "pdf_page_number": page,
                # GOLD 1.1 evidence fields
                "page": page,
                "sha256": fragment.get("file_sha256") or fragment.get("sha256"),
                "document_code": fragment.get("document_code"),
                "revision": fragment.get("revision"),
                "approval_status": fragment.get("approval_status"),
                "bbox_norm": _bbox_norm(fragment),
                "coordinate_space": fragment.get("coordinate_space"),
                "role": fragment.get("role"),
            }
        )
    check = {
        "parameter_code": matrix_v11.export_parameter_code(internal_code, code_style),
        "location": str(delta.get("location") or group.get("entity_name") or group.get("object_id") or ""),
        "pd_value": _value_by_stage(group, "PD"),
        "rd_value": _value_by_stage(group, "RD"),
        "id_value": _value_by_stage(group, "ID"),
        "violation_label": _violation_label_for_group(group),
        "protocol_status": _protocol_status_for_group(group),
        "criticality": parameter.get("criticality") or delta.get("criticality"),
        "evidence": fragments,
    }
    check.update(_gold11_fields(group, check, internal_code, context or {}))
    return check


def _gold11_fields(group: dict[str, Any], check: dict[str, Any], internal_code: str, context: dict[str, Any]) -> dict[str, Any]:
    """GOLD 1.1 fields of one check. Identifiers are content hashes (no DB ids), so the export stays byte-identical
    across runs: finding_id = object + code + location (stable while the evidence changes); evidence_group_id also
    covers the cited (stage, file, page) set."""
    parameter = group.get("parameter") if isinstance(group.get("parameter"), dict) else {}
    delta = group.get("delta") if isinstance(group.get("delta"), dict) else {}
    row = matrix_v11.lookup(internal_code)
    matrix_code = row["matrix_code"] if row else None
    object_id = str(group.get("object_id") or context.get("object_id") or "")
    code_key = matrix_code or internal_code.strip().upper()
    citations = sorted({f"{item['stage']}:{item['file_id']}:{item['pdf_page_number']}" for item in check["evidence"]})
    status = str(group.get("finding_status") or "").upper()
    stage_values = {"PD": check["pd_value"], "RD": check["rd_value"], "ID": check["id_value"]}
    present = [stage for stage in STAGE_ORDER if stage_values[stage] is not None]
    expected_stage = present[0] if present else None
    actual_stage = present[1] if len(present) > 1 else None
    evidence = sorted(check["evidence"], key=_stage_rank)
    source_expected = _pick_source(evidence, expected_stage, "expected")
    after_expected = [item for item in evidence if source_expected is not None and _stage_rank(item) > _stage_rank(source_expected)]
    source_actual = _pick_source(after_expected, actual_stage, "actual")
    priority = str(group.get("review_priority") or parameter.get("review_priority") or "").upper()
    if priority not in GOLD11_REVIEW_PRIORITIES:
        priority = (row or {}).get("review_priority")
    model_version = group.get("model_version") or context.get("model_version")
    fields: dict[str, Any] = {
        "object_id": object_id,
        "parameter_id": int(row["parameter_id"]) if row else None,
        "parameter_code_legacy": row["legacy_code"] if row else internal_code,
        "matrix_code": matrix_code,
        "rule_version": "/".join(str(part) for part in (delta.get("source"), model_version) if part) or None,
        "evidence_group_id": "EG-" + _short_hash(object_id, code_key, check["location"], *citations),
        "finding_id": "F-" + _short_hash(object_id, code_key, check["location"]),
        "expected_value": stage_values[expected_stage] if expected_stage else None,
        "actual_value": stage_values[actual_stage] if actual_stage else None,
        "expected_stage": expected_stage,
        "actual_stage": actual_stage,
    }
    for prefix, source in (("source_expected", source_expected), ("source_actual", source_actual)):
        source = source or {}
        fields.update({
            f"{prefix}_file_id": source.get("file_id"),
            f"{prefix}_sha256": source.get("sha256"),
            f"{prefix}_stage": source.get("stage"),
            f"{prefix}_code": source.get("document_code"),
            f"{prefix}_revision": source.get("revision"),
            f"{prefix}_approval": source.get("approval_status"),
            f"{prefix}_page": source.get("page"),
            f"{prefix}_bbox_polygon": source.get("bbox_norm"),
        })
    fields.update({
        "approved_change_ref": "NONE",
        "completeness_status": "COMPLETE" if status in GOLD11_FINDING_STATUSES else GOLD11_COMPLETENESS_BY_STATUS.get(status, "NOT_COMPARABLE"),
        "finding_status": status if status in GOLD11_FINDING_STATUSES else None,
        "finding_status_internal": status or None,
        "review_priority": priority,
        "confidence": group.get("confidence"),
        "dataset_version": group.get("dataset_version") or context.get("dataset_version"),
        "matrix_version": group.get("matrix_version") or context.get("matrix_version"),
        "model_version": model_version,
        "input_manifest_hash": context.get("input_manifest_hash") or group.get("input_manifest_hash"),
    })
    return fields


def _pick_source(evidence: list[dict[str, Any]], stage: str | None, role: str) -> dict[str, Any] | None:
    """The evidence item behind the expected/actual value. `evidence` is in PD, RD, ID order. With the value's stage
    known: an item of that stage, preferring the one the pipeline tagged with `role`. Without a value (e.g. a gated or
    missing-evidence group that still cites pages): the item tagged with `role`, else the earliest cited stage."""
    candidates = [item for item in evidence if item["stage"] == stage] if stage else evidence
    for item in candidates:
        if item.get("role") == role:
            return item
    return candidates[0] if candidates else None


def _stage_rank(item: dict[str, Any]) -> int:
    return STAGE_ORDER.index(item["stage"]) if item.get("stage") in STAGE_ORDER else len(STAGE_ORDER)


def _bbox_norm(fragment: dict[str, Any]) -> list[float] | None:
    """Normalized [x0, y0, x1, y1] in [0;1], top-left origin. Pixel/point boxes are normalized by the page size when
    it is known; anything else is dropped rather than guessed."""
    raw = fragment.get("bbox_normalized") or fragment.get("bbox")
    try:
        values = [float(value) for value in raw] if isinstance(raw, (list, tuple)) and len(raw) == 4 else None
    except (TypeError, ValueError):
        values = None
    if values is None:
        return None
    if max(values) > 1.0 + 1e-6:
        width = _optional_float(fragment.get("page_width"))
        height = _optional_float(fragment.get("page_height"))
        if not width or not height:
            return None
        values = [values[0] / width, values[1] / height, values[2] / width, values[3] / height]
    x0, x1 = sorted((values[0], values[2]))
    y0, y1 = sorted((values[1], values[3]))
    return [round(min(max(value, 0.0), 1.0), 6) for value in (x0, y0, x1, y1)]


def _short_hash(*parts: object) -> str:
    return hashlib.sha256("\x1f".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:16]


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def validate_submission_basic(submission: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not submission.get("object_id"):
        errors.append("object_id is required")
    checks = submission.get("checks")
    if not isinstance(checks, list):
        errors.append("checks must be an array")
        return errors
    for index, check in enumerate(checks):
        prefix = f"checks[{index}]"
        for field in ("parameter_code", "location", "violation_label", "evidence"):
            if field not in check:
                errors.append(f"{prefix}.{field} is required")
        if check.get("violation_label") not in SUBMISSION_VIOLATION_LABELS:
            errors.append(f"{prefix}.violation_label is invalid")
        if check.get("protocol_status") is not None and check.get("protocol_status") not in SUBMISSION_PROTOCOL_STATUSES:
            errors.append(f"{prefix}.protocol_status is invalid")
        if not isinstance(check.get("evidence"), list):
            errors.append(f"{prefix}.evidence must be an array")
            continue
        for evidence_index, fragment in enumerate(check.get("evidence") or []):
            eprefix = f"{prefix}.evidence[{evidence_index}]"
            if fragment.get("stage") not in {"PD", "RD", "ID"}:
                errors.append(f"{eprefix}.stage is invalid")
            if not fragment.get("file_id"):
                errors.append(f"{eprefix}.file_id is required")
            page = fragment.get("pdf_page_number")
            if not isinstance(page, int) or page < 1:
                errors.append(f"{eprefix}.pdf_page_number must be a positive integer")
    return errors


def validate_submission_schema(submission: dict[str, Any], schema_path: str | Path) -> list[str]:
    from jsonschema import Draft202012Validator

    with Path(schema_path).open(encoding="utf-8") as stream:
        schema = json.load(stream)
    Draft202012Validator.check_schema(schema)
    return [f"{error.json_path}: {error.message}" for error in Draft202012Validator(schema).iter_errors(submission)]


def _value_by_stage(group: dict[str, Any], stage: str) -> Any:
    for fragment in group.get("fragments") or []:
        if isinstance(fragment, dict) and _dataset_stage(fragment.get("stage"), fragment.get("dataset_stage")) == stage:
            return fragment.get("extracted_value")
    return None


def _dataset_stage(stage: Any, dataset_stage: Any = None) -> str:
    raw = str(dataset_stage or "").upper()
    if raw in {"PD", "RD", "ID"}:
        return raw
    return DATASET_STAGE_BY_INTERNAL.get(str(stage or ""), str(stage or "PD").upper())


def _violation_label_for_group(group: dict[str, Any]) -> str:
    delta = group.get("delta") if isinstance(group.get("delta"), dict) else {}
    status = str(group.get("finding_status") or "").upper()
    if status in {"CANDIDATE", "CONFIRMED_VIOLATION"}:
        return "VIOLATION_PRESENT"
    if status in {"NEGATIVE_VERIFIED", "NOT_APPLICABLE"}:
        return "NO_VIOLATION"
    if status == "MISSING_EVIDENCE" and delta.get("reason") in MISSING_DOCUMENT_REASONS:
        return "MISSING_DOCUMENT"
    return "COMPARISON_IMPOSSIBLE"


def _comparison_result_for_group(group: dict[str, Any]) -> str:
    delta = group.get("delta") if isinstance(group.get("delta"), dict) else {}
    raw = str(delta.get("comparison_result") or "").upper().replace(" ", "_")
    if raw:
        return raw
    label = _violation_label_for_group(group)
    if label == "VIOLATION_PRESENT":
        return "TRIGGERED"
    if label == "NO_VIOLATION":
        return "NON_TRIGGERING"
    return label


def _normalized_value_summary(values: dict[str, Any]) -> str:
    parts = []
    for stage in ("PD", "RD", "ID"):
        value = values.get(stage)
        if value not in (None, ""):
            parts.append(f"{stage}:{value}")
    return " | ".join(parts)


def _protocol_status_for_group(group: dict[str, Any]) -> str:
    delta = group.get("delta") if isinstance(group.get("delta"), dict) else {}
    status = str(group.get("finding_status") or "").upper()
    priority = str(group.get("review_priority") or (group.get("parameter") or {}).get("review_priority") or "").upper()
    if status in {"CANDIDATE", "CONFIRMED_VIOLATION"}:
        return "CRITICAL" if priority == "HIGH" else "WARNING"
    if status in {"NEGATIVE_VERIFIED", "NOT_APPLICABLE"}:
        return "OK"
    if status == "MISSING_EVIDENCE":
        return _missing_protocol_status(group)
    return "COMPARISON_IMPOSSIBLE"


def _missing_protocol_status(group: dict[str, Any]) -> str:
    delta = group.get("delta") if isinstance(group.get("delta"), dict) else {}
    stages = delta.get("stages") if isinstance(delta.get("stages"), list) else []
    joined = " ".join(str(stage).upper() for stage in stages)
    if "ПД" in joined or "PD" in joined:
        return "PD_MISSING"
    if "РД" in joined or "RD" in joined:
        return "RD_MISSING"
    if "ИД" in joined or "ID" in joined:
        return "ID_MISSING"
    return "COMPARISON_IMPOSSIBLE"


def _optional_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _guard_prediction(group: dict[str, Any]) -> None:
    if (group.get("delta") or {}).get("source") == "gold_fixture" or any(
        fragment.get("extractor") == "gold_fixture" for fragment in group.get("fragments") or []
    ):
        raise LeakageGuardError("GOLD-derived protocol cannot be exported as independent predictions; rerun the process")
