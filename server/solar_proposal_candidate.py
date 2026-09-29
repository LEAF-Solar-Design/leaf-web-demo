"""Pinned, tenant-bound proposal validation shared by submission and acceptance."""
import json
import hashlib

from leaf_cloud_client import validate_params, proposal_provenance
from solar_design_graph import GraphValidationError, _bounded_json
from solar_graph_context import resolve_graph_context
from solar_solve_results import _check_binding, complete_search


RESULT_SCHEMA = "leaf.solar-bound-proposal.v1"


def requires_candidate(tool):
    import solar_tools
    return "proposal_candidate" in (solar_tools.get(tool.get("name")) or {}).get("trusted_inputs", [])


def validate_commit_request(proposal_job_id, params):
    if (type(params) is not dict
            or set(params) - {"drawing_id", "expected_rev", "cancel"}
            or type(params.get("cancel", False)) is not bool):
        raise GraphValidationError("INVALID_COMMIT_REQUEST")
    if proposal_job_id is None:
        raise GraphValidationError("PROPOSAL_JOB_REQUIRED")
    if (type(proposal_job_id) is not str or not 1 <= len(proposal_job_id) <= 256
            or not proposal_job_id.strip()):
        raise GraphValidationError("INVALID_COMMIT_REQUEST")


def validate_snapshot(snapshot):
    _bounded_json(snapshot)
    if (type(snapshot) is not dict
            or set(snapshot) != {"proposal_job_id", "params", "solve_scope", "candidate", "proposal_proof"}
            or type(snapshot["proposal_job_id"]) is not str
            or not 1 <= len(snapshot["proposal_job_id"]) <= 256
            or not snapshot["proposal_job_id"].strip()
            or any(type(snapshot[k]) is not dict for k in ("params", "candidate", "proposal_proof"))):
        raise GraphValidationError("INVALID_SOLVE_CANDIDATE")
    validate_scope(snapshot["solve_scope"])
    return json.loads(canonical(snapshot))


def proposal_identity(snapshot):
    snapshot = validate_snapshot(snapshot)
    return {"proposal_job_id": snapshot["proposal_job_id"],
            "candidate_sha256": hashlib.sha256(canonical(snapshot["candidate"]).encode("utf-8")).hexdigest(),
            "scope": snapshot["solve_scope"]}


def verify_snapshot(backend, tenant_id, drawing_id, source_version, snapshot):
    snapshot = validate_snapshot(snapshot)
    scope = snapshot["solve_scope"]
    if scope["drawing_id"] != drawing_id or scope["source_version"] != source_version:
        raise GraphValidationError("INVALID_SOLVE_BINDING")
    # Resolve identity/basis first so specific binding-kernel refusals survive.
    _resolve_commit_binding(backend, tenant_id, snapshot["proposal_job_id"], scope, snapshot["params"])
    candidate = snapshot["candidate"]
    try:
        receipt = candidate["proposal"]
        proof = proposal_provenance(receipt, snapshot["params"], tenant_id, snapshot["proposal_job_id"])
        if canonical(proof) != canonical(snapshot["proposal_proof"]):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise GraphValidationError("INVALID_SOLVE_PROPOSAL") from None
    verify_candidate(backend, tenant_id, snapshot["proposal_job_id"], scope,
                     snapshot["params"], receipt, candidate)
    return snapshot


def resolve_proposal(tenant_id, drawing_id, source_version, proposal_job_id, params):
    """Authenticate a completed app-owned job once, then detach its trusted input."""
    import jobs
    import solar_tools
    import write_loop

    validate_commit_request(proposal_job_id, params)
    record = jobs.get_job(proposal_job_id)
    if (not isinstance(record, dict) or record.get("tenant_id") != tenant_id
            or record.get("tool") != "solar-solve-proposal"
            or (solar_tools.get(record["tool"]) or {}).get("adapter") != "cloud-proposal"
            or record.get("status") != "complete"
            or type(record.get("result")) is not dict or record["result"].get("ok") is not True):
        raise GraphValidationError("INVALID_SOLVE_CANDIDATE")
    try:
        scope = jobs.solve_scope_context(proposal_job_id)
        validate_scope(scope)
        if (record.get("dwg") != scope["drawing_id"]
                or record.get("dwg_version") != scope["source_version"]
                or drawing_id != scope["drawing_id"]):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise GraphValidationError("INVALID_SOLVE_BINDING") from None
    payload = record["result"].get("result")
    if (type(payload) is not dict or set(payload) != {"schema", "proposal", "candidate", "scope"}
            or payload.get("schema") != RESULT_SCHEMA):
        raise GraphValidationError("INVALID_SOLVE_CANDIDATE")
    if canonical(payload["scope"]) != canonical({k: scope[k] for k in ("drawing_id", "source_version")}):
        raise GraphValidationError("INVALID_SOLVE_BINDING")
    backend = write_loop.backend_for_tenant(tenant_id, aps_live=False, da=None)
    _resolve_commit_binding(backend, tenant_id, proposal_job_id, scope, record.get("params"))
    try:
        proof = proposal_provenance(payload["proposal"], record["params"], tenant_id, proposal_job_id)
        if (type(record.get("provenance")) is not dict
                or any(record["provenance"].get(k) != v for k, v in proof.items())):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise GraphValidationError("INVALID_SOLVE_PROPOSAL") from None
    candidate = verify_candidate(backend, tenant_id, proposal_job_id, scope, record["params"],
                                 payload["proposal"], payload["candidate"])
    if scope["source_version"] != source_version:
        raise GraphValidationError("STALE_SOLVE_RESULT")
    return validate_snapshot({"proposal_job_id": proposal_job_id, "params": record["params"],
                              "solve_scope": scope, "candidate": candidate, "proposal_proof": proof})


def _resolve_commit_binding(backend, tenant_id, job_id, scope, params):
    try:
        return resolve_binding(backend, tenant_id, job_id, scope, params)
    except GraphValidationError:
        raise
    except (KeyError, TypeError, ValueError):
        raise GraphValidationError("INVALID_SOLVE_BINDING") from None


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
