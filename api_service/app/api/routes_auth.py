from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Header
from sqlalchemy import select
from sqlalchemy.orm import joinedload
from sqlalchemy.orm import Session

from ..db.models import User, UserSession
from ..db.session import get_db
from ..schemas import LoginRequest, AuthTokenOut, MeOut
from ..security import verify_password, generate_session_token, expires_at
from .auth import get_current_user

router = APIRouter(tags=["auth"])


@router.post("/auth/login", response_model=AuthTokenOut)
def login(payload: LoginRequest, db: Session = Depends(get_db)):
    user = db.execute(select(User).options(joinedload(User.organization)).where(User.login == payload.login)).scalar_one_or_none()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid login or password")

    session = UserSession(user_id=user.id, token=generate_session_token(), expires_at=expires_at())
    db.add(session)
    db.commit()
    db.refresh(session)
    return AuthTokenOut(access_token=session.token, user=MeOut(
        id=user.id,
        login=user.login,
        email=user.email,
        is_admin=bool(user.is_admin),
        role=str(user.role or "INSPECTOR"),
        organization_id=user.organization_id,
        organization_name=(user.organization.name if getattr(user, 'organization', None) else None),
    ))


@router.post("/auth/logout")
def logout(
    authorization: str | None = Header(default=None, alias="Authorization"),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        sess = db.execute(select(UserSession).where(UserSession.token == token, UserSession.user_id == user.id)).scalar_one_or_none()
        if sess:
            db.delete(sess)
            db.commit()
    return {"ok": True}
