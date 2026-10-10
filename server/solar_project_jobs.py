"""Internal durable Solar graph orchestration; no HTTP or catalog registration."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from uuid import UUID

import platform_link
import solar_project_context as project
import solar_project_graph as graph
import solar_tools
import tool_validate
from solar_design_graph import GraphValidationError
from solar_graph_seed import validate_seed_request


def _jobs():
    return platform_link._canonical_jobs_module()


def _submission_fingerprint(org, proj, drawing, parent, tenant, actor, tool, params, manifest):
    return _jobs()._fingerprint("canonical-project-graph-submission", {
        "schema": _jobs().PROJECT_GRAPH_JOB_SCHEMA,
        "organization_id": str(org), "project_id": str(proj), "drawing_id": str(drawing),
        "input_version_id": str(parent), "request_tenant_id": str(tenant),
        "actor_binding_id": str(actor), "tool_name": tool, "parameters": params,
        "tool_manifest_sha256": manifest, "initialized": "initialize" in params,
        "max_attempts": 3,
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
    if "initialize" in params:
        prepared = graph.prepare_project_graph_seed(context, params, **args)
    else:
        prepared = graph.prepare_project_graph_commit(context, tool_name, params, **args)
    return prepared.request["parameters"]


def submit_project_graph_job(
    tenant, project_id, drawing_id, input_version_id, tool_name, params, *,
    tool_manifest_sha256, checkout_capability, idempotency_key,
):
    org, actor = project._access(tenant, project_id, write=True)
    if not isinstance(drawing_id, UUID) or not isinstance(input_version_id, UUID):
        project.refuse("INVALID_BINDING")
    if not isinstance(actor, UUID):
        project.refuse("INVALID_BINDING")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise project.ProjectContextError("SIP_R4_IDEMPOTENCY_KEY_REQUIRED")
    jobs = _jobs()
    existing = platform_link.platform_db().run_transaction(lambda conn:
        jobs.get_project_graph_job_by_key(org, project_id, idempotency_key, conn=conn))
    if existing is not None:
        if existing.get("deleted_at") is not None:
            project.refuse("CONTEXT_NOT_FOUND")
        normalized = graph._parameters(SimpleNamespace(drawing_id=drawing_id), params)
        fingerprint = _submission_fingerprint(org, project_id, drawing_id, input_version_id,
            tenant, actor, tool_name, normalized, tool_manifest_sha256)
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
    normalized = _validate_new_project_graph_params(context, tool_name, normalized,
        checkout=admission.checkout, tool_manifest_sha256=tool_manifest_sha256)
    from leaf_platform import entitlements
    denial, stored_org = entitlements.stored_job_entitlement_verdict(org, "run")
    if denial is not None:
        raise entitlements.EntitlementDenied(denial)
    execution_context = {
        "schema": jobs.PROJECT_GRAPH_JOB_SCHEMA, "authority_mode": "postgres_canonical",
        "execution_path": "local", "drawing_id": str(drawing_id),
        "actor_binding_id": str(actor), "checkout_fence": str(admission.checkout.fence),
        "tool_manifest_sha256": tool_manifest_sha256,
        "parent_intake_sha256": context.intake_sha256, "initialized": initialized,
    }
    fingerprint = _submission_fingerprint(org, project_id, drawing_id, input_version_id,
        tenant, actor, tool_name, normalized, tool_manifest_sha256)
    with project.write_loop.drawing_mutation_refusal_guard() as refusal:
        if refusal is not None:
            project.refuse("WRITES_DRAINED")
        return platform_link.platform_db().run_transaction(lambda conn:
            jobs.submit_project_graph_job(org, project_id, str(tenant), tool_name, normalized,
                idempotency_key, input_version_id=input_version_id,
                execution_context=execution_context, submission_fingerprint=fingerprint,
                pinned_tier=stored_org.tier if stored_org is not None else None, conn=conn))


def prepare_project_graph_job(job, *, conn):
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
    checkout = project.CheckoutLease(org, proj, drawing, "Stored job", actor, None, None,
                                     int(execution["checkout_fence"]))
    args = {"checkout": checkout, "job_id": job_id, "attempt": job["attempt"],
            "tool_manifest_sha256": execution["tool_manifest_sha256"]}
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
        graph.project_graph_job_provenance(job_id, receipt, expected=prepared, conn=conn)
        return receipt

    try:
        with project.write_loop.drawing_mutation_refusal_guard() as refusal:
            if refusal is not None:
                project.refuse("WRITES_DRAINED")
            return platform_link.platform_db().run_transaction(lambda conn:
                jobs.complete_project_graph_job(job_id, worker_id, attempt, completion,
                                                publish_and_prove=publish_and_prove, conn=conn))
    except jobs._GraphLeaseLost:
        return "not_owner", None


class GraphLeaseGuard:
    """Renew only worker custody, binding every heartbeat to the claimed attempt."""
    def __init__(self, jobs, job_id, owner, attempt, lease_seconds):
        self._jobs, self._job_id, self._owner, self._attempt = jobs, job_id, owner, attempt
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
    retryable = isinstance(exc, _TRANSPORT_FAILURES) or reason in {
        "SIP_R1_STORE_UNAVAILABLE", "SIP_R1_CHECKOUT_UNAVAILABLE"} or (
        reason == "SIP_R1_INTAKE_UNAVAILABLE" and _transport_caused(exc))
    error = {"error_code": "GRAPH_JOB_FAILED",
             "message": "canonical Solar graph preparation failed", "retryable": retryable}
    if isinstance(reason, str):
        error["reason_code"] = reason
    return error


def run_once(owner, *, tool_name, lease_seconds=30.0):
    if tool_name not in graph.SUPPORTED_TOOLS:
        raise ValueError(f"no canonical solver adapter for {tool_name}")
    jobs = _jobs()
    job = jobs.claim_project_graph_job(owner, tool_name=tool_name, lease_seconds=lease_seconds)
    if job is None:
        return False
    job_id, attempt = UUID(str(job["job_id"])), job["attempt"]
    guard = GraphLeaseGuard(jobs, job_id, owner, attempt, lease_seconds)
    guard.start()
    try:
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
    if guard.lost:
        return True
    try:
        complete_project_graph_job(job_id, owner, attempt, prepared)
    except Exception:
        # Uncertain persistence is recovered by readback/reclaim, never a false failure.
        pass
    return True
