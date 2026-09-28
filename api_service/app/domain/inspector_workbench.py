"""Phase 12 / S5: backend of the inspector workbench (expert session §13-17, §19, §35).

What lives here
  * Versioned evidence edits (§13). An inspector adds, removes, refines or restores evidence fragments; every change
    is a new `InspectorEdit` row (user_id, timestamp, reason, reference to the source entity, previous value). The
    machine output (`EvidenceFragment`), source fragments/OCR and the original files are never written to: the
    effective evidence is the machine output with the edits replayed on top (`effective_fragments`).
  * Inspector revision choice with justification (§12-13). The decision rules for sources belong to S3 (file registry,
    Перечень ИД 1.1); until S3 ships them `revision_scopes` derives the scopes from the existing revision analysis
    (`document_facts` via `comparison_gate.GateContext`) and the choice is recorded here. Contract with S3: if
    `app.domain.file_registry` exposes `revision_scopes(db, process)` / `apply_inspector_revision_choice(db, process,
    choice)` / `completeness_report(db, process)`, they are used instead of the stubs below (`_s3_hook`).
  * Bulk decisions on same-type candidates (§17): one parameter code per batch, a shared comment is mandatory, a bulk
    rejection needs an explicit confirmation flag.
  * Verification time (§35): from the first opening of the protocol to its finalization, written to the audit log.
  * The data behind the single candidate window (§14-15): card fields, per-stage panels, low-confidence/LOW_QUALITY
    flags, page geometry/rendering for client-side bbox overlays.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from functools import lru_cache
import hashlib
from io import BytesIO
import importlib
import os
from pathlib import Path
import re
from typing import Any, Callable, Iterable

from fastapi import HTTPException
from sqlalchemy.orm import Session, selectinload

from ..db.models import (
    AuditLog,
    DocumentVersion,
    EvidenceFragment,
    EvidenceGroup,
    InspectionProcess,
    InspectorEdit,
    Param,
    Protocol,
)
from . import document_facts as df
from . import matrix_v11
from .comparison_gate import GateContext
from .dataset_sources import original_document_bytes
from .v3_pipeline import (
    DECISION_TO_STATUS,
    PROCESS_FINALIZED,
    STATUS_CANDIDATE,
    STATUS_SUSPICION,
    _project_documents,
    add_audit,
    evidence_fragment_to_dict,
    evidence_group_to_dict,
    list_active_params,
    record_inspector_decision,
    validate_finding_transition,
)
from evaluation.exporter import evidence_group_to_submission_check

ENTITY_FRAGMENT = "EVIDENCE_FRAGMENT"
ENTITY_REVISION = "REVISION_CHOICE"
ACTION_ADD = "ADD"
ACTION_REFINE = "REFINE"
ACTION_REMOVE = "REMOVE"
ACTION_RESTORE = "RESTORE"
ACTION_CHOOSE = "CHOOSE"

ORIGIN_MACHINE = "MACHINE"
ORIGIN_INSPECTOR = "INSPECTOR"
FRAGMENT_ACTIVE = "ACTIVE"
FRAGMENT_REMOVED = "REMOVED"
FRAGMENT_ORPHANED = "ORPHANED"  # the machine fragment an edit refers to was recomputed away

FRAGMENT_ROLES = ("expected", "actual", "context")
STAGES = ("PD", "RD", "ID")
INTERNAL_TO_STAGE = {"project": "PD", "working": "RD", "as_built": "ID"}
# Fields of an evidence fragment an inspector can change; everything else (file identity, hash, code, revision,
# approval status) follows from the document and is never typed in by hand.
EDITABLE_FIELDS = ("page", "bbox_norm", "extracted_value", "role", "note")
SNAPSHOT_FIELDS = (
    "document_version_id", "file_id", "file", "file_sha256", "stage", "dataset_stage", "document_code", "revision",
    "approval_status", "page", "bbox_norm", "bbox_pdf", "extracted_value", "role", "note", "context", "extractor",
    "confidence",
)

MIN_REASON_CHARS = 3
MAX_BULK_GROUPS = 200
LOW_CONFIDENCE_ENV = "CASE10_LOW_CONFIDENCE_THRESHOLD"
DEFAULT_LOW_CONFIDENCE = 0.75
LOW_QUALITY = "LOW_QUALITY"
QUALITY_KEYS = ("quality_status", "quality_flag", "page_quality")

VERIFICATION_OPENED = "VERIFICATION_OPENED"
VERIFICATION_TIME_MEASURED = "VERIFICATION_TIME_MEASURED"
DECISION_UI_METRICS = "DECISION_UI_METRICS"
BULK_DECISION = "BULK_DECISION"
EDIT_AUDIT_ACTIONS = ("EVIDENCE_FRAGMENT_ADDED", "EVIDENCE_FRAGMENT_REFINED", "EVIDENCE_FRAGMENT_REMOVED",
                      "EVIDENCE_FRAGMENT_RESTORED", "REVISION_CHOSEN_BY_INSPECTOR")


# ---------------------------------------------------------------------------------------------------------------
# S3 contract
# ---------------------------------------------------------------------------------------------------------------
def _s3_hook(name: str) -> Callable[..., Any] | None:
    """A function of S3's `file_registry` module, or None while S3 has not shipped it (then the stub here runs)."""
    try:
        module = importlib.import_module(f"{__package__}.file_registry")
    except ImportError:
        return None
    hook = getattr(module, name, None)
    return hook if callable(hook) else None


# ---------------------------------------------------------------------------------------------------------------
# guards
# ---------------------------------------------------------------------------------------------------------------
def require_reason(reason: object, *, what: str = "reason") -> str:
    text = str(reason or "").strip()
    if len(text) < MIN_REASON_CHARS:
        raise HTTPException(status_code=422, detail=f"A {what} of at least {MIN_REASON_CHARS} characters is required")
    return text


def require_open_process(process: InspectionProcess) -> None:
    if process.status == PROCESS_FINALIZED:
        raise HTTPException(status_code=409, detail="Finalized protocol cannot be changed")


def low_confidence_threshold() -> float:
    try:
        return float(os.getenv(LOW_CONFIDENCE_ENV, str(DEFAULT_LOW_CONFIDENCE)))
    except ValueError:
        return DEFAULT_LOW_CONFIDENCE


# ---------------------------------------------------------------------------------------------------------------
# documents: bytes, page geometry, rendering (client-side bbox overlays, "go to source")
# ---------------------------------------------------------------------------------------------------------------
ORIGINALS_ROOTS_ENV = "CASE10_ORIGINALS_ROOTS"


def _sha_ok(data: bytes, expected: object) -> bool:
    return bool(expected) and hashlib.sha256(data).hexdigest() == str(expected)


def document_bytes(document: DocumentVersion) -> bytes:
    """Original bytes of a document, always SHA-256-checked against the recorded hash, never a converted copy:
    the dataset original (`original_document_bytes`: CASE10_ORIGINALS_ROOT or the mounted package), else the same
    relative path under any of `CASE10_ORIGINALS_ROOTS` (os.pathsep-separated; several objects extracted to
    different places, e.g. a demo stand), else an uploaded file on disk."""
    expected = document.file_hash or document.content_hash
    try:
        return original_document_bytes(document)
    except (FileNotFoundError, ValueError, KeyError):
        pass
    meta = document.dataset_metadata or {}
    row = meta.get("document_manifest") or meta.get("files_index") or {}
    relative = str(row.get("relative_path") or row.get("source_relative_path") or "").replace("\\", "/")
    if relative and ".." not in Path(relative).parts and ":" not in relative and not relative.startswith("/"):
        for root in filter(None, os.environ.get(ORIGINALS_ROOTS_ENV, "").split(os.pathsep)):
            base = Path(root).resolve()
            source = (base / relative).resolve()
            if source.is_relative_to(base) and source.is_file():
                data = source.read_bytes()
                if _sha_ok(data, expected):
                    return data
    path = Path(str(document.file_path or ""))
    if document.file_path and path.is_file():
        data = path.read_bytes()
        if expected and not _sha_ok(data, expected):
            raise ValueError("Stored file does not match the recorded SHA-256")
        return data
    raise FileNotFoundError("Original document is not available")


def _document_cache_key(document: DocumentVersion) -> tuple[int, str]:
    return int(document.id), str(document.file_hash or document.content_hash or document.file_path or "")


@lru_cache(maxsize=512)
def _page_geometry_cached(key: tuple[int, str], page: int, loader: Callable[[], bytes]) -> dict[str, Any]:
    import fitz

    with fitz.open(stream=loader(), filetype="pdf") as pdf:
        if page < 1 or page > len(pdf):
            raise ValueError("Page is outside the document")
        rect = pdf[page - 1].rect  # visible frame (after /Rotate), the frame bbox_pdf and the renderer use
        return {"page": page, "page_count": len(pdf), "width": float(rect.width), "height": float(rect.height),
                "rotation": int(pdf[page - 1].rotation)}


def page_geometry(document: DocumentVersion, page: int) -> dict[str, Any]:
    key = _document_cache_key(document)
    return dict(_page_geometry_cached(key, int(page), _Loader(document)))


class _Loader:
    """Hashable-by-key wrapper so `lru_cache` keys on the document identity, not on the ORM object."""

    def __init__(self, document: DocumentVersion):
        self.document = document
        self.key = _document_cache_key(document)

    def __call__(self) -> bytes:
        return document_bytes(self.document)

    def __hash__(self) -> int:
        return hash(self.key)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Loader) and other.key == self.key


@lru_cache(maxsize=48)
def _render_page_cached(key: tuple[int, str], page: int, max_side: int, loader: _Loader) -> bytes:
    import fitz

    with fitz.open(stream=loader(), filetype="pdf") as pdf:
        if page < 1 or page > len(pdf):
            raise ValueError("Page is outside the document")
        target = pdf[page - 1]
        scale = min(3.0, max_side / max(target.rect.width, target.rect.height))
        return target.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False, annots=False).tobytes("png")


def render_page_png(document: DocumentVersion, page: int, *, max_side: int = 1800) -> bytes:
    """The page as the inspector sees it in a PDF viewer (native rotation), without any highlight: overlays are
    drawn by the client from `bbox_norm` in the same visible frame."""
    max_side = max(600, min(int(max_side), 3200))
    return _render_page_cached(_document_cache_key(document), int(page), max_side, _Loader(document))


def extract_page_pdf(document: DocumentVersion, page: int) -> bytes:
    """"Go to source": the original page itself (vector content, text layer) cut out of the original PDF."""
    import fitz

    with fitz.open(stream=document_bytes(document), filetype="pdf") as pdf:
        if page < 1 or page > len(pdf):
            raise ValueError("Page is outside the document")
        out = fitz.open()
        out.insert_pdf(pdf, from_page=page - 1, to_page=page - 1)
        buffer = BytesIO(out.tobytes(garbage=3, deflate=True))
        out.close()
        return buffer.getvalue()


def _view_bbox(fragment: dict[str, Any], document: DocumentVersion | None) -> tuple[list[float] | None, str]:
    """bbox of a fragment normalized to the rendered page (visible frame). bbox_pdf / visible page size is exact;
    the stored normalized bbox is the fallback (its frame is not guaranteed -- reported as approximate)."""
    bbox_pdf = fragment.get("bbox_pdf")
    page = fragment.get("page")
    if document is not None and page and isinstance(bbox_pdf, list) and len(bbox_pdf) == 4:
        try:
            geometry = page_geometry(document, int(page))
        except (FileNotFoundError, ValueError, KeyError, RuntimeError):
            geometry = None
        if geometry and geometry["width"] > 0 and geometry["height"] > 0:
            w, h = geometry["width"], geometry["height"]
            x1, y1, x2, y2 = (float(v) for v in bbox_pdf)
            return [_clip01(x1 / w), _clip01(y1 / h), _clip01(x2 / w), _clip01(y2 / h)], "EXACT"
    bbox = fragment.get("bbox_norm") or fragment.get("bbox_normalized") or fragment.get("bbox")
    if isinstance(bbox, list) and len(bbox) == 4 and all(isinstance(v, (int, float)) for v in bbox):
        if all(0 <= float(v) <= 1.0001 for v in bbox):
            return [float(v) for v in bbox], "APPROXIMATE"
    return None, "NONE"


def _clip01(value: float) -> float:
    return round(min(1.0, max(0.0, value)), 5)


def validate_bbox_norm(bbox: object) -> list[float]:
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        raise HTTPException(status_code=422, detail="bbox_norm must be [x1, y1, x2, y2] in [0;1]")
    try:
        x1, y1, x2, y2 = (float(v) for v in bbox)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="bbox_norm must be numeric") from exc
    if not (0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1):
        raise HTTPException(status_code=422, detail="bbox_norm must satisfy 0 <= x1 < x2 <= 1 and 0 <= y1 < y2 <= 1")
    return [round(x1, 5), round(y1, 5), round(x2, 5), round(y2, 5)]


# ---------------------------------------------------------------------------------------------------------------
# versioned evidence
# ---------------------------------------------------------------------------------------------------------------
def machine_key(fragment_id: int) -> str:
    return f"machine:{int(fragment_id)}"


def _stage_of(fragment: dict[str, Any]) -> str:
    stage = str(fragment.get("dataset_stage") or "").upper()
    if stage in STAGES:
        return stage
    return INTERNAL_TO_STAGE.get(str(fragment.get("stage") or ""), "UNKNOWN")


def _machine_snapshot(fragment: dict[str, Any]) -> dict[str, Any]:
    snapshot = {key: fragment.get(key) for key in SNAPSHOT_FIELDS}
    snapshot["bbox_norm"] = fragment.get("bbox_normalized") or fragment.get("bbox")
    snapshot["dataset_stage"] = _stage_of(fragment)
    return snapshot


def _document_snapshot(document: DocumentVersion) -> dict[str, Any]:
    stage = str(document.dataset_stage or "").upper()
    if stage not in STAGES:
        stage = INTERNAL_TO_STAGE.get(str(document.doc_stage or document.document_stage or ""), "UNKNOWN")
    return {
        "document_version_id": int(document.id),
        "file_id": document.dataset_file_id,
        "file": document.filename,
        "file_sha256": document.file_hash or document.content_hash,
        "stage": document.doc_stage or document.document_stage,
        "dataset_stage": stage,
        "document_code": document.document_code,
        "revision": document.revision,
        "approval_status": document.approval_status,
    }


def _edits_for_group(db: Session, group_id: int) -> list[InspectorEdit]:
    return (
        db.query(InspectorEdit)
        .filter(InspectorEdit.evidence_group_id == int(group_id), InspectorEdit.entity_type == ENTITY_FRAGMENT)
        .order_by(InspectorEdit.id.asc())
        .all()
    )


def edit_to_dict(edit: InspectorEdit) -> dict[str, Any]:
    user = edit.user
    return {
        "id": int(edit.id),
        "entity_type": edit.entity_type,
        "entity_key": edit.entity_key,
        "action": edit.action,
        "version": int(edit.version),
        "evidence_group_id": edit.evidence_group_id,
        "source_fragment_id": edit.source_fragment_id,
        "source_document_version_id": edit.source_document_version_id,
        "previous_edit_id": edit.previous_edit_id,
        "previous_value": edit.previous_value,
        "new_value": edit.new_value,
        "reason": edit.reason,
        "user_id": edit.user_id,
        "user_login": getattr(user, "login", None),
        "created_at": edit.created_at,
    }


def _replay(state: dict[str, Any] | None, status: str, edit: InspectorEdit) -> tuple[dict[str, Any] | None, str]:
    if edit.action in (ACTION_ADD, ACTION_REFINE):
        return dict(edit.new_value or {}), FRAGMENT_ACTIVE
    if edit.action == ACTION_REMOVE:
        return state, FRAGMENT_REMOVED
    if edit.action == ACTION_RESTORE:
        return (dict(edit.new_value) if edit.new_value else state), FRAGMENT_ACTIVE
    return state, status


def effective_fragments(db: Session, group: EvidenceGroup, *, with_view: bool = True) -> list[dict[str, Any]]:
    """Machine fragments (never modified) with the inspector's versions replayed on top, plus inspector-added ones.
    Each item: key, origin, status, version, `current` (what the inspector sees now), `machine` (the untouched
    machine output, None for an added fragment), `view_bbox` (normalized to the rendered page)."""
    machine_rows = (
        db.query(EvidenceFragment)
        .filter(EvidenceFragment.evidence_group_id == int(group.id))
        .order_by(EvidenceFragment.role.asc(), EvidenceFragment.id.asc())
        .all()
    )
    by_key: dict[str, list[InspectorEdit]] = defaultdict(list)
    for edit in _edits_for_group(db, int(group.id)):
        by_key[edit.entity_key].append(edit)
    documents: dict[int, DocumentVersion | None] = {}

    def doc_for(doc_id: object) -> DocumentVersion | None:
        if not doc_id:
            return None
        key = int(doc_id)
        if key not in documents:
            documents[key] = db.get(DocumentVersion, key)
        return documents[key]

    out: list[dict[str, Any]] = []
    for row in machine_rows:
        machine = _machine_snapshot(evidence_fragment_to_dict(row))
        key = machine_key(int(row.id))
        state, status = dict(machine), FRAGMENT_ACTIVE
        history = by_key.pop(key, [])
        for edit in history:
            state, status = _replay(state, status, edit)
        out.append(_effective_item(key, ORIGIN_MACHINE, status, history, state, machine, int(row.id)))
    for key, history in by_key.items():
        state, status = None, FRAGMENT_ACTIVE
        for edit in history:
            state, status = _replay(state, status, edit)
        if key.startswith("manual:"):
            out.append(_effective_item(key, ORIGIN_INSPECTOR, status, history, state or {}, None, None))
        else:  # the machine fragment behind these edits no longer exists (recomputed): keep the history visible
            out.append(_effective_item(key, ORIGIN_MACHINE, FRAGMENT_ORPHANED, history, state or {}, None, history[0].source_fragment_id))
    if with_view:
        for item in out:
            item["view_bbox"], item["view_bbox_quality"] = _view_bbox(item["current"], doc_for(item["current"].get("document_version_id")))
    return out


def _effective_item(key: str, origin: str, status: str, history: list[InspectorEdit], current: dict[str, Any],
                    machine: dict[str, Any] | None, fragment_id: int | None) -> dict[str, Any]:
    last = history[-1] if history else None
    return {
        "key": key,
        "origin": origin,
        "status": status,
        "version": int(last.version) if last else 0,
        "edited": bool(history),
        "fragment_id": fragment_id,
        "stage": _stage_of(current),
        "current": current,
        "machine": machine,
        "last_edit": edit_to_dict(last) if last else None,
    }


def _next_version(db: Session, process_id: str, entity_key: str) -> tuple[int, InspectorEdit | None]:
    last = (
        db.query(InspectorEdit)
        .filter(InspectorEdit.process_id == str(process_id), InspectorEdit.entity_key == entity_key)
        .order_by(InspectorEdit.version.desc())
        .first()
    )
    return (int(last.version) + 1 if last else 1), last


def _find_effective(db: Session, group: EvidenceGroup, key: str) -> dict[str, Any]:
    for item in effective_fragments(db, group, with_view=False):
        if item["key"] == key:
            return item
    raise HTTPException(status_code=404, detail="Evidence fragment not found in this evidence group")


def _record_edit(db: Session, *, process: InspectionProcess, group: EvidenceGroup | None, entity_type: str, entity_key: str,
                 action: str, previous_value: dict[str, Any] | None, new_value: dict[str, Any] | None, reason: str,
                 user_id: int | None, source_fragment_id: int | None = None,
                 source_document_version_id: int | None = None, audit_action: str | None = None,
                 entity_key_from_id: str | None = None) -> InspectorEdit:
    """One new version of one entity + its audit entry. `entity_key_from_id` names a NEW entity after the row's own
    id ("manual:<id>"), so an added fragment's key is stable and unique without a separate sequence."""
    version, previous = _next_version(db, str(process.id), entity_key)
    edit = InspectorEdit(
        organization_id=int(process.organization_id),
        project_id=int(process.project_id),
        process_id=str(process.id),
        object_id=(group.object_id if group is not None else None) or process.object_id,
        evidence_group_id=int(group.id) if group is not None else None,
        entity_type=entity_type,
        entity_key=entity_key,
        action=action,
        version=version,
        source_fragment_id=source_fragment_id,
        source_document_version_id=source_document_version_id,
        previous_edit_id=int(previous.id) if previous else None,
        previous_value=previous_value,
        new_value=new_value,
        reason=reason,
        user_id=user_id,
    )
    db.add(edit)
    db.flush()
    if entity_key_from_id:
        edit.entity_key = f"{entity_key_from_id}{int(edit.id)}"
        entity_key = edit.entity_key
        db.add(edit)
        db.flush()
    add_audit(
        db,
        action=audit_action or f"{entity_type}_{action}",
        user_id=user_id,
        process=process,
        object_id=edit.object_id,
        details={"inspector_edit_id": int(edit.id), "entity_key": entity_key, "version": version,
                 "evidence_group_id": edit.evidence_group_id, "reason": reason},
    )
    return edit


def _group_process(db: Session, group: EvidenceGroup) -> InspectionProcess:
    process = db.get(InspectionProcess, str(group.process_id))
    if process is None:
        raise HTTPException(status_code=404, detail="Inspection process not found")
    return process


def _checked_page(document: DocumentVersion, page: object) -> int:
    try:
        number = int(page)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="page must be a positive integer") from exc
    if number < 1:
        raise HTTPException(status_code=422, detail="page must be a positive integer")
    try:
        geometry = page_geometry(document, number)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (FileNotFoundError, KeyError, RuntimeError):
        return number  # original not readable here: keep the page as entered, geometry-dependent fields stay empty
    if number > geometry["page_count"]:
        raise HTTPException(status_code=422, detail="Page is outside the document")
    return number


def _bbox_pdf_for(document: DocumentVersion, page: int, bbox_norm: list[float] | None) -> list[float] | None:
    if not bbox_norm:
        return None
    try:
        geometry = page_geometry(document, page)
    except (FileNotFoundError, ValueError, KeyError, RuntimeError):
        return None
    w, h = geometry["width"], geometry["height"]
    return [round(bbox_norm[0] * w, 2), round(bbox_norm[1] * h, 2), round(bbox_norm[2] * w, 2), round(bbox_norm[3] * h, 2)]


def add_fragment(db: Session, group: EvidenceGroup, *, document_version_id: int, page: object, bbox_norm: object,
                 extracted_value: str | None, role: str | None, note: str | None, reason: object,
                 user_id: int | None) -> InspectorEdit:
    process = _group_process(db, group)
    require_open_process(process)
    reason_text = require_reason(reason)
    document = db.get(DocumentVersion, int(document_version_id))
    if document is None or int(document.project_id) != int(group.project_id) or int(document.organization_id) != int(group.organization_id):
        raise HTTPException(status_code=404, detail="Document not found in this project")
    number = _checked_page(document, page)
    bbox = validate_bbox_norm(bbox_norm) if bbox_norm is not None else None
    role_value = str(role or "context")
    if role_value not in FRAGMENT_ROLES:
        raise HTTPException(status_code=422, detail=f"role must be one of {', '.join(FRAGMENT_ROLES)}")
    state = {
        **_document_snapshot(document),
        "page": number,
        "bbox_norm": bbox,
        "bbox_pdf": _bbox_pdf_for(document, number, bbox),
        "extracted_value": (str(extracted_value).strip() or None) if extracted_value is not None else None,
        "role": role_value,
        "note": (str(note).strip() or None) if note is not None else None,
        "context": None,
        "extractor": "inspector",
        "confidence": None,
    }
    # A new entity: its key is taken from the ADD row's own id (a unique placeholder until the row has one).
    placeholder = f"manual:new:{group.id}:{datetime.utcnow().timestamp()}"
    edit = _record_edit(db, process=process, group=group, entity_type=ENTITY_FRAGMENT, entity_key=placeholder,
                        action=ACTION_ADD, previous_value=None, new_value=state, reason=reason_text, user_id=user_id,
                        source_document_version_id=int(document.id), audit_action="EVIDENCE_FRAGMENT_ADDED",
                        entity_key_from_id="manual:")
    db.commit()
    db.refresh(edit)
    return edit


def refine_fragment(db: Session, group: EvidenceGroup, key: str, *, changes: dict[str, Any], reason: object,
                    user_id: int | None) -> InspectorEdit:
    process = _group_process(db, group)
    require_open_process(process)
    reason_text = require_reason(reason)
    item = _find_effective(db, group, key)
    if item["status"] != FRAGMENT_ACTIVE:
        raise HTTPException(status_code=409, detail="Only an active fragment can be refined (restore it first)")
    current = dict(item["current"])
    updated = dict(current)
    document = db.get(DocumentVersion, int(current["document_version_id"])) if current.get("document_version_id") else None
    unknown = set(changes) - set(EDITABLE_FIELDS)
    if unknown:
        raise HTTPException(status_code=422, detail=f"Not editable: {', '.join(sorted(unknown))}")
    if "page" in changes and changes["page"] is not None:
        if document is None:
            raise HTTPException(status_code=422, detail="Fragment has no document to re-page")
        updated["page"] = _checked_page(document, changes["page"])
    if "bbox_norm" in changes:
        updated["bbox_norm"] = validate_bbox_norm(changes["bbox_norm"]) if changes["bbox_norm"] is not None else None
    if "page" in changes or "bbox_norm" in changes:
        updated["bbox_pdf"] = _bbox_pdf_for(document, int(updated["page"]), updated.get("bbox_norm")) if document and updated.get("page") else None
    if "extracted_value" in changes:
        updated["extracted_value"] = (str(changes["extracted_value"]).strip() or None) if changes["extracted_value"] is not None else None
    if "role" in changes and changes["role"] is not None:
        if changes["role"] not in FRAGMENT_ROLES:
            raise HTTPException(status_code=422, detail=f"role must be one of {', '.join(FRAGMENT_ROLES)}")
        updated["role"] = changes["role"]
    if "note" in changes:
        updated["note"] = (str(changes["note"]).strip() or None) if changes["note"] is not None else None
    if updated == current:
        raise HTTPException(status_code=422, detail="Nothing changed")
    edit = _record_edit(db, process=process, group=group, entity_type=ENTITY_FRAGMENT, entity_key=key, action=ACTION_REFINE,
                        previous_value=current, new_value=updated, reason=reason_text, user_id=user_id,
                        source_fragment_id=item["fragment_id"], source_document_version_id=current.get("document_version_id"),
                        audit_action="EVIDENCE_FRAGMENT_REFINED")
    db.commit()
    db.refresh(edit)
    return edit


def set_fragment_status(db: Session, group: EvidenceGroup, key: str, *, remove: bool, reason: object,
                        user_id: int | None) -> InspectorEdit:
    process = _group_process(db, group)
    require_open_process(process)
    reason_text = require_reason(reason)
    item = _find_effective(db, group, key)
    wanted = FRAGMENT_REMOVED if remove else FRAGMENT_ACTIVE
    if item["status"] == FRAGMENT_ORPHANED:
        raise HTTPException(status_code=409, detail="The machine fragment behind this entry was recomputed; it cannot be changed")
    if item["status"] == wanted:
        raise HTTPException(status_code=409, detail=f"Fragment is already {wanted.lower()}")
    current = dict(item["current"])
    edit = _record_edit(db, process=process, group=group, entity_type=ENTITY_FRAGMENT, entity_key=key,
                        action=ACTION_REMOVE if remove else ACTION_RESTORE,
                        previous_value={**current, "status": item["status"]}, new_value={**current, "status": wanted},
                        reason=reason_text, user_id=user_id, source_fragment_id=item["fragment_id"],
                        source_document_version_id=current.get("document_version_id"),
                        audit_action="EVIDENCE_FRAGMENT_REMOVED" if remove else "EVIDENCE_FRAGMENT_RESTORED")
    db.commit()
    db.refresh(edit)
    return edit


def group_edit_history(db: Session, group: EvidenceGroup) -> list[dict[str, Any]]:
    return [edit_to_dict(edit) for edit in _edits_for_group(db, int(group.id))]


# ---------------------------------------------------------------------------------------------------------------
# quality / queue flags
# ---------------------------------------------------------------------------------------------------------------
def quality_flags(group: EvidenceGroup | dict[str, Any], fragments: Iterable[dict[str, Any]], threshold: float) -> dict[str, Any]:
    """low_confidence: group or any cited fragment below the threshold. low_quality: an explicit LOW_QUALITY flag
    set by the pipeline (S3: on fragments/pages, or as the comparability status / gate reason) -- never guessed."""
    if isinstance(group, dict):
        confidence, comparability, delta = group.get("confidence"), group.get("comparability_status"), group.get("delta")
    else:
        confidence, comparability, delta = group.confidence, group.comparability_status, group.delta
    delta = delta if isinstance(delta, dict) else {}
    reasons: list[str] = []
    for fragment in fragments:
        for key in QUALITY_KEYS:
            if str(fragment.get(key) or "").upper() == LOW_QUALITY:
                reasons.append(f"{fragment.get('file_id') or fragment.get('file') or 'файл'} стр. {fragment.get('page')}")
                break
        else:
            flags = fragment.get("quality_flags")
            if isinstance(flags, (list, tuple)) and LOW_QUALITY in {str(f).upper() for f in flags}:
                reasons.append(f"{fragment.get('file_id') or fragment.get('file') or 'файл'} стр. {fragment.get('page')}")
    if str(comparability or "").upper() == LOW_QUALITY or str(delta.get("reason") or "").upper() == LOW_QUALITY \
            or str((delta.get("gate") or {}).get("reason") if isinstance(delta.get("gate"), dict) else "").upper() == LOW_QUALITY:
        reasons.append("сравнение заблокировано: LOW_QUALITY")
    confidences = [float(c) for c in [confidence, *[f.get("confidence") for f in fragments]] if isinstance(c, (int, float))]
    min_conf = min(confidences) if confidences else None
    return {
        "low_confidence": min_conf is not None and min_conf < threshold,
        "min_confidence": min_conf,
        "low_quality": bool(reasons),
        "low_quality_reasons": sorted(set(reasons)),
    }


# ---------------------------------------------------------------------------------------------------------------
# workbench payloads
# ---------------------------------------------------------------------------------------------------------------
def _param_codes(param: Param | None, delta: dict[str, Any]) -> dict[str, Any]:
    internal = str(delta.get("parameter_code") or (param.code if param else "") or "")
    row = matrix_v11.lookup(internal)
    return {
        "internal_code": internal or None,
        "matrix_code": row["matrix_code"] if row else None,
        "legacy_code": row["legacy_code"] if row else internal or None,
        "type_key": (row["matrix_code"] if row else internal) or "—",
    }


def _inspector_status(decisions: list[Any]) -> str:
    if not decisions:
        return "PENDING"
    last = sorted(decisions, key=lambda row: row.id)[-1]
    return {"Confirm": "CONFIRMED", "Reject": "REJECTED", "Clarification Required": "CLARIFICATION_REQUIRED"}.get(last.decision, "PENDING")


def workbench_summary(db: Session, process: InspectionProcess) -> dict[str, Any]:
    """The candidate queue: one light row per evidence group (no page rendering, one query per table)."""
    groups = (
        db.query(EvidenceGroup)
        .options(selectinload(EvidenceGroup.param), selectinload(EvidenceGroup.decisions), selectinload(EvidenceGroup.canonical_entity))
        .filter(EvidenceGroup.process_id == str(process.id))
        .order_by(EvidenceGroup.review_priority.asc(), EvidenceGroup.id.asc())
        .all()
    )
    group_ids = [int(g.id) for g in groups]
    fragments_by_group: dict[int, list[dict[str, Any]]] = defaultdict(list)
    if group_ids:  # eager-load what evidence_fragment_to_dict touches: no per-fragment queries (§11: API p95 <= 200 ms)
        rows = (
            db.query(EvidenceFragment)
            .options(selectinload(EvidenceFragment.document_version), selectinload(EvidenceFragment.source_fragment))
            .filter(EvidenceFragment.evidence_group_id.in_(group_ids))
            .all()
        )
        for row in rows:
            fragments_by_group[int(row.evidence_group_id)].append(evidence_fragment_to_dict(row))
    edits_by_group: Counter = Counter()
    if group_ids:
        for (gid,) in db.query(InspectorEdit.evidence_group_id).filter(InspectorEdit.evidence_group_id.in_(group_ids)).all():
            edits_by_group[int(gid)] += 1
    threshold = low_confidence_threshold()
    items = []
    sections: Counter = Counter()
    for group in groups:
        param = group.param
        delta = group.delta if isinstance(group.delta, dict) else {}
        codes = _param_codes(param, delta)
        fragments = fragments_by_group.get(int(group.id), [])
        stages = sorted({_stage_of(f) for f in fragments} & set(STAGES), key=STAGES.index)
        section = param.section if param else None
        if section:
            sections[section] += 1
        items.append({
            "id": int(group.id),
            **codes,
            "name": param.parameter_name if param else None,
            "section": section,
            "finding_status": group.finding_status,
            "inspector_status": _inspector_status(list(group.decisions)),
            "is_suspicion": group.finding_status == STATUS_SUSPICION or delta.get("matrix_scope") == "FREE_SEARCH",
            "location": delta.get("location") or (group.canonical_entity.canonical_name if group.canonical_entity else None),
            "expected": group.expected_value,
            "actual": group.actual_value,
            "confidence": group.confidence,
            "review_priority": group.review_priority,
            "comparability_status": group.comparability_status,
            "reason": delta.get("reason"),
            "needs_reverification": bool(group.needs_reverification),
            "stages": stages,
            "fragments": len(fragments),
            "edits": int(edits_by_group.get(int(group.id), 0)),
            **quality_flags(group, fragments, threshold),
        })
    return {
        "process_id": process.id,
        "object_id": process.object_id,
        "process_status": process.status,
        "low_confidence_threshold": threshold,
        "sections": [{"section": key, "count": value} for key, value in sorted(sections.items())],
        "counts": dict(Counter(item["finding_status"] for item in items)),
        "items": items,
    }


def workbench_group(db: Session, group: EvidenceGroup) -> dict[str, Any]:
    """Everything the single candidate window shows: the §14 card, the ПД/РД/ИД panels (effective evidence with
    page-normalized boxes), the decision and edit history."""
    base = evidence_group_to_dict(db, group, include_fragments=True, include_inspector_evidence=False)
    check = evidence_group_to_submission_check(base)
    param = group.param
    delta = group.delta if isinstance(group.delta, dict) else {}
    codes = _param_codes(param, delta)
    effective = effective_fragments(db, group)
    decisions = base.get("decisions") or []
    extractors = sorted({str(f["current"].get("extractor")) for f in effective if f["current"].get("extractor")})
    card = {
        "id": int(group.id),
        "evidence_group_id": check.get("evidence_group_id"),
        "finding_id": check.get("finding_id"),
        "object_id": group.object_id,
        **codes,
        "parameter_code": check.get("parameter_code"),
        "parameter_name": param.parameter_name if param else None,
        "section": param.section if param else None,
        "unit": param.unit if param else None,
        "rule": {
            "source": delta.get("source"),
            "rule_version": check.get("rule_version"),
            "trigger_logic": param.trigger_logic if param else None,
            "comparison_scenario": group.comparison_scenario,
            "extractors": extractors,
        },
        "location": check.get("location"),
        "expected_value": check.get("expected_value"),
        "expected_stage": check.get("expected_stage"),
        "actual_value": check.get("actual_value"),
        "actual_stage": check.get("actual_stage"),
        "values_by_stage": {"PD": check.get("pd_value"), "RD": check.get("rd_value"), "ID": check.get("id_value")},
        "source_expected": {k.replace("source_expected_", ""): v for k, v in check.items() if k.startswith("source_expected_")},
        "source_actual": {k.replace("source_actual_", ""): v for k, v in check.items() if k.startswith("source_actual_")},
        "delta": delta,
        "confidence": group.confidence,
        "review_priority": check.get("review_priority"),
        "finding_status": group.finding_status,
        "violation_label": check.get("violation_label"),
        "protocol_status": check.get("protocol_status"),
        "comparability_status": group.comparability_status,
        "completeness_status": check.get("completeness_status"),
        "inspector_status": base.get("inspector_status"),
        "last_decision": decisions[-1] if decisions else None,
        "needs_reverification": bool(group.needs_reverification),
    }
    panels = {stage: [item for item in effective if item["stage"] == stage] for stage in STAGES}
    return {
        **base,
        "card": card,
        "effective_fragments": effective,
        "panels": panels,
        "visible_stages": [stage for stage in STAGES if any(item["status"] != FRAGMENT_ORPHANED for item in panels[stage])],
        "edits": group_edit_history(db, group),
        "quality": quality_flags(group, [item["current"] for item in effective], low_confidence_threshold()),
    }


# ---------------------------------------------------------------------------------------------------------------
# bulk decisions (§17)
# ---------------------------------------------------------------------------------------------------------------
def bulk_decide(db: Session, *, group_ids: list[int], organization_id: int, decision: str, reason_code: str | None,
                comment: object, confirm_bulk_reject: bool, user_id: int | None) -> dict[str, Any]:
    ids = list(dict.fromkeys(int(g) for g in group_ids))
    if not ids:
        raise HTTPException(status_code=422, detail="No evidence groups selected")
    if len(ids) > MAX_BULK_GROUPS:
        raise HTTPException(status_code=422, detail=f"At most {MAX_BULK_GROUPS} evidence groups per bulk operation")
    if decision not in DECISION_TO_STATUS:
        raise HTTPException(status_code=400, detail="Unsupported inspector decision")
    comment_text = require_reason(comment, what="shared comment")
    groups = [db.get(EvidenceGroup, gid) for gid in ids]
    if any(g is None or int(g.organization_id) != int(organization_id) for g in groups):
        raise HTTPException(status_code=404, detail="Evidence group not found")
    processes = {str(g.process_id) for g in groups}
    if len(processes) != 1:
        raise HTTPException(status_code=422, detail="Bulk operations are limited to one inspection process")
    process = db.get(InspectionProcess, processes.pop())
    require_open_process(process)
    types = {_param_codes(g.param, g.delta if isinstance(g.delta, dict) else {})["type_key"] for g in groups}
    if len(types) != 1:
        raise HTTPException(status_code=422, detail="Bulk operations are allowed only for candidates of one parameter code")
    if decision == "Reject" and not confirm_bulk_reject:
        raise HTTPException(status_code=409, detail="Bulk rejection requires explicit confirmation (confirm_bulk_reject=true)")
    target = DECISION_TO_STATUS[decision]
    for group in groups:  # validate everything before the first write: a batch is applied whole or not at all
        validate_finding_transition(str(group.finding_status), target)
    decided = []
    for group in groups:
        row = record_inspector_decision(db, evidence_group_id=int(group.id), decision=decision,
                                        reason_code=reason_code or ("OTHER" if decision == "Reject" else None),
                                        comment=comment_text, user_id=user_id)
        decided.append(int(row.evidence_group_id))
    add_audit(db, action=BULK_DECISION, user_id=user_id, process=process,
              details={"decision": decision, "evidence_group_ids": decided, "count": len(decided),
                       "type_key": next(iter(types)), "reason_code": reason_code, "comment": comment_text,
                       "confirmed_bulk_reject": bool(confirm_bulk_reject)})
    db.commit()
    return {"decision": decision, "finding_status": target, "evidence_group_ids": decided, "count": len(decided),
            "type_key": next(iter(types))}


# ---------------------------------------------------------------------------------------------------------------
# revision choice (§12-13) -- stub until S3's file_registry exists
# ---------------------------------------------------------------------------------------------------------------
def _doc_summary(doc: DocumentVersion) -> dict[str, Any]:
    return {
        **_document_snapshot(doc),
        "approval_date": doc.approval_date,
        "predecessor_id": doc.predecessor_id,
        "successor_id": doc.successor_id,
        "discipline": doc.discipline,
    }


def _stub_revision_scopes(db: Session, process: InspectionProcess) -> list[dict[str, Any]]:
    docs = _project_documents(db, process)
    context = GateContext(docs)
    doc_by_key = {facts.key: doc for doc in docs for facts in [context.facts.get(int(doc.id))] if facts is not None}
    scopes: list[dict[str, Any]] = []
    for conflict in context.revisions.conflicts:
        candidates = [_doc_summary(doc_by_key[key]) for key in conflict.keys if key in doc_by_key]
        if len(candidates) < 2:
            continue
        scopes.append({
            "scope_key": f"{conflict.stage}|{conflict.type}|{conflict.scope}",
            "stage": conflict.stage,
            "scope": conflict.scope,
            "type": conflict.type,
            "system_status": "CLARIFICATION_REQUIRED",
            "system_choice_id": None,
            "basis": conflict.detail.get("reason") or "редакции не упорядочиваются по имени/штампу",
            "candidates": candidates,
        })
    newest_groups: dict[str, list[str]] = defaultdict(list)
    for superseded, newest in context.revisions.superseded.items():
        newest_groups[newest].append(superseded)
    for newest, olders in sorted(newest_groups.items()):
        newest_doc = next((doc for key, doc in doc_by_key.items() if doc.dataset_file_id == newest or key == newest), None)
        members = [doc_by_key[key] for key in olders if key in doc_by_key]
        if newest_doc is None or not members:
            continue
        facts = context.facts.get(int(newest_doc.id))
        scopes.append({
            "scope_key": f"{facts.stage if facts else '?'}|SUPERSEDED|{newest}",
            "stage": facts.stage if facts else None,
            "scope": facts.section_key if facts else None,
            "type": "SUPERSEDED_BY_NEWER_REVISION",
            "system_status": "RESOLVED",
            "system_choice_id": int(newest_doc.id),
            "basis": "маркер редакции в имени/штампе (изм./ред./корр.): выбрана последняя, прежние исключены",
            "candidates": [_doc_summary(newest_doc), *[_doc_summary(doc) for doc in members]],
        })
    by_chain: dict[int, list[DocumentVersion]] = {}
    by_id = {int(doc.id): doc for doc in docs}
    for doc in docs:
        if doc.successor_id is None and doc.predecessor_id is not None:
            chain, cursor = [], doc
            while cursor is not None and len(chain) < 50:
                chain.append(cursor)
                cursor = by_id.get(int(cursor.predecessor_id)) if cursor.predecessor_id else None
            by_chain[int(doc.id)] = chain
    for tip_id, chain in sorted(by_chain.items()):
        tip = chain[0]
        approved = str(tip.approval_status or "").upper() in {"APPROVED", "FOR_CONSTRUCTION", "SIGNED"}
        scopes.append({
            "scope_key": f"{INTERNAL_TO_STAGE.get(str(tip.doc_stage or tip.document_stage), '?')}|CHAIN|{tip.document_code or tip_id}",
            "stage": INTERNAL_TO_STAGE.get(str(tip.doc_stage or tip.document_stage)),
            "scope": tip.document_code,
            "type": "PREDECESSOR_CHAIN",
            "system_status": "RESOLVED" if approved else "CLARIFICATION_REQUIRED",
            "system_choice_id": int(tip.id) if approved else None,
            "basis": "цепочка predecessor/successor: последняя утверждённая редакция" if approved
                     else "последняя редакция цепочки не имеет статуса утверждения",
            "candidates": [_doc_summary(doc) for doc in chain],
        })
    return scopes


def revision_scopes(db: Session, process: InspectionProcess) -> dict[str, Any]:
    hook = _s3_hook("revision_scopes")
    source = "file_registry" if hook else "s5_stub_document_facts"
    scopes = list(hook(db, process)) if hook else _stub_revision_scopes(db, process)
    for scope in scopes:
        history = (
            db.query(InspectorEdit)
            .filter(InspectorEdit.process_id == str(process.id), InspectorEdit.entity_key == f"revision:{scope['scope_key']}")
            .order_by(InspectorEdit.version.asc())
            .all()
        )
        choice = history[-1] if history else None
        scope["inspector_choice"] = edit_to_dict(choice) if choice else None
        scope["history"] = [edit_to_dict(edit) for edit in history]
        scope["effective_choice_id"] = (choice.source_document_version_id if choice else scope.get("system_choice_id"))
        scope["status"] = "RESOLVED_BY_INSPECTOR" if choice else scope.get("system_status")
    return {
        "process_id": process.id,
        "source": source,
        "scopes": scopes,
        "open_conflicts": sum(1 for s in scopes if s["status"] == "CLARIFICATION_REQUIRED"),
    }


def choose_revision(db: Session, process: InspectionProcess, *, scope_key: str, document_version_id: int,
                    justification: object, user_id: int | None) -> dict[str, Any]:
    require_open_process(process)
    reason_text = require_reason(justification, what="justification")
    scopes = revision_scopes(db, process)["scopes"]
    scope = next((s for s in scopes if s["scope_key"] == scope_key), None)
    if scope is None:
        raise HTTPException(status_code=404, detail="Revision scope not found")
    candidate = next((c for c in scope["candidates"] if int(c["document_version_id"]) == int(document_version_id)), None)
    if candidate is None:
        raise HTTPException(status_code=422, detail="The chosen document is not a candidate edition of this scope")
    previous_choice = scope.get("inspector_choice")
    previous_value = (previous_choice or {}).get("new_value") or {
        "system_status": scope.get("system_status"), "system_choice_id": scope.get("system_choice_id"), "basis": scope.get("basis"),
    }
    new_value = {"scope_key": scope_key, "stage": scope.get("stage"), "scope": scope.get("scope"), "chosen": candidate,
                 "rejected": [c for c in scope["candidates"] if int(c["document_version_id"]) != int(document_version_id)]}
    edit = _record_edit(db, process=process, group=None, entity_type=ENTITY_REVISION, entity_key=f"revision:{scope_key}",
                        action=ACTION_CHOOSE, previous_value=previous_value, new_value=new_value, reason=reason_text,
                        user_id=user_id, source_document_version_id=int(document_version_id),
                        audit_action="REVISION_CHOSEN_BY_INSPECTOR")
    hook = _s3_hook("apply_inspector_revision_choice")
    if hook:
        effect = hook(db, process, {**new_value, "inspector_edit_id": int(edit.id), "justification": reason_text})
    else:
        effect = {"applied": False, "status": "RECORDED",
                  "note": "Выбор записан с обоснованием и историей; пересчёт по выбранной редакции выполняет бэкенд реестра (S3)."}
    db.commit()
    db.refresh(edit)
    return {"choice": edit_to_dict(edit), "effect": effect}


# ---------------------------------------------------------------------------------------------------------------
# completeness against an expected manifest (§19) -- stub until S3's file_registry exists
# ---------------------------------------------------------------------------------------------------------------
_SECTION_TOKEN_RE = re.compile(r"Раздел\s*\d+\.?\s*(.+)$", re.IGNORECASE)


# PD volumes that exist as a discipline set in РД/ИД (after document_facts' canonicalization: ИОС4 == ОВ, ...);
# the rest (ПЗ, ПОС, ООС, ПБ, ОДИ, ЭЭ, СМ, ...) are PD-only volumes with no РД set of their own.
_WORKING_STAGE_SECTIONS = frozenset({"АР", "КР", "ПЗУ", "ГП", "ТХ", "ИОС1", "ИОС2", "ИОС3", "ИОС4", "ИОС5", "ИОС6", "ИОС7"})


def _section_token(section: str | None) -> str | None:
    """"Раздел 5. ИОС4" -> "ИОС4", "Раздел 2. СПЗУ" -> "ПЗУ": the tag vocabulary of `document_facts`."""
    if not section:
        return None
    match = _SECTION_TOKEN_RE.search(section)
    token = (match.group(1) if match else section).strip().upper()
    return df._TOKEN_TAG.get(token, token) or None


def _stub_completeness(db: Session, process: InspectionProcess) -> dict[str, Any]:
    docs = _project_documents(db, process)
    context = GateContext(docs)
    params = list_active_params(db, organization_id=int(process.organization_id), project_id=int(process.project_id),
                                matrix_version=process.matrix_version)
    expected: dict[tuple[str, str], int] = Counter()
    labels: dict[str, str] = {}
    for param in params:
        token = _section_token(param.section)
        if not token:
            continue
        labels[token] = param.section
        for stage, flag in (("PD", param.source_pd), ("RD", param.source_rd), ("ID", param.source_id)):
            if flag:
                expected[(stage, token)] += 1
    not_applicable = {(stage, token) for (stage, token) in expected if stage != "PD" and token not in _WORKING_STAGE_SECTIONS}
    uploaded: dict[str, int] = Counter()
    found: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    mixed_stage_files = 0
    excluded = context.integrity.excluded_keys
    for doc in docs:
        facts = context.facts.get(int(doc.id))
        if facts is None or facts.key in excluded:
            continue
        stages = [facts.stage] if facts.stage in STAGES else (["RD", "ID"] if facts.stage == "RD_ID_MIXED" else [])
        if facts.stage == "RD_ID_MIXED":
            mixed_stage_files += 1
        tags = {str(t).upper() for t in (facts.tags or ())}
        for stage in stages:
            uploaded[stage] += 1
            for (exp_stage, token) in list(expected):
                if exp_stage == stage and (token in tags or any(t.startswith(token) for t in tags)):
                    found[(stage, token)].append({"document_version_id": int(doc.id), "file_id": doc.dataset_file_id,
                                                  "file": doc.filename, "mixed_stage": facts.stage == "RD_ID_MIXED"})
    rows = []
    for (stage, token), count in sorted(expected.items(), key=lambda item: (STAGES.index(item[0][0]), item[0][1])):
        files = found.get((stage, token), [])
        mixed_only = bool(files) and all(f["mixed_stage"] for f in files)
        if (stage, token) in not_applicable and not files:
            status = "NOT_APPLICABLE"
        elif not files:
            status = "MISSING_EVIDENCE"
        elif mixed_only:
            status = "UNCERTAIN"
        else:
            status = "UPLOADED"
        rows.append({
            "stage": stage,
            "section": labels.get(token, token),
            "section_token": token,
            "expected": (stage, token) not in not_applicable,
            "expected_basis": (f"раздел ПД без отдельного комплекта {stage}: параметры сверяются с томом ПД"
                               if (stage, token) in not_applicable else f"матрица 1.1: {count} парам. раздела с источником {stage}"),
            "uploaded": len(files),
            "files": files[:20],
            "status": status,
            "uncertainty": "файлы помечены как смешанная стадия РД/ИД" if mixed_only else None,
        })
    missing = [r for r in rows if r["status"] == "MISSING_EVIDENCE"]
    uncertain = [r for r in rows if r["status"] == "UNCERTAIN"]
    status = "INCOMPLETE" if missing else ("CLARIFICATION_REQUIRED" if uncertain else "COMPLETE")
    return {
        "process_id": process.id,
        "source": "s5_stub_matrix_sections",
        "registry_present": False,
        "status": status,
        "basis": ("ожидаемый манифест = разделы матрицы 1.1 по источникам ПД/РД/ИД; найдено = разделы, распознанные "
                  "по именам/шифрам файлов. Реестр файлов (Перечень ИД 1.1) не загружен — пакет требует уточнения."),
        "stages": [{"stage": stage, "uploaded_files": int(uploaded.get(stage, 0)),
                    "expected_sections": sum(1 for r in rows if r["stage"] == stage and r["expected"]),
                    "found_sections": sum(1 for r in rows if r["stage"] == stage and r["status"] == "UPLOADED")}
                   for stage in STAGES],
        "mixed_stage_files": mixed_stage_files,
        "excluded_files": len(excluded),
        "rows": rows,
    }


def completeness_panel(db: Session, process: InspectionProcess) -> dict[str, Any]:
    hook = _s3_hook("completeness_report")
    return hook(db, process) if hook else _stub_completeness(db, process)


# ---------------------------------------------------------------------------------------------------------------
# verification time (§35)
# ---------------------------------------------------------------------------------------------------------------
def _audit_rows(db: Session, process: InspectionProcess, actions: Iterable[str]) -> list[AuditLog]:
    return (
        db.query(AuditLog)
        .filter(AuditLog.process_id == str(process.id), AuditLog.action.in_(list(actions)))
        .order_by(AuditLog.id.asc())
        .all()
    )


def _at(db: Session, row: AuditLog) -> datetime:
    """Precise moment of an audit event: the `at` the workbench recorded, or -- for a finalization -- the protocol's
    own `finalized_at` (set in Python with microseconds; the audit row's server timestamp is whole seconds)."""
    details = row.details if isinstance(row.details, dict) else {}
    try:
        return datetime.fromisoformat(str(details.get("at")))
    except (TypeError, ValueError):
        pass
    if row.action == "PROTOCOL_FINALIZED" and details.get("protocol_id"):
        protocol = db.get(Protocol, int(details["protocol_id"]))
        if protocol is not None and protocol.finalized_at is not None:
            return protocol.finalized_at
    return row.timestamp


def _cycle_start_id(db: Session, process: InspectionProcess) -> int:
    """Audit id where the current verification cycle starts: after the last unfinalize (a reopened protocol is
    measured again), else 0."""
    rows = _audit_rows(db, process, ("PROTOCOL_UNFINALIZED",))
    return int(rows[-1].id) if rows else 0


def open_verification(db: Session, process: InspectionProcess, *, user_id: int | None) -> dict[str, Any]:
    start = _cycle_start_id(db, process)
    opened = [row for row in _audit_rows(db, process, (VERIFICATION_OPENED,)) if int(row.id) > start]
    if not opened and process.status != PROCESS_FINALIZED:
        add_audit(db, action=VERIFICATION_OPENED, user_id=user_id, process=process,
                  details={"at": datetime.utcnow().isoformat(), "pending_candidates": _pending(db, process)})
        db.commit()
    return verification_timing(db, process)


def _pending(db: Session, process: InspectionProcess) -> int:
    return db.query(EvidenceGroup).filter(EvidenceGroup.process_id == str(process.id), EvidenceGroup.finding_status == STATUS_CANDIDATE).count()


def verification_timing(db: Session, process: InspectionProcess) -> dict[str, Any]:
    start = _cycle_start_id(db, process)
    rows = [row for row in _audit_rows(db, process, (VERIFICATION_OPENED, "PROTOCOL_FINALIZED", "INSPECTOR_DECISION",
                                                      DECISION_UI_METRICS, VERIFICATION_TIME_MEASURED)) if int(row.id) > start]
    opened = next((row for row in rows if row.action == VERIFICATION_OPENED), None)
    finalized = next((row for row in rows if row.action == "PROTOCOL_FINALIZED" and opened is not None and row.id > opened.id), None)
    decisions = [row for row in rows if row.action == "INSPECTOR_DECISION" and opened is not None and row.id > opened.id
                 and (finalized is None or row.id < finalized.id)]
    ui = [row.details for row in rows if row.action == DECISION_UI_METRICS and isinstance(row.details, dict)
          and opened is not None and row.id > opened.id and (finalized is None or row.id < finalized.id)]
    edits = 0
    if opened is not None:  # every inspector edit writes exactly one audit row; count those inside the cycle
        query = db.query(AuditLog).filter(AuditLog.process_id == str(process.id), AuditLog.action.in_(EDIT_AUDIT_ACTIONS),
                                          AuditLog.id > opened.id)
        if finalized is not None:
            query = query.filter(AuditLog.id < finalized.id)
        edits = query.count()
    opened_at = _at(db, opened) if opened else None
    finalized_at = _at(db, finalized) if finalized else None
    end = finalized_at or datetime.utcnow()
    duration = max(0.0, (end - opened_at).total_seconds()) if opened_at else None
    actions = [float(u["actions"]) for u in ui if isinstance(u.get("actions"), (int, float))]
    seconds = [float(u["elapsed_ms"]) / 1000 for u in ui if isinstance(u.get("elapsed_ms"), (int, float))]
    measured = [row.details for row in _audit_rows(db, process, (VERIFICATION_TIME_MEASURED,)) if isinstance(row.details, dict)]
    return {
        "process_id": process.id,
        "opened_at": opened_at,
        "finalized_at": finalized_at,
        "running": opened_at is not None and finalized_at is None,
        "duration_seconds": round(duration, 1) if duration is not None else None,
        "decisions": len(decisions),
        "evidence_edits": edits,
        "ui_decisions_measured": len(ui),
        "avg_actions_per_decision": round(sum(actions) / len(actions), 2) if actions else None,
        "avg_seconds_per_decision": round(sum(seconds) / len(seconds), 1) if seconds else None,
        "pending_candidates": _pending(db, process),
        "measurements": measured,
    }


def record_verification_time(db: Session, process: InspectionProcess, *, protocol_id: int, user_id: int | None) -> dict[str, Any] | None:
    """Called right after a successful finalize: freezes the cycle's timing into the audit log (the pitch number)."""
    timing = verification_timing(db, process)
    if timing["opened_at"] is None or timing["finalized_at"] is None:
        return None
    details = {
        "protocol_id": int(protocol_id),
        "opened_at": timing["opened_at"].isoformat(),
        "finalized_at": timing["finalized_at"].isoformat(),
        "duration_seconds": timing["duration_seconds"],
        "decisions": timing["decisions"],
        "evidence_edits": timing["evidence_edits"],
        "avg_actions_per_decision": timing["avg_actions_per_decision"],
        "avg_seconds_per_decision": timing["avg_seconds_per_decision"],
    }
    add_audit(db, action=VERIFICATION_TIME_MEASURED, user_id=user_id, process=process, details=details)
    db.commit()
    return details


def record_decision_ui_metrics(db: Session, process: InspectionProcess, *, evidence_group_id: int, metrics: dict[str, Any],
                               user_id: int | None) -> None:
    clean: dict[str, Any] = {"evidence_group_id": int(evidence_group_id), "at": datetime.utcnow().isoformat()}
    for key in ("actions", "elapsed_ms"):
        value = metrics.get(key)
        if isinstance(value, (int, float)) and 0 <= value < 10_000_000:
            clean[key] = value
    for key in ("via", "decision"):
        if isinstance(metrics.get(key), str):
            clean[key] = metrics[key][:32]
    add_audit(db, action=DECISION_UI_METRICS, user_id=user_id, process=process, details=clean)
    db.commit()
