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
import hashlib
import hmac
import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

import deps
import entitlements
import jobs
from envelopes import DEFAULT_HTTP_STATUS, ErrorCode, error_response, with_envelope_fields

router = APIRouter()

PLATFORM_CONTRACT_VERSION = "leaf.platform.v1alpha1"
AUTOFILL_TOOL = "string-autofill-opt"


class RunRequest(BaseModel):
    tool: str
    params: Dict[str, Any] = {}
    dwg: str = "rooftop_demo"
    dwg_version: Optional[int] = None  # None -> head (unchanged default); pin to a
    # specific immutable drawing version (da/store.py resolve_version) otherwise.


class TerminalCallback(BaseModel):
    """Broker/worker terminal delivery. Replays are safe by job fingerprint."""
    worker_id: str
    status: str
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None
    provenance: Dict[str, Any]


def _record_body(rec: Dict[str, Any]) -> Dict[str, Any]:
    """Job record -> response body. Top-level `error` is the JOB's error (null
    unless failed); degraded_mode comes from the result envelope when present.

    On a quota_exceeded error, also HOIST quota_kind/tier/limit/used from the
    error object to the top level of the body (frontend api.js/ResultPanel read
    them there, not nested under `error`) so the UI can disambiguate the daily
    run-count quota from the USD spend-cap quota without reaching into `error`.
    """
    body = dict(rec)
    result = rec.get("result") or {}
    body["degraded_mode"] = bool(result.get("degraded_mode", False))
    error = rec.get("error")
    if isinstance(error, dict) and error.get("error_code") == "quota_exceeded":
        for key in ("quota_kind", "tier", "limit", "used"):
            if key in error:
                body[key] = error[key]
    return body


def _job_for_tenant(job_id: str, tenant_id: str) -> Optional[Dict[str, Any]]:
    legacy = jobs.get_job(job_id)
    if legacy is not None:
        return legacy if legacy.get("tenant_id") == str(tenant_id) else None
    return jobs.platform_link.get_canonical_job(job_id, str(tenant_id))


def _canonical_tenant_id(tenant: Any, context: Dict[str, Any]) -> str:
    """Bind JWT tenant/org claims to the server-owned platform identity."""
    canonical = str(context["org_id"])
    if deps.auth_live():
        if not isinstance(tenant, deps.TenantContext) \
                or str(tenant) != canonical or str(tenant.org_id or "") != canonical:
            raise ValueError(
                "verified tenant and org claims must match the active platform identity binding")
    return canonical if context.get("authority_mode") == "postgres_canonical" else str(tenant)


@router.post("/api/run")
def run(req: RunRequest, wait: int = 0, tenant_id: str = Depends(deps.require_tenant),
        x_org_id: Optional[str] = Header(default=None),
        x_project_id: Optional[str] = Header(default=None),
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
        authorization: Optional[str] = Header(default=None)):
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

    try:
        platform_context = jobs.platform_link.resolve_submission_context(
            x_org_id, x_project_id, authorization)
        resolved_org = (str(platform_context["org_id"]) if platform_context else None)
        resolved_project = (str(platform_context["project_id"]) if platform_context else None)
        authority_mode = (str(platform_context["authority_mode"])
                          if platform_context else "legacy_sqlite")
        if platform_context is not None:
            if not tool.get("canonical_only"):
                raise ValueError(
                    "project-scoped canonical execution is enabled only for a connected solver adapter")
            if req.dwg_version is not None:
                # Fail closed on ambiguity: canonical drawings pin by version
                # UUID in ``dwg``; the legacy integer pin has no meaning here.
                raise ValueError(
                    "dwg_version applies to the legacy path; canonical runs pin by version UUID in dwg")
            try:
                input_version_id = str(uuid.UUID(req.dwg))
            except (ValueError, AttributeError):
                raise ValueError("a canonical drawing version UUID is required") from None
            request_tenant = _canonical_tenant_id(tenant_id, platform_context)
            job_id = jobs.platform_link.submit_canonical_solve(
                platform_context, request_tenant, str(tool["name"]), params,
                idempotency_key, input_version_id)
        else:
            if tool.get("canonical_only"):
                raise ValueError("X-Org-Id and X-Project-Id are required for this canonical solver")
            job_id = jobs.submit_job(
                tenant_id, tool, params, req.dwg, aps_live=deps.APS_LIVE,
                org_id=resolved_org, project_id=resolved_project,
                dwg_version=req.dwg_version,
                idempotency_key=idempotency_key, authority_mode=authority_mode,
                platform_context=platform_context,
            )
    except jobs.platform_link.CanonicalEntitlementDenied as exc:
        # STORED-org entitlement denial from the canonical choke point (P1
        # floor): hand the documented 403/503 envelope back verbatim — never
        # rewrap it as BAD_PARAMS.
        return exc.response
    except ValueError as exc:
        message = str(exc)
        return error_response(
            ErrorCode.BAD_PARAMS, message, retryable=False,
            status_code=400 if "required" in message else 409,
        )

    if wait:
        if platform_context is None:
            rec = jobs.wait_for_terminal(job_id, timeout_s=jobs.job_max_s() + 30)
        else:
            deadline = time.time() + jobs.job_max_s() + 30
            rec = _job_for_tenant(job_id, str(tenant_id))
            while rec is not None and rec["status"] not in jobs.TERMINAL \
                    and time.time() < deadline:
                time.sleep(0.1)
                rec = _job_for_tenant(job_id, str(tenant_id))
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
    rec = _job_for_tenant(job_id, str(tenant))
    # 404 (never 403) both when the job is unknown AND when it belongs to another
    # tenant — no cross-tenant existence leak (security-audit F8).
    if rec is None or rec.get("tenant_id") != str(tenant):
        return error_response(ErrorCode.BAD_PARAMS, f"unknown job_id: {job_id}",
                              retryable=False, status_code=404)
    return _record_body(rec)


@router.post("/internal/jobs/{job_id}/callback")
def terminal_callback(job_id: str, callback: TerminalCallback,
                      x_tenant_id: Optional[str] = Header(default=None),
                      x_broker_secret: Optional[str] = Header(default=None)):
    """Idempotent worker callback, fail-closed behind the broker credential.

    The public tenant session is deliberately insufficient authority to complete
    work. The configured broker secret, tenant correlation, and active lease owner
    must all match. A repeated matching callback returns success; a differing
    terminal result is retained as a conflict and cannot overwrite the first.
    """
    secret = os.environ.get("LEAF_BROKER_SECRET")
    if not secret:
        return error_response(ErrorCode.INTERNAL, "worker callbacks are disabled",
                              retryable=False, status_code=503)
    if not hmac.compare_digest(x_broker_secret or "", secret):
        return error_response(ErrorCode.BAD_PARAMS, "invalid worker callback credential",
                              retryable=False, status_code=401)
    rec = jobs.get_job(job_id)
    if rec is None or rec.get("tenant_id") != str(x_tenant_id):
        return error_response(ErrorCode.BAD_PARAMS, f"unknown job_id: {job_id}",
                              retryable=False, status_code=404)
    try:
        outcome = jobs.complete_callback(job_id, callback.status, result_env=callback.result,
                                         error=callback.error, provenance=callback.provenance,
                                         worker_id=callback.worker_id)
    except ValueError as exc:
        return error_response(ErrorCode.BAD_PARAMS, str(exc), retryable=False, status_code=400)
    if outcome == "conflict":
        return error_response(ErrorCode.BAD_PARAMS, "conflicting terminal callback",
                              retryable=False, status_code=409)
    if outcome == "not_owner":
        return error_response(ErrorCode.BAD_PARAMS, "callback lease is not current",
                              retryable=True, status_code=409)
    return _record_body(jobs.get_job(job_id) or rec)


@router.get("/api/jobs/{job_id}/stream")
async def stream_job(job_id: str, tenant=Depends(deps.require_tenant)):
    """SSE: emit each (status, progress) transition; close after terminal.

    Async generator (B1): a sync generator here is consumed via AnyIO's
    threadpool, pinning one of its ~40 worker threads for the stream's whole
    lifetime — N concurrent streams starve every sync endpoint. The 0.5s cadence
    now awaits on the event loop; the per-tick DB read hops to a thread so the
    loop never blocks on the jobs-module lock. Wire format/timing unchanged."""
    _rec0 = await asyncio.to_thread(_job_for_tenant, job_id, str(tenant))
    if _rec0 is None or _rec0.get("tenant_id") != str(tenant):
        return error_response(ErrorCode.BAD_PARAMS, f"unknown job_id: {job_id}",
                              retryable=False, status_code=404)

    async def event_stream():
        last = None
        deadline = time.time() + jobs.job_max_s() + 60
        while time.time() < deadline:
            rec = await asyncio.to_thread(_job_for_tenant, job_id, str(tenant))
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
    canonical = jobs.platform_link.list_canonical_jobs(str(tenant), limit=limit)
    legacy = jobs.list_jobs(str(tenant), limit)
    return with_envelope_fields(
        {"jobs": [_record_body(r) for r in (canonical + legacy)[:limit]]}
    )


@router.get("/api/platform/capabilities")
def platform_capabilities(
    tenant=Depends(deps.require_tenant),
    x_org_id: Optional[str] = Header(default=None),
    x_project_id: Optional[str] = Header(default=None),
    authorization: Optional[str] = Header(default=None),
):
    """Authenticated capability truth; never promotes from configuration alone."""
    try:
        context = jobs.platform_link.resolve_submission_context(
            x_org_id, x_project_id, authorization)
    except ValueError as exc:
        return error_response(ErrorCode.BAD_PARAMS, str(exc), retryable=False, status_code=409)
    if context is None:
        return error_response(ErrorCode.BAD_PARAMS, "X-Project-Id is required",
                              retryable=False, status_code=400)
    try:
        _canonical_tenant_id(tenant, context)
    except ValueError as exc:
        return error_response(ErrorCode.BAD_PARAMS, str(exc), retryable=False, status_code=409)
    health = jobs.platform_link.canonical_worker_health(AUTOFILL_TOOL)
    now = datetime.now(timezone.utc)
    tier = entitlements.resolve_tier(tenant)
    entitled = entitlements.entitlements_for(tier).get("solve", False)
    observed = str(health["observed_at"]) if health else now.isoformat()
    expires = ((datetime.fromisoformat(observed) if health else now)
               + timedelta(seconds=15)).isoformat()
    evidence = []
    if health:
        evidence_payload = {"tool": AUTOFILL_TOOL, "runtime": health["runtime"],
                            "sourceRevision": health["source_revision"],
                            "sourceSha256": health["source_sha256"],
                            "observedAt": observed}
        digest = hashlib.sha256(json.dumps(
            evidence_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        evidence = [{"kind": "observability", "uri": "urn:leaf:runtime:autofill-worker",
                     "verifiedAt": observed,
                     "digest": {"algorithm": "sha256", "value": digest}}]
    availability = {
        "contractVersion": PLATFORM_CONTRACT_VERSION,
        "authority": "leaf-platform-registry",
        "productCapability": "drawing.solve.strings",
        "implementationState": "implemented",
        "runtimeState": "degraded" if health else "unavailable",
        # A heartbeat proves a connected runtime, not the staging G1A gate.  Do
        # not elevate the customer surface to shipping until a durable gate
        # attestation exists and is checked here.
        "state": "connected_degraded" if health else "failed_retryable",
        "observedAt": observed,
        "expiresAt": expires,
        "reasonCode": "staging_gate_unverified" if health else "canonical_worker_offline",
        "evidence": evidence,
        **({"fallback": {"mode": "read_only", "provenanceRequired": True}}
           if health else {}),
    }
    return with_envelope_fields({
        "contractVersion": PLATFORM_CONTRACT_VERSION,
        "projectId": str(context["project_id"]),
        "capabilities": [{"id": "drawing.solve.strings", "label": "Solve strings",
                          "description": "Run the canonical AutoFill target solver.",
                          "availability": availability, "toolCapabilities": ["solve"],
                          "entitlements": ["solve"], "entitled": bool(entitled)}],
    })


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
