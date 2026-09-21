"""Offline submission and terminal contracts for the unwired local graph kind."""
import copy
import json
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import write_loop
import store
import broker_client
import jobs
import solar_local_graph as local
from envelopes import ErrorCode, DEFAULT_HTTP_STATUS, error_obj, err_envelope
from product_capability_availability import (
    W1_CAPABILITIES, LOCAL_GRAPH_COMMIT_ADAPTER, is_local_graph_commit,
)
from test_w1_local_graph_adapter import seed, held, run
from test_w1_design_graph import graph  # noqa: F401

TENANT = "fixture-tenant"
JOB = "w1-job"
TOOL = {"name": "solar-settings"}


def params():
    return {"drawing_id": "solar", "expected_rev": 0, "changes": {"panels_in_sequence": 3}}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr(broker_client.requests.sessions.Session, "request",
                        lambda *a, **k: pytest.fail("offline network forbidden"))


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setitem(W1_CAPABILITIES["solar-settings"], "adapter", local.ADAPTER_KIND)


@pytest.fixture
def isolated_jobs(tmp_path, monkeypatch):
    jobs.reset_connection()
    monkeypatch.setattr(jobs, "DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(jobs, "job_store_mode", lambda: "sqlite")
    monkeypatch.setattr(jobs, "ensure_started", lambda: jobs._db())
    monkeypatch.setattr(jobs, "_write_terminal_receipt", lambda *a: None)
    monkeypatch.setattr(jobs.platform_link, "on_submit", lambda *a, **k: None)
    monkeypatch.setattr(jobs.platform_link, "on_running", lambda *a, **k: None)
    jobs._db()
    yield
    jobs.reset_connection()


@pytest.fixture
def committed(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    with held(backend) as fence:
        result = run(backend, fence=fence, job_id=JOB)
    return backend, result


def proof(result, backend, **overrides):
    args = dict(params=params(), tenant_id=TENANT, job_id=JOB, tool=TOOL["name"],
                source_version=1, backend=backend)
    args.update(overrides)
    return local.graph_commit_provenance(result, **args)


def test_kind_is_inert_until_enabled(monkeypatch):
    assert LOCAL_GRAPH_COMMIT_ADAPTER == local.ADAPTER_KIND
    for name in local.LOCAL_GRAPH_TOOLS:
        assert not is_local_graph_commit({"name": name})
    monkeypatch.setitem(W1_CAPABILITIES["solar-settings"], "adapter", local.ADAPTER_KIND)
    assert is_local_graph_commit(TOOL)
    assert not is_local_graph_commit({"name": "solar-correct-string"})
    with pytest.raises(TypeError):
        is_local_graph_commit("solar-settings")
    assert not jobs._allows_local_fallback(dict(TOOL, allow_local_fallback=True))


@pytest.mark.parametrize("override", [
    {"aps_live": True}, {"dwg_version": None}, {"dwg_version": 0}, {"dwg_version": True},
    {"checkout_holder": None}, {"checkout_holder": ""},
    {"checkout_holder": store.ANONYMOUS_HOLDER}, {"checkout_fence": None},
    {"checkout_fence": 0}, {"checkout_fence": True}, {"params": {}},
    {"params": {"drawing_id": 7}},
])
def test_submission_refused_before_insert(enabled, isolated_jobs, override):
    args = dict(tenant_id=TENANT, tool=TOOL, params=params(), dwg="solar", aps_live=False,
                dwg_version=1, checkout_holder="fixture-owner", checkout_fence=1)
    args.update(override)
    with pytest.raises(ValueError):
        jobs.submit_job(**args)
    assert not jobs._query("SELECT job_id FROM jobs")


def test_shipped_submission_unchanged(isolated_jobs, monkeypatch):
    class QueuedExecutor:
        def submit(self, *a, **k):
            pass
    monkeypatch.setattr(jobs, "_executors", {jobs.lane_for(TOOL, False): QueuedExecutor()})
    job_id = jobs.submit_job(TENANT, TOOL, {}, "solar", False,
                             checkout_holder=store.ANONYMOUS_HOLDER)
    assert jobs.get_job(job_id)["status"] == "submitted"


def test_provenance(committed):
    backend, result = committed
    original = params()
    assert proof(result, backend, params=original) == {
        "execution_mode": "local_graph_commit", "adapter": local.ADAPTER_KIND,
        "request_sha256": result["request_sha256"], "graph_sha256": result["graph_sha256"],
        "intake_sha256": result["intake_sha256"], "source_version": 1, "new_version": 2,
    }
    assert original == params()


@pytest.mark.parametrize("mutation", [
    "tenant", "job", "tool", "drawing", "request", "source", "version", "parent",
    "graph_digest", "intake_digest", "schema", "adapter", "unchanged", "revision",
    "bare_graph", "no_version", "no_drawing", "another_job", "bool_revision", "bool_source",
])
def test_provenance_rejects_mutations(committed, graph, mutation):
    backend, original = committed
    result = copy.deepcopy(original)
    overrides = {}
    fields = {"tenant": "tenant_id", "job": "job_id", "tool": "tool", "drawing": "drawing_id",
              "request": "request_sha256", "graph_digest": "graph_sha256",
              "intake_digest": "intake_sha256", "schema": "schema_version", "adapter": "adapter"}
    if mutation in fields:
        result[fields[mutation]] = "other"
    elif mutation == "source":
        overrides["source_version"] = 2
    elif mutation == "version":
        result["new_version"]["version"] = 3
    elif mutation == "parent":
        result["new_version"]["parent"] = 2
    elif mutation == "unchanged":
        result["drawing_changed"] = False
    elif mutation == "revision":
        result["after_rev"] = result["before_rev"]
    elif mutation == "bare_graph":
        result = graph
    elif mutation == "no_version":
        result.pop("new_version")
    elif mutation == "no_drawing":
        overrides["params"] = {}
    elif mutation == "another_job":
        result["job_id"] = "job-a"
        overrides["job_id"] = "job-a"
    elif mutation == "bool_revision":
        result["before_rev"] = False
    else:
        overrides["source_version"] = True
    with pytest.raises(ValueError, match="^graph commit terminal proof rejected$"):
        proof(result, backend, **overrides)


def test_completed_version_survives_head_advance(committed):
    backend, result = committed
    with held(backend) as fence:
        run(backend, {"drawing_id": "solar", "expected_rev": 1,
                      "changes": {"panels_in_sequence": 4}},
            source_version=2, fence=fence, job_id="later-job")
    assert store.load_manifest(backend, TENANT, "solar")["head"] == 3
    assert proof(result, backend)["new_version"] == 2


def test_receipt_committed_by_another_job(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with held(backend) as fence:
        result = run(backend, fence=fence, job_id="job-b")
    with pytest.raises(ValueError, match="^graph commit terminal proof rejected$"):
        proof(result, backend, job_id="job-a")


def test_provenance_rejects_a_rewritten_manifest_note(committed):
    # The receipt, the workitem binding, the stored bytes and the graph all still agree: only the
    # manifest entry's request binding was rewritten, so only the note comparison can refuse it.
    backend, result = committed
    manifest = store.load_manifest(backend, TENANT, "solar")
    entry = next(row for row in manifest["versions"] if row["v"] == 2)
    assert entry["note"] == "solar-graph-commit:" + result["request_sha256"]
    entry["note"] = "solar-graph-commit:" + "0" * 64
    store.save_manifest(backend, TENANT, "solar", manifest)
    with pytest.raises(ValueError, match="^graph commit terminal proof rejected$"):
        proof(result, backend)


def test_provenance_rejects_rewritten_stored_bytes_that_keep_the_graph(committed):
    # The manifest's digest and the embedded graph are untouched, so neither the entry comparison nor
    # the graph read-back can see this: only hashing the stored bytes themselves refuses it.
    backend, result = committed
    _, key = store.resolve_version(backend, TENANT, "solar", 2)
    stored = json.loads(backend.get(key))
    stored["unrelated"] = 1
    backend.put(key, json.dumps(stored).encode("utf-8"))
    reopened = local.resolve_graph_context(backend, TENANT, "solar", 2)
    assert reopened["graph_sha256"] == result["graph_sha256"]
    with pytest.raises(ValueError, match="^graph commit terminal proof rejected$"):
        proof(result, backend)


@pytest.mark.parametrize("mutation", [
    "none", "aps", "cloud", "fallback", "job", "context", "provenance", "version",
])
def test_terminal_context(enabled, committed, mutation):
    backend, result = committed
    provenance = {"attempt": 1, "execution_path": "local", **proof(result, backend)}
    execution = {"tool": TOOL, "aps_live": False, "dwg_version": 1,
                 "graph_commit": {"tenant_id": TENANT}}
    env = {"ok": True, "result": result}
    job_id = JOB
    jobs._validate_terminal_context("complete", env, provenance, 1, execution,
                                    job_id=job_id, durable_params=params())
    if mutation == "none":
        return
    if mutation == "aps":
        execution["aps_live"] = True
    elif mutation == "cloud":
        provenance["execution_path"] = "cloud"
    elif mutation == "fallback":
        provenance["fallback"] = True
    elif mutation == "job":
        job_id = None
    elif mutation == "context":
        execution.pop("graph_commit")
    elif mutation == "provenance":
        provenance["request_sha256"] = "other"
    else:
        execution.pop("dwg_version")
    with pytest.raises(ValueError):
        jobs._validate_terminal_context("complete", env, provenance, 1, execution,
                                        job_id=job_id, durable_params=params())


def broker_args():
    return dict(tenant_id=TENANT, tool=TOOL, params=params(), dwg="solar", aps_live=False,
                job_id=JOB, dwg_version=1, checkout_holder="fixture-owner", checkout_fence=1)


@pytest.mark.parametrize("override", [
    {"aps_live": True}, {"file_only": True}, {"test_source": "fixture"}, {"job_id": None},
    {"dwg_version": None}, {"dwg_version": True}, {"checkout_holder": None},
    {"checkout_holder": store.ANONYMOUS_HOLDER}, {"checkout_fence": None},
    {"checkout_fence": True},
])
def test_broker_refuses_before_post(enabled, monkeypatch, override):
    monkeypatch.setattr(broker_client.requests, "post", lambda *a, **k: pytest.fail("unexpected POST"))
    args = broker_args()
    args.update(override)
    with pytest.raises(ValueError):
        broker_client.run_via_broker(**args)


class Reply:
    def __init__(self, body, status=200):
        self.body = body
        self.status_code = status

    def json(self):
        return self.body


def classified_failure():
    error = error_obj(ErrorCode.BAD_PARAMS, "stale graph", False)
    error["reason_code"] = "STALE_GRAPH_REVISION"
    return {"ok": False, "error": error}


@pytest.mark.parametrize("mutation", ["status", "missing", "job", "tenant", "parent", "valid", "failure"])
def test_broker_receipt(enabled, committed, monkeypatch, mutation):
    _, result = committed
    body = {"ok": True, "result": copy.deepcopy(result)}
    status = 200
    if mutation == "status":
        status = 500
    elif mutation == "missing":
        body.pop("result")
    elif mutation == "job":
        body["result"]["job_id"] = "other"
    elif mutation == "tenant":
        body["result"]["tenant_id"] = "other"
    elif mutation == "parent":
        body["result"]["new_version"]["parent"] = 2
    elif mutation == "failure":
        status, body = 409, classified_failure()
    monkeypatch.setattr(broker_client.requests, "post", lambda *a, **k: Reply(body, status))
    if mutation in {"valid", "failure"}:
        assert broker_client.run_via_broker(**broker_args()) is body
    else:
        with pytest.raises(broker_client.BrokerReceiptRejected, match="^graph commit receipt rejected$"):
            broker_client.run_via_broker(**broker_args())


@pytest.fixture
def api(enabled, isolated_jobs, graph, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import jobs as route
    import entitlements

    backend, _ = seed(tmp_path, monkeypatch, graph)
    tool = next(t for t in json.loads((SERVER / "write_tools.json").read_text())["tools"]
                if t["name"] == TOOL["name"])

    class InlineExecutor:
        def submit(self, fn, *args, **kwargs):
            fn(*args, **kwargs)

    monkeypatch.setattr(jobs, "_executors", {jobs.lane_for(tool, False): InlineExecutor()})
    monkeypatch.setattr(route.deps, "find_tool", lambda *a: tool)
    monkeypatch.setattr(route.deps, "effective_tools_with_provenance", lambda *a: [])
    monkeypatch.setattr(route.deps, "backedge_run_identity", lambda tenant, *a: tenant)
    monkeypatch.setattr(route.deps, "auth_live", lambda: False)
    monkeypatch.setattr(entitlements, "w1_tool_availability", lambda *a, **k: None)
    monkeypatch.setattr(jobs.platform_link, "resolve_submission_context", lambda *a: None)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    monkeypatch.setattr(route.catalog, "live_aps_runtime_authorized", lambda *a, **k: True)
    monkeypatch.delenv("LEAF_EXACT_WRITE_PINS_REQUIRED", raising=False)
    mode = {"value": "valid"}

    def transport(url, *, json, headers, timeout):
        assert url.endswith("/broker/run")
        assert json["aps_live"] is False
        if mode["value"] == "failure":
            return Reply(classified_failure(), 409)
        result = local.run_local_graph_commit(
            backend, json["tenant_id"], json["tool"]["name"], json["params"],
            drawing_id=json["params"]["drawing_id"], source_version=json["dwg_version"],
            holder=json["checkout_holder"], fence=json["checkout_fence"], job_id=json["job_id"])
        if mode["value"] == "wrong_job":
            result["job_id"] = "another-job"
        elif mode["value"] == "absent_version":
            result["new_version"]["version"] = 99
        return Reply({"ok": True, "result": result})

    monkeypatch.setattr(broker_client.requests, "post", transport)
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[route.deps.require_tenant] = lambda: route.deps.TenantContext(
        TENANT, tier="demo", subject="fixture-subject", authority_resolved=True)
    body = {"tool": tool["name"], "dwg": "solar", "params": params(),
            "catalog_digest": route.deps.catalog_tool_digest(tool)}
    body["params"].pop("drawing_id")
    with held(backend) as fence:
        monkeypatch.setattr(route, "_checkout_identity", lambda *a: ("fixture-owner", fence))
        yield TestClient(app), backend, body, mode, route


def test_api_commit_pins_parent(api):
    client, backend, body, _, _ = api
    response = client.post("/api/run?wait=1", json=body)
    assert response.status_code == 200, response.text
    env = response.json()
    rec = jobs.get_job(env["result"]["job_id"])
    assert rec["status"] == "complete" and rec["dwg_version"] == 1
    assert rec["params"]["drawing_id"] == "solar"
    provenance = env["execution_provenance"]
    assert provenance["execution_path"] == "local"
    receipt = proof(env["result"], backend, job_id=env["result"]["job_id"])
    assert all(provenance[key] == value for key, value in receipt.items())
    assert rec["provenance"] == provenance
    assert store.load_manifest(backend, TENANT, "solar")["head"] == 2


@pytest.mark.parametrize("mutation,status,reason", [
    ("conflict", 409, "DRAWING_ID_CONFLICT"), ("anonymous", 403, "CHECKOUT_REQUIRED"),
    ("missing", 409, "GRAPH_CONTEXT_UNAVAILABLE"),
])
def test_api_refusal(api, monkeypatch, mutation, status, reason):
    client, backend, body, _, route = api
    if mutation == "conflict":
        body["params"]["drawing_id"] = "other"
    elif mutation == "anonymous":
        monkeypatch.setattr(route, "_checkout_identity", lambda *a: (store.ANONYMOUS_HOLDER, None))
    else:
        body["dwg"] = "missing"
    response = client.post("/api/run?wait=1", json=body)
    assert response.status_code == status, response.text
    assert response.json()["reason_code"] == reason
    assert not jobs._query("SELECT job_id FROM jobs")
    if mutation == "missing":
        with pytest.raises((OSError, KeyError)):
            store.load_manifest(backend, TENANT, "missing")


def test_api_preserves_classified_failure(api):
    client, _, body, mode, _ = api
    mode["value"] = "failure"
    response = client.post("/api/run?wait=1", json=body)
    assert response.status_code == DEFAULT_HTTP_STATUS[ErrorCode.BAD_PARAMS], response.text
    assert response.json()["reason_code"] == "STALE_GRAPH_REVISION"
    rows = jobs._query("SELECT job_id FROM jobs")
    assert len(rows) == 1 and jobs.get_job(rows[0]["job_id"])["status"] == "failed"


@pytest.mark.parametrize("reason", [None, "lower", "A" * 70])
def test_failure_envelope_legacy_shape(reason):
    error = error_obj(ErrorCode.BAD_PARAMS, "invalid", False)
    expected = err_envelope(ErrorCode.BAD_PARAMS, "invalid", False, tool=TOOL["name"], timing_ms=0)
    if reason is not None:
        error["reason_code"] = reason
    actual = jobs.failed_envelope_from({"error": error, "tool": TOOL["name"]})
    assert actual == expected and "reason_code" not in actual


@pytest.mark.parametrize("mutation", ["wrong_job", "absent_version"])
def test_worker_rejects_unproven_receipt(api, mutation):
    client, _, body, mode, _ = api
    mode["value"] = mutation
    response = client.post("/api/run?wait=1", json=body)
    assert response.status_code == DEFAULT_HTTP_STATUS[ErrorCode.INTERNAL], response.text
    rows = jobs._query("SELECT job_id FROM jobs")
    assert len(rows) == 1
    rec = jobs.get_job(rows[0]["job_id"])
    assert rec["status"] == "failed" and rec["error"]["error_code"] == ErrorCode.INTERNAL
