"""Private HTTP adapter for the P8 repository-edit coordinator.

The harness may reach only the four closed coordination operations through
this router.  Repository selection, actor roles, durable state, and Git work
remain owned by the already-landed coordination layers.
"""
from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, Body, Header
from fastapi.responses import JSONResponse

import deps
from project_repository_edit_coordination import (
    CoordinationError,
    RepositoryEditCoordinationState,
    handle_authorize_publish,
    handle_record_staged,
    handle_recover_publish,
    handle_settle_publish,
)

router = APIRouter()

_state = RepositoryEditCoordinationState()
_AUTHORITY_DENIED = {"detail": "dispatch authority unavailable"}
_COORDINATION_UNAVAILABLE = {"detail": "repository edit unavailable"}


def _body_tenant(body: object, *, receipt_owned: bool) -> str | None:
    if type(body) is not dict:
        return None
    record = body
    if receipt_owned:
        receipt = record.get("receipt")
        if type(receipt) is not dict:
            return None
        value = receipt.get("tenant_id")
    else:
        value = record.get("tenant_id")
    return value if isinstance(value, str) else None


def _dispatch(
    handler: Callable[[RepositoryEditCoordinationState, object], dict],
    body: object,
    *,
    tenant_id: str | None,
    dispatch_secret: str | None,
    receipt_owned: bool = False,
) -> Any:
    if not tenant_id or not deps._dispatch_secret_ok(dispatch_secret):
        return JSONResponse(status_code=401, content=_AUTHORITY_DENIED)
    if _body_tenant(body, receipt_owned=receipt_owned) != tenant_id:
        return JSONResponse(status_code=404, content=_COORDINATION_UNAVAILABLE)
    try:
        return handler(_state, body)
    except CoordinationError:
        return JSONResponse(status_code=404, content=_COORDINATION_UNAVAILABLE)
    except Exception:
        return JSONResponse(status_code=503, content=_COORDINATION_UNAVAILABLE)


@router.post("/internal/project-repository-edit/record-staged")
def record_staged(
    body: Any = Body(...),
    x_tenant_id: str | None = Header(default=None),
    x_dispatch_secret: str | None = Header(default=None),
) -> Any:
    return _dispatch(
        handle_record_staged,
        body,
        tenant_id=x_tenant_id,
        dispatch_secret=x_dispatch_secret,
        receipt_owned=True,
    )


@router.post("/internal/project-repository-edit/authorize-publish")
def authorize_publish(
    body: Any = Body(...),
    x_tenant_id: str | None = Header(default=None),
    x_dispatch_secret: str | None = Header(default=None),
) -> Any:
    return _dispatch(
        handle_authorize_publish,
        body,
        tenant_id=x_tenant_id,
        dispatch_secret=x_dispatch_secret,
    )


@router.post("/internal/project-repository-edit/settle-publish")
def settle_publish(
    body: Any = Body(...),
    x_tenant_id: str | None = Header(default=None),
    x_dispatch_secret: str | None = Header(default=None),
) -> Any:
    return _dispatch(
        handle_settle_publish,
        body,
        tenant_id=x_tenant_id,
        dispatch_secret=x_dispatch_secret,
    )


@router.post("/internal/project-repository-edit/recover-publish")
def recover_publish(
    body: Any = Body(...),
    x_tenant_id: str | None = Header(default=None),
    x_dispatch_secret: str | None = Header(default=None),
) -> Any:
    return _dispatch(
        handle_recover_publish,
        body,
        tenant_id=x_tenant_id,
        dispatch_secret=x_dispatch_secret,
    )
