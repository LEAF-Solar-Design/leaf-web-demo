"""GET /api/session — Intake JSON (contract section 1)."""
from __future__ import annotations

from fastapi import APIRouter

import deps
from envelopes import ErrorCode, error_response, with_envelope_fields

router = APIRouter()


@router.get("/api/session")
def session(dwg: str = "rooftop_demo"):
    """Return the Intake JSON (contract section 1). APS_LIVE=0 -> cached sample.

    LEGACY NOTE: the APS_LIVE=1 extract path still runs in-process via Lane A's
    client (pre-broker). Migrating extract through the broker is a follow-up for
    the session that owns extract/provisioning; the async TOOL-RUN path never
    touches da.* in this process.
    """
    if deps.APS_LIVE:
        da = deps.get_da_client()
        if da is None or not hasattr(da, "extract"):
            return error_response(ErrorCode.APS_UNAVAILABLE,
                                  "APS_LIVE=1 but da/client.py (Lane A) is not importable",
                                  retryable=False, status_code=500)
        try:
            # root/Lane A owns the actual dwg path resolution; use the known sample path
            local = str(deps.DATA_FILE.parent / f"{dwg}.dwg")
            return with_envelope_fields({"intake": da.extract(local)})
        except Exception as exc:  # noqa: BLE001
            return error_response(ErrorCode.APS_UNAVAILABLE, f"DA extract failed: {exc}",
                                  retryable=True, status_code=502)
    if not deps.DATA_FILE.exists():
        return error_response(ErrorCode.INTERNAL, f"cached intake not found: {deps.DATA_FILE}",
                              retryable=False, status_code=404)
    return with_envelope_fields({"intake": deps.load_cached_intake()})
