"""Authenticated, deployment-owned identity evidence for staging acceptance."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

import deps
from deployment_identity import deployment_identity

router = APIRouter()


@router.get("/api/deployment-identity")
def get_deployment_identity(_tenant=Depends(deps.require_tenant)) -> dict[str, Any]:
    """Return the live receipt only to an authenticated tenant session."""
    try:
        return deployment_identity()
    except ValueError as exc:
        raise HTTPException(status_code=503, detail="deployment identity unavailable") from exc
