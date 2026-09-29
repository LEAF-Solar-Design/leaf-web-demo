"""Pinned, tenant-bound proposal validation shared by submission and acceptance."""
import json

from leaf_cloud_client import validate_params, proposal_provenance
from solar_design_graph import GraphValidationError, _bounded_json
from solar_graph_context import resolve_graph_context
from solar_solve_results import _check_binding, complete_search


RESULT_SCHEMA = "leaf.solar-bound-proposal.v1"


def canonical(value):
    _bounded_json(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False)


def validate_scope(scope):
    _bounded_json(scope)
    if (type(scope) is not dict
            or set(scope) != {"drawing_id", "source_version", "binding"}
            or type(scope["drawing_id"]) is not str
            or not 1 <= len(scope["drawing_id"]) <= 256
            or type(scope["source_version"]) is not int or scope["source_version"] < 1
            or type(scope["binding"]) is not dict):
        raise GraphValidationError("INVALID_SOLVE_BINDING")
    binding = scope["binding"]
    if (set(binding) != {"source_rev", "source_hash", "graph_sha256", "frame_ref",
                         "request", "tenant_id", "job_id", "phase"}
            or type(binding["source_rev"]) is not int or binding["source_rev"] < 0
            or type(binding["request"]) is not dict
            or any(type(binding[k]) is not str or not 1 <= len(binding[k]) <= 256
                   for k in ("frame_ref", "tenant_id", "job_id"))
            or binding["phase"] not in ("initial", "background")
            or any(type(binding[k]) is not str or len(binding[k]) != 64
                   or any(c not in "0123456789abcdef" for c in binding[k])
                   for k in ("source_hash", "graph_sha256"))):
        raise GraphValidationError("INVALID_SOLVE_BINDING")
    return scope


def resolve_binding(backend, tenant_id, job_id, scope, params):
    scope = validate_scope(scope)
    binding = scope["binding"]
    parsed = validate_params(params).request.model_dump()
    if (binding["tenant_id"] != tenant_id or binding["job_id"] != job_id
            or canonical(binding["request"]) != canonical(parsed)):
        raise GraphValidationError("INVALID_SOLVE_BINDING")
    graph = resolve_graph_context(
        backend, tenant_id, scope["drawing_id"], scope["source_version"])["graph"]
    _check_binding(graph, binding)
    return graph


def verify_candidate(backend, tenant_id, job_id, scope, params, receipt, candidate):
    graph = resolve_binding(backend, tenant_id, job_id, scope, params)
    expected = complete_search(graph, scope["binding"], receipt)
    if canonical(expected) != canonical(candidate):
        raise GraphValidationError("INVALID_SOLVE_CANDIDATE")
    return expected


def bound_provenance(result, params, tenant_id, job_id, scope):
    """Recompute terminal proof from the app's own immutable drawing source."""
    import write_loop

    validate_scope(scope)
    _bounded_json(result)
    expected_scope = {k: scope[k] for k in ("drawing_id", "source_version")}
    if (type(result) is not dict
            or set(result) != {"schema", "proposal", "candidate", "scope"}
            or result["schema"] != RESULT_SCHEMA
            or canonical(result["scope"]) != canonical(expected_scope)):
        raise GraphValidationError("INVALID_SOLVE_CANDIDATE")
    proof = proposal_provenance(result["proposal"], params, tenant_id, job_id)
    backend = write_loop.backend_for_tenant(tenant_id, aps_live=False, da=None)
    verify_candidate(backend, tenant_id, job_id, scope, params,
                     result["proposal"], result["candidate"])
    return proof
