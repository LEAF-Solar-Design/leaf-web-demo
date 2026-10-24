"""One custodied canonical proposal, with offline reconstruction and no publication."""
from __future__ import annotations

import hashlib
import json
from uuid import UUID

import deps
import leaf_cloud_client as cloud
import platform_link
import solar_project_context as project
import solar_project_graph as graph
import solar_solve_results as kernel
import solar_tools
from solar_design_graph import GraphValidationError

TOOL_NAME = cloud.TOOL_NAME
COMMIT_TOOL = "solar-commit-solve"
SOLVE_CLASSIFICATION = "live"
RESULT_SCHEMA = "leaf.solar-solve-proposal.v1"


def _store():
    from leaf_platform import project_solve_store
    return project_solve_store


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def refuse_split(params, solve_context):
    """Split intent has a specific refusal even when it is not valid catalog JSON."""
    pending = [params, solve_context]
    while pending:
        value = pending.pop()
        if type(value) is dict:
            if set(value) & {"proposals", "proofs", "split_state", "pieces", "row_indices"}:
                raise GraphValidationError("SOLVE_SPLIT_UNSUPPORTED")
            pending.extend(value.values())
        elif type(value) is list:
            pending.extend(value)


def plan_request(context, params, solve_context, *, job_id=UUID(int=0)):
    refuse_split(params, solve_context)
    # A graph that cannot be solved names its missing input before the request shape is judged.
    from product_capability_availability import w1_graph_readiness
    readiness = w1_graph_readiness(context.graph)[TOOL_NAME]
    if not readiness["input_ready"]:
        raise GraphValidationError(readiness["input_reason"].upper())
    parsed = cloud.validate_params(params)
    if (type(solve_context) is not dict
            or set(solve_context) != {"frame_ref", "expected_rev", "phase"}
            or type(solve_context["expected_rev"]) is not int):
        raise GraphValidationError("INVALID_SOLVE_BINDING")
    binding = kernel.bind_request(context.graph, parsed.request.model_dump(),
        expected_rev=solve_context["expected_rev"], frame_ref=solve_context["frame_ref"],
        phase=solve_context["phase"], tenant_id=str(context.organization_id), job_id=str(job_id))
    wire = cloud.canonical_bytes(parsed.request.wire_payload())
    return {"normalized_params": parsed.model_dump(), "wire_bytes": wire,
            "wire_sha256": _sha(wire), "binding": binding,
            "solve_binding": {"frame_id": binding["frame_ref"], "phase": binding["phase"],
                "expected_rev": binding["source_rev"], "graph_sha256": binding["graph_sha256"],
                "request": binding["request"]}}


def _authority(job):
    org, proj, _parent, job_id, drawing, _actor = platform_link._canonical_jobs_module()._graph_ids(job)
    store = _store()
    return store.ExchangeScope(org, proj, drawing, job_id), store.JobAttempt(
        job["lease_owner"], job["attempt"])


def _plan(job, context):
    graph._tool(TOOL_NAME, job["execution_context"]["tool_manifest_sha256"])
    bound = job["execution_context"]["solve_binding"]
    plan = plan_request(context, job["params"], {"frame_ref": bound["frame_id"],
        "expected_rev": bound["expected_rev"], "phase": bound["phase"]}, job_id=UUID(str(job["job_id"])))
    if bound != plan["solve_binding"]:
        platform_link._canonical_jobs_module()._graph_binding_error()
    return plan


def _reservation(job, context, plan, classification):
    return {"request_bytes": plan["wire_bytes"], "request_sha256": plan["wire_sha256"],
        "grant_ref": plan["normalized_params"]["grant_ref"], "classification": classification,
        "input_version_id": context.parent_version_id, "parent_intake_sha256": context.intake_sha256,
        "graph_revision": context.graph["rev"], "graph_sha256": kernel.digest(context.graph),
        "frame_id": plan["binding"]["frame_ref"], "phase": plan["binding"]["phase"]}


def _derive(job, context, plan, exchange):
    """Rebuild the kernel candidate from exact stored response bytes, without credentials."""
    reservation = _reservation(job, context, plan, exchange["classification"])
    if any(exchange[key] != value for key, value in reservation.items()):
        raise GraphValidationError("INVALID_SOLVE_BINDING")
    raw = exchange["response_bytes"]
    if (type(raw) is not bytes or not raw or len(raw) > _store().MAX_RESPONSE_BYTES
            or _sha(raw) != exchange["response_sha256"]
            or exchange["solver_endpoint"] != cloud.SOLVER_URL
            or exchange["solver_version"] != "1.0.0"
            or exchange["classification"] not in ("live", "synthetic")):
        raise GraphValidationError("INVALID_SOLVE_PROPOSAL")
    # Reject duplicate keys and nonfinite values before the response model can discard them.
    response_value = json.loads(raw, object_pairs_hook=project._object_pairs,
        parse_constant=project._nonfinite, parse_float=project._json_float)
    response = cloud.StringerResponse.model_validate(response_value)
    request = cloud.StringerRequest.model_validate(plan["binding"]["request"])
    proposal = {"schema_version": "leaf.solar-proposal.v1", "job_id": str(job["job_id"]),
        "tenant_id": str(context.organization_id), "drawing_changed": False,
        "request_sha256": exchange["request_sha256"], "response_sha256": exchange["response_sha256"],
        "solver": {"endpoint": cloud.SOLVER_URL, "adapter_version": "1.0.0"},
        "proposal": response.model_dump(), "visited_path": response.original_visited_path(request)}
    candidate = kernel.complete_search(context.graph, plan["binding"], proposal)
    scope = {"organization_id": str(context.organization_id), "project_id": str(context.project_id),
        "drawing_id": str(context.drawing_id), "input_version_id": str(context.parent_version_id),
        "parent_intake_sha256": context.intake_sha256, "job_id": str(job["job_id"]),
        "tool_manifest_sha256": job["execution_context"]["tool_manifest_sha256"],
        "graph_revision": context.graph["rev"], "graph_sha256": kernel.digest(context.graph),
        "classification": exchange["classification"]}
    envelope = {"schema": "leaf.canonical-solve-candidate.v1", "scope": scope, "candidate": candidate}
    proof = {"scope": scope, "proposal_proof": candidate["proof"],
             "candidate_sha256": _sha(cloud.canonical_bytes(envelope))}
    lengths = response.data.best_result.info.sequence_length
    if len(lengths) > 4096:
        raise GraphValidationError("INVALID_SOLVE_PROPOSAL")
    frame = next(frame for frame in context.graph["frames"] if frame["id"] == exchange["frame_id"])
    path, offset, summary = proposal["visited_path"], 0, []
    for count in lengths:
        cells = path[offset:offset + count]
        summary.append({"panel_count": count, "endpoints": [
            frame["matrix"][r][c]["panel_ref"] for r, c in (cells[0], cells[-1])]})
        offset += count
    return cloud.canonical_bytes(envelope), cloud.canonical_bytes(proof), summary


def _result(exchange, summary, provenance):
    return {"schema": RESULT_SCHEMA, "candidate_sha256": exchange["candidate_sha256"],
        "request_sha256": exchange["request_sha256"], "response_sha256": exchange["response_sha256"],
        "frame_id": exchange["frame_id"], "phase": exchange["phase"],
        "candidate_summary": summary, "execution_provenance": provenance}


def prepare_job(job, guard):
    from solar_project_jobs import _job_context
    store, db = _store(), platform_link.platform_db()
    context = db.run_transaction(lambda conn: _job_context(job, conn))
    plan, (scope, lease) = _plan(job, context), _authority(job)
    if SOLVE_CLASSIFICATION not in ("live", "synthetic"):
        raise GraphValidationError("INVALID_SOLVE_CLASSIFICATION")
    reservation = _reservation(job, context, plan, SOLVE_CLASSIFICATION)
    try:
        exchange = store.get_exchange(scope, lease)
    except store.ExchangeNotFound:
        exchange = store.prepare_exchange(scope, lease, **reservation)
    if any(exchange[key] != value for key, value in reservation.items()):
        raise store.ExchangeConflict()
    resume = store.resume_exchange(scope, lease)
    if resume.action == "outcome_unknown":
        raise cloud.CloudError("solve_outcome_unknown", 502)
    if resume.action == "refused":
        raise cloud.CloudError(resume.exchange["refusal_code"], 502)
    if resume.action == "send_ready":
        try:
            grant = cloud.resolve_grant(reservation["grant_ref"], str(context.organization_id))
        except cloud.CloudError as exc:
            store.refuse_exchange(scope, lease, reason_code=exc.classification)
            raise
        if guard.lost or not store.begin_send(scope, lease):
            return None
        try:
            raw = cloud.post_stringer(cloud.StringerRequest.model_validate(plan["binding"]["request"]),
                                      grant, preserve_uncertainty=True)
        except cloud.CloudError as exc:
            if guard.lost:
                return None
            if exc.classification == "solve_outcome_unknown":
                store.record_outcome_unknown(scope, lease)
            else:
                store.refuse_exchange(scope, lease, reason_code=exc.classification)
            raise
        except Exception:
            return None  # Durable send intent can only be settled by readback/reclaim.
        if guard.lost:
            return None
        if type(raw) is not bytes or not raw or len(raw) > store.MAX_RESPONSE_BYTES:
            store.refuse_exchange(scope, lease, reason_code="cloud_response_invalid")
            raise cloud.CloudError("cloud_response_invalid", 502)
        try:
            store.capture_response(scope, lease, response_bytes=raw, response_sha256=_sha(raw),
                                   solver_endpoint=cloud.SOLVER_URL, solver_version="1.0.0")
        except Exception:
            return None
    if guard.lost:
        return None
    exchange = store.get_exchange(scope, lease)
    try:
        candidate, proof, summary = _derive(job, context, plan, exchange)
    except (ValueError, KeyError, TypeError, GraphValidationError):
        if exchange["state"] != "completed":
            store.refuse_exchange(scope, lease, reason_code="cloud_response_invalid")
        raise cloud.CloudError("cloud_response_invalid", 502) from None
    if exchange["state"] != "completed":
        store.complete_exchange(scope, lease, candidate_bytes=candidate, candidate_sha256=_sha(candidate),
            proof_bytes=proof, proof_sha256=_sha(proof))
        exchange = store.get_exchange(scope, lease)
    elif (exchange["candidate_bytes"] != candidate or exchange["proof_bytes"] != proof
            or exchange["candidate_sha256"] != _sha(candidate) or exchange["proof_sha256"] != _sha(proof)):
        raise store.ExchangeConflict()
    provenance = {"schema": RESULT_SCHEMA, "attempt": job["attempt"], "worker_id": lease.worker_id,
        "execution_path": "leaf_cloud_service", "classification": exchange["classification"],
        "tool_manifest_sha256": job["execution_context"]["tool_manifest_sha256"],
        "proof_sha256": exchange["proof_sha256"]}
    return _result(exchange, summary, provenance)


def project_solve_job_provenance(job_id, result=None, *, conn):
    """Rederive custody offline; optionally also check the caller's result projection."""
    from solar_project_jobs import _job_context
    job = graph._stored_job(job_id, conn)
    jobs = platform_link._canonical_jobs_module()
    org, proj, _parent, stored_id, drawing, _actor = jobs._graph_ids(job)
    if job["tool_name"] != TOOL_NAME or stored_id != job_id:
        jobs._graph_binding_error()
    exchange = _store().read_completed_exchange(org, proj, drawing, job_id, conn=conn)
    context = _job_context(job, conn)
    candidate, proof, summary = _derive(job, context, _plan(job, context), exchange)
    if (candidate != exchange["candidate_bytes"] or proof != exchange["proof_bytes"]
            or _sha(candidate) != exchange["candidate_sha256"] or _sha(proof) != exchange["proof_sha256"]):
        jobs._graph_binding_error()
    provenance = result.get("execution_provenance", {}) if type(result) is dict else {}
    expected = {"schema": RESULT_SCHEMA, "attempt": job["attempt"],
        "worker_id": job["lease_owner"] if job["status"] == "running" else (job.get("provenance") or {}).get("worker_id"),
        "execution_path": "leaf_cloud_service", "classification": exchange["classification"],
        "tool_manifest_sha256": deps.catalog_tool_digest(solar_tools.trusted_record(TOOL_NAME)),
        "proof_sha256": exchange["proof_sha256"]}
    if result is not None and (provenance != expected or result != _result(exchange, summary, expected)):
        jobs._graph_binding_error()
    return {"candidate": json.loads(candidate), "proof": json.loads(proof)}


def _proposal_id(value):
    try:
        if type(value) is not str or str(UUID(value)) != value:
            raise ValueError()
        return UUID(value)
    except (ValueError, TypeError, AttributeError):
        raise GraphValidationError("SOLVE_PROPOSAL_REQUIRED") from None


def freeze_proposal(context, proposal_job_id, *, conn):
    """Resolve only canonical custody in the exact parent scope, before a job insert."""
    job_id = _proposal_id(proposal_job_id)
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM jobs WHERE job_id=%s AND deleted_at IS NULL", (job_id,))
        row = cur.fetchone()
    execution = row.get("execution_context") if row is not None else None
    if (row is None or row.get("deleted_at") is not None
            or str(row.get("org_id")) != str(context.organization_id)
            or str(row.get("project_id")) != str(context.project_id)
            or row.get("tool_name") != TOOL_NAME or row.get("kind") != "run"
            or type(execution) is not dict or execution.get("drawing_id") != str(context.drawing_id)):
        raise GraphValidationError("SOLVE_PROPOSAL_NOT_FOUND")
    if row.get("status") != "succeeded":
        raise GraphValidationError("SOLVE_PROPOSAL_NOT_SUCCEEDED")
    if str(row.get("input_version_id")) != str(context.parent_version_id):
        raise GraphValidationError("SOLVE_PROPOSAL_STALE")
    manifest = deps.catalog_tool_digest(solar_tools.trusted_record(TOOL_NAME))
    if execution.get("tool_manifest_sha256") != manifest:
        raise GraphValidationError("SOLVE_PROPOSAL_MANIFEST_MISMATCH")
    try:
        platform_link._canonical_jobs_module()._graph_ids(row)
        exchange = _store().read_completed_exchange(context.organization_id, context.project_id,
            context.drawing_id, job_id, conn=conn)
        if (exchange["input_version_id"] != context.parent_version_id
                or exchange["parent_intake_sha256"] != context.intake_sha256
                or exchange["graph_sha256"] != kernel.digest(context.graph)
                or exchange["graph_revision"] != context.graph["rev"]):
            raise GraphValidationError("SOLVE_PROPOSAL_EVIDENCE_INVALID")
    except (_store().SolveExchangeError, project.ProjectContextError, GraphValidationError,
            ValueError, TypeError, KeyError):
        raise GraphValidationError("SOLVE_PROPOSAL_EVIDENCE_INVALID") from None
    return {"proposal_job_id": str(job_id), "proposal_tool_manifest_sha256": manifest,
            **{key: exchange[key] for key in ("candidate_sha256", "request_sha256", "response_sha256")}}


def commit_candidate(context, trusted_proposal, *, conn):
    """Recheck the frozen identity and rederive the candidate without result or network IO."""
    frozen = freeze_proposal(context, trusted_proposal["proposal_job_id"], conn=conn)
    if frozen != trusted_proposal:
        raise GraphValidationError("SOLVE_PROPOSAL_EVIDENCE_INVALID")
    try:
        proof = project_solve_job_provenance(UUID(frozen["proposal_job_id"]), conn=conn)
        if _sha(cloud.canonical_bytes(proof["candidate"])) != frozen["candidate_sha256"]:
            raise GraphValidationError("SOLVE_PROPOSAL_EVIDENCE_INVALID")
        return proof["candidate"]["candidate"]
    except (_store().SolveExchangeError, project.ProjectContextError, GraphValidationError,
            ValueError, TypeError, KeyError, AttributeError):
        raise GraphValidationError("SOLVE_PROPOSAL_EVIDENCE_INVALID") from None
