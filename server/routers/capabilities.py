"""GET /api/capabilities — capability catalog (CONTRACT-ADDENDUM section 9).

Internal/QA tools are filtered SERVER-SIDE by default; the X-Internal-Role: qa
header opts in (v1 stub — a future auth sibling supplies real role identity).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header

import catalog
import deps
from envelopes import with_envelope_fields

router = APIRouter()


@router.get("/api/capabilities")
def capabilities(x_internal_role: Optional[str] = Header(default=None),
                 tenant=Depends(deps.require_tenant)) -> Dict[str, Any]:
    """Capability catalog, TENANT-SCOPED for the folded portion (wave 4): globals for
    everyone, only the requesting tenant's OWN repo tools folded in."""
    include_internal = (x_internal_role or "").strip().lower() == "qa"
    families = catalog.build_catalog(deps.all_tools(str(tenant)), include_internal=include_internal)
    return with_envelope_fields({"families": families})
