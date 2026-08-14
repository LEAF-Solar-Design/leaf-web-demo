"""Broker-owned APS blank-DWG feasibility producer.

This module has no credential loader.  The broker injects its already-loaded
``da/client.py`` module, which remains the sole APS credential holder.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Callable


CONTRACT = "leaf.aps-blank-dwg-feasibility.v1"
ACTIVITY_ID = "LeafBlankDwgFeasibility"
OUTPUT_NAME = "blank.dwg"
DWG_MAGIC = b"AC10"


def activity_spec(engine: str) -> dict[str, Any]:
    """Return the exact no-input Design Automation activity definition."""
    script = """(vl-load-com)
(setq leaf-out (strcat (getvar \"DWGPREFIX\") \"blank.dwg\"))
(vla-SaveAs (vla-get-ActiveDocument (vlax-get-acad-object)) leaf-out)
(princ)
"""
    return {
        "id": ACTIVITY_ID,
        "engine": engine,
        "commandLine": [
            r'$(engine.path)\accoreconsole.exe /s "$(settings[script].path)"'
        ],
        "parameters": {
            "Result": {"verb": "put", "required": True, "localName": OUTPUT_NAME},
        },
        "settings": {"script": {"value": script}},
        "description": "Leaf protected feasibility probe for a true no-input blank DWG.",
    }


def _ensure_activity(da: Any) -> str:
    """Idempotently provision the one broker-owned activity and alias."""
    spec = activity_spec(da.ENGINE)
    headers = {**da._auth_headers(), "Content-Type": "application/json"}
    created = da.requests.post(
        f"{da.DA}/activities",
        headers=headers,
        data=da.json.dumps(spec),
        timeout=da._HTTP_TIMEOUT,
    )
    if created.status_code != 409:
        created.raise_for_status()
    alias = da.requests.post(
        f"{da.DA}/activities/{ACTIVITY_ID}/aliases",
        headers=headers,
        data=da.json.dumps({"id": da.ALIAS, "version": 1}),
        timeout=da._HTTP_TIMEOUT,
    )
    if alias.status_code not in (200, 201, 409):
        alias.raise_for_status()
    return da.activity_qualified(ACTIVITY_ID)


def _cost(da: Any, status: dict[str, Any]) -> dict[str, float] | None:
    seconds = da._engine_seconds(status)
    if seconds is None:
        return None
    return {
        "engine_seconds": float(seconds),
        "usd_est": round(
            float(seconds) / 3600.0 * float(os.environ.get("APS_USD_PER_HR", "10")),
            4,
        ),
    }


def _combined_cost(*costs: dict[str, Any] | None) -> dict[str, float] | None:
    usable = [value for value in costs if isinstance(value, dict)]
    if not usable:
        return None
    return {
        "engine_seconds": round(
            sum(float(value.get("engine_seconds") or 0.0) for value in usable), 2
        ),
        "usd_est": round(sum(float(value.get("usd_est") or 0.0) for value in usable), 4),
    }


def _unsupported(
    *,
    source_sha: str,
    reason: str,
    workitem_id: str | None,
    cost: dict[str, float] | None,
) -> dict[str, Any]:
    return {
        "contract": CONTRACT,
        "status": "unsupported",
        "source_sha": source_sha,
        "activity": ACTIVITY_ID,
        "workitem_id": workitem_id,
        "output": None,
        "drawing": None,
        "read": None,
        "cost": cost,
        "fallback": "upload_only",
        "reason": reason,
        "degraded_mode": False,
    }


def run(
    da: Any,
    *,
    tenant_id: str,
    source_sha: str,
    read_tool: dict[str, Any],
    publish: Callable[[bytes, str], dict[str, Any]],
    on_submitted: Callable[[str | None], None] | None = None,
) -> dict[str, Any]:
    """Run one paid no-input create, validate/read it, then publish version 1.

    A terminal APS rejection, invalid DWG, or failed re-extract is a closed
    unsupported result. Credential, transport, provisioning, and publication
    failures raise, so an operational fault cannot masquerade as lack of APS
    product support.
    """
    activity = _ensure_activity(da)
    nonce = f"blank-dwg/{tenant_id}/{int(time.time())}-{os.urandom(6).hex()}.dwg"
    upload_key, output_url = da.scratch_signed_upload_url(nonce)
    status = da.submit_workitem(
        activity,
        {"Result": {"url": output_url, "verb": "put"}},
        dry_run=False,
        poll=True,
        tenant_id=tenant_id,
        on_submitted=on_submitted,
    )
    workitem_id = status.get("id") if isinstance(status, dict) else None
    create_cost = _cost(da, status)
    if status.get("status") != "success":
        return _unsupported(
            source_sha=source_sha,
            reason="no_input_activity_rejected",
            workitem_id=workitem_id,
            cost=create_cost,
        )

    try:
        da.finalize_scratch_upload(nonce, upload_key)
        payload = da.download_scratch_object(nonce)
    finally:
        try:
            da.delete_scratch_object(nonce)
        except Exception:
            pass

    if len(payload) < 6 or not payload.startswith(DWG_MAGIC):
        return _unsupported(
            source_sha=source_sha,
            reason="invalid_dwg_output",
            workitem_id=workitem_id,
            cost=create_cost,
        )

    digest = hashlib.sha256(payload).hexdigest()
    with tempfile.TemporaryDirectory(prefix="leaf-blank-dwg-") as tmp:
        path = Path(tmp) / OUTPUT_NAME
        path.write_bytes(payload)
        read = da.run_tool(
            str(path),
            read_tool,
            {},
            tenant_id=tenant_id,
            on_submitted=on_submitted,
        )
    if not isinstance(read, dict) or not read.get("ok"):
        return _unsupported(
            source_sha=source_sha,
            reason="read_tool_failed",
            workitem_id=workitem_id,
            cost=_combined_cost(create_cost, read.get("cost") if isinstance(read, dict) else None),
        )

    drawing = publish(payload, digest)
    combined = _combined_cost(create_cost, read.get("cost"))
    return {
        "contract": CONTRACT,
        "status": "supported",
        "source_sha": source_sha,
        "activity": ACTIVITY_ID,
        "workitem_id": workitem_id,
        "output": {"sha256": digest, "bytes": len(payload), "version": 1},
        "drawing": drawing,
        "read": {
            "tool": read_tool["name"],
            "ok": True,
            "result": read.get("result"),
        },
        "cost": combined,
        "fallback": None,
        "reason": None,
        "degraded_mode": False,
    }
