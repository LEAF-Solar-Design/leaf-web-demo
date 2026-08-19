"""Card B-C6: conversation harness suite + fence negative control.

Oracle (frozen, Negative control clause): with conv_durable OFF the whole
path is unreachable (this is the flip-time fence proof, executable
standalone).

Executable standalone: no DATABASE_URL, no live PostgreSQL. Every route in
conversations.router is exercised through a TestClient mounting ONLY that
router. "Unreachable" is proven two ways at once: every route responds 404,
AND every storage-layer and project-resolution function conversations.py
could reach is replaced by a spy that RAISES if it is ever invoked -- so a
route that happens to 404 only after doing work would still fail this suite,
not just one that 404s and did nothing.

test_flip_time_the_same_routes_become_reachable_the_instant_the_flag_turns_on
is the flip-time half of the proof: the SAME router, the SAME fixtures, prove
the identical routes become reachable the instant the flag turns on, and
unreachable again the instant it turns back off -- so the 404 above is shown
to be the flag's doing, not some other reason the route can never be hit.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import conversations  # noqa: E402
import deps  # noqa: E402


class UnreachableStoreCall(AssertionError):
    """Raised by every spy below while conv_durable is OFF -- a route that
    reaches this far is not actually fenced."""


def _forbid(name: str):
    def _raise(*_args: Any, **_kwargs: Any) -> Any:
        raise UnreachableStoreCall(
            f"{name}() was called while conv_durable was OFF -- the read/write"
            " path is reachable, the fence is broken"
        )
    return _raise


ROUTES: List[tuple[str, str, Dict[str, Any] | None]] = [
    ("POST", "/api/projects/proj-1/conversations", {"title": "x"}),
    ("GET", "/api/projects/proj-1/conversations", None),
    ("GET", "/api/projects/proj-1/conversations/conv-1", None),
    ("POST", "/api/projects/proj-1/conversations/recover", None),
]


@pytest.fixture(autouse=True)
def _flag_starts_off(monkeypatch: Any) -> None:
    monkeypatch.delenv(conversations.FLAG_CONV_DURABLE, raising=False)


@pytest.fixture
def app_client() -> TestClient:
    app = FastAPI()
    app.include_router(conversations.router)
    # A plain str tenant is a legitimate, real override shape: deps.tenant_echo
    # treats any non-TenantContext tenant as a no-op passthrough, so this never
    # masks a fence bug behind a fake identity type the real dependency chain
    # would never produce.
    app.dependency_overrides[deps.require_active_tenant] = lambda: "fenced-tenant"
    return TestClient(app)


def test_every_conversation_route_is_unreachable_with_conv_durable_off(
    monkeypatch: Any, app_client: TestClient,
) -> None:
    monkeypatch.setattr(conversations, "create_conversation", _forbid("create_conversation"))
    monkeypatch.setattr(conversations, "list_conversations", _forbid("list_conversations"))
    monkeypatch.setattr(conversations, "get_conversation", _forbid("get_conversation"))
    monkeypatch.setattr(
        conversations, "recover_or_create_conversation",
        _forbid("recover_or_create_conversation"),
    )
    monkeypatch.setattr(conversations, "_require_project", _forbid("_require_project"))
    monkeypatch.setattr(conversations, "_actor_binding_id", _forbid("_actor_binding_id"))
    monkeypatch.setattr(conversations, "platform_db", _forbid("platform_db"))
    monkeypatch.setattr(conversations, "platform_store", _forbid("platform_store"))

    assert conversations.conv_durable_enabled() is False
    for method, url, json_body in ROUTES:
        resp = app_client.request(method, url, json=json_body)
        assert resp.status_code == 404, f"{method} {url} must 404 while conv_durable is OFF"
        body = resp.json()
        assert "conversation_id" not in body
        assert "conversations" not in body


def test_flip_time_the_same_routes_become_reachable_the_instant_the_flag_turns_on(
    monkeypatch: Any, app_client: TestClient,
) -> None:
    calls: Dict[str, int] = {"create": 0, "list": 0, "get": 0, "recover": 0}

    def _row() -> Dict[str, Any]:
        return {
            "conversation_id": "conv-1", "org_id": "org-1", "project_id": "proj-1",
            "title": "x", "created_by_binding_id": "actor-1",
            "created_at": None, "updated_at": None,
        }

    def _fake_create(*_a: Any, **_k: Any) -> Dict[str, Any]:
        calls["create"] += 1
        return _row()

    def _fake_list(*_a: Any, **_k: Any) -> List[Dict[str, Any]]:
        calls["list"] += 1
        return []

    def _fake_get(*_a: Any, **_k: Any) -> Dict[str, Any]:
        calls["get"] += 1
        return _row()

    def _fake_recover(*_a: Any, **_k: Any) -> Dict[str, Any]:
        calls["recover"] += 1
        return _row()

    monkeypatch.setattr(conversations, "create_conversation", _fake_create)
    monkeypatch.setattr(conversations, "list_conversations", _fake_list)
    monkeypatch.setattr(conversations, "get_conversation", _fake_get)
    monkeypatch.setattr(conversations, "recover_or_create_conversation", _fake_recover)
    monkeypatch.setattr(conversations, "_require_project", lambda *_a, **_k: "org-1")
    monkeypatch.setattr(conversations, "_actor_binding_id", lambda *_a, **_k: "actor-1")

    # Flag OFF: unreachable through the SAME wired fixtures as the ON case
    # below, so the only variable that changes across this test is the flag.
    assert conversations.conv_durable_enabled() is False
    resp = app_client.post("/api/projects/proj-1/conversations", json={"title": "x"})
    assert resp.status_code == 404
    assert calls["create"] == 0

    # Flip ON: the identical request now reaches the store.
    monkeypatch.setenv(conversations.FLAG_CONV_DURABLE, "1")
    assert conversations.conv_durable_enabled() is True

    resp = app_client.post("/api/projects/proj-1/conversations", json={"title": "x"})
    assert resp.status_code == 201
    assert calls["create"] == 1

    resp = app_client.get("/api/projects/proj-1/conversations")
    assert resp.status_code == 200
    assert calls["list"] == 1

    resp = app_client.get("/api/projects/proj-1/conversations/conv-1")
    assert resp.status_code == 200
    assert calls["get"] == 1

    resp = app_client.post("/api/projects/proj-1/conversations/recover")
    assert resp.status_code == 200
    assert calls["recover"] == 1

    # Flip back OFF: unreachable again, at the exact same routes, with no
    # further store calls -- the fence, not the store's own behavior, is what
    # changed.
    monkeypatch.delenv(conversations.FLAG_CONV_DURABLE, raising=False)
    assert conversations.conv_durable_enabled() is False
    for method, url, json_body in ROUTES:
        resp = app_client.request(method, url, json=json_body)
        assert resp.status_code == 404, f"{method} {url} must 404 once conv_durable flips back off"
    assert calls == {"create": 1, "list": 1, "get": 1, "recover": 1}
