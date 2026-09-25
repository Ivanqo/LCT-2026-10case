"""Server-side batch path over HTTP (Phase 12, S6): a package that is already on the server (a folder or a ZIP below
CASE10_BATCH_PACKAGES_ROOT) plus its file registry -> documents -> process queued to the CASE10 worker.

The HTTP twin of `python -m app.cli run`, with the same import code (`app.batch_package`), for packages too big for
the 50/200 MB UI upload (expert session §9, Q14). Unlike the CLI it writes into the service database, so the
process shows up in the inspector UI with page previews, decisions and the protocol. The request carries paths,
never file bytes, and only paths inside the packages root are accepted.

    POST /api/case10/batch-runs                     {"project_id", "package", "registry"?, "object_id"?}
    GET  /api/case10/batch-runs/{process_id}        process status + package report
    GET  /api/case10/batch-runs/{process_id}/result submission JSON, same format as the CLI's result.json
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import batch_package as bp
from ..config import settings
from ..db.models import AuditLog, InspectionProcess, User
from ..db.session import get_db
from ..domain.official_dataset import MATRIX_VERSION_OFFICIAL
from ..domain.v3_jobs import enqueue_process_job, publish_job_message
from ..domain.v3_pipeline import add_audit, create_process, latest_protocol, process_to_dict, protocol_to_dict
from .auth import require_supervisor
from .utils import get_project_for_org

router = APIRouter(tags=["case10-batch"])

AUDIT_ACTION = "CASE10_BATCH_PACKAGE_IMPORTED"
UNPACK_DIR = "_unpacked"


class BatchRunIn(BaseModel):
    project_id: int
    package: str
    registry: str | None = None
    object_id: str | None = None
    run_immediately: bool = True


@router.post("/case10/batch-runs")
def create_batch_run(payload: BatchRunIn, db: Session = Depends(get_db), user: User = Depends(require_supervisor)):
    organization_id = _require_org(user)
    get_project_for_org(db, payload.project_id, organization_id)
    root = _packages_root()
    source = _inside_root(root, payload.package, "package")
    registry_path = _inside_root(root, payload.registry, "registry") if payload.registry else None
    try:
        package_root, package_name = bp.resolve_package(
            source, unpack_root=root / UNPACK_DIR, max_unpacked_bytes=settings.BATCH_MAX_UNPACKED_BYTES)
        files = bp.scan_package(package_root)
        registry = bp.read_registry(registry_path) if registry_path else None
        plan = bp.build_plan(
            files, registry, package_name=package_name, object_id=payload.object_id,
            registry_sha256=bp.sha256_file(registry_path) if registry_path else None,
            originals_prefix=package_root.relative_to(root).as_posix(),
        )
    except bp.PackageError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not plan.rows:
        raise HTTPException(status_code=422, detail="package contains no files")

    manifest = settings.DATA_DIR / "batch_manifests" / f"{hashlib.sha256(str(package_root).encode()).hexdigest()[:16]}.jsonl"
    documents = bp.import_plan(db, plan, project_id=payload.project_id, organization_id=organization_id,
                               manifest_path=manifest)
    process = create_process(db, project_id=payload.project_id, organization_id=organization_id,
                             object_id=plan.object_id, matrix_version=MATRIX_VERSION_OFFICIAL)
    add_audit(db, action=AUDIT_ACTION, user_id=int(user.id), process=process,
              details={"package": payload.package, "registry": payload.registry, "report": plan.report})
    job = None
    if payload.run_immediately:
        job = enqueue_process_job(db, process, user_id=int(user.id), reason="batch_package")
    db.commit()
    if job is not None:
        publish_job_message(job)
    db.refresh(process)
    return {
        "process": process_to_dict(db, process, include_protocol_payload=False),
        "documents_imported": documents,
        "package": _report_summary(plan.report),
    }


@router.get("/case10/batch-runs/{process_id}")
def get_batch_run(process_id: str, db: Session = Depends(get_db), user: User = Depends(require_supervisor)):
    process = _org_process(db, process_id, user)
    return {
        "process": process_to_dict(db, process, include_protocol_payload=False),
        "package": _report_summary(_package_report(db, process)),
    }


@router.get("/case10/batch-runs/{process_id}/result")
def get_batch_result(process_id: str, db: Session = Depends(get_db), user: User = Depends(require_supervisor)):
    from evaluation.exporter import protocol_to_submission, validate_submission_basic, validate_submission_schema

    process = _org_process(db, process_id, user)
    protocol = latest_protocol(db, project_id=int(process.project_id), organization_id=int(process.organization_id),
                               process_id=process.id)
    if protocol is None:
        raise HTTPException(status_code=409, detail=f"process is {process.status}; no protocol yet")
    submission = protocol_to_submission(protocol_to_dict(protocol))
    submission["package"] = _package_report(db, process)
    schema = Path(__file__).resolve().parents[1] / "reference_data" / "case_data" / "submission_schema.json"
    errors = validate_submission_schema(submission, schema) + validate_submission_basic(submission)
    if errors:
        raise HTTPException(status_code=500, detail={"message": "result does not validate", "errors": errors[:20]})
    return submission


def _packages_root() -> Path:
    root = settings.BATCH_PACKAGES_ROOT
    originals = os.environ.get("CASE10_ORIGINALS_ROOT", "").strip()
    # The worker reads document bytes through CASE10_ORIGINALS_ROOT (dataset_sources.py); a package outside it
    # would import fine and then fail page by page during the run.
    if not originals or Path(originals).resolve() != root:
        raise HTTPException(status_code=503, detail="server batch path is not configured: set CASE10_ORIGINALS_ROOT "
                                                    "to the same directory as CASE10_BATCH_PACKAGES_ROOT")
    if not root.is_dir():
        raise HTTPException(status_code=503, detail=f"packages root does not exist: {root}")
    return root


def _inside_root(root: Path, relative: str, what: str) -> Path:
    parts = PurePosixPath(str(relative).replace("\\", "/")).parts
    if not parts or ".." in parts or str(relative).startswith(("/", "\\")) or ":" in str(relative):
        raise HTTPException(status_code=422, detail=f"{what} must be a path relative to the packages root")
    path = (root / Path(*parts)).resolve()
    if not path.is_relative_to(root) or not path.exists():
        raise HTTPException(status_code=404, detail=f"{what} not found under the packages root: {relative}")
    return path


def _require_org(user: User) -> int:
    if user.organization_id is None:
        raise HTTPException(status_code=403, detail="User is not attached to an organization")
    return int(user.organization_id)


def _org_process(db: Session, process_id: str, user: User) -> InspectionProcess:
    process = db.get(InspectionProcess, str(process_id))
    if not process or int(process.organization_id) != _require_org(user):
        raise HTTPException(status_code=404, detail="Inspection process not found")
    return process


def _package_report(db: Session, process: InspectionProcess) -> dict[str, Any] | None:
    row = (
        db.query(AuditLog)
        .filter(AuditLog.process_id == str(process.id), AuditLog.action == AUDIT_ACTION)
        .order_by(AuditLog.id.desc())
        .first()
    )
    return (row.details or {}).get("report") if row else None


def _report_summary(report: dict[str, Any] | None) -> dict[str, Any] | None:
    if not report:
        return None
    summary = {key: value for key, value in report.items() if key != "files"}
    summary["files_listed"] = len(report.get("files") or [])
    return summary
