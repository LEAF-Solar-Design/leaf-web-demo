"""Internal project Solar policy, with no HTTP registration.

project_runs_enabled() returns a boolean, reading the exact enable flag at call time.
project_tool_admission(...) returns {status_code, content}; refusals contain the complete response body, submissions contain a job_id, previews contain no job_id.
project_catalog_availability(tool, *, enabled, admission=None) returns a fresh availability dictionary; admission=None means no exact request was checked.
"""
from __future__ import annotations

from copy import deepcopy
import json
import os
from uuid import UUID

from fastapi import HTTPException

import deps
import dependency_health
import entitlements
import platform_link
from demo_policy import demo_run_verdict
from demo_actor import DemoActorRefused, DemoActorUnavailable, is_demo_context, resolve_demo_actor
import solar_project_context as project
import solar_project_graph as graph
import solar_project_jobs as service
import solar_tools
from envelopes import ErrorCode, err_envelope, with_envelope_fields
from leaf_platform.entitlements import EntitlementDenied
from solar_design_graph import GraphValidationError
from leaf_cloud_grants import CloudError


_FAILURES = {
    "demo_actor_refused": (403, False),
    "demo_tool_not_allowed": (403, False),
    "demo_paid_service_not_allowed": (403, False),
    "SIP_R1_INVALID_BINDING": (400, False), "SIP_R1_CONTEXT_NOT_FOUND": (404, False),
    "SIP_R1_PROJECT_FORBIDDEN": (403, False), "SIP_R1_CANONICAL_AUTHORITY_REQUIRED": (409, False),
    "SIP_R1_INTAKE_PROOF_REQUIRED": (409, False), "SIP_R1_INTAKE_REFERENCE_INVALID": (500, False),
    "SIP_R1_INTAKE_DIGEST_MISMATCH": (500, False), "SIP_R1_INTAKE_INVALID": (500, False),
    "SIP_R1_INTAKE_UNAVAILABLE": (503, True), "SIP_R1_STORE_UNAVAILABLE": (503, True),
    "SIP_R1_CHECKOUT_PARAMS_INVALID": (400, False), "SIP_R1_CHECKOUT_CONFLICT": (409, True),
    "SIP_R1_CHECKOUT_REQUIRED": (409, False), "SIP_R1_CHECKOUT_EXPIRED": (409, False),
    "SIP_R1_CHECKOUT_DENIED": (403, False), "SIP_R1_CHECKOUT_STALE": (409, False),
    "SIP_R1_CHECKOUT_UNAVAILABLE": (503, True), "SIP_R1_FENCE_EXHAUSTED": (409, False),
    "SIP_R1_WRITES_DRAINED": (503, True), "SIP_R1_STALE_VERSION": (409, False),
    "SIP_R1_INTERNAL": (500, False),
    "SIP_R2_PUBLICATION_PARAMS_INVALID": (400, False),
    "SIP_R2_IDEMPOTENCY_CONFLICT": (409, False), "SIP_R2_PUBLICATION_REQUIRED": (409, False),
    "SIP_R3_TOOL_UNSUPPORTED": (409, False), "SIP_R3_TOOL_MANIFEST_MISMATCH": (409, False),
    "SIP_R3_SERVICE_EVIDENCE_REQUIRED": (409, False), "SIP_R3_PROOF_REJECTED": (500, False),
    "SIP_R4_IDEMPOTENCY_KEY_REQUIRED": (400, False), "SIP_R4_IDEMPOTENCY_CONFLICT": (409, False),
    "SIP_R4_JOB_BINDING_MISMATCH": (409, False),
    "SIP_R4_WORKER_UNAVAILABLE": (503, True),
}
_UNAVAILABLE = "Project Solar admission unavailable."
_RESOLVER_MESSAGES = frozenset((
    "X-Project-Id is required with X-Org-Id",
    "platform database is required for a project-scoped run",
    "X-Org-Id is required for a project-scoped run",
    "project context does not belong to the verified platform tenant",
    "project context was not found for the verified platform tenant",
    "verified subject must match the active platform identity binding",
))
_UNAUTHENTICATED = "Authentication is required for project Solar admission."
_FORBIDDEN = "Project Solar admission is not permitted for this identity."
_IDENTITY_REFUSALS = frozenset((
    "token verified but carries no external subject",
    "verified subject has no active platform identity binding",
    "platform role does not permit mutation",
))


def project_runs_enabled():
    return os.environ.get("LEAF_SOLAR_PROJECT_RUN_ENABLED", "0") == "1"


def _refusal(reason, *, status=409, retryable=False, message=_UNAVAILABLE):
    code = ErrorCode.UNAUTHENTICATED if status == 401 else (
        ErrorCode.FORBIDDEN if status == 403 else (
            ErrorCode.INTERNAL if status >= 500 else ErrorCode.BAD_PARAMS))
    body = err_envelope(code, message, retryable)
    if reason is not None:
        body["error"]["reason_code"] = reason
    return {"status_code": status, "content": body}


def _http_refusal(exc):
    """Classify an identity HTTPException as the app handler does; never echo free text.

    401 and 403 keep their status and code. A 403 carries its detail only when it is one
    of the three fixed identity sentences; every other detail becomes a fixed sentence,
    because a 401 detail can embed the token verifier's own error text.
    """
    if exc.status_code == 401:
        return _refusal(None, status=401, message=_UNAUTHENTICATED)
    if exc.status_code == 403:
        detail = exc.detail
        known = type(detail) is str and detail in _IDENTITY_REFUSALS
        return _refusal(None, status=403, message=detail if known else _FORBIDDEN)
    return _refusal("SIP_R1_INTERNAL", status=500)


def _response(response):
    return {"status_code": response.status_code, "content": json.loads(response.body)}


def _trusted(tool):
    name = tool.get("name") if isinstance(tool, dict) else None
    if not isinstance(name, str) or name not in graph.SUPPORTED_TOOLS:
        return None, "SIP_R3_TOOL_UNSUPPORTED"
    trusted = solar_tools.trusted_record(name)
    if trusted is None or deps.catalog_tool_digest(tool) != deps.catalog_tool_digest(trusted):
        return None, "SIP_R3_TOOL_MANIFEST_MISMATCH"
    return trusted, None


def project_tool_admission(tool, tenant, *, org_header, project_header, authorization,
                           input_version_id, drawing_id, params, catalog_digest,
                           idempotency_key, checkout_capability, preview, solve_context=None,
                           proposal_job_id=None, expected_intake_sha256=None):
    if not project_runs_enabled():
        return _refusal("project_execution_disabled")
    try:
        if is_demo_context(tenant):
            demo_params = deepcopy(tool.get("default_params", {}))
            demo_params.update(deepcopy(params or {}))
            reason = demo_run_verdict(tenant, tool.get("name"), demo_params)
            if reason:
                return _refusal(reason, status=403)
        else:
            tier = entitlements.resolve_tier(tenant)
            roles, elevated = entitlements.resolve_roles(tenant)
            required = entitlements.tool_required_capability(tool)
            try:
                allowed = entitlements.entitlements_for(tier, roles, elevated).get(required, False)
            except entitlements.EntitlementsError:
                return _response(entitlements.policy_unavailable_response(required, tier))
            if not allowed:
                return _response(entitlements.entitlement_denied_response(required, tier))
        normalized = deepcopy(tool.get("default_params", {}))
        normalized.update(deepcopy(params or {}))
        trusted, reason = _trusted(tool)
        if reason:
            return _refusal(reason)
        manifest = deps.catalog_tool_digest(trusted)
        if catalog_digest != manifest:
            return _refusal("SIP_R3_TOOL_MANIFEST_MISMATCH", message=
                "tool manifest changed after approval; refresh tools and confirm again")
        delegated = False
        if checkout_capability is not None and not isinstance(checkout_capability, str):
            from project_checkout_custody import DelegatedCheckout
            delegated = isinstance(checkout_capability, DelegatedCheckout)
        try:
            if delegated:
                # Backedges retain the verified subject, never a borrowed bearer.
                bound = checkout_capability.input_binding
                if drawing_id is None:
                    drawing_id = bound["drawing_id"]
                if (str(tenant) != bound["organization_id"]
                        or org_header != bound["organization_id"]
                        or project_header != bound["project_id"]
                        or str(input_version_id) != bound["input_version_id"]
                        or str(drawing_id) != bound["drawing_id"]
                        or expected_intake_sha256 != bound["intake_sha256"]
                        or idempotency_key != checkout_capability.idempotency_key):
                    project.refuse("CHECKOUT_DENIED")
                org = platform_link.require_project_access(
                    tenant, UUID(bound["project_id"]), write=True)
                if str(org) != bound["organization_id"]:
                    project.refuse("PROJECT_FORBIDDEN")
                context = platform_link.resolve_project_authority(
                    UUID(bound["organization_id"]), UUID(bound["project_id"]))
            else:
                context = platform_link.resolve_submission_context(
                    org_header, project_header, authorization,
                    **({"tenant_context": tenant} if is_demo_context(tenant) else {}))
        except project.ProjectContextError:
            raise
        except platform_link.ProjectSessionForbidden:
            project.refuse("PROJECT_FORBIDDEN")
        except platform_link.ProjectAuthorityRequired:
            project.refuse("CANONICAL_AUTHORITY_REQUIRED")
        except (platform_link.ProjectAuthorityNotFound, LookupError):
            project.refuse("CONTEXT_NOT_FOUND")
        except DemoActorUnavailable:
            return _refusal("SIP_R1_STORE_UNAVAILABLE", status=503, retryable=True)
        except ValueError as exc:
            message = exc.args[0] if exc.args and exc.args[0] in _RESOLVER_MESSAGES else _UNAVAILABLE
            return _refusal(None, status=400 if "required" in message else 409, message=message)
        except DemoActorRefused:
            raise
        except Exception:
            if is_demo_context(tenant):
                return _refusal("SIP_R1_STORE_UNAVAILABLE", status=503, retryable=True)
            raise
        if context is None:
            return _refusal(None, status=400, message="X-Project-Id is required with X-Org-Id")
        try:
            actor = resolve_demo_actor(tenant)
        except DemoActorRefused:
            raise
        except Exception:
            if is_demo_context(tenant):
                return _refusal("SIP_R1_STORE_UNAVAILABLE", status=503, retryable=True)
            raise
        if str(context["org_id"]) != str(actor.org_id if actor else tenant) or (
                deps.auth_live() and (not isinstance(tenant, deps.TenantContext) or not tenant.subject)):
            return _refusal(None, message="verified subject must match the active platform identity binding")
        if context.get("authority_mode") != "postgres_canonical":
            project.refuse("CANONICAL_AUTHORITY_REQUIRED")
        try:
            parent = UUID(str(input_version_id))
        except (ValueError, TypeError, AttributeError):
            return _refusal(None, status=400, message="a canonical drawing version UUID is required")
        try:
            if drawing_id is None and trusted["name"] in ("solar-solve-proposal", "solar-commit-solve"):
                org, _actor = project._access(tenant, UUID(str(context["project_id"])), write=True)
                binding = project.graph_store().resolve_version_binding(
                    org, UUID(str(context["project_id"])), parent)
                drawing_id = binding.drawing_id
            drawing = UUID(str(drawing_id))
        except (ValueError, TypeError, AttributeError):
            return _refusal(None, status=400,
                message="a canonical drawing artifact UUID is required in params.drawing_id")
        args = (tenant, UUID(str(context["project_id"])), drawing, parent, trusted["name"], normalized)
        kwargs = {"tool_manifest_sha256": manifest, "checkout_capability": checkout_capability,
                  "idempotency_key": idempotency_key}
        if delegated:
            kwargs["expected_intake_sha256"] = expected_intake_sha256
        if trusted["name"] == "solar-solve-proposal":
            kwargs["solve_context"] = solve_context
        if trusted["name"] == "solar-commit-solve":
            kwargs["proposal_job_id"] = proposal_job_id
        if preview:
            row = service.submit_project_graph_job(*args, **kwargs, preview=True)
            return {"status_code": 200, "content": with_envelope_fields(
                {"admissible": True, "replay": "job_id" in row})}
        if delegated or trusted["name"] in ("solar-solve-proposal", "solar-commit-solve"):
            job_id = str(service.submit_project_graph_job(*args, **kwargs)["job_id"])
        else:
            job_id = platform_link.submit_canonical_graph(*args, **kwargs)
        return {"status_code": 202, "content": deps.tenant_echo(with_envelope_fields(
            {"job_id": job_id, "status": "submitted"}), tenant)}
    except DemoActorRefused:
        return _refusal("demo_actor_refused", status=403)
    except (platform_link.CanonicalEntitlementDenied, EntitlementDenied) as exc:
        return _response(exc.response)
    except GraphValidationError as exc:
        return _refusal(exc.code, message="Canonical Solar request was refused.")
    except CloudError as exc:
        statuses = {"cloud_request_invalid": 400, "cloud_auth_missing": 401,
                    "cloud_tenant_unauthorized": 403, "cloud_upstream_failure": 502,
                    "cloud_response_invalid": 502}
        reason = exc.classification if exc.classification in statuses else "cloud_upstream_failure"
        return _refusal(reason.upper(), status=statuses[reason],
                        message="Canonical Solar cloud sizing was refused.")
    except project.ProjectContextError as exc:
        reason = exc.reason_code if exc.reason_code in _FAILURES else "SIP_R1_INTERNAL"
        status, retryable = _FAILURES[reason]
        return _refusal(reason, status=status, retryable=retryable)
    except HTTPException as exc:
        return _http_refusal(exc)
    except Exception:
        return _refusal("SIP_R1_INTERNAL", status=500)


def project_drawing_tool_availability(tool, tenant, *, read_tool, exact_version, write_tool=False):
    """Project availability for a non-Solar catalog tool, derived from its executor.

    read_tool is the run route's own admission predicate for the existing project read
    executor (routers.jobs._is_project_read_tool), which binds the run to an exact canonical
    version. write_tool is the run route's project write predicate (a registered drawing.write
    builtin with a server-owned producer in project_drawing_write). Only such a tool with an
    exact version is available, and only when the tenant is entitled to the tool's required
    capability. Every other non-Solar tool has no project executor and stays typed
    unavailable. Never raises.
    """
    entitled = False
    entitlement_reason = None
    try:
        tier = entitlements.resolve_tier(tenant)
        roles, elevated = entitlements.resolve_roles(tenant)
        required = entitlements.tool_required_capability(tool)
        entitled = bool(entitlements.entitlements_for(tier, roles, elevated).get(required, False))
        if not entitled:
            entitlement_reason = "entitlement_required"
    except entitlements.EntitlementsError:
        entitlement_reason = "entitlement_policy_unavailable"
    engine = bool(read_tool or write_tool)
    ready = engine and bool(exact_version)
    engine_reason = None if engine else "project_adapter_unavailable"
    input_reason = None if ready else (engine_reason or "project_request_required")
    reason = engine_reason or (None if ready else input_reason) or entitlement_reason
    return {
        "entitled": entitled, "engine_ready": engine, "implemented": True,
        "input_ready": ready, "input_reason": input_reason,
        "entitlement_reason": entitlement_reason, "engine_reason": engine_reason,
        "implementation_reason": None, "refusal_reasons": [reason] if reason else [],
        "runnable": entitled and ready, "admission_checked": False, "admission": None,
        "project_read": ready and bool(read_tool),
        "project_write": ready and bool(write_tool) and not read_tool,
    }


def project_catalog_availability(tool, *, enabled, admission=None):
    """Project policy and required worker coverage without resolving a request."""
    entitled = engine = ready = checked = False
    error = detail = None
    entitlement_reason = None
    engine_reason = None
    if not enabled:
        reason = engine_reason = "project_execution_disabled"
    elif _trusted(tool)[1] is not None:
        reason = engine_reason = "project_adapter_unavailable"
    elif admission is None:
        engine = True
        reason = "project_request_required"
        try:
            if not dependency_health.canonical_worker_covers(tool["name"]):
                engine = False
                reason = engine_reason = "SIP_R4_WORKER_UNAVAILABLE"
        except dependency_health.DependencyUnavailable:
            engine = False
            reason = engine_reason = "SIP_R1_STORE_UNAVAILABLE"
    else:
        engine = checked = True
        error = deepcopy(admission["content"].get("error"))
        detail = {"status_code": admission["status_code"], "error": error}
        if admission["status_code"] < 400 and error is None:
            entitled = ready = True
            reason = None
        elif admission["status_code"] == 503 and admission["content"].get("entitlement_required"):
            reason = entitlement_reason = "entitlement_policy_unavailable"
        elif admission["content"].get("entitlement_required"):
            reason = entitlement_reason = "entitlement_required"
        elif error and error.get("reason_code") == "SIP_R4_WORKER_UNAVAILABLE":
            engine = False
            reason = engine_reason = "SIP_R4_WORKER_UNAVAILABLE"
        else:
            reason = "capability_not_ready"
    return {
        "entitled": entitled, "engine_ready": engine, "implemented": True,
        "input_ready": ready, "input_reason": None if ready else reason,
        "entitlement_reason": entitlement_reason, "engine_reason": engine_reason,
        "implementation_reason": None, "refusal_reasons": [reason] if reason else [],
        "runnable": entitled and engine and ready,
        "admission_checked": checked, "admission": detail,
    }
