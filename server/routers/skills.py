"""Tenant-safe skill catalog routes."""
from __future__ import annotations

import hmac
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header

import deps
import skills_catalog
from routers import ops


router = APIRouter()


def _is_operator(x_ops_secret: Optional[str]) -> bool:
    """Use the existing ops-secret principal proof without making this route ops-only."""
    secret = ops._ops_secret()
    return secret is not None and hmac.compare_digest(x_ops_secret or "", secret)


@router.get("/api/skills")
def get_skills(
    _tenant: Any = Depends(deps.require_tenant),
    x_ops_secret: Optional[str] = Header(default=None),
) -> dict[str, list[dict[str, str]]]:
    """List the tenant bundle, plus the separate operator bundle when authorized."""
    return skills_catalog.catalog(operator=_is_operator(x_ops_secret))
