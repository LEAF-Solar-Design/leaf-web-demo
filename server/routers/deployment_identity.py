"""Authenticated, live-derived identity evidence for staging acceptance."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

import deps
from deployment_identity_live import (
    LiveIdentityUnavailable,
    live_deployment_identity,
)

router = APIRouter()


@router.get("/api/deployment-identity")
def get_deployment_identity(_tenant=Depends(deps.require_tenant)) -> dict[str, Any]:
    """Return the identity of the services that are RUNNING right now.

    This is derived from live ECS state on every request, so it cannot go
    stale. The stored ``LEAF_DEPLOYMENT_IDENTITY`` receipt contributes only a
    digest-to-commit mapping, and only for a service whose receipt digest
    equals its live digest, so a stale receipt goes inert rather than false.

    200 does NOT mean converged. A convergence gate must read ``status`` and
    require ``verified``; ``mismatch`` and ``unattested`` are non-answers that
    happen to carry truthful digests. 503 means live state could not be read,
    which is the one case where no honest answer exists.
    """
    try:
        return live_deployment_identity()
    except LiveIdentityUnavailable as exc:
        raise HTTPException(
            status_code=503, detail=f"deployment identity unavailable: {exc}"
        ) from exc
