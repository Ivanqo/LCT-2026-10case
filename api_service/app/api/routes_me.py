from __future__ import annotations

from fastapi import APIRouter, Depends

from ..db.models import User
from ..schemas import MeOut
from .auth import get_current_user

router = APIRouter(tags=["auth"])


@router.get("/me", response_model=MeOut)
def me(user: User = Depends(get_current_user)):
    return MeOut(
        id=user.id,
        login=user.login,
        email=user.email,
        is_admin=bool(user.is_admin),
        role=str(user.role or "INSPECTOR"),
        organization_id=user.organization_id,
        organization_name=(user.organization.name if getattr(user, 'organization', None) else None),
    )
