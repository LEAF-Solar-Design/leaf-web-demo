"""Pinned Lab routers, all admitted through the same ordered dependencies."""
from fastapi import APIRouter, Depends

import deps
from routers import lab_activation, lab_artifacts, lab_runs, lab_stream, lab_surface, lab_widgets
from routers.lab_surface import lab_rail, require_auth_live

PINNED_ROUTERS = ("lab_surface", "lab_artifacts", "lab_widgets", "lab_runs", "lab_stream", "lab_activation")

router = APIRouter()
router.include_router(lab_surface.router, dependencies=[
    Depends(lab_rail), Depends(require_auth_live), Depends(deps.require_tenant),
])
router.include_router(lab_artifacts.router, dependencies=[
    Depends(lab_rail), Depends(require_auth_live), Depends(deps.require_tenant),
])
router.include_router(lab_widgets.router, dependencies=[
    Depends(lab_rail), Depends(require_auth_live), Depends(deps.require_tenant),
])
router.include_router(lab_runs.router, dependencies=[
    Depends(lab_rail), Depends(require_auth_live), Depends(deps.require_tenant),
])
router.include_router(lab_stream.router, dependencies=[
    Depends(lab_rail), Depends(require_auth_live), Depends(deps.require_tenant),
])
router.include_router(lab_activation.router, dependencies=[
    Depends(lab_rail), Depends(require_auth_live), Depends(deps.require_tenant),
])
