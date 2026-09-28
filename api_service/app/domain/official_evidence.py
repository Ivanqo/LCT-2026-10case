"""Evidence discovery from document annotations. GOLD labels never enter inference."""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from decimal import Decimal
import hashlib

from sqlalchemy.orm import Session

from ..db.models import DocumentVersion, EvidenceFragment, InspectionProcess, Param, SourceFragment
from . import trigger_policy as tp
from . import file_registry
from .anchor_search import GENERIC_SITE_LOCATION
from .comparison_gate import REASON_MISSING_DISCIPLINE, GateContext, GateDecision
from .dataset_sources import normalized_original_geometry
from .evidence_groups import sweep_orphaned_evidence_groups, upsert_evidence_group
from .explication_compare import collect_explication_groups, explication_compare_enabled
from .generic_compound_extraction import (
    GENERIC_COMPOUND_EXTRACTOR_VERSION,
    CompoundObservation,
    collect_compound_observations,
    compare_components,
    new_compound_budget,
)
from .generic_enum_extraction import (
    GENERIC_ENUM_EXTRACTOR_VERSION,
    EnumObservation,
    collect_enum_observations,
    enum_values_equal,
    new_enum_budget,
)
from .generic_matrix_extraction import (
    GENERIC_EXTRACTOR_VERSION,
    GenericObservation,
    collect_generic_observations,
    eligible_params,
    new_generic_budget,
    values_equal,
)
from .generic_table_row_count import (
    GENERIC_TABLE_COUNT_EXTRACTOR_VERSION,
    TableCountObservation,
    collect_table_count_observations,
    counts_equal,
    new_table_count_budget,
)
from .live_candidate_tagger import new_live_tagger_budget, tag_live_candidates
from .logical_analysis import run_logical_analysis_module
from .official_rule_packs import (
    SUPPORTED_RULE_CODES,
    extract_official_rule_observations,
    find_confirming_fallback_observation,
    new_fallback_budget,
    rule_is_violation,
)


STAGE_FIELDS = {"project": "source_pd", "working": "source_rd", "as_built": "source_id"}
STAGE_CODES = {"project": "PD", "working": "RD", "as_built": "ID"}
_QUALITY_PAGE_CACHE: dict[tuple[str, str, int], dict] = {}


# Both the organizer-supplied training data (`learning_annotation`, imported
# by `official_dataset._import_learning_split` for the 3 official hackathon
# objects only) and this project's own live-scanned equivalent
# (`live_candidate_tagger.LIVE_TAGGER_SOURCE_SYSTEM`, see that module) tag a
# candidate page/field the exact same way -- source_system is the only thing
# that differs; every downstream `by_code` consumer treats the two
# identically, additively (an object with organizer tags is unaffected; an
# object with none now has a real candidate pool instead of a structural
# zero -- see project_case10_new_objects_no_tagger_gap memory).
_INFERENCE_SOURCE_SYSTEMS = frozenset({"learning_annotation", "live_tagger"})


def inference_annotation(fragment: SourceFragment) -> bool:
    meta = fragment.metadata_json or {}
    return (fragment.source_system in _INFERENCE_SOURCE_SYSTEMS
            and meta.get("annotation_type") == "MATRIX_FIELD"
            and meta.get("status") == "AUTO_FIELD_CANDIDATE"
            and not meta.get("check_id"))


def _full_catalog_params(db: Session, params: list[Param]) -> list[Param]:
    """The live tagger must search the FULL matrix catalog for anchor
    phrases regardless of whether `params` was narrowed to a single
    incrementally-affected code (see v3_pipeline.py's `target_codes`
    scoping) -- otherwise a document's `live_tagger_scan` idempotency
    marker (see live_candidate_tagger.py) would wrongly claim pages were
    checked for codes an incremental run never actually searched for."""
    matrix_version_id = params[0].matrix_version_id
    return (
        db.query(Param)
        .filter(Param.matrix_version_id == matrix_version_id, Param.is_active == True)  # noqa: E712
        .all()
    )


def create_official_evidence_groups(
    db: Session,
    process: InspectionProcess,
    params: list[Param],
    docs: list[DocumentVersion],
    *,
    user_id: int | None = None,
    explication_audit: Callable[[dict], None] | None = None,
) -> list[dict]:
    selection = file_registry.selection_context(db, process, docs)
    excluded_doc_ids = selection["excluded_document_ids"]
    candidate_docs = [doc for doc in docs if int(doc.id) not in excluded_doc_ids]
    by_id = {doc.id: doc for doc in candidate_docs}
    if by_id and params:
        tag_live_candidates(db, candidate_docs, _full_catalog_params(db, params), budget=new_live_tagger_budget())
    by_code = defaultdict(list)
    fragments = []
    low_quality_by_code: dict[str, list[dict]] = defaultdict(list)
    if by_id:
        fragments = db.query(SourceFragment).filter(SourceFragment.document_version_id.in_(by_id)).all()
        for fragment in fragments:
            if inference_annotation(fragment):
                code = str((fragment.metadata_json or {}).get("code") or "")
                doc = by_id.get(int(fragment.document_version_id))
                if doc is None or not code:
                    continue
                quality = _quality_for_page(doc, int(fragment.page or 0)) if fragment.page else None
                if quality and quality.get("quality_status") == "LOW_QUALITY":
                    _mark_source_fragment_low_quality(db, fragment, quality)
                    low_quality_by_code[code].append(_quality_issue(code, _stage_code(doc), doc, fragment.page,
                                                                    quality, source_fragment=fragment))
                else:
                    by_code[code].append(fragment)
    # Decision layer (Phase 10, prompt B): document facts (byte duplicates / unreadable / service files,
    # ordered-vs-conflicting revisions, section inventory) computed once from metadata. The rule-pack tier is
    # untouched: it keeps seeing every document and every fragment exactly as before.
    gate = GateContext(candidate_docs, rule_pack_codes=SUPPORTED_RULE_CODES)
    if gate.excluded_doc_ids or gate.superseded_doc_ids:
        by_code = defaultdict(list, {
            code: rows if code in SUPPORTED_RULE_CODES else [f for f in rows if not gate.drop_from_generic_candidates(f.document_version_id)]
            for code, rows in by_code.items()
        })
    # Restricting extraction to the codes the target params actually need is
    # what makes a param-scoped incremental run cheaper than a full one: the
    # expensive page rendering/OCR inside extract_official_rule_observations
    # is skipped entirely for rule packs outside this run's impact set.
    only_codes = {str(param.code) for param in params} & SUPPORTED_RULE_CODES
    rule_observations, extraction_context = extract_official_rule_observations(
        candidate_docs, fragments, only_codes=only_codes,
    )
    # Rule packs can locate evidence directly from the source PDF rather than a tagged fragment. Recheck each
    # cited page before its value is allowed into comparison or the protocol.
    for code, observations in list(rule_observations.items()):
        accepted = []
        for observation in observations:
            quality = _quality_for_page(observation.document, int(observation.page))
            if quality.get("quality_status") == "LOW_QUALITY":
                source_fragment = getattr(observation, "source_fragment", None)
                if source_fragment is not None:
                    _mark_source_fragment_low_quality(db, source_fragment, quality)
                low_quality_by_code[str(code)].append(_quality_issue(
                    str(code), str(observation.stage), observation.document, observation.page, quality,
                    source_fragment=source_fragment, bbox=getattr(observation, "bbox_normalized", None),
                    confidence=getattr(observation, "confidence", None),
                ))
            else:
                accepted.append(observation)
        rule_observations[code] = accepted
    fallback_budget = new_fallback_budget()
    fallback_diagnostics: list[dict] = []
    available_stages = {doc.doc_stage for doc in candidate_docs if doc.dataset_stage != "RD_ID_MIXED"}
    has_mixed = any(doc.dataset_stage == "RD_ID_MIXED" for doc in candidate_docs)
    # Generic anchor+number extraction: a single shared mechanism (see
    # generic_matrix_extraction.py) covering the class of parameters whose
    # catalog name is a plain physical-quantity table label, for the ~127
    # codes that have no tuned official rule pack. Computed once, up front,
    # in one batched pass over every eligible param -- not per param inside
    # the loop below -- so pages shared by several parameters (a single ТЭП
    # table routinely answers several at once) are only rendered once.
    generic_candidates = eligible_params(params, excluded_codes=SUPPORTED_RULE_CODES)
    generic_observations = collect_generic_observations(
        generic_candidates, by_code, by_id, stage_codes=STAGE_CODES, budget=new_generic_budget(),
    )
    # Enum-class anchor mechanism (concrete/steel/rebar grade, fire/energy
    # class codes, ...): a second, independent generic tier, tried after the
    # numeric one and before the annotation-context fallback -- see
    # generic_enum_extraction.py. Disjoint by construction from the numeric
    # candidate pool (classify_unit gives each param exactly one class).
    enum_observations = collect_enum_observations(
        params, by_code, by_id, stage_codes=STAGE_CODES, budget=new_enum_budget(), excluded_codes=SUPPORTED_RULE_CODES,
    )
    # Compound-unit anchor mechanism (N independently-comparable numeric
    # fields behind one "/"-separated unit, e.g. "шт. / компл."): a third
    # generic tier, tried after enum and before the annotation-context
    # fallback -- see generic_compound_extraction.py. Disjoint from both
    # prior tiers by construction (classify_unit gives each param exactly
    # one class, and only genuinely all-numeric compounds are eligible).
    compound_observations = collect_compound_observations(
        params, by_code, by_id, stage_codes=STAGE_CODES, budget=new_compound_budget(), excluded_codes=SUPPORTED_RULE_CODES,
    )
    # Table-row-count mechanism (apartments/parking spaces/handrails/...
    # listed one-per-row in a real RD-stage exposition/specification table):
    # a fourth generic tier, tried after compound and before the
    # annotation-context fallback -- see generic_table_row_count.py. Scoped
    # to an explicit semantic allowlist, not just NUMERIC_COUNT unit, so it
    # never counts the wrong thing (see matrix_unit_classifier.py).
    table_count_observations = collect_table_count_observations(
        params, by_code, by_id, stage_codes=STAGE_CODES, budget=new_table_count_budget(), excluded_codes=SUPPORTED_RULE_CODES,
    )
    touched_keys: dict[int, set[str]] = {}
    for param in params:
        param_touched = touched_keys.setdefault(int(param.id), set())
        quality_issues = _dedupe_quality_issues(low_quality_by_code.get(str(param.code), []))
        if quality_issues:
            clean_stages = _clean_stages_for_param(
                param, by_code, by_id, rule_observations, generic_observations, enum_observations,
                compound_observations, table_count_observations,
            )
            required = {stage for stage, field in (("PD", "source_pd"), ("RD", "source_rd"), ("ID", "source_id"))
                        if bool(getattr(param, field, False))}
            blocks_comparison = any(str(issue["stage"]) in required and str(issue["stage"]) not in clean_stages
                                    for issue in quality_issues)
            quality_key = _upsert_quality_warning_group(
                db, process, param, quality_issues, blocks_comparison=blocks_comparison, user_id=user_id,
            )
            if quality_key:
                param_touched.add(quality_key)
            if blocks_comparison:
                continue
        source_block = file_registry.blocking_for_param(param, docs, selection)
        if source_block:
            key = _upsert_selection_clarification_group(db, process, param, source_block, user_id=user_id)
            if key:
                param_touched.add(key)
            continue
        rule_keys = _upsert_rule_groups(
            db, process, param, rule_observations.get(param.code, []),
            documents=extraction_context["documents"],
            exclude_pages=extraction_context["scanned_pages"],
            budget=fallback_budget,
            fallback_diagnostics=fallback_diagnostics,
            user_id=user_id,
        )
        if rule_keys:
            param_touched.update(rule_keys)
            continue
        generic_key = _upsert_generic_group(
            db, process, param, generic_observations.get(int(param.id)) or {}, user_id=user_id, gate=gate,
        )
        if generic_key:
            param_touched.add(generic_key)
            continue
        enum_key = _upsert_enum_group(
            db, process, param, enum_observations.get(int(param.id)) or {}, user_id=user_id, gate=gate,
        )
        if enum_key:
            param_touched.add(enum_key)
            continue
        compound_key = _upsert_compound_group(
            db, process, param, compound_observations.get(int(param.id)) or {}, user_id=user_id, gate=gate,
        )
        if compound_key:
            param_touched.add(compound_key)
            continue
        table_count_key = _upsert_table_count_group(
            db, process, param, table_count_observations.get(int(param.id)) or {}, user_id=user_id, gate=gate,
        )
        if table_count_key:
            param_touched.add(table_count_key)
            continue
        needed = [stage for stage, field in STAGE_FIELDS.items() if getattr(param, field)]
        missing = [STAGE_CODES[stage] for stage in needed if stage not in available_stages]
        candidates = by_code.get(param.code, [])
        selected = {}
        # A keyword hit provides context, not a normalized value or proof of a violation.
        for fragment in sorted(candidates, key=lambda f: (-(f.confidence or 0), f.document_version_id, f.page or 0, f.id)):
            doc = by_id[fragment.document_version_id]
            stage = doc.dataset_stage or STAGE_CODES.get(doc.doc_stage)
            if stage not in selected:
                selected[stage] = fragment
        reason = "values_not_extracted" if selected else "no_relevant_evidence"
        status = "NOT_COMPARABLE" if selected else "MISSING_EVIDENCE"
        # Комплектность (ТЗ 9.2 п.3): the stage exists but holds no document of the section the catalog names.
        missing_discipline: list[str] = []
        if not has_mixed and param.code not in SUPPORTED_RULE_CODES:
            for stage, code_ in (("project", "PD"), ("working", "RD")):
                if getattr(param, STAGE_FIELDS[stage]) and code_ not in missing and gate.stage_completeness(param, code_)[0] == "ABSENT":
                    missing_discipline.append(code_)
        if has_mixed and any(stage in missing for stage in ("RD", "ID")):
            status, reason = "CLARIFICATION_REQUIRED", "mixed_stage_requires_review"
        elif missing_discipline:
            # both kinds of absence are reported; the severest stage leads the protocol status (PD > RD > ID, ТЗ 9.2)
            status, reason = "MISSING_EVIDENCE", REASON_MISSING_DISCIPLINE
            missing = [code_ for code_ in ("PD", "RD", "ID") if code_ in set(missing) | set(missing_discipline)]
        elif missing:
            status, reason = "MISSING_EVIDENCE", "missing_stage"
        group_key = "annotation"
        param_touched.add(group_key)
        fragment_specs = []
        for fragment in selected.values():
            doc = by_id[fragment.document_version_id]
            geometry = normalized_original_geometry(doc, fragment)
            if geometry is None:
                continue
            bbox, page_width, page_height = geometry
            fragment_specs.append({
                "document_version_id": doc.id, "source_fragment_id": fragment.id,
                "dataset_file_id": doc.dataset_file_id, "file_sha256": doc.file_hash or doc.content_hash,
                "stage": doc.doc_stage, "discipline": doc.discipline, "document_code": doc.document_code,
                "revision": doc.revision, "approval_status": doc.approval_status,
                "page": fragment.page, "bbox": bbox, "bbox_pdf": fragment.bbox_pdf,
                "page_width": page_width, "page_height": page_height,
                "polygon": [[bbox[0], bbox[1]], [bbox[2], bbox[1]], [bbox[2], bbox[3]], [bbox[0], bbox[3]]] if bbox else None,
                "role": "context", "context": fragment.text, "extractor": "annotation_context",
                "confidence": fragment.confidence,
            })
        group, should_write_fragments = upsert_evidence_group(
            db, process, param, group_key,
            fields={
                "matrix_version": process.matrix_version,
                "model_version": "official-evidence-baseline-v1",
                "dataset_version": process.dataset_version,
                "comparison_scenario": process.upload_scenario,
                "completeness_status": "PARTIALLY_LOADED" if missing else "COMPLETE",
                "comparability_status": "NOT_COMPARABLE",
                "finding_status": status,
                "review_priority": param.review_priority,
                "delta": {"source": "document_annotations", "matrix_scope": "MATRIX", "reason": reason,
                          "stages": missing, "location": "", "candidate_regions": len(candidates)},
            },
            fragment_specs=fragment_specs,
            user_id=user_id,
        )
        if group is not None and should_write_fragments:
            for spec in fragment_specs:
                db.add(EvidenceFragment(evidence_group_id=group.id, **spec))
            db.flush()
    if explication_compare_enabled():
        explication = collect_explication_groups(
            db, process, params, candidate_docs, gate=gate, touched_keys=touched_keys, user_id=user_id,
        )
        if explication_audit is not None:
            explication_audit(explication)

    # Module 5 (TZ 9.5, free hypothesis search): logical-analysis rules read
    # back the matrix comparisons this run just wrote above, so they must run
    # after that loop but before the sweep below, sharing its `touched_keys`
    # dict -- otherwise a suspicion's group_key would look orphaned to that
    # same sweep and be deleted immediately. See logical_analysis.py.
    run_logical_analysis_module(db, process, params, touched_keys, fragments=fragments, by_id=by_id, user_id=user_id)
    sweep_orphaned_evidence_groups(db, process, {int(p.id) for p in params}, touched_keys, user_id=user_id)
    return fallback_diagnostics


def _stage_code(document) -> str:
    raw = str(getattr(document, "dataset_stage", None) or "").upper()
    if raw in {"PD", "RD", "ID"}:
        return raw
    return {"project": "PD", "working": "RD", "as_built": "ID"}.get(
        str(getattr(document, "doc_stage", None) or getattr(document, "document_stage", None) or ""), "UNKNOWN",
    )


def _quality_for_page(document, page_number: int) -> dict:
    """Assess only pages that became candidate evidence; include a deterministic OCR confidence reason when needed."""
    if int(page_number) < 1:
        return {"quality_status": "OK", "reasons": []}
    from . import dataset_sources

    metadata = document.dataset_metadata if isinstance(document.dataset_metadata, dict) else {}
    manifest = metadata.get("document_manifest") or metadata.get("files_index") or {}
    relative = str(manifest.get("relative_path") or manifest.get("source_relative_path") or "").replace("\\", "/")
    sha256 = str(document.file_hash or document.content_hash or "")
    if not relative.lower().endswith(".pdf") or not sha256:
        return {"quality_status": "OK", "reasons": []}
    cache_key = (relative, sha256, int(page_number))
    if cache_key in _QUALITY_PAGE_CACHE:
        return _QUALITY_PAGE_CACHE[cache_key]

    reasons: list[str] = []
    result: dict[str, Any] = {"page": int(page_number), "rotation": 0, "ocr_confidence": None, "reasons": reasons}
    try:
        import fitz

        data = dataset_sources.original_document_bytes(document)
        with fitz.open(stream=data, filetype="pdf") as pdf:
            if page_number > len(pdf):
                result["quality_status"] = "LOW_QUALITY"
                result["reasons"] = ["PAGE_OUT_OF_RANGE"]
            else:
                page = pdf[page_number - 1]
                rotation = int(page.rotation) % 360
                result["rotation"] = rotation
                if rotation:
                    reasons.append("ROTATED_PAGE")

                text = str(page.get_text("text") or "").strip()
                words = page.get_text("words") or []
                broken_text_layer = dataset_sources._looks_like_broken_cmap_text(text)
                if broken_text_layer or (not text and not words):
                    try:
                        ocr_words = dataset_sources._ocr_page_words(relative, sha256, int(page_number),
                                                                     dataset_sources.settings.OCR_LANG)
                    except Exception:  # OCR unavailable is itself a reason not to trust a textless page.
                        ocr_words = None
                    confidences = [float(word.get("confidence")) for word in ocr_words
                                   if isinstance(word.get("confidence"), (int, float))] if ocr_words else []
                    mean_confidence = sum(confidences) / len(confidences) if confidences else 0.0
                    result["ocr_confidence"] = round(mean_confidence, 1)
                    if not confidences:
                        reasons.append("BROKEN_TEXT_LAYER_OCR_UNAVAILABLE" if broken_text_layer else "NO_TEXT_LAYER_OCR_UNAVAILABLE")
                    elif mean_confidence < 55.0:
                        reasons.append("BROKEN_TEXT_LAYER_LOW_OCR_CONFIDENCE" if broken_text_layer else "NO_TEXT_LAYER_LOW_OCR_CONFIDENCE")

                word_rects = [fitz.Rect(word[:4]) for word in words if len(word) >= 5 and str(word[4]).strip()]
                for annotation in page.annots() or ():
                    annotation_name = str((annotation.type or (None, ""))[1]).casefold()
                    if "stamp" not in annotation_name:
                        continue
                    if any(annotation.rect.intersects(rect) for rect in word_rects):
                        reasons.append("STAMP_OVER_TEXT")
                        break

                page_area = max(1.0, float(page.rect.width) * float(page.rect.height))
                if word_rects:
                    for image in page.get_images(full=True):
                        xref = int(image[0])
                        for image_rect in page.get_image_rects(xref):
                            if image_rect.is_empty or image_rect.get_area() / page_area >= 0.35:
                                continue
                            overlaps = 0
                            for word_rect in word_rects:
                                intersection = image_rect & word_rect
                                if not intersection.is_empty and intersection.get_area() / max(1.0, word_rect.get_area()) >= 0.3:
                                    overlaps += 1
                            if overlaps >= 3:
                                reasons.append("IMAGE_STAMP_OVER_TEXT")
                                break
                        if "IMAGE_STAMP_OVER_TEXT" in reasons:
                            break
                result["quality_status"] = "LOW_QUALITY" if reasons else "OK"
                result["reasons"] = sorted(set(reasons))
    except Exception as exc:
        result["quality_status"] = "LOW_QUALITY"
        result["reasons"] = [f"PAGE_QUALITY_READ_FAILED:{type(exc).__name__}"]
    _QUALITY_PAGE_CACHE[cache_key] = result
    return result


def _mark_source_fragment_low_quality(db, fragment: SourceFragment, quality: dict) -> None:
    metadata = dict(fragment.metadata_json) if isinstance(fragment.metadata_json, dict) else {}
    metadata["quality_status"] = "LOW_QUALITY"
    metadata["quality_flags"] = ["LOW_QUALITY"]
    metadata["quality_reasons"] = list(quality.get("reasons") or [])
    metadata["quality_details"] = {key: quality.get(key) for key in ("page", "rotation", "ocr_confidence")}
    fragment.metadata_json = metadata
    db.add(fragment)


def _quality_issue(code: str, stage: str, document, page: int, quality: dict, *, source_fragment=None,
                   bbox=None, confidence=None) -> dict:
    return {
        "code": str(code), "stage": str(stage), "document_version_id": int(document.id),
        "file_id": document.dataset_file_id, "file": document.filename, "page": int(page),
        "quality_status": "LOW_QUALITY", "reasons": list(quality.get("reasons") or []),
        "rotation": quality.get("rotation"), "ocr_confidence": quality.get("ocr_confidence"),
        "source_fragment": source_fragment, "bbox": bbox, "confidence": confidence,
    }


def _dedupe_quality_issues(issues: list[dict]) -> list[dict]:
    seen: set[tuple[int, int, str]] = set()
    result = []
    for issue in sorted(issues, key=lambda row: (row["stage"], row["file_id"] or "", row["page"])):
        key = (int(issue["document_version_id"]), int(issue["page"]), str(issue["stage"]))
        if key not in seen:
            seen.add(key)
            result.append(issue)
    return result


def _clean_stages_for_param(param, by_code, by_id, rule_observations, generic_observations,
                            enum_observations, compound_observations, table_count_observations) -> set[str]:
    stages = {_stage_code(by_id[fragment.document_version_id]) for fragment in by_code.get(str(param.code), [])
              if fragment.document_version_id in by_id}
    stages.update(str(row.stage) for row in rule_observations.get(str(param.code), []))
    for collection in (generic_observations.get(int(param.id)) or {}, enum_observations.get(int(param.id)) or {},
                       compound_observations.get(int(param.id)) or {}, table_count_observations.get(int(param.id)) or {}):
        stages.update(str(stage) for stage in collection)
    return stages


def _upsert_quality_warning_group(db, process, param, issues: list[dict], *, blocks_comparison: bool,
                                 user_id: int | None = None) -> str | None:
    group_key = "low_quality"
    fragment_specs = []
    for issue in issues:
        document = db.get(DocumentVersion, int(issue["document_version_id"]))
        if document is None:
            continue
        source_fragment = issue.get("source_fragment")
        bbox = issue.get("bbox")
        page_width = page_height = None
        bbox_pdf = None
        if source_fragment is not None:
            geometry = normalized_original_geometry(document, source_fragment)
            if geometry:
                bbox, page_width, page_height = geometry
                bbox_pdf = source_fragment.bbox_pdf
        stage = str(issue["stage"])
        internal_stage = {"PD": "project", "RD": "working", "ID": "as_built"}.get(stage, document.doc_stage)
        fragment_specs.append({
            "document_version_id": int(document.id),
            "source_fragment_id": int(source_fragment.id) if source_fragment is not None else None,
            "dataset_file_id": document.dataset_file_id,
            "file_sha256": document.file_hash or document.content_hash,
            "stage": internal_stage,
            "discipline": document.discipline,
            "document_code": document.document_code,
            "revision": document.revision,
            "approval_status": document.approval_status,
            "page": int(issue["page"]),
            "bbox": bbox,
            "bbox_pdf": bbox_pdf,
            "page_width": page_width,
            "page_height": page_height,
            "polygon": [[bbox[0], bbox[1]], [bbox[2], bbox[1]], [bbox[2], bbox[3]], [bbox[0], bbox[3]]] if bbox else None,
            "role": "context",
            "context": "LOW_QUALITY: " + "; ".join(issue.get("reasons") or []),
            "extractor": "quality_assessment",
            "confidence": (float(issue["ocr_confidence"]) / 100.0 if isinstance(issue.get("ocr_confidence"), (int, float))
                           else issue.get("confidence")),
        })
    safe_issues = [{key: value for key, value in issue.items() if key not in {"source_fragment", "bbox", "confidence"}}
                   for issue in issues]
    group, should_write = upsert_evidence_group(
        db, process, param, group_key,
        fields={
            "matrix_version": process.matrix_version,
            "model_version": "page-quality-v1",
            "dataset_version": process.dataset_version,
            "comparison_scenario": process.upload_scenario,
            "completeness_status": "PARTIALLY_LOADED" if blocks_comparison else "COMPLETE",
            "comparability_status": "LOW_QUALITY",
            "finding_status": "NOT_COMPARABLE",
            "review_priority": param.review_priority,
            "expected_value": None,
            "actual_value": None,
            "delta": {"source": "page_quality", "reason": "LOW_QUALITY", "gate": {"reason": "LOW_QUALITY"},
                      "blocks_comparison": bool(blocks_comparison), "pages": safe_issues},
        },
        fragment_specs=fragment_specs,
        user_id=user_id,
    )
    if group is not None and should_write:
        for spec in fragment_specs:
            db.add(EvidenceFragment(evidence_group_id=int(group.id), **spec))
        db.flush()
    return group_key


def _upsert_selection_clarification_group(db, process, param, block: dict, *, user_id: int | None = None) -> str | None:
    """Keep an unresolved registry/edition decision visible without exporting a violation or a guessed value."""
    key = "file_registry_selection"
    upsert_evidence_group(
        db, process, param, key,
        fields={
            "matrix_version": process.matrix_version,
            "model_version": "file-registry-v1",
            "dataset_version": process.dataset_version,
            "comparison_scenario": process.upload_scenario,
            "completeness_status": "PARTIALLY_LOADED",
            "comparability_status": "NOT_COMPARABLE",
            "finding_status": "CLARIFICATION_REQUIRED",
            "review_priority": param.review_priority,
            "expected_value": None,
            "actual_value": None,
            "delta": {
                "source": "file_registry",
                "reason": "CLARIFICATION_REQUIRED",
                "basis": block.get("basis"),
                "documents": block.get("documents") or [],
            },
        },
        fragment_specs=[],
        user_id=user_id,
    )
    return key


_STAGE_INTERNAL = {"PD": "project", "RD": "working", "ID": "as_built"}


def _with_llm_verification(delta: dict, stage_observations: dict) -> dict:
    """Adds the optional LLM verifier's per-stage verdicts (see
    llm_candidate_verifier.py) to a generic-tier group's `delta` for the
    inspector's evidence card -- only when at least one exists, so `delta`
    stays byte-identical to before whenever the verifier is disabled."""
    verdicts = {
        stage: obs.llm_verification
        for stage, obs in stage_observations.items()
        if getattr(obs, "llm_verification", None)
    }
    if verdicts:
        delta["llm_verification"] = verdicts
    return delta


# ------------------------------------------------------------------------------------------------
# Decision layer (Phase 10, prompt B): gate + trigger policy shared by the four generic tiers.
# ------------------------------------------------------------------------------------------------
_UNIT_FROM_FAMILY = {
    "LENGTH_M": "м", "LENGTH_MM": "мм", "AREA_M2": "м2", "AREA_MM2": "мм2", "VOLUME_M3": "м3",
    "PERCENT": "%", "PERMILLE": "‰", "AREA_HA": "га",
}


def _later(stage_observations: dict) -> list[tuple[str, object]]:
    return [(stage, stage_observations[stage]) for stage in ("RD", "ID") if stage in stage_observations]


def _numeric_trigger(param: Param, stage_observations: dict) -> "tp.TriggerEvaluation":
    rule = tp.rule_for(param.code, trigger_logic=getattr(param, "trigger_logic", None))
    expected = stage_observations["PD"]
    evaluations = []
    for stage, obs in _later(stage_observations):
        family = ((getattr(obs, "gate_facts", None) or {}).get("unit_evidence") or {}).get("adjacent_family")
        evaluations.append(tp.evaluate_numeric(
            rule, expected.decimal_value, obs.decimal_value, value_unit=_UNIT_FROM_FAMILY.get(family), stage=stage,
        ))
    return tp.aggregate(rule, evaluations)


def _enum_trigger(param: Param, stage_observations: dict) -> "tp.TriggerEvaluation":
    rule = tp.rule_for(param.code, trigger_logic=getattr(param, "trigger_logic", None))
    expected = stage_observations["PD"]
    return tp.aggregate(rule, [
        tp.evaluate_enum(rule, expected.canonical_value, obs.canonical_value, stage=stage) for stage, obs in _later(stage_observations)
    ])


def _compound_trigger(param: Param, stage_observations: dict) -> "tp.TriggerEvaluation":
    rule = tp.rule_for(param.code, trigger_logic=getattr(param, "trigger_logic", None))
    expected = stage_observations["PD"]
    return tp.aggregate(rule, [
        tp.evaluate_components(rule, expected.component_labels, expected.normalized_values, obs.normalized_values, stage=stage)
        for stage, obs in _later(stage_observations)
    ])


def _count_trigger(param: Param, stage_observations: dict) -> "tp.TriggerEvaluation":
    rule = tp.rule_for(param.code, trigger_logic=getattr(param, "trigger_logic", None))
    expected = stage_observations["PD"]
    return tp.aggregate(rule, [
        tp.evaluate_numeric(rule, Decimal(expected.row_count), Decimal(obs.row_count), stage=stage) for stage, obs in _later(stage_observations)
    ])


def _verdict(equal: bool, trigger: "tp.TriggerEvaluation | None") -> tuple[str, dict]:
    """(finding_status, extra delta). Different values are a CANDIDATE only when the catalog trigger fires (or
    cannot be decided -- recall first); a change that provably cannot satisfy the trigger is a verified
    negative with `reason = change_not_triggering`. Equal values stay exactly as they were."""
    if equal:
        return "NEGATIVE_VERIFIED", {}
    if trigger is not None and trigger.status == tp.NOT_FIRING:
        return "NEGATIVE_VERIFIED", {"reason": tp.CHANGE_NOT_TRIGGERING, "trigger": trigger.to_delta()}
    return "CANDIDATE", ({"trigger": trigger.to_delta()} if trigger is not None else {})


def _raw_value(tier: str, obs) -> object:
    if tier == "numeric":
        return obs.normalized_value
    if tier == "enum":
        return obs.canonical_value
    if tier == "table_count":
        return obs.row_count
    return obs.display_value


def _upsert_gated_group(
    db: Session,
    process: InspectionProcess,
    param: Param,
    stage_observations: dict,
    decision: GateDecision,
    *,
    tier: str,
    group_key: str,
    model_version: str,
    source: str,
    method: str,
    user_id: int | None = None,
) -> str:
    """Writes the abstention the gate decided on, under the SAME group key the comparable group would have
    used (so an existing inspector decision is preserved/flagged by `upsert_evidence_group`, not orphaned).
    The cited pages stay as context evidence for the inspector; the raw numbers the extractor read are kept in
    `delta.values_seen` but are NOT exported as PD/RD values -- a rejected reading must not be scored as one."""
    needed = {stage for stage, field in (("PD", "source_pd"), ("RD", "source_rd"), ("ID", "source_id")) if getattr(param, field)}
    missing = sorted(needed - set(stage_observations))
    fragment_specs = []
    for stage, obs in stage_observations.items():
        bbox = obs.bbox_normalized
        fragment_specs.append({
            "document_version_id": obs.document.id,
            "source_fragment_id": obs.source_fragment.id if obs.source_fragment and obs.source_fragment.id is not None else None,
            "dataset_file_id": obs.document.dataset_file_id,
            "file_sha256": obs.document.file_hash or obs.document.content_hash,
            "stage": _STAGE_INTERNAL[stage],
            "discipline": obs.document.discipline,
            "document_code": obs.document.document_code,
            "revision": obs.document.revision,
            "approval_status": obs.document.approval_status,
            "page": obs.page,
            "bbox": bbox,
            "bbox_pdf": obs.bbox_pdf,
            "page_width": obs.page_width,
            "page_height": obs.page_height,
            "polygon": [[bbox[0], bbox[1]], [bbox[2], bbox[1]], [bbox[2], bbox[3]], [bbox[0], bbox[3]]],
            "extracted_value": None,
            "role": "context",
            "context": obs.context,
            "extractor": obs.extractor,
            "confidence": obs.confidence,
        })
    delta = {
        "source": source,
        "matrix_scope": "MATRIX",
        "parameter_code": param.code,
        "location": GENERIC_SITE_LOCATION,
        "comparison_result": "COMPARISON_IMPOSSIBLE",
        "values_seen": {stage: _raw_value(tier, obs) for stage, obs in stage_observations.items()},
        "missing_stages": missing,
        "extraction_method": method,
        "gold_validated": False,
        **decision.to_delta(),
    }
    group, should_write_fragments = upsert_evidence_group(
        db, process, param, group_key,
        fields={
            "matrix_version": process.matrix_version,
            "model_version": model_version,
            "dataset_version": process.dataset_version,
            "comparison_scenario": "FULL" if not missing else "PD_RD_PAIRWISE",
            "completeness_status": "COMPLETE" if not missing else "PARTIALLY_LOADED",
            "comparability_status": "NOT_COMPARABLE",
            "finding_status": decision.status,
            "review_priority": param.review_priority,
            "confidence": min(obs.confidence for obs in stage_observations.values()),
            "delta": delta,
        },
        fragment_specs=fragment_specs,
        user_id=user_id,
    )
    if group is not None and should_write_fragments:
        for spec in fragment_specs:
            db.add(EvidenceFragment(evidence_group_id=group.id, **spec))
        db.flush()
    return group_key


def _upsert_generic_group(
    db: Session,
    process: InspectionProcess,
    param: Param,
    stage_observations: dict[str, GenericObservation],
    *,
    user_id: int | None = None,
    gate: GateContext | None = None,
) -> str | None:
    """Mirrors `_upsert_rule_groups`'s PD+RD-required, ID-optional-bonus
    comparison shape, but for a generic anchor-extracted observation instead
    of a tuned rule pack's. Requiring both PD and RD anchor hits to
    independently parse a number is itself a precision gate: a spurious
    single-page match cannot produce a comparable finding on its own, it
    only ever produces one when the *same* mechanism confidently finds a
    number under the *same* label on two independent stages. Returns None
    (no group written) when PD or RD is missing, so the caller falls through
    to the existing MISSING_EVIDENCE/NOT_COMPARABLE annotation-context path
    unchanged -- this mechanism only ever adds a comparable outcome, never
    removes the pre-existing abstention behaviour."""
    if "PD" not in stage_observations or "RD" not in stage_observations:
        return None
    decision = gate.evaluate(param, "numeric", stage_observations) if gate is not None else None
    if decision is not None:
        return _upsert_gated_group(
            db, process, param, stage_observations, decision, tier="numeric", group_key="generic:anchor",
            model_version=GENERIC_EXTRACTOR_VERSION, source="generic_anchor_extractor",
            method="anchor_phrase_plus_nearest_number", user_id=user_id,
        )
    expected = stage_observations["PD"]
    actual = stage_observations.get("ID") or stage_observations["RD"]
    later = [stage_observations[stage] for stage in ("RD", "ID") if stage in stage_observations]
    equal = all(values_equal(expected.decimal_value, obs.decimal_value) for obs in later)
    finding_status, verdict_delta = _verdict(equal, None if equal else _numeric_trigger(param, stage_observations))
    needed = {stage for stage, field in (("PD", "source_pd"), ("RD", "source_rd"), ("ID", "source_id")) if getattr(param, field)}
    missing = sorted(needed - set(stage_observations))

    fragment_specs = []
    for stage, obs in stage_observations.items():
        role = "expected" if stage == "PD" else ("actual" if obs is actual else "context")
        bbox = obs.bbox_normalized
        fragment_specs.append({
            "document_version_id": obs.document.id,
            "source_fragment_id": obs.source_fragment.id if obs.source_fragment and obs.source_fragment.id is not None else None,
            "dataset_file_id": obs.document.dataset_file_id,
            "file_sha256": obs.document.file_hash or obs.document.content_hash,
            "stage": _STAGE_INTERNAL[stage],
            "discipline": obs.document.discipline,
            "document_code": obs.document.document_code,
            "revision": obs.document.revision,
            "approval_status": obs.document.approval_status,
            "page": obs.page,
            "bbox": bbox,
            "bbox_pdf": obs.bbox_pdf,
            "page_width": obs.page_width,
            "page_height": obs.page_height,
            "polygon": [[bbox[0], bbox[1]], [bbox[2], bbox[1]], [bbox[2], bbox[3]], [bbox[0], bbox[3]]],
            "extracted_value": obs.value,
            "role": role,
            "context": obs.context,
            "extractor": obs.extractor,
            "confidence": obs.confidence,
        })

    group_key = "generic:anchor"
    group, should_write_fragments = upsert_evidence_group(
        db, process, param, group_key,
        fields={
            "matrix_version": process.matrix_version,
            "model_version": GENERIC_EXTRACTOR_VERSION,
            "dataset_version": process.dataset_version,
            "comparison_scenario": "FULL" if not missing else "PD_RD_PAIRWISE",
            "completeness_status": "COMPLETE" if not missing else "PARTIALLY_LOADED",
            "comparability_status": "COMPARABLE",
            "finding_status": finding_status,
            "expected_value": expected.value,
            "actual_value": actual.value,
            "review_priority": param.review_priority,
            "confidence": min(obs.confidence for obs in [expected, *later]),
            "delta": _with_llm_verification({
                "source": "generic_anchor_extractor",
                "matrix_scope": "MATRIX",
                "parameter_code": param.code,
                "location": expected.location,
                "comparison_result": "TRIGGERED" if finding_status == "CANDIDATE" else "NON_TRIGGERING",
                "values": {stage: obs.normalized_value for stage, obs in stage_observations.items()},
                "missing_stages": missing,
                "extraction_method": "anchor_phrase_plus_nearest_number",
                "gold_validated": False,
                **verdict_delta,
            }, stage_observations),
        },
        fragment_specs=fragment_specs,
        user_id=user_id,
    )
    if group is not None and should_write_fragments:
        for spec in fragment_specs:
            db.add(EvidenceFragment(evidence_group_id=group.id, **spec))
        db.flush()
    return group_key


def _upsert_enum_group(
    db: Session,
    process: InspectionProcess,
    param: Param,
    stage_observations: dict[str, EnumObservation],
    *,
    user_id: int | None = None,
    gate: GateContext | None = None,
) -> str | None:
    """Same PD+RD-required, ID-optional-bonus shape as `_upsert_generic_group`,
    for an enum-class anchor observation instead of a number: comparison is
    exact canonical-value equality (`enum_values_equal`), never a numeric
    tolerance. Returns None (no group written) when PD or RD is missing, so
    the caller falls through to the existing MISSING_EVIDENCE/NOT_COMPARABLE
    annotation-context path unchanged."""
    if "PD" not in stage_observations or "RD" not in stage_observations:
        return None
    decision = gate.evaluate(param, "enum", stage_observations) if gate is not None else None
    if decision is not None:
        return _upsert_gated_group(
            db, process, param, stage_observations, decision, tier="enum", group_key="generic:enum",
            model_version=GENERIC_ENUM_EXTRACTOR_VERSION, source="generic_enum_extractor",
            method="anchor_phrase_plus_enum_vocabulary", user_id=user_id,
        )
    expected = stage_observations["PD"]
    actual = stage_observations.get("ID") or stage_observations["RD"]
    later = [stage_observations[stage] for stage in ("RD", "ID") if stage in stage_observations]
    equal = all(enum_values_equal(expected.canonical_value, obs.canonical_value) for obs in later)
    finding_status, verdict_delta = _verdict(equal, None if equal else _enum_trigger(param, stage_observations))
    needed = {stage for stage, field in (("PD", "source_pd"), ("RD", "source_rd"), ("ID", "source_id")) if getattr(param, field)}
    missing = sorted(needed - set(stage_observations))

    fragment_specs = []
    for stage, obs in stage_observations.items():
        role = "expected" if stage == "PD" else ("actual" if obs is actual else "context")
        bbox = obs.bbox_normalized
        fragment_specs.append({
            "document_version_id": obs.document.id,
            "source_fragment_id": obs.source_fragment.id if obs.source_fragment and obs.source_fragment.id is not None else None,
            "dataset_file_id": obs.document.dataset_file_id,
            "file_sha256": obs.document.file_hash or obs.document.content_hash,
            "stage": _STAGE_INTERNAL[stage],
            "discipline": obs.document.discipline,
            "document_code": obs.document.document_code,
            "revision": obs.document.revision,
            "approval_status": obs.document.approval_status,
            "page": obs.page,
            "bbox": bbox,
            "bbox_pdf": obs.bbox_pdf,
            "page_width": obs.page_width,
            "page_height": obs.page_height,
            "polygon": [[bbox[0], bbox[1]], [bbox[2], bbox[1]], [bbox[2], bbox[3]], [bbox[0], bbox[3]]],
            "extracted_value": obs.value,
            "role": role,
            "context": obs.context,
            "extractor": obs.extractor,
            "confidence": obs.confidence,
        })

    group_key = "generic:enum"
    group, should_write_fragments = upsert_evidence_group(
        db, process, param, group_key,
        fields={
            "matrix_version": process.matrix_version,
            "model_version": GENERIC_ENUM_EXTRACTOR_VERSION,
            "dataset_version": process.dataset_version,
            "comparison_scenario": "FULL" if not missing else "PD_RD_PAIRWISE",
            "completeness_status": "COMPLETE" if not missing else "PARTIALLY_LOADED",
            "comparability_status": "COMPARABLE",
            "finding_status": finding_status,
            "expected_value": expected.value,
            "actual_value": actual.value,
            "review_priority": param.review_priority,
            "confidence": min(obs.confidence for obs in [expected, *later]),
            "delta": _with_llm_verification({
                "source": "generic_enum_extractor",
                "matrix_scope": "MATRIX",
                "parameter_code": param.code,
                "location": expected.location,
                "comparison_result": "TRIGGERED" if finding_status == "CANDIDATE" else "NON_TRIGGERING",
                "values": {stage: obs.canonical_value for stage, obs in stage_observations.items()},
                "vocabulary_family": expected.family,
                "missing_stages": missing,
                "extraction_method": "anchor_phrase_plus_enum_vocabulary",
                "gold_validated": False,
                **verdict_delta,
            }, stage_observations),
        },
        fragment_specs=fragment_specs,
        user_id=user_id,
    )
    if group is not None and should_write_fragments:
        for spec in fragment_specs:
            db.add(EvidenceFragment(evidence_group_id=group.id, **spec))
        db.flush()
    return group_key


def _upsert_compound_group(
    db: Session,
    process: InspectionProcess,
    param: Param,
    stage_observations: dict[str, CompoundObservation],
    *,
    user_id: int | None = None,
    gate: GateContext | None = None,
) -> str | None:
    """Same PD+RD-required, ID-optional-bonus shape as `_upsert_generic_group`
    / `_upsert_enum_group`, for a multi-component compound observation: the
    comparison is component-wise (`compare_components`), so a single
    differing field is enough to flag `CANDIDATE` even when every other
    field agrees. Returns None (no group written) when PD or RD is missing."""
    if "PD" not in stage_observations or "RD" not in stage_observations:
        return None
    decision = gate.evaluate(param, "compound", stage_observations) if gate is not None else None
    if decision is not None:
        return _upsert_gated_group(
            db, process, param, stage_observations, decision, tier="compound", group_key="generic:compound",
            model_version=GENERIC_COMPOUND_EXTRACTOR_VERSION, source="generic_compound_extractor",
            method="anchor_phrase_plus_sequential_numbers", user_id=user_id,
        )
    expected = stage_observations["PD"]
    actual = stage_observations.get("ID") or stage_observations["RD"]
    later = [stage_observations[stage] for stage in ("RD", "ID") if stage in stage_observations]
    equal = all(all(c.equal for c in compare_components(expected, obs)) for obs in later)
    finding_status, verdict_delta = _verdict(equal, None if equal else _compound_trigger(param, stage_observations))
    needed = {stage for stage, field in (("PD", "source_pd"), ("RD", "source_rd"), ("ID", "source_id")) if getattr(param, field)}
    missing = sorted(needed - set(stage_observations))

    fragment_specs = []
    for stage, obs in stage_observations.items():
        role = "expected" if stage == "PD" else ("actual" if obs is actual else "context")
        bbox = obs.bbox_normalized
        fragment_specs.append({
            "document_version_id": obs.document.id,
            "source_fragment_id": obs.source_fragment.id if obs.source_fragment and obs.source_fragment.id is not None else None,
            "dataset_file_id": obs.document.dataset_file_id,
            "file_sha256": obs.document.file_hash or obs.document.content_hash,
            "stage": _STAGE_INTERNAL[stage],
            "discipline": obs.document.discipline,
            "document_code": obs.document.document_code,
            "revision": obs.document.revision,
            "approval_status": obs.document.approval_status,
            "page": obs.page,
            "bbox": bbox,
            "bbox_pdf": obs.bbox_pdf,
            "page_width": obs.page_width,
            "page_height": obs.page_height,
            "polygon": [[bbox[0], bbox[1]], [bbox[2], bbox[1]], [bbox[2], bbox[3]], [bbox[0], bbox[3]]],
            "extracted_value": obs.display_value,
            "role": role,
            "context": obs.context,
            "extractor": obs.extractor,
            "confidence": obs.confidence,
        })

    group_key = "generic:compound"
    group, should_write_fragments = upsert_evidence_group(
        db, process, param, group_key,
        fields={
            "matrix_version": process.matrix_version,
            "model_version": GENERIC_COMPOUND_EXTRACTOR_VERSION,
            "dataset_version": process.dataset_version,
            "comparison_scenario": "FULL" if not missing else "PD_RD_PAIRWISE",
            "completeness_status": "COMPLETE" if not missing else "PARTIALLY_LOADED",
            "comparability_status": "COMPARABLE",
            "finding_status": finding_status,
            "expected_value": expected.display_value,
            "actual_value": actual.display_value,
            "review_priority": param.review_priority,
            "confidence": min(obs.confidence for obs in [expected, *later]),
            "delta": _with_llm_verification({
                "source": "generic_compound_extractor",
                "matrix_scope": "MATRIX",
                "parameter_code": param.code,
                "location": expected.location,
                "comparison_result": "TRIGGERED" if finding_status == "CANDIDATE" else "NON_TRIGGERING",
                "values": {stage: obs.display_value for stage, obs in stage_observations.items()},
                "components": [
                    {"label": c.label, "expected": c.expected, "actual": c.actual, "equal": c.equal}
                    for c in compare_components(expected, actual)
                ],
                "missing_stages": missing,
                "extraction_method": "anchor_phrase_plus_sequential_numbers",
                "gold_validated": False,
                **verdict_delta,
            }, stage_observations),
        },
        fragment_specs=fragment_specs,
        user_id=user_id,
    )
    if group is not None and should_write_fragments:
        for spec in fragment_specs:
            db.add(EvidenceFragment(evidence_group_id=group.id, **spec))
        db.flush()
    return group_key


def _upsert_table_count_group(
    db: Session,
    process: InspectionProcess,
    param: Param,
    stage_observations: dict[str, TableCountObservation],
    *,
    user_id: int | None = None,
    gate: GateContext | None = None,
) -> str | None:
    """Same PD+RD-required, ID-optional-bonus shape as the other three
    generic mechanisms, for a table-row-count observation: comparison is
    exact integer equality (`counts_equal`) -- a row count has no meaningful
    rounding tolerance. Returns None (no group written) when PD or RD is
    missing (in practice PD rarely yields one: see module docstring -- most
    PD-side sources here are a ТЭП summary scalar, not an enumerable table,
    so this mechanism's real comparable yield is expected to be low without
    also widening the numeric mechanism to NUMERIC_COUNT units, which is out
    of this session's scope)."""
    if "PD" not in stage_observations or "RD" not in stage_observations:
        return None
    decision = gate.evaluate(param, "table_count", stage_observations) if gate is not None else None
    if decision is not None:
        return _upsert_gated_group(
            db, process, param, stage_observations, decision, tier="table_count", group_key="generic:table_count",
            model_version=GENERIC_TABLE_COUNT_EXTRACTOR_VERSION, source="generic_table_row_count_extractor",
            method="title_match_plus_row_clustering", user_id=user_id,
        )
    expected = stage_observations["PD"]
    actual = stage_observations.get("ID") or stage_observations["RD"]
    later = [stage_observations[stage] for stage in ("RD", "ID") if stage in stage_observations]
    equal = all(counts_equal(expected.row_count, obs.row_count) for obs in later)
    finding_status, verdict_delta = _verdict(equal, None if equal else _count_trigger(param, stage_observations))
    needed = {stage for stage, field in (("PD", "source_pd"), ("RD", "source_rd"), ("ID", "source_id")) if getattr(param, field)}
    missing = sorted(needed - set(stage_observations))

    fragment_specs = []
    for stage, obs in stage_observations.items():
        role = "expected" if stage == "PD" else ("actual" if obs is actual else "context")
        bbox = obs.bbox_normalized
        fragment_specs.append({
            "document_version_id": obs.document.id,
            "source_fragment_id": obs.source_fragment.id if obs.source_fragment and obs.source_fragment.id is not None else None,
            "dataset_file_id": obs.document.dataset_file_id,
            "file_sha256": obs.document.file_hash or obs.document.content_hash,
            "stage": _STAGE_INTERNAL[stage],
            "discipline": obs.document.discipline,
            "document_code": obs.document.document_code,
            "revision": obs.document.revision,
            "approval_status": obs.document.approval_status,
            "page": obs.page,
            "bbox": bbox,
            "bbox_pdf": obs.bbox_pdf,
            "page_width": obs.page_width,
            "page_height": obs.page_height,
            "polygon": [[bbox[0], bbox[1]], [bbox[2], bbox[1]], [bbox[2], bbox[3]], [bbox[0], bbox[3]]],
            "extracted_value": obs.display_value,
            "role": role,
            "context": obs.context,
            "extractor": obs.extractor,
            "confidence": obs.confidence,
        })

    group_key = "generic:table_count"
    group, should_write_fragments = upsert_evidence_group(
        db, process, param, group_key,
        fields={
            "matrix_version": process.matrix_version,
            "model_version": GENERIC_TABLE_COUNT_EXTRACTOR_VERSION,
            "dataset_version": process.dataset_version,
            "comparison_scenario": "FULL" if not missing else "PD_RD_PAIRWISE",
            "completeness_status": "COMPLETE" if not missing else "PARTIALLY_LOADED",
            "comparability_status": "COMPARABLE",
            "finding_status": finding_status,
            "expected_value": expected.display_value,
            "actual_value": actual.display_value,
            "review_priority": param.review_priority,
            "confidence": min(obs.confidence for obs in [expected, *later]),
            "delta": _with_llm_verification({
                "source": "generic_table_row_count_extractor",
                "matrix_scope": "MATRIX",
                "parameter_code": param.code,
                "location": expected.location,
                "comparison_result": "TRIGGERED" if finding_status == "CANDIDATE" else "NON_TRIGGERING",
                "values": {stage: obs.row_count for stage, obs in stage_observations.items()},
                "missing_stages": missing,
                "extraction_method": "title_match_plus_row_clustering",
                "gold_validated": False,
                **verdict_delta,
            }, stage_observations),
        },
        fragment_specs=fragment_specs,
        user_id=user_id,
    )
    if group is not None and should_write_fragments:
        for spec in fragment_specs:
            db.add(EvidenceFragment(evidence_group_id=group.id, **spec))
        db.flush()
    return group_key


def _upsert_rule_groups(
    db, process, param, observations, *, documents=None, exclude_pages=None, budget=None, fallback_diagnostics=None, user_id=None,
) -> set[str]:
    """Same rule-pack matching as before, but persisted through
    `upsert_evidence_group` keyed by `location` so a location whose evidence
    is unchanged since the last run is never touched, and one an inspector
    already decided on is preserved (flagged, not silently overwritten) when
    its evidence basis changes."""
    by_location = defaultdict(lambda: defaultdict(list))
    for observation in observations:
        by_location[observation.location][observation.stage].append(observation)

    touched: set[str] = set()
    for location, by_stage in by_location.items():
        if not by_stage.get("PD") or not by_stage.get("RD"):
            continue
        selected = {
            stage: sorted(rows, key=lambda row: (-row.confidence, int(row.document.id), row.page))[0]
            for stage, rows in by_stage.items()
            if stage in {"PD", "RD", "ID"} and rows
        }
        expected = selected["PD"]
        later = [selected[stage] for stage in ("RD", "ID") if stage in selected]
        comparisons = [rule_is_violation(param.code, expected.normalized_value, row.normalized_value) for row in later]
        if not comparisons or any(result is None for result in comparisons):
            continue
        violation = any(comparisons)
        actual = selected.get("ID") or selected["RD"]
        missing = [stage for stage, enabled in (("PD", param.source_pd), ("RD", param.source_rd), ("ID", param.source_id)) if enabled and stage not in selected]
        # Bounded, confirm-only fallback: only for a location PD/RD already agree
        # is NOT a violation, and only to look for evidence of the SAME value the
        # other stages already established. It can never introduce a value that
        # was not already the decision, so it cannot manufacture a new violation.
        if not violation and missing and documents and budget is not None:
            for stage in list(missing):
                candidate, diagnostics = find_confirming_fallback_observation(
                    param.code,
                    stage,
                    location=location,
                    expected_normalized_value=expected.normalized_value,
                    documents=documents,
                    exclude_pages=exclude_pages or set(),
                    budget=budget,
                )
                if fallback_diagnostics is not None:
                    fallback_diagnostics.append(diagnostics)
                if candidate:
                    selected[stage] = candidate
                    by_stage.setdefault(stage, []).append(candidate)
            missing = [stage for stage, enabled in (("PD", param.source_pd), ("RD", param.source_rd), ("ID", param.source_id)) if enabled and stage not in selected]
            actual = selected.get("ID") or selected["RD"]
        # Multiple pages of a stage's document can independently confirm the same
        # normalized value (e.g. a note repeated on a plan and a section sheet); keep
        # all of them as corroborating evidence instead of an arbitrary single pick.
        supporting = {
            stage: sorted(
                (row for row in rows if row.normalized_value == selected[stage].normalized_value),
                key=lambda row: (-row.confidence, int(row.document.id), row.page),
            )
            for stage, rows in by_stage.items()
            if stage in selected
        }
        fragment_specs = []
        for stage in ("PD", "RD", "ID"):
            role = "expected" if stage == "PD" else "actual" if stage == "ID" or "ID" not in selected else "context"
            for observation in supporting.get(stage, []):
                bbox = observation.bbox_normalized
                fragment_specs.append({
                    "document_version_id": observation.document.id,
                    "source_fragment_id": observation.source_fragment.id if observation.source_fragment else None,
                    "dataset_file_id": observation.document.dataset_file_id,
                    "file_sha256": observation.document.file_hash or observation.document.content_hash,
                    "stage": {"PD": "project", "RD": "working", "ID": "as_built"}[stage],
                    "discipline": observation.document.discipline,
                    "document_code": observation.document.document_code,
                    "revision": observation.document.revision,
                    "approval_status": observation.document.approval_status,
                    "page": observation.page,
                    "bbox": bbox,
                    "bbox_pdf": observation.bbox_pdf,
                    "page_width": observation.page_width,
                    "page_height": observation.page_height,
                    "polygon": [[bbox[0], bbox[1]], [bbox[2], bbox[1]], [bbox[2], bbox[3]], [bbox[0], bbox[3]]],
                    "extracted_value": observation.value,
                    "role": role,
                    "context": observation.context,
                    "extractor": observation.extractor,
                    "confidence": observation.confidence,
                })
        group_key = f"loc:{location}"
        group, should_write_fragments = upsert_evidence_group(
            db, process, param, group_key,
            fields={
                "matrix_version": process.matrix_version,
                "model_version": "official-rule-packs-v2",
                "dataset_version": process.dataset_version,
                "comparison_scenario": "FULL" if not missing else "PD_RD_PAIRWISE",
                "completeness_status": "COMPLETE" if not missing else "PARTIALLY_LOADED",
                "comparability_status": "COMPARABLE",
                "finding_status": "CANDIDATE" if violation else "NEGATIVE_VERIFIED",
                "expected_value": expected.value,
                "actual_value": actual.value,
                "review_priority": param.review_priority,
                "confidence": min(row.confidence for row in [expected, *later]),
                "delta": {
                    "source": "official_rule_pack",
                    "matrix_scope": "MATRIX",
                    "parameter_code": param.code,
                    "location": location,
                    "comparison_result": "TRIGGERED" if violation else "NON_TRIGGERING",
                    "values": {stage: row.normalized_value for stage, row in selected.items()},
                    "missing_stages": missing,
                },
            },
            fragment_specs=fragment_specs,
            user_id=user_id,
        )
        touched.add(group_key)
        if group is not None and should_write_fragments:
            for spec in fragment_specs:
                db.add(EvidenceFragment(evidence_group_id=group.id, **spec))
            db.flush()
    return touched
