"""Authenticated Lab status and the reversible Lab admission rail."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

import deps
import lab_run_store
from routers.lab_artifacts import _call

router = APIRouter(tags=["lab_surface"])

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
    return {
        "ok": True,
        "schema": "leaf.lab-status.v1",
        "rail": "on",
        "posture": lab_posture(),
        "tenant": str(getattr(tenant, "tenant_id", tenant)),
        "worker": {"paused": _call(lab_run_store.paused), "workers": workers,
                   "queue_age_max_s": _call(lab_run_store.oldest_queued_age_s)},
    }
