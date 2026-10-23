"""Internal durable Solar graph orchestration; no HTTP or catalog registration."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from uuid import UUID

import platform_link
from demo_actor import is_demo_context
from demo_policy import demo_run_verdict
import solar_project_context as project
import solar_project_graph as graph
import solar_project_sizing as sizing
import solar_project_solve as solve
import solar_sizing_client as cloud
import solar_tools
import tool_validate
from solar_design_graph import GraphValidationError
from solar_graph_seed import validate_seed_request


def _jobs():
    return platform_link._canonical_jobs_module()


def _submission_fingerprint(org, proj, drawing, parent, tenant, actor, tool, params, manifest,
                            solve_context=None, proposal_job_id=None):
    return _jobs()._fingerprint("canonical-project-graph-submission", {
        "schema": _jobs().PROJECT_GRAPH_JOB_SCHEMA,
        "organization_id": str(org), "project_id": str(proj), "drawing_id": str(drawing),
        "input_version_id": str(parent), "request_tenant_id": str(tenant),
        "actor_binding_id": str(actor), "tool_name": tool, "parameters": params,
        "tool_manifest_sha256": manifest, "initialized": "initialize" in params,
        "max_attempts": 3,
        **({"solve_context": solve_context} if tool == solve.TOOL_NAME else {}),
        **({"proposal_job_id": proposal_job_id} if tool == solve.COMMIT_TOOL else {}),
    })


def _validate_new_project_graph_params(context, tool_name, params, *, checkout,
                                       tool_manifest_sha256):
    """Prepare a new submission once, publishing nothing, and return the parameters it bound.

    The tool schemas leave nested request nodes open, so the worker's own preparation is the
    only complete request contract: what it would refuse is refused here, before the insert,
    with its existing code. The probe job identity never leaves this function.
    """
    args = {"checkout": checkout, "job_id": UUID(int=0), "attempt": 1,
            "tool_manifest_sha256": tool_manifest_sha256}
    if graph._global_sizing(tool_name, params):
        plan = sizing.plan_sizing_request(params, context.graph, context.intake_sha256,
                                         tool_manifest_sha256)
        cloud.resolve_grant(plan["normalized_params"]["grant_ref"], str(context.organization_id))
        return plan["normalized_params"]
    if "initialize" in params:
        prepared = graph.prepare_project_graph_seed(context, params, **args)
    else:
        prepared = graph.prepare_project_graph_commit(context, tool_name, params, **args)
    return prepared.request["parameters"]


def submit_project_graph_job(
    tenant, project_id, drawing_id, input_version_id, tool_name, params, *,
    tool_manifest_sha256, checkout_capability, idempotency_key, preview=False, solve_context=None,
    proposal_job_id=None,
):
    if is_demo_context(tenant):
        reason = demo_run_verdict(tenant, tool_name, params)
        if reason:
            raise project.ProjectContextError(reason)
    org, actor = project._access(tenant, project_id, write=True)
    if not isinstance(drawing_id, UUID) or not isinstance(input_version_id, UUID):
        project.refuse("INVALID_BINDING")
    if not isinstance(actor, UUID):
        project.refuse("INVALID_BINDING")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise project.ProjectContextError("SIP_R4_IDEMPOTENCY_KEY_REQUIRED")
    jobs = _jobs()
    if tool_name == solve.TOOL_NAME:
        solve.refuse_split(params, solve_context)
    existing = platform_link.platform_db().run_transaction(lambda conn:
        jobs.get_project_graph_job_by_key(org, project_id, idempotency_key, conn=conn))
    if existing is not None:
        if existing.get("deleted_at") is not None:
            project.refuse("CONTEXT_NOT_FOUND")
        normalized = graph._parameters(SimpleNamespace(drawing_id=drawing_id), params)
        if tool_name == solve.TOOL_NAME:
            try:
                normalized = solve.cloud.validate_params(normalized).model_dump()
            except solve.cloud.CloudError:
                pass
        if graph._global_sizing(tool_name, normalized):
            requests = normalized.get("requests")
            if type(requests) is dict and len(requests) == 1:
                target = next(iter(requests))
                try:
                    parsed = cloud.SizingParams.model_validate({
                        "grant_ref": normalized.get("grant_ref"), "request": requests[target]})
                except (ValueError, TypeError):
                    pass  # Invalid intent cannot match the admitted fingerprint.
                else:
                    # Match pure planning's representation without grant, parent or checkout IO.
                    normalized = {**normalized, "grant_ref": parsed.grant_ref,
                        "requests": {target: parsed.request.wire()},
                        "cancel": normalized.get("cancel", False)}
        fingerprint = _submission_fingerprint(org, project_id, drawing_id, input_version_id,
            tenant, actor, tool_name, normalized, tool_manifest_sha256, solve_context, proposal_job_id)
        return jobs._graph_replay(existing, fingerprint)
    admission = project.verify_at_admission(tenant, project_id, drawing_id, input_version_id,
                                           checkout_capability=checkout_capability)
    graph._tool(tool_name, tool_manifest_sha256)
    context = graph.resolve_project_graph_context(admission.context)
    normalized = graph._parameters(context, params)
    initialized = "initialize" in normalized
    if initialized:
        if tool_name != "solar-settings":
            raise GraphValidationError("INVALID_SEED_REQUEST")
        validate_seed_request(normalized["initialize"])
    # The job row stores these parameters verbatim. The tool's schema closes the request root
    # (a caller storage path, authorization material); preparation closes what it leaves open.
    if tool_validate.validate_params(solar_tools.trusted_record(tool_name), normalized):
        raise GraphValidationError("INVALID_SETTINGS_REQUEST")
    if tool_name == solve.TOOL_NAME:
        plan = solve.plan_request(context, normalized, solve_context)
        normalized = plan["normalized_params"]
        solve.cloud.resolve_grant(normalized["grant_ref"], str(org))
    elif tool_name == solve.COMMIT_TOOL:
        def prepare_commit(conn):
            frozen = solve.freeze_proposal(context, proposal_job_id, conn=conn)
            prepared = graph.prepare_project_graph_commit(context, tool_name, normalized,
                checkout=admission.checkout, job_id=UUID(int=0), attempt=1,
                tool_manifest_sha256=tool_manifest_sha256, trusted_proposal=frozen, conn=conn)
            return frozen, prepared.request["parameters"]
        trusted_proposal, normalized = platform_link.platform_db().run_transaction(prepare_commit)
    else:
        normalized = _validate_new_project_graph_params(context, tool_name, normalized,
            checkout=admission.checkout, tool_manifest_sha256=tool_manifest_sha256)
    from leaf_platform import entitlements
    denial, stored_org = entitlements.stored_job_entitlement_verdict(org, "run")
    if is_demo_context(tenant) and stored_org is not None:
        if (stored_org.status != "active" or stored_org.tier != "demo_live"
                or stored_org.account_class != "demo"):
            raise project.ProjectContextError("demo_tool_not_allowed")
        reason = demo_run_verdict(tenant, tool_name, normalized)
        if reason:
            raise project.ProjectContextError(reason)
        denial = None
    if denial is not None:
        raise entitlements.EntitlementDenied(denial)
    execution_context = {
        "schema": jobs.PROJECT_GRAPH_JOB_SCHEMA, "authority_mode": "postgres_canonical",
        "execution_path": "local", "drawing_id": str(drawing_id),
        "actor_binding_id": str(actor), "checkout_fence": str(admission.checkout.fence),
        "tool_manifest_sha256": tool_manifest_sha256,
        "parent_intake_sha256": context.intake_sha256, "initialized": initialized,
        **({"solve_binding": plan["solve_binding"]} if tool_name == solve.TOOL_NAME else {}),
        **({"trusted_proposal": trusted_proposal} if tool_name == solve.COMMIT_TOOL else {}),
    }
    fingerprint = _submission_fingerprint(org, project_id, drawing_id, input_version_id,
        tenant, actor, tool_name, normalized, tool_manifest_sha256, solve_context, proposal_job_id)
    with project.write_loop.drawing_mutation_refusal_guard() as refusal:
        if refusal is not None:
            project.refuse("WRITES_DRAINED")
        if preview:
            return {"admissible": True, "replay": False}
        return platform_link.platform_db().run_transaction(lambda conn:
            jobs.submit_project_graph_job(org, project_id, str(tenant), tool_name, normalized,
                idempotency_key, input_version_id=input_version_id,
                execution_context=execution_context, submission_fingerprint=fingerprint,
                pinned_tier=stored_org.tier if stored_org is not None else None, conn=conn))


def _job_context(job, conn):
    jobs = _jobs()
    org, proj, parent, job_id, drawing, actor = jobs._graph_ids(job)
    if job["attempt"] < 1:
        jobs._graph_binding_error()
    jobs._graph_scope(job, conn)
    binding = project.graph_store().resolve_version_binding(org, proj, parent,
                                                           drawing_id=drawing, conn=conn)
    context = graph.resolve_project_graph_context(project._read_intake(binding))
    execution = job["execution_context"]
    if context.intake_sha256 != execution["parent_intake_sha256"]:
        jobs._graph_binding_error()
    return context


def prepare_project_graph_job(job, *, conn):
    jobs = _jobs()
    org, proj, parent, job_id, drawing, actor = jobs._graph_ids(job)
    context = _job_context(job, conn)
    execution = job["execution_context"]
    checkout = project.CheckoutLease(org, proj, drawing, "Stored job", actor, None, None,
                                     int(execution["checkout_fence"]))
    args = {"checkout": checkout, "job_id": job_id, "attempt": job["attempt"],
            "tool_manifest_sha256": execution["tool_manifest_sha256"], "conn": conn}
    if execution["initialized"]:
        prepared = graph.prepare_project_graph_seed(context, job["params"], **args)
    else:
        prepared = graph.prepare_project_graph_commit(context, job["tool_name"], job["params"], **args)
    jobs._validate_graph_request(job, prepared.request)
    return prepared


def complete_project_graph_job(job_id, worker_id, attempt, prepared):
    jobs = _jobs()
    if not isinstance(prepared, graph.PreparedProjectGraphCommit):
        jobs._graph_binding_error()
    completion = {"request": prepared.request, "output_intake_sha256": prepared.output_intake_sha256,
                  "receipt": json.loads(prepared.receipt_bytes)}

    def publish_and_prove(row, conn):
        jobs._validate_graph_request(row, prepared.request)
        receipt = graph.publish_project_graph_commit(prepared,
            actor_binding_id=UUID(row["execution_context"]["actor_binding_id"]), conn=conn)
        graph._pending_project_graph_job_provenance(job_id, receipt, expected=prepared, conn=conn)
        return receipt

    def complete(conn):
        if graph._global_sizing(prepared.request["tool"], prepared.request["parameters"]):
            row = graph._stored_job(job_id, conn)
            scope, _ = graph._exchange_authority(row)
            return graph._exchanges().complete_exchange(scope,
                graph._exchanges().JobAttempt(worker_id, attempt), completion,
                publish_and_prove=publish_and_prove, conn=conn)
        return jobs.complete_project_graph_job(job_id, worker_id, attempt, completion,
                                               publish_and_prove=publish_and_prove, conn=conn)

    try:
        with project.write_loop.drawing_mutation_refusal_guard() as refusal:
            if refusal is not None:
                project.refuse("WRITES_DRAINED")
            return platform_link.platform_db().run_transaction(complete)
    except jobs._GraphLeaseLost:
        return "not_owner", None


class GraphLeaseGuard:
    """Renew only worker custody, binding every heartbeat to the claimed attempt."""
    def __init__(self, jobs, job_id, owner, attempt, lease_seconds, *, on_renew=None):
        self._jobs, self._job_id, self._owner, self._attempt = jobs, job_id, owner, attempt
        self._on_renew = on_renew
        self._seconds = lease_seconds
        self._interval = max(min(lease_seconds / 3, 10), 0.05)
        self._stop, self._lost = threading.Event(), threading.Event()
        self._thread = threading.Thread(target=self._renew, name=f"graph-lease-{job_id}", daemon=True)

    @property
    def lost(self):
        return self._lost.is_set()

    def start(self):
        self._thread.start()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=self._interval + 1)
        if self._thread.is_alive():
            self._lost.set()

    def _renew(self):
        while not self._stop.wait(self._interval):
            try:
                if not self._jobs.heartbeat_project_graph_job(
                    self._job_id, self._owner, self._attempt, lease_seconds=self._seconds):
                    self._lost.set()
                    return
                if self._on_renew is not None:
                    self._on_renew()
            except Exception:
                self._lost.set()
                return


_TRANSPORT_FAILURES = (TimeoutError, ConnectionError)
_CAUSE_DEPTH = 8


def _transport_caused(exc):
    """True when a transport failure is in the bounded cause chain of a refusal.

    The stored-intake reader turns every blob read failure into SIP_R1_INTAKE_UNAVAILABLE; the
    failure it was handling stays on the refusal as its context, which is how a timeout or a
    dropped connection is told from a missing or unreadable object.
    """
    for _ in range(_CAUSE_DEPTH):
        exc = exc.__cause__ or exc.__context__
        if exc is None:
            return False
        if isinstance(exc, _TRANSPORT_FAILURES):
            return True
    return False


def _preparation_error(exc):
    reason = getattr(exc, "reason_code", getattr(exc, "code", None))
    if isinstance(exc, cloud.CloudError):
        allowed = {"cloud_request_invalid", "cloud_auth_missing", "cloud_tenant_unauthorized",
                   "cloud_upstream_failure", "cloud_response_invalid", "sizing_outcome_unknown",
                   "solve_outcome_unknown"}
        reason = (exc.classification if exc.classification in allowed
                  else "cloud_upstream_failure").upper()
    if isinstance(reason, str) and reason.startswith("SIP_R14_EXCHANGE_"):
        reason = "SIP_R14_EVIDENCE_REJECTED"
    retryable = isinstance(exc, _TRANSPORT_FAILURES) or reason in {
        "SIP_R1_STORE_UNAVAILABLE", "SIP_R1_CHECKOUT_UNAVAILABLE"} or (
        reason == "SIP_R1_INTAKE_UNAVAILABLE" and _transport_caused(exc))
    error = {"error_code": "GRAPH_JOB_FAILED",
             "message": "canonical Solar graph preparation failed", "retryable": retryable}
    if isinstance(reason, str):
        error["reason_code"] = reason
    return error


def _cloud_prepare(job, guard):
    """Only committed send ownership permits transport; uncertain writes await reclaim."""
    store = graph._exchanges()
    db = platform_link.platform_db()
    context = db.run_transaction(lambda conn: _job_context(job, conn))
    plan = sizing.plan_sizing_request(job["params"], context.graph, context.intake_sha256,
                                     job["execution_context"]["tool_manifest_sha256"])
    scope, lease = graph._exchange_authority(job)
    classification = cloud.SIZING_CLASSIFICATION
    if type(classification) is not str or classification not in ("live", "synthetic"):
        # A bootstrap fault, refused before anything is reserved or sent.
        raise GraphValidationError("SIP_R14_CLASSIFICATION_INVALID")
    reservation = {"request_bytes": plan["wire_bytes"], "request_sha256": plan["wire_sha256"],
                   "grant_ref": plan["normalized_params"]["grant_ref"], "classification": classification}
    exchange = store.get_exchange(scope, lease)
    if exchange is None:
        store.prepare_exchange(scope, lease, **reservation)
    elif any(exchange[key] != value for key, value in reservation.items()):
        raise store.ExchangeConflict()
    resume = store.resume_exchange(scope, lease)
    if resume.action == "awaiting_response":
        return None
    if resume.action == "outcome_unknown":
        raise cloud.CloudError("sizing_outcome_unknown", 502)
    if resume.action == "refused":
        raise cloud.CloudError(resume.exchange["refusal_code"], 502)
    if resume.action == "published":
        db.run_transaction(lambda conn: graph.project_graph_job_provenance(
            scope.job_id, job["result"]["graph_commit"],
            expected=prepare_project_graph_job(job, conn=conn), conn=conn))
        return None
    if resume.action == "send_ready":
        try:
            grant = cloud.resolve_grant(plan["normalized_params"]["grant_ref"], str(context.organization_id))
        except cloud.CloudError as exc:
            store.refuse_exchange(scope, lease, reason_code=exc.classification)
            raise
        if guard.lost or not store.begin_send(scope, lease):
            return None
        try:
            raw = cloud.post_string_length(cloud.SizingRequest.model_validate(plan["normalized_request"]),
                                           grant, preserve_uncertainty=True)
        except cloud.CloudError as exc:
            if guard.lost:
                return None
            if exc.classification == "sizing_outcome_unknown":
                store.record_outcome_unknown(scope, lease)
            else:
                store.refuse_exchange(scope, lease, reason_code=exc.classification)
            raise
        except Exception:
            # Send intent is durable. An unexpected transport failure is not a resend permit.
            return None
        if guard.lost:
            return None
        if type(raw) is not bytes or not raw or len(raw) > store.MAX_RESPONSE_BYTES:
            # A complete answer the store cannot hold (empty or oversized) is a known invalid
            # response, never an uncertain delivery.
            store.refuse_exchange(scope, lease, reason_code="cloud_response_invalid")
            raise cloud.CloudError("cloud_response_invalid", 502)
        try:
            store.capture_response(scope, lease, response_bytes=raw, response_sha256=graph._sha(raw))
        except Exception:
            return None
    if guard.lost:
        return None
    exchange = store.get_exchange(scope, lease)
    if exchange["evidence_bytes"] is None:
        try:
            expected = graph._sizing_expected(job, context, exchange)
            evidence = sizing.build_sizing_evidence(job["params"], context.graph,
                context.intake_sha256, expected["tool_manifest_sha256"], exchange["response_bytes"],
                identity=expected["identity"], parent_version_id=expected["parent_version_id"],
                producer=expected["producer"], classification=exchange["classification"])
        except GraphValidationError:
            store.refuse_exchange(scope, lease, reason_code="cloud_response_invalid")
            raise cloud.CloudError("cloud_response_invalid", 502) from None
        raw = graph.local.canonical_bytes(evidence)
        try:
            store.attach_evidence(scope, lease, evidence_bytes=raw, evidence_sha256=graph._sha(raw))
        except Exception:
            return None
    return db.run_transaction(lambda conn: prepare_project_graph_job(job, conn=conn))


def run_once(owner, *, tool_name, lease_seconds=30.0, on_renew=None):
    if tool_name not in graph.SUPPORTED_TOOLS:
        raise ValueError(f"no canonical solver adapter for {tool_name}")
    jobs = _jobs()
    job = jobs.claim_project_graph_job(owner, tool_name=tool_name, lease_seconds=lease_seconds)
    if job is None:
        return False
    job_id, attempt = UUID(str(job["job_id"])), job["attempt"]
    guard = GraphLeaseGuard(jobs, job_id, owner, attempt, lease_seconds,
                            **({"on_renew": on_renew} if on_renew is not None else {}))
    guard.start()
    try:
        if tool_name == solve.TOOL_NAME:
            prepared = solve.prepare_job(job, guard)
        elif graph._global_sizing(tool_name, job.get("params")):
            prepared = _cloud_prepare(job, guard)
        else:
            prepared = platform_link.platform_db().run_transaction(
                lambda conn: prepare_project_graph_job(job, conn=conn))
    except Exception as exc:
        guard.close()
        if not guard.lost:
            try:
                jobs.fail_project_graph_job(job_id, owner, attempt, _preparation_error(exc))
            except Exception:
                # Failure persistence also recovers through the durable lease.
                pass
        return True
    guard.close()
    if guard.lost or prepared is None:
        return True
    try:
        if tool_name == solve.TOOL_NAME:
            platform_link.platform_db().run_transaction(lambda conn:
                jobs.complete_project_solve_job(job_id, owner, attempt, prepared, conn=conn))
        else:
            complete_project_graph_job(job_id, owner, attempt, prepared)
    except Exception:
        # Uncertain persistence is recovered by readback/reclaim, never a false failure.
        pass
    return True
