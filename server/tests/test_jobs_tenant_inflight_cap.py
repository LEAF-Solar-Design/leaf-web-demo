"""F12: one tenant cannot fill the shared async-job lanes, even without APS."""
from __future__ import annotations

import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import jobs  # noqa: E402


TOOL = {"name": "inflight-test", "engine_op": "count_by_layer",
        "capabilities": ["drawing.read"], "params": {}}


class QueuedExecutor:
    def submit(self, *args, **kwargs):
        # No worker is started or abandoned: every job stays submitted.
        pass


@pytest.fixture
def queued(monkeypatch):
    monkeypatch.delenv("LEAF_TENANT_MAX_INFLIGHT", raising=False)
    monkeypatch.setattr(jobs, "_reaper_started", True)
    monkeypatch.setattr(jobs, "_executors", {
        jobs.LANE_FAST: QueuedExecutor(), jobs.LANE_SLOW: QueuedExecutor(),
    })
    monkeypatch.setattr(jobs.platform_link, "on_submit", lambda *a, **k: None)


@pytest.fixture
def sqlite_store(queued, monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_JOBS_STORE", "legacy")
    monkeypatch.setattr(jobs, "DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(jobs, "_conn", None)
    yield
    jobs.reset_connection()


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, queued, monkeypatch):
    if request.param == "sqlite":
        request.getfixturevalue("sqlite_store")
        yield
        return
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL is required for PostgreSQL job tests")
    from job_pg_store import _db

    db = _db()
    db.apply_migration()
    monkeypatch.setenv("LEAF_JOBS_STORE", "postgres")
    tenants = []
    yield tenants
    # Scope cleanup to this test's tenants, never global counts or deletions.
    with db.transaction() as conn:
        for tenant in tenants:
            conn.execute("DELETE FROM async_jobs WHERE tenant_id = %s", (tenant,))


def tenant(store=None):
    value = "inflight-" + uuid.uuid4().hex
    if store is not None:
        store.append(value)
    return value


def submit(owner, **kwargs):
    return jobs.submit_job(owner, TOOL, {}, "demo", aps_live=False, **kwargs)


def assert_capped(owner, limit):
    with pytest.raises(jobs.TenantInflightCapExceeded) as caught:
        submit(owner)
    assert caught.value.tenant_id == owner
    assert caught.value.limit == limit
    assert caught.value.in_flight == limit


def test_default_cap_is_per_tenant(sqlite_store):
    owner = tenant()
    ids = [submit(owner) for _ in range(32)]
    assert len(set(ids)) == 32
    assert_capped(owner, 32)
    assert jobs.get_job(submit(tenant()))["status"] == "submitted"


@pytest.mark.parametrize("status", ["complete", "failed"])
def test_terminal_job_frees_a_slot(sqlite_store, status):
    owner = tenant()
    ids = [submit(owner) for _ in range(32)]
    jobs._exec("UPDATE jobs SET status = ? WHERE job_id = ?", (status, ids[0]))
    assert submit(owner) not in ids
    assert_capped(owner, 32)


@pytest.mark.parametrize("raw,limit", [
    ("2", 2), ("", 32), ("   ", 32), ("0", 32), ("-1", 32),
    ("abc", 32), ("1e3", 32), ("1001", 32), ("1_0", 32),
])
def test_config_never_disables_cap(sqlite_store, monkeypatch, raw, limit):
    monkeypatch.setenv("LEAF_TENANT_MAX_INFLIGHT", raw)
    owner = tenant()
    for _ in range(limit):
        submit(owner)
    assert_capped(owner, limit)


def test_invalid_config_logs_once(monkeypatch, caplog):
    monkeypatch.setattr(jobs, "_tenant_cap_warned", False)
    monkeypatch.setenv("LEAF_TENANT_MAX_INFLIGHT", "abc")
    for _ in range(3):
        assert jobs.tenant_max_inflight() == 32
    assert sum("Invalid LEAF_TENANT_MAX_INFLIGHT" in r.message
               for r in caplog.records) == 1


def test_concurrent_submissions_respect_cap(store, monkeypatch):
    monkeypatch.setenv("LEAF_TENANT_MAX_INFLIGHT", "4")
    owner = tenant(store)
    barrier = threading.Barrier(16)

    def contender(_):
        barrier.wait(timeout=30)
        try:
            return submit(owner)
        except jobs.TenantInflightCapExceeded as exc:
            assert (exc.tenant_id, exc.limit, exc.in_flight) == (owner, 4, 4)
            return None

    with ThreadPoolExecutor(max_workers=16) as pool:
        ids = list(pool.map(contender, range(16)))
    assert len([job_id for job_id in ids if job_id is not None]) == 4
    assert len(jobs.list_jobs(owner)) == 4
    assert all(rec["status"] == "submitted" for rec in jobs.list_jobs(owner))


def test_concurrent_idempotent_replay_at_cap(store, monkeypatch):
    monkeypatch.setenv("LEAF_TENANT_MAX_INFLIGHT", "2")
    owner = tenant(store)
    context = {"project_id": "project-" + uuid.uuid4().hex, "idempotency_key": "same"}
    original = submit(owner, **context)
    running = submit(owner)
    assert jobs.claim_lease(running, "test-worker") == 1
    assert_capped(owner, 2)
    barrier = threading.Barrier(16)

    def replay(_):
        barrier.wait(timeout=30)
        return submit(owner, **context)

    with ThreadPoolExecutor(max_workers=16) as pool:
        assert list(pool.map(replay, range(16))) == [original] * 16
    assert len(jobs.list_jobs(owner)) == 2
    with pytest.raises(ValueError, match="different run input"):
        jobs.submit_job(owner, TOOL, {"changed": True}, "demo", False, **context)


def test_plan_jobs_share_the_cap(sqlite_store, monkeypatch):
    monkeypatch.setenv("LEAF_TENANT_MAX_INFLIGHT", "1")
    owner = tenant()
    jobs.submit_plan_job(owner, {"parent_version": 1}, "demo",
                         checkout_holder=None, checkout_fence=None)
    assert_capped(owner, 1)
    with pytest.raises(jobs.TenantInflightCapExceeded):
        jobs.submit_plan_job(owner, {"parent_version": 1}, "demo",
                             checkout_holder=None, checkout_fence=None)


@pytest.mark.parametrize("wait", [0, 1])
def test_router_returns_retryable_429(sqlite_store, monkeypatch, wait):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import deps
    from routers import jobs as route

    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    monkeypatch.setenv("LEAF_TENANT_MAX_INFLIGHT", "2")
    monkeypatch.setattr(deps, "APS_LIVE", False)
    monkeypatch.setattr(deps, "find_tool", lambda *a: TOOL)
    monkeypatch.setattr(deps, "effective_tools_with_provenance", lambda *a: [])
    monkeypatch.setattr(deps, "load_engine_registry_tools", lambda: [])
    monkeypatch.setattr(route, "_checkout_identity", lambda *a: ("anonymous", None))
    app = FastAPI()
    app.include_router(route.router)
    owner = tenant()
    payload = {"tool": TOOL["name"], "params": {}, "dwg": "demo",
               "catalog_digest": deps.catalog_tool_view(TOOL)["catalog_digest"]}
    with TestClient(app) as client:
        for _ in range(2):
            response = client.post("/api/run", headers={"X-Tenant-Id": owner}, json=payload)
            assert response.status_code == 202, response.text
        response = client.post(f"/api/run?wait={wait}",
                               headers={"X-Tenant-Id": owner}, json=payload)
        assert response.status_code == 429, response.text
        body = response.json()
        assert body["error"]["error_code"] == "quota_exceeded"
        assert body["error"]["retryable"] is True
        assert body["quota_kind"] == "tenant_inflight"
        assert (body["limit"], body["used"]) == (2, 2)
        assert "2 runs queued or running" in body["error"]["message"]
        assert "retry when one finishes" in body["error"]["message"]
        response = client.post("/api/run", headers={"X-Tenant-Id": tenant()}, json=payload)
        assert response.status_code == 202, response.text
