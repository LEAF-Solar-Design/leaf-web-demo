"""GET /api/session — Intake JSON (contract section 1). Moved verbatim from app.py."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException

import deps

router = APIRouter()


@router.get("/api/session")
def session(dwg: str = "rooftop_demo") -> Dict[str, Any]:
    """Return the Intake JSON (contract section 1). APS_LIVE=0 -> cached sample."""
    if deps.APS_LIVE:
        da = deps.get_da_client()
        if da is None or not hasattr(da, "extract"):
            raise HTTPException(500, "APS_LIVE=1 but da/client.py (Lane A) is not importable")
        try:
            # root/Lane A owns the actual dwg path resolution; use the known sample path
            local = str(deps.DATA_FILE.parent / f"{dwg}.dwg")
            return {"intake": da.extract(local)}
        except Exception as exc:
            raise HTTPException(502, f"DA extract failed: {exc}")
    if not deps.DATA_FILE.exists():
        raise HTTPException(404, f"cached intake not found: {deps.DATA_FILE}")
    return {"intake": deps.load_cached_intake()}
