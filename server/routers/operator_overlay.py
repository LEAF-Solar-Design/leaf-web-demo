"""Operator overlay runbook surface (contract/OPERATOR.md Lane F). Additive
namespace /api/operator/runbooks/tenant-overlay/*. Always-confirm, gated by a
one-use authority; ships dark (enabled:false in operator_policy.json) and
mounts only under LEAF_OPERATOR_ENABLED=1.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import operator_deps
from operator_deps import OperatorContext

router = APIRouter()


def _rb():
    import operator_overlay_runbook
    return operator_overlay_runbook


def _wrap(fn, *args, **kwargs):
    import operator_runbook_tx
    import operator_authority
    try:
        return fn(*args, **kwargs)
    except (operator_runbook_tx.RunbookError,
            operator_authority.AuthorityDenied) as exc:
        # Precondition / drift / validation / denied authority -> 409.
        raise HTTPException(status_code=409, detail=exc.reason) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - store/authority backend down
        raise HTTPException(status_code=503,
                            detail="operator_runbook_unavailable") from exc


class ProposeBody(BaseModel):
    tenant_id: str
    overlay: Dict[str, Any]
    model_config = {"extra": "forbid"}


class ExecuteBody(BaseModel):
    tenant_id: str
    overlay: Dict[str, Any]
    authority_id: str
    model_config = {"extra": "forbid"}


@router.get("/api/operator/runbooks/tenant-overlay/{tenant_id}/state")
def overlay_state(tenant_id: str,
                  op: OperatorContext = Depends(operator_deps.require_operator)) -> Dict[str, Any]:
    return _wrap(_rb().resolve_target, tenant_id)


@router.post("/api/operator/runbooks/tenant-overlay/propose")
def overlay_propose(body: ProposeBody,
                    op: OperatorContext = Depends(operator_deps.require_operator)) -> Dict[str, Any]:
    return _wrap(_rb().propose, op, body.tenant_id, body.overlay)


@router.post("/api/operator/runbooks/tenant-overlay/execute")
def overlay_execute(body: ExecuteBody,
                    op: OperatorContext = Depends(operator_deps.require_operator)) -> Dict[str, Any]:
    return _wrap(_rb().execute, op, body.tenant_id, body.overlay,
                 body.authority_id)
