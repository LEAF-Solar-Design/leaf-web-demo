"""GET /api/capabilities — capability catalog (CONTRACT-ADDENDUM section 9).

Internal/QA tools are filtered SERVER-SIDE by default; the X-Internal-Role: qa
header opts in (v1 stub — a future auth sibling supplies real role identity).
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Header

import catalog
import deps
from envelopes import with_envelope_fields

router = APIRouter()


@router.get("/api/capabilities")
def capabilities(x_internal_role: Optional[str] = Header(default=None)) -> Dict[str, Any]:
    include_internal = (x_internal_role or "").strip().lower() == "qa"
    families = catalog.build_catalog(deps.all_tools(), include_internal=include_internal)
    return with_envelope_fields({"families": families})
