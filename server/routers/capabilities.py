"""GET /api/capabilities — capability catalog (CONTRACT-ADDENDUM section 9).

Internal/QA tools are filtered SERVER-SIDE by default. X-Internal-Role: qa
requests the projection, but the existing X-Ops-Secret credential authorizes it.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Header

import catalog
import deps
from envelopes import with_envelope_fields
from routers import ops as ops_router

router = APIRouter()


@router.get("/api/capabilities")
def capabilities(x_internal_role: Optional[str] = Header(default=None),
                 x_ops_secret: Optional[str] = Header(default=None),
                 tenant=Depends(deps.require_tenant)) -> Any:
    """Capability catalog, TENANT-SCOPED for the folded portion (wave 4): globals for
    everyone, only the requesting tenant's OWN repo tools folded in."""
    include_internal = (x_internal_role or "").strip().lower() == "qa"
    if include_internal:
        denial = ops_router._require_ops(x_ops_secret)
        if denial is not None:
            return denial
    families = catalog.build_catalog(deps.all_tools(str(tenant)), include_internal=include_internal)
    return with_envelope_fields({"families": families})
