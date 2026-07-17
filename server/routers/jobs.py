"""
Async job spine endpoints (CONTRACT-ADDENDUM section 7) — this session's lane.

POST /api/run                -> HTTP 202 {job_id, status:"submitted"}  (async, <200ms)
POST /api/run?wait=1         -> blocks; returns the final section-3 envelope (back-compat)
GET  /api/jobs/{job_id}      -> durable job record (result/error when terminal)
GET  /api/jobs/{job_id}/stream -> SSE status transitions until terminal
GET  /api/jobs?tenant_id=&limit= -> recent jobs (reconnect-after-tab-close)
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

import deps
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
def run(req: RunRequest, wait: int = 0, tenant_id: str = Depends(deps.require_tenant)):
    """Submit a tool run as a durable background job (202), or block with ?wait=1."""
    tool = deps.find_tool(req.tool)
    if tool is None:
        return error_response(ErrorCode.UNKNOWN_TOOL, f"unknown tool: {req.tool}",
                              retryable=False, tool=req.tool)

    # merge authored default_params under caller params
    params = dict(tool.get("default_params", {}))
    params.update(req.params or {})

    job_id = jobs.submit_job(tenant_id, tool, params, req.dwg, aps_live=deps.APS_LIVE)

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
        content=with_envelope_fields({"job_id": job_id, "status": "submitted"}),
    )


@router.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    rec = jobs.get_job(job_id)
    if rec is None:
        return error_response(ErrorCode.BAD_PARAMS, f"unknown job_id: {job_id}",
                              retryable=False, status_code=404)
    return _record_body(rec)


@router.get("/api/jobs/{job_id}/stream")
def stream_job(job_id: str):
    """SSE: emit each (status, progress) transition; close after terminal."""
    if jobs.get_job(job_id) is None:
        return error_response(ErrorCode.BAD_PARAMS, f"unknown job_id: {job_id}",
                              retryable=False, status_code=404)

    def event_stream():
        last = None
        deadline = time.time() + jobs.job_max_s() + 60
        while time.time() < deadline:
            rec = jobs.get_job(job_id)
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
            time.sleep(0.5)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/api/jobs")
def list_jobs(tenant_id: Optional[str] = None, limit: int = 20):
    return with_envelope_fields({"jobs": [_record_body(r) for r in jobs.list_jobs(tenant_id, limit)]})
