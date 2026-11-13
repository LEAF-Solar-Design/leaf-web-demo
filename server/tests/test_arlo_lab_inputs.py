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
        self.files, self.receipts = {}, {}
        self.role, self.live, self.authority = "owner", True, "postgres_canonical"
        self.lock = RLock()
        self.sql = []

    @contextmanager
    def connection(self):
        with self.lock:
            yield self

    def transaction(self, operation, **kwargs):
        with self.lock:
            saved = copy.deepcopy((self.inputs, self.versions, self.jobs, self.files, self.receipts))
            try:
                return operation(self)
            except Exception:
                self.inputs, self.versions, self.jobs, self.files, self.receipts = saved
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
        elif sql.startswith("SELECT content FROM project_files"):
            self.row = self.store.files.get((org, project, params["path"])) if allowed else None
        elif sql.startswith("INSERT INTO project_files"):
            assert params["actor"] == BINDING
            assert params["sha"] == hashlib.sha256(params["content"].encode()).hexdigest()
            self.store.files[(org, project, params["path"])] = {"content": params["content"]}
        elif sql.startswith("SELECT receipt_id"):
            self.row = self.store.receipts.get((org, project, params["action"], params["idempotency_key"]))
        elif sql.startswith("INSERT INTO project_lifecycle_receipts"):
            self.row = {**params, "result_json": params["result_json"].obj,
                        "created_at": datetime.now(timezone.utc)}
            self.store.receipts[(org, project, params["action"], params["idempotency_key"])] = self.row
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


def input_endpoint(response, action="", project=PROJECT):
    return f"/api/projects/{project}/arlo-inputs/{response['input_version_id']}{action}"


def constraint_patch(response):
    return {"requirements": {"clearance_m": response["request"]["requirements"]["clearance_m"] * 2}}


def test_revision_is_immutable_scoped_and_reopens_as_active(client, store):
    original = register()
    saved_inputs, saved_versions = copy.deepcopy((store.inputs, store.versions))
    response = client.post(input_endpoint(original, "/revisions"), headers=headers("revision"),
                           json={"patch": constraint_patch(original)})
    assert response.status_code == 201, response.text
    revised = response.json()
    assert revised["input_version_id"] != original["input_version_id"]
    assert revised["parent_id"] == revised["original_id"] == original["input_version_id"]
    assert revised["input_sha256"] == arlo_lab.digest(revised["request"]) != original["input_sha256"]
    assert revised["request"]["requirements"]["clearance_m"] == constraint_patch(original)["requirements"]["clearance_m"]
    for version in saved_inputs:
        assert store.inputs[version] == saved_inputs[version]
        assert store.versions[version] == saved_versions[version]
    replay = client.post(input_endpoint(original, "/revisions"), headers=headers("revision"),
                         json={"patch": constraint_patch(original)})
    assert replay.json() == revised and len(store.inputs) == 2
    assert client.get(input_endpoint(original), headers=headers()).json() == revised
    assert arlo_lab.load_registered_request(context(revised), revised["request"]) == revised["request"]
    bound = arlo_design._validated_input(revised["request"], context(revised))
    assert bound["organization_id"] == str(ORG)
    assert bound["project_id"] == str(PROJECT)
    assert bound["input_version_id"] == revised["input_version_id"]
    assert revised["request"]["organization_id"] == original["request"]["organization_id"]
    assert client.post(input_endpoint(original, "/revisions"), headers=headers("stale"),
                       json={"patch": constraint_patch(original)}).status_code == 409


def test_reference_revision_keeps_raw_hash_and_worker_binds_revision(client, store, monkeypatch):
    import project_reference_materials as originals
    from leaf_platform import project_reference_materials as references

    material_id = uuid.uuid4()
    request = arlo_lab.load_example("feeder-lab-v1", "1")
    raw = json.dumps(request, indent=2).encode()
    raw_sha = hashlib.sha256(raw).hexdigest()
    monkeypatch.setattr(references, "get_material", lambda *_: {
        "material_id": str(material_id), "media_type": "application/json",
        "byte_size": len(raw), "sha256": raw_sha})
    monkeypatch.setattr(originals, "read_content", lambda *_: raw)
    url = f"/api/projects/{PROJECT}/reference-files/{material_id}/arlo-input"
    original = client.post(url, headers=headers()).json()
    revised_response = client.post(input_endpoint(original, "/revisions"), headers=headers("reference-revision"),
                                    json={"patch": constraint_patch(original)})
    assert revised_response.status_code == 201, revised_response.text
    revised = revised_response.json()
    assert revised["material_id"] == str(material_id)
    assert revised["original_sha256"] == original["original_sha256"] == raw_sha
    assert revised["input_sha256"] != original["input_sha256"]
    assert arlo_lab.load_registered_request(context(revised), revised["request"]) == revised["request"]
    captured = []
    validate = arlo_design._validated_input
    def capture(params, job_context):
        body = validate(params, job_context)
        captured.append(body)
        return body
    monkeypatch.setattr(arlo_design, "_validated_input", capture)
    def stop_before_process(*_):
        raise RuntimeError("captured before source process")
    monkeypatch.setattr(arlo_design, "_root", stop_before_process)
    with pytest.raises(RuntimeError, match="captured before source process"):
        arlo_design.run(revised["request"], job_context={**context(revised), "job_id": str(uuid.uuid4())})
    assert captured[0]["organization_id"] == str(ORG)
    assert captured[0]["project_id"] == str(PROJECT)
    assert captured[0]["input_version_id"] == revised["input_version_id"]
    assert captured[0]["requirements"] == revised["request"]["requirements"]
    assert client.post(input_endpoint(revised, "/restore"), headers=headers("reference-restore")).json() == original
    assert client.post(url, headers=headers()).json() == original
    version = uuid.UUID(revised["input_version_id"])
    store.versions[version]["provenance"]["parent_id"] = str(uuid.uuid4())
    with pytest.raises(ValueError, match="reference provenance mismatch"):
        arlo_lab.load_registered_request(context(revised), revised["request"])


@pytest.mark.parametrize("patch, detail", [
    ({"requirements": {"diameter_m": -1}}, "diameter_m must be greater than zero"),
    ({"requirements": {"clearance_m": -1}}, "clearance_m must be nonnegative"),
    ({"requirements": {"max_support_span_m": 0}}, "max_support_span_m must be greater than zero"),
    ({"requirements": {"labor_rate": True}}, "labor_rate must be a finite number"),
    ({"requirements": {"diameter_m": "wide"}}, "diameter_m must be a finite number"),
    ({"requirements": {"diameter_m": 10 ** 400}}, "diameter_m must be a finite number"),
    ({"budget": {"timeout_seconds": 181}}, "budget.timeout_seconds"),
    ({"budget": {"beam_width": 1.5}}, "beam_width must be a whole number"),
    ({"requirements": {"unknown": 1}}, "not an editable numeric constraint"),
    ({"organization_id": "foreign"}, "Only requirements and budget"),
    ({"scenario": {}}, "Only requirements and budget"),
    ({}, "Only requirements and budget"),
])
def test_invalid_revision_preserves_latest_input_and_result(client, store, patch, detail):
    original = register()
    revised = client.post(input_endpoint(original, "/revisions"), headers=headers("valid"),
                          json={"patch": constraint_patch(original)}).json()
    job_id = job(store, "succeeded")
    store.jobs[job_id].update(input_version_id=uuid.UUID(revised["input_version_id"]),
                              params=revised["request"], result={"solver_result": {"proposals": []}})
    before = copy.deepcopy((store.inputs, store.versions, store.jobs, store.files, store.receipts))
    rejected = client.post(input_endpoint(revised, "/revisions"), headers=headers("invalid"), json={"patch": patch})
    assert rejected.status_code == 422, rejected.text
    assert detail in rejected.json()["detail"]
    assert (store.inputs, store.versions, store.jobs, store.files, store.receipts) == before
    assert client.get(input_endpoint(original), headers=headers()).json() == revised
    assert client.get(f"/api/projects/{PROJECT}/arlo-jobs/{job_id}", headers=headers()).json()["job"]["result"] == before[2][job_id]["result"]


def test_restore_preserves_revision_history_and_replays_without_switching_active(client, store):
    original = register()
    revised = client.post(input_endpoint(original, "/revisions"), headers=headers("revise"),
                          json={"patch": constraint_patch(original)}).json()
    second = client.post(input_endpoint(revised, "/revisions"), headers=headers("revise-again"),
                         json={"patch": constraint_patch(revised)}).json()
    assert second["parent_id"] == revised["input_version_id"]
    assert second["original_id"] == original["input_version_id"]
    before = copy.deepcopy((store.inputs, store.versions, store.jobs))
    restored = client.post(input_endpoint(second, "/restore"), headers=headers("restore"))
    assert restored.status_code == 200 and restored.json() == original
    assert client.get(input_endpoint(revised), headers=headers()).json() == original
    assert (store.inputs, store.versions, store.jobs) == before
    new_revision = client.post(input_endpoint(original, "/revisions"), headers=headers("new-revision"),
                               json={"patch": constraint_patch(original)}).json()
    assert client.post(input_endpoint(second, "/restore"), headers=headers("restore")).json() == original
    assert client.get(input_endpoint(original), headers=headers()).json() == new_revision
    assert client.post(input_endpoint(second, "/restore"), headers=headers("stale-restore")).status_code == 409


def test_revising_back_to_original_constraints_selects_original(client, store):
    original = register()
    revised = client.post(input_endpoint(original, "/revisions"), headers=headers("revise"),
                          json={"patch": constraint_patch(original)}).json()
    response = client.post(input_endpoint(revised, "/revisions"), headers=headers("revert"),
                           json={"patch": {"requirements": original["request"]["requirements"]}})
    assert response.status_code == 201 and response.json() == original
    assert len(store.inputs) == 2


@pytest.mark.parametrize("action", ["/revisions", "/restore"])
@pytest.mark.parametrize("case", ["unauthenticated", "foreign_project", "foreign_input", "revoked", "read_only"])
def test_revision_and_restore_authorization(client, store, action, case):
    original = register()
    before = copy.deepcopy((store.inputs, store.versions, store.files, store.receipts))
    auth, project = headers("forbidden"), PROJECT
    if case == "unauthenticated":
        auth.pop("Authorization")
    elif case == "foreign_project":
        project = uuid.uuid4()
    elif case == "foreign_input":
        original = {**original, "input_version_id": str(uuid.uuid4())}
    else:
        store.role = None if case == "revoked" else "read_only"
    response = client.post(input_endpoint(original, action, project), headers=auth,
                           json={"patch": constraint_patch(original)} if action == "/revisions" else None)
    assert response.status_code in (401, 403, 404), response.text
    assert (store.inputs, store.versions, store.files, store.receipts) == before


@pytest.mark.parametrize("case", ["read_only", "unauthenticated", "foreign_project", "revoked"])
def test_active_input_read_authorization(client, store, case):
    original = register()
    auth, project = headers(), PROJECT
    if case == "read_only":
        store.role = "read_only"
    elif case == "unauthenticated":
        auth.pop("Authorization")
    elif case == "foreign_project":
        project = uuid.uuid4()
    else:
        store.role = None
    response = client.get(input_endpoint(original, project=project), headers=auth)
    if case == "read_only":
        assert response.status_code == 200 and response.json() == original
    else:
        assert response.status_code in (401, 403, 404)
        assert "request" not in response.json()


def test_project_reference_registration_postgres(tmp_path, monkeypatch):
    import os
    from test_project_drawing_editor import canonical_ci_pg

    if not ((os.environ.get("LEAF_E364B_PG_PARENT") and os.environ.get("DATABASE_URL"))
            or canonical_ci_pg()):
        pytest.skip("requires managed or verified canonical disposable PostgreSQL")

    import write_loop
    import store as blobs
    from leaf_platform import project_lifecycle, store as metadata_store

    db = platform_link.platform_db()
    db.apply_migration()
    backend = blobs.FilesystemBackend(str(tmp_path))
    monkeypatch.setattr(write_loop, "upload_backend_for_tenant", lambda tenant: backend)
    org = metadata_store.create_org(name="ARLO reference proof")
    actor = metadata_store.create_identity_binding(org.org_id, "auth0", str(uuid.uuid4()), role="owner")

    def new_project(name):
        return project_lifecycle.create_blank_project(
            org.org_id, actor.binding_id, name=name,
            idempotency_key=str(uuid.uuid4()))["project"]["project_id"]

    project, other = new_project("ARLO input"), new_project("ARLO other")
    monkeypatch.setattr(api.platform_deps, "auth_live", lambda: True)
    monkeypatch.setattr(api.platform_deps, "_verified_identity", lambda authorization:
                        SimpleNamespace(platform_tenant_id=org.org_id, binding_id=actor.binding_id))
    app = FastAPI()
    app.include_router(api.router)
    reference_url = f"/api/projects/{project}/reference-files"
    request = arlo_lab.load_example("feeder-lab-v1", "1")
    request["seed"] = 17
    raw = json.dumps(request, indent=2).encode()

    with TestClient(app) as web:
        def upload(filename, content_type, data):
            result = web.post(f"{reference_url}?filename={filename}", content=data,
                              headers={**headers(), "Content-Type": content_type})
            assert result.status_code == 201, result.text
            return result.json()["material"]["material_id"]

        material = upload("request.json", "application/json", raw)
        url = f"{reference_url}/{material}/arlo-input"
        registered = web.post(url, headers=headers())
        assert registered.status_code == 201, registered.text
        result = registered.json()
        assert web.post(url, headers=headers()).json() == result
        assert result["organization_id"] == str(org.org_id) and result["project_id"] == project
        assert result["material_id"] == material
        assert result["original_sha256"] == hashlib.sha256(raw).hexdigest()
        assert result["input_sha256"] == arlo_lab.digest(request)
        assert web.get(f"{reference_url}/{material}/content", headers=headers()).content == raw

        revision_url = f"/api/projects/{project}/arlo-inputs/{result['input_version_id']}"
        revision = web.post(revision_url + "/revisions", headers=headers("pg-revision"),
                            json={"patch": constraint_patch(result)})
        assert revision.status_code == 201, revision.text
        revised = revision.json()
        assert revised["parent_id"] == revised["original_id"] == result["input_version_id"]
        assert revised["original_sha256"] == result["original_sha256"]
        assert web.get(revision_url, headers=headers()).json() == revised
        revised_url = f"/api/projects/{project}/arlo-inputs/{revised['input_version_id']}"
        rejected = web.post(revised_url + "/revisions", headers=headers("pg-invalid"),
                            json={"patch": {"requirements": {"diameter_m": -1}}})
        assert rejected.status_code == 422 and "diameter_m" in rejected.json()["detail"]
        assert web.get(revision_url, headers=headers()).json() == revised
        assert arlo_lab.load_registered_request({"org_id": org.org_id, "project_id": project,
            "input_version_id": revised["input_version_id"]}, revised["request"]) == revised["request"]
        restored = web.post(revised_url + "/restore", headers=headers("pg-restore"))
        assert restored.status_code == 200 and restored.json() == result
        assert web.get(revision_url, headers=headers()).json() == result
        assert web.get(f"{reference_url}/{material}/content", headers=headers()).content == raw

        changed = upload("changed.json", "application/json", json.dumps({**request, "seed": 18}).encode())
        assert web.post(f"{reference_url}/{changed}/arlo-input", headers=headers()).status_code == 409
        unsupported = upload("notes.txt", "text/plain", b"not an ARLO request")
        assert web.post(f"{reference_url}/{unsupported}/arlo-input", headers=headers()).status_code == 422
        wrong_project = f"/api/projects/{other}/reference-files/{material}/arlo-input"
        assert web.post(wrong_project, headers=headers()).status_code == 404

    version = result["input_version_id"]
    scope = {"org_id": org.org_id, "project_id": project, "input_version_id": version}
    assert arlo_lab.load_registered_request(scope, result["request"]) == request
    with pytest.raises(ValueError, match="differs"):
        arlo_lab.load_registered_request(scope, {**request, "seed": 18})
    with db.connection() as conn:
        row = conn.execute("SELECT provenance FROM drawing_versions WHERE version_id = %s",
                           (version,)).fetchone()
    assert row["provenance"]["material_id"] == material
    assert row["provenance"]["original_sha256"] == result["original_sha256"]
    assert row["provenance"]["input_sha256"] == result["input_sha256"]


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


def test_registered_request_accepts_browser_numeric_spelling_and_returns_stored_bytes(store):
    response = register()
    submitted = copy.deepcopy(response["request"])
    assert submitted["budget"]["timeout_seconds"] == 180.0
    submitted["budget"]["timeout_seconds"] = 180
    assert arlo_lab.digest(submitted) != response["input_sha256"]
    loaded = arlo_lab.load_registered_request(context(response), submitted)
    assert arlo_lab.digest(loaded) == response["input_sha256"]
    assert type(loaded["budget"]["timeout_seconds"]) is float


@pytest.mark.parametrize("value", [180.000001, True, "180", None])
def test_registered_request_refuses_changed_numeric_value_or_type(store, value):
    response = register()
    submitted = copy.deepcopy(response["request"])
    submitted["budget"]["timeout_seconds"] = value
    with pytest.raises(ValueError, match="registered immutable input"):
        arlo_lab.load_registered_request(context(response), submitted)


@pytest.mark.parametrize("stored,submitted", [(1, True), (0, False),
    (9007199254740993, 9007199254740992), (1.000000000000001, 1),
    (0, float("nan")), (0, float("inf"))])
def test_registered_request_refuses_boolean_precision_loss_and_nonfinite(store, monkeypatch, stored, submitted):
    request = copy.deepcopy(arlo_lab.load_example("feeder-lab-v1", "1"))
    request["seed"] = stored
    monkeypatch.setattr(arlo_lab, "load_example", lambda *_: request)
    response = register()
    params = copy.deepcopy(response["request"])
    params["seed"] = submitted
    with pytest.raises(ValueError):
        arlo_lab.load_registered_request(context(response), params)


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
