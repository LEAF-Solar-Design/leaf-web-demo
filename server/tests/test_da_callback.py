"""Callback-primary completion seam tests.

Run from ``server/``: ``python -m pytest tests/test_da_callback.py -q``.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import broker


@pytest.fixture(autouse=True)
def callback_env(monkeypatch):
    monkeypatch.setenv("LEAF_CALLBACK_SECRET", "test-callback-secret")
    monkeypatch.delenv("LEAF_CALLBACK_PRIMARY", raising=False)
    monkeypatch.delenv("LEAF_CALLBACK_URL", raising=False)
    callbacks = broker._get_callbacks()
    assert callbacks is not None
    callbacks._consumed.clear()


def _signed(body: bytes) -> str:
    callbacks = broker._get_callbacks()
    assert callbacks is not None
    return callbacks.sign_payload(body)


def test_callback_uses_compare_digest_and_returns_structured_success(monkeypatch):
    callbacks = broker._get_callbacks()
    assert callbacks is not None
    observed = []
    original = callbacks.hmac.compare_digest

    def spy(expected, supplied):
        observed.append((expected, supplied))
        return original(expected, supplied)

    monkeypatch.setattr(callbacks.hmac, "compare_digest", spy)
    body = json.dumps({"job_id": "job-1", "status": "success"}).encode()
    response = TestClient(broker.app).post(
        "/da/callback", content=body,
        headers={"X-Leaf-Signature": _signed(body), "X-Leaf-Timestamp": str(time.time()),
                 "X-Leaf-Nonce": "nonce-1"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["completion_mode"] == "callback"
    assert observed, "signature verification must call hmac.compare_digest"


def test_invalid_or_missing_signature_fails_closed_without_consuming_state():
    callbacks = broker._get_callbacks()
    assert callbacks is not None
    body = b'{"job_id":"job-2","status":"success"}'
    before = dict(callbacks._consumed)
    response = TestClient(broker.app).post(
        "/da/callback", content=body,
        headers={"X-Leaf-Timestamp": str(time.time()), "X-Leaf-Nonce": "nonce-2"},
    )
    assert response.status_code == 401
    assert response.json()["ok"] is False
    assert response.json()["error"]["error_code"] == "BAD_PARAMS"
    assert callbacks._consumed == before


def test_poll_default_callback_primary_and_reaper_fallback(monkeypatch):
    class DA:
        def __init__(self):
            self.calls = []

        def run_tool(self, local, tool, params):
            self.calls.append("poll")
            return {"ok": True}

        def run_tool_callback(self, local, tool, params, *, callback_url):
            self.calls.append(("callback", callback_url))
            return {"ok": True}

        def cancel_workitem(self, workitem_id):
            self.calls.append(("reap", workitem_id))
            return {"cancelled": True}

    da = DA()
    monkeypatch.setattr(broker, "_get_da", lambda: da)
    assert broker._run_live_tool(da, "drawing.dwg", {"name": "tool"}, {}) == {"ok": True}
    assert da.calls == ["poll"]

    monkeypatch.setenv("LEAF_CALLBACK_PRIMARY", "1")
    monkeypatch.setenv("LEAF_CALLBACK_URL", "https://example.test/da/callback")
    assert broker._run_live_tool(da, "drawing.dwg", {"name": "tool"}, {}) == {"ok": True}
    assert da.calls[-1] == ("callback", "https://example.test/da/callback")

    response = broker.broker_reap(broker.BrokerReapRequest(
        records=[{"status": "submitted", "workitem_id": "wi-1", "session_closed": True}],
        live=True,
    ))
    assert response.status_code == 200
    assert ("reap", "wi-1") in da.calls


def test_replayed_job_nonce_cannot_complete_twice():
    body = b'{"job_id":"job-3","status":"success"}'
    headers = {"X-Leaf-Signature": _signed(body), "X-Leaf-Timestamp": str(time.time()),
               "X-Leaf-Nonce": "nonce-3"}
    client = TestClient(broker.app)
    first = client.post("/da/callback", content=body, headers=headers)
    second = client.post("/da/callback", content=body, headers=headers)
    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["message"] == "callback rejected: replay"
