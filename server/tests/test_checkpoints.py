"""Acceptance tests for session checkpoint metadata routes.

The session and checkpoint stores read SESSIONS_DB once at import, so redirect
it before importing either module to keep the real server database untouched.
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

_TMP_DIR = Path(tempfile.mkdtemp(prefix="checkpoints-test-"))
os.environ.setdefault("SESSIONS_DB", str(_TMP_DIR / "sessions.db"))
os.environ.setdefault("LEAF_AUTH_LIVE", "0")

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import pytest  # noqa: E402

import session_store  # noqa: E402
from routers import checkpoints as checkpoints_router  # noqa: E402


@pytest.fixture()
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from envelopes import install_error_handlers

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(checkpoints_router.router)
    return TestClient(app, raise_server_exceptions=False)


# Captured BEFORE the autouse stub below replaces the module attribute, so the
# one test that exercises the REAL resolver can still reach it. Without this,
# the stub shadowed the function under test and the "real head" test was
# asserting against the stub's constant — a test of nothing.
_REAL_DRAWING_VERSION = checkpoints_router._drawing_version


@pytest.fixture(autouse=True)
def drawing_version(monkeypatch):
    monkeypatch.setattr(checkpoints_router, "_drawing_version", lambda tenant, drawing: 7)


_counter = [0]


def _h(tenant: str) -> dict:
    return {"X-Tenant-Id": tenant}


def _session(tenant: str = "tenant-a") -> dict:
    _counter[0] += 1
    return session_store.get_or_create_session(tenant, f"checkpoint-drawing-{_counter[0]}")


def _create(client, session: dict, label=None):
    body = {} if label is None else {"label": label}
    return client.post(
        f"/api/sessions/{session['session_id']}/checkpoints",
        json=body,
        headers=_h(session["tenant_id"]),
    )


def test_create_and_list_checkpoint_round_trip(client):
    session = _session()
    created = _create(client, session, "before edit")
    assert created.status_code == 201, created.text
    checkpoint = created.json()
    assert checkpoint["session_id"] == session["session_id"]
    assert checkpoint["drawing_id"] == session["drawing_id"]
    assert checkpoint["drawing_version"] == "7"
    assert checkpoint["transcript_seq"] == 0
    assert checkpoint["label"] == "before edit"

    listed = client.get(
        f"/api/sessions/{session['session_id']}/checkpoints",
        headers=_h(session["tenant_id"]),
    )
    assert listed.status_code == 200, listed.text
    assert listed.json()["checkpoints"] == [
        {key: checkpoint[key] for key in checkpoint if key not in {"error", "degraded_mode"}}
    ]


def test_cross_tenant_checkpoint_access_matches_unknown_session_404(client):
    session = _session("tenant-owner")
    unknown = client.get("/api/sessions/no-such-session/checkpoints", headers=_h("tenant-intruder"))
    foreign = client.get(
        f"/api/sessions/{session['session_id']}/checkpoints",
        headers=_h("tenant-intruder"),
    )
    assert unknown.status_code == foreign.status_code == 404
    assert unknown.json()["error"]["error_code"] == foreign.json()["error"]["error_code"] == "session_not_found"


def test_checkpoint_records_transcript_sequence_at_creation(client):
    session = _session()
    for event_type in ("turn_started", "text_delta", "turn_complete"):
        session_store.append_event(session["session_id"], "turn-1", event_type, {})

    created = _create(client, session)
    assert created.status_code == 201, created.text
    assert created.json()["transcript_seq"] == 3


def test_checkpoint_cap_and_label_limit(client):
    session = _session()
    for _ in range(50):
        created = _create(client, session)
        assert created.status_code == 201, created.text

    overflow = _create(client, session)
    assert overflow.status_code == 409, overflow.text
    assert "50 checkpoints" in overflow.json()["error"]["message"]

    other_session = _session()
    too_long = _create(client, other_session, "x" * 201)
    assert too_long.status_code == 400, too_long.text


def test_drawing_version_reads_the_current_head_without_bootstrapping(monkeypatch):
    backend = object()
    calls = []
    monkeypatch.setattr(
        checkpoints_router.write_loop,
        "backend_for_tenant",
        lambda *args, **kwargs: backend,
    )
    monkeypatch.setitem(
        sys.modules,
        "store",
        types.SimpleNamespace(
            load_manifest=lambda got_backend, tenant, drawing: calls.append(
                (got_backend, tenant, drawing)) or {"head": 9},
        ),
    )

    assert _REAL_DRAWING_VERSION("tenant-a", "drawing-a") == 9
    assert calls == [(backend, "tenant-a", "drawing-a")]
