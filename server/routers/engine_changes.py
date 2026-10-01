"""Machine-published acceptance cards and two-factor admin receipts."""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request

import deps
import platform_link
from routers.ops import _ops_secret, _require_ops

router = APIRouter()
_MAX_BODY = 128 * 1024


def _store():
    try:
        store = platform_link.engine_change_cards_store()
        platform_link.platform_db().get_database_url()
        return store
    except Exception as exc:
        raise HTTPException(status_code=503, detail="engine_change_cards_database_unavailable") from exc


def _call(method, *args, **kwargs):
    store = _store()
    try:
        return getattr(store, method)(*args, **kwargs)
    except store.CardConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        # Do not expose SQL, connection strings or the publisher's body.
        raise HTTPException(status_code=503, detail="engine_change_cards_database_unavailable") from exc


def _found(value):
    if value is None:
        raise HTTPException(status_code=404, detail="engine_change_card_not_found")
    return value


def _require_admin(tenant=Depends(deps.require_active_tenant)):
    # The active tenant dependency resolves the verified token and the existing
    # two-factor admin elevation; request headers/body never grant this tier.
    if (getattr(tenant, "tier", None) != "admin"
            or not isinstance(getattr(tenant, "subject", None), str)
            or not tenant.subject.strip()):
        raise HTTPException(status_code=403, detail="engine_changes_admin_required")
    return tenant


@router.post("/internal/ops/engine-changes/cards")
async def ingest_card(request: Request, x_ops_secret: Optional[str] = Header(default=None)):
    refusal = _require_ops(x_ops_secret)
    if refusal is not None:
        return refusal
    # These durable machine records require a credential even in an auth-off demo.
    if _ops_secret() is None:
        raise HTTPException(status_code=503, detail="engine_change_cards_ops_secret_not_configured")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_BODY:
            raise HTTPException(status_code=413, detail="engine_change_card_body_too_large")
        body.extend(chunk)
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise HTTPException(status_code=422, detail="invalid_engine_change_card_json") from exc
    if not isinstance(payload, dict) or "operation_id" not in payload:
        raise HTTPException(status_code=422, detail="operation_id_and_card_fields_required")
    operation_id = payload.pop("operation_id")
    return _call("upsert_card", operation_id, payload)


@router.get("/api/engine-changes")
def list_cards(
    limit: int = Query(default=100, ge=1, le=100),
    before: Optional[str] = Query(default=None, max_length=36),
    tenant=Depends(_require_admin),
):
    return _call("list_cards", tenant.subject, limit=limit, before=before)


@router.get("/api/engine-changes/{card_id}")
def get_card(card_id: str, tenant=Depends(_require_admin)):
    return _found(_call("get_card", card_id))


@router.post("/api/engine-changes/{card_id}/read")
def mark_read(card_id: str, tenant=Depends(_require_admin)):
    return _found(_call("mark_read", card_id, tenant.subject))


@router.post("/api/engine-changes/{card_id}/hold-request")
def request_hold(card_id: str, tenant=Depends(_require_admin)):
    return _found(_call("request_hold", card_id, tenant.subject))
