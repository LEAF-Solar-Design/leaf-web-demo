"""SSD1 B2 transports frozen entity identity through durable job delivery."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

import broker
import broker_client
import deps
import entity_scope
import jobs
import session_store
from envelopes import ErrorCode, error_obj
from routers import jobs as jobs_router


B = {"drawing_id": "D", "base_version": 7,
     "base_source_sha256": "ea702b5e2f4c103d5256e9f0653f5ae06d8d08d3a62d76b53902ce96f46dea77",
     "allowed_handles": ["AB12"]}
B2 = {**B, "allowed_handles": ["CD34"]}
TOOL = {"name": "count-by-layer"}
JOB_HASHES = ["fb1c034524fc1daedd6b0492041b021a7b636590399823273861601c53aa2ba8",
              "8382b91c9597735fe1590a6425b7c1c096e988584d02047fe525482a5a9c7c2c",
              "1227341a20ace90eb7c2b27ddac62f8611a51abce50ea9044b3f29f21afeb23b"]
RUN_HASHES = ["a014ea2bbd4e0103198a1728b003ae8b60035679c899f10f62e0e58ef76fdbab",
              "9bb3d2a100d5ccb148849c65ba387e2ddefdbb832b7b0e267ae6cfee547da909",
              "f6a227e137000f5fa5faa0b3c2a78b499b6a5fe60ce63afed5d5ca406f99b623"]


class Queue:
    def __init__(self):
        self.pending = []
        self.submissions = []

    def submit(self, fn, *args, **kwargs):
        self.pending.append((fn, args, kwargs))
        self.submissions.append((fn, args, kwargs))

    def run(self):
        fn, args, kwargs = self.pending.pop(0)
        fn(*args, **kwargs)


@pytest.fixture
def lane(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, "DB_PATH", tmp_path / "jobs.db")
    monkeypatch.setattr(jobs, "_conn", None)
    monkeypatch.setattr(jobs, "job_store_mode", lambda: "legacy")
    monkeypatch.setattr(jobs, "ensure_started", lambda: None)
    monkeypatch.setattr(jobs.platform_link, "on_submit", lambda *a, **k: None)
    monkeypatch.setattr(jobs.platform_link, "on_running", lambda *a, **k: None)
    monkeypatch.setenv("JOB_MAX_ATTEMPTS", "3")
    monkeypatch.setattr(jobs, "max_attempts", lambda: 3)
    queue = Queue()
    monkeypatch.setattr(jobs, "_executors", {jobs.LANE_FAST: queue, jobs.LANE_SLOW: queue})
    yield queue
    jobs.reset_connection()


@pytest.fixture
def wire(monkeypatch):
    bodies = []

    def post(url, *, json, **kwargs):
        bodies.append((url, deepcopy(json)))
        return SimpleNamespace(status_code=200, json=lambda: {"ok": True, "result": {}})

    monkeypatch.setattr(broker_client.requests, "post", post)
    return bodies


def submit(scope=None, **kwargs):
    return jobs.submit_job("T", TOOL, {}, "D", False, org_id="O", project_id="P",
                           dwg_version=7, idempotency_key=kwargs.pop("key", "K"),
                           entity_scope=scope, **kwargs)


def row(jid):
    return dict(jobs._query("SELECT * FROM jobs WHERE job_id = ?", (jid,))[0])


def assert_stored_hash(jid, expected):
    stored = row(jid)
    execution = json.loads(stored["execution_json"])
    payload = {"tenantId": stored["tenant_id"], "orgId": stored["org_id"],
               "projectId": stored["project_id"], "tool": execution["tool"],
               "params": json.loads(stored["params_json"]), "dwg": stored["dwg"],
               "apsLive": execution["aps_live"], "authorityMode": stored["authority_mode"],
               "dwgVersion": stored["dwg_version"]}
    if "entity_scope" in execution:
        payload["entity_scope"] = execution["entity_scope"]
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"),
                                       default=str).encode()).hexdigest()
    assert stored["submission_fingerprint"] == digest == expected


def test_t25_unscoped_job_calls_broker_without_entity_scope_kwarg(lane, monkeypatch):
    calls = []

    def run(tenant_id, tool, params, dwg, aps_live, *, timeout_s,
            dwg_version, ledger_event_key, checkout_holder, checkout_fence, job_id):
        calls.append(dict(tenant_id=tenant_id, tool=tool, params=params, dwg=dwg,
                          aps_live=aps_live, timeout_s=timeout_s, dwg_version=dwg_version,
                          ledger_event_key=ledger_event_key, checkout_holder=checkout_holder,
                          checkout_fence=checkout_fence, job_id=job_id))
        return {"ok": True, "result": {}}

    monkeypatch.setattr(broker_client, "run_via_broker", run)
    jid = submit()
    lane.run()
    assert jobs.get_job(jid)["status"] == "complete"
    assert len(calls) == 1
    assert calls[0]["job_id"] == jid
    assert "entity_scope" not in calls[0]


def test_t01_unscoped_bytes_unchanged(lane, wire):
    jid = submit()
    assert_stored_hash(jid, JOB_HASHES[0])
    assert "entity_scope" not in json.loads(row(jid)["execution_json"])
    lane.run()
    expected = {"tenant_id": "T", "tool": TOOL, "params": {}, "dwg": "D",
                "aps_live": False, "dwg_version": 7, "ledger_event_key": f"{jid}:broker-run",
                "checkout_holder": None, "checkout_fence": None, "job_id": jid}
    assert list(wire[0][1].items()) == list(expected.items())


@pytest.fixture
def route_lane(lane, tmp_path, monkeypatch):
    monkeypatch.setattr(session_store, "DB_PATH", tmp_path / "sessions.db")
    monkeypatch.setattr(session_store, "_conn", None)
    monkeypatch.setattr(session_store, "_store_mode", lambda: "legacy")
    session_store.ensure_started()
    sid = session_store.get_or_create_session("T", "D")["session_id"]
    session_store.append_event(sid, "turn-1", "turn_started", {"entity_scope": B})
    monkeypatch.setattr(jobs.platform_link, "resolve_submission_context", lambda *a: None)
    monkeypatch.setattr(jobs_router, "_checkout_identity", lambda *a: ("anonymous", None))
    monkeypatch.setattr(jobs_router, "_store", lambda: SimpleNamespace(authority_mode=lambda: "legacy"))
    monkeypatch.setattr(deps, "auth_live", lambda: False)
    monkeypatch.setenv("LEAF_EXACT_WRITE_PINS_REQUIRED", "0")
    monkeypatch.setattr(deps, "effective_tools_with_provenance", lambda *a: [])
    monkeypatch.setattr(deps, "load_engine_registry_tools", lambda: [])
    monkeypatch.setattr(jobs_router.entitlements, "entitlements_for",
                        lambda *a: {"run_read": True, "run_write": True})
    monkeypatch.setattr(jobs_router.entitlements, "w1_tool_availability", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(jobs_router.router)
    app.dependency_overrides[deps.require_tenant] = lambda: deps.TenantContext("T", tier="demo")
    with TestClient(app) as client:
        def run(tool):
            monkeypatch.setattr(deps, "find_tool", lambda *a: tool)
            response = client.post("/api/run", headers={
                "X-Authority-Session-Id": sid, "X-Authority-Turn-Id": "turn-1"}, json={
                    "tool": tool["name"], "params": {}, "dwg": "D",
                    "catalog_digest": deps.catalog_tool_digest(tool)})
            assert response.status_code == 202, response.text
            jid = response.json()["job_id"]
            assert json.loads(row(jid)["execution_json"])["entity_scope"] == B
            assert row(jid)["params_json"] == "{}"
            lane.run()
            return jid
        yield SimpleNamespace(run=run, sid=sid)
    if session_store._conn is not None:
        session_store._conn.close()
        session_store._conn = None


def test_t02_scoped_read_carries_b(route_lane, wire):
    route_lane.run(TOOL)
    assert wire[0][1]["entity_scope"] == B
    assert wire[0][1]["params"] == {}


def test_t03_scoped_write_carries_b(route_lane, monkeypatch):
    captured = []
    spy = Mock(wraps=broker._execute)
    monkeypatch.setattr(broker, "_execute", spy)
    monkeypatch.setattr(broker, "_broker_store_mode", lambda: "legacy")
    monkeypatch.setattr(broker, "_ledger_append", lambda *a: None)
    monkeypatch.setattr(broker, "_emit_aps_metric", lambda *a: None)
    # Stop inside the real executor, after proving the request crossed its boundary.
    monkeypatch.setattr(broker, "tenant_disabled", lambda tenant: True)

    def post(url, *, json, **kwargs):
        captured.append(deepcopy(json))
        response = broker._broker_run_request(broker.BrokerRunRequest(**json))
        return SimpleNamespace(status_code=response.status_code,
                               json=lambda: __import__("json").loads(response.body))

    monkeypatch.setattr(broker_client.requests, "post", post)
    route_lane.run({"name": "scope-write", "capabilities": ["drawing.write"]})
    assert captured[0]["entity_scope"] == B
    assert spy.call_count == 1
    assert spy.call_args.args[0].entity_scope == B


def test_t04_confirm_keeps_the_frozen_binding(route_lane, wire, monkeypatch):
    current = session_store.get_session(route_lane.sid)
    current["entity_scope"] = {"drawing_id": "D", "handle": "CD34"}
    monkeypatch.setattr(session_store, "get_session", lambda *a: deepcopy(current))
    session_store.append_event(route_lane.sid, "turn-1", "selection_changed",
                               {"entity_scope": current["entity_scope"]})
    route_lane.run(TOOL)
    assert wire[0][1]["entity_scope"] == B


def test_t11_same_key_same_scope_one_job(lane):
    binding = deepcopy(B)
    jid = submit(binding)
    binding["allowed_handles"][0] = "changed-after-submit"
    assert submit(B) == jid
    assert len(lane.submissions) == 1
    assert_stored_hash(jid, JOB_HASHES[1])


def test_t12_same_key_other_scope_conflicts(lane):
    jid = submit(B)
    with pytest.raises(ValueError, match="idempotency key already exists with different run input"):
        submit(B2)
    assert len(jobs._query("SELECT job_id FROM jobs")) == 1
    assert len(lane.submissions) == 1
    assert_stored_hash(jid, JOB_HASHES[1])
    assert_stored_hash(submit(B2, key="K2"), JOB_HASHES[2])


def test_t13_same_key_unscoped_then_scoped(lane):
    submit()
    with pytest.raises(ValueError, match="idempotency key already exists with different run input"):
        submit(B)
    assert len(jobs._query("SELECT job_id FROM jobs")) == 1
    assert len(lane.submissions) == 1


def test_t14_durable_readback_both_stores(lane, monkeypatch):
    jid = submit(B)
    assert jobs.entity_scope_context(jid) == B
    copy = jobs.entity_scope_context(jid)
    copy["allowed_handles"].append("foreign")
    assert jobs.entity_scope_context(jid) == B
    assert jobs.entity_scope_context(submit(key="unscoped")) is None
    durable = {"entity_scope": deepcopy(B)}
    reads = []
    monkeypatch.setattr(jobs, "job_store_mode", lambda: "postgres")
    monkeypatch.setattr(jobs, "_pg_store", SimpleNamespace(
        execution=lambda job: reads.append(job) or deepcopy(durable)))
    assert jobs.entity_scope_context(jid) == B
    assert reads == [jid]
    durable.clear()
    assert jobs.entity_scope_context(jid) is None


def test_t15_retry_rereads_durable_scope(lane, monkeypatch):
    jid = submit(B)
    calls = []
    reader = Mock(wraps=jobs.entity_scope_context)
    monkeypatch.setattr(jobs, "entity_scope_context", reader)
    monkeypatch.setattr(entity_scope, "resolve_turn_binding",
                        lambda *a: pytest.fail("ended turn must not be consulted"))

    def run(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {"ok": False, "error": error_obj(ErrorCode.INTERNAL, "retry", True)}
        return {"ok": True, "result": {}}

    monkeypatch.setattr(broker_client, "run_via_broker", run)
    lane.run()
    assert jobs.get_job(jid)["status"] == "submitted"
    lane.run()
    assert [c["entity_scope"] for c in calls] == [B, B]
    assert [c["job_id"] for c in calls] == [jid, jid]
    assert reader.call_count == 2
    assert jobs.get_job(jid)["status"] == "complete"


def test_t16_recovery_rereads_durable_scope(lane, wire, monkeypatch):
    jid = submit(B)
    lane.pending.clear()
    monkeypatch.setattr(entity_scope.store, "resolve_version", lambda *a: (99, "new-head"))
    monkeypatch.setattr(entity_scope, "resolve_turn_binding",
                        lambda *a: pytest.fail("recovery must not consult the turn"))
    assert jobs._redispatch_record(jid)
    lane.run()
    assert wire[0][1]["entity_scope"] == B
    assert wire[0][1]["entity_scope"]["base_version"] == 7


def test_t17_malformed_durable_scope_fails_before_broker(lane, wire):
    jid = submit(B)
    execution = json.loads(row(jid)["execution_json"])
    execution["entity_scope"] = {"drawing_id": "D"}
    jobs._db().execute("UPDATE jobs SET execution_json = ? WHERE job_id = ?",
                       (json.dumps(execution), jid))
    jobs._db().commit()
    lane.run()
    record = jobs.get_job(jid)
    assert record["status"] == "failed"
    assert record["error"]["message"] == "invalid durable entity scope"
    assert not wire


def test_t18_local_fallback_keeps_b(lane, monkeypatch):
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return ({"ok": False, "error": error_obj(ErrorCode.INTERNAL, "cloud failed", False)}
                if args[4] else {"ok": True, "result": {}})

    monkeypatch.setattr(broker_client, "run_via_broker", run)
    jid = jobs.submit_job("T", {**TOOL, "allow_local_fallback": True}, {}, "D", True,
                          dwg_version=7, entity_scope=B)
    lane.run()
    assert [c[0][4] for c in calls] == [True, False]
    assert [c[1]["entity_scope"] for c in calls] == [B, B]
    assert calls[1][1]["ledger_event_key"] == f"{jid}:broker-fallback"
    assert jobs.get_job(jid)["status"] == "complete"


def plan():
    return {"drawing_id": "d", "parent_version": 7, "mutations": {},
            "plan_sha256": "a" * 64, "source_sha256": B["base_source_sha256"]}


def test_t19_plan_job_carries_b(lane, wire):
    binding = {**B, "drawing_id": "d"}
    jid = jobs.submit_plan_job("T", plan(), "d", checkout_holder="holder", checkout_fence=1,
                               entity_scope=binding)
    assert json.loads(row(jid)["execution_json"])["entity_scope"] == binding
    lane.run()
    url, body = wire[0]
    assert url.endswith("/broker/run-plan")
    assert body["entity_scope"] == binding
    assert "entity_scope" not in body["plan"]
    req = broker.BrokerPlanRunRequest(**body)
    assert req.entity_scope == binding
    unscoped = broker.BrokerPlanRunRequest(**{k: v for k, v in body.items() if k != "entity_scope"})
    other = req.model_copy(update={"entity_scope": {**binding, "allowed_handles": ["CD34"]}})
    assert len({broker._broker_request_fingerprint(r) for r in (req, unscoped, other)}) == 3
    response = broker._broker_run(broker.BrokerRunRequest(
        tenant_id="T", tool={"name": jobs.PLAN_TOOL_NAME}, entity_scope=binding))
    assert response.status_code == 400
    assert json.loads(response.body)["error"]["message"] == "reserved tool name"


@pytest.mark.parametrize("binding,expected", zip([None, B, B2], RUN_HASHES))
def test_t20_broker_fingerprint(binding, expected):
    request = broker.BrokerRunRequest(tenant_id="T", tool=TOOL, params={}, dwg="D",
                                     dwg_version=7, aps_live=False, entity_scope=binding)
    assert broker._broker_request_fingerprint(request) == expected


@pytest.mark.parametrize("value", [{}, {**B, "base_version": True},
                                    {**B, "allowed_handles": ["AB12", "CD34"]}])
@pytest.mark.parametrize("kind", ["run", "plan"])
def test_t21_wire_rejects_malformed_scope(kind, value):
    with pytest.raises(ValidationError):
        if kind == "run":
            broker.BrokerRunRequest(tenant_id="T", tool=TOOL, entity_scope=value)
        else:
            broker.BrokerPlanRunRequest(tenant_id="T", plan=plan(), dwg="d", dwg_version=7,
                                       entity_scope=value)


@pytest.mark.parametrize("extra", [{"tool": {"name": "solar-settings"}},
                                    {"file_only": True}, {"test_source": "return {}"}])
def test_t22_scoped_broker_refuses_local_graph_and_completion(monkeypatch, extra):
    def unexpected(*args, **kwargs):
        pytest.fail("refused scope reached admission, ledger or execution")

    for name in ("_postgres_store", "_execute", "_ledger_append"):
        monkeypatch.setattr(broker, name, unexpected)
    monkeypatch.setattr(broker, "_broker_store_mode", lambda: "postgres")
    req = broker.BrokerRunRequest(**{"tenant_id": "T", "tool": TOOL, "entity_scope": B, **extra})
    response = broker._broker_run_request(req)
    assert response.status_code == 400
    assert json.loads(response.body)["error"]["reason_code"] == "entity_scope_mutation_unsupported"


@pytest.mark.parametrize("extra", [{"capability_provenance": {}}, {"completion_provenance": {}},
                                    {"tool": {"name": "solar-settings"}}])
def test_t23_submit_refuses_scope_on_campaign_paths(lane, extra):
    args = {"tenant_id": "T", "tool": TOOL, "params": {}, "dwg": "D",
            "aps_live": False, "entity_scope": B, **extra}
    with pytest.raises(ValueError, match="entity-scoped runs cannot use this execution path"):
        jobs._submit_job(**args)
    assert jobs._query("SELECT job_id FROM jobs") == []
    assert not lane.submissions


@pytest.mark.parametrize("value,accepted", [
    (B, True), (B2, True), (None, False), ({}, False), ([], False), (True, False),
    ({**B, "extra": 1}, False), ({**B, "drawing_id": "../D"}, False),
    ({**B, "base_version": True}, False), ({**B, "base_version": 0}, False),
    ({**B, "base_version": 7.0}, False), ({**B, "base_source_sha256": "A" * 64}, False),
    ({**B, "allowed_handles": []}, False),
    ({**B, "allowed_handles": ["AB12", "CD34"]}, False),
    ({**B, "allowed_handles": ["bad handle"]}, False)])
def test_t24_validate_binding_matches_stored_binding(value, accepted):
    for validate in (entity_scope.validate_binding,
                     lambda v: entity_scope.stored_binding({"entity_scope": v})):
        if accepted:
            result = validate(value)
            assert isinstance(result, dict)
            assert result == value and result is not value
            assert result["allowed_handles"] is not value["allowed_handles"]
        else:
            with pytest.raises(entity_scope.ScopeError) as exc:
                validate(value)
            assert exc.value.status_code == 409


@pytest.mark.parametrize("value", [{}, {"other": 1}, "x"])
def test_t24b_stored_binding_without_a_binding_is_none(value):
    assert entity_scope.stored_binding(value) is None
