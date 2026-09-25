from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import Optional
from sqlalchemy import select

from ..db.session import get_db
from ..db.models import User, QASession
from ..schemas import HistoryItem
from .auth import get_current_user
from .utils import get_project_for_org

router = APIRouter(tags=["history"])


@router.get("/history", response_model=list[HistoryItem])
def history(
    project_id: int,
    user_label: Optional[str] = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if user.organization_id is None:
        raise HTTPException(status_code=403, detail="User is not attached to an organization")

    get_project_for_org(db, project_id, user.organization_id)

    stmt = select(QASession).where(QASession.project_id == project_id)
    if user_label:
        stmt = stmt.where(QASession.user_label == user_label)
    stmt = stmt.order_by(QASession.created_at.desc())
    rows = db.execute(stmt).scalars().all()
    return rows
