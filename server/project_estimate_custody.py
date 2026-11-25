"""Source-bound estimate snapshots published by the canonical drawing editor."""
from copy import deepcopy
from hashlib import sha256
from uuid import UUID

from starlette.responses import Response
import entitlements
import project_drawing_editor as editor
import solar_estimate as material
import solar_project_admission as policy
import solar_project_context as project
import solar_project_graph as graphs
import solar_project_read as reads
from envelopes import with_envelope_fields
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError

KEY = "solar_estimate"
SNAPSHOT_SCHEMA = "leaf.project-estimate-snapshot.v1"
RESULT_SCHEMA = "leaf.project-estimate.v1"


def _fail(code):
    raise GraphValidationError(code)


def _enabled(project_id, drawing_id, version_id):
    if not policy.project_runs_enabled():
        _fail("project_execution_disabled")
    if not all(isinstance(value, UUID) for value in (project_id, drawing_id, version_id)):
        _fail("ESTIMATE_REQUEST_INVALID")


def _read_entitlement(tenant, organization_id):
    tier = entitlements.resolve_tier(tenant)
    roles, elevated = entitlements.resolve_roles(tenant)
    try:
        allowed = entitlements.entitlements_for(tier, roles, elevated).get("run_read", False)
    except entitlements.EntitlementsError:
        return entitlements.policy_unavailable_response("run_read", tier)
    if not allowed:
        return entitlements.entitlement_denied_response("run_read", tier)
    denial, _ = reads.stored.stored_job_entitlement_verdict(organization_id, "extract")
    return denial


def save_estimate(tenant, project_id, drawing_id, parent_version_id, rate_card, *,
                  catalog_digest, request_id, capability):
    _enabled(project_id, drawing_id, parent_version_id)
    project._access(tenant, project_id, write=True)
    if not isinstance(request_id, UUID):
        _fail("ESTIMATE_REQUEST_INVALID")
    result = reads.run_project_read(tenant, project_id, drawing_id, parent_version_id,
        "solar-takeoff", {}, catalog_digest=catalog_digest, current=False)
    if isinstance(result, Response):
        return result
    result = result["result"]
    parent = project.resolve_context(tenant, project_id, parent_version_id,
                                     drawing_id=drawing_id, write=True)
    graph = graphs.resolve_project_graph_context(parent)
    binding = {key: result[key] for key in
               ("organization_id", "project_id", "drawing_id", "input_version_id", "graph_sha256")}
    binding["intake_sha256"] = result["input_intake_sha256"]
    if (binding != {"organization_id": str(graph.organization_id),
            "project_id": str(project_id), "drawing_id": str(drawing_id),
            "input_version_id": str(parent_version_id), "intake_sha256": parent.intake_sha256,
            "graph_sha256": graph.graph_sha256}):
        _fail("ESTIMATE_SOURCE_MISMATCH")
    estimate = material.price_takeoff(result["output"]["summary"], rate_card,
                                     source_binding=binding)
    encoded = material.encode_estimate(estimate)
    raw, _ = editor.read_dxf(tenant, project_id, drawing_id, parent_version_id)
    intake = deepcopy(parent.intake)
    intake[KEY] = {"schema": SNAPSHOT_SCHEMA, "estimate": encoded.decode("utf-8"),
                   "source_dxf_sha256": sha256(raw).hexdigest(),
                   "source_version_id": str(parent_version_id)}
    receipt = editor.save_dxf(tenant, project_id, drawing_id, parent_version_id, raw,
        request_id=request_id, capability=capability, _restored_intake=canonical_bytes(intake))
    return with_envelope_fields({"result": {"schema": RESULT_SCHEMA, "saved": True,
        "publication": receipt, "content_sha256": estimate["content_sha256"], "estimate": estimate}})


def read_estimate(tenant, project_id, drawing_id, version_id):
    _enabled(project_id, drawing_id, version_id)
    verified = project.resolve_context(tenant, project_id, version_id,
                                       drawing_id=drawing_id, write=False)
    denial = _read_entitlement(tenant, verified.binding.organization_id)
    if denial is not None:
        return denial
    identity = {key: str(getattr(verified.binding, key)) for key in
                ("organization_id", "project_id", "drawing_id", "input_version_id")}
    if KEY not in verified.intake:
        return with_envelope_fields({"result": {"schema": RESULT_SCHEMA, "saved": False, **identity}})
    snapshot = verified.intake[KEY]
    if (type(snapshot) is not dict or set(snapshot) !=
            {"schema", "estimate", "source_dxf_sha256", "source_version_id"}
            or snapshot["schema"] != SNAPSHOT_SCHEMA or type(snapshot["estimate"]) is not str
            or not material._hex(snapshot["source_dxf_sha256"])):
        _fail("ESTIMATE_SNAPSHOT_INVALID")
    estimate = material.decode_estimate(snapshot["estimate"])
    if material.encode_estimate(estimate).decode("utf-8") != snapshot["estimate"]:
        _fail("ESTIMATE_SNAPSHOT_INVALID")
    binding = estimate["source_binding"]
    if (any(binding[key] != str(value) for key, value in (
            ("organization_id", verified.binding.organization_id), ("project_id", project_id),
            ("drawing_id", drawing_id)))
            or snapshot["source_version_id"] != binding["input_version_id"]):
        _fail("ESTIMATE_SOURCE_MISMATCH")
    source_id = UUID(binding["input_version_id"])
    source = project.resolve_context(tenant, project_id, source_id, drawing_id=drawing_id, write=False)
    source_graph = graphs.resolve_project_graph_context(source)
    if (source.intake_sha256 != binding["intake_sha256"]
            or source_graph.graph_sha256 != binding["graph_sha256"]):
        _fail("ESTIMATE_SOURCE_MISMATCH")
    original, _ = editor.read_dxf(tenant, project_id, drawing_id, source_id)
    if sha256(original).hexdigest() != snapshot["source_dxf_sha256"]:
        _fail("ESTIMATE_SOURCE_MISMATCH")
    selected_graph = graphs.resolve_project_graph_context(verified)
    selected, _ = editor.read_dxf(tenant, project_id, drawing_id, version_id)
    if (sha256(selected).hexdigest() != snapshot["source_dxf_sha256"]
            or selected_graph.graph_sha256 != binding["graph_sha256"]):
        _fail("ESTIMATE_SOURCE_CHANGED")
    return with_envelope_fields({"result": {"schema": RESULT_SCHEMA, "saved": True,
        **identity, "content_sha256": estimate["content_sha256"], "estimate": estimate}})
