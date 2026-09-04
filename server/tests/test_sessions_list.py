"""
Binary acceptance for GET /api/sessions (standardization slice 6b), the
surface an Opus review of head a144cd14 found with ZERO server-side test
coverage (`server/tests` had no match for scope_handle, SCOPE_KINDS,
_decode_cursor, next_cursor, or list_sessions on this route). Every test here
maps to a numbered finding from that review.

Hermetic: in-process TestClient wrapping ONLY routers/sessions.py, same
posture as tests/test_sessions_routes.py (SESSIONS_DB redirected to a fresh
tmp path before session_store's first import).

Run:  cd server && python -m pytest tests/test_sessions_list.py -q
"""
from __future__ import annotations

from contextlib import contextmanager
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict

_TMP_DIR = Path(tempfile.mkdtemp(prefix="sessions-list-test-"))
_TMP_DB = _TMP_DIR / "sessions.db"
os.environ.setdefault("SESSIONS_DB", str(_TMP_DB))
os.environ.setdefault("LEAF_AUTH_LIVE", "0")  # legacy X-Tenant-Id header stub

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import pytest  # noqa: E402

import session_store  # noqa: E402  (import after env redirect, by design)
from routers import sessions as sessions_router  # noqa: E402

_counter = [0]


def _new_drawing() -> str:
    _counter[0] += 1
    return f"drawing-list-{_counter[0]}"


@pytest.fixture()
def client():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from envelopes import install_error_handlers

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(sessions_router.router)
    return TestClient(app, raise_server_exceptions=False)


def _h(tenant: str) -> dict:
    return {"X-Tenant-Id": tenant}


def _attach(client, tenant: str, drawing_id: str, **extra) -> Dict[str, Any]:
    body = {"drawing_id": drawing_id, **extra}
    r = client.post("/api/sessions", json=body, headers=_h(tenant))
    assert r.status_code == 200, r.text
    return r.json()


def _post_turn(session_id: str, text: str) -> None:
    """A minimal turn_started event, bypassing the harness entirely -- the
    thing under test is session_store's title/turn_count bookkeeping, not
    turn dispatch."""
    session_store.append_event(session_id, f"turn-{time.time_ns()}", "turn_started",
                               {"text": text})


# =========================================================================== #
# Review finding 1: tenant isolation must hold on the LIST route exactly as
# it does on every per-session route. This is the regression guard for the
# WHERE-clause rewrite (finding 2) and the documented org-level list contract:
# it must go RED if a future change ever lets one tenant's rows leak into
# another tenant's page.
# =========================================================================== #
def test_list_never_crosses_tenants(client):
    mine = _attach(client, "tenant-list-a", _new_drawing())
    theirs = _attach(client, "tenant-list-b", _new_drawing())

    mine_page = client.get("/api/sessions", headers=_h("tenant-list-a")).json()
    their_page = client.get("/api/sessions", headers=_h("tenant-list-b")).json()

    mine_ids = {row["id"] for row in mine_page["sessions"]}
    their_ids = {row["id"] for row in their_page["sessions"]}
    assert mine["session_id"] in mine_ids
    assert theirs["session_id"] in their_ids
    assert mine["session_id"] not in their_ids
    assert theirs["session_id"] not in mine_ids


def test_list_scoped_to_one_tenant_even_with_many_sessions(client):
    tenant = "tenant-list-solo"
    other = "tenant-list-solo-other"
    seeded = [_attach(client, tenant, _new_drawing())["session_id"] for _ in range(3)]
    _attach(client, other, _new_drawing())

    page = client.get("/api/sessions", headers=_h(tenant)).json()
    listed = {row["id"] for row in page["sessions"]}
    assert set(seeded) <= listed


# =========================================================================== #
# Review finding 3: turn_count is maintained incrementally by append_event,
# never a per-row COUNT(*) at list time. This proves the number is CORRECT,
# which a query-shape change alone cannot.
# =========================================================================== #
def test_turn_count_reflects_turn_started_events_not_all_events(client):
    sess = _attach(client, "tenant-list-turns", _new_drawing())
    session_id = sess["session_id"]
    _post_turn(session_id, "first message")
    _post_turn(session_id, "second message")
    # A non-turn_started event must NOT move the counter.
    session_store.append_event(session_id, None, "turn_complete", {})

    page = client.get("/api/sessions", headers=_h("tenant-list-turns")).json()
    row = next(r for r in page["sessions"] if r["id"] == session_id)
    assert row["turn_count"] == 2


def test_title_is_first_turn_text_never_rewritten(client):
    sess = _attach(client, "tenant-list-title", _new_drawing())
    session_id = sess["session_id"]
    _post_turn(session_id, "the very first prompt")
    _post_turn(session_id, "a much later prompt")

    page = client.get("/api/sessions", headers=_h("tenant-list-title")).json()
    row = next(r for r in page["sessions"] if r["id"] == session_id)
    assert row["title"] == "the very first prompt"


# =========================================================================== #
# Review finding 9: a derived scope must never be presented as a recorded
# one.
# =========================================================================== #
def test_scope_recorded_false_for_a_pre_0053_style_row(client):
    sess = _attach(client, "tenant-list-scope", _new_drawing())
    page = client.get("/api/sessions", headers=_h("tenant-list-scope")).json()
    row = next(r for r in page["sessions"] if r["id"] == sess["session_id"])
    assert row["scope"]["recorded"] is False
    assert row["scope"]["kind"] == "drawing"


def test_scope_recorded_true_when_an_explicit_scope_was_attached(client):
    drawing = _new_drawing()
    sess = _attach(client, "tenant-list-scope-explicit", drawing,
                   scope={"kind": "drawing", "handle": drawing})
    page = client.get("/api/sessions", headers=_h("tenant-list-scope-explicit")).json()
    row = next(r for r in page["sessions"] if r["id"] == sess["session_id"])
    assert row["scope"]["recorded"] is True


# =========================================================================== #
# Review finding 10: a project scope query is canonicalized on read exactly
# as it is on write. Unit-level (no HTTP): the router function itself.
# =========================================================================== #
def test_parse_scope_query_canonicalizes_project_uuid_case():
    import uuid

    raw_uuid = uuid.uuid4()
    upper = f"project:{str(raw_uuid).upper()}"
    lower = f"project:{str(raw_uuid)}"
    kind_u, handle_u = sessions_router._parse_scope_query(upper)
    kind_l, handle_l = sessions_router._parse_scope_query(lower)
    assert kind_u == kind_l == "project"
    assert handle_u == handle_l == str(raw_uuid)


def test_parse_scope_query_rejects_a_non_uuid_project_handle():
    with pytest.raises(ValueError):
        sessions_router._parse_scope_query("project:not-a-uuid")


# =========================================================================== #
# Malformed input stays a 422, unaffected by the rewrite.
# =========================================================================== #
def test_list_malformed_scope_is_422(client):
    r = client.get("/api/sessions?scope=bogus", headers=_h("tenant-list-a"))
    assert r.status_code == 422, r.text


def test_list_malformed_cursor_is_422(client):
    r = client.get("/api/sessions?cursor=not-base64!!", headers=_h("tenant-list-a"))
    assert r.status_code == 422, r.text


def test_postgres_org_query_keeps_each_ordered_branch_parenthesized(monkeypatch):
    """The two index walks are legal PostgreSQL set-operation operands.

    ORDER BY/LIMIT before UNION ALL requires each SELECT to be parenthesized.
    Without these inner parentheses the real PostgreSQL route fails at UNION,
    even though the SQLite route and every pure mapping test stay green.
    """
    statements = []

    class Result:
        @staticmethod
        def fetchall():
            return []

    class Connection:
        @staticmethod
        def execute(statement, args=None):
            statements.append((statement, args))
            return Result()

    class Database:
        @staticmethod
        @contextmanager
        def transaction(**_kwargs):
            yield Connection()

    monkeypatch.setattr(session_store, "_platform_db", lambda: Database())
    session_store._pg_list_sessions("11111111-1111-4111-8111-111111111111")

    query = statements[-1][0]
    assert "SELECT * FROM ((SELECT" in query
    assert ") UNION ALL (SELECT" in query
    assert ")) merged ORDER BY" in query
