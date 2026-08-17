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
from operator_deps import OperatorContext

router = APIRouter()


class WorkerDispatchBody(BaseModel):
    # Reject unknown fields outright (sibling-router pattern): a dropped field
    # must be a 422, never a silent ignore.
    model_config = {"extra": "forbid"}

    commands: List[str] = Field(..., min_length=1, max_length=50)
    repo: Optional[str] = None
    timeout_ms: Optional[int] = Field(default=None, ge=1, le=1_800_000)


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
