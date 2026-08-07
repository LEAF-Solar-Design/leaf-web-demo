"""Operator secret-broker read surface (contract/OPERATOR.md Wave 3). Additive
namespace /api/operator/secrets. Returns handle METADATA only (scope,
environment, kind, TTL) — never a secret value. Injection is never exposed as
an HTTP route: a value is only ever minted and injected into one adapter call
server-side, so there is no endpoint that can return a credential. Mounts only
under LEAF_OPERATOR_ENABLED=1.
"""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException

import operator_deps
from operator_deps import OperatorContext

router = APIRouter()


def _broker():
    import operator_secret_broker
    return operator_secret_broker


def _wrap(fn, *args, **kwargs):
    broker = _broker()
    try:
        return fn(*args, **kwargs)
    except broker.SecretBrokerError as exc:
        raise HTTPException(status_code=409, detail=exc.reason) from exc
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503,
                            detail="operator_secret_broker_unavailable") from exc


@router.get("/api/operator/secrets")
def list_secrets(op: OperatorContext = Depends(operator_deps.require_operator)) -> Dict[str, Any]:
    return {"handles": _wrap(_broker().list_handles)}


@router.get("/api/operator/secrets/{handle}")
def describe_secret(handle: str,
                    op: OperatorContext = Depends(operator_deps.require_operator)) -> Dict[str, Any]:
    meta = _wrap(_broker().describe, handle)
    if meta is None:
        raise HTTPException(status_code=404, detail="secret_handle_not_found")
    return meta
