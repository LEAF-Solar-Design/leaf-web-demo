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

import re
import sys
import uuid
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
