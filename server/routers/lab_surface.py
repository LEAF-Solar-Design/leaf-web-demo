"""Authenticated Lab status and the reversible Lab admission rail."""
from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from threading import Lock
from typing import Any

import requests
from fastapi import APIRouter, Depends, HTTPException

import broker_client
import deps
import entitlements
import lab_run_store
import lab_tool_manifests
from routers.lab_artifacts import _call

router = APIRouter(tags=["lab_surface"])
logger = logging.getLogger(__name__)

FLAG_LAB_SURFACE = "LEAF_LAB_SURFACE_ENABLED"


def lab_posture() -> str:
    """Use the deployed runtime vocabulary and deployment environment guard."""
    import runtime_posture

    if runtime_posture._production_runtime():
        return "production"
    if (runtime_posture._deployed_runtime() and os.environ.get(
            "LEAF_DEPLOYMENT_ENVIRONMENT", "").strip().lower() in {"", "staging"}):
        return "staging"
    return "local"


def lab_surface_enabled() -> bool:
    """Read explicit configuration first; only staging defaults to enabled."""
    raw = os.environ.get(FLAG_LAB_SURFACE)
    if raw is not None:
        value = raw.strip().lower()
        if value in {"0", "false", "no", "off"}:
            return False
        return value in {"1", "true", "yes", "on"}
    return lab_posture() == "staging"


def lab_rail() -> None:
    if not lab_surface_enabled():
        raise HTTPException(status_code=404, detail={
            "ok": False, "status": "refused", "reason": "lab_surface_disabled",
        })


def require_auth_live() -> None:
    if not deps.auth_live():
        raise HTTPException(status_code=503, detail={
            "ok": False, "status": "refused", "reason": "lab_auth_live_required",
        })


@router.get("/api/lab/status")
def status(tenant: Any = Depends(deps.require_tenant)) -> dict[str, Any]:
    response = {
        "ok": True,
        "schema": "leaf.lab-status.v1",
        "rail": "on",
        "posture": lab_posture(),
        "tenant": str(getattr(tenant, "tenant_id", tenant)),
    }
    try:
        tier = entitlements.resolve_tier(tenant)
        roles, elevated = entitlements.resolve_roles(tenant)
        allowed = entitlements.entitlements_for(tier, roles, elevated).get("build", False)
    except Exception:
        # Optional worker details fail closed without hiding the surface state.
        return response
    if not allowed:
        return response

    try:
        workers = []
        for row in _call(lab_run_store.worker_status, 60):
            heartbeat = row["heartbeat_at"]
            if isinstance(heartbeat, str):
                heartbeat = datetime.fromisoformat(heartbeat)
            workers.append({
                "build_revision": row["build_revision"], "color": row["color"],
                "heartbeat_age_s": max(0, (datetime.now(timezone.utc) - heartbeat).total_seconds()),
                "active_runs": row["active_runs"], "queued_runs": row["queued_runs"],
            })
        response["worker"] = {
            "paused": _call(lab_run_store.paused), "workers": workers,
            "queue_age_max_s": _call(lab_run_store.oldest_queued_age_s),
        }
    except HTTPException as exc:
        reason = exc.detail if isinstance(exc.detail, str) else "lab_store_unavailable"
        response["worker"] = {"state": "unavailable", "reason": reason}
        logger.warning("Lab worker status unavailable: %s", reason)
    except Exception:
        response["worker"] = {"state": "unavailable", "reason": "lab_store_unavailable"}
        logger.warning("Lab worker status unavailable: %s", "lab_store_unavailable")
    return response


_TOOL_ID = re.compile(r"[a-z][a-z0-9]*(-[a-z0-9]+)*")
_TOOL_VERSION = re.compile(r"[0-9A-Za-z.+-]{1,64}")
_broker_lab_sandbox_cache: tuple[float, str] | None = None
_broker_lab_sandbox_lock = Lock()


def _broker_lab_sandbox_tier() -> str:
    # Fail closed on broker errors; cache valid tiers for 60 s and failures for 10 s.
    global _broker_lab_sandbox_cache
    with _broker_lab_sandbox_lock:
        if (_broker_lab_sandbox_cache is not None
                and time.monotonic() < _broker_lab_sandbox_cache[0]):
            return _broker_lab_sandbox_cache[1]
        tier, ttl = "invalid", 10
        try:
            response = requests.get(f"{broker_client.broker_url()}/broker/health", timeout=3,
                                    headers={"Cache-Control": "no-cache"})
            if response.status_code == 200:
                data = response.json()
                value = data.get("lab_sandbox_tier") if isinstance(data, dict) else None
                if isinstance(value, str) and value in {"off", "subprocess", "microvm"}:
                    tier, ttl = value, 60
        except Exception:
            pass
        _broker_lab_sandbox_cache = (time.monotonic() + ttl, tier)
        return tier


@router.get("/api/lab/tools")
def tools(tenant: Any = Depends(deps.require_tenant)) -> dict[str, Any]:
    try:
        projection = lab_tool_manifests.tenant_tool_projection(
            str(getattr(tenant, "tenant_id", tenant)))
    except Exception:
        raise HTTPException(status_code=503, detail="lab_tools_unavailable") from None

    # Only rows whose name is a Lab id and whose version is a plain version string
    # are listed, so no path or other raw text reaches the response; the rest are counted.
    listed = [tool for tool in projection
              if isinstance(tool.name, str) and _TOOL_ID.fullmatch(tool.name)
              and isinstance(tool.version, str) and _TOOL_VERSION.fullmatch(tool.version)]
    result = []
    for tool in listed[:200]:
        source = "tenant" if tool.source in {"tenant_repo", "authored"} else "shipped"
        reason = tool.reason
        if reason is None and os.environ.get("LEAF_LAB_TOOL_NODES") != "1":
            reason = "tool nodes off"
        if reason is None:
            import runtime_posture

            if source == "tenant" and not runtime_posture._authored_execution_enabled():
                reason = "authored execution not armed"
            elif _broker_lab_sandbox_tier() != "microvm":
                reason = "sandbox not armed"
        result.append({
            "id": tool.name, "version": tool.version,
            "title": tool.manifest["title"] if tool.manifest is not None else tool.name,
            "source": source, "manifest": tool.manifest,
            "effect": tool.trusted.effect if tool.trusted is not None else "refused",
            "available": reason is None, "reason": reason,
        })
    return {"ok": True, "schema": "leaf.lab-tools.v1", "tools": result,
            "truncated": len(listed) > 200, "omitted": len(projection) - len(listed)}
