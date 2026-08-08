"""Operator external-write runbook surface (contract/OPERATOR.md section 4.1,
operator.external_write). Additive namespace /api/operator/external/*.
Always-confirm, gated by a one-use authority; ships dark (enabled:false in
operator_policy.json) and mounts only under LEAF_OPERATOR_ENABLED=1. No route
returns a token value, and the destination allowlist is metadata only.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import operator_deps
from operator_deps import OperatorContext

router = APIRouter()


def _rb():
    import operator_external_write_runbook
    return operator_external_write_runbook


def _wrap(fn, *args, **kwargs):
    import operator_authority
    rb = _rb()
    try:
        return fn(*args, **kwargs)
    except (rb.RunbookError, operator_authority.AuthorityDenied) as exc:
        raise HTTPException(status_code=409, detail=exc.reason) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - store/authority backend down
        raise HTTPException(status_code=503,
                            detail="operator_runbook_unavailable") from exc


class ProposeBody(BaseModel):
    destination: str
    token_handle: str
    adapter: str
    payload: Optional[Dict[str, Any]] = None
    model_config = {"extra": "forbid"}


class ExecuteBody(BaseModel):
    destination: str
    token_handle: str
    adapter: str
    authority_id: str
    payload: Optional[Dict[str, Any]] = None
    model_config = {"extra": "forbid"}


@router.get("/api/operator/external/destinations")
def external_destinations(op: OperatorContext = Depends(operator_deps.require_operator)) -> Dict[str, Any]:
    import operator_external_adapters as ext
    return {"destinations": _wrap(ext.list_destinations)}


@router.post("/api/operator/external/propose")
def external_propose(body: ProposeBody,
                     op: OperatorContext = Depends(operator_deps.require_operator)) -> Dict[str, Any]:
    return _wrap(_rb().propose, op, body.destination, body.token_handle,
                 body.adapter, body.payload)


@router.post("/api/operator/external/execute")
def external_execute(body: ExecuteBody,
                     op: OperatorContext = Depends(operator_deps.require_operator)) -> Dict[str, Any]:
    return _wrap(_rb().execute, op, body.destination, body.token_handle,
                 body.adapter, body.authority_id, body.payload)
