from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import select

from ..db.session import get_db
from ..db.models import Project, User
from ..schemas import ProjectCreate, ProjectOut
from .auth import get_current_user

router = APIRouter(tags=["projects"])


@router.post("/projects", response_model=ProjectOut)
def create_project(payload: ProjectCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.organization_id is None:
        raise HTTPException(status_code=403, detail="User is not attached to an organization")

    existing = db.execute(
        select(Project).where(Project.organization_id == user.organization_id, Project.name == payload.name)
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status_code=409, detail="Project with this name already exists in your organization")

    p = Project(name=payload.name, description=payload.description, organization_id=user.organization_id)
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


@router.get("/projects", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.organization_id is None:
        return []
    rows = db.execute(
        select(Project).where(Project.organization_id == user.organization_id).order_by(Project.created_at.desc())
    ).scalars().all()
    return rows
