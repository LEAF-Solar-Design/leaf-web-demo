"""Runs HTTP contract on SQLite and PostgreSQL authorities."""
from datetime import timedelta
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient
import pytest

import deps
import lab_artifacts as ledger
import lab_run_store as store
from lab_canonical import graph_digest
from routers import lab_router, lab_runs
from test_lab_evaluate import case
from test_lab_run_store import authority  # noqa: F401 -- PostgreSQL twin


@pytest.fixture
def rig(authority, monkeypatch):
    monkeypatch.setenv("LEAF_LAB_STORE", store.store_mode())
    monkeypatch.setenv("LEAF_LAB_SURFACE_ENABLED", "1")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_BUILD_REVISION", "abc1234")
    monkeypatch.delenv("LEAF_ENTITLEMENTS_FILE", raising=False)
    monkeypatch.setattr(deps, "effective_tools_with_provenance", lambda tenant: [])
    graph, registry = case()
    projection_registry_for = lab_runs.lab_tool_manifests.registry_for
    monkeypatch.setattr(lab_runs.lab_tool_manifests, "registry_for", lambda tenant: registry)
    artifact = ledger.save(authority, "doc", graph, None, "member")
    context = deps.TenantContext(authority, tier="hosted_pro", subject="run-actor")
    app = FastAPI()
    app.include_router(lab_router.router)
    app.dependency_overrides[deps.require_tenant] = lambda: context
    prior = store.paused()
    with TestClient(app, raise_server_exceptions=False) as http:
        yield SimpleNamespace(http=http, tenant=authority, context=context, app=app,
                              graph=graph, registry=registry, artifact=artifact,
                              projection_registry_for=projection_registry_for)
    store.set_paused(prior, "", "test")


def create(rig):
    return rig.http.post("/api/lab/runs", json={"doc_id": "doc", "graph_revision": 1})


def test_create_pins_and_public_row(rig):
    response = create(rig)
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    assert response.json() == {"run_id": run_id, "state": "queued"}
    row = store.get(rig.tenant, run_id)
    assert row["graph_sha256"] == rig.artifact["sha256"]
    assert row["graph_revision"] == 1
    assert row["input_digest"] == graph_digest({})
    assert row["registry_digest"] == store.registry_digest()
    assert (row["ops_revision"], row["adapter_revision"], row["build_revision"]) == (
        "lab-ops.v1", "lab-worker.v1", "abc1234")
    public = rig.http.get("/api/lab/runs/" + run_id)
    assert public.status_code == 200
    assert public.json() == jsonable_encoder({k: v for k, v in row.items()
                                            if k not in {"lease_owner", "lease_fence"}})
    assert create(rig).json()["run_id"] != run_id


@pytest.mark.parametrize("kind,code", [
    ("host", "lab_graph_host_refused"), ("effect", "lab_graph_effect_refused"),
    ("invalid", "lab_graph_unknown_block"),
    ("malformed", "lab_graph_invalid"),
])
def test_refuses_before_enqueue(rig, monkeypatch, kind, code):
    if kind in {"host", "effect"}:
        monkeypatch.setattr(lab_runs.lab_registry, "lookup", lambda name, version:
                            SimpleNamespace(residency="host" if kind == "host" else "tenant",
                                            effect="pure" if kind == "host" else "write")
                            if name == "identity" else None)
    elif kind == "malformed":
        monkeypatch.setattr(ledger, "get", lambda *args: {**rig.artifact, "graph": {}})
    else:
        rig.registry.clear()
    def forbidden(*args, **kwargs):
        pytest.fail("refused graph reached enqueue")
    monkeypatch.setattr(store, "create", forbidden)
    response = create(rig)
    assert response.status_code == 422, response.text
    assert response.json() == {"detail": code}


@pytest.mark.parametrize("flag,drawing,host,code", [
    ("1", {"project_id": "project", "drawing_id": "drawing", "dwg_version": 1}, False, None),
    ("1", {"project_id": "project", "drawing_id": "drawing", "dwg_version": 0}, False,
     "lab_tool_drawing_required"),
    ("1", None, False, "lab_tool_drawing_required"),
    ("1", None, True, "lab_graph_host_refused"),
    ("0", {"project_id": "project", "drawing_id": "drawing", "dwg_version": 1}, False,
     "lab_graph_effect_refused"),
])
def test_tool_node_admission(rig, monkeypatch, flag, drawing, host, code):
    from dataclasses import replace
    from test_lab_tool_nodes import tool_graph
    tool = tool_graph(monkeypatch)
    tool.graph["blocks"][0]["settings"] = {"drawing": drawing} if drawing is not None else {}
    if host:
        monkeypatch.setitem(lab_runs.lab_registry.REGISTRY, (tool.trusted.kind, tool.trusted.version),
                            replace(tool.trusted, residency="host"))
    monkeypatch.setenv("LEAF_LAB_TOOL_NODES", flag)
    monkeypatch.setattr(lab_runs.lab_tool_manifests, "registry_for", lambda tenant: tool.registry)
    monkeypatch.setattr(ledger, "get", lambda *args: {**rig.artifact, "graph": tool.graph})
    response = create(rig)
    if code is None:
        assert response.status_code == 202, response.text
    else:
        assert response.status_code == 422, response.text
        assert response.json() == {"detail": code}


@pytest.mark.parametrize("change,code", [
    (None, None), ("disabled", "lab_graph_effect_refused"),
    ("stale", "lab_graph_unknown_block"), ("drawing", "lab_graph_invalid_settings"),
    ("malformed-drawing", "lab_graph_invalid_settings"),
    ("host", "lab_graph_host_refused"),
])
def test_projected_tool_admission(rig, monkeypatch, change, code):
    from copy import deepcopy
    from test_lab_tool_nodes import projected_graph
    projected = projected_graph(monkeypatch)
    monkeypatch.setattr(lab_runs.lab_tool_manifests, "registry_for", rig.projection_registry_for)
    monkeypatch.setenv("LEAF_LAB_TOOL_NODES", "0" if change == "disabled" else "1")
    graph = projected.graph
    if change == "stale":
        graph["blocks"][0]["registry"]["code_sha256"] = "0" * 64
    elif change == "drawing":
        graph["blocks"][0]["settings"] = {}
    elif change == "malformed-drawing":
        graph["blocks"][0]["settings"]["drawing"]["dwg_version"] = "2"
    elif change == "host":
        # A typed host source still cannot feed a runnable projected tool.
        source_graph, source_registry = case("constant-number")
        source = deepcopy(source_graph["blocks"][0])
        manifest = deepcopy(next(iter(source_registry.values())))
        manifest.update(id="host-source", effect="read")
        target_pin = projected.manifest["inputs"]["max-rows"]
        for key in ("mode", "unit", "nullable", "schema"):
            manifest["outputs"]["result"][key] = json.loads(json.dumps(target_pin[key]))
        source["id"] = "host"
        source["registry"]["id"] = "host-source"
        graph["blocks"].insert(0, source)
        graph["pipes"] = [{"from": {"node": "host", "port": "result"},
                           "to": {"node": "node", "port": "max-rows"}}]
        registry = rig.projection_registry_for(str(rig.tenant))
        registry["host-source@" + source["registry"]["version"]] = manifest
        monkeypatch.setattr(lab_runs.lab_tool_manifests, "registry_for", lambda tenant: registry)
    monkeypatch.setattr(ledger, "get", lambda *args: {**rig.artifact, "graph": graph})
    response = create(rig)
    assert response.status_code == (202 if code is None else 422), response.text
    if code is not None:
        assert response.json() == {"detail": code}


def test_queue_full(rig):
    for _ in range(16):
        assert create(rig).status_code == 202
    response = create(rig)
    assert response.status_code == 409
    assert response.json() == {"detail": "lab_run_queue_full"}


@pytest.mark.parametrize("method,suffix", [("GET", ""), ("GET", "/events"), ("POST", "/cancel")])
def test_foreign_and_missing_run(rig, method, suffix):
    run_id = create(rig).json()["run_id"]
    rig.app.dependency_overrides[deps.require_tenant] = lambda: deps.TenantContext(
        rig.tenant + "-foreign", tier="hosted_pro", subject="other")
    for key in (run_id, "missing"):
        response = rig.http.request(method, "/api/lab/runs/" + key + suffix)
        assert response.status_code == 404
        assert response.json() == {"detail": "lab_run_not_found"}
    assert store.get(rig.tenant, run_id)["state"] == "queued"


def test_events_cursor(rig):
    run_id = create(rig).json()["run_id"]
    lease = store.claim(rig.tenant, run_id, "worker", 60, build_revision="abc1234", color="active")
    assert store.append_events(rig.tenant, run_id, "worker", lease["fence"], [
        {"kind": "progress", "payload": {"n": n}} for n in range(3)])
    path = "/api/lab/runs/" + run_id + "/events"
    first = rig.http.get(path, params={"after_seq": -1, "limit": 1})
    assert first.status_code == 200
    assert [e["seq"] for e in first.json()["events"]] == [0]
    rest = rig.http.get(path, params={"after_seq": 0, "limit": 500})
    assert [e["seq"] for e in rest.json()["events"]] == [1, 2]


def test_lease_takeover_event_hides_previous_holder(rig, monkeypatch):
    run_id = create(rig).json()["run_id"]
    now = store._now()
    monkeypatch.setattr(store, "_now", lambda: now)
    first = store.claim(rig.tenant, run_id, "private-worker", 60,
                        build_revision="abc1234", color="active")
    assert first
    monkeypatch.setattr(store, "_now", lambda: now + timedelta(seconds=61))
    assert store.claim(rig.tenant, run_id, "replacement-worker", 60,
                       build_revision="abc1234", color="active")
    stored = store.list_events(rig.tenant, run_id, after_seq=-1)
    assert stored[0]["payload"] == {"previous_owner": "private-worker",
                                     "previous_fence": first["fence"]}
    response = rig.http.get("/api/lab/runs/" + run_id + "/events?after_seq=-1")
    assert response.status_code == 200, response.text
    assert response.json() == {"events": [jsonable_encoder({
        "seq": stored[0]["seq"], "kind": "lease_takeover", "block_id": None,
        "at": stored[0]["created_at"], "payload": {},
    })]}
    for private in ("previous_owner", "previous_fence", "private-worker", "replacement-worker"):
        assert private not in response.text


@pytest.mark.parametrize("kind,payload,expected", [
    ("value", {"out": {"type": "number", "data": 42},
               "other": {"type": "string", "data": "ok"}},
              {"out": {"type": "number", "data": 42},
               "other": {"type": "string", "data": "ok"}}),
    ("failed", {"reason": "worker_error"}, {"reason": "worker_error"}),
    ("suspended", {"reason": "maintenance"}, {"reason": "maintenance"}),
    ("cancelled", {"reason": "cancelled"}, {}),
    ("finished", {}, {}),
    ("coalesced", {"removed": 3, "count": 3}, {"removed": 3}),
    ("unknown-kind", {"reason": "private"}, {}),
])
def test_event_payload_projection(rig, kind, payload, expected):
    run_id = create(rig).json()["run_id"]
    lease = store.claim(rig.tenant, run_id, "worker", 60, build_revision="abc1234", color="active")
    if kind != "value":
        payload = {**payload, "previous_owner": "private-worker", "previous_fence": 99,
                   "owner": "private-worker", "fence": 99, "actor": "private-actor",
                   "extra": "private-extra"}
    assert store.append_events(rig.tenant, run_id, "worker", lease["fence"], [
        {"kind": kind, "block_id": "block", "payload": payload}])
    stored = store.list_events(rig.tenant, run_id, after_seq=-1)[0]
    response = rig.http.get("/api/lab/runs/" + run_id + "/events?after_seq=-1")
    assert response.status_code == 200, response.text
    assert response.json() == {"events": [jsonable_encoder({
        "seq": 0, "kind": kind, "block_id": "block", "at": stored["created_at"],
        "payload": expected,
    })]}


def test_cancel_actor_and_idempotency(rig):
    run_id = create(rig).json()["run_id"]
    path = "/api/lab/runs/" + run_id + "/cancel"
    assert rig.http.post(path).json() == {"cancel_requested": True}
    assert rig.http.post(path).json() == {"cancel_requested": False}
    row = store.get(rig.tenant, run_id)
    assert row["state"] == "cancelled" and row["cancel_requested_by"] == "run-actor"


@pytest.mark.parametrize("action", ["pause", "resume"])
def test_worker_controls_need_deploy(rig, action):
    path = "/api/lab/runs/worker/" + action
    rig.app.dependency_overrides[deps.require_tenant] = lambda: deps.TenantContext(
        rig.tenant, tier="hosted_starter", subject="member")
    response = rig.http.post(path, json={"reason": "maintenance"} if action == "pause" else None)
    assert response.status_code == 403
    rig.app.dependency_overrides[deps.require_tenant] = lambda: rig.context
    response = rig.http.post(path, json={"reason": "maintenance"} if action == "pause" else None)
    assert response.status_code == 200
    assert response.json() == {"paused": action == "pause"}
    assert store.paused() is (action == "pause")


def test_status_worker_fields_and_liveness(rig, monkeypatch):
    now = store._now()
    monkeypatch.setattr(store, "_now", lambda: now - timedelta(seconds=61))
    store.record_worker_status(rig.tenant + "-stale", "old", "idle", 0, 0)
    monkeypatch.setattr(store, "_now", lambda: now)
    store.record_worker_status(rig.tenant, "abc1234", "active", 1, 2)
    create(rig)
    response = rig.http.get("/api/lab/status")
    assert response.status_code == 200, response.text
    status = response.json()["worker"]
    assert set(status) == {"paused", "workers", "queue_age_max_s"}
    worker = next(w for w in status["workers"] if w["build_revision"] == "abc1234")
    assert set(worker) == {"build_revision", "color", "heartbeat_age_s", "active_runs", "queued_runs"}
    assert worker["color"] == "active" and worker["active_runs"] == 1 and worker["queued_runs"] == 2
    assert worker["heartbeat_age_s"] >= 0 and status["queue_age_max_s"] >= 0
    assert not any(w["build_revision"] == "old" for w in status["workers"])
    assert "worker_id" not in response.text and "lease_owner" not in response.text and "fence" not in response.text
    store._run("lab-worker-control", lambda sql: sql.execute(
        "DELETE FROM lab_worker_status WHERE worker_id=?", (rig.tenant + "-stale",)), write=True)


@pytest.mark.parametrize("body", [{}, {"doc_id": "doc", "graph_revision": True},
    {"doc_id": "doc", "graph_revision": 0}, {"doc_id": "doc", "graph_revision": "1"},
    {"doc_id": "doc", "graph_revision": 1, "run_id": "caller"}])
def test_exact_create_fields(rig, body):
    assert rig.http.post("/api/lab/runs", json=body).status_code == 422


@pytest.mark.parametrize("query", ["limit=0", "limit=501", "after_seq=-2"])
def test_event_bounds(rig, query):
    run_id = create(rig).json()["run_id"]
    assert rig.http.get("/api/lab/runs/" + run_id + "/events?" + query).status_code == 422


@pytest.mark.parametrize("operation", ["create", "status"])
def test_store_failures_are_sanitized(rig, monkeypatch, operation):
    def fail(*args, **kwargs):
        raise RuntimeError("postgres://private-host/secret")
    monkeypatch.setattr(store, "create" if operation == "create" else "worker_status", fail)
    response = create(rig) if operation == "create" else rig.http.get("/api/lab/status")
    if operation == "status":
        assert response.status_code == 200
        assert response.json()["worker"] == {"state": "unavailable", "reason": "lab_store_unavailable"}
        assert "private-host" not in response.text
    else:
        assert response.status_code == 503
        assert response.json() == {"detail": "lab_store_unavailable"}
