from __future__ import annotations

from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Header, Request
from sqlalchemy.orm import Session
from sqlalchemy import select

from ..db.session import get_db
from ..db.models import User, UserSession
from ..request_context import resolve_client_ip, set_client_ip, set_user_id


def _expired(expires_at: datetime) -> bool:
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at <= datetime.now(timezone.utc)


async def get_current_user(
    request: Request,
    authorization: str | None = Header(default=None, alias="Authorization"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    db: Session = Depends(get_db),
) -> User:
    # Must stay a coroutine (not a plain def): FastAPI runs sync dependencies
    # in a worker thread via anyio.to_thread.run_sync, which copies the
    # current contextvars.Context into that thread -- set_client_ip() would
    # then mutate a throwaway copy instead of the request's own context, and
    # the ip would never reach add_audit() back on the event loop.
    set_client_ip(resolve_client_ip(request))
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1].strip()
        sess = db.execute(select(UserSession).where(UserSession.token == token)).scalar_one_or_none()
        if not sess or _expired(sess.expires_at):
            raise HTTPException(status_code=401, detail="Invalid or expired access token")
        user = db.get(User, sess.user_id)
        if not user:
            raise HTTPException(status_code=401, detail="User not found")
        set_user_id(user.id)
        return user

    if x_api_key:
        user = db.execute(select(User).where(User.api_key == x_api_key)).scalar_one_or_none()
        if user:
            set_user_id(user.id)
            return user
    raise HTTPException(status_code=401, detail="Authentication required")


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


SUPERVISOR_ROLES = {"ADMIN", "SUPERVISOR"}


def is_supervisor(user: User) -> bool:
    return bool(user.is_admin) or str(user.role or "").upper() in SUPERVISOR_ROLES


def require_supervisor(user: User = Depends(get_current_user)) -> User:
    if not is_supervisor(user):
        raise HTTPException(status_code=403, detail="Supervisor or admin access required")
    return user


ML_ENGINEER_ROLES = {"ADMIN", "ML_ENGINEER"}


def is_ml_engineer(user: User) -> bool:
    return bool(user.is_admin) or str(user.role or "").upper() in ML_ENGINEER_ROLES


def require_ml_engineer(user: User = Depends(get_current_user)) -> User:
    if not is_ml_engineer(user):
        raise HTTPException(status_code=403, detail="ML engineer or admin access required")
    return user
