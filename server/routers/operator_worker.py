"""Operator worker surface (contract/OPERATOR.md Lane D). CAPABILITY-ONLY:
POST /api/operator/worker/dispatch validates the operator principal and
DISPATCHES a bounded job to the isolated disposable worker. It NEVER executes
the commands in the app process. Broad/privileged operator work runs only in the
egress-locked worker, so a handler cannot reach a production deploy route.

Ships dark: mounting the router alone grants no broad-execution capability (the
worker fails closed on a non-isolating substrate, and network is always denied).
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

import operator_authority
import operator_deps
import deps as tenant_deps
from operator_deps import OperatorContext

router = APIRouter()


class WorkerDispatchBody(BaseModel):
    # Reject unknown fields outright (sibling-router pattern): a dropped field
    # must be a 422, never a silent ignore.
    model_config = {"extra": "forbid"}

    commands: List[str] = Field(..., min_length=1, max_length=50)
    repo: Optional[str] = None
    timeout_ms: Optional[int] = Field(default=None, ge=1, le=1_800_000)


class WorkerCancelBody(BaseModel):
    """The browser can name a worker/run pair, but never its owner or tenant.

    Those two identities come only from the authenticated request dependencies.
    This prevents a caller from turning an otherwise valid pair into a
    cross-owner or cross-tenant cancellation request.
    """
    model_config = {"extra": "forbid"}

    worker_id: str = Field(..., min_length=1, max_length=256)
    run_id: str = Field(..., min_length=1, max_length=256)


@router.post("/api/operator/worker/dispatch")
def dispatch_worker(body: WorkerDispatchBody,
                    op: OperatorContext = Depends(operator_deps.require_operator)):
    # Admission step 1 (contract section 5): the kill switch denies every
    # operator write path, this one included. 409 like the sibling runbooks.
    if operator_authority.kill_switch_active():
        raise HTTPException(status_code=409, detail="kill_switch_active")
    # Capability-only: forward to the isolated worker. This handler runs no
    # command itself.
    import operator_worker_dispatch as dispatch
    try:
        return dispatch.dispatch_to_isolated_worker(
            op, body.commands, repo=body.repo, timeout_ms=body.timeout_ms)
    except dispatch.OperatorWorkerError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.reason) from exc
    except Exception as exc:  # harness unreachable etc. -> fail closed
        raise HTTPException(
            status_code=503, detail="operator_worker_unavailable") from exc


@router.post("/api/operator/worker/cancel")
def cancel_worker(
    body: WorkerCancelBody,
    op: OperatorContext = Depends(operator_deps.require_operator),
    tenant=Depends(tenant_deps.require_tenant),
):
    """Cancel exactly one active worker run after server-side authorization.

    ``worker_id`` and ``run_id`` are an opaque browser-selected target pair.
    The dispatch module first resolves that pair against the control plane and
    checks its current state, owner, and tenant against this request's trusted
    identities.  Only then may it invoke the bounded stop primitive.
    """
    if operator_authority.kill_switch_active():
        raise HTTPException(status_code=409, detail="kill_switch_active")
    # The operator principal registry can contain narrower roles for read-only
    # console surfaces. Cancellation is an O2 control action, never one of
    # those roles' capabilities.
    if op.role != "operator":
        raise HTTPException(status_code=404, detail="operator_not_found")
    import operator_worker_dispatch as dispatch
    try:
        return dispatch.cancel_exact_active_worker(
            op, str(tenant), body.worker_id, body.run_id)
    except dispatch.OperatorWorkerError as exc:
        raise HTTPException(status_code=exc.http_status, detail=exc.reason) from exc
    except Exception as exc:  # control-plane failure must never become a cancel
        raise HTTPException(
            status_code=503, detail="operator_worker_unavailable") from exc
