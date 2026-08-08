"""Operator credential-rotation runbook surface (contract/OPERATOR.md section
4.1, operator.worker_credential_rotate). Additive namespace
/api/operator/runbooks/credential/*. Always-confirm, gated by a one-use
authority; ships dark (enabled:false in operator_policy.json) and mounts only
under LEAF_OPERATOR_ENABLED=1. No route ever returns a secret value.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import operator_deps
from operator_deps import OperatorContext

router = APIRouter()


def _rb():
    import operator_credential_rotate_runbook
    return operator_credential_rotate_runbook


def _wrap(fn, *args, **kwargs):
    import operator_authority
    rb = _rb()
    try:
        return fn(*args, **kwargs)
    except (rb.RunbookError, operator_authority.AuthorityDenied) as exc:
        # Precondition / drift / validation / denied authority -> 409.
        raise HTTPException(status_code=409, detail=exc.reason) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - store/authority backend down
        raise HTTPException(status_code=503,
                            detail="operator_runbook_unavailable") from exc


class ProposeBody(BaseModel):
    credential_handle: str
    model_config = {"extra": "forbid"}


class ExecuteBody(BaseModel):
    credential_handle: str
    authority_id: str
    model_config = {"extra": "forbid"}


@router.get("/api/operator/runbooks/credential/{handle}/state")
def credential_state(handle: str,
                     op: OperatorContext = Depends(operator_deps.require_operator)) -> Dict[str, Any]:
    return _wrap(_rb().resolve_rotation_state, handle)


@router.post("/api/operator/runbooks/credential/propose")
def credential_propose(body: ProposeBody,
                       op: OperatorContext = Depends(operator_deps.require_operator)) -> Dict[str, Any]:
    return _wrap(_rb().propose, op, body.credential_handle)


@router.post("/api/operator/runbooks/credential/execute")
def credential_execute(body: ExecuteBody,
                       op: OperatorContext = Depends(operator_deps.require_operator)) -> Dict[str, Any]:
    return _wrap(_rb().execute, op, body.credential_handle, body.authority_id)
