import uuid
import subprocess
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

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
        self.claim_requests = []
        self.lease_renewed = Event()

    def record_worker_heartbeat(self, *args):
        self.heartbeats.append(args)

    def claim_next(self, owner, *, lease_seconds=30, tool_name=None):
        self.claim_requests.append((owner, lease_seconds, tool_name))
        if self.job is not None and self.job.get("tool_name") != tool_name:
            return None
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
    assert store.claim_requests == [("worker-1", 30.0, "string-autofill-opt")]
    assert store.completed[0][3] == {"attempt": 2, "execution_path": "local",
                                     "solver_revision": "abc123", "source_sha256": "c" * 64,
                                     "runtime": "python-test"}


def test_worker_leaves_unknown_adapter_unclaimed(monkeypatch):
    store = FakeCanonicalJobs({"job_id": "job-2", "attempt": 1,
                               "tool_name": "not-real", "params": {}})
    monkeypatch.setattr(canonical_worker.platform_link, "_canonical_jobs_module", lambda: store)
    monkeypatch.setattr(canonical_worker.autofill, "descriptor", lambda: {
        "tool_name": "string-autofill-opt", "runtime": "python-test",
        "source_revision": "abc123", "source_sha256": "c" * 64})
    assert canonical_worker.run_once("worker-2") is False
    assert not store.completed
    assert not store.failed
    assert store.job["tool_name"] == "not-real"
    assert store.claim_requests == [("worker-2", 30.0, "string-autofill-opt")]


def test_worker_idle_claim_is_false(monkeypatch):
    store = FakeCanonicalJobs()
    monkeypatch.setattr(canonical_worker.platform_link, "_canonical_jobs_module", lambda: store)
    monkeypatch.setattr(canonical_worker.autofill, "descriptor", lambda: {
        "tool_name": "string-autofill-opt", "runtime": "python-test",
        "source_revision": "abc123", "source_sha256": "c" * 64})
    assert canonical_worker.run_once("worker-idle") is False


def test_worker_retries_bounded_transient_solver_timeout(monkeypatch):
    store = FakeCanonicalJobs({"job_id": "job-timeout", "attempt": 1,
                               "tool_name": "string-autofill-opt", "params": {}})
    monkeypatch.setattr(canonical_worker.platform_link, "_canonical_jobs_module", lambda: store)
    monkeypatch.setattr(canonical_worker.autofill, "descriptor", lambda: {
        "tool_name": "string-autofill-opt", "runtime": "python-test",
        "source_revision": "abc123", "source_sha256": "c" * 64})
    monkeypatch.setitem(
        canonical_worker.ADAPTERS, "string-autofill-opt",
        lambda _params: (_ for _ in ()).throw(subprocess.TimeoutExpired("solver", 60)))

    assert canonical_worker.run_once("worker-timeout") is True
    assert store.failed[0][2]["retryable"] is True


def test_worker_does_not_terminalize_completion_infrastructure_failure(monkeypatch):
    store = FakeCanonicalJobs({"job_id": "job-complete-db", "attempt": 1,
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
    monkeypatch.setattr(
        store, "complete_solve",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ConnectionError("database unavailable")))

    assert canonical_worker.run_once("worker-complete-db") is True
    assert store.failed == []


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
                                   dwg=str(uuid.uuid4()),
                                   catalog_digest=jobs_router.deps.catalog_tool_digest(tool)),
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
        jobs_router.RunRequest(
            tool="string-autofill-opt", params={},
            catalog_digest=jobs_router.deps.catalog_tool_digest(tool)),
        tenant_id="tenant-1", x_org_id=None, x_project_id=None,
        idempotency_key=None, authorization=None)
    assert response.status_code == 400
    assert b"X-Org-Id and X-Project-Id are required" in response.body


def test_canonical_identity_prefers_verified_subject_binding(monkeypatch):
    context = {"org_id": uuid.uuid4(), "project_id": uuid.uuid4(),
               "authority_mode": "postgres_canonical"}
    canonical = str(context["org_id"])
    monkeypatch.setattr(jobs_router.deps, "auth_live", lambda: True)
    binding = SimpleNamespace(platform_tenant_id=context["org_id"])
    store = SimpleNamespace(resolve_active_identity_binding=lambda authority, subject: (
        binding if (authority, subject) == ("auth0", "auth0|owner") else None))
    monkeypatch.setattr(jobs_router.jobs.platform_link, "platform_store", lambda: store)
    stale = jobs_router.deps.TenantContext(
        "stale-website-tenant", org_id="stale-website-org", subject="auth0|owner")
    assert jobs_router._canonical_tenant_id(stale, context) == canonical
    assert jobs_router._bound_tenant_id(stale) == canonical

    for tenant in (
        jobs_router.deps.TenantContext("foreign-tenant", org_id=canonical),
        jobs_router.deps.TenantContext(canonical, org_id=canonical, subject="auth0|foreign"),
        canonical,
    ):
        try:
            jobs_router._canonical_tenant_id(tenant, context)
        except ValueError as exc:
            assert "subject must match" in str(exc)
        else:
            raise AssertionError("mismatched platform identity was accepted")


def _fresh_heartbeat():
    """A heartbeat stamped NOW. The old fixture used `2099-01-01`, which made the
    lease look permanently fresh; the catalog's clock-skew bound refuses an
    observation more than one TTL into the future, so that stamp now (correctly)
    reports a REJECTED measurement rather than a live one. See the dedicated test
    below: that refusal is the point of this lane, not a casualty of it."""
    from datetime import datetime, timezone
    return {"runtime": "python-test", "source_revision": "abc123",
            "source_sha256": "c" * 64,
            "observed_at": datetime.now(timezone.utc).isoformat()}


def _by_id(response):
    return {c["id"]: c for c in response["capabilities"]}


def test_capability_projection_requires_live_worker_and_reports_entitlement(monkeypatch):
    context = {"org_id": "org-1", "project_id": "project-1",
               "authority_mode": "postgres_canonical"}
    monkeypatch.setattr(jobs_router.jobs.platform_link, "resolve_submission_context",
                        lambda *_args: context)
    monkeypatch.setattr(jobs_router.jobs.platform_link, "canonical_worker_health",
                        lambda _tool: _fresh_heartbeat())
    available = jobs_router.platform_capabilities(
        tenant="demo", x_org_id="org-1", x_project_id="project-1", authorization=None)
    # Selected BY ID, not by index: the route now emits all seven release-gate
    # capabilities in gate order, so `[0]` is `drawing.inspect`, not the solver.
    capability = _by_id(available)[jobs_router.SOLVE_CAPABILITY]
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
    assert _by_id(restricted)[jobs_router.SOLVE_CAPABILITY]["entitled"] is False

    monkeypatch.setattr(jobs_router.jobs.platform_link, "canonical_worker_health",
                        lambda _tool: None)
    offline = jobs_router.platform_capabilities(
        tenant="demo", x_org_id="org-1", x_project_id="project-1", authorization=None)
    offline_solve = _by_id(offline)[jobs_router.SOLVE_CAPABILITY]
    assert offline_solve["availability"]["state"] == "failed_retryable"
    assert offline_solve["availability"]["evidence"] == []


def test_the_route_emits_every_release_gate_capability_fail_closed(monkeypatch):
    """L1.5. This route hand-built ONE descriptor inline and emitted nothing for
    the other six release gates, so the console could not even name them. All
    seven now come from the ratified catalog, and the six with no measurement come
    back LOCKED rather than missing: absent and locked are different answers and
    only one of them is honest."""
    import product_capability_availability as catalog

    context = {"org_id": "org-1", "project_id": "project-1",
               "authority_mode": "postgres_canonical"}
    monkeypatch.setattr(jobs_router.jobs.platform_link, "resolve_submission_context",
                        lambda *_args: context)
    monkeypatch.setattr(jobs_router.jobs.platform_link, "canonical_worker_health",
                        lambda _tool: _fresh_heartbeat())
    response = jobs_router.platform_capabilities(
        tenant="demo", x_org_id="org-1", x_project_id="project-1", authorization=None)

    # Exact ids, in exact release-gate order, straight from the catalog.
    assert [c["id"] for c in response["capabilities"]] == [
        entry.id for entry in catalog.PRODUCT_CAPABILITIES]

    # Labels and descriptions come from the catalog too, not from this route. The
    # solver's description was the one place server and console DISAGREED before
    # this lane: the route said "Run the canonical AutoFill target solver." while
    # leaf_website capabilityCatalog.ts said the catalog text.
    solve_entry = catalog.capability(jobs_router.SOLVE_CAPABILITY)
    solve = _by_id(response)[jobs_router.SOLVE_CAPABILITY]
    assert solve["label"] == solve_entry.label
    assert solve["description"] == solve_entry.description

    for descriptor in response["capabilities"]:
        if descriptor["id"] == jobs_router.SOLVE_CAPABILITY:
            continue
        availability = descriptor["availability"]
        assert availability["state"] == "locked_planned", descriptor["id"]
        assert availability["reasonCode"] == catalog.REASON_NO_MEASUREMENT
        assert availability["evidence"] == []
        # Round trip: everything emitted passes the validator that mirrors the
        # console's, so the browser cannot silently swallow any of it.
        assert catalog.is_well_formed_availability(availability) is True

    assert catalog.is_well_formed_availability(solve["availability"]) is True


def test_a_measurement_the_console_would_refuse_locks_with_a_distinct_reason(monkeypatch):
    """The silent failure this lane closes. A future-dated observation is exactly
    what the console clock-skew bound refuses, so emitting it produced a capability
    the browser dropped on the floor with NOTHING reporting why.

    Routed through the catalog the same payload becomes a locked capability
    carrying `live_availability_rejected_by_contract_validator`, distinguishable
    from having no measurement at all. That distinction is the point: an
    integrator defect must not look like an idle worker."""
    import product_capability_availability as catalog

    context = {"org_id": "org-1", "project_id": "project-1",
               "authority_mode": "postgres_canonical"}
    monkeypatch.setattr(jobs_router.jobs.platform_link, "resolve_submission_context",
                        lambda *_args: context)
    monkeypatch.setattr(jobs_router.jobs.platform_link, "canonical_worker_health",
                        lambda _tool: {"runtime": "python-test", "source_revision": "abc123",
                                       "source_sha256": "c" * 64,
                                       "observed_at": "2099-01-01T00:00:00+00:00"})
    response = jobs_router.platform_capabilities(
        tenant="demo", x_org_id="org-1", x_project_id="project-1", authorization=None)
    solve = _by_id(response)[jobs_router.SOLVE_CAPABILITY]
    assert solve["availability"]["state"] == "locked_planned"
    assert solve["availability"]["reasonCode"] == catalog.REASON_REJECTED_MEASUREMENT
    # NOT the absent-measurement reason: a refused payload and no payload must
    # never be reported the same way.
    assert solve["availability"]["reasonCode"] != catalog.REASON_NO_MEASUREMENT


def test_every_catalog_entitlement_name_is_mapped_or_explicitly_unmapped():
    """The two vocabularies do not overlap. `server/entitlements.py` speaks of
    run_read/run_write/solve/build; the catalog speaks of
    run_read/run/solve/review/author. An unmapped name resolves to False, which is
    fail-closed and correct, but it is INDISTINGUISHABLE from a real denial, so a
    capability whose entitlement nobody wired would read "not entitled" forever
    with nothing reporting why. Same silent-failure shape this lane exists to
    remove, so the gap is pinned here instead of left to drift."""
    import entitlements
    import product_capability_availability as catalog

    declared = {name for entry in catalog.PRODUCT_CAPABILITIES
                for name in entry.entitlements}
    mapped = set(jobs_router._ENTITLEMENT_POLICY_KEY)
    assert declared == mapped | jobs_router._UNMAPPED_ENTITLEMENTS, (
        "a catalog entitlement name is neither mapped to a policy capability nor "
        "recorded as deliberately unmapped; wire it or record it")
    # Every MAPPED name must name a real policy capability, or the mapping is a
    # typo that silently denies.
    for policy_key in jobs_router._ENTITLEMENT_POLICY_KEY.values():
        assert policy_key in entitlements.CAPABILITIES, policy_key
    # And no unmapped name may quietly become real without updating the map.
    assert jobs_router._UNMAPPED_ENTITLEMENTS.isdisjoint(entitlements.CAPABILITIES)


def test_entitlement_resolves_per_capability_not_from_one_hardcoded_key(monkeypatch):
    """The route used to read a single `solve` boolean and stamp it on the one
    descriptor it emitted. Seven capabilities declare four different entitlement
    names, so one key cannot answer for all of them."""
    context = {"org_id": "org-1", "project_id": "project-1",
               "authority_mode": "postgres_canonical"}
    monkeypatch.setattr(jobs_router.jobs.platform_link, "resolve_submission_context",
                        lambda *_args: context)
    monkeypatch.setattr(jobs_router.jobs.platform_link, "canonical_worker_health",
                        lambda _tool: None)

    # `hosted_starter` grants run_read but NOT solve, so the two must differ.
    starter = _by_id(jobs_router.platform_capabilities(
        tenant=jobs_router.deps.TenantContext("tenant", tier="hosted_starter"),
        x_org_id="org-1", x_project_id="project-1", authorization=None))
    assert starter[jobs_router.SOLVE_CAPABILITY]["entitled"] is False
    assert starter["drawing.check.electrical"]["entitled"] is False

    demo = _by_id(jobs_router.platform_capabilities(
        tenant="demo", x_org_id="org-1", x_project_id="project-1", authorization=None))
    assert demo[jobs_router.SOLVE_CAPABILITY]["entitled"] is True

    # An unmapped entitlement name denies for EVERY tier, including full-access
    # demo. Fail-closed by decision, pinned so it cannot drift open unnoticed.
    for capability_id in ("drawing.run.approved", "evidence.generate",
                          "tool.author.company"):
        assert demo[capability_id]["entitled"] is False, capability_id

    # A capability that does not declare an entitlement carries no `entitled` key
    # at all: absent is not the same as false.
    assert "entitled" not in demo["drawing.inspect"]
    assert "entitled" not in demo["review.evidence"]


def test_an_unreadable_entitlement_policy_denies_every_capability(monkeypatch):
    """Fail closed on a broken policy, and do it for all seven rather than for the
    one key the route used to read."""
    context = {"org_id": "org-1", "project_id": "project-1",
               "authority_mode": "postgres_canonical"}
    monkeypatch.setattr(jobs_router.jobs.platform_link, "resolve_submission_context",
                        lambda *_args: context)
    monkeypatch.setattr(jobs_router.jobs.platform_link, "canonical_worker_health",
                        lambda _tool: None)

    import entitlements

    def boom(_tier):
        raise entitlements.EntitlementsError("policy unreadable")

    monkeypatch.setattr(entitlements, "entitlements_for", boom)
    response = jobs_router.platform_capabilities(
        tenant="demo", x_org_id="org-1", x_project_id="project-1", authorization=None)
    for descriptor in response["capabilities"]:
        if "entitled" in descriptor:
            assert descriptor["entitled"] is False, descriptor["id"]


def test_canonical_worker_container_contract_is_non_root_and_source_bound():
    dockerfile = (REPO_ROOT / "deploy" / "Dockerfile.canonical-worker").read_text()
    overlay = (REPO_ROOT / "docker-compose.canonical.yml").read_text()
    smoke = (REPO_ROOT / "scripts" / "canonical-container-smoke.py").read_text()

    assert "COPY --from=autofill_solver" in dockerfile
    assert "ARG AUTOFILL_SOLVER_REVISION" in dockerfile
    assert "AUTOFILL_SOLVER_REVISION=${AUTOFILL_SOLVER_REVISION}" in dockerfile
    assert "USER 65532:65532" in dockerfile
    assert "canonical_worker_health('string-autofill-opt')" in dockerfile
    assert "ARG AUTOFILL_SOLVER_REVISION" in dockerfile
    assert "AUTOFILL_SOLVER_REVISION=${AUTOFILL_SOLVER_REVISION}" in dockerfile
    assert "autofill-solver-sources.json" in dockerfile
    assert "attest_source" in dockerfile
    assert 'CMD ["python", "canonical_worker.py"' in dockerfile
    assert '"--lease-seconds", "30"' not in dockerfile

    assert "additional_contexts:" in overlay
    assert overlay.count("autofill_solver: ${AUTOFILL_SOLVER_CONTEXT:-../autofill-solver}") == 2
    assert overlay.count("AUTOFILL_SOLVER_REVISION: ${AUTOFILL_SOLVER_REVISION:?") == 2
    assert "condition: service_completed_successfully" in overlay
    assert "db.apply_migration()" in overlay
    postgres_block = overlay.split("  postgres:", 1)[1].split("  migrate:", 1)[0]
    assert "ports:" not in postgres_block

    assert "idempotent resubmission created another job" in smoke
    assert 'descriptor["source_revision"] != expected_revision' in smoke
    assert '"solverRevision": descriptor["source_revision"]' in smoke
    assert 'expected = {"jobs": 1, "solves": 1, "history": 1, "outbox": 2, "solvePins": 3}' in smoke


def test_autofill_descriptor_prefers_and_validates_exact_configured_revision(
        monkeypatch, tmp_path):
    from solver_adapters import autofill

    (tmp_path / "solver.py").write_text("def solve_targets(): pass\n", encoding="utf-8")
    revision = "a" * 40
    monkeypatch.setenv("AUTOFILL_SOLVER_REVISION", revision)
    monkeypatch.setattr(autofill, "_git_source_revision", lambda _root: None)
    assert autofill.descriptor(solver_root=tmp_path)["source_revision"] == revision

    monkeypatch.setenv("AUTOFILL_SOLVER_REVISION", "not-an-exact-commit")
    with pytest.raises(RuntimeError, match="exact lowercase 40-character commit"):
        autofill.descriptor(solver_root=tmp_path)


def test_autofill_descriptor_rejects_configured_checkout_mismatch(monkeypatch, tmp_path):
    from solver_adapters import autofill

    (tmp_path / "solver.py").write_text("def solve_targets(): pass\n", encoding="utf-8")
    monkeypatch.setenv("AUTOFILL_SOLVER_REVISION", "a" * 40)
    monkeypatch.setattr(autofill, "_git_source_revision", lambda _root: "b" * 40)
    with pytest.raises(RuntimeError, match="does not match the solver checkout"):
        autofill.descriptor(solver_root=tmp_path)


def test_autofill_descriptor_binds_runtime_revision_to_image_marker(monkeypatch, tmp_path):
    from solver_adapters import autofill

    revision = "a" * 40
    (tmp_path / "solver.py").write_text("def solve_targets(): pass\n", encoding="utf-8")
    (tmp_path / ".leaf-source-revision").write_text(revision + "\n", encoding="utf-8")
    monkeypatch.setattr(autofill, "_git_source_revision", lambda _root: None)
    monkeypatch.delenv("AUTOFILL_SOLVER_REVISION", raising=False)
    assert autofill.descriptor(solver_root=tmp_path)["source_revision"] == revision

    monkeypatch.setenv("AUTOFILL_SOLVER_REVISION", "b" * 40)
    with pytest.raises(RuntimeError, match="does not match the worker image"):
        autofill.descriptor(solver_root=tmp_path)


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
            jobs_router.RunRequest(
                tool="string-autofill-opt", params={}, dwg=str(uuid.uuid4()),
                catalog_digest=jobs_router.deps.catalog_tool_digest(tool)),
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
            jobs_router.RunRequest(
                tool="demo-read-tool", params={},
                catalog_digest=jobs_router.deps.catalog_tool_digest(tool)),
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


def test_run_route_non_utf8_policy_is_structured_503(monkeypatch, tmp_path):
    """UnicodeDecodeError is a ValueError, not an OSError — a non-UTF-8 policy
    file must still convert to the structured 503, never a bare 500."""
    import json

    bad = tmp_path / "binary-entitlements.json"
    bad.write_bytes(b"\xff\xfe\x00garbage\x9c")
    monkeypatch.setenv("LEAF_ENTITLEMENTS_FILE", str(bad))
    tool = {"name": "demo-read-tool", "capabilities": ["drawing.read"], "default_params": {}}
    monkeypatch.setattr(jobs_router.deps, "find_tool", lambda *_args: tool)
    response = jobs_router.run(
            jobs_router.RunRequest(
                tool="demo-read-tool", params={},
                catalog_digest=jobs_router.deps.catalog_tool_digest(tool)),
        tenant_id="tenant-1", x_org_id=None, x_project_id=None,
        idempotency_key=None, authorization=None)
    assert response.status_code == 503
    body = json.loads(response.body)
    assert body["error"]["error_code"] == "INTERNAL"
    assert body["error"]["retryable"] is True
