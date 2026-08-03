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
from typing import Any, Dict, Optional, Tuple

from fastapi import APIRouter, Depends, Header
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

import checkout_capability
import deps
import entitlements
import jobs
import write_loop
import product_capability_availability as capability_catalog
import customization_service
from envelopes import DEFAULT_HTTP_STATUS, ErrorCode, error_response, with_envelope_fields

try:  # APS domain metrics (CloudWatch EMF); best-effort, optional — mirrors jobs.py
    import emf_metrics
except Exception:  # pragma: no cover
    emf_metrics = None  # type: ignore[assignment]

router = APIRouter()


def _emit_submit_latency(ms: float, aps_live: Any, tenant_id: Any,
                         tool: Any, job_id: Any) -> None:
    """Best-effort SubmitLatency emission. The emitter already swallows its own
    failures; this wrapper covers the import being absent, so observability can
    never turn a successful 202 into a 500.

    It does NOT cover the call site's own argument expressions, which Python
    evaluates before this function is entered. Keep them incapable of raising
    (they are today: a module constant, a str(), a dict .get(), and a local)."""
    if emf_metrics is None:
        return
    try:
        emf_metrics.emit_submit_latency(
            ms, aps_live=aps_live, tenant_id=tenant_id, tool=tool, job_id=job_id)
    except Exception:  # noqa: BLE001 - metrics must never break a submitted job
        pass


def _store():
    """da/store.py, imported lazily — same pattern as routers/drawings.py. The
    module is on sys.path via write_loop's setup, which importing `jobs` above
    has already performed, so this cannot run before the path is prepared."""
    import store

    return store


PLATFORM_CONTRACT_VERSION = "leaf.platform.v1alpha1"
AUTOFILL_TOOL = "string-autofill-opt"

# The one capability this backend can measure today. Named from the catalog, not
# hand-typed: a typo in a capability id is invisible at runtime, because the
# console's validator simply never matches and the capability shows locked
# forever with nothing reporting why. That is the whole reason the catalog exists.
SOLVE_CAPABILITY = "drawing.solve.strings"

# Catalog entitlement name -> the entitlement-policy capability that grants it.
#
# These are two different vocabularies and they do NOT fully overlap. The policy
# (server/entitlements.py CAPABILITIES) speaks of run_read/run_write/solve/build;
# the catalog speaks of run_read/run/solve/review/author. Only `run_read` and
# `solve` exist on both sides.
#
# Mapping the rest would mean INVENTING policy semantics — deciding whether
# "run an approved tool" is run_read or run_write, or which policy key grants
# "review" — and an entitlement is a security boundary, not a naming convenience.
# So the unmapped names deny, which is the same answer `entitlements_for().get()`
# already gives for an absent key, and `_UNMAPPED_ENTITLEMENTS` records that the
# denial is a DECISION rather than an oversight. A test pins both sets, so adding
# a catalog capability with a new entitlement name fails loudly here instead of
# silently resolving to "not entitled" forever.
_ENTITLEMENT_POLICY_KEY = {
    "run_read": "run_read",
    "solve": "solve",
    # `author` -> `build` is not a guess, it is the mapping this server already
    # enforces: `customization_service.stage` (the tool-authoring path) gates on
    # `entitlements_for(tier).get("build")`, and the policy's own denial message
    # for `build` reads "does not include authoring new tools (Build)". Leaving it
    # unmapped denied `tool.author.company` to demo, self_hosted and hosted_pro,
    # every one of which has `build: True` and can in fact author — the projection
    # would have contradicted the enforcement.
    "author": "build",
}
# `run` and `review` genuinely have no counterpart. `run` could be run_read or
# run_write depending on the tool, and no policy capability corresponds to
# `review` at all. Picking one would be inventing a security boundary.
_UNMAPPED_ENTITLEMENTS = frozenset({"run", "review"})


def _entitled_by_capability(policy: Dict[str, bool]) -> Dict[str, bool]:
    """Resolve each catalog capability's entitlement from the tier policy.

    Fail closed twice over: an entitlement name with no policy mapping denies,
    and a mapped name absent from the policy denies (the policy's own rule is
    that only an explicit `true` grants).
    """
    resolved: Dict[str, bool] = {}
    for entry in capability_catalog.PRODUCT_CAPABILITIES:
        if not entry.declares_entitlement:
            continue
        granted = True
        for name in entry.entitlements:
            key = _ENTITLEMENT_POLICY_KEY.get(name)
            if key is None or policy.get(key) is not True:
                granted = False
                break
        resolved[entry.id] = granted
    return resolved


def _measured_solve_availability(health: Optional[Dict[str, Any]],
                                 now: datetime) -> Dict[str, Any]:
    """Today's hand-built availability, unchanged in substance, now handed to the
    catalog as a MEASUREMENT rather than emitted directly.

    A heartbeat proves a connected runtime, not the staging G1A gate, so this
    still refuses to claim `shipping`. What changes is who arbitrates: the
    catalog validates this payload against the same rules the console applies,
    so a malformed or already-expired lease becomes a locked capability carrying
    `live_availability_rejected_by_contract_validator` instead of being emitted
    and then silently swallowed by the browser.
    """
    # AN EXPIRED LEASE IS AN OFFLINE WORKER, NOT AN INTEGRATOR DEFECT. The
    # heartbeat query admits a row on `observed_at >= now - 15s` INCLUSIVELY, and
    # the lease this builds runs `observed + 15s`, so a row at the edge of that
    # window yields an `expiresAt` that is already in the past. Handing that to
    # the catalog got it refused as `live_availability_rejected_by_contract_validator`
    # — a reason that means "the integrator emitted something malformed" and would
    # send whoever read it hunting a wiring bug that does not exist. Ordinary lease
    # expiry is the `canonical_worker_offline` case, so classify it here where the
    # freshness is actually known.
    if health:
        lease_expiry = (datetime.fromisoformat(str(health["observed_at"]))
                        + timedelta(seconds=capability_catalog.LEASE_TTL_SECONDS))
        if lease_expiry <= now:
            health = None
    observed = str(health["observed_at"]) if health else now.isoformat()
    expires = ((datetime.fromisoformat(observed) if health else now)
               + timedelta(seconds=capability_catalog.LEASE_TTL_SECONDS)).isoformat()
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
    return {
        "contractVersion": PLATFORM_CONTRACT_VERSION,
        "authority": "leaf-platform-registry",
        "productCapability": SOLVE_CAPABILITY,
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


class RunRequest(BaseModel):
    tool: str
    params: Dict[str, Any] = {}
    dwg: str = "rooftop_demo"
    dwg_version: Optional[int] = None  # None -> head (unchanged default); pin to a
    catalog_digest: Optional[str] = None
    expected_drawing_head: Optional[int] = None
    catalog_commit: Optional[str] = None
    effective_catalog_digest: Optional[str] = None
    tool_manifest_sha256: Optional[str] = None
    # specific immutable drawing version (da/store.py resolve_version) otherwise.
    #
    # SINGLE-WRITER IDENTITY IS NOT A BODY FIELD. `holder`/`fence` used to live
    # here, and a caller could choose both. That was the hole: GET /versions
    # publishes the current holder and used to publish its fence, so any
    # same-tenant session could read the pair and present it to publish under
    # someone else's lease. Pydantic drops unknown fields, so a client still
    # sending them is not rejected — its run is simply UNNAMED, which the store
    # refuses against every active lock (`store.ANONYMOUS_HOLDER`). That is the
    # right failure: a client that has not been issued a capability has not
    # proven anything.
    #
    # The identity now arrives as the opaque `X-Checkout-Capability` header
    # (server/checkout_capability.py), which the route exchanges for the
    # manifest's own (holder, fence) before submitting. Header, not body, so the
    # credential is never persisted into the durable job record and never
    # forwarded to the broker — only the pair it proves is.


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


def _active_binding_tenant(tenant: Any) -> Optional[str]:
    if deps.auth_live() and isinstance(tenant, deps.TenantContext) and tenant.subject:
        binding = jobs.platform_link.platform_store().resolve_active_identity_binding(
            "auth0", tenant.subject)
        if binding is not None:
            return str(binding.platform_tenant_id)
    return None


def _bound_tenant_id(tenant: Any) -> str:
    """Prefer the active server-owned platform binding over stale JWT org claims."""
    return _active_binding_tenant(tenant) or str(tenant)


def _legacy_drawing_head(tenant_id: str, drawing_id: str) -> int:
    """Read the current legacy drawing head for an optimistic write check."""
    import store
    import write_loop

    backend = write_loop.backend_for_tenant(
        tenant_id,
        aps_live=False,
        da=None,
    )
    write_loop.ensure_demo_drawing(backend, tenant_id, drawing_id)
    manifest = store.load_manifest(backend, tenant_id, drawing_id)
    return int(manifest["head"])


def _canonical_tenant_id(tenant: Any, context: Dict[str, Any]) -> str:
    """Bind JWT tenant/org claims to the server-owned platform identity."""
    canonical = str(context["org_id"])
    if deps.auth_live():
        if not isinstance(tenant, deps.TenantContext) \
                or _active_binding_tenant(tenant) != canonical:
            raise ValueError(
                "verified subject must match the active platform identity binding")
    return canonical if context.get("authority_mode") == "postgres_canonical" else str(tenant)


def _checkout_identity(tenant_id: Any, drawing_id: str,
                       capability: Optional[str]) -> Tuple[str, Optional[int]]:
    """Exchange the presented capability for the (holder, fence) the write path
    carries. Never trusts anything the caller named.

    Returns `(ANONYMOUS_HOLDER, None)` when the drawing has no ACTIVE checkout or
    when its state cannot be read at all: the run then publishes freely on an
    unlocked drawing and is refused against any live lease, which is the
    fail-closed answer for a caller that has proven nothing.

    A REJECTED capability is deliberately not an error here. The gate that
    matters runs in the store, under the row lock, at the moment of publish
    (`da/store.py put_drawing`), and a read tool with a stale header must not be
    blocked by a lock it never consults. Presenting a bad capability therefore
    lands the caller in exactly the unnamed case: refused against any active
    lock, allowed on an unlocked drawing.
    """
    store = _store()
    anonymous = str(store.ANONYMOUS_HOLDER)
    try:
        backend = write_loop.backend_for_tenant(str(tenant_id), aps_live=False, da=None)
        co = store.load_manifest(backend, str(tenant_id), drawing_id).get("checkout")
        if not store.checkout_active(co):
            return anonymous, None
        holder, fence = checkout_capability.verify(
            capability, tenant_id, drawing_id, co)
    except Exception:  # noqa: BLE001 - unreadable state or a bad capability -> unnamed
        return anonymous, None
    return holder, fence


@router.post("/api/run")
def run(req: RunRequest, wait: int = 0, tenant_id: Any = Depends(deps.require_tenant),
        x_org_id: Optional[str] = Header(default=None),
        x_project_id: Optional[str] = Header(default=None),
        idempotency_key: Optional[str] = Header(default=None, alias="Idempotency-Key"),
        authorization: Optional[str] = Header(default=None),
        x_checkout_capability: Optional[str] = Header(default=None),
        authority_session_id: Optional[str] = Header(
            default=None, alias="X-Authority-Session-Id"),
        authority_turn_id: Optional[str] = Header(
            default=None, alias="X-Authority-Turn-Id")):
    """Submit a tool run as a durable background job (202), or block with ?wait=1.

    OPTIONAL project context: when the caller sends BOTH ``X-Org-Id`` and
    ``X-Project-Id``, the run is additionally recorded as a canonical platform
    Job (best-effort, env-gated — see server/platform_link.py). Absent either
    header the behaviour is byte-identical to before.

    SINGLE-WRITER: a `drawing.write` run publishes a version, so it must prove it
    owns the drawing's checkout. `X-Checkout-Capability` (from POST
    /api/drawings/{id}/checkout) is exchanged HERE for the manifest's own
    (holder, fence), and only that pair travels on to the broker and the store.
    No capability, or a stale one, means the run is unnamed and is refused
    against any active lock.
    """
    # SUBMIT-LATENCY CLOCK (emf SubmitLatency, emitted just before the 202).
    # Authority resolution is part of submission and belongs inside this clock.
    submit_t0 = time.perf_counter()
    resolved_tenant = deps.backedge_run_identity(
        tenant_id, authority_session_id, authority_turn_id
    )
    if resolved_tenant is None:
        return error_response(
            ErrorCode.FORBIDDEN,
            "active same-account turn authority is required for conversational runs",
            retryable=False,
            status_code=403,
        )
    tenant_id = resolved_tenant

    # Started at the first statement of the body so it covers every unit of work
    # this handler does to turn a request into a job_id. It does NOT cover the
    # ASGI/dependency prologue that ran before the body (deps.require_tenant and
    # request-body parsing), so the number is a floor on the request's server
    # time, not the whole of it. That is the honest boundary available without a
    # middleware, and the work it misses is fixed-cost: a regression in what this
    # contract is about — resolution, gating, and the durable insert below — is
    # inside the window.
    # TENANT-SCOPED resolution (wave 4): resolve the tool from the REQUESTING tenant's
    # catalog (globals + that tenant's own repo tools). A tool authored by another
    # tenant is not in this tenant's catalog -> UNKNOWN_TOOL, so it can never be run
    # cross-tenant. The tenant_id then threads jobs -> broker -> tool_loader so entry
    # resolution + execution read the same tenant's repo.
    tool = deps.find_tool(req.tool, str(tenant_id))
    if tool is None:
        return error_response(ErrorCode.UNKNOWN_TOOL, f"unknown tool: {req.tool}",
                              retryable=False, tool=req.tool)
    current_catalog_digest = deps.catalog_tool_digest(tool)
    if not isinstance(req.catalog_digest, str) or not hmac.compare_digest(
            req.catalog_digest, current_catalog_digest):
        return error_response(
            ErrorCode.BAD_PARAMS,
            "catalog tool changed or confirmation digest is missing; refresh tools and confirm again",
            retryable=False, tool=req.tool, status_code=409,
        )

    # ENTITLEMENT GATE (§17): the tenant's tier must grant the capability this tool needs
    # (run_write for a drawing.write tool, else run_read). Enforced HERE in the execution
    # chain — before job submission, on BOTH the async and ?wait=1 paths — so it cannot be
    # bypassed by the UI. Off-auth/demo tier grants everything (friction-free).
    tier = entitlements.resolve_tier(tenant_id)
    roles, elevated = entitlements.resolve_roles(tenant_id)
    required = entitlements.tool_required_capability(tool)
    try:
        allowed = entitlements.entitlements_for(tier, roles, elevated).get(required, False)
    except entitlements.EntitlementsError:
        # Present-but-untrustworthy policy file: refuse with the structured
        # 503, never an unstructured 500 (and never an allow).
        return entitlements.policy_unavailable_response(required, tier)
    if not allowed:
        return entitlements.entitlement_denied_response(required, tier)

    # merge authored default_params under caller params
    params = dict(tool.get("default_params", {}))
    params.update(req.params or {})

    # Exchange the capability for the lock's OWN (holder, fence) before anything
    # is submitted, so the identity that reaches the store was read from the
    # manifest rather than chosen by the caller.
    # write_loop resolves the target from `params.drawing_id`, NOT from `dwg`
    # (which names the intake source). Asking it means the capability is verified
    # against the drawing that will actually be published to.
    target_drawing_id = write_loop.target_drawing_id(params)
    checkout_holder, checkout_fence = _checkout_identity(
        tenant_id, target_drawing_id, x_checkout_capability)

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
            if req.expected_drawing_head is not None:
                raise ValueError(
                    "expected_drawing_head applies only to legacy versioned drawings")
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
            exact_pins_required = required == "run_write" and (
                os.environ.get("LEAF_EXACT_WRITE_PINS_REQUIRED", "0") == "1"
                or (
                    deps.auth_live()
                    and isinstance(tenant_id, deps.TenantContext)
                    and getattr(tenant_id, "backedge", False)
                )
            )
            exact_pin_values = (
                req.expected_drawing_head,
                req.dwg_version,
                req.catalog_commit,
                req.effective_catalog_digest,
                req.tool_manifest_sha256,
            )
            if exact_pins_required and any(value is None for value in exact_pin_values):
                raise ValueError(
                    "conversational write requires exact catalog and drawing pins")
            if req.expected_drawing_head is not None:
                if req.dwg_version != req.expected_drawing_head:
                    raise ValueError(
                        "approved drawing version does not match expected drawing head")
                if not all((req.catalog_commit, req.effective_catalog_digest,
                            req.tool_manifest_sha256)):
                    raise ValueError(
                        "write approval is missing exact catalog generation pins")
                if not hmac.compare_digest(
                        req.tool_manifest_sha256 or "", current_catalog_digest):
                    raise ValueError(
                        "tool manifest changed after approval; refresh tools and confirm again")
                current_pin = (
                    customization_service.effective_catalog_pin(str(tenant_id))
                    or deps.base_catalog_pin(deps.all_tools(str(tenant_id)))
                )
                if not (
                    hmac.compare_digest(
                        req.catalog_commit or "", current_pin["catalog_commit"]
                    )
                    and hmac.compare_digest(
                        req.effective_catalog_digest or "",
                        current_pin["effective_catalog_digest"],
                    )
                ):
                    raise ValueError(
                        "effective catalog changed after approval; refresh tools and confirm again")
                current_head = _legacy_drawing_head(str(tenant_id), req.dwg)
                if current_head != req.expected_drawing_head:
                    raise ValueError(
                        "drawing head changed after approval; refresh drawing state and confirm again")
            if required == "run_write":
                store = _store()
                if store.authority_mode() == "postgres":
                    # Refuse before the durable job insert. The worker repeats
                    # this check immediately before paid APS work, closing the
                    # race between this preflight and execution. Legacy keeps
                    # its first-write bootstrap and unlocked-write contract.
                    backend = write_loop.backend_for_tenant(
                        str(tenant_id), aps_live=False, da=None)
                    try:
                        store.authorize_checkout(
                            backend, str(tenant_id), target_drawing_id,
                            checkout_holder, checkout_fence)
                    except store.CheckoutDenied as exc:
                        return error_response(
                            ErrorCode.FORBIDDEN, str(exc), retryable=False,
                            status_code=403)
            job_id = jobs.submit_job(
                tenant_id, tool, params, req.dwg, aps_live=deps.APS_LIVE,
                org_id=resolved_org, project_id=resolved_project,
                dwg_version=req.dwg_version,
                idempotency_key=idempotency_key, authority_mode=authority_mode,
                platform_context=platform_context,
                # Derived from the VERIFIED capability, never from the request
                # body. A caller that proved nothing gets the RESERVED anonymous
                # id, which no lock can ever hold (store.acquire_checkout refuses
                # it), so its run is refused against every active lock and still
                # publishes freely on an unlocked drawing.
                checkout_holder=checkout_holder,
                checkout_fence=checkout_fence,
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

    body = deps.tenant_echo(
        with_envelope_fields({"job_id": job_id, "status": "submitted"}), tenant_id
    )
    # Measured HERE, on the 202 path ONLY. `?wait=1` above blocks on execution and
    # returns 200, so timing it would put engine seconds into a submit metric and
    # recreate exactly the coupling test_1b exists to forbid. Two request
    # populations with two different contracts do not share one gauge.
    _emit_submit_latency(
        (time.perf_counter() - submit_t0) * 1000.0,
        deps.APS_LIVE, str(tenant_id), tool.get("name") or req.tool, job_id,
    )
    return JSONResponse(status_code=202, content=body)


@router.get("/api/jobs/{job_id}")
def get_job(job_id: str, tenant=Depends(deps.require_tenant)):
    tenant_id = _bound_tenant_id(tenant)
    rec = _job_for_tenant(job_id, tenant_id)
    # 404 (never 403) both when the job is unknown AND when it belongs to another
    # tenant — no cross-tenant existence leak (security-audit F8).
    if rec is None or rec.get("tenant_id") != tenant_id:
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
    tenant_id = _bound_tenant_id(tenant)
    _rec0 = await asyncio.to_thread(_job_for_tenant, job_id, tenant_id)
    if _rec0 is None or _rec0.get("tenant_id") != tenant_id:
        return error_response(ErrorCode.BAD_PARAMS, f"unknown job_id: {job_id}",
                              retryable=False, status_code=404)

    async def event_stream():
        last = None
        deadline = time.time() + jobs.job_max_s() + 60
        while time.time() < deadline:
            rec = await asyncio.to_thread(_job_for_tenant, job_id, tenant_id)
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
    canonical = jobs.platform_link.list_canonical_jobs(_bound_tenant_id(tenant), limit=limit)
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
    roles, elevated = entitlements.resolve_roles(tenant)
    try:
        policy = entitlements.entitlements_for(tier, roles, elevated)
    except entitlements.EntitlementsError:
        # An unreadable policy grants nothing; the projection reports
        # not-entitled while the write path answers with the structured 503.
        policy = {}
    # THE CATALOG EMITS, NOT THIS ENDPOINT. Until now this route hand-built ONE
    # descriptor inline: its id, label, description, tool capabilities and
    # entitlements were all typed here, and the other six release-gate
    # capabilities had no entry at all, so the console could not even name them.
    # Every id and label now comes from the ratified catalog, which is pinned
    # character-for-character against leaf_website's `capabilityCatalog.ts`, and
    # the six unmeasured capabilities come back fail-closed rather than missing.
    #
    # The measurement below is the ONLY thing this route still owns, which is the
    # split the catalog module was written for: it holds no routing and no health
    # probing, and this route holds no capability vocabulary.
    live = {SOLVE_CAPABILITY: _measured_solve_availability(health, now)}
    descriptors = capability_catalog.build_descriptors(
        live, _entitled_by_capability(policy), now)
    return with_envelope_fields({
        "contractVersion": PLATFORM_CONTRACT_VERSION,
        "projectId": str(context["project_id"]),
        "capabilities": descriptors,
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
