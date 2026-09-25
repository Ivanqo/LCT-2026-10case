from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import select

from ..db.models import Project


def get_project_for_org(db: Session, project_id: int, organization_id: int | None) -> Project:
    project = db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    # organization_id is required for regular users; for safety treat None as forbidden
    if organization_id is None or project.organization_id != organization_id:
        raise HTTPException(status_code=403, detail="Project is not accessible for this organization")
    return project
