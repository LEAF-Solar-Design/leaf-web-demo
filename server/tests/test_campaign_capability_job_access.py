"""Campaign host and completion job surfaces enforce current project membership."""
import asyncio
from copy import deepcopy
import json
import uuid

import pytest

import deps
import jobs
import campaign_transform_job as transform
from routers import jobs as routes
from test_campaign_capability_job import context


def completion_context():
    return transform.validate_context({
        **transform.CONSTANTS, **{key: str(uuid.uuid4()) for key in transform.IDS},
        "tenant_id": "tenant-transform", "contract_version": 1,
        "change_set_id": "transform-change", "catalog_commit": "a" * 40,
        "effective_catalog_digest": "b" * 64, "tool_manifest_sha256": "sha256:" + "c" * 64,
        "tool_source_sha256": "d" * 64, "input_sha256": "e" * 64,
    })


def provenance_key(rec):
    return "completion_provenance" if "completion_provenance" in rec else "capability_provenance"


@pytest.fixture(params=["capability_provenance", "completion_provenance"])
def access(monkeypatch, request):
    ctx = completion_context() if request.param == "completion_provenance" else context()
    ctx["tenant_id"] = ctx["org_id"]
    tenant = deps.TenantContext(ctx["tenant_id"], subject="auth0|reader")
    rec = {"job_id": str(uuid.uuid4()), "tenant_id": ctx["tenant_id"],
           "org_id": ctx["org_id"], "project_id": ctx["project_id"],
           request.param: ctx, "tool": ctx["tool_name"], "status": "running", "progress": "host",
           "elapsed_ms": 0, "error": None, "result": None}
    state = {"read": True, "write": False, "missing": False, "closed": [], "allowed_project": ctx["project_id"]}
    calls = []

    def require(caller, project, *, write):
        assert caller is tenant
        assert project == rec["project_id"]
        calls.append(write)
        if state["missing"]:
            raise LookupError("missing project")
        if not state["read"] or project != state["allowed_project"] or (write and not state["write"]):
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
    key = provenance_key(rec)
    assert routes.get_job(rec["job_id"], tenant)[key] == rec[key]
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
    del rec[provenance_key(rec)]
    state["read"] = False
    assert routes.get_job(rec["job_id"], tenant)["job_id"] == rec["job_id"]
    assert routes.close_job(rec["job_id"], tenant)["closed"]
    assert len(routes.list_jobs(20, tenant)["jobs"]) == 1
    assert not calls


def test_canonical_mirror_uses_stored_spine_for_access(access, monkeypatch):
    tenant, rec, state, _ = access
    canonical_id = str(uuid.uuid4())
    canonical = {**rec, "job_id": canonical_id}
    del canonical[provenance_key(canonical)]

    class Store:
        def linked_capability_job(self, jid, tenant_id):
            return deepcopy(rec) if jid == canonical_id and tenant_id == str(tenant) else None

    monkeypatch.setattr(jobs, "job_store_mode", lambda: "postgres")
    monkeypatch.setattr(jobs, "_pg_store", Store())
    monkeypatch.setattr(jobs.platform_link, "list_canonical_jobs", lambda *a, **k: [canonical])
    assert routes.get_job(canonical_id, tenant)["job_id"] == rec["job_id"]
    assert len(routes.list_jobs(20, tenant)["jobs"]) == 1
    assert routes.close_job(canonical_id, tenant).status_code == 403
    state["write"] = True
    assert routes.close_job(canonical_id, tenant)["closed"] is True
    assert state["closed"] == [rec["job_id"]]
    state["read"] = False
    assert routes.get_job(canonical_id, tenant).status_code == 403
    assert routes.list_jobs(20, tenant)["jobs"] == []
    assert asyncio.run(routes.stream_job(canonical_id, tenant)).status_code == 403


def test_same_org_other_project_denies_all_routes(access):
    tenant, rec, state, calls = access
    other_project = str(uuid.uuid4())
    rec["project_id"] = other_project
    rec[provenance_key(rec)]["project_id"] = other_project
    assert rec["org_id"] == str(tenant)
    assert routes.get_job(rec["job_id"], tenant).status_code == 403
    assert routes.list_jobs(20, tenant)["jobs"] == []
    assert asyncio.run(routes.stream_job(rec["job_id"], tenant)).status_code == 403
    assert routes.close_job(rec["job_id"], tenant).status_code == 403
    assert calls == [False, False, False, True]
    assert not state["closed"]


@pytest.mark.parametrize("malformed", [None, {}, "not-a-context", {"schema": "unknown"}])
def test_malformed_context_does_not_disclose_or_close(access, malformed):
    tenant, rec, state, calls = access
    rec[provenance_key(rec)] = malformed
    assert routes.get_job(rec["job_id"], tenant).status_code == 404
    assert routes.list_jobs(20, tenant)["jobs"] == []
    assert asyncio.run(routes.stream_job(rec["job_id"], tenant)).status_code == 404
    assert routes.close_job(rec["job_id"], tenant).status_code == 404
    assert not calls and not state["closed"]


def test_conflicting_provenance_kinds_are_rejected(access):
    tenant, rec, state, calls = access
    rec["capability_provenance"] = context()
    rec["completion_provenance"] = completion_context()
    assert routes.get_job(rec["job_id"], tenant).status_code == 404
    assert routes.list_jobs(20, tenant)["jobs"] == []
    assert asyncio.run(routes.stream_job(rec["job_id"], tenant)).status_code == 404
    assert routes.close_job(rec["job_id"], tenant).status_code == 404
    assert not calls and not state["closed"]


@pytest.mark.parametrize("field", ["tenant_id", "org_id", "project_id", "tool_name"])
def test_context_and_stored_row_scope_must_match(access, field):
    tenant, rec, state, calls = access
    if field == "tool_name":
        rec["tool"] = "different-published-tool"
    else:
        rec[provenance_key(rec)][field] = str(uuid.uuid4())
    assert routes.get_job(rec["job_id"], tenant).status_code == 404
    assert routes.list_jobs(20, tenant)["jobs"] == []
    assert asyncio.run(routes.stream_job(rec["job_id"], tenant)).status_code == 404
    assert routes.close_job(rec["job_id"], tenant).status_code == 404
    assert not calls and not state["closed"]
