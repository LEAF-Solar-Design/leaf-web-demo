"""Four PostgreSQL proofs of project Solar read and artifact authority."""
from copy import deepcopy
from hashlib import sha256
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
_admission_spec = importlib.util.spec_from_file_location(
    "_sip_r6_pg_admission_helpers", ROOT / "platform/tests/test_sip_r5_admission.py")
admission = importlib.util.module_from_spec(_admission_spec)
_admission_spec.loader.exec_module(admission)
pg = admission.pg
_chain_spec = importlib.util.spec_from_file_location(
    "_sip_r6_pg_chain_helpers", ROOT / "platform/tests/test_sip_r3b_chain.py")
chain = importlib.util.module_from_spec(_chain_spec)
_chain_spec.loader.exec_module(chain)

import solar_project_read as reads
import solar_project_artifacts as artifacts
import solar_local_read as local_read
import test_w1_design_graph as design_cases
import test_w1_equipment as equipment_cases

pytestmark = pytest.mark.skipif(
    not (os.environ.get("DATABASE_URL") or (ROOT / "platform/.env.local").exists()),
    reason="PostgreSQL integration test requires DATABASE_URL")


@pytest.fixture
def world(make_org, monkeypatch):
    generator = admission.world.__wrapped__(make_org, monkeypatch)
    fresh = next(generator)
    try:
        yield fresh
    finally:
        with pytest.raises(StopIteration):
            next(generator)


@pytest.fixture
def http(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    import envelopes
    from routers import project_drawings, jobs, capabilities
    principal = {"tenant": None}
    app = FastAPI()
    envelopes.install_error_handlers(app)
    app.middleware("http")(project_drawings.no_store_responses)
    app.include_router(project_drawings.router)
    app.include_router(jobs.router)
    app.include_router(capabilities.router)
    app.dependency_overrides[pg.deps.require_tenant] = lambda: principal["tenant"]
    app.dependency_overrides[pg.deps.require_active_tenant] = lambda: principal["tenant"]
    monkeypatch.setattr(jobs.jobs, "submit_job", pg.blocked)
    monkeypatch.setattr(jobs.jobs, "wait_for_terminal", pg.blocked)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, principal=principal, route=jobs)


def _principal(s):
    return admission._principal(s)


def _bind(http, s):
    http.principal["tenant"] = _principal(s)


def _headers(s, key):
    return {"X-Org-Id": str(s.org), "X-Project-Id": str(s.project), "Idempotency-Key": key,
        "X-Checkout-Capability": pg.checkout_capability.mint(_principal(s),
            pg.project.checkout_scope(s.project, s.drawing), s.lease.fence)}


def _commit(http, s, tool, params, version, key):
    _bind(http, s)
    body = {"tool": tool, "params": {**deepcopy(params), "drawing_id": str(s.drawing)},
            "dwg": str(version), "catalog_digest": pg.manifest(tool)}
    response = http.client.post("/api/run", json=body, headers=_headers(s, key))
    assert response.status_code == 202, response.text
    result = admission._settle(s, response.json()["job_id"])
    return UUID(result["output_version_id"])


def _seed(http, s):
    return _commit(http, s, "solar-settings", pg.seed_params(s), s.parent, "r6-seed")


def _string_source(http, s):
    # Embedded setup belongs to the initial imported version. Every successor is fenced.
    helpers = chain.commit.__globals__
    ctx = helpers["assigned_parent"](s,
        equipment_cases.case.__wrapped__(design_cases.graph.__wrapped__()))
    first = _commit(http, s, "solar-homeruns", {"expected_rev": ctx.graph["rev"]},
                    s.parent, "r6-homeruns")
    return _commit(http, s, "solar-schedule",
        {"expected_rev": ctx.graph["rev"] + 1, "insertion_point": [0, 0, 0]}, first, "r6-schedule")


def _read_path(s, version):
    return f"/api/projects/{s.project}/drawings/{s.drawing}/versions/{version}/solar-reads"


def _file_path(s, artifact_id):
    return f"/api/projects/{s.project}/drawings/{s.drawing}/solar-artifacts/{artifact_id}"


def _read(http, s, version, *, tool="solar-nec-ampacity-correction", params=None):
    if params is None:
        params = {"base_ampacity": 100, "temp_factor": .91, "conduit_factor": .8}
    # Reads need neither a checkout capability nor idempotency headers.
    return http.client.post(_read_path(s, version), json={"tool": tool, "params": params,
        "catalog_digest": pg.manifest(tool)})


def _census(s):
    with pg.db.cursor() as cur:
        counts = []
        for table in ("jobs", "drawing_versions", "history_operations", "outbox_entries",
                      "solve_records", "project_drawing_checkouts"):
            cur.execute(f"SELECT count(*) AS n FROM {table} WHERE org_id=%s AND project_id=%s",
                        (s.org, s.project))
            counts.append(cur.fetchone()["n"])
        cur.execute("SELECT * FROM project_drawing_checkouts WHERE org_id=%s AND project_id=%s",
                    (s.org, s.project))
        return tuple(counts), cur.fetchall()


def _refusal(response, status, reason):
    assert response.status_code == status, response.text
    assert response.json()["error"]["reason_code"] == reason
    assert response.headers["cache-control"] == "no-store"


def _forge(s, meta, **changes):
    meta = {**meta, **changes}
    binding = {key: meta[key] for key in artifacts._BINDING_KEYS}
    binding["schema"] = artifacts.BINDING_SCHEMA
    meta["artifact_id"] = sha256(pg.local.canonical_bytes(binding)).hexdigest()
    s.blobs.data[artifacts._key(meta, meta["artifact_id"])] = pg.local.canonical_bytes(meta)
    return meta["artifact_id"]


def test_sip_r6_pg_reads_do_not_publish(world, http, monkeypatch):
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    s = world()
    version = _seed(http, s)
    _bind(http, s)
    before = _census(s), deepcopy(s.blobs.data), list(s.blobs.writes)
    for tool, params in (("solar-nec-ampacity-correction", {
        "base_ampacity": 100, "temp_factor": .91, "conduit_factor": .8}), ("solar-string-data", {})):
        response = _read(http, s, version, tool=tool, params=params)
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert result["input_version_id"] == str(version) and result["drawing_changed"] is False
        assert "job_id" not in result and "artifact" not in result["output"]
    assert (_census(s), s.blobs.data, s.blobs.writes) == before
    s = world()
    version = _string_source(http, s)
    _bind(http, s)
    before = _census(s), deepcopy(s.blobs.data), list(s.blobs.writes)
    response = _read(http, s, version, tool="solar-string-data", params={})
    assert response.status_code == 200, response.text
    ref = response.json()["result"]["output"]["artifact"]
    namespace = {"organization_id": str(s.org), "project_id": str(s.project), "drawing_id": str(s.drawing)}
    keys = [artifacts._key(namespace, ref["content_sha256"], blob=True), artifacts._key(namespace, ref["artifact_id"])]
    assert _census(s) == before[0]
    assert set(s.blobs.data) - set(before[1]) == set(keys)
    assert s.blobs.writes[len(before[2]):] == keys
    assert all(s.blobs.data[key] == value for key, value in before[1].items())


def test_sip_r6_pg_membership_and_scope(world, http, monkeypatch):
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    s, foreign = world(), world()
    version = _string_source(http, s)
    _bind(http, s)
    response = _read(http, s, version, tool="solar-string-data", params={})
    assert response.status_code == 200, response.text
    ref = response.json()["result"]["output"]["artifact"]
    path = _file_path(s, ref["artifact_id"])
    actor = s.actor
    s.actor = s.other
    with pg.db.cursor() as cur:
        cur.execute("UPDATE project_member_bindings SET role='read_only' "
                    "WHERE org_id=%s AND project_id=%s AND binding_id=%s", (s.org, s.project, s.actor))
    _bind(http, s)
    assert _read(http, s, version).status_code == 200
    assert http.client.get(path).status_code == 200
    for status in ("revoked",):
        with pg.db.cursor() as cur:
            cur.execute("UPDATE project_member_bindings SET status=%s, revoked_at=clock_timestamp() "
                        "WHERE org_id=%s AND project_id=%s AND binding_id=%s",
                        (status, s.org, s.project, s.actor))
        before = _census(s), list(s.blobs.reads), list(s.blobs.writes)
        _refusal(_read(http, s, version), 403, "SIP_R1_PROJECT_FORBIDDEN")
        _refusal(http.client.get(path, headers={"If-None-Match": "*"}), 403, "SIP_R1_PROJECT_FORBIDDEN")
        assert (_census(s), s.blobs.reads, s.blobs.writes) == before
    s.actor = actor
    _bind(http, foreign)
    before = _census(s), _census(foreign), list(s.blobs.reads), list(foreign.blobs.reads)
    _refusal(_read(http, s, version), 404, "SIP_R1_CONTEXT_NOT_FOUND")
    _refusal(http.client.get(path), 404, "SIP_R1_CONTEXT_NOT_FOUND")
    assert (_census(s), _census(foreign), s.blobs.reads, foreign.blobs.reads) == before
    # A fresh subject each run: the gate runs this file twice on one database
    # (its own suite, then the platform suite), and a subject stays bound to its org.
    outsider = pg.store.create_identity_binding(s.org, "auth0", f"r6-nonmember-{uuid4()}", role="editor")
    s.actor = outsider.binding_id
    _bind(http, s)
    _refusal(_read(http, s, version), 403, "SIP_R1_PROJECT_FORBIDDEN")
    _refusal(http.client.get(path), 403, "SIP_R1_PROJECT_FORBIDDEN")


def test_sip_r6_pg_artifact_source_binding(world, http, monkeypatch):
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    s, foreign = world(), world()
    version, foreign_version = _string_source(http, s), _seed(http, foreign)
    _bind(http, s)
    before = _census(s), _census(foreign), list(s.blobs.writes)
    _refusal(_read(http, s, foreign_version), 404, "SIP_R1_CONTEXT_NOT_FOUND")
    other_path = _read_path(s, version).replace(str(s.drawing), str(foreign.drawing))
    _refusal(http.client.post(other_path, json={"tool": "solar-string-data", "params": {},
        "catalog_digest": pg.manifest("solar-string-data")}), 404, "SIP_R1_CONTEXT_NOT_FOUND")
    assert (_census(s), _census(foreign), s.blobs.writes) == before
    response = _read(http, s, version, tool="solar-string-data", params={})
    assert response.status_code == 200, response.text
    ref = response.json()["result"]["output"]["artifact"]
    source = pg.project.resolve_context(_principal(s), s.project, version, drawing_id=s.drawing, write=False)
    assert ref["source_version_id"] == str(version) and ref["source_intake_sha256"] == source.intake_sha256
    meta = json.loads(s.blobs.data[artifacts._key(ref, ref["artifact_id"])])
    downloaded = http.client.get(ref["download"])
    assert downloaded.status_code == 200 and len(downloaded.content) == 973
    assert downloaded.content == local_read._load_builtin("solar-string-data").run(
        source.intake["solar_design_graph"], {}).content
    for fields, reason, status in (({"source_version_id": str(foreign_version)}, "SIP_R1_CONTEXT_NOT_FOUND", 404),
                                  ({"source_intake_sha256": "d" * 64}, "SIP_R6_ARTIFACT_SOURCE_MISMATCH", 500),
                                  ({"graph_sha256": "d" * 64}, "SIP_R6_ARTIFACT_SOURCE_MISMATCH", 500)):
        forged = _forge(s, meta, **fields)
        _refusal(http.client.get(_file_path(s, forged), headers={"If-None-Match": "*"}), status, reason)
    _bind(http, foreign)
    _refusal(http.client.get(ref["download"]), 404, "SIP_R1_CONTEXT_NOT_FOUND")


def test_sip_r6_pg_historical_and_current(world, http, monkeypatch):
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    s = world()
    version = _string_source(http, s)
    _bind(http, s)
    response = _read(http, s, version, tool="solar-string-data", params={})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    ref = result["output"]["artifact"]
    path = ref["download"]
    original = http.client.get(path + "?current=true")
    assert original.status_code == 200 and len(original.content) == 973
    successor = _commit(http, s, "solar-settings", {"expected_rev": 3,
        "project_changes": {"name": "Successor"}}, version, "r6-successor")
    assert successor != version
    before = _census(s), deepcopy(s.blobs.data), list(s.blobs.writes)
    historical = _read(http, s, version, tool="solar-string-data", params={})
    assert historical.status_code == 200, historical.text
    assert historical.json()["result"]["is_head"] is False
    assert historical.json()["result"]["head_version_id"] == str(successor)
    assert historical.json()["result"]["output"]["artifact"] == ref
    # The identical read may verify its two existing immutable keys again.
    assert _census(s) == before[0] and s.blobs.data == before[1]
    old = http.client.get(path)
    assert old.status_code == 200 and old.content == original.content
    _refusal(http.client.get(path + "?current=true", headers={"If-None-Match": original.headers["etag"]}),
             409, "ARTIFACT_STALE")
    current_request = {"tool": "solar-nec-ampacity-correction", "params": {
        "base_ampacity": 100, "temp_factor": .91, "conduit_factor": .8},
        "catalog_digest": pg.manifest("solar-nec-ampacity-correction"), "current": True}
    _refusal(http.client.post(_read_path(s, version), json=current_request), 409, "SIP_R6_STALE_CURRENT")
    assert _census(s) == before[0] and s.blobs.data == before[1]
