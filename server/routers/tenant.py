"""Per-tenant Claude grant linking (wave 4, CONTRACT-ADDENDUM §16).

The APP is a THIN PROXY to the harness per-tenant grant store — it NEVER persists,
logs, or echoes the token. The token flows app -> harness in ONE request body and is
never written to disk, a log line, or a response by this process. Status reads/echoes
carry only ``{linked, linked_at}`` (never the token).

    POST   /api/tenant/claude-grant  {token, kind?} -> §10 {linked:true, linked_at, kind}
    GET    /api/tenant/claude-grant                 -> §10 {linked:bool, linked_at, kind}
    DELETE /api/tenant/claude-grant                 -> §10 {linked:false, linked_at:null}

Grant KIND (§17): "oauth" for an owner-attested Claude subscription,
or "api_key" for a tenant-owned Anthropic API key. POST may carry an optional ``kind``
(else the harness auto-detects from the token prefix); status echoes the linked ``kind``
(never the token).

Tenant identity is the usual ``require_tenant`` (X-Tenant-Id stub, or the live JWT
claim). Harness unreachable / not configured -> BROKER_UNREACHABLE envelope (HTTP 502).
"""
from __future__ import annotations

import logging
import os
import urllib.parse
from typing import Any, Dict, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import deps
from envelopes import DEFAULT_HTTP_STATUS, ErrorCode, err_envelope, with_envelope_fields

router = APIRouter()
logger = logging.getLogger(__name__)


class GrantLinkRequest(BaseModel):
    token: str
    # Optional credential kind (§17): "oauth" (Claude subscription) or
    # "api_key" (tenant-owned API key). When omitted the harness AUTO-DETECTS from the token
    # prefix. Passed through to the harness; the token itself is never persisted app-side.
    kind: Optional[str] = None
    label: Optional[str] = None
    # OAuth mounts may join automatic routing only after the owner explicitly
    # attests which supported Claude subscription plan owns the credential.
    plan: Optional[Literal["pro", "max", "team", "enterprise"]] = None


class GrantActivateRequest(BaseModel):
    account_id: str


def _harness_url() -> str:
    return os.environ.get("LEAF_AUTHOR_HARNESS_URL", "").rstrip("/")


def _unreachable(detail: str) -> JSONResponse:
    # Safe operational detail only. Callers never pass the request body or
    # token here, so production logs can diagnose the app-to-harness hop
    # without weakening the grant's write-only contract.
    logger.warning("Claude grant harness unreachable: %s", detail)
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


def _require_grant_owner(tenant=Depends(deps.require_active_tenant)):
    """Require the current platform tenant owner for grant administration.

    Auth-off keeps the hermetic demo contract unchanged. In live mode, resolve
    the binding and role again at the point of mutation so a recent revoke or
    tenant move fails closed before the harness receives any request.
    """
    if not deps.auth_live():
        return tenant
    if not isinstance(tenant, deps.TenantContext) or not tenant.subject:
        raise HTTPException(status_code=403, detail="tenant owner authority required")
    try:
        import platform_link

        store = platform_link.platform_store()
        binding = store.resolve_active_identity_binding("auth0", tenant.subject)
        if binding is None or str(binding.platform_tenant_id) != str(tenant):
            raise HTTPException(status_code=403, detail="tenant owner authority required")
        role = store.active_identity_role(binding.platform_tenant_id, binding.binding_id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - authority outages fail closed
        raise HTTPException(
            status_code=503,
            detail="platform identity binding authority is unavailable",
        ) from exc
    if role != "owner":
        raise HTTPException(status_code=403, detail="tenant owner authority required")
    return tenant


def _rejected(status_code: int) -> JSONResponse:
    """Preserve a harness client-error status without echoing its body."""
    return JSONResponse(
        status_code=status_code,
        content=err_envelope(
            ErrorCode.BAD_PARAMS,
            "Claude account request rejected",
            retryable=False,
        ),
    )


def _status_body(harness_json: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize the harness status into a §10-enveloped {linked, linked_at, kind}.
    Defensive: only these safe fields are surfaced, so a token can NEVER be echoed even if
    a future harness bug returned one. `kind` (§17) is the credential kind, never a secret."""
    safe_accounts = []
    raw_accounts = harness_json.get("accounts")
    if isinstance(raw_accounts, list):
        for account in raw_accounts:
            if not isinstance(account, dict):
                continue
            if (not isinstance(account.get("id"), str)
                    or not isinstance(account.get("label"), str)
                    or account.get("kind") not in {"oauth", "api_key"}):
                continue
            safe_accounts.append({
                "id": account["id"],
                "label": account["label"],
                "kind": account["kind"],
                "linked_at": account.get("linked_at")
                if isinstance(account.get("linked_at"), str) else None,
                "active": bool(account.get("active", False)),
                "plan": account.get("plan")
                if account.get("plan") in {"pro", "max", "team", "enterprise"} else None,
                "eligible": bool(account.get("eligible", False)),
                "usage_tokens": account.get("usage_tokens")
                if isinstance(account.get("usage_tokens"), int)
                and account.get("usage_tokens") >= 0 else 0,
                "cooldown_until": account.get("cooldown_until")
                if isinstance(account.get("cooldown_until"), str) else None,
            })
    return with_envelope_fields({
        "linked": bool(harness_json.get("linked", False)),
        "linked_at": harness_json.get("linked_at"),
        "kind": harness_json.get("kind"),
        "active_account_id": harness_json.get("active_account_id")
        if isinstance(harness_json.get("active_account_id"), str) else None,
        "accounts": safe_accounts,
    })


def _diagnostic_body(harness_json: Dict[str, Any]) -> Dict[str, Any]:
    """Whitelist the frozen token-free diagnostic schema.

    Unknown fields are discarded so a harness defect cannot expose a path,
    filename, token fact, or other storage detail through the app.
    """
    owner = harness_json.get("owner")
    persistence = harness_json.get("persistence")
    owner = owner if isinstance(owner, dict) else {}
    persistence = persistence if isinstance(persistence, dict) else {}
    return with_envelope_fields({
        "schema": "leaf.grant-diagnostic.v1",
        "linked": bool(harness_json.get("linked", False)),
        "kind": harness_json.get("kind")
        if harness_json.get("kind") in {"oauth", "api_key", "missing"} else "missing",
        "linked_at": harness_json.get("linked_at")
        if isinstance(harness_json.get("linked_at"), str) else None,
        "backend": "file",
        "path_class": harness_json.get("path_class")
        if harness_json.get("path_class") in {"efs_access_point", "local_file", "environment"}
        else "local_file",
        "record_format": harness_json.get("record_format")
        if harness_json.get("record_format") in {
            "v1", "v2", "v3", "legacy", "environment", "missing", "invalid"
        } else "invalid",
        "legacy_fallback_present": bool(harness_json.get("legacy_fallback_present", False)),
        "owner": {
            "uid": owner.get("uid") if isinstance(owner.get("uid"), int) else None,
            "gid": owner.get("gid") if isinstance(owner.get("gid"), int) else None,
            "mode": owner.get("mode") if isinstance(owner.get("mode"), str) else None,
        },
        "persistence": {
            "atomic_publish": bool(persistence.get("atomic_publish", False)),
            "file_fsync": bool(persistence.get("file_fsync", False)),
            "directory_fsync": bool(persistence.get("directory_fsync", False)),
        },
        "degraded": bool(harness_json.get("degraded", True)),
    })


@router.get("/api/tenant/claude-grant/diagnostic")
def grant_diagnostic(tenant=Depends(_require_grant_owner)):
    """Return authenticated, token-free grant storage diagnostics."""
    url = _grant_admin_url(str(tenant))
    if url is None:
        return _unreachable("LEAF_AUTHOR_HARNESS_URL not configured")
    try:
        import requests
        import broker_client
        r = requests.get(
            f"{url}/diagnostic",
            headers=broker_client.harness_headers(),
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        return _unreachable(f"harness request failed: {type(exc).__name__}")
    if r.status_code >= 500:
        return _unreachable(f"harness returned HTTP {r.status_code}")
    try:
        hj = r.json()
    except ValueError:
        return _unreachable("harness returned non-JSON")
    return deps.tenant_echo(_diagnostic_body(hj), tenant)


@router.post("/api/tenant/claude-grant")
def link_grant(req: GrantLinkRequest, tenant=Depends(_require_grant_owner)):
    """Mount a supported Claude subscription or API grant for THIS tenant. The token is
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
    if req.label is not None:
        payload["label"] = req.label
    if req.plan is not None:
        payload["plan"] = req.plan
    try:
        import requests
        import broker_client
        r = requests.put(url, json=payload, headers=broker_client.harness_headers(), timeout=30)
    except Exception as exc:  # noqa: BLE001  (connection/timeout/etc.)
        return _unreachable(f"harness request failed: {type(exc).__name__}")
    if r.status_code >= 500:
        return _unreachable(f"harness returned HTTP {r.status_code}")
    if r.status_code >= 400:
        return _rejected(r.status_code)
    try:
        hj = r.json()
    except ValueError:
        return _unreachable("harness returned non-JSON")
    _emit_grant_event("grant.linked", tenant, hj, req.kind)
    return deps.tenant_echo(_status_body(hj), tenant)


# The only kind vocabulary telemetry may carry: the request model accepts any
# string (the harness ignores invalid values and auto-detects), so emitting a
# raw req.kind would let a caller place arbitrary text (or a token) into
# telemetry (review #426 round-1 blocker 3).
_GRANT_KINDS = frozenset({"oauth", "api_key"})


def _emit_grant_event(name: str, tenant, harness_body, requested_kind=None) -> None:
    """Best-effort grant.linked / grant.unlinked product event (P2): the
    unlock for Build + chat. `kind` is ALLOWLISTED: the harness response's
    kind wins when valid, else a valid requested kind, else the label is
    omitted entirely. NEVER the token; NEVER raises."""
    try:
        import telemetry_sink

        kind = None
        if isinstance(harness_body, dict):
            for key in ("kind", "grant_kind"):
                if harness_body.get(key) in _GRANT_KINDS:
                    kind = harness_body[key]
                    break
        if kind is None and requested_kind in _GRANT_KINDS:
            kind = requested_kind
        tid = str(tenant)
        telemetry_sink.emit(
            name,
            tenant_id=tid,
            tenant_kind="guest" if tid.startswith("guest-") else "account",
            session_id="server",
            labels={"kind": kind} if kind else None,
        )
    except Exception:  # noqa: BLE001 - telemetry never touches the grant flow
        pass


@router.patch("/api/tenant/claude-grant")
def activate_grant(req: GrantActivateRequest, tenant=Depends(_require_grant_owner)):
    """Select one of THIS tenant's linked accounts as the authoring credential."""
    url = _grant_admin_url(str(tenant))
    if url is None:
        return _unreachable("LEAF_AUTHOR_HARNESS_URL not configured")
    try:
        import requests
        import broker_client
        r = requests.patch(
            url,
            json={"account_id": req.account_id},
            headers=broker_client.harness_headers(),
            timeout=30,
        )
    except Exception as exc:  # noqa: BLE001
        return _unreachable(f"harness request failed: {type(exc).__name__}")
    if r.status_code >= 500:
        return _unreachable(f"harness returned HTTP {r.status_code}")
    if r.status_code >= 400:
        return _rejected(r.status_code)
    try:
        hj = r.json()
    except ValueError:
        return _unreachable("harness returned non-JSON")
    return deps.tenant_echo(_status_body(hj), tenant)


@router.get("/api/tenant/claude-grant")
def grant_status(tenant=Depends(_require_grant_owner)):
    """Whether THIS tenant has a linked grant. Never returns the token."""
    url = _grant_admin_url(str(tenant))
    if url is None:
        return _unreachable("LEAF_AUTHOR_HARNESS_URL not configured")
    try:
        import requests
        import broker_client
        r = requests.get(url, headers=broker_client.harness_headers(), timeout=30)
    except Exception as exc:  # noqa: BLE001
        return _unreachable(f"harness request failed: {type(exc).__name__}")
    if r.status_code >= 500:
        return _unreachable(f"harness returned HTTP {r.status_code}")
    try:
        hj = r.json()
    except ValueError:
        return _unreachable("harness returned non-JSON")
    return deps.tenant_echo(_status_body(hj), tenant)


@router.delete("/api/tenant/claude-grant")
def unlink_grant(account_id: Optional[str] = None, tenant=Depends(_require_grant_owner)):
    """Unlink THIS tenant's grant."""
    url = _grant_admin_url(str(tenant))
    if url is None:
        return _unreachable("LEAF_AUTHOR_HARNESS_URL not configured")
    try:
        import requests
        import broker_client
        params = {"account_id": account_id} if account_id else None
        r = requests.delete(url, params=params, headers=broker_client.harness_headers(), timeout=30)
    except Exception as exc:  # noqa: BLE001
        return _unreachable(f"harness request failed: {type(exc).__name__}")
    if r.status_code >= 500:
        return _unreachable(f"harness returned HTTP {r.status_code}")
    if r.status_code >= 400:
        return _rejected(r.status_code)
    try:
        hj = r.json()
    except ValueError:
        # a bodyless 200/204 delete is fine -> report unlinked.
        hj = {"linked": False, "linked_at": None}
    _emit_grant_event("grant.unlinked", tenant, hj)
    return deps.tenant_echo(_status_body(hj), tenant)
