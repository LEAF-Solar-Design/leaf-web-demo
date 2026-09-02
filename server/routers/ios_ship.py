"""Browser-safe Wave D iOS readiness, launch, execution, and receipt routes."""
from __future__ import annotations

import importlib.util
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import deps
import entitlements
import telemetry_sink

router = APIRouter()
_DISPATCH: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None
_PROVIDER_READINESS: Optional[Callable[[Dict[str, str]], Dict[str, Any]]] = None
_SETUP_ACTION = "mount-apple-ship-dispatch"
_READINESS_KIND = "leaf.ios-ship-readiness.v1"
_SECRET_KEY_RE = re.compile(
    r"(password|passwd|two.?factor|2fa|otp|p8|\.p8|private[_ -]?key|certificate|"
    r"provisioning|profile|credential|secret|keychain|authkey|token|session|cookie|"
    r"api[_ -]?key|signing[_ -]?(key|cert))", re.IGNORECASE)
_APP_COLORS = frozenset({"primary", "alternate"})


def set_dispatch(callable_: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]]) -> None:
    global _DISPATCH
    _DISPATCH = callable_


def set_provider_readiness(callable_: Optional[Callable[[Dict[str, str]], Dict[str, Any]]]) -> None:
    global _PROVIDER_READINESS
    _PROVIDER_READINESS = callable_


def _app_color() -> Optional[str]:
    color = os.environ.get("LEAF_IOS_SHIP_APP_COLOR", "").strip()
    return color if color in _APP_COLORS else None


def _store():
    if "leaf_platform" not in sys.modules:
        pkg_dir = Path(__file__).resolve().parent.parent.parent / "platform"
        spec = importlib.util.spec_from_file_location(
            "leaf_platform", pkg_dir / "__init__.py", submodule_search_locations=[str(pkg_dir)])
        mod = importlib.util.module_from_spec(spec)
        sys.modules["leaf_platform"] = mod
        spec.loader.exec_module(mod)
    from leaf_platform import ios_ship
    return ios_ship


def _reject_secret_shaped(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and _SECRET_KEY_RE.search(key):
                raise ValueError(f"secret-shaped field at {path}.{key}")
            _reject_secret_shaped(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secret_shaped(child, f"{path}[{index}]")


class LaunchRequest(BaseModel):
    project_id: str
    approval_id: str
    revision: str
    source_revision: str
    source_sha256: str
    bundle_identifier: str
    marketing_version: str
    build_number: str


def _failure(status: int, code: str, message: str, *,
             setup_action: Optional[str] = None) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": {
        "error_code": code, "message": message, "retryable": status >= 500,
        **({"setup_action": setup_action} if setup_action else {})}})


def _tenant_id(tenant: Any) -> str:
    return str(getattr(tenant, "tenant_id", tenant))


def _closed_readiness(project_id: str, reason: str, setup_action: Optional[str]) -> Dict[str, Any]:
    return {"record_kind": _READINESS_KIND, "launchable": False, "healthy": False,
            "reported_at": None, "project_id": project_id, "grant_status": None,
            "dispatch_available": False, "reason": reason,
            "setup_action": setup_action, "approved_launch": None}


def _caller_org(store: Any, tenant: Any, project_id: str) -> Any:
    """Live requests use the verified binding. Project discovery is dev-only."""
    verified = getattr(tenant, "org_id", None)
    if verified:
        subject = getattr(tenant, "subject", None)
        return (verified if subject and store.project_accessible(
            verified, project_id, subject) else None)
    return store.project_org(project_id)


def _launch_principal(store: Any, tenant: Any, org_id: Any,
                      project_id: str) -> Optional[str]:
    subject = getattr(tenant, "subject", None)
    if subject:
        return store.resolve_ship_principal(org_id, project_id, subject)
    # Auth-off development seam. Live mode always carries a verified subject.
    return _tenant_id(tenant)


def _refresh_readiness(store: Any, org_id: Any, tenant_id: str, project_id: str,
                       revision: str, approved: Optional[Dict[str, str]]) -> Dict[str, Any]:
    if _PROVIDER_READINESS is None:
        return _closed_readiness(project_id, "provider_unavailable", _SETUP_ACTION)
    if approved is None:
        return store.readiness_projection(org_id, tenant_id, project_id, revision)
    scope = {
        "tenant_id": tenant_id, "project_id": project_id,
        "source_revision": approved["source_revision"], "source_sha256": approved["source_sha256"],
        "bundle_id": approved["bundle_identifier"], "marketing_version": approved["marketing_version"],
        "build_number": approved["build_number"],
    }
    try:
        provider = _PROVIDER_READINESS(scope)
    except Exception as exc:
        action = getattr(exc, "setup_action", _SETUP_ACTION)
        return _closed_readiness(project_id, "provider_unavailable", action)
    setup_action = provider["setup_action"]
    store.record_grant(org_id, tenant_id, {
        "grant_id": provider["grant_id"],
        "status": "healthy" if provider["healthy"] else "unavailable",
        "expires_at": provider["grant_expires_at"],
    })
    store.upsert_readiness({
        "record_kind": _READINESS_KIND, "healthy": provider["healthy"],
        "reported_at": provider["reported_at"], "org_id": str(org_id),
        "project_id": project_id, "tenant_id": tenant_id,
        "dispatch": {"available": provider["dispatch_available"], "action": setup_action},
    })
    return store.readiness_projection(org_id, tenant_id, project_id, revision)


@router.get("/api/ios-ship/readiness")
def readiness(project_id: str, revision: str,
              tenant: Any = Depends(deps.require_tenant)) -> JSONResponse:
    try:
        store = _store()
        org_id = _caller_org(store, tenant, project_id)
        if org_id is None:
            return JSONResponse(status_code=200, content={"ok": True, "readiness":
                _closed_readiness(project_id, "project_unavailable", "open-owned-project")})
        approved = store.get_approved_ios_ship_revision(org_id, project_id, revision)
        if _DISPATCH is None:
            return JSONResponse(status_code=200, content={"ok": True, "readiness":
                _closed_readiness(project_id, "dispatch_unavailable", _SETUP_ACTION)})
        if _app_color() is None:
            return JSONResponse(status_code=200, content={"ok": True, "readiness":
                _closed_readiness(project_id, "app_color_unavailable", _SETUP_ACTION)})
        projection = _refresh_readiness(
            store, org_id, _tenant_id(tenant), project_id, revision, approved)
        return JSONResponse(status_code=200, content={"ok": True, "readiness": projection})
    except Exception:
        return JSONResponse(status_code=200, content={"ok": True, "readiness":
            _closed_readiness(project_id, "readiness_unavailable", _SETUP_ACTION)})


def _ship_event(tenant_id: str, tenant_kind: str, name: str, stage: str,
                reason: Optional[str] = None) -> None:
    try:  # telemetry never touches the response
        labels: Dict[str, Any] = {"stage": stage}
        if reason is not None:
            labels["reason"] = reason
        telemetry_sink.emit(name, tenant_id=tenant_id, tenant_kind=tenant_kind,
                            session_id="none", labels=labels)
    except Exception:
        pass


@router.post("/api/ios-ship/launch")
async def launch(request: Request, req: LaunchRequest,
                 tenant: Any = Depends(deps.require_tenant),
                 idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key")) -> JSONResponse:
    tid = _tenant_id(tenant)
    tkind = "guest" if tid.startswith("guest-") else "account"

    def ship_event(name: str, stage: str, reason: Optional[str] = None) -> None:
        _ship_event(tid, tkind, name, stage, reason)

    ship_event("ship.requested", "received")
    tier = entitlements.resolve_tier(tenant)
    roles, elevated = entitlements.resolve_roles(tenant)
    try:
        deploy_allowed = entitlements.entitlements_for(tier, roles, elevated).get("deploy", False)
    except entitlements.EntitlementsError:
        ship_event("ship.failed", "authorization", "entitlement_policy_unavailable")
        return entitlements.policy_unavailable_response("deploy", tier)
    if not deploy_allowed:
        ship_event("ship.failed", "authorization", "entitlement_required")
        return entitlements.entitlement_denied_response("deploy", tier)
    if not idempotency_key:
        ship_event("ship.failed", "validation", "idempotency_key_required")
        return _failure(400, "idempotency_key_required", "Idempotency-Key header is required")
    try:
        _reject_secret_shaped(await request.json())
    except ValueError as exc:
        ship_event("ship.failed", "validation", "secret_shaped_field")
        return _failure(400, "secret_shaped_field", str(exc))
    if _DISPATCH is None:
        ship_event("ship.failed", "provisioning", "dispatch_unavailable")
        return _failure(503, "dispatch_unavailable", "ship dispatch is not mounted",
                        setup_action=_SETUP_ACTION)
    app_color = _app_color()
    if app_color is None:
        ship_event("ship.failed", "provisioning", "app_color_unavailable")
        return _failure(503, "app_color_unavailable", "server app color is unavailable",
                        setup_action=_SETUP_ACTION)
    try:
        store = _store()
    except Exception:
        ship_event("ship.failed", "provisioning", "launch_unavailable")
        return _failure(503, "launch_unavailable", "launch store is unavailable",
                        setup_action=_SETUP_ACTION)
    try:
        org_id = _caller_org(store, tenant, req.project_id)
        if org_id is None:
            ship_event("ship.failed", "authorization", "project_unavailable")
            return _failure(404, "project_unavailable", "project is unavailable")
        principal = _launch_principal(store, tenant, org_id, req.project_id)
        if principal is None:
            ship_event("ship.failed", "authorization", "launch_forbidden")
            return _failure(403, "launch_forbidden", "platform role does not permit iOS launch")
        execution = store.launch_execution(
            org_id, _tenant_id(tenant), principal, req.project_id,
            approval_id=req.approval_id, revision=req.revision,
            source_revision=req.source_revision, source_sha256=req.source_sha256,
            bundle_identifier=req.bundle_identifier, marketing_version=req.marketing_version,
            build_number=req.build_number, app_color=app_color, idempotency_key=idempotency_key,
            dispatch=_DISPATCH)
        ship_event("ship.completed", "dispatched")
        return JSONResponse(status_code=202, content={"ok": True, "execution": execution})
    except (store.GrantInvalid, store.RevisionNotApproved, store.LaunchConflict) as exc:
        ship_event("ship.failed", "grant", exc.code)
        return _failure(409, exc.code, str(exc), setup_action=exc.setup_action)
    except store.ProjectUnavailable as exc:
        ship_event("ship.failed", "authorization", exc.code)
        return _failure(404, exc.code, str(exc))
    except store.IosShipError as exc:
        ship_event("ship.failed", "dispatch", exc.code)
        return _failure(400, exc.code, str(exc), setup_action=exc.setup_action)
    except Exception:
        ship_event("ship.failed", "dispatch", "launch_unavailable")
        return _failure(503, "launch_unavailable", "launch is unavailable",
                        setup_action=_SETUP_ACTION)


@router.get("/api/ios-ship/executions/{execution_id}")
def execution(execution_id: str, project_id: str,
              tenant: Any = Depends(deps.require_tenant)) -> JSONResponse:
    try:
        store = _store()
        org_id = _caller_org(store, tenant, project_id)
        row = store.get_execution(org_id, project_id, execution_id) if org_id else None
    except Exception:
        return _failure(503, "execution_unavailable", "execution status is unavailable")
    return (JSONResponse(status_code=200, content={"ok": True, "execution": row})
            if row else _failure(404, "execution_not_found", "execution is unavailable"))


@router.get("/api/ios-ship/receipts/{receipt_id}")
def receipt(receipt_id: str, project_id: str,
            tenant: Any = Depends(deps.require_tenant)) -> JSONResponse:
    try:
        store = _store()
    except Exception:
        return _failure(503, "receipt_unavailable", "receipt is unavailable")
    try:
        org_id = _caller_org(store, tenant, project_id)
        row = store.read_receipt(org_id, project_id, receipt_id) if org_id else None
    except store.ReceiptTampered as exc:
        return _failure(409, exc.code, str(exc))
    except Exception:
        return _failure(503, "receipt_unavailable", "receipt is unavailable")
    return (JSONResponse(status_code=200, content={"ok": True, "receipt": row})
            if row else _failure(404, "receipt_not_found", "receipt is unavailable"))
