"""Existing job surfaces enforce the ReciPDF job's stored project membership."""
import asyncio
from copy import deepcopy
import json
import uuid

import pytest

import deps
import jobs
from routers import jobs as routes
from test_campaign_capability_job import context


@pytest.fixture
def access(monkeypatch):
    ctx = context()
    ctx["tenant_id"] = ctx["org_id"]
    tenant = deps.TenantContext(ctx["tenant_id"], subject="auth0|reader")
    rec = {"job_id": str(uuid.uuid4()), "tenant_id": ctx["tenant_id"],
           "org_id": ctx["org_id"], "project_id": ctx["project_id"],
           "capability_provenance": ctx, "status": "running", "progress": "host",
           "elapsed_ms": 0, "error": None, "result": None}
    state = {"read": True, "write": False, "missing": False, "closed": []}
    calls = []

    def require(caller, project, *, write):
        assert caller is tenant
        assert project == rec["project_id"]
        calls.append(write)
        if state["missing"]:
            raise LookupError("missing project")
        if not state["read"] or (write and not state["write"]):
            raise jobs.platform_link.ProjectSessionForbidden("revoked")
        return rec["org_id"]

    monkeypatch.setattr(routes, "_bound_tenant_id", lambda caller: str(caller))
    monkeypatch.setattr(jobs, "job_store_mode", lambda: "legacy")
    monkeypatch.setattr(jobs, "get_job", lambda jid: deepcopy(rec) if jid == rec["job_id"] else None)
    monkeypatch.setattr(jobs, "list_jobs", lambda tenant, limit: [deepcopy(rec)])
    monkeypatch.setattr(jobs.platform_link, "list_canonical_jobs", lambda *a, **k: [])
    monkeypatch.setattr(jobs.platform_link, "get_canonical_job", lambda *a: None)
    monkeypatch.setattr(jobs.platform_link, "require_project_access", require)
    monkeypatch.setattr(jobs, "mark_job_closed", lambda jid: state["closed"].append(jid) or True)
    return tenant, rec, state, calls


def test_reader_get_list_and_viewer_close_denial(access):
    tenant, rec, state, calls = access
    assert routes.get_job(rec["job_id"], tenant)["capability_provenance"] == rec["capability_provenance"]
    assert len(routes.list_jobs(20, tenant)["jobs"]) == 1
    assert routes.close_job(rec["job_id"], tenant).status_code == 403
    assert not state["closed"]
    state["write"] = True
    assert routes.close_job(rec["job_id"], tenant)["closed"] is True
    assert calls == [False, False, True, True]


def test_revoked_and_missing_project_shapes(access):
    tenant, rec, state, _ = access
    state["read"] = False
    assert routes.get_job(rec["job_id"], tenant).status_code == 403
    assert routes.list_jobs(20, tenant)["jobs"] == []
    assert asyncio.run(routes.stream_job(rec["job_id"], tenant)).status_code == 403
    state["missing"] = True
    assert routes.get_job(rec["job_id"], tenant).status_code == 404
    assert routes.close_job(rec["job_id"], tenant).status_code == 404


def test_foreign_tenant_and_project_do_not_disclose(access):
    tenant, rec, _, calls = access
    foreign = deps.TenantContext(str(uuid.uuid4()), subject="auth0|foreign")
    assert routes.get_job(rec["job_id"], foreign).status_code == 404
    assert routes.close_job(rec["job_id"], foreign).status_code == 404
    assert asyncio.run(routes.stream_job(rec["job_id"], foreign)).status_code == 404
    rec["project_id"] = str(uuid.uuid4())
    assert routes.get_job(rec["job_id"], tenant).status_code == 404
    assert not calls


def test_stream_rechecks_access_before_each_payload(access):
    tenant, rec, state, calls = access

    async def consume():
        response = await routes.stream_job(rec["job_id"], tenant)
        first = await anext(response.body_iterator)
        assert json.loads(first.removeprefix("data: "))["status"] == "running"
        state["read"] = False
        second = await anext(response.body_iterator)
        assert json.loads(second.removeprefix("data: ")) == {"job_id": rec["job_id"], "status": "unknown"}
        with pytest.raises(StopAsyncIteration):
            await anext(response.body_iterator)

    asyncio.run(consume())
    assert len(calls) == 3


def test_ordinary_jobs_keep_existing_access_behavior(access):
    tenant, rec, state, calls = access
    del rec["capability_provenance"]
    state["read"] = False
    assert routes.get_job(rec["job_id"], tenant)["job_id"] == rec["job_id"]
    assert routes.close_job(rec["job_id"], tenant)["closed"]
    assert len(routes.list_jobs(20, tenant)["jobs"]) == 1
    assert not calls


def test_canonical_mirror_uses_stored_spine_for_access(access, monkeypatch):
    tenant, rec, state, _ = access
    canonical_id = str(uuid.uuid4())
    canonical = {**rec, "job_id": canonical_id}
    del canonical["capability_provenance"]

    class Store:
        def linked_capability_job(self, jid, tenant_id):
            return deepcopy(rec) if jid == canonical_id and tenant_id == str(tenant) else None

    monkeypatch.setattr(jobs, "job_store_mode", lambda: "postgres")
    monkeypatch.setattr(jobs, "_pg_store", Store())
    monkeypatch.setattr(jobs.platform_link, "list_canonical_jobs", lambda *a, **k: [canonical])
    assert routes.get_job(canonical_id, tenant)["job_id"] == rec["job_id"]
    assert len(routes.list_jobs(20, tenant)["jobs"]) == 1
    state["read"] = False
    assert routes.get_job(canonical_id, tenant).status_code == 403
    assert routes.list_jobs(20, tenant)["jobs"] == []
