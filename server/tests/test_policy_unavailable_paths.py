"""Request-path handling for a present but untrustworthy entitlements policy.

Run: ``cd server && python -m pytest tests/test_policy_unavailable_paths.py -q``.
"""
from __future__ import annotations

import json
import platform as _stdlib_platform
import sys
from pathlib import Path

import pytest
from starlette.requests import Request

_stdlib_platform.python_implementation()

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import broker
import entitlements
from routers import agent as agent_router
from routers import author as author_router
from routers import sessions as sessions_router
from routers import tools as tools_router


def _plain_request() -> Request:
    """Minimal ASGI scope for calling `post_message` directly (it takes the live
    `Request` so a bring-your-own credential can be TLS-gated). This path never
    sends a credential_grant, so the transport is never actually consulted."""
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [],
                    "query_string": b"", "scheme": "http",
                    "server": ("testserver", 80)})


def _assert_policy_unavailable(response, required: str) -> None:
    assert response.status_code == 503
    body = json.loads(response.body)
    assert body == {
        "entitlement_required": True,
        "required": required,
        "tier": "demo",
        "error": {
            "error_code": "INTERNAL",
            "message": "entitlement policy is unavailable; request refused (fail closed).",
            "retryable": True,
        },
        "degraded_mode": False,
    }


def test_invalid_policy_returns_the_503_envelope_from_all_unfixed_paths(monkeypatch, tmp_path):
    policy = tmp_path / "entitlements.json"
    policy.write_text("{not valid JSON", encoding="utf-8")
    monkeypatch.setenv("LEAF_ENTITLEMENTS_FILE", str(policy))

    author_response = author_router.author(author_router.AuthorRequest(description="make a tool"),
                                           tenant="tenant-a")

    monkeypatch.setattr(sessions_router, "_require_owned_session", lambda *_args: {})
    sessions_response = sessions_router.post_message(
        "session-a", sessions_router.MessageRequest(text="hello"), _plain_request(),
        tenant="tenant-a")

    monkeypatch.setenv("LEAF_APP_DISPATCH_SECRET", "dispatch-secret")
    agent_response = agent_router.internal_gate(
        agent_router.GateRequest(tenant_id="tenant-a", session_id="session-a", turn_id="turn-a",
                                 action="run_read_tool"),
        x_dispatch_secret="dispatch-secret")

    entitlements_response = tools_router.get_entitlements(tenant="tenant-a")

    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "broker-ledger.jsonl")
    monkeypatch.setattr(broker, "_get_da", lambda: pytest.fail("policy error reached APS client"))
    broker_response = broker.broker_run(broker.BrokerRunRequest(
        tenant_id="tenant-a", tool={"name": "read-tool", "capabilities": []},
        params={}, dwg="rooftop_demo", aps_live=False))

    for response, required in (
        (author_response, "build"),
        (sessions_response, "converse"),
        (agent_response, "converse"),
        (entitlements_response, "run_read"),
        (broker_response, "run_read"),
    ):
        _assert_policy_unavailable(response, required)


def test_missing_policy_file_keeps_the_existing_default_behavior(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_ENTITLEMENTS_FILE", str(tmp_path / "missing-entitlements.json"))

    response = tools_router.get_entitlements(tenant="tenant-a")

    assert response["tier"] == "demo"
    assert response["source"] == "policy"
    assert response["entitlements"] == entitlements._HARDCODED_DEFAULTS["demo"]
