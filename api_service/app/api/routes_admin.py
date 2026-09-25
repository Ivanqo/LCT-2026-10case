from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select, func
from sqlalchemy.orm import Session
import re
import secrets
import csv
from io import StringIO

from ..db.models import Organization, User, Project, QASession, UserCredentialRecord
from ..db.session import get_db
from ..schemas import OrganizationCreate, OrganizationOut, UserCreate, UserOut, UserBatchCreate, UserCredentialOut, AdminQuestionListOut, AdminQuestionOut, UserCredentialRecordOut, UserCredentialRecordListOut
from ..security import generate_api_key, generate_password, hash_password
from .auth import require_admin

router = APIRouter(tags=["admin"])


def _make_fallback_email(db: Session, login: str, org_id: int | None = None) -> str:
    safe_login = re.sub(r"[^a-z0-9]+", "-", login.lower()).strip("-") or "user"
    org_part = f"org{org_id}" if org_id is not None else "org"
    candidate = f"{safe_login}-{org_part}@local.invalid"
    while db.execute(select(User.id).where(User.email == candidate)).first():
        candidate = f"{safe_login}-{org_part}-{secrets.token_hex(3)}@local.invalid"
    return candidate


def _next_login(db: Session, prefix: str, seq: int) -> str:
    base = f"{prefix}{seq:03d}"
    login = base
    n = 1
    while db.execute(select(User.id).where(User.login == login)).first():
        n += 1
        login = f"{base}_{n}"
    return login


def _credential_out(record: UserCredentialRecord) -> UserCredentialRecordOut:
    return UserCredentialRecordOut(
        id=int(record.id),
        user_id=int(record.user_id),
        organization_id=int(record.organization_id) if record.organization_id is not None else None,
        login=record.login,
        email=record.email,
        password=record.password_plain,
        api_key=record.api_key,
        is_admin=bool(record.is_admin),
        created_by_user_id=int(record.created_by_user_id) if record.created_by_user_id is not None else None,
        created_at=record.created_at,
    )


def _remember_created_credentials(db: Session, user: User, password: str, admin: User) -> UserCredentialRecord:
    record = UserCredentialRecord(
        user_id=user.id,
        organization_id=user.organization_id,
        login=user.login,
        email=user.email,
        password_plain=password,
        api_key=user.api_key,
        is_admin=bool(user.is_admin),
        created_by_user_id=admin.id,
    )
    db.add(record)
    return record


def _accounts_txt(rows: list[UserCredentialRecordOut] | list[UserCredentialOut], title: str = "Созданные аккаунты") -> str:
    lines = [title, "=" * len(title), ""]
    for idx, r in enumerate(rows, 1):
        lines.extend([
            f"{idx}. {r.login}",
            f"   login: {r.login}",
            f"   password: {getattr(r, 'password', None) or 'недоступен'}",
            f"   email: {r.email or ''}",
            f"   api_key: {r.api_key}",
            f"   role: {'admin' if r.is_admin else 'user'}",
            f"   organization_id: {r.organization_id or ''}",
            "",
        ])
    return "\n".join(lines)


@router.post("/admin/orgs", response_model=OrganizationOut)
def create_org(payload: OrganizationCreate, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    existing = db.execute(select(Organization).where(Organization.name == payload.name)).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Organization with this name already exists")
    slug = payload.name.strip().lower().replace(" ", "-")
    org = Organization(name=payload.name, slug=slug)
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


@router.get("/admin/orgs", response_model=list[OrganizationOut])
def list_orgs(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    return db.execute(select(Organization).order_by(Organization.created_at.desc())).scalars().all()


@router.post("/admin/users", response_model=UserCredentialOut)
def create_user(payload: UserCreate, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    org = db.get(Organization, payload.organization_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    if db.execute(select(User).where(User.login == payload.login)).first():
        raise HTTPException(status_code=409, detail="User with this login already exists")
    if payload.email and db.execute(select(User).where(User.email == payload.email)).first():
        raise HTTPException(status_code=409, detail="User with this email already exists")

    password = payload.password or generate_password()
    email = payload.email or _make_fallback_email(db, payload.login, org.id)
    u = User(
        login=payload.login,
        email=email,
        password_hash=hash_password(password),
        api_key=generate_api_key(),
        is_admin=bool(payload.is_admin),
        role=payload.role or "INSPECTOR",
        organization_id=org.id,
    )
    db.add(u)
    db.flush()
    _remember_created_credentials(db, u, password, admin)
    db.commit()
    db.refresh(u)
    return UserCredentialOut(**UserOut.model_validate(u).model_dump(), password=password)


@router.post("/admin/users/batch", response_model=list[UserCredentialOut])
def create_users_batch(payload: UserBatchCreate, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    org = db.get(Organization, payload.organization_id)
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")

    created: list[UserCredentialOut] = []
    for seq in range(1, payload.count + 1):
        login = _next_login(db, payload.login_prefix, seq)
        password = generate_password()
        u = User(
            login=login,
            email=_make_fallback_email(db, login, org.id),
            password_hash=hash_password(password),
            api_key=generate_api_key(),
            is_admin=bool(payload.is_admin),
            role=payload.role or "INSPECTOR",
            organization_id=org.id,
        )
        db.add(u)
        db.flush()
        _remember_created_credentials(db, u, password, admin)
        created.append(UserCredentialOut(**UserOut.model_validate(u).model_dump(), password=password))
    db.commit()
    return created


@router.get("/admin/users", response_model=list[UserOut])
def list_users(organization_id: int | None = None, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    q = select(User).order_by(User.created_at.desc())
    if organization_id is not None:
        q = q.where(User.organization_id == organization_id)
    return db.execute(q).scalars().all()


@router.get("/admin/user-credentials", response_model=UserCredentialRecordListOut)
def list_user_credentials(
    organization_id: int | None = None,
    limit: int = 200,
    offset: int = 0,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    limit = max(1, min(int(limit or 200), 1000))
    offset = max(0, int(offset or 0))
    effective_org_id = organization_id if organization_id is not None else admin.organization_id
    stmt = select(UserCredentialRecord).order_by(UserCredentialRecord.created_at.desc())
    count_stmt = select(func.count()).select_from(UserCredentialRecord)
    if effective_org_id is not None:
        stmt = stmt.where(UserCredentialRecord.organization_id == effective_org_id)
        count_stmt = count_stmt.where(UserCredentialRecord.organization_id == effective_org_id)
    total = int(db.execute(count_stmt).scalar() or 0)
    records = db.execute(stmt.limit(limit).offset(offset)).scalars().all()
    return UserCredentialRecordListOut(items=[_credential_out(r) for r in records], total=total)


@router.get("/admin/user-credentials.txt")
def download_user_credentials_txt(
    organization_id: int | None = None,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    effective_org_id = organization_id if organization_id is not None else admin.organization_id
    stmt = select(UserCredentialRecord).order_by(UserCredentialRecord.created_at.desc())
    if effective_org_id is not None:
        stmt = stmt.where(UserCredentialRecord.organization_id == effective_org_id)
    records = db.execute(stmt).scalars().all()
    rows = [_credential_out(r) for r in records]
    content = _accounts_txt(rows, title="Данные созданных аккаунтов")
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="created_accounts.txt"'},
    )



def _question_row_to_out(row) -> AdminQuestionOut:
    qa, project, organization, asked_user = row
    answer = qa.answer or ""
    answer_preview = re.sub(r"\s+", " ", answer).strip()
    if len(answer_preview) > 360:
        answer_preview = answer_preview[:357].rstrip() + "..."
    return AdminQuestionOut(
        id=int(qa.id),
        organization_id=int(organization.id) if organization else None,
        organization_name=organization.name if organization else None,
        project_id=int(project.id),
        project_name=project.name,
        user_id=int(asked_user.id) if asked_user else None,
        user_login=asked_user.login if asked_user else None,
        user_email=asked_user.email if asked_user else None,
        user_label=qa.user_label,
        question=qa.question,
        answer_preview=answer_preview,
        created_at=qa.created_at,
    )


def _questions_stmt(admin: User, organization_id: int | None = None, project_id: int | None = None, user_id: int | None = None):
    stmt = (
        select(QASession, Project, Organization, User)
        .join(Project, QASession.project_id == Project.id)
        .outerjoin(Organization, Project.organization_id == Organization.id)
        .outerjoin(User, QASession.user_id == User.id)
    )
    # By default an admin sees questions from their own organization. A platform
    # admin without organization can see all organizations.
    effective_org_id = organization_id if organization_id is not None else admin.organization_id
    if effective_org_id is not None:
        stmt = stmt.where(Project.organization_id == effective_org_id)
    if project_id is not None:
        stmt = stmt.where(QASession.project_id == project_id)
    if user_id is not None:
        stmt = stmt.where(QASession.user_id == user_id)
    return stmt


@router.get("/admin/questions", response_model=AdminQuestionListOut)
def list_admin_questions(
    organization_id: int | None = None,
    project_id: int | None = None,
    user_id: int | None = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    limit = max(1, min(int(limit or 100), 500))
    offset = max(0, int(offset or 0))
    base = _questions_stmt(admin, organization_id=organization_id, project_id=project_id, user_id=user_id)
    count_stmt = select(func.count()).select_from(base.subquery())
    total = int(db.execute(count_stmt).scalar() or 0)
    rows = db.execute(base.order_by(QASession.created_at.desc()).limit(limit).offset(offset)).all()
    return AdminQuestionListOut(items=[_question_row_to_out(row) for row in rows], total=total)


@router.get("/admin/questions.csv")
def download_admin_questions_csv(
    organization_id: int | None = None,
    project_id: int | None = None,
    user_id: int | None = None,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    stmt = _questions_stmt(admin, organization_id=organization_id, project_id=project_id, user_id=user_id)
    rows = db.execute(stmt.order_by(QASession.created_at.desc())).all()
    out = StringIO()
    out.write("\ufeff")
    writer = csv.writer(out, delimiter=';')
    writer.writerow([
        "question_id",
        "created_at",
        "organization_id",
        "organization_name",
        "project_id",
        "project_name",
        "user_id",
        "user_login",
        "user_email",
        "user_label",
        "question",
        "answer",
    ])
    for qa, project, organization, asked_user in rows:
        writer.writerow([
            qa.id,
            qa.created_at.isoformat() if qa.created_at else "",
            organization.id if organization else "",
            organization.name if organization else "",
            project.id,
            project.name,
            asked_user.id if asked_user else "",
            asked_user.login if asked_user else "",
            asked_user.email if asked_user else "",
            qa.user_label or "",
            qa.question or "",
            qa.answer or "",
        ])
    filename = "organization_questions.csv"
    return Response(
        content=out.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
