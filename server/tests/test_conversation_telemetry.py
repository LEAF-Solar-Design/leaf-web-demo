"""Card TEL-2a: conversation.* SERVER telemetry (split of parked TEL-2).

Acceptance oracle (board/claimed/TEL-2a.yaml):
  - Events: conversation.started / conversation.message_appended (role
    label) / conversation.recovered / conversation.truncated /
    conversation.deleted, emitted at the real route choke points.
  - PLATFORM_TELEMETRY.md conventions: domain.action names, string labels,
    no secrets or prompt text in labels. Emission wrapped try/except at the
    call site -- telemetry never touches the response.
  - Mutation-red per event family via TestClient routes; raising-sink test
    proves fire-and-forget.

conversation.message_appended and conversation.recovered are wired into the
two real route choke points this card's file owns (api_post_message,
api_recover_conversation_tail): proven via TestClient, reading
telemetry_sink.emit's captured arguments directly, so deleting either
emit call turns the corresponding test red.

conversation.started / conversation.truncated / conversation.deleted have no
real choke point inside this card's file boundary (routers/conversations.py
+ this test file only -- see the module note in routers/conversations.py):
proven here by direct unit test instead, exactly the TEL-5 -> TEL-7
checkpoint.created/restored precedent.
"""
from __future__ import annotations

import json
import re
import socket
import sys
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import conversations  # noqa: E402
import deps  # noqa: E402
import platform_link  # noqa: E402
import telemetry_sink  # noqa: E402
from routers import conversations as conversations_router  # noqa: E402


@pytest.fixture()
def captured(monkeypatch) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []

    def fake_emit(name, **kw):
        calls.append({"name": name, **kw})
        return True

    monkeypatch.setattr(telemetry_sink, "emit", fake_emit)
    return calls


def _names(calls: List[Dict[str, Any]]) -> List[str]:
    return [c["name"] for c in calls]


class _Tenant(str):
    subject: str = ""


def _client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(conversations_router.router)
    tenant = _Tenant(str(uuid.uuid4()))
    tenant.subject = f"auth0|{uuid.uuid4().hex}"
    app.dependency_overrides[deps.require_active_tenant] = lambda: tenant
    return TestClient(app), tenant


def _message_url(project_id: str = "proj-1", conversation_id: str = "conv-1") -> str:
    return f"/api/projects/{project_id}/conversations/{conversation_id}/messages"


def _recovery_url(project_id: str = "proj-1") -> str:
    return f"/api/projects/{project_id}/conversations/recovery/tail"


def _stub_message_write(monkeypatch, *, org_id: str, conversation_id: str) -> None:
    monkeypatch.setenv(conversations.FLAG_CONV_DURABLE, "1")
    monkeypatch.setattr(platform_link, "require_project_access", lambda *a, **k: org_id)
    monkeypatch.setattr(conversations, "_actor_binding_id", lambda *a, **k: str(uuid.uuid4()))
    monkeypatch.setattr(
        conversations, "get_conversation",
        lambda *a, **k: {"conversation_id": conversation_id})


def _fake_create_message(*, replayed: bool, metadata: Dict[str, Any]):
    message = {
        "message_id": str(uuid.uuid4()), "conversation_id": "conv-1",
        "content": "hi", "metadata": metadata,
        "created_at": "2026-08-20T00:00:00Z",
    }

    def _create_message(*_a: Any, **_k: Any) -> Dict[str, Any]:
        return {"message": message, "replayed": replayed, "quarantine": None}

    return _create_message


@pytest.fixture()
def firehose_wire(monkeypatch):
    """Keep the real sink path, with isolated state and no provider access."""
    with telemetry_sink._wake:
        monkeypatch.setattr(telemetry_sink, "_queue", deque(maxlen=telemetry_sink._QUEUE_MAX))
        monkeypatch.setattr(telemetry_sink, "_stats", {
            "enqueued": 0, "flushed": 0, "dropped_overflow": 0, "dropped_flush": 0,
        })
        monkeypatch.setattr(telemetry_sink, "_noted", set())
    monkeypatch.setenv("LEAF_TELEMETRY_BACKEND", "firehose")
    monkeypatch.setenv("LEAF_TELEMETRY_FIREHOSE_STREAM", "n2-synthetic-firehose")
    monkeypatch.setenv("LEAF_METRICS_ENVIRONMENT", "test")
    monkeypatch.setenv("LEAF_APP_VERSION", "n2-test")
    monkeypatch.setattr(telemetry_sink, "disabled_reason", lambda: None)
    monkeypatch.setattr(telemetry_sink, "_ensure_flusher", lambda: None)
    calls = []

    class Recorder:
        def put_record_batch(self, **kwargs):
            calls.append(kwargs)
            return {"FailedPutCount": 0, "RequestResponses": [{"RecordId": "n2-record"}]}

    def unexpected_external_call(*args, **kwargs):
        pytest.fail("unexpected SDK construction or network call in N2 wire test")

    monkeypatch.setattr(telemetry_sink, "_get_firehose_client", lambda: Recorder())
    for name in ("_make_firehose_client", "_make_client", "_get_client"):
        monkeypatch.setattr(telemetry_sink, name, unexpected_external_call)
    monkeypatch.setattr(socket, "create_connection", unexpected_external_call)
    monkeypatch.setattr(socket, "getaddrinfo", unexpected_external_call)
    return calls


def _assert_message_wire(calls, *, role, tenant_id="n2-synthetic-tenant",
                         conversation_id="n2-synthetic-conversation", forbidden=()):
    with telemetry_sink._wake:
        assert len(telemetry_sink._queue) == 1
        queued = dict(telemetry_sink._queue[0])
    assert telemetry_sink.stats() == {
        "enqueued": 1, "flushed": 0, "dropped_overflow": 0, "dropped_flush": 0,
    }
    assert calls == []
    telemetry_sink._drain_once(0)
    assert len(calls) == 1
    assert set(calls[0]) == {"DeliveryStreamName", "Records"}
    assert calls[0]["DeliveryStreamName"] == "n2-synthetic-firehose"
    records = calls[0]["Records"]
    assert len(records) == 1
    assert set(records[0]) == {"Data"}
    data = records[0]["Data"]
    assert type(data) is bytes
    text = data.decode("utf-8")
    assert text.endswith("\n") and text.count("\n") == 1
    wire = json.loads(text)
    assert wire == queued
    assert set(wire) == {
        "timestamp", "event_type", "event_name", "tenant_id", "tenant_kind",
        "user_email", "session_id", "environment", "app_version", "labels",
    }
    assert wire["event_name"] == "conversation.message_appended"
    assert wire["event_type"] == "custom_event"
    assert wire["tenant_id"] == tenant_id
    assert wire["tenant_kind"] == "account"
    assert wire["user_email"] is None
    assert wire["session_id"] == "server"
    assert wire["environment"] == "test"
    assert wire["app_version"] == "n2-test"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", wire["timestamp"])
    stamped = datetime.fromisoformat(wire["timestamp"].replace("Z", "+00:00"))
    assert abs((datetime.now(timezone.utc) - stamped).total_seconds()) < 60
    assert type(wire["labels"]) is str
    labels = json.loads(wire["labels"])
    expected_keys = {"conversation_id", "schema_version", "event_id"}
    if role is not None:
        expected_keys.add("role")
        assert labels["role"] == role
    assert set(labels) == expected_keys
    assert all(type(value) is str for value in labels.values())
    assert labels["conversation_id"] == conversation_id
    assert labels["schema_version"] == "1"
    event_id = uuid.UUID(labels["event_id"])
    assert str(event_id) == labels["event_id"] and event_id.version == 4
    for marker in forbidden:
        assert marker not in json.dumps(queued, ensure_ascii=False)
        assert marker not in json.dumps(wire, ensure_ascii=False)
        assert marker not in wire["labels"]
    with telemetry_sink._wake:
        assert not telemetry_sink._queue
    assert telemetry_sink.stats() == {
        "enqueued": 1, "flushed": 1, "dropped_overflow": 0, "dropped_flush": 0,
    }


@pytest.mark.parametrize("role", ["user", "assistant", "server"])
def test_message_appended_role_vocabulary_reaches_firehose_unchanged(firehose_wire, role):
    conversations_router.record_conversation_message_appended(
        tenant_id="n2-synthetic-tenant", tenant_kind="account",
        conversation_id="n2-synthetic-conversation", role=role)
    _assert_message_wire(firehose_wire, role=role)


@pytest.mark.parametrize("role,markers", [
    ("synthetic-user@example.invalid", ("synthetic-user@example.invalid",)),
    ("192.0.2.21", ("192.0.2.21",)),
    ("C:/synthetic-project/roof-notes.dwg", ("C:/synthetic-project/roof-notes.dwg",)),
    ("N2_SYNTHETIC_PROJECT_NOTE", ("N2_SYNTHETIC_PROJECT_NOTE",)),
    ("Bearer " + "N2_SYNTHETIC_NOT_A_CREDENTIAL", ("N2_SYNTHETIC_NOT_A_CREDENTIAL",)),
    ('{"email":"n2-nested@example.invalid","token":"N2_NESTED_DUMMY"}',
     ("n2-nested@example.invalid", "N2_NESTED_DUMMY")),
    ("", ()),
    ("USER", ("USER",)),
    (" user ", (" user ",)),
    ("x" * 500, ("x" * 500,)),
    ("合成" * 80, ("合成" * 80,)),
])
def test_message_appended_unrecognized_role_reaches_firehose_as_other(firehose_wire, role, markers):
    assert len(role.encode("utf-8")) <= 500
    conversations_router.record_conversation_message_appended(
        tenant_id="n2-synthetic-tenant", tenant_kind="account",
        conversation_id="n2-synthetic-conversation", role=role)
    _assert_message_wire(firehose_wire, role="other", forbidden=markers)


@pytest.mark.parametrize("omit_role", [False, True])
def test_message_appended_absent_role_remains_absent_on_firehose(firehose_wire, omit_role):
    kwargs = {} if omit_role else {"role": None}
    conversations_router.record_conversation_message_appended(
        tenant_id="n2-synthetic-tenant", tenant_kind="account",
        conversation_id="n2-synthetic-conversation", **kwargs)
    _assert_message_wire(firehose_wire, role=None)


class _N2StringSubclass(str):
    def __eq__(self, other):
        raise AssertionError("role subclass must not be compared")

    def __hash__(self):
        raise AssertionError("role subclass must not be hashed")

    def __str__(self):
        raise AssertionError("role subclass must not be coerced")


class _N2Unstringifiable:
    def __str__(self):
        raise AssertionError("role object must not be coerced")


@pytest.mark.parametrize("role", [
    {"nested": ["n2-dict@example.invalid", {"token": "N2_DICT_DUMMY"}]},
    [{"email": "n2-list@example.invalid", "token": "N2_LIST_DUMMY"}],
    42, True, _N2StringSubclass("user"), _N2Unstringifiable(),
], ids=["dict", "list", "int", "bool", "str-subclass", "unstringifiable"])
def test_message_appended_malformed_direct_role_preserves_event_without_coercion(firehose_wire, role):
    conversations_router.record_conversation_message_appended(
        tenant_id="n2-synthetic-tenant", tenant_kind="account",
        conversation_id="n2-synthetic-conversation", role=role)
    _assert_message_wire(firehose_wire, role="other", forbidden=(
        "n2-dict@example.invalid", "N2_DICT_DUMMY", "n2-list@example.invalid", "N2_LIST_DUMMY",
    ))


def test_message_route_preserves_original_metadata_while_redacting_telemetry_role(monkeypatch, firehose_wire):
    _stub_message_write(monkeypatch, org_id="n2-synthetic-org", conversation_id="conv-1")
    monkeypatch.setattr(conversations, "_actor_binding_id", lambda *a, **k: "n2-synthetic-actor")
    writes = []
    content = "N2_SYNTHETIC_MESSAGE_CONTENT"
    metadata = {"role": "n2-route@example.invalid"}

    def create_message(*args, **kwargs):
        writes.append((args, kwargs))
        return {"message": {
            "message_id": "n2-synthetic-message", "conversation_id": args[2],
            "content": kwargs["content"], "metadata": dict(kwargs["metadata"]),
            "created_at": "2026-08-20T00:00:00Z",
        }, "replayed": False, "quarantine": None}

    monkeypatch.setattr(conversations, "create_message", create_message)
    client, tenant = _client(monkeypatch)
    with client:
        response = client.post(
            _message_url(), json={"content": content, "metadata": metadata},
            headers={"Idempotency-Key": "n2-synthetic-key"},
        )
    assert response.status_code == 201
    assert writes == [(
        ("n2-synthetic-org", "proj-1", "conv-1", "n2-synthetic-actor"),
        {"idempotency_key": "n2-synthetic-key", "content": content, "metadata": metadata},
    )]
    body = response.json()
    assert body["content"] == content
    assert body["metadata"] == metadata
    assert body["conversation_id"] == "conv-1"
    assert body["message_id"] == "n2-synthetic-message"
    _assert_message_wire(firehose_wire, role="other", tenant_id=str(tenant),
                         conversation_id="conv-1", forbidden=(metadata["role"], content))


# --------------------------------------------------------------------------- #
# conversation.message_appended: real choke point (api_post_message)
# --------------------------------------------------------------------------- #

def test_message_appended_emits_role_label_on_a_fresh_insert(monkeypatch, captured):
    org_id = str(uuid.uuid4())
    _stub_message_write(monkeypatch, org_id=org_id, conversation_id="conv-1")
    monkeypatch.setattr(
        conversations, "create_message",
        _fake_create_message(replayed=False, metadata={"role": "user"}))
    client, _tenant = _client(monkeypatch)

    resp = client.post(
        _message_url(conversation_id="conv-1"),
        json={"content": "hi", "metadata": {"role": "user"}},
        headers={"Idempotency-Key": "k1"},
    )

    assert resp.status_code == 201
    assert _names(captured) == ["conversation.message_appended"]
    call = captured[0]
    assert call["labels"] == {"conversation_id": "conv-1", "role": "user"}
    assert call["tenant_id"] == str(_tenant)
    assert call["tenant_kind"] == "account"


def test_message_appended_omits_role_label_when_metadata_has_none(monkeypatch, captured):
    org_id = str(uuid.uuid4())
    _stub_message_write(monkeypatch, org_id=org_id, conversation_id="conv-1")
    monkeypatch.setattr(
        conversations, "create_message",
        _fake_create_message(replayed=False, metadata={}))
    client, _tenant = _client(monkeypatch)

    resp = client.post(
        _message_url(conversation_id="conv-1"), json={"content": "hi"},
        headers={"Idempotency-Key": "k1"},
    )

    assert resp.status_code == 201
    assert _names(captured) == ["conversation.message_appended"]
    assert captured[0]["labels"] == {"conversation_id": "conv-1"}


def test_message_appended_is_silent_on_a_replayed_insert(monkeypatch, captured):
    """A replayed idempotency-key write appended nothing new -- the event
    must not fire a second time for the same logical append."""
    org_id = str(uuid.uuid4())
    _stub_message_write(monkeypatch, org_id=org_id, conversation_id="conv-1")
    monkeypatch.setattr(
        conversations, "create_message",
        _fake_create_message(replayed=True, metadata={"role": "user"}))
    client, _tenant = _client(monkeypatch)

    resp = client.post(
        _message_url(conversation_id="conv-1"),
        json={"content": "hi", "metadata": {"role": "user"}},
        headers={"Idempotency-Key": "k1"},
    )

    assert resp.status_code == 200
    assert _names(captured) == []


def test_message_appended_never_fires_on_a_rejected_write(monkeypatch, captured):
    """Zero-write paths (flag off, validation failure) must never emit."""
    monkeypatch.delenv(conversations.FLAG_CONV_DURABLE, raising=False)
    client, _tenant = _client(monkeypatch)

    resp = client.post(_message_url(), json={"content": "hi"},
                       headers={"Idempotency-Key": "k1"})

    assert resp.status_code == 404
    assert _names(captured) == []


def test_message_appended_raising_sink_never_reaches_the_response(monkeypatch):
    """Real-route raising-sink proof: the sink raising must never surface as
    a broken response on the actual message-write choke point."""
    org_id = str(uuid.uuid4())
    _stub_message_write(monkeypatch, org_id=org_id, conversation_id="conv-1")
    monkeypatch.setattr(
        conversations, "create_message",
        _fake_create_message(replayed=False, metadata={"role": "user"}))

    def _raising_emit(*_a: Any, **_k: Any) -> bool:
        raise RuntimeError("sink down")

    monkeypatch.setattr(telemetry_sink, "emit", _raising_emit)
    client, _tenant = _client(monkeypatch)

    resp = client.post(
        _message_url(conversation_id="conv-1"),
        json={"content": "hi", "metadata": {"role": "user"}},
        headers={"Idempotency-Key": "k1"},
    )

    assert resp.status_code == 201


# --------------------------------------------------------------------------- #
# conversation.recovered: real choke point (api_recover_conversation_tail)
# --------------------------------------------------------------------------- #

def _stub_recovery(monkeypatch, *, page: Dict[str, Any]) -> None:
    monkeypatch.setenv(conversations.FLAG_CONV_DURABLE, "1")
    monkeypatch.setattr(
        conversations, "_require_project", lambda tenant, project_id, *, write: "org-1")
    monkeypatch.setattr(
        conversations, "recover_conversation_tail",
        lambda org_id, project_id, *, cursor=None,
        limit=conversations.RECOVERY_DEFAULT_LIMIT: page,
    )


def test_recovered_emits_items_gaps_has_more_labels(monkeypatch, captured):
    page = {
        "items": [{"message_id": "m1"}], "gaps": [{"message_id": "m2"}],
        "next_cursor": None, "has_more": True,
    }
    _stub_recovery(monkeypatch, page=page)
    client, _tenant = _client(monkeypatch)

    resp = client.get(_recovery_url(project_id="proj-1"))

    assert resp.status_code == 200
    assert _names(captured) == ["conversation.recovered"]
    call = captured[0]
    assert call["labels"] == {
        "project_id": "proj-1", "items_n": "1", "gaps_n": "1", "has_more": "True",
    }


def test_recovered_emits_zero_counts_for_an_empty_project(monkeypatch, captured):
    page = {"items": [], "gaps": [], "next_cursor": None, "has_more": False}
    _stub_recovery(monkeypatch, page=page)
    client, _tenant = _client(monkeypatch)

    resp = client.get(_recovery_url(project_id="proj-1"))

    assert resp.status_code == 200
    assert captured[0]["labels"] == {
        "project_id": "proj-1", "items_n": "0", "gaps_n": "0", "has_more": "False",
    }


def test_recovered_never_fires_on_flag_off_or_denied(monkeypatch, captured):
    monkeypatch.delenv(conversations.FLAG_CONV_DURABLE, raising=False)
    client, _tenant = _client(monkeypatch)

    resp = client.get(_recovery_url())

    assert resp.status_code == 404
    assert _names(captured) == []


def test_recovered_raising_sink_never_reaches_the_response(monkeypatch):
    page = {"items": [], "gaps": [], "next_cursor": None, "has_more": False}
    _stub_recovery(monkeypatch, page=page)

    def _raising_emit(*_a: Any, **_k: Any) -> bool:
        raise RuntimeError("sink down")

    monkeypatch.setattr(telemetry_sink, "emit", _raising_emit)
    client, _tenant = _client(monkeypatch)

    resp = client.get(_recovery_url(project_id="proj-1"))

    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# conversation.started / truncated / deleted: no real choke point inside
# this card's file boundary (see the module note in routers/conversations.py)
# -- proven by direct unit test instead, matching the TEL-5 -> TEL-7
# checkpoint.created/restored precedent.
# --------------------------------------------------------------------------- #

def test_conversation_started_emits_conversation_id(captured):
    conversations_router.record_conversation_started(
        tenant_id="org-1", tenant_kind="account", conversation_id="conv-1")

    assert _names(captured) == ["conversation.started"]
    call = captured[0]
    assert call["labels"] == {"conversation_id": "conv-1"}
    assert call["tenant_id"] == "org-1"
    assert call["tenant_kind"] == "account"
    assert call["session_id"] == "server"


def test_conversation_truncated_emits_the_gc_receipts_own_fields(captured):
    conversations_router.record_conversation_truncated(
        tenant_id="org-1", tenant_kind="account", deleted_message_count=42,
        truncated_by_row_cap=True, truncated_by_wall_clock=False,
    )

    assert _names(captured) == ["conversation.truncated"]
    assert captured[0]["labels"] == {
        "deleted_message_count": "42", "truncated_by_row_cap": "True",
        "truncated_by_wall_clock": "False",
    }


def test_conversation_deleted_emits_conversation_id(captured):
    conversations_router.record_conversation_deleted(
        tenant_id="org-1", tenant_kind="account", conversation_id="conv-1")

    assert _names(captured) == ["conversation.deleted"]
    assert captured[0]["labels"] == {"conversation_id": "conv-1"}


# --------------------------------------------------------------------------- #
# Inventory: every event family the oracle names is proven reachable
# --------------------------------------------------------------------------- #

EVENT_FAMILIES = (
    "conversation.started", "conversation.message_appended",
    "conversation.recovered", "conversation.truncated", "conversation.deleted",
)


def test_inventory_shows_every_named_family(monkeypatch, captured):
    org_id = str(uuid.uuid4())
    _stub_message_write(monkeypatch, org_id=org_id, conversation_id="conv-1")
    monkeypatch.setattr(
        conversations, "create_message",
        _fake_create_message(replayed=False, metadata={"role": "user"}))
    page = {"items": [], "gaps": [], "next_cursor": None, "has_more": False}
    monkeypatch.setattr(
        conversations, "recover_conversation_tail",
        lambda *a, **k: page)
    client, _tenant = _client(monkeypatch)
    client.post(
        _message_url(conversation_id="conv-1"),
        json={"content": "hi", "metadata": {"role": "user"}},
        headers={"Idempotency-Key": "k1"},
    )
    monkeypatch.setattr(
        conversations, "_require_project", lambda tenant, project_id, *, write: org_id)
    client.get(_recovery_url(project_id="proj-1"))

    conversations_router.record_conversation_started(
        tenant_id=org_id, tenant_kind="account", conversation_id="conv-1")
    conversations_router.record_conversation_truncated(
        tenant_id=org_id, tenant_kind="account", deleted_message_count=1,
        truncated_by_row_cap=False, truncated_by_wall_clock=False,
    )
    conversations_router.record_conversation_deleted(
        tenant_id=org_id, tenant_kind="account", conversation_id="conv-1")

    seen = set(_names(captured))
    missing = set(EVENT_FAMILIES) - seen
    assert not missing, f"acceptance oracle families never emitted: {sorted(missing)}"


def test_every_family_name_follows_platform_telemetry_naming_convention():
    """domain.action, lowercase snake, exactly one dot (PLATFORM_TELEMETRY.md)."""
    for name in EVENT_FAMILIES:
        assert re.fullmatch(r"[a-z_]+\.[a-z_]+", name), name


# --------------------------------------------------------------------------- #
# Train-gate requirement: a raising sink must not reach the caller
# (fail-open telemetry) for the two direct-call-only families too.
# --------------------------------------------------------------------------- #

class TestEmissionFailureNeverBreaksTheCaller:
    def _raising_emit(self, *_a: Any, **_k: Any) -> bool:
        raise RuntimeError("sink down")

    def test_all_five_record_helpers_swallow_sink_failure(self, monkeypatch):
        monkeypatch.setattr(telemetry_sink, "emit", self._raising_emit)
        conversations_router.record_conversation_started(
            tenant_id="t", tenant_kind="account", conversation_id="c1")
        conversations_router.record_conversation_message_appended(
            tenant_id="t", tenant_kind="account", conversation_id="c1", role="user")
        conversations_router.record_conversation_recovered(
            tenant_id="t", tenant_kind="account", project_id="p1",
            items_n=1, gaps_n=0, has_more=False,
        )
        conversations_router.record_conversation_truncated(
            tenant_id="t", tenant_kind="account", deleted_message_count=1,
            truncated_by_row_cap=False, truncated_by_wall_clock=False,
        )
        conversations_router.record_conversation_deleted(
            tenant_id="t", tenant_kind="account", conversation_id="c1")
