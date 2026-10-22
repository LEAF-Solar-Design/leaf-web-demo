"""Pinned Lab routers, all admitted through the same ordered dependencies."""
from fastapi import APIRouter, Depends

import deps
from routers import lab_surface
from routers.lab_surface import lab_rail, require_auth_live

PINNED_ROUTERS = ("lab_surface",)

router = APIRouter()
router.include_router(lab_surface.router, dependencies=[
    Depends(lab_rail), Depends(require_auth_live), Depends(deps.require_tenant),
])
