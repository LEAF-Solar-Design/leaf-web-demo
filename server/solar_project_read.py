"""Synchronous, bounded reads of exact verified canonical Solar graphs."""
from copy import deepcopy
from hashlib import sha256
from uuid import UUID

from fastapi.responses import JSONResponse
import deps
import entitlements
import solar_local_read as local
import solar_project_admission as policy
import solar_project_artifacts as artifacts
import solar_project_context as project
import solar_project_graph as graph_adapter
import solar_tools
from envelopes import with_envelope_fields
from leaf_cloud_client import canonical_bytes
from leaf_platform import entitlements as stored
from solar_design_graph import GraphValidationError, _bounded_json
from tool_validate import validate_params

MAX_REQUEST_BYTES = 262144
MAX_OUTPUT_BYTES = local.MAX_OUTPUT_BYTES
RESULT_SCHEMA = "leaf.solar-project-read.v1"
REQUEST_SCHEMA = "leaf.solar-project-read-request.v1"
SUPPORTED_READ_TOOLS = frozenset((
    "solar-select-by-zone", "solar-design-presets-list", "solar-autofill-plan",
    "solar-string-rebuild", "solar-string-data", "solar-color-strings", "solar-guardrails-read",
    "solar-electrical-schedules", "solar-cable-export", "solar-nec-ampacity-correction",
    "solar-nec-ac-voltage-drop", "solar-nec-conduit-fill", "solar-nec-feeder-ocpd",
))


def _fail(code):
    raise GraphValidationError(code)


def _parameters(project_id, drawing_id, version_id, tool_name, params, catalog_digest, current):
    if (any(not isinstance(value, UUID) for value in (project_id, drawing_id, version_id))
            or type(tool_name) is not str or not tool_name
            or type(catalog_digest) is not str or not catalog_digest
            or type(params) is not dict or type(current) is not bool):
        _fail("SIP_R6_INVALID_REQUEST")
    try:
        request = {"tool": tool_name, "params": params, "catalog_digest": catalog_digest}
        if current:
            request["current"] = True
        _bounded_json(request)
        raw = canonical_bytes(request)
    except (ValueError, TypeError, RecursionError, GraphValidationError):
        _fail("SIP_R6_INVALID_REQUEST")
    if len(raw) > MAX_REQUEST_BYTES:
        _fail("SIP_R6_REQUEST_TOO_LARGE")
    if "drawing_id" in params and (type(params["drawing_id"]) is not str
                                    or params["drawing_id"] != str(drawing_id)):
        _fail("SIP_R6_INVALID_REQUEST")
    copied = deepcopy(params)
    copied.pop("drawing_id", None)
    return copied


def _trusted(tenant, name, catalog_digest):
    if name not in SUPPORTED_READ_TOOLS:
        _fail("SIP_R6_TOOL_UNSUPPORTED")
    effective = deps.find_tool(name, str(tenant))
    trusted = solar_tools.trusted_record(name)
    try:
        matches = (type(effective) is dict and type(trusted) is dict
                   and deps.catalog_tool_digest(effective) == deps.catalog_tool_digest(trusted)
                   and catalog_digest == deps.catalog_tool_digest(trusted))
    except (ValueError, TypeError, UnicodeError, RecursionError):
        matches = False
    if not matches:
        _fail("SIP_R6_TOOL_MANIFEST_MISMATCH")
    declaration = solar_tools.get(name)
    module = local._load_builtin(name)
    if (type(declaration) is not dict or declaration["adapter"] != "local-graph-read" or declaration["trusted_inputs"]
            or entitlements.tool_required_capability(trusted) != "run_read"
            or local._reads_physical_head(module) or local._reads_version_history(module)):
        _fail("SIP_R6_TOOL_UNSUPPORTED")
    return trusted


def run_project_read(tenant, project_id, drawing_id, input_version_id, tool_name, params, *,
                     catalog_digest, current=False):
    if not policy.project_runs_enabled():
        _fail("project_execution_disabled")
    copied = _parameters(project_id, drawing_id, input_version_id, tool_name, params,
                         catalog_digest, current)
    trusted = _trusted(tenant, tool_name, catalog_digest)
    tier = entitlements.resolve_tier(tenant)
    roles, elevated = entitlements.resolve_roles(tenant)
    try:
        allowed = entitlements.entitlements_for(tier, roles, elevated).get("run_read", False)
    except entitlements.EntitlementsError:
        return entitlements.policy_unavailable_response("run_read", tier)
    if not allowed:
        return entitlements.entitlement_denied_response("run_read", tier)
    verified = project.resolve_context(tenant, project_id, input_version_id,
                                       drawing_id=drawing_id, write=False)
    context = graph_adapter.resolve_project_graph_context(verified)
    if context.graph is None:
        _fail("GRAPH_NOT_EMBEDDED")
    denial, _org = stored.stored_job_entitlement_verdict(context.organization_id, "extract")
    if denial is not None:
        return denial
    normalized = deepcopy(trusted.get("default_params", {}))
    normalized.update(copied)
    if validate_params(trusted, normalized):
        _fail(solar_tools.get(tool_name)["invalid_request_code"])
    request = {"schema": REQUEST_SCHEMA,
        "organization_id": str(context.organization_id), "project_id": str(project_id),
        "drawing_id": str(drawing_id), "input_version_id": str(input_version_id),
        "input_intake_sha256": context.intake_sha256, "graph_sha256": context.graph_sha256,
        "tool": tool_name, "tool_manifest_sha256": catalog_digest, "parameters": normalized}
    request_sha = sha256(canonical_bytes(request)).hexdigest()
    sink = artifacts.ProjectArtifactSink(None, artifacts.project_artifact_binding(
        context, tool_name, catalog_digest, request_sha))
    output, data = local._read_output(tool_name, context.graph, normalized, sink)
    result = {"schema": RESULT_SCHEMA, "adapter": "project-local-graph-read",
        **{key: value for key, value in request.items() if key not in ("schema", "parameters")},
        "graph_project_id": context.graph_project_id,
        "head_version_id": str(verified.binding.head_version_id), "is_head": verified.binding.is_head,
        "current_required": current, "request_sha256": request_sha, "output": output,
        "output_sha256": sha256(data).hexdigest(), "output_bytes": len(data), "drawing_changed": False}
    body = with_envelope_fields({"result": result})
    # Use the actual HTTP serializer too: canonical output size alone is insufficient.
    if len(JSONResponse(content=body).body) > MAX_OUTPUT_BYTES:
        _fail("READ_OUTPUT_LIMIT_EXCEEDED")
    final = project.resolve_context(tenant, project_id, input_version_id,
                                    drawing_id=drawing_id, write=False)
    final_graph = graph_adapter.resolve_project_graph_context(final)
    if ((final_graph.organization_id, final_graph.project_id, final_graph.drawing_id,
         final_graph.parent_version_id) != (context.organization_id, context.project_id,
                                           context.drawing_id, input_version_id)
            or final.intake_sha256 != context.intake_sha256
            or final_graph.graph_sha256 != context.graph_sha256):
        _fail("SIP_R6_ARTIFACT_SOURCE_MISMATCH")
    if current and not final.binding.is_head:
        _fail("SIP_R6_STALE_CURRENT")
    result.update(head_version_id=str(final.binding.head_version_id), is_head=final.binding.is_head)
    body = with_envelope_fields({"result": result})
    if len(JSONResponse(content=body).body) > MAX_OUTPUT_BYTES:
        _fail("READ_OUTPUT_LIMIT_EXCEEDED")
    sink.commit()
    return body
