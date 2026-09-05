"""Authenticated registration, immutable reads and canonical cancellation seams.

Uses a transaction fake, never a developer or production database. SQL still
runs through the production service and existing role/terminal transition code.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import copy
from datetime import datetime, timezone
import json
import hashlib
from threading import RLock
from types import SimpleNamespace
import uuid

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
import pytest

import platform_link
from solver_adapters import arlo_design

platform_link._load_platform()
from leaf_platform import api, arlo_lab, canonical_jobs

ORG, PROJECT, BINDING = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


class MemoryTransactions:
    def __init__(self):
        self.inputs, self.versions, self.jobs = {}, {}, {}
        self.role, self.live, self.authority = "owner", True, "postgres_canonical"
        self.lock = RLock()
        self.sql = []

    @contextmanager
    def connection(self):
        with self.lock:
            yield self

    def transaction(self, operation, **kwargs):
        with self.lock:
            saved = copy.deepcopy((self.inputs, self.versions, self.jobs))
            try:
                return operation(self)
            except Exception:
                self.inputs, self.versions, self.jobs = saved
                raise

    @contextmanager
    def cursor(self):
        yield MemoryCursor(self)


class MemoryCursor:
    def __init__(self, store):
        self.store, self.row = store, None

    def execute(self, sql, params):
        self.store.sql.append(sql)
        self.row = None
        org = params.get("org", params.get("org_id"))
        project = params.get("project", params.get("project_id"))
        allowed = org == ORG and project == PROJECT and self.store.live
        if sql.startswith("SELECT project_id, org_id"):
            self.row = {"org_id": ORG, "project_id": PROJECT} if allowed else None
        elif sql.startswith("SELECT role FROM identity_bindings"):
            self.row = {"role": self.store.role} if org == ORG else None
        elif sql.startswith("SELECT role FROM project_member_bindings"):
            self.row = {"role": self.store.role} if allowed and self.store.role else None
        elif sql.startswith("SELECT COALESCE"):
            self.row = {"authority_mode": self.store.authority}
        elif sql.startswith("SELECT v.import_fingerprint"):
            version = next((v for v in self.store.versions.values()
                            if v["idempotency_key"] == params["key"] and not v.get("deleted")), None)
            if version:
                self.row = {**version, **self.store.inputs[version["input_version_id"]]}
        elif sql.startswith("INSERT INTO drawing_artifacts"):
            pass
        elif sql.startswith("INSERT INTO drawing_versions"):
            self.store.versions[params["version"]] = {
                "input_version_id": params["version"], "org_id": org, "project_id": project,
                "idempotency_key": params["key"], "import_fingerprint": params["fingerprint"],
                "provenance": params["provenance"].obj, "intake_ref": params["ref"], "oss_object": None}
        elif sql.startswith("INSERT INTO arlo_lab_inputs"):
            self.row = {"input_version_id": params["version"], "org_id": org,
                        "project_id": project, "example_id": params["example"],
                        "example_version": params["example_version"], "input_sha256": params["digest"],
                        "request_json": params["request"]}
            self.store.inputs[params["version"]] = dict(self.row)
        elif sql.startswith("SELECT i.*"):
            entry = self.store.inputs.get(params["version"])
            version = self.store.versions.get(params["version"])
            if allowed and entry and version and not version.get("deleted"):
                self.row = {**version, **entry}
        elif sql.startswith("SELECT * FROM jobs"):
            self.row = self.store.jobs.get(params.get("job", params.get("job_id")))
            if "tool_name = 'arlo-design'" in sql and (not allowed or
                    self.row is None or self.row["tool_name"] != "arlo-design"):
                self.row = None
        elif sql.startswith("UPDATE jobs SET status='cancelled'"):
            self.row = self.store.jobs[params["job"]]
            if self.row["status"] in {"running", "queued"}:
                self.row.update(status="cancelled", error=params["error"].obj,
                    provenance=params["provenance"].obj, lease_owner=None, lease_expires_at=None,
                    terminal_fingerprint=params["fingerprint"])
        elif sql.startswith("UPDATE jobs SET terminal_conflict"):
            self.store.jobs[params["job_id"]]["terminal_conflict"] = params["conflict"].obj
        else:
            raise AssertionError(f"unexpected SQL: {sql}")

    def fetchone(self):
        return copy.deepcopy(self.row)


@pytest.fixture
def store(monkeypatch):
    store = MemoryTransactions()
    monkeypatch.setattr(arlo_lab, "run_transaction", store.transaction)
    monkeypatch.setattr(arlo_lab, "connection", store.connection)
    monkeypatch.setattr(canonical_jobs, "connection", store.connection)
    return store


@pytest.fixture
def client(store, monkeypatch):
    monkeypatch.setattr(api.platform_deps, "auth_live", lambda: True)
    def identity(authorization):
        if authorization != "Bearer verified-session":
            raise HTTPException(401, "authenticated platform session required")
        return SimpleNamespace(platform_tenant_id=ORG, binding_id=BINDING)
    monkeypatch.setattr(api.platform_deps, "_verified_identity", identity)
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app)


def headers(key="input-1"):
    return {"Authorization": "Bearer verified-session", "Idempotency-Key": key,
            "X-Org-Id": str(uuid.uuid4())}


def endpoint(project=PROJECT):
    return f"/api/projects/{project}/arlo-examples/feeder-lab-v1/inputs"


def register(key="input-1"):
    return arlo_lab.register_input(ORG, PROJECT, BINDING, example_id="feeder-lab-v1",
                                  example_version="1", idempotency_key=key)


def context(response):
    return {"org_id": ORG, "project_id": PROJECT, "input_version_id": response["input_version_id"]}


def test_registration_replays_exact_immutable_input_and_ignores_org_header(client, store):
    first = client.post(endpoint(), headers=headers(), json={"example_version": "1"})
    second = client.post(endpoint(), headers=headers(), json={"example_version": "1"})
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    response = first.json()
    assert response["organization_id"] == str(ORG)
    assert response["request"]["organization_id"] == "example-org"
    assert arlo_lab.digest(response["request"]) == response["input_sha256"]
    assert json.loads(response["request_canonical_json"]) == response["request"]
    assert hashlib.sha256(response["request_canonical_json"].encode()).hexdigest() == response["input_sha256"]
    assert '"timeout_seconds":180.0' in response["request_canonical_json"]
    assert len(store.inputs) == len(store.versions) == 1
    version = next(iter(store.versions.values()))
    assert version["oss_object"] is None
    assert version["provenance"]["native_verified"] is False
    assert arlo_lab.load_registered_request(context(response), response["request"]) == response["request"]


def test_concurrent_duplicate_registration_returns_one_input(store):
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda _: register(), range(2)))
    assert responses[0] == responses[1] and len(store.inputs) == 1


def test_same_key_changed_source_conflicts_without_overwrite(store, monkeypatch):
    first = register()
    changed = copy.deepcopy(first["request"])
    changed["seed"] = 2
    monkeypatch.setattr(arlo_lab, "load_example", lambda *_: changed)
    with pytest.raises(arlo_lab.lifecycle.LifecycleConflict, match="different content"):
        register()
    assert len(store.inputs) == 1


@pytest.mark.parametrize("case", ["unauthenticated", "foreign_project", "revoked", "read_only"])
def test_registration_authority_refuses_before_storage(client, store, case):
    auth, project = headers(), PROJECT
    if case == "unauthenticated":
        auth.pop("Authorization")
    elif case == "foreign_project":
        project = uuid.uuid4()
    else:
        store.role = None if case == "revoked" else "read_only"
    response = client.post(endpoint(project), headers=auth, json={"example_version": "1"})
    assert response.status_code in (401, 403, 404)
    assert not store.inputs


def test_registration_rejects_client_content_and_unknown_example(client, store):
    assert client.post(endpoint(), headers=headers(), json={"example_version": "1", "request": {}}).status_code == 422
    assert client.post(endpoint(), headers=headers(), json={"example_version": "2"}).status_code == 404
    assert not store.inputs


@pytest.mark.parametrize("mutation", ["params", "digest", "provenance", "deleted", "project"])
def test_worker_refuses_mutation_and_stale_input(store, mutation):
    response = register()
    ctx, params = context(response), copy.deepcopy(response["request"])
    version = uuid.UUID(response["input_version_id"])
    if mutation == "params":
        params["seed"] = 1
    elif mutation == "digest":
        store.inputs[version]["request_json"] = "{}"
    elif mutation == "provenance":
        store.versions[version]["provenance"]["contract"] = "dwg"
    elif mutation == "deleted":
        store.versions[version]["deleted"] = True
    else:
        ctx["project_id"] = uuid.uuid4()
    with pytest.raises((ValueError, arlo_lab.lifecycle.LifecycleConflict)):
        arlo_lab.load_registered_request(ctx, params)


def test_durable_adapter_loads_registered_bytes_before_source_process(store, monkeypatch):
    response = register()
    params = dict(response["request"], seed=3)
    with pytest.raises(ValueError, match="registered immutable input"):
        arlo_design.run(params, job_context={**context(response), "job_id": str(uuid.uuid4())})


def job(store, status="running", tool="arlo-design"):
    job_id = uuid.uuid4()
    store.jobs[job_id] = {"job_id": job_id, "org_id": ORG, "project_id": PROJECT,
        "status": status, "tool_name": tool, "params": {}, "attempt": 1,
        "lease_owner": "worker", "lease_expires_at": datetime.now(timezone.utc),
        "terminal_fingerprint": None, "error": None, "result": None}
    return job_id


@pytest.mark.parametrize("status", ["queued", "running", "succeeded", "failed", "cancelled"])
def test_cancel_is_idempotent_and_preserves_terminal_jobs(client, store, status):
    job_id = job(store, status)
    url = f"/api/projects/{PROJECT}/arlo-jobs/{job_id}/cancel"
    first, second = client.post(url, headers=headers()), client.post(url, headers=headers())
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    expected = "cancelled" if status in ("queued", "running") else status
    assert first.json()["job"]["status"] == expected
    if status in ("queued", "running"):
        assert store.jobs[job_id]["lease_owner"] is None


def test_cancellation_fences_racing_worker_failure_and_success(store):
    job_id = job(store)
    arlo_lab.job_view(ORG, PROJECT, BINDING, job_id, cancel=True)
    failure = canonical_jobs.fail_or_retry(job_id, "worker", {"message": "late failure"}, {"attempt": 1})
    result = {"solver": "arlo-design", "solver_input": {}, "solver_result": {},
              "request_sha256": arlo_lab.digest({}), "input_sha256": arlo_lab.digest({}),
              "result_sha256": arlo_lab.digest({}), "solver_revision": "r", "source_sha256": "a"*64,
              "runtime": "python-test"}
    provenance = {"attempt": 1, "execution_path": "local", **{
        key: result[key] for key in ("solver_revision", "source_sha256", "runtime")}}
    completion = canonical_jobs.complete_solve(job_id, "worker", result, provenance)
    assert failure == completion == "conflict"
    assert store.jobs[job_id]["status"] == "cancelled"
    assert store.jobs[job_id]["result"] is None


def test_status_and_cancel_require_matching_project_and_arlo_tool(client, store):
    job_id = job(store, tool="string-autofill-opt")
    assert client.get(f"/api/projects/{PROJECT}/arlo-jobs/{job_id}", headers=headers()).status_code == 404
    job_id = job(store)
    assert client.get(f"/api/projects/{uuid.uuid4()}/arlo-jobs/{job_id}", headers=headers()).status_code == 404
    store.role = "read_only"
    url = f"/api/projects/{PROJECT}/arlo-jobs/{job_id}"
    assert client.get(url, headers=headers()).status_code == 200
    assert client.post(url + "/cancel", headers=headers()).status_code == 403


def test_run_refuses_nonmember_before_canonical_job_submission(monkeypatch):
    from routers import jobs as route
    tool = {"name": "arlo-design", "canonical_only": True,
            "capabilities": ["solve"], "default_params": {}}
    monkeypatch.setattr(route.deps, "find_tool", lambda *_: tool)
    monkeypatch.setattr(route.jobs.platform_link, "resolve_submission_context", lambda *_: {
        "org_id": ORG, "project_id": PROJECT, "authority_mode": "postgres_canonical"})
    monkeypatch.setattr(route.jobs.platform_link, "require_project_access",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(platform_link.ProjectSessionForbidden("revoked")))
    monkeypatch.setattr(route.jobs.platform_link, "submit_canonical_solve",
        lambda *_: pytest.fail("nonmember submitted a canonical job"))
    response = route.run(route.RunRequest(tool="arlo-design", dwg=str(uuid.uuid4()),
        params={}, catalog_digest=route.deps.catalog_tool_digest(tool)),
        tenant_id=str(ORG), x_org_id=str(ORG), x_project_id=str(PROJECT),
        idempotency_key="run-1", authorization="Bearer verified")
    assert response.status_code == 403


def test_job_proof_strings_preserve_python_numbers_and_bind_actual_objects(client, store):
    job_id = job(store, "succeeded")
    params, solver_input, solver_result = {"cost": 1.0}, {"offset": 1e-7}, {"cost": 2.0}
    store.jobs[job_id].update(params=params, result={"solver_input": solver_input,
        "solver_result": solver_result, "request_sha256": arlo_lab.digest(params),
        "input_sha256": arlo_lab.digest(solver_input), "result_sha256": arlo_lab.digest(solver_result)})
    response = client.get(f"/api/projects/{PROJECT}/arlo-jobs/{job_id}", headers=headers())
    view = response.json()["job"]
    for name, value, hash_name in (("request", params, "request_sha256"),
            ("solver_input", solver_input, "input_sha256"),
            ("solver_result", solver_result, "result_sha256")):
        proof = view["canonical_json"][name]
        assert json.loads(proof) == value
        assert hashlib.sha256(proof.encode()).hexdigest() == view["result"][hash_name]
    assert view["canonical_json"]["request"] == '{"cost":1.0}'
    assert view["canonical_json"]["solver_input"] == '{"offset":1e-07}'
    assert hashlib.sha256(b'{"cost":1}').hexdigest() != view["result"]["request_sha256"]
