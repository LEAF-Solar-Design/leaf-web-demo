"""SSD1 record A: prompt and approval identity, without execution containment."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

import pytest

os.environ.setdefault("SESSIONS_DB", str(Path(tempfile.mkdtemp(prefix="entity-scope-")) / "sessions.db"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deps
import entity_scope
import session_store
import turn_runner
from routers import sessions as sessions_router
from test_turn_runner import turn_stub, _wait_until

E = {"drawing_id": "D", "handle": "AB12"}
INTAKE = {"polylines": [{"handle": "AB12"}, {"handle": "CD34"}],
          "circles": [{"handle": "EF56"}]}
SOURCE = json.dumps(INTAKE).encode()
B = {"drawing_id": "D", "base_version": 7,
     "base_source_sha256": hashlib.sha256(SOURCE).hexdigest(), "allowed_handles": ["AB12"]}


@pytest.fixture
def lane(monkeypatch, turn_stub):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from envelopes import install_error_handlers

    url, stub = turn_stub
    tenant = "scope-" + uuid.uuid4().hex
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "legacy")
    monkeypatch.setenv("LEAF_CONVERSE_HARNESS_URL", url)
    monkeypatch.setenv("TURN_MAX_S", "10")
    monkeypatch.setattr(sessions_router.request_journal, "enabled", lambda: False)
    monkeypatch.setattr(turn_runner.session_policy, "get_policy", lambda *a: "confirm_all")
    monkeypatch.setattr(turn_runner.agent_ledger, "append", lambda *a, **k: None)
    monkeypatch.setattr(turn_runner.telemetry_sink, "emit", lambda *a, **k: None)
    sess = session_store.get_or_create_session(tenant, "D")
    reads = []
    backend = SimpleNamespace(get=lambda key: SOURCE)
    monkeypatch.setattr(entity_scope.write_loop, "backend_for_tenant", lambda *a, **k: backend)
    monkeypatch.setattr(entity_scope.store, "resolve_version", lambda b, t, d, v: (7, "source-7"))

    def read(b, t, d, v):
        reads.append((d, v))
        return v, deepcopy(INTAKE)

    monkeypatch.setattr(entity_scope.write_loop, "read_intake", read)
    app = FastAPI()
    install_error_handlers(app)
    app.add_middleware(sessions_router.MessageBodyLimitMiddleware)
    app.include_router(sessions_router.router)
    app.dependency_overrides[deps.require_active_tenant] = lambda: tenant
    client = TestClient(app)
    cid = "scope-" + uuid.uuid4().hex
    stub.SCRIPT = [{"type": "turn_complete", "data": {"stop_reason": "end_turn"}}]

    def post(body):
        return client.post(f"/api/sessions/{sess['session_id']}/messages", json=body)

    def events():
        return session_store.recent_events(sess["session_id"], 100)

    def drain():
        assert _wait_until(lambda: session_store.get_session(sess["session_id"])["active_turn_id"] is None)

    result = SimpleNamespace(tenant=tenant, sid=sess["session_id"], cid=cid, stub=stub,
                             post=post, events=events, drain=drain, reads=reads, client=client)
    yield result
    result.drain()
    client.close()


def proposal(lane, shapes=("proposed_run",), capability="drawing.write"):
    forged = dict(B, allowed_handles=["CD34"])
    lane.stub.SCRIPT = []
    for shape in shapes:
        data = {"confirmation_id": lane.cid, "dwg": "D", "tool": "drawing.write",
                "params": {"x": 1}, "capability": capability,
                "kind": "run_capability", "entity_scope": forged,
                "payload": {"dwg": "D", "entity_scope": forged}}
        lane.stub.SCRIPT.append({"type": shape, "data": data})
    lane.stub.SCRIPT.append({"type": "turn_complete", "data": {"stop_reason": "awaiting_approval"}})


def starts(lane):
    return [e["data"] for e in lane.events() if e["type"] == "turn_started"]


def refuse(lane, body, status, message=None):
    response = lane.post(body)
    assert response.status_code == status, response.text
    error = response.json()["error"]
    assert error["error_code"] == "BAD_PARAMS"
    assert error["retryable"] is False
    if message:
        assert error["message"] == message
    assert lane.events() == []
    assert lane.stub.LAST_BODY is None
    assert session_store.get_approval(lane.cid) is None


def test_absent_binding_preserves_legacy_body_and_behavior(lane):
    assert lane.post({"text": "move A"}).status_code == 202
    lane.drain()
    assert starts(lane) == [{"text": "move A"}]
    assert lane.reads == []
    assert "entity_scope" not in lane.stub.LAST_BODY


def test_valid_binding_is_frozen_on_turn_started(lane):
    assert lane.post({"text": "move A", "entity_scope": E}).status_code == 202
    lane.drain()
    assert starts(lane) == [{"text": "move A", "entity_scope": B}]
    assert "entity_scope" not in lane.stub.LAST_BODY


def test_missing_handle_is_refused_before_turn(lane):
    refuse(lane, {"text": "move A", "entity_scope": dict(E, handle="FFFF")}, 400,
           "entity_scope handle is not present in drawing")


def test_other_drawing_is_refused_without_reading_it(lane):
    refuse(lane, {"text": "move A", "entity_scope": dict(E, drawing_id="D2")}, 409,
           "entity_scope drawing does not match session drawing")
    assert lane.reads == []


MALFORMED = [("null", None), ("array", []), ("scalar", "AB12"), ("empty_object", {}),
             ("missing_drawing", {"handle": "AB12"}), ("missing_handle", {"drawing_id": "D"}),
             ("extra_key", dict(E, allowed_handles=["AB12"]))]
for field in E:
    for name, value in [("null", None), ("number", 1), ("boolean", True), ("array", []),
                        ("object", {}), ("empty", ""), ("overlong", "A" * 129),
                        ("space", " A"), ("slash", "A/B"), ("control", "A\nB"), ("unicode", "é")]:
        MALFORMED.append((f"{field}_{name}", dict(E, **{field: value})))


@pytest.mark.parametrize("value", [v for _, v in MALFORMED], ids=[n for n, _ in MALFORMED])
def test_malformed_binding_is_422(lane, value):
    refuse(lane, {"text": "move A", "entity_scope": value}, 422)


@pytest.mark.parametrize("nested", [False, True])
def test_confirm_cannot_supply_entity_scope(lane, nested):
    session_store.create_approval(lane.cid, lane.sid, lane.tenant, turn_id="proposal",
                                  tool=None, params=None, capability=None, rationale=None,
                                  kind="run", payload={"entity_scope": B}, ttl_s=300)
    session_store.decide_approval(lane.cid, True)
    body = {"confirm": {"confirmationId": lane.cid, "approved": True}}
    (body["confirm"] if nested else body)["entity_scope"] = E
    response = lane.post(body)
    assert response.status_code == 400
    assert response.json()["error"]["message"] == "confirm turns cannot supply entity_scope"
    assert not session_store.get_approval(lane.cid)["consumed"]
    assert starts(lane) == []


def resume(lane):
    session_store.decide_approval(lane.cid, True)
    lane.stub.SCRIPT = [{"type": "turn_complete", "data": {"stop_reason": "end_turn"}}]
    assert lane.post({"confirm": {"confirmationId": lane.cid, "approved": True}}).status_code == 202
    lane.drain()
    assert "entity_scope" not in lane.stub.LAST_BODY["confirm"]["proposal"]


def test_reattach_after_proposal_preserves_approval_binding(lane):
    proposal(lane)
    assert lane.post({"text": "move A", "entity_scope": E}).status_code == 202
    lane.drain()
    session_store.get_or_create_session(lane.tenant, "D", scope_kind="entity", scope_handle="CD34")
    assert session_store.get_session(lane.sid)["scope_handle"] == "CD34"
    assert session_store.get_approval(lane.cid)["payload"]["entity_scope"] == B
    resume(lane)
    assert [e["entity_scope"] for e in starts(lane)] == [B, B]


def test_unbound_turn_never_inherits_session_scope(lane):
    session_store.get_or_create_session(lane.tenant, "D", scope_kind="entity", scope_handle="AB12")
    proposal(lane)
    assert lane.post({"text": "move A"}).status_code == 202
    lane.drain()
    assert "entity_scope" not in session_store.get_approval(lane.cid)["payload"]
    resume(lane)
    assert all("entity_scope" not in e for e in starts(lane))
    assert lane.reads == []


# The unbound proposed_run case passes on main by construction and is kept as a regression guard.
@pytest.mark.parametrize("bound", [False, True])
@pytest.mark.parametrize("shape", ["proposed_run", "confirmation_required"])
def test_harness_cannot_supply_entity_scope(lane, bound, shape):
    proposal(lane, (shape,))
    body = {"text": "move A"}
    if bound:
        body["entity_scope"] = E
    assert lane.post(body).status_code == 202
    lane.drain()
    payload = session_store.get_approval(lane.cid)["payload"]
    assert payload.get("entity_scope") == (B if bound else None)
    if not bound:
        assert "entity_scope" not in payload


@pytest.mark.parametrize("shapes", [("proposed_run",), ("confirmation_required",),
                                     ("proposed_run", "confirmation_required")])
def test_both_approval_event_shapes_preserve_binding(lane, shapes):
    proposal(lane, shapes)
    assert lane.post({"text": "move A", "entity_scope": E}).status_code == 202
    lane.drain()
    approval = session_store.get_approval(lane.cid)
    assert approval["payload"]["entity_scope"] == B
    assert not any(e["type"] == "error" for e in lane.events())


def test_policy_resume_preserves_binding(lane, monkeypatch):
    proposal(lane, capability="drawing.read")
    assert lane.post({"text": "move A", "entity_scope": E}).status_code == 202
    lane.drain()
    session_store.get_or_create_session(lane.tenant, "D", scope_kind="entity", scope_handle="CD34")
    lane.stub.SCRIPT = [{"type": "turn_complete", "data": {"stop_reason": "end_turn"}}]
    monkeypatch.setattr(turn_runner.session_policy, "get_policy", lambda *a: "auto_approve_reads")
    monkeypatch.setattr(turn_runner.agent_gate, "read_pending_strict", lambda *a: (None, "absent"))
    turn_runner._auto_confirm_reads(lane.tenant, lane.sid,
                                   {lane.cid: {"capability": "drawing.read"}}, None, "demo")
    lane.drain()
    assert [e["entity_scope"] for e in starts(lane)] == [B, B]
    assert lane.reads == [("D", 7)]


@pytest.mark.parametrize("busy", [False, True])
def test_scoped_queue_is_refused(lane, busy):
    if busy:
        assert session_store.try_begin_turn(lane.sid, "busy", 30)
    try:
        refuse(lane, {"text": "move A", "entity_scope": E, "queue": True}, 400,
               "entity_scope turns cannot be queued")
    finally:
        if busy:
            session_store.end_turn(lane.sid, "busy")


def test_binding_pins_one_numeric_source_version(lane, monkeypatch):
    head = [7]
    def resolve(*args):
        version = head[0]
        head[0] = 8
        return version, "source-7"
    monkeypatch.setattr(entity_scope.store, "resolve_version", resolve)
    assert lane.post({"text": "move A", "entity_scope": E}).status_code == 202
    lane.drain()
    assert head == [8]
    assert lane.reads == [("D", 7)]
    assert starts(lane)[0]["entity_scope"] == B


def test_scope_snapshot_is_not_aliased(lane, monkeypatch):
    proposal(lane)
    snapshot = deepcopy(B)
    monkeypatch.setattr(entity_scope, "freeze", lambda *a: snapshot)
    assert lane.post({"text": "move A", "entity_scope": E}).status_code == 202
    snapshot["allowed_handles"][0] = "CD34"
    lane.stub.SCRIPT[0]["data"]["entity_scope"]["allowed_handles"][0] = "FFFF"
    lane.drain()
    assert starts(lane)[0]["entity_scope"] == B
    assert session_store.get_approval(lane.cid)["payload"]["entity_scope"] == B


@pytest.mark.parametrize("failure,status", [("missing", 404), ("duplicate", 409), ("collection", 404), ("proof", 503)])
def test_drawing_refusals(lane, monkeypatch, failure, status):
    def read(*args):
        if failure == "missing":
            raise KeyError("missing drawing")
        if failure == "proof":
            raise entity_scope.write_loop.ProofStateUnreadable("unreadable")
        if failure == "duplicate":
            return 7, {"polylines": [{"handle": "AB12"}, {"handle": "AB12"}]}
        return 7, {"polylines": {"AB12": {}}}
    monkeypatch.setattr(entity_scope.write_loop, "read_intake", read)
    response = lane.post({"text": "move A", "entity_scope": E})
    assert response.status_code == status, response.text
    assert response.json()["error"]["error_code"] == ("INTERNAL" if status == 503 else "BAD_PARAMS")
    assert response.json()["error"]["retryable"] is (status == 503)
    assert lane.events() == []
    assert lane.stub.LAST_BODY is None


def test_request_id_distinguishes_entity_scope(lane, monkeypatch):
    journal = sessions_router.request_journal
    rows = {}
    digests = []
    original_digest = journal.payload_digest
    monkeypatch.setattr(journal, "enabled", lambda: True)
    monkeypatch.setattr(journal, "get_request", lambda rid: deepcopy(rows.get(rid)))
    monkeypatch.setattr(journal, "active_counts", lambda *a, **k: {"executing": 0, "queued": 0})
    monkeypatch.setattr(journal, "claim_next_queued_and_turn", lambda **k: None)

    def digest(value):
        digests.append(deepcopy(value))
        return original_digest(value)

    def admit(**kw):
        rid = kw["request_id"]
        if rid in rows:
            for key, value in kw.items():
                if rows[rid].get(key) != value:
                    raise journal.RequestConflict("conflict")
            return deepcopy(rows[rid]), False
        rows[rid] = dict(kw, state="admitted")
        return deepcopy(rows[rid]), True

    def begin(rid, tid, **kw):
        if not session_store.try_begin_turn(kw["session_id"], tid, kw["stale_after_s"]):
            return False
        rows[rid].update(state="executing", turn_id=tid)
        return True

    def finish(rid, tid, **kw):
        rows[rid].update(state=kw["state"], response_status=kw["response_status"], response_json=kw["response"])

    monkeypatch.setattr(journal, "payload_digest", digest)
    monkeypatch.setattr(journal, "admit_request", admit)
    monkeypatch.setattr(journal, "begin_request_and_turn", begin)
    monkeypatch.setattr(journal, "finish_request", finish)
    rid = "11111111-1111-4111-8111-111111111111"
    body = {"text": "move", "request_id": rid, "entity_scope": E}
    assert lane.post(body).status_code == 202
    lane.drain()
    for altered in [dict(body, entity_scope=dict(E, handle="CD34")), {"text": "move", "request_id": rid}]:
        response = lane.post(altered)
        assert response.status_code == 409, response.text
        assert response.json()["error"]["error_code"] == "BAD_PARAMS"
    assert lane.post(body).status_code in (200, 202)
    assert len(starts(lane)) == 1
    assert lane.reads == [("D", 7)]
    assert digests[0] != digests[1] != digests[2]
    assert digests[0] == digests[3]
    assert json.dumps(digests[2]) == json.dumps({"text": "move", "classifier_hint": None, "model": None, "queue": False})


@pytest.fixture
def binding_journal(lane, monkeypatch):
    journal = sessions_router.request_journal
    monkeypatch.setattr(journal, "enabled", lambda: True)
    monkeypatch.setattr(journal, "active_counts", lambda *a, **k: {"executing": 0, "queued": 0})
    monkeypatch.setattr(entity_scope.store, "resolve_version", lambda *a: (8, "source-8"))
    monkeypatch.setattr(entity_scope.write_loop, "read_intake",
                        lambda *a: (8, {"polylines": [{"handle": "CD34"}]}))
    rid = str(uuid.uuid4())
    body = {"text": "move", "request_id": rid, "entity_scope": E}
    row = {"request_id": rid, "tenant_id": lane.tenant, "drawing_id": "D",
           "session_id": lane.sid, "principal_key": "", "org_id": None, "project_id": None,
           "digest": journal.payload_digest({"text": "move", "classifier_hint": None,
                                             "model": None, "queue": False, "entity_scope": E}),
           "state": "admitted"}
    get = Mock(return_value=deepcopy(row))
    admit = Mock(return_value=(deepcopy(row), True))
    fail = Mock()
    start = Mock(side_effect=AssertionError("binding refusal must not start a turn"))
    freeze = Mock(wraps=entity_scope.freeze)
    monkeypatch.setattr(journal, "get_request", get)
    monkeypatch.setattr(journal, "admit_request", admit)
    monkeypatch.setattr(journal, "fail_admitted", fail)
    monkeypatch.setattr(turn_runner, "start_turn", start)
    monkeypatch.setattr(entity_scope, "freeze", freeze)
    return SimpleNamespace(body=body, row=row, get=get, admit=admit, fail=fail,
                           start=start, freeze=freeze)


def test_concurrent_replay_is_judged_by_admission_not_fresh_state(lane, binding_journal):
    journal = binding_journal
    recorded = {"status": "started", "turn_id": "recorded-turn", "entity_scope": B}
    completed = dict(journal.row, state="completed", response_status=202, response_json=recorded)
    journal.get.return_value = None
    journal.admit.return_value = (completed, False)
    response = lane.post(journal.body)
    assert response.status_code == 202, response.text
    assert response.json() == json.loads(sessions_router._journal_response(completed, lane.tenant).body)
    journal.admit.assert_called_once_with(**{k: v for k, v in journal.row.items() if k != "state"})
    journal.freeze.assert_not_called()
    journal.fail.assert_not_called()
    journal.start.assert_not_called()


def test_fresh_admission_records_the_binding_refusal(lane, binding_journal):
    journal = binding_journal
    response = lane.post(journal.body)
    assert response.status_code == 400, response.text
    error = response.json()["error"]
    assert error["error_code"] == "BAD_PARAMS"
    assert error["message"] == "entity_scope handle is not present in drawing"
    assert error["retryable"] is False
    journal.admit.assert_called_once()
    journal.freeze.assert_called_once()
    journal.fail.assert_called_once_with(journal.body["request_id"], response_status=400,
                                         response=response.json())
    journal.start.assert_not_called()
    failed = dict(journal.row, state="failed", response_status=400, response_json=response.json())
    journal.get.return_value = failed
    journal.admit.return_value = (failed, False)
    replay = lane.post(journal.body)
    assert replay.status_code == 400
    assert replay.json() == json.loads(sessions_router._journal_response(failed, lane.tenant).body)
    assert replay.json()["error"] == error
    assert journal.admit.call_count == 2
    journal.freeze.assert_called_once()
    journal.fail.assert_called_once()
    journal.start.assert_not_called()


def test_binding_refusal_after_row_moves_replays_the_row(lane, binding_journal):
    journal = binding_journal
    completed = dict(journal.row, state="completed", response_status=202,
                     response_json={"status": "started", "turn_id": "winning-turn", "entity_scope": B})
    journal.get.return_value = completed
    response = lane.post(journal.body)
    assert response.status_code == 202, response.text
    assert response.json() == json.loads(sessions_router._journal_response(completed, lane.tenant).body)
    journal.admit.assert_called_once()
    journal.freeze.assert_called_once()
    journal.get.assert_called_once_with(journal.body["request_id"])
    journal.fail.assert_not_called()
    journal.start.assert_not_called()


def test_unjournaled_binding_refusal_is_unchanged(lane, binding_journal, monkeypatch):
    journal = binding_journal
    monkeypatch.setattr(sessions_router.request_journal, "enabled", lambda: False)
    refuse(lane, journal.body, 400, "entity_scope handle is not present in drawing")
    journal.freeze.assert_called_once()
    journal.get.assert_not_called()
    journal.admit.assert_not_called()
    journal.fail.assert_not_called()
    journal.start.assert_not_called()


@pytest.mark.parametrize("value", [None, {}, dict(B, base_version=True), dict(B, allowed_handles=["AB12", "CD34"])])
def test_invalid_stored_binding_requires_fresh_approval(lane, value):
    session_store.create_approval(lane.cid, lane.sid, lane.tenant, turn_id="proposal",
                                  tool="drawing.write", params={}, capability="drawing.write",
                                  rationale=None, kind="run", ttl_s=300,
                                  payload={"dwg": "D", "entity_scope": value})
    session_store.decide_approval(lane.cid, True)
    response = lane.post({"confirm": {"confirmationId": lane.cid, "approved": True}})
    assert response.status_code == 409
    assert response.json()["error"]["error_code"] == "BAD_PARAMS"
    assert "new approval" in response.json()["error"]["message"]
    assert starts(lane) == []
    assert lane.reads == []


# This passes on main by construction and is kept as a regression guard.
def test_foreign_and_unknown_sessions_do_not_read_drawing(lane):
    foreign = session_store.get_or_create_session("foreign-" + uuid.uuid4().hex, "D")
    for sid in [foreign["session_id"], "unknown-session"]:
        response = lane.client.post(f"/api/sessions/{sid}/messages", json={"text": "move A", "entity_scope": E})
        assert response.status_code == 404
        assert response.json()["error"]["error_code"] == "session_not_found"
    assert lane.reads == []


@pytest.mark.parametrize("collection", entity_scope._COLLECTIONS)
def test_membership_uses_entity_handle_in_each_collection(lane, monkeypatch, collection):
    monkeypatch.setattr(entity_scope.write_loop, "read_intake",
                        lambda *a: (7, {collection: [{"handle": "AB12"}]}))
    assert lane.post({"text": "move A", "entity_scope": E}).status_code == 202
    lane.drain()
    assert starts(lane)[0]["entity_scope"] == B


def test_property_keys_and_handleless_entities_are_not_membership(lane, monkeypatch):
    monkeypatch.setattr(entity_scope.write_loop, "read_intake",
                        lambda *a: (7, {"polylines": [{"id": "AB12"}],
                                        "properties": {"AB12": {}}, "groups": [{"handle": "AB12"}]}))
    refuse(lane, {"text": "move A", "entity_scope": E}, 400)


def test_scoped_nonobject_approval_payload_fails_relay(lane):
    lane.stub.SCRIPT = [{"type": "confirmation_required",
                         "data": {"confirmation_id": lane.cid, "payload": ["not an object"]}}]
    assert lane.post({"text": "move A", "entity_scope": E}).status_code == 202
    lane.drain()
    assert session_store.get_approval(lane.cid) is None
    assert any(e["type"] == "error" for e in lane.events())
