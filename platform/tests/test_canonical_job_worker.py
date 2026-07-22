import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from leaf_platform import canonical_jobs, store
from leaf_platform.db import cursor


def _project(make_org, name="Canonical worker"):
    org = make_org(name)
    project = store.create_project(org.org_id, name)
    return org, project


def _result(answer=40):
    request = {}
    solver_input = {"groups": [], "panelsPerString": 10, "options": {}}
    solver_result = {"feasible": True, "groupTargets": {"A": answer}}
    digest = hashlib.sha256(json.dumps(
        solver_result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    input_digest = hashlib.sha256(json.dumps(
        solver_input, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    request_digest = hashlib.sha256(json.dumps(
        request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"solver": "string-autofill-opt", "solver_input": solver_input,
            "solver_result": solver_result, "request_sha256": request_digest,
            "input_sha256": input_digest, "result_sha256": digest,
            "solver_revision": "revision-1", "source_sha256": "c" * 64,
            "runtime": "python-test"}


def _provenance(attempt=1):
    return {"attempt": attempt, "execution_path": "local",
            "solver_revision": "revision-1", "source_sha256": "c" * 64,
            "runtime": "python-test"}


def test_submission_fails_closed_until_postgres_and_is_idempotent(make_org):
    org, project = _project(make_org)
    with pytest.raises(ValueError, match="not postgres_canonical"):
        canonical_jobs.submit_solve_job(
            org.org_id, project.project_id, "tenant-a", "string-autofill-opt", {}, "req-1")
    store.set_project_authority_mode(org.org_id, project.project_id, "postgres_canonical")
    other_org, other_project = _project(make_org, "Other owner")
    foreign_version = store.create_drawing_version(other_org.org_id, other_project.project_id)
    with pytest.raises(ValueError, match="unavailable"):
        canonical_jobs.submit_solve_job(
            org.org_id, project.project_id, "tenant-a", "string-autofill-opt", {},
            "foreign-version", input_version_id=foreign_version.version_id)
    first = canonical_jobs.submit_solve_job(
        org.org_id, project.project_id, "tenant-a", "string-autofill-opt",
        {"panelsPerString": 10}, "req-1")
    repeated = canonical_jobs.submit_solve_job(
        org.org_id, project.project_id, "tenant-a", "string-autofill-opt",
        {"panelsPerString": 10}, "req-1")
    assert repeated["job_id"] == first["job_id"]
    assert set(first["execution_context"]["snapshot_pins"]) == {"catalog", "standards", "ahj"}
    assert first["execution_context"]["capability_state"] == "connected_degraded"
    with pytest.raises(ValueError, match="different run input"):
        canonical_jobs.submit_solve_job(
            org.org_id, project.project_id, "tenant-a", "string-autofill-opt",
            {"panelsPerString": 11}, "req-1")


def test_two_claimers_get_one_owner_and_tenant_reads_are_isolated(make_org):
    org, project = _project(make_org)
    store.set_project_authority_mode(org.org_id, project.project_id, "postgres_canonical")
    job = canonical_jobs.submit_solve_job(
        org.org_id, project.project_id, "tenant-claim", "string-autofill-opt", {}, "claim-1")
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(
            lambda owner: canonical_jobs.claim_next(owner, request_tenant_id="tenant-claim"),
            ("worker-a", "worker-b")))
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    assert winners[0]["job_id"] == job["job_id"]
    assert winners[0]["attempt"] == 1
    assert canonical_jobs.get_job_for_tenant(job["job_id"], "other-tenant") is None
    assert canonical_jobs.list_jobs_for_tenant("other-tenant") == []


def test_heartbeat_and_stale_completion_require_current_unexpired_lease(make_org):
    org, project = _project(make_org)
    store.set_project_authority_mode(org.org_id, project.project_id, "postgres_canonical")
    job = canonical_jobs.submit_solve_job(
        org.org_id, project.project_id, "tenant-lease", "string-autofill-opt", {}, "lease-1")
    start = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    assert canonical_jobs.claim_next(
        "worker-a", lease_seconds=10, now=start,
        request_tenant_id="tenant-lease")["job_id"] == job["job_id"]
    assert canonical_jobs.heartbeat(job["job_id"], "worker-b", now=start) is False
    assert canonical_jobs.heartbeat(job["job_id"], "worker-a", now=start + timedelta(seconds=5))
    assert canonical_jobs.complete_solve(
        job["job_id"], "worker-a", _result(), _provenance(),
        now=start + timedelta(seconds=40)) == "not_owner"


def test_completion_atomically_writes_one_solve_history_and_outbox(make_org):
    org, project = _project(make_org)
    store.set_project_authority_mode(org.org_id, project.project_id, "postgres_canonical")
    job = canonical_jobs.submit_solve_job(
        org.org_id, project.project_id, "tenant-complete", "string-autofill-opt", {}, "done-1")
    claimed = canonical_jobs.claim_next("worker-complete", request_tenant_id="tenant-complete")
    assert claimed["job_id"] == job["job_id"]
    result, provenance = _result(), _provenance()
    assert canonical_jobs.complete_solve(
        job["job_id"], "worker-complete", result, provenance) == "applied"
    assert canonical_jobs.complete_solve(
        job["job_id"], "worker-complete", result, provenance) == "duplicate"
    conflicting = _result(answer=30)
    assert canonical_jobs.complete_solve(
        job["job_id"], "worker-complete", conflicting, provenance) == "conflict"
    saved = canonical_jobs.get_job_for_tenant(job["job_id"], "tenant-complete")
    assert saved["status"] == "succeeded"
    assert saved["result"]["solver_result"] == result["solver_result"]
    assert saved["result"]["solve_hash"]
    assert saved["result"]["history_hash"]
    assert set(saved["result"]["snapshotPins"]) == {"catalog", "standards", "ahj"}
    with cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM solve_records WHERE org_id = %(org)s AND project_id = %(project)s",
                    {"org": org.org_id, "project": project.project_id})
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT COUNT(*) AS n FROM history_operations WHERE org_id = %(org)s AND project_id = %(project)s",
                    {"org": org.org_id, "project": project.project_id})
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT COUNT(*) AS n FROM outbox_entries WHERE org_id = %(org)s AND project_id = %(project)s",
                    {"org": org.org_id, "project": project.project_id})
        assert cur.fetchone()["n"] == 2
        cur.execute("SELECT COUNT(*) AS n FROM solve_snapshot_pins WHERE solve_id = %(solve)s",
                    {"solve": saved["result"]["solve_id"]})
        assert cur.fetchone()["n"] == 3


def test_completion_rejects_false_hash_bool_attempt_and_provenance_mismatch(make_org):
    org, project = _project(make_org)
    store.set_project_authority_mode(org.org_id, project.project_id, "postgres_canonical")
    job = canonical_jobs.submit_solve_job(
        org.org_id, project.project_id, "tenant-invalid", "string-autofill-opt", {}, "invalid-1")
    canonical_jobs.claim_next("worker-invalid", request_tenant_id="tenant-invalid")
    bad_hash = _result()
    bad_hash["result_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="does not match"):
        canonical_jobs.complete_solve(job["job_id"], "worker-invalid", bad_hash, _provenance())
    bad_input = _result()
    bad_input["input_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="input_sha256 does not match"):
        canonical_jobs.complete_solve(job["job_id"], "worker-invalid", bad_input, _provenance())
    bad_request = _result()
    bad_request["request_sha256"] = "b" * 64
    with pytest.raises(ValueError, match="request_sha256 does not match"):
        canonical_jobs.complete_solve(job["job_id"], "worker-invalid", bad_request, _provenance())
    bad_solver = _result()
    bad_solver["solver"] = "different-solver"
    with pytest.raises(ValueError, match="solver identity"):
        canonical_jobs.complete_solve(job["job_id"], "worker-invalid", bad_solver, _provenance())
    with pytest.raises(ValueError, match="durable attempt"):
        canonical_jobs.complete_solve(job["job_id"], "worker-invalid", _result(), _provenance(True))
    mismatch = _result()
    mismatch["runtime"] = "python-other"
    with pytest.raises(ValueError, match="does not match provenance"):
        canonical_jobs.complete_solve(job["job_id"], "worker-invalid", mismatch, _provenance())
    assert canonical_jobs.get_job_for_tenant(job["job_id"], "tenant-invalid")["status"] == "running"


def test_retry_is_bounded_and_expired_owner_cannot_release(make_org):
    org, project = _project(make_org)
    store.set_project_authority_mode(org.org_id, project.project_id, "postgres_canonical")
    job = canonical_jobs.submit_solve_job(
        org.org_id, project.project_id, "tenant-retry", "string-autofill-opt", {},
        "retry-1", max_attempts=2)
    start = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    canonical_jobs.claim_next(
        "worker-1", lease_seconds=10, now=start, request_tenant_id="tenant-retry")
    error = {"error_code": "SOLVER_FAILED", "message": "temporary", "retryable": True}
    assert canonical_jobs.fail_or_retry(
        job["job_id"], "worker-1", error, _provenance(),
        now=start + timedelta(seconds=20)) == "not_owner"
    assert canonical_jobs.fail_or_retry(
        job["job_id"], "worker-1", error, _provenance(),
        now=start + timedelta(seconds=5)) == "retry"
    second = canonical_jobs.claim_next(
        "worker-2", now=start + timedelta(seconds=6), request_tenant_id="tenant-retry")
    assert second["attempt"] == 2
    assert canonical_jobs.fail_or_retry(
        job["job_id"], "worker-2", error, _provenance(2),
        now=start + timedelta(seconds=7)) == "failed"
    assert canonical_jobs.get_job_for_tenant(job["job_id"], "tenant-retry")["status"] == "failed"


def test_worker_health_is_short_lived():
    observed = datetime(2026, 7, 21, 12, 0, tzinfo=timezone.utc)
    canonical_jobs.record_worker_heartbeat(
        "health-worker", "string-autofill-opt", "python-test", "revision-test", "c" * 64,
        now=observed)
    health = canonical_jobs.worker_health(
        "string-autofill-opt", now=observed + timedelta(seconds=5))
    assert health["worker_id"] == "health-worker"
    assert canonical_jobs.worker_health(
        "string-autofill-opt", now=observed + timedelta(seconds=16)) is None
