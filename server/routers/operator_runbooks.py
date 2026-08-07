"""Operator runbook surface (contract/OPERATOR.md Lane F). Additive namespace
/api/operator/runbooks/*. Every route resolves the principal fresh via
require_operator. Write actions are always-confirm and gated by a one-use
execution authority (Lane B); they ship dark (the action is enabled:false in
operator_policy.json) so mounting alone grants nothing.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import operator_deps
from operator_deps import OperatorContext

router = APIRouter()

_ACTIONS = {"pause": "operator.tenant_agent_pause",
            "resume": "operator.tenant_agent_resume"}


def _runbooks():
    import operator_runbooks
    return operator_runbooks


def _wrap(fn, *args, **kwargs):
    runbooks = _runbooks()
    try:
        return fn(*args, **kwargs)
    except runbooks.RunbookError as exc:
        # Precondition / drift / not-revisioned are 409 conflicts.
        raise HTTPException(status_code=409, detail=exc.reason) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - store/authority failure = 503
        # AuthorityDenied (bad/expired/replayed/drifted authority) also lands
        # here; surface it as a 409 the operator can act on, everything else
        # is an unavailable backend.
        reason = getattr(exc, "reason", None)
        if reason:
            raise HTTPException(status_code=409, detail=reason) from exc
        raise HTTPException(status_code=503,
                            detail="operator_runbook_unavailable") from exc


class TenantBody(BaseModel):
    tenant_id: str
    model_config = {"extra": "forbid"}


class ExecuteBody(BaseModel):
    tenant_id: str
    authority_id: str
    model_config = {"extra": "forbid"}


@router.get("/api/operator/runbooks/tenant-agent/{tenant_id}/state")
def tenant_state(tenant_id: str,
                 op: OperatorContext = Depends(operator_deps.require_operator)) -> Dict[str, Any]:
    """Server-owned target resolution (read-only)."""
    return _wrap(_runbooks().resolve_tenant_agent_target, tenant_id)


@router.post("/api/operator/runbooks/tenant-agent/{verb}/propose")
def propose(verb: str, body: TenantBody,
            op: OperatorContext = Depends(operator_deps.require_operator)) -> Dict[str, Any]:
    action = _ACTIONS.get(verb)
    if action is None:
        raise HTTPException(status_code=404, detail="unknown_runbook")
    return _wrap(_runbooks().propose, op, action, body.tenant_id)


@router.post("/api/operator/runbooks/tenant-agent/{verb}/execute")
def execute(verb: str, body: ExecuteBody,
            op: OperatorContext = Depends(operator_deps.require_operator)) -> Dict[str, Any]:
    action = _ACTIONS.get(verb)
    if action is None:
        raise HTTPException(status_code=404, detail="unknown_runbook")
    return _wrap(_runbooks().execute, op, action, body.tenant_id,
                 body.authority_id)
