"""Internal authenticated callbacks from the separately deployed iOS provider."""
from __future__ import annotations

import hmac
import importlib.util
import sys
from pathlib import Path
import re
from typing import Any, Dict, Optional

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse

from ios_ship_provider import ProviderConfig, ProviderConfigurationError


router = APIRouter()
_CONFIG: Optional[ProviderConfig] = None
_PROGRESS_FIELDS = frozenset({
    "org_id", "tenant_id", "project_id", "execution_id", "provider_run_id", "status", "stage",
})
_PROGRESS_REQUIRED_FIELDS = _PROGRESS_FIELDS - {"stage"}
_RECEIPT_FIELDS = frozenset({
    "org_id", "tenant_id", "project_id", "execution_id", "provider_run_id", "receipt",
})
_IDENTITY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TENANT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)


def set_config(config: Optional[ProviderConfig]) -> None:
    global _CONFIG
    _CONFIG = config


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


def _failure(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": {
        "error_code": code, "message": message, "retryable": status >= 500}})


def _authorized(authorization: Optional[str], provider_identity: Optional[str]) -> Optional[JSONResponse]:
    if _CONFIG is None:
        return _failure(503, "provider_callback_unavailable",
                        "provider callback is unavailable")
    try:
        expected = _CONFIG.read_token()
    except ProviderConfigurationError:
        return _failure(503, "provider_callback_unavailable",
                        "provider callback is unavailable")
    supplied = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        return _failure(401, "provider_unauthorized", "provider authentication failed")
    if not provider_identity or not hmac.compare_digest(provider_identity, _CONFIG.provider_id):
        return _failure(401, "provider_unauthorized", "provider authentication failed")
    return None


def _valid_identity(value: Any) -> bool:
    return isinstance(value, str) and bool(_IDENTITY_RE.fullmatch(value))


def _valid_callback_scope(body: Dict[str, Any], execution_id: str) -> bool:
    return (body.get("execution_id") == execution_id and _UUID_RE.fullmatch(execution_id) is not None
            and all(_UUID_RE.fullmatch(str(body.get(name, ""))) is not None
                    for name in ("org_id", "project_id", "execution_id"))
            and isinstance(body.get("tenant_id"), str)
            and _TENANT_RE.fullmatch(body["tenant_id"]) is not None
            and _valid_identity(body.get("provider_run_id")))


async def _payload(request: Request, fields: frozenset[str]) -> Optional[Dict[str, Any]]:
    try:
        value = await request.json()
    except Exception:
        return None
    return value if isinstance(value, dict) and set(value) == fields else None


async def _progress_payload(request: Request) -> Optional[Dict[str, Any]]:
    try:
        value = await request.json()
    except Exception:
        return None
    if not isinstance(value, dict) or not _PROGRESS_REQUIRED_FIELDS <= set(value) \
            or set(value) - _PROGRESS_FIELDS:
        return None
    return value


@router.post("/internal/v1/ios-ship/executions/{execution_id}/progress")
async def progress(execution_id: str, request: Request,
                   authorization: Optional[str] = Header(default=None),
                   provider_identity: Optional[str] = Header(default=None,
                       alias="X-Leaf-Ios-Ship-Provider")) -> JSONResponse:
    denied = _authorized(authorization, provider_identity)
    if denied is not None:
        return denied
    body = await _progress_payload(request)
    if body is None or not _valid_callback_scope(body, execution_id) \
            or any(not isinstance(body.get(name), str) or not body[name]
                           for name in _PROGRESS_FIELDS - {"stage"}) \
            or (body.get("stage") is not None and not isinstance(body["stage"], str)):
        return _failure(400, "invalid_provider_progress", "provider progress is invalid")
    try:
        store = _store()
        execution = store.record_provider_progress(
            body["org_id"], body["tenant_id"], body["project_id"], execution_id,
            body["provider_run_id"], status=body["status"], stage=body.get("stage"))
        return JSONResponse(status_code=200, content={"ok": True, "execution": execution})
    except Exception as exc:
        code = getattr(exc, "code", "provider_progress_unavailable")
        status = (503 if code == "provider_progress_unavailable" else
                  409 if code in {"provider_scope_mismatch", "execution_terminal"} else 400)
        return _failure(status, code, "provider progress was rejected")


@router.post("/internal/v1/ios-ship/executions/{execution_id}/receipt")
async def receipt(execution_id: str, request: Request,
                  authorization: Optional[str] = Header(default=None),
                  provider_identity: Optional[str] = Header(default=None,
                      alias="X-Leaf-Ios-Ship-Provider")) -> JSONResponse:
    denied = _authorized(authorization, provider_identity)
    if denied is not None:
        return denied
    body = await _payload(request, _RECEIPT_FIELDS)
    if body is None or not _valid_callback_scope(body, execution_id) \
            or any(not isinstance(body.get(name), str) or not body[name]
                           for name in _RECEIPT_FIELDS - {"receipt"}) \
            or not isinstance(body.get("receipt"), dict):
        return _failure(400, "invalid_provider_receipt", "provider receipt is invalid")
    try:
        store = _store()
        written = store.record_provider_receipt(
            body["org_id"], body["tenant_id"], body["project_id"], execution_id,
            body["provider_run_id"], body["receipt"])
        return JSONResponse(status_code=200, content={"ok": True, "receipt": written})
    except Exception as exc:
        code = getattr(exc, "code", "provider_receipt_unavailable")
        status = (503 if code == "provider_receipt_unavailable" else
                  409 if code in {
                      "provider_scope_mismatch", "receipt_identity_mismatch",
                      "receipt_tampered",
                  } else 400)
        return _failure(status, code, "provider receipt was rejected")
