"""Per-tenant Claude grant linking (wave 4, CONTRACT-ADDENDUM §16).

The APP is a THIN PROXY to the harness per-tenant grant store — it NEVER persists,
logs, or echoes the token. The token flows app -> harness in ONE request body and is
never written to disk, a log line, or a response by this process. Status reads/echoes
carry only ``{linked, linked_at}`` (never the token).

    POST   /api/tenant/claude-grant  {token, kind?} -> §10 {linked:true, linked_at, kind}
    GET    /api/tenant/claude-grant                 -> §10 {linked:bool, linked_at, kind}
    DELETE /api/tenant/claude-grant                 -> §10 {linked:false, linked_at:null}

Grant KIND (§17): "oauth" (per-user "sign in with Claude") or "api_key" (enterprise BYO
key). POST may carry an optional ``kind`` (else the harness auto-detects from the token
prefix); status echoes the linked ``kind`` (never the token).

Tenant identity is the usual ``require_tenant`` (X-Tenant-Id stub, or the live JWT
claim). Harness unreachable / not configured -> BROKER_UNREACHABLE envelope (HTTP 502).
"""
from __future__ import annotations

import os
import urllib.parse
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import deps
from envelopes import DEFAULT_HTTP_STATUS, ErrorCode, err_envelope, with_envelope_fields

router = APIRouter()


class GrantLinkRequest(BaseModel):
    token: str
    # Optional credential kind (§17): "oauth" (per-user "sign in with Claude") or
    # "api_key" (enterprise BYO key). When omitted the harness AUTO-DETECTS from the token
    # prefix. Passed through to the harness; the token itself is never persisted app-side.
    kind: Optional[str] = None


def _harness_url() -> str:
    return os.environ.get("LEAF_AUTHOR_HARNESS_URL", "").rstrip("/")


def _unreachable(detail: str) -> JSONResponse:
    return JSONResponse(
        status_code=DEFAULT_HTTP_STATUS[ErrorCode.BROKER_UNREACHABLE],  # 502
        content=err_envelope(ErrorCode.BROKER_UNREACHABLE,
                             f"author harness unreachable: {detail}", retryable=True),
    )


def _grant_admin_url(tenant: str) -> Optional[str]:
    base = _harness_url()
    if not base:
        return None
    return f"{base}/grants/{urllib.parse.quote(str(tenant), safe='')}"


def _status_body(harness_json: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize the harness status into a §10-enveloped {linked, linked_at, kind}.
    Defensive: only these safe fields are surfaced, so a token can NEVER be echoed even if
    a future harness bug returned one. `kind` (§17) is the credential kind, never a secret."""
    return with_envelope_fields({
        "linked": bool(harness_json.get("linked", False)),
        "linked_at": harness_json.get("linked_at"),
        "kind": harness_json.get("kind"),
    })


@router.post("/api/tenant/claude-grant")
def link_grant(req: GrantLinkRequest, tenant=Depends(deps.require_tenant)):
    """Link (or replace) THIS tenant's 'sign in with Claude' grant. The token is
    forwarded to the harness store in this one request and is NEVER persisted, logged,
    or echoed by the app."""
    url = _grant_admin_url(str(tenant))
    if url is None:
        return _unreachable("LEAF_AUTHOR_HARNESS_URL not configured")
    # Forward the token (+ optional kind) in ONE request; kind is included only when the
    # caller set it, so the harness auto-detects otherwise. The token is NEVER persisted,
    # logged, or echoed by the app.
    payload: Dict[str, Any] = {"token": req.token}
    if req.kind is not None:
        payload["kind"] = req.kind
    try:
        import requests
        import broker_client
        r = requests.put(url, json=payload, headers=broker_client.harness_headers(), timeout=30)
    except Exception as exc:  # noqa: BLE001  (connection/timeout/etc.)
        return _unreachable(str(exc))
    if r.status_code >= 500:
        return _unreachable(f"harness returned HTTP {r.status_code}")
    try:
        hj = r.json()
    except ValueError:
        return _unreachable("harness returned non-JSON")
    return deps.tenant_echo(_status_body(hj), tenant)


@router.get("/api/tenant/claude-grant")
def grant_status(tenant=Depends(deps.require_tenant)):
    """Whether THIS tenant has a linked grant. Never returns the token."""
    url = _grant_admin_url(str(tenant))
    if url is None:
        return _unreachable("LEAF_AUTHOR_HARNESS_URL not configured")
    try:
        import requests
        import broker_client
        r = requests.get(url, headers=broker_client.harness_headers(), timeout=30)
    except Exception as exc:  # noqa: BLE001
        return _unreachable(str(exc))
    if r.status_code >= 500:
        return _unreachable(f"harness returned HTTP {r.status_code}")
    try:
        hj = r.json()
    except ValueError:
        return _unreachable("harness returned non-JSON")
    return deps.tenant_echo(_status_body(hj), tenant)


@router.delete("/api/tenant/claude-grant")
def unlink_grant(tenant=Depends(deps.require_tenant)):
    """Unlink THIS tenant's grant."""
    url = _grant_admin_url(str(tenant))
    if url is None:
        return _unreachable("LEAF_AUTHOR_HARNESS_URL not configured")
    try:
        import requests
        import broker_client
        r = requests.delete(url, headers=broker_client.harness_headers(), timeout=30)
    except Exception as exc:  # noqa: BLE001
        return _unreachable(str(exc))
    if r.status_code >= 500:
        return _unreachable(f"harness returned HTTP {r.status_code}")
    try:
        hj = r.json()
    except ValueError:
        # a bodyless 200/204 delete is fine -> report unlinked.
        hj = {"linked": False, "linked_at": None}
    return deps.tenant_echo(_status_body(hj), tenant)
