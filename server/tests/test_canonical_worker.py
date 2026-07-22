import uuid
from pathlib import Path
from threading import Event

import canonical_worker
from routers import jobs as jobs_router


REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeCanonicalJobs:
    def __init__(self, job=None, *, heartbeat_result=True):
        self.job = job
        self.heartbeat_result = heartbeat_result
        self.completed = []
        self.failed = []
        self.heartbeats = []
        self.job_heartbeats = []
        self.lease_renewed = Event()

    def record_worker_heartbeat(self, *args):
        self.heartbeats.append(args)

    def claim_next(self, owner, *, lease_seconds=30):
        job, self.job = self.job, None
        return job

    def heartbeat(self, job_id, owner, *, lease_seconds=30):
        self.job_heartbeats.append((job_id, owner, lease_seconds))
        self.lease_renewed.set()
        return self.heartbeat_result

    def complete_solve(self, job_id, owner, result, provenance):
        self.completed.append((job_id, owner, result, provenance))
        return "applied"

    def fail_or_retry(self, job_id, owner, error, provenance):
        self.failed.append((job_id, owner, error, provenance))
        return "failed"


def test_worker_runs_registered_adapter_and_completes(monkeypatch):
    store = FakeCanonicalJobs({"job_id": "job-1", "attempt": 2,
                               "tool_name": "string-autofill-opt", "params": {}})
    monkeypatch.setattr(canonical_worker.platform_link, "_canonical_jobs_module", lambda: store)
    monkeypatch.setattr(canonical_worker.autofill, "descriptor", lambda: {
        "tool_name": "string-autofill-opt", "runtime": "python-test",
        "source_revision": "abc123", "source_sha256": "c" * 64})
    monkeypatch.setitem(canonical_worker.ADAPTERS, "string-autofill-opt", lambda _params: {
        "solver_revision": "abc123", "source_sha256": "c" * 64,
        "runtime": "python-test", "solver_result": {"ok": True},
        "solver_input": {}, "request_sha256": "a" * 64,
        "input_sha256": "a" * 64, "result_sha256": "b" * 64})
    assert canonical_worker.run_once("worker-1") is True
    assert not store.failed
    assert store.heartbeats
    assert store.completed[0][3] == {"attempt": 2, "execution_path": "local",
                                     "solver_revision": "abc123", "source_sha256": "c" * 64,
                                     "runtime": "python-test"}


def test_worker_fails_unknown_adapter_without_false_success(monkeypatch):
    store = FakeCanonicalJobs({"job_id": "job-2", "attempt": 1,
                               "tool_name": "not-real", "params": {}})
    monkeypatch.setattr(canonical_worker.platform_link, "_canonical_jobs_module", lambda: store)
    monkeypatch.setattr(canonical_worker.autofill, "descriptor", lambda: {
        "tool_name": "string-autofill-opt", "runtime": "python-test",
        "source_revision": "abc123", "source_sha256": "c" * 64})
    assert canonical_worker.run_once("worker-2") is True
    assert not store.completed
    assert store.failed[0][2]["error_code"] == "SOLVER_FAILED"


def test_worker_idle_claim_is_false(monkeypatch):
    store = FakeCanonicalJobs()
    monkeypatch.setattr(canonical_worker.platform_link, "_canonical_jobs_module", lambda: store)
    monkeypatch.setattr(canonical_worker.autofill, "descriptor", lambda: {
        "tool_name": "string-autofill-opt", "runtime": "python-test",
        "source_revision": "abc123", "source_sha256": "c" * 64})
    assert canonical_worker.run_once("worker-idle") is False


def test_worker_renews_lease_during_synchronous_solver(monkeypatch):
    store = FakeCanonicalJobs({"job_id": "job-long", "attempt": 1,
                               "tool_name": "string-autofill-opt", "params": {}})
    monkeypatch.setattr(canonical_worker.platform_link, "_canonical_jobs_module", lambda: store)
    monkeypatch.setattr(canonical_worker.autofill, "descriptor", lambda: {
        "tool_name": "string-autofill-opt", "runtime": "python-test",
        "source_revision": "abc123", "source_sha256": "c" * 64})

    def slow_adapter(_params):
        assert store.lease_renewed.wait(1.0)
        return {"solver_revision": "abc123", "source_sha256": "c" * 64,
                "runtime": "python-test", "solver_result": {"ok": True},
                "solver_input": {}, "request_sha256": "a" * 64,
                "input_sha256": "a" * 64, "result_sha256": "b" * 64}

    monkeypatch.setitem(canonical_worker.ADAPTERS, "string-autofill-opt", slow_adapter)
    assert canonical_worker.run_once("worker-long", lease_seconds=0.15) is True
    assert store.job_heartbeats == [("job-long", "worker-long", 0.15)]
    assert len(store.completed) == 1


def test_worker_does_not_commit_after_lease_renewal_is_rejected(monkeypatch):
    store = FakeCanonicalJobs({"job_id": "job-lost", "attempt": 1,
                               "tool_name": "string-autofill-opt", "params": {}},
                              heartbeat_result=False)
    monkeypatch.setattr(canonical_worker.platform_link, "_canonical_jobs_module", lambda: store)
    monkeypatch.setattr(canonical_worker.autofill, "descriptor", lambda: {
        "tool_name": "string-autofill-opt", "runtime": "python-test",
        "source_revision": "abc123", "source_sha256": "c" * 64})

    def lease_losing_adapter(_params):
        assert store.lease_renewed.wait(1.0)
        return {"solver_revision": "abc123", "source_sha256": "c" * 64,
                "runtime": "python-test", "solver_result": {"ok": True},
                "solver_input": {}, "request_sha256": "a" * 64,
                "input_sha256": "a" * 64, "result_sha256": "b" * 64}

    monkeypatch.setitem(canonical_worker.ADAPTERS, "string-autofill-opt", lease_losing_adapter)
    assert canonical_worker.run_once("worker-lost", lease_seconds=0.15) is True
    assert not store.completed
    assert not store.failed


def test_run_route_submits_canonical_tool_without_sqlite_mirror(monkeypatch):
    tool = {"name": "string-autofill-opt", "canonical_only": True,
            "capabilities": ["drawing.read"], "default_params": {}}
    context = {"org_id": "org-1", "project_id": "project-1",
               "authority_mode": "postgres_canonical"}
    submitted = []
    monkeypatch.setattr(jobs_router.deps, "find_tool", lambda *_args: tool)
    monkeypatch.setattr(jobs_router.jobs.platform_link, "resolve_submission_context",
                        lambda *_args: context)
    monkeypatch.setattr(jobs_router.jobs.platform_link, "submit_canonical_solve",
                        lambda *args: submitted.append(args) or "canonical-job-1")
    monkeypatch.setattr(jobs_router.jobs, "submit_job",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            AssertionError("canonical route touched SQLite")))
    response = jobs_router.run(
        jobs_router.RunRequest(tool="string-autofill-opt", params={"groups": [{}],
                                                                      "panelsPerString": 10},
                               dwg=str(uuid.uuid4())),
        tenant_id="tenant-1", x_org_id="org-1", x_project_id="project-1",
        idempotency_key="request-1", authorization="Bearer verified")
    assert response.status_code == 202
    assert b'"job_id":"canonical-job-1"' in response.body
    assert submitted[0][0] == context
    assert submitted[0][-2] == "request-1"


def test_canonical_tool_requires_project_context(monkeypatch):
    tool = {"name": "string-autofill-opt", "canonical_only": True,
            "capabilities": ["drawing.read"], "default_params": {}}
    monkeypatch.setattr(jobs_router.deps, "find_tool", lambda *_args: tool)
    monkeypatch.setattr(jobs_router.jobs.platform_link, "resolve_submission_context",
                        lambda *_args: None)
    response = jobs_router.run(
        jobs_router.RunRequest(tool="string-autofill-opt", params={}),
        tenant_id="tenant-1", x_org_id=None, x_project_id=None,
        idempotency_key=None, authorization=None)
    assert response.status_code == 400
    assert b"X-Org-Id and X-Project-Id are required" in response.body


def test_canonical_identity_rejects_confused_deputy_claims(monkeypatch):
    context = {"org_id": uuid.uuid4(), "project_id": uuid.uuid4(),
               "authority_mode": "postgres_canonical"}
    canonical = str(context["org_id"])
    monkeypatch.setattr(jobs_router.deps, "auth_live", lambda: True)
    assert jobs_router._canonical_tenant_id(
        jobs_router.deps.TenantContext(canonical, org_id=canonical), context) == canonical
    for tenant in (
        jobs_router.deps.TenantContext("foreign-tenant", org_id=canonical),
        jobs_router.deps.TenantContext(canonical, org_id=str(uuid.uuid4())),
        canonical,
    ):
        try:
            jobs_router._canonical_tenant_id(tenant, context)
        except ValueError as exc:
            assert "must match" in str(exc)
        else:
            raise AssertionError("mismatched platform identity was accepted")


def test_capability_projection_requires_live_worker_and_reports_entitlement(monkeypatch):
    context = {"org_id": "org-1", "project_id": "project-1",
               "authority_mode": "postgres_canonical"}
    monkeypatch.setattr(jobs_router.jobs.platform_link, "resolve_submission_context",
                        lambda *_args: context)
    monkeypatch.setattr(jobs_router.jobs.platform_link, "canonical_worker_health",
                        lambda _tool: {"runtime": "python-test", "source_revision": "abc123",
                                       "source_sha256": "c" * 64,
                                       "observed_at": "2099-01-01T00:00:00+00:00"})
    available = jobs_router.platform_capabilities(
        tenant="demo", x_org_id="org-1", x_project_id="project-1", authorization=None)
    capability = available["capabilities"][0]
    assert capability["availability"]["state"] == "connected_degraded"
    assert capability["availability"]["reasonCode"] == "staging_gate_unverified"
    assert capability["availability"]["runtimeState"] == "degraded"
    assert capability["availability"]["fallback"] == {
        "mode": "read_only", "provenanceRequired": True}
    assert capability["availability"]["evidence"]
    assert capability["entitled"] is True

    restricted = jobs_router.platform_capabilities(
        tenant=jobs_router.deps.TenantContext("tenant", tier="restricted"),
        x_org_id="org-1", x_project_id="project-1", authorization=None)
    assert restricted["capabilities"][0]["entitled"] is False

    monkeypatch.setattr(jobs_router.jobs.platform_link, "canonical_worker_health",
                        lambda _tool: None)
    offline = jobs_router.platform_capabilities(
        tenant="demo", x_org_id="org-1", x_project_id="project-1", authorization=None)
    assert offline["capabilities"][0]["availability"]["state"] == "failed_retryable"
    assert offline["capabilities"][0]["availability"]["evidence"] == []


def test_canonical_worker_container_contract_is_non_root_and_source_bound():
    dockerfile = (REPO_ROOT / "deploy" / "Dockerfile.canonical-worker").read_text()
    overlay = (REPO_ROOT / "docker-compose.canonical.yml").read_text()
    smoke = (REPO_ROOT / "scripts" / "canonical-container-smoke.py").read_text()

    assert "COPY --from=autofill_solver" in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert "canonical_worker_health('string-autofill-opt')" in dockerfile
    assert 'CMD ["python", "canonical_worker.py"' in dockerfile
    assert '"--lease-seconds", "30"' in dockerfile

    assert "additional_contexts:" in overlay
    assert "autofill_solver: ../autofill-solver" in overlay
    assert "condition: service_completed_successfully" in overlay
    assert "db.apply_migration()" in overlay
    postgres_block = overlay.split("  postgres:", 1)[1].split("  migrate:", 1)[0]
    assert "ports:" not in postgres_block

    assert "idempotent resubmission created another job" in smoke
    assert 'expected = {"jobs": 1, "solves": 1, "history": 1, "outbox": 2, "solvePins": 3}' in smoke


def test_run_route_returns_stored_entitlement_denial_verbatim(monkeypatch):
    """P1 floor: a stored-org denial raised at the canonical choke point comes
    back as the documented envelope, never rewrapped as BAD_PARAMS."""
    from fastapi.responses import JSONResponse

    tool = {"name": "string-autofill-opt", "canonical_only": True,
            "capabilities": ["drawing.read"], "default_params": {}}
    context = {"org_id": "org-1", "project_id": "project-1",
               "authority_mode": "postgres_canonical"}
    denial = JSONResponse(status_code=403, content={
        "entitlement_required": True, "required": "run_write", "tier": "restricted",
        "error": {"error_code": "ENTITLEMENT_REQUIRED",
                  "message": "denied", "retryable": False},
        "degraded_mode": False})

    def _deny(*_args, **_kwargs):
        raise jobs_router.jobs.platform_link.CanonicalEntitlementDenied(denial)

    monkeypatch.setattr(jobs_router.deps, "find_tool", lambda *_args: tool)
    monkeypatch.setattr(jobs_router.jobs.platform_link, "resolve_submission_context",
                        lambda *_args: context)
    monkeypatch.setattr(jobs_router.jobs.platform_link, "submit_canonical_solve", _deny)
    monkeypatch.setattr(jobs_router.jobs, "submit_job",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(
                            AssertionError("denied canonical run touched SQLite")))
    response = jobs_router.run(
        jobs_router.RunRequest(tool="string-autofill-opt", params={}, dwg=str(uuid.uuid4())),
        tenant_id="tenant-1", x_org_id="org-1", x_project_id="project-1",
        idempotency_key="request-denied-1", authorization="Bearer verified")
    assert response is denial


def test_run_route_invalid_policy_is_structured_503(monkeypatch, tmp_path):
    """A present-but-invalid entitlements file must refuse /api/run with the
    documented 503 envelope, never an unstructured 500."""
    import json

    bad = tmp_path / "bad-entitlements.json"
    bad.write_text("{this is not json", encoding="utf-8")
    monkeypatch.setenv("LEAF_ENTITLEMENTS_FILE", str(bad))
    tool = {"name": "demo-read-tool", "capabilities": ["drawing.read"], "default_params": {}}
    monkeypatch.setattr(jobs_router.deps, "find_tool", lambda *_args: tool)
    response = jobs_router.run(
        jobs_router.RunRequest(tool="demo-read-tool", params={}),
        tenant_id="tenant-1", x_org_id=None, x_project_id=None,
        idempotency_key=None, authorization=None)
    assert response.status_code == 503
    body = json.loads(response.body)
    assert body["entitlement_required"] is True
    assert body["required"] == "run_read"
    assert body["tier"] == "demo"
    assert body["degraded_mode"] is False
    assert body["error"]["error_code"] == "INTERNAL"
    assert body["error"]["retryable"] is True
