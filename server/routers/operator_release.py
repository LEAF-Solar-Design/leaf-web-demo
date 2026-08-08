"""Operator staging release runbook surface (contract/OPERATOR.md section 4.1,
operator.stage_release_candidate). Additive namespace
/api/operator/release/*. Always-confirm, gated by a one-use authority; STAGING
ONLY; ships dark (enabled:false in operator_policy.json) and mounts only under
LEAF_OPERATOR_ENABLED=1. No route names or reaches a production deploy path.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import operator_deps
from operator_deps import OperatorContext

router = APIRouter()


def _rb():
    import operator_stage_release_runbook
    return operator_stage_release_runbook


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
    source_sha: str
    target: str
    model_config = {"extra": "forbid"}


class ExecuteBody(BaseModel):
    source_sha: str
    target: str
    authority_id: str
    model_config = {"extra": "forbid"}


@router.get("/api/operator/release/{target}/{source_sha}/state")
def release_state(target: str, source_sha: str,
                  op: OperatorContext = Depends(operator_deps.require_operator)) -> Dict[str, Any]:
    return _wrap(_rb().resolve_candidate, source_sha, target)


@router.post("/api/operator/release/propose")
def release_propose(body: ProposeBody,
                    op: OperatorContext = Depends(operator_deps.require_operator)) -> Dict[str, Any]:
    return _wrap(_rb().propose, op, body.source_sha, body.target)


@router.post("/api/operator/release/execute")
def release_execute(body: ExecuteBody,
                    op: OperatorContext = Depends(operator_deps.require_operator)) -> Dict[str, Any]:
    return _wrap(_rb().execute, op, body.source_sha, body.target,
                 body.authority_id)
