"""File-registry policy for the S5 inspector workbench and the CASE10 comparison gate.

The parser and package matcher live in :mod:`app.batch_package` (S6). This module consumes the imported 1.1
manifest rows and applies the Перечень ИД 1.1 source-selection rules: approved editions and explicit replacement
chains may be selected automatically; unresolved competition blocks a verdict until an inspector records a choice.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime
import hashlib
from typing import Any, Iterable

from fastapi import HTTPException
from sqlalchemy.orm import Session

from ..batch_package import PACKAGE_SPLIT, STATUS_ACCEPTED, STATUS_CLARIFICATION_REQUIRED
from ..db.models import AuditLog, DocumentVersion, InspectorEdit, Param
from . import document_facts as df

APPROVED_STATUSES = frozenset({"APPROVED", "FOR_CONSTRUCTION", "SIGNED"})
FORBIDDEN_STATUSES = frozenset({"SUPERSEDED", "CANCELLED"})
_STAGES = ("PD", "RD", "ID")
_INTERNAL_STAGE = {"project": "PD", "working": "RD", "as_built": "ID"}
_STAGE_SOURCE = {"PD": "source_pd", "RD": "source_rd", "ID": "source_id"}


def _manifest(doc: DocumentVersion) -> dict[str, Any]:
    metadata = doc.dataset_metadata if isinstance(doc.dataset_metadata, dict) else {}
    row = metadata.get("document_manifest") or metadata.get("files_index") or {}
    return row if isinstance(row, dict) else {}


def _docs(db: Session, process) -> list[DocumentVersion]:
    from .v3_pipeline import _project_documents

    return list(_project_documents(db, process))


def _process_registry_report(db: Session, process) -> dict[str, Any] | None:
    row = (
        db.query(AuditLog)
        .filter(
            AuditLog.process_id == str(process.id),
            AuditLog.action.in_(("CASE10_BATCH_PACKAGE_IMPORTED", "CASE10_UPLOAD_PACKAGE_IMPORTED")),
        )
        .order_by(AuditLog.id.desc())
        .first()
    )
    details = row.details if row and isinstance(row.details, dict) else {}
    report = details.get("report")
    return report if isinstance(report, dict) else None


def _registry_status(doc: DocumentVersion, package_report: dict[str, Any] | None) -> tuple[str | None, str | None]:
    if str(doc.dataset_split or "").upper() != PACKAGE_SPLIT:
        return None, None
    row = _manifest(doc)
    relative = str(row.get("relative_path") or "").replace("\\", "/").strip("/")
    for issue in (package_report or {}).get("issues") or []:
        if not isinstance(issue, dict):
            continue
        issue_file = str(issue.get("file") or "").replace("\\", "/").strip("/")
        if not issue_file or not (relative == issue_file or relative.endswith("/" + issue_file)):
            continue
        link_issues = [str(value) for value in issue.get("issues") or []
                       if str(value) in {"PREDECESSOR_NOT_IN_PACKAGE", "SUCCESSOR_NOT_IN_PACKAGE"}]
        if link_issues:
            ref = str(issue.get("ref") or "неизвестный file_id")
            return STATUS_CLARIFICATION_REQUIRED, f"{', '.join(sorted(set(link_issues)))}: {ref}"
    status = str(row.get("registry_status") or (package_report or {}).get("registry_status") or "").upper() or None
    reason = str(row.get("registry_status_reason") or (package_report or {}).get("registry_status_reason") or "").upper() or None
    if status is None:
        if not row.get("registry"):
            return STATUS_CLARIFICATION_REQUIRED, "REGISTRY_MISSING"
        return STATUS_ACCEPTED, None
    return status, reason


def _approval(doc: DocumentVersion) -> str:
    return str(doc.approval_status or "UNKNOWN").strip().upper()


def _doc_summary(doc: DocumentVersion) -> dict[str, Any]:
    stage = str(doc.dataset_stage or "").upper()
    if stage not in _STAGES:
        stage = _INTERNAL_STAGE.get(str(doc.doc_stage or doc.document_stage or ""), "UNKNOWN")
    return {
        "document_version_id": int(doc.id),
        "file_id": doc.dataset_file_id,
        "file": doc.filename,
        "document_code": doc.document_code,
        "revision": doc.revision,
        "approval_status": _approval(doc),
        "approval_date": doc.approval_date.isoformat() if doc.approval_date else None,
        "predecessor_id": doc.predecessor_id,
        "successor_id": doc.successor_id,
        "stage": stage,
        "discipline": doc.discipline or doc.dataset_section,
    }


def _date_value(doc: DocumentVersion) -> date | None:
    value = doc.approval_date
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _choose_system_candidate(candidates: list[DocumentVersion], *, reason: str, explicit_tip: int | None = None) -> tuple[str, int | None, str]:
    active = [doc for doc in candidates if _approval(doc) not in FORBIDDEN_STATUSES]
    if explicit_tip is not None:
        tip = next((doc for doc in active if int(doc.id) == int(explicit_tip)), None)
        if tip and _approval(tip) in APPROVED_STATUSES:
            return "RESOLVED", int(tip.id), f"{reason}; единственный утверждённый конец цепочки predecessor/successor"

    approved = [doc for doc in active if _approval(doc) in APPROVED_STATUSES]
    if len(approved) == 1:
        doc = approved[0]
        date_text = f", дата утверждения {doc.approval_date.date().isoformat()}" if isinstance(doc.approval_date, datetime) else ""
        return "RESOLVED", int(doc.id), f"{reason}; единственная утверждённая редакция{date_text}"
    if len(approved) > 1:
        dated = [(doc, _date_value(doc)) for doc in approved]
        valid_dates = [value for _doc, value in dated if value is not None]
        latest = max(valid_dates) if valid_dates else None
        newest = [doc for doc, value in dated if latest is not None and value == latest]
        if len(valid_dates) == len(approved) and len(newest) == 1:
            return "RESOLVED", int(newest[0].id), f"{reason}; выбрана единственная редакция с наиболее поздней датой утверждения {latest.isoformat()}"
    return "CLARIFICATION_REQUIRED", None, f"{reason}; несколько возможных источников или нет утверждённой редакции"


def _scope_key(stage: str | None, scope_type: str, scope: str | None, ids: Iterable[int]) -> str:
    material = "|".join(str(item) for item in sorted(int(value) for value in ids))
    suffix = hashlib.sha1(material.encode("utf-8")).hexdigest()[:8]
    return f"{stage or '?'}|{scope_type}|{scope or 'UNKNOWN'}|{suffix}"


def _inspector_choices(db: Session, process) -> dict[str, InspectorEdit]:
    rows = (
        db.query(InspectorEdit)
        .filter(InspectorEdit.process_id == str(process.id), InspectorEdit.entity_type == "REVISION_CHOICE")
        .order_by(InspectorEdit.version.asc(), InspectorEdit.id.asc())
        .all()
    )
    latest: dict[str, InspectorEdit] = {}
    for row in rows:
        latest[str(row.entity_key)] = row
    return latest


def revision_scopes(db: Session, process) -> list[dict[str, Any]]:
    """Return the S5 revision-scope contract, enriched with registry approval and chain fields."""
    docs = _docs(db, process)
    by_id = {int(doc.id): doc for doc in docs}
    package_report = _process_registry_report(db, process)
    facts_by_id = {int(doc.id): df.facts_from_document(doc) for doc in docs}
    facts = list(facts_by_id.values())
    integrity = df.analyze_integrity(facts)
    revisions = df.analyze_revisions(facts, exclude_keys=integrity.excluded_keys)
    scopes: list[dict[str, Any]] = []
    covered: set[int] = set()

    def add_scope(stage: str | None, scope: str | None, scope_type: str, candidates: list[DocumentVersion],
                  basis: str, explicit_tip: int | None = None, force_clarification: bool = False) -> None:
        unique = {int(doc.id): doc for doc in candidates}
        candidates = sorted(unique.values(), key=lambda doc: (str(doc.dataset_file_id or ""), int(doc.id)))
        if not candidates:
            return
        if force_clarification:
            status, choice_id, resolved_basis = (
                "CLARIFICATION_REQUIRED", None,
                f"{basis}; цепочка predecessor/successor не образует единственную линейную последовательность",
            )
        else:
            status, choice_id, resolved_basis = _choose_system_candidate(candidates, reason=basis, explicit_tip=explicit_tip)
        scopes.append({
            "scope_key": _scope_key(stage, scope_type, scope, (int(doc.id) for doc in candidates)),
            "stage": stage,
            "scope": scope,
            "type": scope_type,
            "system_status": status,
            "system_choice_id": choice_id,
            "basis": resolved_basis,
            "candidates": [_doc_summary(doc) for doc in candidates],
        })
        covered.update(int(doc.id) for doc in candidates)

    # Explicit predecessor/successor chains from Перечень ИД 1.1 take precedence over filename heuristics.
    remaining = set(by_id)
    while remaining:
        seed = min(remaining)
        component: set[int] = set()
        pending = [seed]
        while pending:
            current = pending.pop()
            if current in component or current not in by_id:
                continue
            component.add(current)
            doc = by_id[current]
            for other in (doc.predecessor_id, doc.successor_id):
                if other is not None and int(other) in by_id and int(other) not in component:
                    pending.append(int(other))
        remaining -= component
        if len(component) < 2:
            continue
        chain_docs = [by_id[doc_id] for doc_id in sorted(component)]
        roots = [doc for doc in chain_docs if doc.predecessor_id is None or int(doc.predecessor_id) not in component]
        tips = [doc for doc in chain_docs if doc.successor_id is None or int(doc.successor_id) not in component]
        in_degree: Counter[int] = Counter()
        out_degree: Counter[int] = Counter()
        for doc in chain_docs:
            if doc.predecessor_id is not None and int(doc.predecessor_id) in component:
                in_degree[int(doc.id)] += 1
            if doc.successor_id is not None and int(doc.successor_id) in component:
                out_degree[int(doc.id)] += 1
        linear_chain = len(roots) == 1 and len(tips) == 1 and all(value <= 1 for value in in_degree.values()) \
            and all(value <= 1 for value in out_degree.values())
        explicit_tip = int(tips[0].id) if linear_chain else None
        stage_values = {facts_by_id[int(doc.id)].stage for doc in chain_docs}
        stage = next(iter(stage_values)) if len(stage_values) == 1 else "RD_ID_MIXED"
        scope = str(chain_docs[0].document_code or facts_by_id[int(chain_docs[0].id)].section_key or "revision-chain")
        add_scope(stage, scope, "PREDECESSOR_CHAIN", chain_docs,
                  "реестр Перечня ИД 1.1: predecessor_id / successor_id, approval_status и approval_date",
                  explicit_tip=explicit_tip, force_clarification=not linear_chain)

    for conflict in revisions.conflicts:
        candidates = [by_id[facts_by_id_key.doc_id] for facts_by_id_key in facts
                      if facts_by_id_key.key in conflict.keys and facts_by_id_key.doc_id in by_id]
        ids = {int(doc.id) for doc in candidates}
        if not candidates or ids <= covered:
            continue
        add_scope(conflict.stage, conflict.scope, conflict.type, candidates,
                  str(conflict.detail.get("reason") or "редакции не упорядочиваются по шифру/редакции"))

    superseded_groups: dict[str, set[str]] = defaultdict(set)
    for old_key, newest_key in revisions.superseded.items():
        superseded_groups[str(newest_key)].add(str(old_key))
    for newest_key, old_keys in sorted(superseded_groups.items()):
        matching = [doc for fact in facts if fact.key == newest_key or fact.key in old_keys
                    for doc in [by_id.get(int(fact.doc_id))] if doc is not None]
        if not matching or {int(doc.id) for doc in matching} <= covered:
            continue
        newest = next((doc for doc in matching if df.facts_from_document(doc).key == newest_key), None)
        tip = int(newest.id) if newest else None
        fact = facts_by_id[int(matching[0].id)]
        add_scope(fact.stage, fact.section_key, "ORDERED_REVISIONS", matching,
                  "маркер редакции и связанная approval_status / approval_date из реестра",
                  explicit_tip=tip)

    for doc in sorted(docs, key=lambda item: int(item.id)):
        if int(doc.id) in covered:
            continue
        status, reason = _registry_status(doc, package_report)
        manifest = _manifest(doc)
        issues = manifest.get("registry_issues") or []
        approval = _approval(doc)
        registry_enforced = str(doc.dataset_split or "").upper() == PACKAGE_SPLIT or bool(manifest.get("registry_required"))
        if (status is not None and status != STATUS_ACCEPTED) or issues:
            stage = facts_by_id[int(doc.id)].stage
            detail = ", ".join(str(item) for item in issues) if issues else reason or "неопределённый статус реестра"
            add_scope(stage, str(doc.document_code or doc.dataset_file_id or doc.id), "REGISTRY_MATCH", [doc],
                      f"Перечень ИД 1.1: {detail}")
            continue
        if registry_enforced and approval not in APPROVED_STATUSES | FORBIDDEN_STATUSES:
            stage = facts_by_id[int(doc.id)].stage
            add_scope(stage, str(doc.document_code or doc.dataset_file_id or doc.id), "APPROVAL_STATUS", [doc],
                      f"реестр содержит approval_status={approval}; утверждённость источника не подтверждена")

    return sorted(scopes, key=lambda scope: (str(scope.get("stage") or ""), str(scope.get("scope") or ""), scope["scope_key"]))


def selection_context(db: Session, process, docs: list[DocumentVersion] | None = None) -> dict[str, Any]:
    """Machine-readable policy used by the comparison pipeline before it can emit a verdict."""
    docs = list(docs if docs is not None else _docs(db, process))
    package_report = _process_registry_report(db, process)
    by_id = {int(doc.id): doc for doc in docs}
    inspector_choices = _inspector_choices(db, process)
    scopes = revision_scopes(db, process)
    excluded: set[int] = set()
    blocked: dict[int, dict[str, Any]] = {}
    for doc in docs:
        if _approval(doc) in FORBIDDEN_STATUSES:
            excluded.add(int(doc.id))
            continue
        status, reason = _registry_status(doc, package_report)
        manifest = _manifest(doc)
        issues = manifest.get("registry_issues") or []
        if status == STATUS_CLARIFICATION_REQUIRED or issues:
            blocked[int(doc.id)] = {
                "reason": "registry_clarification_required",
                "basis": reason or "; ".join(str(item) for item in issues) or "реестр отсутствует или не совпадает с файлами",
                "file_id": doc.dataset_file_id,
            }

    for scope in scopes:
        key = f"revision:{scope['scope_key']}"
        edit = inspector_choices.get(key)
        inspector_id = int(edit.source_document_version_id) if edit and edit.source_document_version_id else None
        choice_id = inspector_id or scope.get("system_choice_id")
        candidate_ids = [int(item["document_version_id"]) for item in scope["candidates"]]
        if choice_id is None:
            reason = {
                "reason": "revision_clarification_required",
                "basis": scope.get("basis") or "неоднозначная редакция",
                "scope_key": scope["scope_key"],
            }
            for document_id in candidate_ids:
                if document_id in by_id and document_id not in excluded:
                    blocked[document_id] = reason
        else:
            for document_id in candidate_ids:
                if document_id != int(choice_id):
                    excluded.add(document_id)
                    blocked.pop(document_id, None)
            if (int(choice_id) in blocked and inspector_id
                    and blocked[int(choice_id)].get("reason") == "revision_clarification_required"):
                # An inspector's justified choice resolves ambiguity, but a source marked SUPERSEDED/CANCELLED is
                # never made eligible by a manual override.
                doc = by_id.get(int(choice_id))
                if doc and _approval(doc) not in FORBIDDEN_STATUSES:
                    blocked.pop(int(choice_id), None)

    return {"excluded_document_ids": excluded, "blocked_by_document": blocked, "scopes": scopes}


def blocking_for_param(param: Param, docs: list[DocumentVersion], selection: dict[str, Any]) -> dict[str, Any] | None:
    """Unresolved registry/edition conditions relevant to this parameter's stage and catalog section."""
    from .anchor_search import source_hints

    hints = source_hints(param)
    blocks = []
    blocked_by_document = selection.get("blocked_by_document") or {}
    for doc in docs:
        document_id = int(doc.id)
        issue = blocked_by_document.get(document_id)
        if not issue or document_id in (selection.get("excluded_document_ids") or set()):
            continue
        raw_stage = str(doc.dataset_stage or "").upper()
        stage = raw_stage if raw_stage in _STAGES else _INTERNAL_STAGE.get(str(doc.doc_stage or doc.document_stage or ""))
        if stage not in _STAGES or not bool(getattr(param, _STAGE_SOURCE[stage], False)):
            continue
        needed = df.hint_tags(hints.get(stage, ""))
        fact = df.facts_from_document(doc)
        if needed and fact.tags and not fact.tags.intersection(needed):
            continue
        blocks.append({"document_version_id": document_id, "file_id": doc.dataset_file_id,
                       "stage": stage, "reason": issue.get("reason"), "basis": issue.get("basis"),
                       "scope_key": issue.get("scope_key")})
    if not blocks:
        return None
    return {
        "reason": "CLARIFICATION_REQUIRED",
        "basis": "; ".join(sorted({str(row.get("basis") or "неопределённый источник") for row in blocks})),
        "documents": blocks,
    }


def apply_inspector_revision_choice(db: Session, process, choice: dict[str, Any]) -> dict[str, Any]:
    """Accept an S5 choice and queue a new CASE10 decision version for the affected document scope."""
    from .v3_jobs import enqueue_process_job, publish_job_message
    from .v3_pipeline import impact_scope_for_documents

    document_id = int((choice.get("chosen") or {}).get("document_version_id") or 0)
    chosen = db.get(DocumentVersion, document_id)
    if chosen is None or int(chosen.project_id) != int(process.project_id) or int(chosen.organization_id) != int(process.organization_id):
        raise HTTPException(status_code=422, detail="The chosen document is not part of this process")
    if _approval(chosen) in FORBIDDEN_STATUSES:
        raise HTTPException(status_code=422, detail="SUPERSEDED/CANCELLED documents cannot be selected as evidence")
    scope_key = str(choice.get("scope_key") or "")
    scope = next((item for item in revision_scopes(db, process) if item["scope_key"] == scope_key), None)
    if scope is None or document_id not in {int(row["document_version_id"]) for row in scope["candidates"]}:
        raise HTTPException(status_code=422, detail="The chosen document is not a candidate edition of this scope")
    ids = {document_id, *(int(row.get("document_version_id") or 0) for row in choice.get("rejected", []))}
    documents = [doc for doc_id in sorted(ids) if (doc := db.get(DocumentVersion, doc_id)) is not None]
    impact = impact_scope_for_documents(db, process, documents)
    affected = list(impact.get("param_codes") or [])
    edit = db.get(InspectorEdit, int(choice.get("inspector_edit_id") or 0))
    job = enqueue_process_job(db, process, user_id=int(edit.user_id) if edit and edit.user_id is not None else None,
                             affected_param_codes=affected or None,
                             reason="inspector_revision_choice", impact_scope=impact)
    db.commit()
    publish_job_message(job)
    return {"applied": True, "status": "RECOMPUTE_QUEUED", "job_id": job.id,
            "selected_document_version_id": document_id, "affected_param_codes": affected}


def completeness_report(db: Session, process) -> dict[str, Any]:
    """Compare the uploaded section inventory with the 1.1 source matrix and registry disciplines."""
    from .anchor_search import source_hints
    from .v3_pipeline import list_active_params

    docs = _docs(db, process)
    package_report = _process_registry_report(db, process)
    selection = selection_context(db, process, docs)
    params = list_active_params(db, organization_id=int(process.organization_id), project_id=int(process.project_id),
                                matrix_version=process.matrix_version)
    expected: dict[tuple[str, str], dict[str, Any]] = {}
    for param in params:
        hints = source_hints(param)
        for stage in _STAGES:
            if not bool(getattr(param, _STAGE_SOURCE[stage], False)):
                continue
            tags = df.hint_tags(hints.get(stage, ""))
            if not tags:
                section = str(param.section or "").strip()
                canonical = df.canonical_manifest_section(section)
                tags = df.expand_tags((canonical,)) if canonical else frozenset()
            for token in tags:
                key = (stage, str(token).upper())
                row = expected.setdefault(key, {"count": 0, "basis": set()})
                row["count"] += 1
                row["basis"].add(f"матрица 1.1: source_{stage.lower()} ({param.code})")

    for doc in docs:
        if str(doc.dataset_split or "").upper() != PACKAGE_SPLIT:
            continue
        manifest = _manifest(doc)
        registry = manifest.get("registry") if isinstance(manifest.get("registry"), dict) else {}
        discipline = str(registry.get("discipline") or doc.discipline or doc.dataset_section or "").strip()
        stage = str(registry.get("doc_stage") or doc.dataset_stage or "").upper()
        token = df.canonical_manifest_section(discipline) or discipline.upper()
        if stage in _STAGES and token:
            row = expected.setdefault((stage, token), {"count": 0, "basis": set()})
            row["basis"].add("Перечень ИД 1.1: discipline + doc_stage из реестра")

    # When the imported package report lists a registry row without a matching file, keep it visible as an expected
    # missing deliverable instead of counting only files that happened to upload.
    missing_registry_rows = (package_report or {}).get("registry_rows_without_file") or []
    for missing in missing_registry_rows:
        stage = str(missing.get("doc_stage") or "").upper()
        row_name = str(missing.get("file_name") or missing.get("file_id") or "").strip()
        token = df.canonical_manifest_section(row_name) or "РЕЕСТР"
        if stage in _STAGES:
            row = expected.setdefault((stage, token), {"count": 0, "basis": set()})
            row["count"] += 1
            row["basis"].add("Перечень ИД 1.1: строка реестра без файла")

    facts_by_id = {int(doc.id): df.facts_from_document(doc) for doc in docs}
    found: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    uploaded: Counter[str] = Counter()
    mixed_files = 0
    for doc in docs:
        fact = facts_by_id[int(doc.id)]
        stages = [fact.stage] if fact.stage in _STAGES else (["RD", "ID"] if fact.stage == "RD_ID_MIXED" else [])
        if fact.stage == "RD_ID_MIXED":
            mixed_files += 1
        for stage in stages:
            uploaded[stage] += 1
            if int(doc.id) in selection["excluded_document_ids"]:
                continue
            for (expected_stage, token), requirement in expected.items():
                if expected_stage == stage and (token in fact.tags or any(item.startswith(token) for item in fact.tags)):
                    found[(stage, token)].append({
                        "document_version_id": int(doc.id), "file_id": doc.dataset_file_id, "file": doc.filename,
                        "approval_status": _approval(doc), "approval_date": doc.approval_date.isoformat() if doc.approval_date else None,
                        "selected": not bool(selection["blocked_by_document"].get(int(doc.id))),
                    })

    rows = []
    for (stage, token), requirement in sorted(expected.items(), key=lambda item: (_STAGES.index(item[0][0]), item[0][1])):
        files = found.get((stage, token), [])
        relevant_docs = [doc for doc in docs if facts_by_id[int(doc.id)].stage in {stage, "RD_ID_MIXED"}
                         and (token in facts_by_id[int(doc.id)].tags or not facts_by_id[int(doc.id)].tags)]
        blocked = [selection["blocked_by_document"][int(doc.id)] for doc in relevant_docs
                   if int(doc.id) in selection["blocked_by_document"]]
        mixed_only = bool(files) and all(facts_by_id[int(row["document_version_id"])].stage == "RD_ID_MIXED" for row in files)
        if blocked:
            status = "CLARIFICATION_REQUIRED"
            uncertainty = "; ".join(sorted({str(item.get("basis") or "неопределённость редакции") for item in blocked}))
        elif not files:
            status = "MISSING_EVIDENCE"
            uncertainty = "строка реестра без загруженного файла" if any("строка реестра" in item for item in requirement["basis"]) else None
        elif mixed_only:
            status = "UNCERTAIN"
            uncertainty = "файлы помечены как смешанная стадия РД/ИД"
        else:
            status = "UPLOADED"
            uncertainty = None
        rows.append({
            "stage": stage,
            "section": token,
            "section_token": token,
            "expected": True,
            "expected_basis": f"{'; '.join(sorted(requirement['basis']))}; параметров: {requirement['count']}",
            "uploaded": len(files),
            "found": len(files),
            "files": files[:50],
            "status": status,
            "uncertainty": uncertainty,
        })

    missing = [row for row in rows if row["status"] == "MISSING_EVIDENCE"]
    unresolved = [row for row in rows if row["status"] in {"CLARIFICATION_REQUIRED", "UNCERTAIN"}]
    registry_present = bool((package_report or {}).get("registry_sha256")) or any(
        bool(_manifest(doc).get("registry")) for doc in docs if str(doc.dataset_split or "").upper() == PACKAGE_SPLIT
    )
    if not registry_present or unresolved:
        status = "CLARIFICATION_REQUIRED"
    elif missing:
        status = "INCOMPLETE"
    else:
        status = "COMPLETE"
    stages = [{
        "stage": stage,
        "uploaded_files": int(uploaded[stage]),
        "expected_sections": sum(1 for row in rows if row["stage"] == stage and row["expected"]),
        "found_sections": sum(1 for row in rows if row["stage"] == stage and row["status"] == "UPLOADED"),
    } for stage in _STAGES]
    return {
        "process_id": process.id,
        "source": "file_registry",
        "registry_present": registry_present,
        "registry_status": (package_report or {}).get("registry_status"),
        "status": status,
        "basis": ("Ожидания: поля source_pd/source_rd/source_id матрицы 1.1; разделы ИД сверены по discipline и doc_stage "
                  "реестра Перечня ИД 1.1. Найдено: загруженные документы с совпавшей стадией и дисциплиной; "
                  "SUPERSEDED/CANCELLED исключены из выбора и доказательств. "
                  + ("Реестр Перечня ИД 1.1 не загружен или не подтверждён." if not registry_present else "")),
        "stages": stages,
        "mixed_stage_files": mixed_files,
        "excluded_files": len(selection["excluded_document_ids"]),
        "rows": rows,
    }
