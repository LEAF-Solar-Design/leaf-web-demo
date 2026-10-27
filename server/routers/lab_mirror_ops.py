"""SEL-316b: operator-only enable for one allowlisted tenant; status-only reply, no tenant or document ids in replies or logs."""

import json
import logging
import re
import threading
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from starlette.concurrency import run_in_threadpool

import lab_artifacts as ledger
import lab_artifacts_git
from routers.ops import _ops_secret, _require_ops

LOG = logging.getLogger(__name__)
ACTOR = "ops:lab-mirror-enable"  # Recorded actor for every enable this route runs.
_MAX_BODY = 4096  # Request body bound in bytes; larger bodies are refused before parsing.
_TENANT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:|@-]{0,127}\Z")  # Bounded tenant id shape.
_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")  # Only well-formed MirrorError codes reach a reply.
_RUNNING = threading.Lock()  # One enable at a time in this process.
_CONFLICT = frozenset({"lab_mirror_mode_refused", "lab_replay_divergence", "lab_ref_conflict",
                       "lab_publish_conflict", "lab_history_not_ancestor", "lab_history_nonlinear",
                       "lab_history_too_long", "lab_ops_ambiguous"})
router = APIRouter(tags=["lab_mirror_ops"])


@router.post("/internal/ops/lab/mirror/enable")
async def enable_lab_mirror(request: Request, x_ops_secret: Optional[str] = Header(default=None)):
    refusal = _require_ops(x_ops_secret)
    if refusal is not None:
        return refusal
    if _ops_secret() is None:
        raise HTTPException(status_code=503, detail="lab_mirror_ops_secret_not_configured")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_BODY:
            raise HTTPException(status_code=413, detail="lab_mirror_body_too_large")
        body.extend(chunk)
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise HTTPException(status_code=422, detail="lab_mirror_tenant_required")
    if (not isinstance(payload, dict) or set(payload) != {"tenant"}
            or not isinstance(payload["tenant"], str) or _TENANT.match(payload["tenant"]) is None):
        raise HTTPException(status_code=422, detail="lab_mirror_tenant_required")
    tenant = payload["tenant"]
    if not lab_artifacts_git._allowed(tenant):
        raise HTTPException(status_code=403, detail="lab_mirror_ineligible")
    if not _RUNNING.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="lab_mirror_enable_busy")
    try:
        status = await run_in_threadpool(_enable, tenant)
        return {"status": status}
    finally:
        _RUNNING.release()


def _done(state):
    return state.get("mode") == "mirror" and (state.get("receipt") or {}).get("status") == "complete"


def _enable(tenant):
    state = lab_artifacts_git.mirror_status(tenant)
    if _done(state):
        return "complete"
    try:
        state = lab_artifacts_git.enable_mirror(tenant, ACTOR)
    except lab_artifacts_git.MirrorError as error:
        code = error.code if isinstance(error.code, str) and _CODE.match(error.code) else "unknown"
        if code == "lab_mirror_mode_refused" and _done(lab_artifacts_git.mirror_status(tenant)):
            return "complete"
        raise HTTPException(status_code=_status(code), detail=code)
    except ledger.StoreMigrating:
        raise HTTPException(status_code=409, detail="lab_store_migrating")
    except ledger.StoreUnavailable:
        raise HTTPException(status_code=503, detail="lab_store_unavailable")
    except HTTPException:
        raise
    except Exception as error:
        LOG.warning("lab_mirror_enable_failed class=%s", type(error).__name__)
        raise HTTPException(status_code=503, detail="lab_mirror_unavailable")
    status = (state.get("receipt") or {}).get("status") if isinstance(state, dict) else None
    if status in ("complete", "in_progress"):
        return status
    raise HTTPException(status_code=409, detail="lab_mirror_not_complete")


def _status(code):
    if code == "lab_mirror_ineligible":
        return 403
    if code == "lab_replay_too_large":
        return 413
    if code in _CONFLICT:
        return 409
    return 503
