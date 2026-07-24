"""Hermetic Marathon adapter acceptance tests (no broker or platform database)."""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import broker_client  # noqa: E402
import jobs  # noqa: E402
from routers import jobs as jobs_router  # noqa: E402


TOOL = {"name": "marathon-tool", "engine_op": "count_by_layer",
        "capabilities": ["drawing.read"]}


class RecordingExecutor:
    def __init__(self, run=False):
        self.calls = []
        self.run = run

    def submit(self, fn, *args, **kwargs):
        self.calls.append((fn, args, kwargs))
        if self.run:
            fn(*args, **kwargs)


@pytest.fixture(autouse=True)
def isolated_jobs(monkeypatch, tmp_path):
    # Preserve any connection established by an earlier test module. Closing it
    # here would make pytest restore a dead connection after this fixture and
    # cause unrelated later API tests to fail with a 500.
    monkeypatch.setattr(jobs, "DB_PATH", tmp_path / "marathon.db")
    monkeypatch.setattr(jobs, "_conn", None)
    monkeypatch.setattr(jobs, "_reaper_started", True)
    monkeypatch.setattr(jobs, "_executors", {jobs.LANE_FAST: RecordingExecutor(),
                                               jobs.LANE_SLOW: RecordingExecutor()})
    monkeypatch.setattr(jobs.platform_link, "on_submit", lambda *a, **k: None)
    monkeypatch.setattr(jobs.platform_link, "on_running", lambda *a, **k: None)
    monkeypatch.setattr(jobs.platform_link, "on_terminal", lambda *a, **k: None)
    yield
    if jobs._conn is not None:
        jobs._conn.close()


def _submit(*, aps_live=False, tool=None):
    return jobs.submit_job("tenant-marathon", tool or TOOL, {}, "demo", aps_live)


def test_competing_claims_have_exactly_one_owner():
    job_id = _submit()
    assert jobs.claim_lease(job_id, "worker-a") == 1
    assert jobs.claim_lease(job_id, "worker-b") is None
    assert jobs.get_job(job_id)["lease"]["owner"] == "worker-a"


def test_project_submission_idempotency_reuses_same_job_and_rejects_collision():
    first = jobs.submit_job(
        "tenant-marathon", TOOL, {"value": 1}, "demo", False,
        org_id="org-1", project_id="project-1", idempotency_key="request-1",
        platform_context={"org_id": "org-1", "project_id": "project-1",
                          "authority_mode": "legacy_sqlite"})
    repeated = jobs.submit_job(
        "tenant-marathon", TOOL, {"value": 1}, "demo", False,
        org_id="org-1", project_id="project-1", idempotency_key="request-1",
        platform_context={"org_id": "org-1", "project_id": "project-1",
                          "authority_mode": "legacy_sqlite"})
    assert repeated == first
    assert len(jobs._executors[jobs.LANE_FAST].calls) == 1
    with pytest.raises(ValueError, match="different run input"):
        jobs.submit_job(
            "tenant-marathon", TOOL, {"value": 2}, "demo", False,
            org_id="org-1", project_id="project-1", idempotency_key="request-1",
            platform_context={"org_id": "org-1", "project_id": "project-1",
                              "authority_mode": "legacy_sqlite"})


def test_live_project_context_uses_verified_binding_and_only_unlocks_postgres(monkeypatch):
    verified_org = uuid.uuid4()
    project_id = uuid.uuid4()
    authority = {"mode": "postgres_canonical"}

    class Store:
        @staticmethod
        def get_project(org_id, requested_project):
            return object() if (org_id, requested_project) == (verified_org, project_id) else None

        @staticmethod
        def get_authority_mode(_org_id, _project_id):
            return authority["mode"]

    class PlatformDeps:
        @staticmethod
        def auth_live():
            return True

        @staticmethod
        def get_write_org_id(x_org_id=None, authorization=None):
            assert x_org_id is None
            assert authorization == "Bearer verified"
            return verified_org

    monkeypatch.setattr(jobs.platform_link, "_db_configured", lambda: True)
    monkeypatch.setattr(jobs.platform_link, "_load_platform",
                        lambda: (Store, object(), PlatformDeps))
    with pytest.raises(ValueError, match="does not belong"):
        jobs.platform_link.resolve_submission_context(
            str(uuid.uuid4()), str(project_id), "Bearer verified")
    context = jobs.platform_link.resolve_submission_context(
        str(verified_org), str(project_id), "Bearer verified")
    assert context == {"org_id": verified_org, "project_id": project_id,
                       "authority_mode": "postgres_canonical"}
    authority["mode"] = "legacy_sqlite"
    with pytest.raises(ValueError, match="legacy_sqlite.*locked"):
        jobs.platform_link.resolve_submission_context(
            str(verified_org), str(project_id), "Bearer verified")


def test_expired_lease_cannot_heartbeat_or_complete_and_attempts_are_bounded(monkeypatch):
    monkeypatch.setenv("JOB_MAX_ATTEMPTS", "1")
    job_id = _submit()
    assert jobs.claim_lease(job_id, "worker-a", now=10.0) == 1
    assert jobs.heartbeat_lease(job_id, "worker-a") is False
    assert jobs.complete_callback(
        job_id, "complete", result_env={"ok": True}, worker_id="worker-a",
        provenance={"attempt": 1, "execution_path": "local"}) == "not_owner"
    assert jobs.claim_lease(job_id, "worker-b", now=100.0) is None
    assert jobs._redispatch_record(job_id) is True
    rec = jobs.get_job(job_id)
    assert rec["status"] == "failed"
    assert rec["attempt"] == 1


def test_expired_worker_cannot_release_retry_or_redispatch():
    job_id = _submit()
    assert jobs.claim_lease(job_id, "worker-old", now=10.0) == 1
    before_calls = len(jobs._executors[jobs.LANE_FAST].calls)
    jobs._retry_or_finish(
        job_id, "worker-old", "tenant-marathon", TOOL, {}, "demo", False,
        {"error_code": "BROKER_UNREACHABLE", "message": "late", "retryable": True},
        {"attempt": 1, "execution_path": "local", "failure": "late"},
    )
    rec = jobs.get_job(job_id)
    assert rec["status"] == "running"
    assert rec["lease"]["owner"] == "worker-old"
    assert len(jobs._executors[jobs.LANE_FAST].calls) == before_calls


def test_restart_recovery_reclaims_dead_owner_to_one_terminal_outcome(monkeypatch):
    job_id = _submit()
    assert jobs.claim_lease(job_id, "dead-owner") == 1
    jobs._exec("UPDATE jobs SET lease_expires_at = 0 WHERE job_id = ?", (job_id,))
    monkeypatch.setattr(jobs, "_executors", {jobs.LANE_FAST: RecordingExecutor(run=True),
                                               jobs.LANE_SLOW: RecordingExecutor(run=True)})
    monkeypatch.setattr(broker_client, "run_via_broker", lambda *a, **k: {"ok": True})

    assert jobs._reap_orphans_once() == 1
    rec = jobs.get_job(job_id)
    assert rec["status"] == "complete"
    assert rec["attempt"] == 2
    # A stale old worker cannot install a competing terminal result.
    assert jobs.complete_callback(job_id, "failed", error={"error_code": "INTERNAL"}) == "conflict"
    assert jobs.get_job(job_id)["status"] == "complete"


def test_duplicate_callback_is_harmless_and_conflict_does_not_overwrite():
    job_id = _submit()
    worker_id = "worker-terminal"
    assert jobs.claim_lease(job_id, worker_id) == 1
    result = {"ok": True, "result": {"answer": 42}}
    provenance = {"attempt": 1, "execution_path": "local"}
    assert jobs.complete_callback(job_id, "complete", result_env=result,
                                  provenance=provenance, worker_id=worker_id) == "applied"
    assert jobs.complete_callback(job_id, "complete", result_env=result,
                                  provenance=provenance, worker_id=worker_id) == "duplicate"
    assert jobs.complete_callback(job_id, "complete", result_env=result,
                                  provenance={"attempt": 1, "execution_path": "local",
                                              "delivery": "different"},
                                  worker_id=worker_id) == "conflict"
    assert jobs.complete_callback(job_id, "failed", error={"error_code": "INTERNAL"}) == "conflict"
    rec = jobs.get_job(job_id)
    assert rec["status"] == "complete"
    assert rec["result"] == result


def test_success_rejects_empty_or_incomplete_provenance():
    job_id = _submit()
    worker_id = "worker-validation"
    assert jobs.claim_lease(job_id, worker_id) == 1
    with pytest.raises(ValueError, match="positive attempt"):
        jobs.complete_callback(job_id, "complete", result_env={"ok": True}, provenance={},
                               worker_id=worker_id)
    with pytest.raises(ValueError, match="execution_path"):
        jobs.complete_callback(
            job_id, "complete", result_env={"ok": True},
            provenance={"attempt": 1, "execution_path": "unknown"}, worker_id=worker_id)


def test_success_rejects_false_result_wrong_attempt_and_masked_fallback():
    false_job = _submit()
    assert jobs.claim_lease(false_job, "worker-false") == 1
    with pytest.raises(ValueError, match="result.ok true"):
        jobs.complete_callback(
            false_job, "complete", result_env={"ok": False}, worker_id="worker-false",
            provenance={"attempt": 1, "execution_path": "local"})

    wrong_attempt_job = _submit()
    assert jobs.claim_lease(wrong_attempt_job, "worker-wrong") == 1
    with pytest.raises(ValueError, match="does not match durable attempt"):
        jobs.complete_callback(
            wrong_attempt_job, "complete", result_env={"ok": True}, worker_id="worker-wrong",
            provenance={"attempt": 999, "execution_path": "local"})

    masked_job = _submit(aps_live=True)
    assert jobs.claim_lease(masked_job, "worker-masked") == 1
    with pytest.raises(ValueError, match="must declare fallback"):
        jobs.complete_callback(
            masked_job, "complete", result_env={"ok": True}, worker_id="worker-masked",
            provenance={"attempt": 1, "execution_path": "local"})
    with pytest.raises(ValueError, match="meaningful cloud_failure"):
        jobs.complete_callback(
            masked_job, "complete", result_env={"ok": True}, worker_id="worker-masked",
            provenance={"attempt": 1, "execution_path": "local", "fallback": True,
                        "fallback_reason": "cloud failed", "cloud_failure": {}})
    invalid_cloud_failures = (
        {"attempt": 1, "execution_path": "cloud", "failure": "   "},
        {"attempt": 1, "execution_path": "cloud",
         "failure": {"code": None, "message": None}},
        {"attempt": True, "execution_path": "cloud", "failure": "cloud unavailable"},
    )
    for cloud_failure in invalid_cloud_failures:
        with pytest.raises(ValueError, match="meaningful cloud_failure"):
            jobs.complete_callback(
                masked_job, "complete", result_env={"ok": True}, worker_id="worker-masked",
                provenance={"attempt": 1, "execution_path": "local", "fallback": True,
                            "fallback_reason": "cloud failed",
                            "cloud_failure": cloud_failure})

    for job_id in (false_job, wrong_attempt_job, masked_job):
        assert jobs.get_job(job_id)["status"] == "running"


def test_success_rejects_error_nonboolean_fallback_and_embedded_provenance_conflict():
    job_id = _submit()
    worker_id = "worker-envelope"
    assert jobs.claim_lease(job_id, worker_id) == 1
    local = {"attempt": 1, "execution_path": "local"}
    with pytest.raises(ValueError, match="cannot include terminal error"):
        jobs.complete_callback(
            job_id, "complete", result_env={"ok": True}, error={"message": "contradiction"},
            worker_id=worker_id, provenance=local)
    with pytest.raises(ValueError, match="fallback flag must be boolean"):
        jobs.complete_callback(
            job_id, "complete", result_env={"ok": True}, worker_id=worker_id,
            provenance={**local, "fallback": 1})
    with pytest.raises(ValueError, match="contradicts terminal provenance"):
        jobs.complete_callback(
            job_id, "complete",
            result_env={"ok": True,
                        "execution_provenance": {"attempt": True, "execution_path": "local"}},
            worker_id=worker_id, provenance=local)
    assert jobs.get_job(job_id)["status"] == "running"


def test_contradictory_callbacks_are_compare_and_set_across_processes():
    job_id = _submit()
    worker_id = "worker-multiprocess"
    assert jobs.claim_lease(job_id, worker_id) == 1
    script = r"""
import json, os, sys
os.environ['JOBS_DB'] = sys.argv[1]
sys.path.insert(0, sys.argv[2])
import jobs
outcome = jobs.complete_callback(
    sys.argv[3], 'complete', result_env={'ok': True, 'winner': sys.argv[5]},
    worker_id=sys.argv[4],
    provenance={'attempt': 1, 'execution_path': 'local', 'delivery': sys.argv[5]},
)
print(outcome)
"""
    args = [str(jobs.DB_PATH), str(SERVER_DIR), job_id, worker_id]
    # The subprocess completes a job, which emits a JobTerminal EMF metric line
    # to STDOUT (the CloudWatch awslogs pattern). This test parses subprocess
    # STDOUT for its "applied"/"conflict" result, so disable EMF in the children
    # to keep their stdout clean. (APS_EMF_DISABLED is the emitter's own gate.)
    _child_env = {**os.environ, "APS_EMF_DISABLED": "1"}
    first = subprocess.Popen(
        [sys.executable, "-c", script, *args, "a"], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=_child_env)
    second = subprocess.Popen(
        [sys.executable, "-c", script, *args, "b"], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, env=_child_env)
    first_out, first_err = first.communicate(timeout=20)
    second_out, second_err = second.communicate(timeout=20)
    assert first.returncode == 0, first_err
    assert second.returncode == 0, second_err
    assert {first_out.strip(), second_out.strip()} == {"applied", "conflict"}
    assert jobs.get_job(job_id)["status"] == "complete"


def test_cloud_failure_with_explicit_local_fallback_records_provenance(monkeypatch):
    tool = dict(TOOL, allow_local_fallback=True)
    job_id = _submit(aps_live=True, tool=tool)

    def broker(_tenant, _tool, _params, _dwg, aps_live, **_kwargs):
        if aps_live:
            return {
                "ok": False,
                "error": {
                    "error_code": jobs.ErrorCode.WORKITEM_FAILED,
                    "message": "cloud terminal failure",
                    "retryable": False,
                },
            }
        return {"ok": True, "result": {"source": "local"}}

    monkeypatch.setattr(broker_client, "run_via_broker", broker)
    jobs._run_job(job_id, "tenant-marathon", tool, {}, "demo", True)
    rec = jobs.get_job(job_id)
    assert rec["status"] == "complete"
    assert rec["provenance"]["execution_path"] == "local"
    assert rec["provenance"]["fallback"] is True
    assert rec["provenance"]["cloud_failure"]["execution_path"] == "cloud"
    assert rec["result"]["execution_provenance"] == rec["provenance"]


def test_response_loss_never_invokes_local_fallback(monkeypatch):
    tool = dict(TOOL, allow_local_fallback=True)
    job_id = _submit(aps_live=True, tool=tool)
    monkeypatch.setattr(
        broker_client, "run_via_broker",
        lambda *a, **k: (_ for _ in ()).throw(
            broker_client.BrokerUnreachable("response lost")),
    )
    monkeypatch.setattr(
        jobs, "_run_local_fallback",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("uncertain cloud execution must not fall back")),
    )

    jobs._run_job(job_id, "tenant-marathon", tool, {}, "demo", True)
    rec = jobs.get_job(job_id)
    assert rec["status"] == "submitted"
    assert rec["error"]["error_code"] == jobs.ErrorCode.BROKER_UNREACHABLE


def test_turn_in_progress_never_invokes_local_fallback(monkeypatch):
    tool = dict(TOOL, allow_local_fallback=True)
    job_id = _submit(aps_live=True, tool=tool)
    monkeypatch.setattr(
        broker_client, "run_via_broker",
        lambda *a, **k: {
            "ok": False,
            "error": {
                "error_code": jobs.ErrorCode.TURN_IN_PROGRESS,
                "message": "admission still executing",
                "retryable": True,
            },
        },
    )
    monkeypatch.setattr(
        jobs, "_run_local_fallback",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("executing admission must not fall back")),
    )

    jobs._run_job(job_id, "tenant-marathon", tool, {}, "demo", True)
    rec = jobs.get_job(job_id)
    assert rec["status"] == "submitted"
    assert rec["error"]["error_code"] == jobs.ErrorCode.TURN_IN_PROGRESS


def test_cloud_failure_without_fallback_is_retryable_not_local(monkeypatch):
    job_id = _submit(aps_live=True)
    monkeypatch.setenv("JOB_MAX_ATTEMPTS", "2")
    monkeypatch.setattr(broker_client, "run_via_broker",
                        lambda *a, **k: (_ for _ in ()).throw(
                            broker_client.BrokerUnreachable("cloud down")))
    jobs._run_job(job_id, "tenant-marathon", TOOL, {}, "demo", True)
    rec = jobs.get_job(job_id)
    assert rec["status"] == "submitted"
    assert rec["error"]["retryable"] is True
    assert rec["provenance"]["execution_path"] == "cloud"
    assert rec["result"] is None


def test_worker_callback_fails_closed_and_requires_current_lease(monkeypatch):
    job_id = _submit()
    assert jobs.claim_lease(job_id, "worker-a") == 1
    body = jobs_router.TerminalCallback(worker_id="worker-a", status="complete",
                                        result={"ok": True},
                                        provenance={"attempt": 1, "execution_path": "local"})

    monkeypatch.delenv("LEAF_BROKER_SECRET", raising=False)
    disabled = jobs_router.terminal_callback(
        job_id, body, x_tenant_id="tenant-marathon", x_broker_secret=None)
    assert disabled.status_code == 503
    assert jobs.get_job(job_id)["status"] == "running"

    monkeypatch.setenv("LEAF_BROKER_SECRET", "broker-test-secret")
    wrong_secret = jobs_router.terminal_callback(
        job_id, body, x_tenant_id="tenant-marathon", x_broker_secret="wrong")
    assert wrong_secret.status_code == 401
    wrong_tenant = jobs_router.terminal_callback(
        job_id, body, x_tenant_id="other-tenant", x_broker_secret="broker-test-secret")
    assert wrong_tenant.status_code == 404

    stale = jobs_router.TerminalCallback(worker_id="worker-b", status="complete",
                                         result={"ok": True},
                                         provenance={"attempt": 1, "execution_path": "local"})
    wrong_owner = jobs_router.terminal_callback(
        job_id, stale, x_tenant_id="tenant-marathon", x_broker_secret="broker-test-secret")
    assert wrong_owner.status_code == 409
    accepted = jobs_router.terminal_callback(
        job_id, body, x_tenant_id="tenant-marathon", x_broker_secret="broker-test-secret")
    assert accepted["status"] == "complete"


def test_worker_callback_rejects_empty_success_provenance(monkeypatch):
    job_id = _submit()
    assert jobs.claim_lease(job_id, "worker-a") == 1
    monkeypatch.setenv("LEAF_BROKER_SECRET", "broker-test-secret")
    body = jobs_router.TerminalCallback(worker_id="worker-a", status="complete",
                                        result={"ok": True}, provenance={})
    response = jobs_router.terminal_callback(
        job_id, body, x_tenant_id="tenant-marathon", x_broker_secret="broker-test-secret")
    assert response.status_code == 400
    assert jobs.get_job(job_id)["status"] == "running"
