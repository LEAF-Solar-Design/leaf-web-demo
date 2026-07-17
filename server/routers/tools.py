"""GET /api/tools — flat back-compat tool list.

Owned downstream by the `dynamic-tool-loader` session.

NOTE: POST /api/run moved to routers/jobs.py (async job spine, ADDENDUM
section 7) — it now returns 202 {job_id}; `?wait=1` keeps the old sync shape.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter

import deps
from envelopes import with_envelope_fields

router = APIRouter()


@router.get("/api/tools")
def tools() -> Dict[str, Any]:
    return with_envelope_fields({"tools": deps.all_tools()})
