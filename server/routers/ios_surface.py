"""Sanitized read-only consumer of the ship-lane's published contract.

Reads exactly three published categories -- readiness, build stage, receipt
id -- through a pluggable, injected source. It never opens a database
connection, dispatches to a provider, or accepts anything Apple-credential
shaped in its request, response, or logs. Every request is answered live;
nothing is cached, so an unreachable upstream can never be served back as a
stale "fresh" contract.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

import deps

router = APIRouter()

_CONTRACT_SCHEMA = "leaf.ios-ship-surface.v1"
_SOURCE: Optional[Callable[[Dict[str, str]], Dict[str, Any]]] = None

# Same closed vocabulary the ship-lane controller emits (platform/ios_ship.py
# _CONTROLLER_STAGES). Duplicated per this repo's frozen-vocabulary
# convention (each consumer owns its own copy; nothing here mutates state).
_BUILD_STAGES = frozenset({
    "SOURCE_APPROVED", "GRANT_READY", "BUNDLE_READY", "MAC_ALLOCATED", "APP_RECORD",
    "XCODE_READY", "SIGNING_READY", "BUILT", "UPLOADED", "COMPLIANCE", "BETA_ASSIGNED",
    "CREDENTIALS_SCRUBBED", "MAC_RELEASED", "RECEIPT",
})
_RECEIPT_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SECRET_KEY_RE = re.compile(
    r"(password|passwd|two.?factor|2fa|otp|p8|\.p8|private[_ -]?key|certificate|"
    r"provisioning|profile|credential|secret|keychain|authkey|token|session|cookie|"
    r"api[_ -]?key|access[_ -]?key|signing[_ -]?(key|cert))", re.IGNORECASE)
_SECRET_VALUE_RE = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|eyJ[a-zA-Z0-9_-]{10,}\.eyJ|"
    r"AAAA[A-Za-z0-9+/]{20,}={0,2}")


class ContractInvalid(ValueError):
    """The upstream payload failed schema validation or leaked secret-shaped data."""


def set_contract_source(callable_: Optional[Callable[[Dict[str, str]], Dict[str, Any]]]) -> None:
    global _SOURCE
    _SOURCE = callable_


def _reject_secret_shaped(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and _SECRET_KEY_RE.search(key):
                raise ContractInvalid(f"secret-shaped field at {path}.{key}")
            _reject_secret_shaped(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secret_shaped(child, f"{path}[{index}]")
    elif isinstance(value, str) and _SECRET_VALUE_RE.search(value):
        raise ContractInvalid(f"secret-shaped value at {path}")


def _valid_reported_at(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ContractInvalid("reported_at must be a string or null")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractInvalid("reported_at is not a valid timestamp") from exc
    if parsed.tzinfo is None:
        raise ContractInvalid("reported_at needs a timezone")
    return value


def _valid_readiness(value: Any) -> Dict[str, bool]:
    if not isinstance(value, dict) or not isinstance(value.get("healthy"), bool) \
            or not isinstance(value.get("launchable"), bool):
        raise ContractInvalid("readiness must carry healthy and launchable booleans")
    return {"healthy": value["healthy"], "launchable": value["launchable"]}


def _valid_build_stage(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or value not in _BUILD_STAGES:
        raise ContractInvalid("build_stage is not in the published stage vocabulary")
    return value


def _valid_receipt_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not _RECEIPT_ID_RE.fullmatch(value):
        raise ContractInvalid("receipt_id is not a valid identifier")
    return value


def validate_contract(project_id: str, revision: str, raw: Any) -> Dict[str, Any]:
    """Schema-validate the published contract and drop every unknown field.

    Only readiness, build stage, and receipt id ever leave this function.
    Any secret-shaped key or value anywhere in ``raw`` fails the whole
    contract closed rather than silently stripping and continuing.
    """
    _reject_secret_shaped(raw)
    if not isinstance(raw, dict) or raw.get("schema") != _CONTRACT_SCHEMA:
        raise ContractInvalid("unsupported contract schema")
    if raw.get("project_id") != project_id or raw.get("revision") != revision:
        raise ContractInvalid("contract scope does not match the request")
    contract = {
        "schema": _CONTRACT_SCHEMA,
        "project_id": project_id,
        "revision": revision,
        "reported_at": _valid_reported_at(raw.get("reported_at")),
        "readiness": _valid_readiness(raw.get("readiness")),
        "build_stage": _valid_build_stage(raw.get("build_stage")),
        "receipt_id": _valid_receipt_id(raw.get("receipt_id")),
    }
    _reject_secret_shaped(contract)
    return contract


def _unavailable(project_id: str, revision: str, reason: str) -> JSONResponse:
    return JSONResponse(status_code=200, content={
        "ok": True, "status": "unavailable", "reason": reason,
        "project_id": project_id, "revision": revision})


def _tenant_id(tenant: Any) -> str:
    return str(getattr(tenant, "tenant_id", tenant))


@router.get("/api/ios-surface/status")
def status(project_id: str, revision: str,
           tenant: Any = Depends(deps.require_tenant)) -> JSONResponse:
    if _SOURCE is None:
        return _unavailable(project_id, revision, "surface_source_unavailable")
    try:
        raw = _SOURCE({"tenant_id": _tenant_id(tenant), "project_id": project_id,
                       "revision": revision})
    except Exception:
        return _unavailable(project_id, revision, "upstream_unreachable")
    try:
        contract = validate_contract(project_id, revision, raw)
    except ContractInvalid:
        return _unavailable(project_id, revision, "contract_invalid")
    return JSONResponse(status_code=200, content={"ok": True, "status": "available",
                                                   "contract": contract})
