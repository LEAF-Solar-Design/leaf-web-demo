"""Authenticated Lab status and the reversible Lab admission rail."""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

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
    from broker import _deployed_runtime, _production_runtime

    if _production_runtime():
        return "production"
    if (_deployed_runtime() and os.environ.get(
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


@router.get("/api/lab/tools")
def tools(tenant: Any = Depends(deps.require_tenant)) -> dict[str, Any]:
    try:
        projection = lab_tool_manifests.tenant_tool_projection(
            str(getattr(tenant, "tenant_id", tenant)))
    except Exception:
        raise HTTPException(status_code=503, detail="lab_tools_unavailable") from None

    result = []
    for tool in projection[:200]:
        source = "tenant" if tool.source in {"tenant_repo", "authored"} else "shipped"
        reason = tool.reason
        if reason is None and os.environ.get("LEAF_LAB_TOOL_NODES") != "1":
            reason = "tool nodes off"
        if reason is None:
            from broker import _authored_execution_enabled, _tool_sandbox_tier

            if source == "tenant" and not _authored_execution_enabled():
                reason = "authored execution not armed"
            else:
                try:
                    if _tool_sandbox_tier() != "microvm":
                        reason = "sandbox not armed"
                except Exception:
                    reason = "sandbox not armed"
        result.append({
            "id": tool.name, "version": tool.version,
            "title": tool.manifest["title"] if tool.manifest is not None else tool.name,
            "source": source, "manifest": tool.manifest,
            "effect": tool.trusted.effect if tool.trusted is not None else "refused",
            "available": reason is None, "reason": reason,
        })
    return {"ok": True, "schema": "leaf.lab-tools.v1", "tools": result,
            "truncated": len(projection) > 200}
