"""Authenticated Lab status and the reversible Lab admission rail."""
from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

import deps

router = APIRouter(tags=["lab_surface"])

FLAG_LAB_SURFACE = "LEAF_LAB_SURFACE_ENABLED"


def lab_posture() -> str:
    """Use the deployed runtime vocabulary and deployment environment guard."""
    from broker import _deployed_runtime, _production_runtime

    if _production_runtime():
        return "production"
    if (_deployed_runtime() and os.environ.get(
            "LEAF_DEPLOYMENT_ENVIRONMENT", "").strip().lower() in {"", "staging"}):
        return "staging"
    return "local"


def lab_surface_enabled() -> bool:
    """Read explicit configuration first; only staging defaults to enabled."""
    raw = os.environ.get(FLAG_LAB_SURFACE)
    if raw is not None:
        value = raw.strip().lower()
        if value in {"0", "false", "no", "off"}:
            return False
        return value in {"1", "true", "yes", "on"}
    return lab_posture() == "staging"


def lab_rail() -> None:
    if not lab_surface_enabled():
        raise HTTPException(status_code=404, detail={
            "ok": False, "status": "refused", "reason": "lab_surface_disabled",
        })


def require_auth_live() -> None:
    if not deps.auth_live():
        raise HTTPException(status_code=503, detail={
            "ok": False, "status": "refused", "reason": "lab_auth_live_required",
        })


@router.get("/api/lab/status")
def status(tenant: Any = Depends(deps.require_tenant)) -> dict[str, Any]:
    return {
        "ok": True,
        "schema": "leaf.lab-status.v1",
        "rail": "on",
        "posture": lab_posture(),
        "tenant": str(getattr(tenant, "tenant_id", tenant)),
    }
