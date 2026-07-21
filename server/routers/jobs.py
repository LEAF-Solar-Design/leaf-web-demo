"""
Async job spine endpoints (CONTRACT-ADDENDUM section 7) — this session's lane.

POST /api/run                -> HTTP 202 {job_id, status:"submitted"}  (async, <200ms)
POST /api/run?wait=1         -> blocks; returns the final section-3 envelope (back-compat)
GET  /api/jobs/{job_id}      -> durable job record (result/error when terminal)
GET  /api/jobs/{job_id}/stream -> SSE status transitions until terminal
GET  /api/jobs?tenant_id=&limit= -> recent jobs (reconnect-after-tab-close)
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

import deps
import entitlements
import jobs
from envelopes import DEFAULT_HTTP_STATUS, ErrorCode, error_response, with_envelope_fields

router = APIRouter()


class RunRequest(BaseModel):
    tool: str
    params: Dict[str, Any] = {}
    dwg: str = "rooftop_demo"


def _record_body(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Job record -> response body. Top-level `error` is the JOB's error (null
    unless failed); degraded_mode comes from the result envelope when present."""
    body = dict(rec)
    result = rec.get("result") or {}
    body["degraded_mode"] = bool(result.get("degraded_mode", False))
    return body


@router.post("/api/run")
def run(req: RunRequest, wait: int = 0, tenant_id: str = Depends(deps.require_tenant),
        x_org_id: Optional[str] = Header(default=None),
        x_project_id: Optional[str] = Header(default=None)):
    """Submit a tool run as a durable background job (202), or block with ?wait=1.

    OPTIONAL project context: when the caller sends BOTH ``X-Org-Id`` and
    ``X-Project-Id``, the run is additionally recorded as a canonical platform
    Job (best-effort, env-gated — see server/platform_link.py). Absent either
    header the behaviour is byte-identical to before.
    """
    # TENANT-SCOPED resolution (wave 4): resolve the tool from the REQUESTING tenant's
    # catalog (globals + that tenant's own repo tools). A tool authored by another
    # tenant is not in this tenant's catalog -> UNKNOWN_TOOL, so it can never be run
    # cross-tenant. The tenant_id then threads jobs -> broker -> tool_loader so entry
    # resolution + execution read the same tenant's repo.
    tool = deps.find_tool(req.tool, str(tenant_id))
    if tool is None:
        return error_response(ErrorCode.UNKNOWN_TOOL, f"unknown tool: {req.tool}",
                              retryable=False, tool=req.tool)

    # ENTITLEMENT GATE (§17): the tenant's tier must grant the capability this tool needs
    # (run_write for a drawing.write tool, else run_read). Enforced HERE in the execution
    # chain — before job submission, on BOTH the async and ?wait=1 paths — so it cannot be
    # bypassed by the UI. Off-auth/demo tier grants everything (friction-free).
    tier = entitlements.resolve_tier(tenant_id)
    required = entitlements.tool_required_capability(tool)
    if not entitlements.entitlements_for(tier).get(required, False):
        return entitlements.entitlement_denied_response(required, tier)

    # merge authored default_params under caller params
    params = dict(tool.get("default_params", {}))
    params.update(req.params or {})

    job_id = jobs.submit_job(tenant_id, tool, params, req.dwg, aps_live=deps.APS_LIVE,
                             org_id=x_org_id, project_id=x_project_id)

    if wait:
        rec = jobs.wait_for_terminal(job_id, timeout_s=jobs.job_max_s() + 30)
        if rec is None:
            return error_response(ErrorCode.INTERNAL, "job record vanished", retryable=False)
        if rec["status"] == "complete":
            return JSONResponse(status_code=200, content=rec["result"])
        env = jobs.failed_envelope_from(rec)
        code = env["error"]["error_code"]
        return JSONResponse(status_code=DEFAULT_HTTP_STATUS.get(code, 500), content=env)

    return JSONResponse(
        status_code=202,
        content=deps.tenant_echo(
            with_envelope_fields({"job_id": job_id, "status": "submitted"}), tenant_id
        ),
    )


@router.get("/api/jobs/{job_id}")
def get_job(job_id: str, tenant=Depends(deps.require_tenant)):
    rec = jobs.get_job(job_id)
    # 404 (never 403) both when the job is unknown AND when it belongs to another
    # tenant — no cross-tenant existence leak (security-audit F8).
    if rec is None or rec.get("tenant_id") != str(tenant):
        return error_response(ErrorCode.BAD_PARAMS, f"unknown job_id: {job_id}",
                              retryable=False, status_code=404)
    return _record_body(rec)


@router.get("/api/jobs/{job_id}/stream")
async def stream_job(job_id: str, tenant=Depends(deps.require_tenant)):
    """SSE: emit each (status, progress) transition; close after terminal.

    Async generator (B1): a sync generator here is consumed via AnyIO's
    threadpool, pinning one of its ~40 worker threads for the stream's whole
    lifetime — N concurrent streams starve every sync endpoint. The 0.5s cadence
    now awaits on the event loop; the per-tick DB read hops to a thread so the
    loop never blocks on the jobs-module lock. Wire format/timing unchanged."""
    _rec0 = await asyncio.to_thread(jobs.get_job, job_id)
    if _rec0 is None or _rec0.get("tenant_id") != str(tenant):
        return error_response(ErrorCode.BAD_PARAMS, f"unknown job_id: {job_id}",
                              retryable=False, status_code=404)

    async def event_stream():
        last = None
        deadline = time.time() + jobs.job_max_s() + 60
        while time.time() < deadline:
            rec = await asyncio.to_thread(jobs.get_job, job_id)
            if rec is None:
                yield "data: " + json.dumps({"job_id": job_id, "status": "unknown"}) + "\n\n"
                return
            key = (rec["status"], rec["progress"])
            if key != last:
                last = key
                payload = {
                    "job_id": job_id,
                    "status": rec["status"],
                    "progress": rec["progress"],
                    "elapsed_ms": rec["elapsed_ms"],
                    "error": rec["error"],
                }
                yield "data: " + json.dumps(payload) + "\n\n"
            if rec["status"] in jobs.TERMINAL:
                return
            await asyncio.sleep(0.5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/api/jobs")
def list_jobs(limit: int = 20, tenant=Depends(deps.require_tenant)):
    # Scope strictly to the RESOLVED caller tenant; a client-supplied tenant_id is
    # never trusted (security-audit F1 — this endpoint had no auth + unbound scope).
    return with_envelope_fields(
        {"jobs": [_record_body(r) for r in jobs.list_jobs(str(tenant), limit)]}
    )


@router.post("/api/jobs/{job_id}/close")
def close_job(job_id: str, tenant_id: str = Depends(deps.require_tenant)):
    """Tab-close / session-end signal: mark this in-flight job's owner gone so the
    orphan reaper fails it (and its APS WorkItem can be reaped broker-side).
    Idempotent. 404s (not 403) on a job owned by another tenant — never leaks
    existence across the tenant boundary."""
    rec = jobs.get_job(job_id)
    if rec is None or str(tenant_id) != rec["tenant_id"]:
        return error_response(ErrorCode.BAD_PARAMS, f"unknown job_id: {job_id}",
                              retryable=False, status_code=404)
    _rec = jobs.get_job(job_id)
    if _rec is not None and _rec.get("tenant_id") != str(tenant_id):
        # Another tenant's job — silently no-op (beacon path, keep it 200/idempotent).
        return JSONResponse(status_code=200,
                            content=with_envelope_fields({"job_id": job_id, "closed": False}))
    flagged = jobs.mark_job_closed(job_id)
    after = jobs.get_job(job_id)
    return with_envelope_fields(deps.tenant_echo(
        {"job_id": job_id, "closed": bool(flagged),
         "status": after["status"] if after else "unknown"}, tenant_id))
