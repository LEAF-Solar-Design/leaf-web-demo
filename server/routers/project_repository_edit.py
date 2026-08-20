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


def _bytes_class(size_bytes: int | None) -> str:
    """Bucketed request-body size, never the raw byte count or its content."""
    if size_bytes is None:
        return "unknown"
    if size_bytes <= 4 * 1024:
        return "xs"
    if size_bytes <= 32 * 1024:
        return "s"
    if size_bytes <= 256 * 1024:
        return "m"
    if size_bytes <= 2 * 1024 * 1024:
        return "l"
    return "xl"


def _emit_edit_applied(body: object, result: object) -> None:
    """Best-effort project.edit_applied (TEL-4). ``record_staged`` is the ONE
    place a project edit's content enters this system -- authorize/settle/
    recover only lease, consume, or settle an edit already recorded here --
    so this is the single write path the acceptance oracle requires be
    observable. Fires only when ``_dispatch`` actually reached the handler
    and it returned a coordination result (never on a 401/404/503 denial).
    Labels carry a changed-path COUNT and a bucketed body-size class, never
    a path, a digest, or any other payload content. NEVER raises.
    """
    if type(result) is not dict:
        return
    try:
        import json as _json

        import telemetry_sink

        receipt = body.get("receipt") if type(body) is dict else None
        changed_paths = receipt.get("changed_paths") if type(receipt) is dict else None
        files_changed = len(changed_paths) if isinstance(changed_paths, list) else None
        tenant_id = receipt.get("tenant_id") if type(receipt) is dict else None
        try:
            body_size = len(_json.dumps(body))
        except Exception:  # noqa: BLE001
            body_size = None
        telemetry_sink.emit(
            "project.edit_applied",
            tenant_id=str(tenant_id) if isinstance(tenant_id, str) else "anon",
            tenant_kind="account",
            session_id="server",
            labels={"files_changed": files_changed, "bytes_class": _bytes_class(body_size)},
        )
    except Exception:  # noqa: BLE001 - telemetry never touches the response
        pass


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
    result = _dispatch(
        handle_record_staged,
        body,
        tenant_id=x_tenant_id,
        dispatch_secret=x_dispatch_secret,
        receipt_owned=True,
    )
    _emit_edit_applied(body, result)
    return result


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
