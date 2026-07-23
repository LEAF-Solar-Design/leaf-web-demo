"""PostgreSQL async-job and callback replay authority tests.

The real database tests skip unless DATABASE_URL is explicit.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
ROOT = SERVER_DIR.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import broker  # noqa: E402
import jobs  # noqa: E402
from job_pg_store import PostgresCallbackReplayStore  # noqa: E402


TOOL = {
    "name": "postgres-job-test",
    "engine_op": "count_by_layer",
    "capabilities": ["drawing.read"],
}


class RecordingExecutor:
    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()

    def submit(self, *args, **kwargs):
        with self.lock:
            self.calls.append((args, kwargs))


def test_postgres_selection_fails_closed_without_database_url(monkeypatch):
    monkeypatch.setenv("LEAF_JOBS_STORE", "postgres")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    import job_pg_store

    class MissingDb:
        @staticmethod
        def cursor():
            raise RuntimeError("DATABASE_URL is not set")

    monkeypatch.setattr(job_pg_store, "_db", lambda: MissingDb)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        jobs.ensure_started()


@pytest.fixture
def postgres_authority(monkeypatch):
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL is required for PostgreSQL job tests")
    import importlib.util

    package = ROOT / "platform"
    loaded = sys.modules.get("leaf_platform")
    if loaded is None:
        spec = importlib.util.spec_from_file_location(
            "leaf_platform", package / "__init__.py",
            submodule_search_locations=[str(package)],
        )
        assert spec is not None and spec.loader is not None
        loaded = importlib.util.module_from_spec(spec)
        sys.modules["leaf_platform"] = loaded
        spec.loader.exec_module(loaded)
    from leaf_platform import db

    db.apply_migration(package / "migrations" / "0011_jobs_callbacks.sql")
    monkeypatch.setenv("LEAF_JOBS_STORE", "postgres")
    monkeypatch.setenv("LEAF_CALLBACK_REPLAY_STORE", "postgres")
    monkeypatch.setattr(jobs, "_reaper_started", True)
    executors = {
        jobs.LANE_FAST: RecordingExecutor(),
        jobs.LANE_SLOW: RecordingExecutor(),
    }
    monkeypatch.setattr(jobs, "_executors", executors)
    monkeypatch.setattr(jobs.platform_link, "on_submit", lambda *a, **k: None)
    monkeypatch.setattr(jobs.platform_link, "on_running", lambda *a, **k: None)
    monkeypatch.setattr(jobs.platform_link, "on_terminal", lambda *a, **k: None)
    yield db, executors


def _submit(tenant_id: str, *, project_id: str, key: str, value: int = 1) -> str:
    return jobs.submit_job(
        tenant_id, TOOL, {"value": value}, "demo", False,
        project_id=project_id, idempotency_key=key,
        authority_mode="postgres_canonical",
    )


def test_concurrent_submission_and_lease_claim_are_fleet_atomic(postgres_authority):
    _, executors = postgres_authority
    tenant = "pg-jobs-" + uuid.uuid4().hex
    project = "project-" + uuid.uuid4().hex
    key = "submit-" + uuid.uuid4().hex

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(lambda _: _submit(
            tenant, project_id=project, key=key), range(2)))
    assert ids[0] == ids[1]
    assert len(executors[jobs.LANE_FAST].calls) == 1

    job_id = ids[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(
            lambda worker: jobs.claim_lease(job_id, worker),
            ("pg-worker-a", "pg-worker-b"),
        ))
    assert sorted(claim for claim in claims if claim is not None) == [1]
    record = jobs.get_job(job_id)
    assert record is not None
    assert record["status"] == "running"
    assert record["lease"]["owner"] in {"pg-worker-a", "pg-worker-b"}

    with pytest.raises(ValueError, match="different run input"):
        _submit(tenant, project_id=project, key=key, value=2)


def test_process_separated_workers_have_one_lease_winner(postgres_authority):
    tenant = "pg-process-claim-" + uuid.uuid4().hex
    job_id = _submit(
        tenant, project_id="project-" + uuid.uuid4().hex,
        key="process-claim-" + uuid.uuid4().hex,
    )
    script = r"""
import os, sys
sys.path.insert(0, sys.argv[1])
os.environ["LEAF_JOBS_STORE"] = "postgres"
import jobs
print(jobs.claim_lease(sys.argv[2], sys.argv[3]))
"""
    environment = dict(os.environ)
    environment["LEAF_JOBS_STORE"] = "postgres"
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script, str(SERVER_DIR), job_id, worker],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=environment,
        )
        for worker in ("process-worker-a", "process-worker-b")
    ]
    outputs = []
    for process in processes:
        stdout, stderr = process.communicate(timeout=30)
        assert process.returncode == 0, stderr
        outputs.append(stdout.strip())
    assert sorted(outputs) == ["1", "None"]


def test_retry_release_is_fenced_and_schedules_exactly_one_retry(postgres_authority):
    _, executors = postgres_authority
    tenant = "pg-retry-" + uuid.uuid4().hex
    job_id = _submit(
        tenant, project_id="project-" + uuid.uuid4().hex,
        key="retry-" + uuid.uuid4().hex,
    )
    worker = "retry-worker"
    assert jobs.claim_lease(job_id, worker) == 1
    before = len(executors[jobs.LANE_FAST].calls)
    error = {
        "error_code": "BROKER_UNREACHABLE", "message": "retry", "retryable": True,
    }
    provenance = {
        "attempt": 1, "execution_path": "local", "failure": "retry",
    }
    jobs._retry_or_finish(
        job_id, worker, tenant, TOOL, {"value": 1}, "demo", False,
        error, provenance,
    )
    record = jobs.get_job(job_id)
    assert record is not None
    assert record["status"] == "submitted"
    assert record["progress"] == "retrying"
    assert record["lease"] is None
    assert len(executors[jobs.LANE_FAST].calls) == before + 1
    assert jobs.heartbeat_lease(job_id, worker) is False
    assert jobs.complete_callback(
        job_id, "complete", result_env={"ok": True}, worker_id=worker,
        provenance={"attempt": 1, "execution_path": "local"},
    ) == "not_owner"
    # A duplicate stale release cannot enqueue another delivery.
    jobs._retry_or_finish(
        job_id, worker, tenant, TOOL, {"value": 1}, "demo", False,
        error, provenance,
    )
    assert len(executors[jobs.LANE_FAST].calls) == before + 1


def test_close_revokes_lease_and_only_reaper_can_terminalize(postgres_authority):
    tenant = "pg-close-" + uuid.uuid4().hex
    job_id = _submit(
        tenant, project_id="project-" + uuid.uuid4().hex,
        key="close-" + uuid.uuid4().hex,
    )
    worker = "close-worker"
    assert jobs.claim_lease(job_id, worker) == 1
    assert jobs.mark_job_closed(job_id) is True
    assert jobs.heartbeat_lease(job_id, worker) is False
    assert jobs.claim_lease(job_id, "replacement-worker") is None
    assert jobs.complete_callback(
        job_id, "complete", result_env={"ok": True}, worker_id=worker,
        provenance={"attempt": 1, "execution_path": "local"},
    ) == "not_owner"
    closed = jobs.get_job(job_id)
    assert closed is not None
    assert closed["status"] == "running"
    assert closed["progress"] == jobs.CLOSED_PROGRESS
    assert closed["lease"] is None
    assert jobs._reap_orphans_once() >= 1
    terminal = jobs.get_job(job_id)
    assert terminal is not None and terminal["status"] == "failed"


def test_close_and_completion_race_has_one_terminal_path(postgres_authority):
    tenant = "pg-close-race-" + uuid.uuid4().hex
    job_id = _submit(
        tenant, project_id="project-" + uuid.uuid4().hex,
        key="close-race-" + uuid.uuid4().hex,
    )
    worker = "close-race-worker"
    assert jobs.claim_lease(job_id, worker) == 1
    barrier = threading.Barrier(2)

    def close():
        barrier.wait(timeout=10)
        return jobs.mark_job_closed(job_id)

    def complete():
        barrier.wait(timeout=10)
        return jobs.complete_callback(
            job_id, "complete", result_env={"ok": True}, worker_id=worker,
            provenance={"attempt": 1, "execution_path": "local"},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        close_future = pool.submit(close)
        complete_future = pool.submit(complete)
        closed, outcome = close_future.result(), complete_future.result()
    assert (closed, outcome) in {(False, "applied"), (True, "not_owner")}
    if outcome == "not_owner":
        assert jobs._reap_orphans_once() >= 1
    record = jobs.get_job(job_id)
    assert record is not None and record["status"] in {"complete", "failed"}


def test_contradictory_terminal_callbacks_keep_one_result_and_conflict_evidence(
    postgres_authority,
):
    db, _ = postgres_authority
    tenant = "pg-terminal-" + uuid.uuid4().hex
    job_id = _submit(
        tenant, project_id="project-" + uuid.uuid4().hex,
        key="terminal-" + uuid.uuid4().hex,
    )
    worker = "pg-terminal-worker"
    assert jobs.claim_lease(job_id, worker) == 1
    barrier = threading.Barrier(2)

    def finish(label: str) -> str:
        barrier.wait(timeout=10)
        return jobs.complete_callback(
            job_id, "complete",
            result_env={"ok": True, "winner": label},
            worker_id=worker,
            provenance={
                "attempt": 1, "execution_path": "local", "delivery": label,
            },
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(finish, ("a", "b")))
    assert sorted(outcomes) == ["applied", "conflict"]
    record = jobs.get_job(job_id)
    assert record is not None and record["status"] == "complete"
    assert record["result"]["winner"] in {"a", "b"}
    with db.cursor() as cur:
        cur.execute(
            "SELECT evidence_json FROM async_job_terminal_conflicts WHERE job_id = %s",
            (job_id,),
        )
        conflicts = cur.fetchall()
    assert len(conflicts) == 1
    assert conflicts[0]["evidence_json"]["result"]["winner"] != record["result"]["winner"]


def test_callback_nonce_replay_is_unique_across_connections_and_expires(
    postgres_authority,
):
    db, _ = postgres_authority
    job_id = "callback-" + uuid.uuid4().hex
    nonce = "nonce-" + uuid.uuid4().hex
    first = PostgresCallbackReplayStore()
    second = PostgresCallbackReplayStore()
    first.ensure_ready()
    second.ensure_ready()
    barrier = threading.Barrier(2)

    def consume(store: PostgresCallbackReplayStore) -> bool:
        barrier.wait(timeout=10)
        return store.consume(job_id, nonce, 1100.0, 1000.0)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(consume, (first, second)))
    assert sorted(outcomes) == [False, True]

    # Expired rows are removed before a new stable operation key is consumed.
    replacement = "nonce-" + uuid.uuid4().hex
    assert first.consume(job_id, replacement, 1001.0, 1000.0) is True
    assert second.consume(job_id, replacement, 1200.0, 1002.0) is True
    with db.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS count FROM callback_consumed_nonces "
            "WHERE job_id = %s AND nonce = %s",
            (job_id, replacement),
        )
        assert int(cur.fetchone()["count"]) == 1


def test_signed_callback_uses_postgres_replay_authority(postgres_authority, monkeypatch):
    monkeypatch.setenv("LEAF_CALLBACK_SECRET", "postgres-callback-secret")
    callbacks = broker._get_callbacks()
    assert callbacks is not None
    body = json.dumps({
        "job_id": "signed-" + uuid.uuid4().hex, "status": "success",
    }).encode()
    timestamp = str(time.time())
    nonce = "signed-nonce-" + uuid.uuid4().hex
    signature = callbacks.sign_payload(body, timestamp, nonce)
    assert callbacks.consume_callback(body, signature, timestamp, nonce)["ok"] is True
    assert callbacks.consume_callback(body, signature, timestamp, nonce) == {
        "ok": False, "reason": "replay",
    }


def test_broker_callback_process_completes_app_postgres_job(postgres_authority):
    tenant = "pg-broker-callback-" + uuid.uuid4().hex
    job_id = jobs.submit_job(
        tenant, TOOL, {}, "demo", True,
        project_id="project-" + uuid.uuid4().hex,
        idempotency_key="broker-callback-" + uuid.uuid4().hex,
        authority_mode="postgres_canonical",
    )
    assert jobs.claim_lease(job_id, "callback-owner") == 1
    secret = "process-callback-secret"
    nonce = "process-callback-" + uuid.uuid4().hex
    script = r"""
import json, os, sys, time
sys.path.insert(0, sys.argv[1])
os.environ["LEAF_JOBS_STORE"] = "postgres"
os.environ["LEAF_CALLBACK_REPLAY_STORE"] = "postgres"
os.environ["LEAF_CALLBACK_SECRET"] = sys.argv[3]
import broker
from fastapi.testclient import TestClient
callbacks = broker._get_callbacks()
body = json.dumps({"job_id": sys.argv[2], "status": "success",
                   "result": {"source": "broker-process"}}).encode()
timestamp = str(time.time())
signature = callbacks.sign_payload(body, timestamp, sys.argv[4])
with TestClient(broker.app) as client:
    response = client.post("/da/callback", content=body, headers={
        "X-Leaf-Signature": signature,
        "X-Leaf-Timestamp": timestamp,
        "X-Leaf-Nonce": sys.argv[4],
    })
print(response.status_code)
print(response.json()["completion"])
"""
    environment = dict(os.environ)
    environment.update({
        "LEAF_JOBS_STORE": "postgres",
        "LEAF_CALLBACK_REPLAY_STORE": "postgres",
        "LEAF_CALLBACK_SECRET": secret,
    })
    process = subprocess.run(
        [sys.executable, "-c", script, str(SERVER_DIR), job_id, secret, nonce],
        capture_output=True, text=True, timeout=45, env=environment,
    )
    assert process.returncode == 0, process.stderr
    assert process.stdout.strip().splitlines()[-2:] == ["200", "applied"]
    record = jobs.get_job(job_id)
    assert record is not None
    assert record["status"] == "complete"
    assert record["result"]["source"] == "broker-process"
