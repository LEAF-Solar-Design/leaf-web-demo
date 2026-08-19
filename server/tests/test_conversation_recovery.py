"""Tests for recovery-on-reconnect (card B-C3): resume a project conversation.

Two tiers, matching the convention in test_conversation_model.py:

* Structural/unit tests (no DATABASE_URL needed) exercise the router wiring,
  the flag gate, cursor encode/decode, the pagination-arithmetic in
  ``list_conversation_messages_page`` against a stubbed store, and the
  query's SQL text (ORDER BY / boundary / no-write) via source inspection.
  They always run.

* PostgreSQL-backed tests require an explicit DATABASE_URL and skip cleanly
  otherwise. These are the real end-to-end proof: multi-page walk equals a
  never-disconnected read, same-timestamp ties are total and dup/drop free,
  a revoked member gets the same-shape 403 as the existing list/get routes,
  and an empty/new project 200s with an empty page instead of 404.

Card B-C2 (the message write path) has not landed yet at the time this card
was built (files_expected has no shared table with it), so DB-backed
fixtures insert message rows directly via raw SQL against the 0048
migration's schema rather than through a write API that does not exist yet.
"""
from __future__ import annotations

import base64
import datetime
import inspect
import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SERVER_DIR.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import conversations  # noqa: E402
import deps  # noqa: E402
import platform_link  # noqa: E402
import routers.conversations as recovery_router_mod  # noqa: E402

requires_database = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="PostgreSQL conversation recovery test requires explicit DATABASE_URL",
)


class _Tenant(str):
    subject: str = ""


# --- router wiring: no shadow risk regardless of include_router order ------ #

def test_recovery_route_has_a_distinct_segment_count_from_sibling_routes() -> None:
    """The router-order-shadowing trap: /conversations/{conversation_id} and
    /conversations/recover (both in conversations.router) are ONE segment
    after "conversations"; this route is TWO, so no path-template regex can
    ever capture, or be captured by, either of them regardless of which
    router app.include_router() mounts first."""
    recovery_route = next(
        r for r in recovery_router_mod.router.routes
        if r.path == recovery_router_mod.RECOVERY_PATH)
    assert recovery_route.path == (
        "/api/projects/{project_id}/conversations/recovery/tail"
    )
    sibling_paths = [r.path for r in conversations.router.routes]
    assert "/api/projects/{project_id}/conversations/{conversation_id}" in sibling_paths
    assert "/api/projects/{project_id}/conversations/recover" in sibling_paths

    def _tail_after_conversations(path: str) -> List[str]:
        return path.split("/conversations", 1)[1].strip("/").split("/")

    recovery_tail = _tail_after_conversations(recovery_route.path)
    for sibling in sibling_paths:
        sibling_tail = _tail_after_conversations(sibling)
        assert len(recovery_tail) != len(sibling_tail), (
            f"{recovery_route.path!r} and {sibling!r} share a segment count "
            "and could shadow each other depending on registration order"
        )


def test_recovery_route_answers_get_and_post_not_a_bare_405() -> None:
    """The flag-off envelope negative control is not guaranteed to know this
    route is GET-only; registering POST too means a flag-off probe on either
    verb gets the disabled 404, never a framework 405."""
    recovery_route = next(
        r for r in recovery_router_mod.router.routes
        if r.path == recovery_router_mod.RECOVERY_PATH)
    assert recovery_route.methods >= {"GET", "POST"}


# --- cursor encode/decode --------------------------------------------------- #

def test_cursor_round_trips_created_at_and_message_id() -> None:
    created_at = datetime.datetime(2026, 8, 19, 12, 30, 5, 123456,
                                    tzinfo=datetime.timezone.utc)
    message_id = uuid.uuid4()
    cursor = conversations.encode_recovery_cursor(created_at, message_id)
    decoded_created_at, decoded_message_id = conversations.decode_recovery_cursor(cursor)
    assert decoded_created_at == created_at
    assert decoded_message_id == message_id


@pytest.mark.parametrize("garbage", [
    "not-base64-!!!",
    base64.urlsafe_b64encode(b"missing-separator").decode("ascii"),
    base64.urlsafe_b64encode(b"2026-08-19|not-a-uuid").decode("ascii"),
    base64.urlsafe_b64encode(b"not-a-date|" + str(uuid.uuid4()).encode()).decode("ascii"),
    "",
])
def test_cursor_rejects_malformed_input(garbage: str) -> None:
    with pytest.raises(conversations.InvalidRecoveryCursor):
        conversations.decode_recovery_cursor(garbage)


# --- query text: static proof the read path can't tie/dup/leak/write ------- #

def test_message_page_query_is_ordered_tiebroken_exclusive_and_bounded() -> None:
    """Reviewer-grep-style static assertions against the real function source
    (not a copy), so a future edit that silently drops the id tiebreak or
    switches to an inclusive boundary fails this test even without a DB."""
    src = inspect.getsource(conversations.list_conversation_messages_page)
    assert "ORDER BY created_at ASC, message_id ASC" in src
    assert "(created_at, message_id) > (%s, %s)" in src
    assert "OFFSET" not in src
    assert "FOR UPDATE" not in src
    assert "LIMIT %s" in src
    for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM", "ON CONFLICT"):
        assert verb not in src.upper()


def test_message_page_query_configures_a_server_side_statement_timeout() -> None:
    src = inspect.getsource(conversations.list_conversation_messages_page)
    assert "_configure_recovery_statement_timeout(cur)" in src
    timeout_src = inspect.getsource(conversations._configure_recovery_statement_timeout)
    assert "set_config('statement_timeout'" in timeout_src
    assert ", true)" in timeout_src


def test_recover_conversation_tail_never_creates_a_conversation() -> None:
    src = inspect.getsource(conversations.recover_conversation_tail)
    assert "create_conversation" not in src
    assert "recover_or_create_conversation" not in src


# --- pagination arithmetic against a stubbed store (no DB required) -------- #

class _FakeCursor:
    def __init__(self, rows_provider):
        self._rows_provider = rows_provider
        self._last_rows: List[Dict[str, Any]] = []

    def execute(self, sql: str, params: tuple) -> None:
        self._last_rows = self._rows_provider(sql, params)

    def fetchall(self) -> List[Dict[str, Any]]:
        return self._last_rows


class _FakeConn:
    def __init__(self, rows_provider):
        self._rows_provider = rows_provider

    def __enter__(self):
        return _FakeCursor(self._rows_provider)

    def __exit__(self, *exc):
        return False


class _FakeDb:
    """Stands in for platform_db(): its cursor() replays exactly what a real
    (org_id, project_id, conversation_id, created_at ASC, message_id ASC)
    scan with an exclusive (created_at, message_id) boundary would return,
    computed here in pure Python against a fixed in-memory row set -- this
    proves list_conversation_messages_page's has_more/next_cursor/slicing
    arithmetic; the SQL text itself is proven separately (static test above)
    and, end to end, by the DATABASE_URL-gated tests below."""

    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = rows

    def cursor(self):
        def rows_provider(sql, params):
            if "statement_timeout" in sql:
                return []
            if "idempotency_key = ANY(%s)" in sql:
                # The poison-fence receipt lookup (one bounded statement per
                # page): resolve receipts by key against the stubbed rows.
                receipt_keys = set(params[-1])
                return [
                    r for r in self._rows
                    if r["idempotency_key"] in receipt_keys
                ]
            if "(created_at, message_id) > (%s, %s)" in sql:
                *_scope, after_created_at, after_message_id, limit_plus_one = params
                ordered = sorted(self._rows, key=lambda r: (r["created_at"], str(r["message_id"])))
                filtered = [
                    r for r in ordered
                    if (r["created_at"], str(r["message_id"]))
                    > (after_created_at, str(after_message_id))
                ]
                return filtered[:limit_plus_one]
            *_scope, limit_plus_one = params
            ordered = sorted(self._rows, key=lambda r: (r["created_at"], str(r["message_id"])))
            return ordered[:limit_plus_one]
        return _FakeConn(rows_provider)


def _message(conversation_id: str, created_at: datetime.datetime, **overrides) -> Dict[str, Any]:
    row = {
        "message_id": uuid.uuid4(),
        "org_id": uuid.uuid4(),
        "project_id": uuid.uuid4(),
        "conversation_id": conversation_id,
        "content": "hello",
        "metadata": {},
        "created_by_binding_id": uuid.uuid4(),
        "idempotency_key": uuid.uuid4().hex,
        "created_at": created_at,
    }
    row.update(overrides)
    return row


def test_full_walk_over_stubbed_store_concats_to_every_row_no_dup_no_drop(monkeypatch) -> None:
    base = datetime.datetime(2026, 8, 19, 0, 0, 0, tzinfo=datetime.timezone.utc)
    conversation_id = str(uuid.uuid4())
    rows = [_message(conversation_id, base + datetime.timedelta(seconds=i)) for i in range(23)]
    fake_db = _FakeDb(rows)
    monkeypatch.setattr(conversations, "platform_db", lambda: fake_db)

    seen_ids: List[str] = []
    cursor = None
    pages = 0
    while True:
        items, _gaps, cursor, has_more = conversations.list_conversation_messages_page(
            "org", "project", conversation_id,
            after=conversations.decode_recovery_cursor(cursor) if cursor else None,
            limit=7,
        )
        seen_ids.extend(item["message_id"] for item in items)
        pages += 1
        assert pages < 20, "pagination did not converge"
        if not has_more:
            assert cursor is None
            break

    expected_ids = [str(r["message_id"]) for r in
                     sorted(rows, key=lambda r: (r["created_at"], str(r["message_id"])))]
    assert seen_ids == expected_ids, "reconnect walk must equal a never-disconnected read"
    assert len(set(seen_ids)) == len(seen_ids), "no row may be delivered twice"


def test_same_timestamp_burst_is_a_total_order_across_a_page_boundary(monkeypatch) -> None:
    """created_at ties (a same-transaction burst) must not dup/drop across a
    page split -- the id tiebreak is what keeps the ORDER total."""
    tie = datetime.datetime(2026, 8, 19, 0, 0, 0, tzinfo=datetime.timezone.utc)
    conversation_id = str(uuid.uuid4())
    rows = [_message(conversation_id, tie) for _ in range(9)]
    fake_db = _FakeDb(rows)
    monkeypatch.setattr(conversations, "platform_db", lambda: fake_db)

    page1, _g1, cursor1, has_more1 = conversations.list_conversation_messages_page(
        "org", "project", conversation_id, after=None, limit=4)
    assert has_more1 is True
    assert len(page1) == 4
    page2, _g2, cursor2, has_more2 = conversations.list_conversation_messages_page(
        "org", "project", conversation_id,
        after=conversations.decode_recovery_cursor(cursor1), limit=4)
    assert len(page2) == 4
    page3, _g3, cursor3, has_more3 = conversations.list_conversation_messages_page(
        "org", "project", conversation_id,
        after=conversations.decode_recovery_cursor(cursor2), limit=4)
    assert has_more3 is False
    assert cursor3 is None
    assert len(page3) == 1

    all_ids = [i["message_id"] for i in page1 + page2 + page3]
    assert len(all_ids) == 9
    assert len(set(all_ids)) == 9, "a tie boundary must not duplicate a row"
    expected_order = [str(r["message_id"]) for r in
                       sorted(rows, key=lambda r: str(r["message_id"]))]
    assert all_ids == expected_order


def test_cursor_boundary_is_exclusive_never_replays_the_cursor_row(monkeypatch) -> None:
    base = datetime.datetime(2026, 8, 19, 0, 0, 0, tzinfo=datetime.timezone.utc)
    conversation_id = str(uuid.uuid4())
    rows = [_message(conversation_id, base + datetime.timedelta(seconds=i)) for i in range(3)]
    fake_db = _FakeDb(rows)
    monkeypatch.setattr(conversations, "platform_db", lambda: fake_db)

    page1, _g1, cursor1, _ = conversations.list_conversation_messages_page(
        "org", "project", conversation_id, after=None, limit=1)
    last_seen_id = page1[0]["message_id"]

    page2, _g2, _cursor2, _ = conversations.list_conversation_messages_page(
        "org", "project", conversation_id,
        after=conversations.decode_recovery_cursor(cursor1), limit=10)
    assert last_seen_id not in [i["message_id"] for i in page2], (
        "an inclusive boundary would replay the cursor row on every reconnect"
    )


def test_empty_new_project_recovers_an_empty_page_not_404(monkeypatch) -> None:
    """Oracle line 3, proven at the pure-function level (no conversation row
    yet -> empty envelope, never a lazy-create, never a 404)."""
    monkeypatch.setattr(conversations, "list_conversations", lambda org_id, project_id: [])
    monkeypatch.setattr(
        conversations, "platform_db",
        lambda: (_ for _ in ()).throw(AssertionError("must not query messages for a project with no conversation")),
    )
    page = conversations.recover_conversation_tail("org-1", "project-1")
    assert page == {"items": [], "gaps": [], "next_cursor": None, "has_more": False}


# --- router-level behavior via a stub app (no DB required) ----------------- #

@pytest.fixture
def stub_client(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(recovery_router_mod.router)
    tenant = _Tenant("org-1")
    tenant.subject = "auth0|stub-subject"
    app.dependency_overrides[deps.require_active_tenant] = lambda: tenant
    return TestClient(app), tenant


def test_flag_off_denies_get_and_post_with_the_disabled_shape(stub_client, monkeypatch) -> None:
    client, _tenant = stub_client
    monkeypatch.delenv(conversations.FLAG_CONV_DURABLE, raising=False)

    def _must_not_be_called(*a, **k):
        raise AssertionError("flag-off must never reach project resolution")
    monkeypatch.setattr(conversations, "_require_project", _must_not_be_called)

    for verb in ("get", "post"):
        resp = getattr(client, verb)(
            "/api/projects/proj-1/conversations/recovery/tail")
        assert resp.status_code == 404
        assert resp.json()["error"]["error_code"] == "BAD_PARAMS"


def test_revoked_membership_gets_the_identical_forbidden_body_as_other_reads(
    stub_client, monkeypatch,
) -> None:
    client, _tenant = stub_client
    monkeypatch.setenv(conversations.FLAG_CONV_DURABLE, "1")

    def _forbidden_lookup(tenant, project_id, *, write):
        raise platform_link.ProjectSessionForbidden("revoked")
    monkeypatch.setattr(conversations, "_require_project", _forbidden_lookup)

    resp = client.get("/api/projects/proj-1/conversations/recovery/tail")
    assert resp.status_code == 403
    baseline = conversations._forbidden()
    import json
    assert resp.json() == json.loads(bytes(baseline.body))


def test_missing_or_cross_tenant_project_gets_the_identical_not_found_body(
    stub_client, monkeypatch,
) -> None:
    client, _tenant = stub_client
    monkeypatch.setenv(conversations.FLAG_CONV_DURABLE, "1")
    monkeypatch.setattr(
        conversations, "_require_project", lambda tenant, project_id, *, write: None)

    resp = client.get("/api/projects/proj-1/conversations/recovery/tail")
    assert resp.status_code == 404
    baseline = conversations._not_found()
    import json
    assert resp.json() == json.loads(bytes(baseline.body))


def test_empty_project_returns_200_empty_envelope_through_the_route(
    stub_client, monkeypatch,
) -> None:
    client, _tenant = stub_client
    monkeypatch.setenv(conversations.FLAG_CONV_DURABLE, "1")
    monkeypatch.setattr(
        conversations, "_require_project", lambda tenant, project_id, *, write: "org-1")
    monkeypatch.setattr(conversations, "list_conversations", lambda org_id, project_id: [])

    resp = client.get("/api/projects/proj-1/conversations/recovery/tail")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["next_cursor"] is None
    assert body["has_more"] is False
    assert body["error"] is None


def test_default_and_out_of_range_limit_is_clamped_by_query_validation(
    stub_client, monkeypatch,
) -> None:
    client, _tenant = stub_client
    monkeypatch.setenv(conversations.FLAG_CONV_DURABLE, "1")
    monkeypatch.setattr(
        conversations, "_require_project", lambda tenant, project_id, *, write: "org-1")

    seen_limits: List[int] = []

    def fake_recover(org_id, project_id, *, cursor=None, limit=conversations.RECOVERY_DEFAULT_LIMIT):
        seen_limits.append(limit)
        return {"items": [], "gaps": [], "next_cursor": None, "has_more": False}
    monkeypatch.setattr(conversations, "recover_conversation_tail", fake_recover)

    resp = client.get("/api/projects/proj-1/conversations/recovery/tail")
    assert resp.status_code == 200
    assert seen_limits[-1] == conversations.RECOVERY_DEFAULT_LIMIT

    for bad_limit in (0, -1, conversations.RECOVERY_MAX_LIMIT + 1, 10_000_000):
        resp = client.get(
            "/api/projects/proj-1/conversations/recovery/tail",
            params={"limit": bad_limit},
        )
        assert resp.status_code == 422, f"limit={bad_limit} must be rejected, not silently clamped"

    resp = client.get(
        "/api/projects/proj-1/conversations/recovery/tail",
        params={"limit": conversations.RECOVERY_MAX_LIMIT},
    )
    assert resp.status_code == 200
    assert seen_limits[-1] == conversations.RECOVERY_MAX_LIMIT


def test_malformed_cursor_is_a_typed_400_not_a_500(stub_client, monkeypatch) -> None:
    client, _tenant = stub_client
    monkeypatch.setenv(conversations.FLAG_CONV_DURABLE, "1")
    monkeypatch.setattr(
        conversations, "_require_project", lambda tenant, project_id, *, write: "org-1")

    resp = client.get(
        "/api/projects/proj-1/conversations/recovery/tail",
        params={"cursor": "not-a-valid-cursor!!"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["error_code"] == "BAD_PARAMS"


# --- PostgreSQL-backed: the real end-to-end proof --------------------------- #

@pytest.fixture
def pg(monkeypatch):
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("PostgreSQL conversation recovery test requires DATABASE_URL")
    db = conversations.platform_db()
    db.apply_migration()
    monkeypatch.setenv(conversations.FLAG_CONV_DURABLE, "1")
    yield db
    db.reset_pool()


def _seed_member(pg_db, org: Any, project: Any, subject: str, *,
                  status: str = "active") -> str:
    binding_id = str(uuid.uuid4())
    with pg_db.cursor() as cur:
        cur.execute(
            "INSERT INTO identity_bindings"
            " (binding_id, external_authority, external_subject,"
            "  platform_tenant_id, platform_user_id, role)"
            " VALUES (%s, 'auth0', %s, %s, %s, 'owner')",
            (binding_id, subject, str(org.org_id), str(uuid.uuid4())),
        )
        cur.execute(
            "INSERT INTO project_member_bindings"
            " (membership_id, org_id, project_id, binding_id, role, status,"
            "  invited_by_binding_id, revoked_at)"
            " VALUES (%s, %s, %s, %s, 'owner', %s, %s,"
            "         CASE WHEN %s = 'revoked' THEN NOW() ELSE NULL END)",
            (str(uuid.uuid4()), str(org.org_id), str(project.project_id),
             binding_id, status, binding_id, status),
        )
    return binding_id


def _insert_message(pg_db, org_id: str, project_id: str, conversation_id: str,
                     created_by_binding_id: str, created_at: datetime.datetime,
                     content: str = "hi") -> str:
    message_id = str(uuid.uuid4())
    with pg_db.cursor() as cur:
        cur.execute(
            "INSERT INTO conversation_messages"
            " (message_id, org_id, project_id, conversation_id, content,"
            "  created_by_binding_id, idempotency_key, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (message_id, org_id, project_id, conversation_id, content,
             created_by_binding_id, uuid.uuid4().hex, created_at),
        )
    return message_id


@requires_database
def test_pg_multi_page_walk_reconstructs_identical_state_to_never_disconnected(pg) -> None:
    store = conversations.platform_store()
    org = store.create_org_with_identity(
        f"conv-recov-org-{uuid.uuid4().hex[:8]}", "auth0", f"auth0|{uuid.uuid4().hex}")
    project = store.create_project(org.org_id, "conv-recov-project")
    subject = f"auth0|{uuid.uuid4().hex}"
    binding_id = _seed_member(pg, org, project, subject)

    conversation = conversations.create_conversation(
        str(org.org_id), str(project.project_id), binding_id, "resume me")

    base = datetime.datetime.now(datetime.timezone.utc)
    expected_ids = []
    for i in range(37):
        mid = _insert_message(
            pg, str(org.org_id), str(project.project_id),
            conversation["conversation_id"], binding_id,
            base + datetime.timedelta(milliseconds=i))
        expected_ids.append(mid)

    tenant = _Tenant(str(org.org_id))
    tenant.subject = subject

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(recovery_router_mod.router)
    app.dependency_overrides[deps.require_active_tenant] = lambda: tenant
    client = TestClient(app)

    seen_ids: List[str] = []
    cursor = None
    for _ in range(50):
        resp = client.get(
            f"/api/projects/{project.project_id}/conversations/recovery/tail",
            params={"limit": 9, **({"cursor": cursor} if cursor else {})},
        )
        assert resp.status_code == 200
        body = resp.json()
        seen_ids.extend(item["message_id"] for item in body["items"])
        cursor = body["next_cursor"]
        if not body["has_more"]:
            assert cursor is None
            break
    else:
        raise AssertionError("pagination did not converge")

    assert seen_ids == expected_ids
    assert len(set(seen_ids)) == len(seen_ids)

    # A never-disconnected client's state: every row, same order, direct query.
    never_disconnected_items, _cursor, _has_more = conversations.list_conversation_messages_page(
        str(org.org_id), str(project.project_id), conversation["conversation_id"],
        after=None, limit=1000,
    )
    assert [m["message_id"] for m in never_disconnected_items] == expected_ids


@requires_database
def test_pg_revoked_member_recovery_denies_with_same_shape_403_as_list_route(pg) -> None:
    store = conversations.platform_store()
    org = store.create_org_with_identity(
        f"conv-revoke-org-{uuid.uuid4().hex[:8]}", "auth0", f"auth0|{uuid.uuid4().hex}")
    project = store.create_project(org.org_id, "conv-revoke-project")
    subject = f"auth0|{uuid.uuid4().hex}"
    _seed_member(pg, org, project, subject, status="revoked")

    tenant = _Tenant(str(org.org_id))
    tenant.subject = subject

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(recovery_router_mod.router)
    app.include_router(conversations.router)
    app.dependency_overrides[deps.require_active_tenant] = lambda: tenant
    client = TestClient(app)

    recovery_resp = client.get(
        f"/api/projects/{project.project_id}/conversations/recovery/tail")
    list_resp = client.get(f"/api/projects/{project.project_id}/conversations")

    assert recovery_resp.status_code == 403
    assert list_resp.status_code == 403
    assert recovery_resp.json() == list_resp.json(), (
        "revoked-member recovery must deny with the SAME-SHAPE 403 as other "
        "conversation lifecycle reads"
    )


@requires_database
def test_pg_empty_new_project_returns_200_empty_page_not_404(pg) -> None:
    store = conversations.platform_store()
    org = store.create_org_with_identity(
        f"conv-newproj-org-{uuid.uuid4().hex[:8]}", "auth0", f"auth0|{uuid.uuid4().hex}")
    project = store.create_project(org.org_id, "conv-newproj-project")
    subject = f"auth0|{uuid.uuid4().hex}"
    _seed_member(pg, org, project, subject)

    tenant = _Tenant(str(org.org_id))
    tenant.subject = subject

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(recovery_router_mod.router)
    app.dependency_overrides[deps.require_active_tenant] = lambda: tenant
    client = TestClient(app)

    resp = client.get(f"/api/projects/{project.project_id}/conversations/recovery/tail")
    assert resp.status_code == 200
    body = resp.json()
    assert body["items"] == []
    assert body["next_cursor"] is None
    assert body["has_more"] is False

    with pg.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM conversations WHERE org_id = %s AND project_id = %s",
            (str(org.org_id), str(project.project_id)),
        )
        assert cur.fetchone()["n"] == 0, "a recovery GET must never lazily create a conversation"


@requires_database
def test_pg_flag_off_denies_get_and_post_and_writes_nothing(pg) -> None:
    store = conversations.platform_store()
    org = store.create_org_with_identity(
        f"conv-flagoff-org-{uuid.uuid4().hex[:8]}", "auth0", f"auth0|{uuid.uuid4().hex}")
    project = store.create_project(org.org_id, "conv-flagoff-project")
    subject = f"auth0|{uuid.uuid4().hex}"
    binding_id = _seed_member(pg, org, project, subject)
    conversation = conversations.create_conversation(
        str(org.org_id), str(project.project_id), binding_id, "seed")
    _insert_message(pg, str(org.org_id), str(project.project_id),
                     conversation["conversation_id"], binding_id,
                     datetime.datetime.now(datetime.timezone.utc))

    tenant = _Tenant(str(org.org_id))
    tenant.subject = subject

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(recovery_router_mod.router)
    app.dependency_overrides[deps.require_active_tenant] = lambda: tenant
    client = TestClient(app)

    os.environ.pop(conversations.FLAG_CONV_DURABLE, None)
    for verb in ("get", "post"):
        resp = getattr(client, verb)(
            f"/api/projects/{project.project_id}/conversations/recovery/tail")
        assert resp.status_code == 404

    with pg.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM conversation_messages WHERE project_id = %s",
            (str(project.project_id),),
        )
        assert cur.fetchone()["n"] == 1, "flag-off recovery must not write or delete anything"


@requires_database
def test_pg_tampered_cross_project_cursor_never_leaks_another_projects_rows(pg) -> None:
    store = conversations.platform_store()

    org_a = store.create_org_with_identity(
        f"conv-tamper-a-{uuid.uuid4().hex[:8]}", "auth0", f"auth0|{uuid.uuid4().hex}")
    project_a = store.create_project(org_a.org_id, "conv-tamper-project-a")
    subject_a = f"auth0|{uuid.uuid4().hex}"
    binding_a = _seed_member(pg, org_a, project_a, subject_a)
    conversation_a = conversations.create_conversation(
        str(org_a.org_id), str(project_a.project_id), binding_a, "a")
    base = datetime.datetime.now(datetime.timezone.utc)
    id_a1 = _insert_message(pg, str(org_a.org_id), str(project_a.project_id),
                             conversation_a["conversation_id"], binding_a, base)
    id_a2 = _insert_message(pg, str(org_a.org_id), str(project_a.project_id),
                             conversation_a["conversation_id"], binding_a,
                             base + datetime.timedelta(seconds=10))

    org_b = store.create_org_with_identity(
        f"conv-tamper-b-{uuid.uuid4().hex[:8]}", "auth0", f"auth0|{uuid.uuid4().hex}")
    project_b = store.create_project(org_b.org_id, "conv-tamper-project-b")
    subject_b = f"auth0|{uuid.uuid4().hex}"
    binding_b = _seed_member(pg, org_b, project_b, subject_b)
    conversation_b = conversations.create_conversation(
        str(org_b.org_id), str(project_b.project_id), binding_b, "b")
    id_b1 = _insert_message(pg, str(org_b.org_id), str(project_b.project_id),
                             conversation_b["conversation_id"], binding_b,
                             base + datetime.timedelta(seconds=5))

    # A cursor minted from project B's timeline, replayed against project A.
    foreign_cursor = conversations.encode_recovery_cursor(
        base + datetime.timedelta(seconds=5), uuid.UUID(id_b1))

    tenant_a = _Tenant(str(org_a.org_id))
    tenant_a.subject = subject_a

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(recovery_router_mod.router)
    app.dependency_overrides[deps.require_active_tenant] = lambda: tenant_a
    client = TestClient(app)

    resp = client.get(
        f"/api/projects/{project_a.project_id}/conversations/recovery/tail",
        params={"cursor": foreign_cursor},
    )
    assert resp.status_code == 200
    returned_ids = {item["message_id"] for item in resp.json()["items"]}
    assert id_b1 not in returned_ids
    assert returned_ids <= {id_a1, id_a2}
    # project A's own row at t+10s is still correctly past the t+5s boundary.
    assert id_a2 in returned_ids
