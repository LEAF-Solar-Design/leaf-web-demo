"""Synthetic authenticated selected-version custody. No provider or live drawing calls."""
from copy import deepcopy
import json
from types import SimpleNamespace
from uuid import UUID
import time

import pytest

import project_execution_context as context
import platform_link
import session_store
import solar_project_context
import turn_runner
from routers import sessions

ORG = "11111111-1111-4111-8111-111111111111"
PROJECT = "22222222-2222-4222-8222-222222222222"
DRAWING = "33333333-3333-4333-8333-333333333333"
VERSION = "44444444-4444-4444-8444-444444444444"
OTHER = "55555555-5555-4555-8555-555555555555"
PIN = {"project_id": PROJECT, "input_version_id": VERSION}
BOUND = {"schema": context.SCHEMA, "organization_id": ORG, "project_id": PROJECT,
         "drawing_id": DRAWING, "input_version_id": VERSION, "intake_sha256": "a" * 64}


@pytest.fixture()
def custody(monkeypatch):
    session = {"tenant_id": ORG, "org_id": ORG, "project_id": PROJECT,
               "drawing_id": "rooftop_demo", "active_turn_id": "turn-1",
               "turn_started_at": time.time(), "session_id": "session-1"}
    calls = []
    def resolve(tenant, project, version, **kwargs):
        calls.append((tenant, project, version, kwargs))
        return SimpleNamespace(binding=SimpleNamespace(
            organization_id=UUID(ORG), project_id=UUID(PROJECT), drawing_id=UUID(DRAWING),
            input_version_id=UUID(VERSION)), intake_sha256="a" * 64)
    monkeypatch.setattr(platform_link, "require_project_session_access", lambda *_a, **_kw: ORG)
    monkeypatch.setattr(solar_project_context, "resolve_context", resolve)
    monkeypatch.setattr(session_store, "get_session", lambda _s: deepcopy(session))
    monkeypatch.setattr(session_store, "turn_started_data", lambda *_a: {context.KEY: deepcopy(BOUND)})
    return session, calls


@pytest.mark.parametrize("value", [None, {}, {**PIN, "organization_id": ORG},
                                   {**PIN, "drawing_id": DRAWING}, {**PIN, "input_version_id": False}])
def test_request_pin_contains_only_two_uuid_choices(value):
    with pytest.raises(context.ContextError, match="project_context_invalid"):
        context.validate_request(value)


def test_freeze_derives_authority_and_accepts_selected_historical_version(custody):
    session, calls = custody
    assert context.freeze(ORG, session, PIN) == BOUND
    assert calls == [(ORG, UUID(PROJECT), UUID(VERSION), {"write": False})]
    # The session's legacy label does not override the selected canonical version.
    assert session["drawing_id"] == "rooftop_demo"
    assert context.freeze(ORG, session, None) is None


def test_wrong_project_and_tenant_refuse_before_read(custody):
    session, calls = custody
    for tenant, requested in ((OTHER, PIN), (ORG, {**PIN, "project_id": OTHER})):
        with pytest.raises(context.ContextError, match="session_mismatch"):
            context.freeze(tenant, session, requested)
    assert calls == []


def test_resolve_revalidates_current_access_and_exact_intake(custody, monkeypatch):
    session, _calls = custody
    assert context.resolve_turn_binding("session-1", "turn-1", ORG) == BOUND
    monkeypatch.setattr(platform_link, "require_project_session_access", lambda *_a, **_k: None)
    with pytest.raises(context.ContextError, match="forbidden"):
        context.resolve_turn_binding("session-1", "turn-1", ORG)


def test_changed_content_cannot_retarget_frozen_version(custody, monkeypatch):
    monkeypatch.setattr(solar_project_context, "resolve_context", lambda *_a, **_k: SimpleNamespace(
        binding=SimpleNamespace(organization_id=UUID(ORG), project_id=UUID(PROJECT),
                                drawing_id=UUID(DRAWING), input_version_id=UUID(VERSION)),
        intake_sha256="b" * 64))
    with pytest.raises(context.ContextError, match="context_changed"):
        context.resolve_turn_binding("session-1", "turn-1", ORG)


@pytest.mark.parametrize("change", [{"active_turn_id": "other"}, {"tenant_id": OTHER},
                                    {"turn_started_at": 0}])
def test_wrong_or_expired_turn_refuses(custody, change):
    session, _calls = custody
    session.update(change)
    with pytest.raises(context.ContextError, match="authority_required"):
        context.resolve_turn_binding("session-1", "turn-1", ORG)


def test_missing_pin_refuses_project_execution_but_preserves_legacy(custody, monkeypatch):
    session, _calls = custody
    monkeypatch.setattr(session_store, "turn_started_data", lambda *_a: {})
    with pytest.raises(context.ContextError, match="context_required"):
        context.resolve_turn_binding("session-1", "turn-1", ORG)
    session.update(org_id=None, project_id=None)
    assert context.resolve_turn_binding("session-1", "turn-1", ORG) is None


def test_store_failure_is_not_unbound_legacy(custody, monkeypatch):
    def failed(*_args):
        raise OSError("private store diagnostics")
    monkeypatch.setattr(session_store, "turn_started_data", failed)
    with pytest.raises(context.ContextError, match="authority_required") as exc:
        context.resolve_turn_binding("session-1", "turn-1", ORG)
    assert "private" not in str(exc.value)


def test_approval_uses_server_binding_and_returns_copies():
    raw = {context.KEY: {**BOUND, "project_id": OTHER}, "dwg": DRAWING}
    result = context.approval_payload(raw, BOUND)
    assert context.stored_binding(result) == BOUND
    result[context.KEY]["project_id"] = OTHER
    assert BOUND["project_id"] == PROJECT
    assert context.stored_binding(context.approval_payload(raw, None)) is None


@pytest.fixture()
def router(custody, monkeypatch):
    session, _calls = custody
    monkeypatch.setattr(sessions, "_require_owned_session", lambda *_a: deepcopy(session))
    monkeypatch.setattr(sessions, "is_demo_context", lambda _t: False)
    monkeypatch.setattr(sessions.entitlements, "resolve_tier", lambda _t: "hosted_pro")
    monkeypatch.setattr(sessions.entitlements, "resolve_roles", lambda _t: ((), False))
    monkeypatch.setattr(sessions.entitlements, "entitlements_for", lambda *_a, **_k: {"converse": True})
    monkeypatch.setattr(sessions.request_journal, "enabled", lambda: False)
    calls = []
    def start(*_a, **kwargs):
        calls.append(deepcopy(kwargs))
        return "new-turn"
    monkeypatch.setattr(turn_runner, "start_turn", start)
    return session, calls


def test_message_freezes_request_pin_for_turn(router):
    _session, calls = router
    response = sessions.post_message("session-1", sessions.MessageRequest(text="read", project_context=PIN), object(), ORG)
    assert response.status_code == 202
    assert calls[0][context.KEY] == BOUND
    assert "project_context" not in calls[0]


def test_ordinary_platform_chat_needs_no_selected_drawing(router):
    _session, calls = router
    assert sessions.post_message("session-1", sessions.MessageRequest(text="Change the header"), object(), ORG).status_code == 202
    assert context.KEY not in calls[0]


def test_confirm_request_cannot_supply_a_new_selection(router):
    _session, calls = router
    response = sessions.post_message("session-1", sessions.MessageRequest(
        confirm={"confirmationId": "approval-1", "approved": True}, project_context=PIN), object(), ORG)
    assert response.status_code == 400
    assert calls == []


def test_confirm_recovers_original_server_selection(router, monkeypatch):
    _session, calls = router
    approval = {"tool": "read-layers", "capability": "drawing.read", "params": {},
                "approved": True, "kind": "run_capability",
                "payload": {"dwg": DRAWING, context.KEY: deepcopy(BOUND)}}
    monkeypatch.setattr(session_store, "consume_approval", lambda *_a, **_k: approval)
    response = sessions.post_message("session-1", sessions.MessageRequest(
        confirm={"confirmationId": "approval-1", "approved": False}), object(), ORG)
    assert response.status_code == 202
    assert calls[0][context.KEY] == BOUND
    assert calls[0]["confirm"]["approved"] is True
    assert calls[0]["confirm"]["proposal"]["dwg"] == DRAWING


def test_old_unbound_project_drawing_approval_requires_new_proposal(router, monkeypatch):
    _session, calls = router
    monkeypatch.setattr(session_store, "consume_approval", lambda *_a, **_k: {
        "tool": "read-layers", "capability": "drawing.read", "kind": "run_capability",
        "payload": {"dwg": "rooftop_demo"}, "approved": True})
    response = sessions.post_message("session-1", sessions.MessageRequest(
        confirm={"confirmationId": "old-approval", "approved": True}), object(), ORG)
    assert response.status_code == 409
    assert b"project_execution_context_required" in response.body
    assert calls == []


def test_turn_start_persists_binding_and_emits_only_constant_sidecar(custody, monkeypatch):
    session, _calls = custody
    events, posted, relays = [], [], []
    monkeypatch.setattr(turn_runner.entitlements, "resolve_tier", lambda _t: "hosted_pro")
    monkeypatch.setattr(turn_runner.entitlements, "resolve_roles", lambda _t: ((), False))
    monkeypatch.setattr(turn_runner.request_journal, "enabled", lambda: False)
    monkeypatch.setattr(session_store, "try_begin_turn", lambda *_a, **_k: True)
    monkeypatch.setattr(session_store, "append_event", lambda *_a: events.append(deepcopy(_a)))
    monkeypatch.setattr(turn_runner, "_harness_url", lambda: "http://synthetic-harness")
    monkeypatch.setattr(turn_runner.instant_execution, "assignment_for_session", lambda *_a: None)
    monkeypatch.setattr(turn_runner.broker_client, "harness_headers", lambda: {})
    monkeypatch.setattr(turn_runner.context_packet, "build_packet", lambda *_a, **_k: {})
    monkeypatch.setattr(turn_runner, "_spawn_relay", lambda *_a, **_k: relays.append(deepcopy(_k)))
    def post(_url, **kwargs):
        posted.append(deepcopy(kwargs))
        return SimpleNamespace(status_code=200)
    monkeypatch.setattr(turn_runner.requests, "post", post)
    turn_runner.start_turn(ORG, "session-1", text="read", project_execution_context=BOUND)
    assert events[0][2] == "turn_started"
    assert events[0][3][context.KEY] == BOUND
    assert posted[0]["headers"]["x-leaf-project-execution"] == "canonical-read"
    payload = json.loads(posted[0]["data"])
    assert payload["drawing_id"] == DRAWING
    assert context.KEY not in payload
    assert "project_context" not in payload
    assert relays[0][context.KEY] == BOUND


@pytest.fixture()
def assistant_state(custody, monkeypatch):
    import solar_project_graph
    session, calls = custody
    monkeypatch.setattr(solar_project_context, "resolve_context", lambda *_a, **_k: SimpleNamespace(
        binding=SimpleNamespace(organization_id=UUID(ORG), project_id=UUID(PROJECT),
            drawing_id=UUID(DRAWING), input_version_id=UUID(VERSION),
            head_version_id=UUID(VERSION), is_head=True, version=SimpleNamespace(seq=3)),
        intake_sha256="a" * 64, intake={"polylines": [{"layer": "Panels", "geometry": "private"}]}))
    monkeypatch.setattr(solar_project_graph, "_companion", lambda _i: {"rev": 17})
    return session


def test_private_assistant_state_reuses_active_turn_and_projects_no_geometry(assistant_state, monkeypatch):
    from routers import agent
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", "synthetic")
    request = agent.ProjectDrawingStateRequest(tenant_id=ORG, session_id="session-1", turn_id="turn-1")
    assert agent.internal_project_drawing_state(request, None).status_code == 401
    response = agent.internal_project_drawing_state(request, "synthetic")
    assert response.status_code == 200
    value = json.loads(response.body)
    assert value["binding"] == BOUND
    assert value["revision"] == 3
    assert value["graph_revision"] == 17
    assert value["summary"] == {"entity_total": 1, "layers": [{"name": "Panels", "count": 1}]}
    assert "geometry" not in response.body.decode()
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("change", [{"tenant_id": OTHER}, {"active_turn_id": "other"},
    {"turn_started_at": 0}])
def test_private_assistant_state_refuses_bad_stored_authority(assistant_state, monkeypatch, change):
    from routers import agent
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", "synthetic")
    assistant_state.update(change)
    response = agent.internal_project_drawing_state(agent.ProjectDrawingStateRequest(
        tenant_id=ORG, session_id="session-1", turn_id="turn-1"), "synthetic")
    assert response.status_code == 403
    assert "binding" not in json.loads(response.body)


def test_private_assistant_unbound_is_explicit_for_project_and_legacy(assistant_state, monkeypatch):
    from routers import agent
    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", "synthetic")
    monkeypatch.setattr(session_store, "turn_started_data", lambda *_a: {})
    request = agent.ProjectDrawingStateRequest(tenant_id=ORG, session_id="session-1", turn_id="turn-1")
    response = agent.internal_project_drawing_state(request, "synthetic")
    assert response.status_code == 200
    assert json.loads(response.body) == {"binding": None}
    assistant_state.update(project_id=None, org_id=None)
    assert json.loads(agent.internal_project_drawing_state(request, "synthetic").body) == {"binding": None}


def test_drawing_state_allows_unbound_active_project_turn(custody, monkeypatch):
    _session, calls = custody
    monkeypatch.setattr(session_store, "turn_started_data", lambda *_a: {})
    assert context.drawing_state("session-1", "turn-1", ORG) == {"binding": None}
    assert calls == []


def test_resolve_turn_binding_still_requires_project_binding_by_default(custody, monkeypatch):
    monkeypatch.setattr(session_store, "turn_started_data", lambda *_a: {})
    with pytest.raises(context.ContextError, match="^project_execution_context_required$"):
        context.resolve_turn_binding("session-1", "turn-1", ORG)


def test_drawing_state_refuses_unbound_inactive_project_turn(custody, monkeypatch):
    monkeypatch.setattr(session_store, "turn_started_data", lambda *_a: {})
    with pytest.raises(context.ContextError, match="^project_turn_authority_required$") as exc:
        context.drawing_state("session-1", "other-turn", ORG)
    assert exc.value.status_code == 403
