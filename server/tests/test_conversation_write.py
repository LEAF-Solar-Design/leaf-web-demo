"""Tests for the conversation message write path (B-C2).

Structural and validation-layer checks always run. Everything that needs a
project-authority read or the durable INSERT requires DATABASE_URL and skips
cleanly without one, matching the convention in test_conversation_model.py,
test_drawing_upload_authority_postgres.py, and test_agent_gate_postgres.py.

The `conversation_messages` table exists via migration 0048 (card B-C1b,
PR #709); the durable write itself is B-C4's create_message (merged first,
same DO NOTHING + replay-SELECT design this card converged on), and this
card contributes the HTTP surface: mount, typed validation rail, route time
budget, and these tests. Tests marked requires_database exercise the real
INSERT and skip cleanly without DATABASE_URL.
"""
from __future__ import annotations

import inspect
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import conversations  # noqa: E402
import deps  # noqa: E402
import platform_link  # noqa: E402
from routers import conversations as conversations_router  # noqa: E402

requires_database = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="PostgreSQL conversation message write test requires explicit DATABASE_URL",
)


# --- pure validation: content byte cap -------------------------------------- #

def test_validate_message_content_accepts_exact_boundary_32768_bytes() -> None:
    content = "a" * conversations.MAX_MESSAGE_CONTENT_BYTES
    assert len(content.encode("utf-8")) == conversations.MAX_MESSAGE_CONTENT_BYTES
    assert conversations.validate_message_content(content) == content


def test_validate_message_content_rejects_32769_bytes() -> None:
    content = "a" * (conversations.MAX_MESSAGE_CONTENT_BYTES + 1)
    with pytest.raises(conversations.MessageRejected) as excinfo:
        conversations.validate_message_content(content)
    assert excinfo.value.status_code == 413


def test_validate_message_content_multibyte_straddle_counts_bytes_not_chars() -> None:
    """10k 4-byte emoji is 10k *characters* (well under a naive char-count
    cap) but 40KB once UTF-8 encoded -- must reject on the real byte size."""
    emoji_count = 10_000
    content = "\U0001F600" * emoji_count
    assert len(content) == emoji_count  # char count alone looks safe
    assert len(content.encode("utf-8")) == emoji_count * 4  # 40,000 bytes
    with pytest.raises(conversations.MessageRejected) as excinfo:
        conversations.validate_message_content(content)
    assert excinfo.value.status_code == 413


def test_validate_message_content_boundary_multibyte_exactly_fits() -> None:
    """8192 4-byte characters is exactly 32768 bytes -- must pass."""
    content = "\U0001F600" * (conversations.MAX_MESSAGE_CONTENT_BYTES // 4)
    assert len(content.encode("utf-8")) == conversations.MAX_MESSAGE_CONTENT_BYTES
    assert conversations.validate_message_content(content) == content


def test_validate_message_content_rejects_empty_string() -> None:
    with pytest.raises(conversations.MessageRejected) as excinfo:
        conversations.validate_message_content("")
    assert excinfo.value.status_code == 422


def test_validate_message_content_rejects_non_string() -> None:
    with pytest.raises(conversations.MessageRejected) as excinfo:
        conversations.validate_message_content(12345)
    assert excinfo.value.status_code == 422


# --- pure validation: metadata allowlist ------------------------------------ #

def test_validate_message_metadata_allows_allowlisted_keys() -> None:
    metadata = {"role": "user", "source": "web"}
    assert conversations.validate_message_metadata(metadata) == metadata


def test_validate_message_metadata_none_returns_empty_dict() -> None:
    assert conversations.validate_message_metadata(None) == {}


def test_validate_message_metadata_rejects_unknown_keys_outright() -> None:
    """Not silently stripped: an allowed key alongside an unknown one still
    rejects the whole request rather than quietly dropping the unknown one."""
    metadata = {"role": "user", "not_allowlisted": "x"}
    with pytest.raises(conversations.MessageRejected) as excinfo:
        conversations.validate_message_metadata(metadata)
    assert excinfo.value.status_code == 422
    assert "not_allowlisted" in str(excinfo.value)


def test_validate_message_metadata_rejects_non_dict() -> None:
    with pytest.raises(conversations.MessageRejected):
        conversations.validate_message_metadata(["role", "user"])


def test_validate_message_metadata_rejects_non_string_value() -> None:
    with pytest.raises(conversations.MessageRejected):
        conversations.validate_message_metadata({"role": 7})


def test_validate_message_metadata_rejects_oversized_value() -> None:
    metadata = {"role": "x" * (conversations.MAX_MESSAGE_METADATA_VALUE_BYTES + 1)}
    with pytest.raises(conversations.MessageRejected):
        conversations.validate_message_metadata(metadata)


def test_validate_message_metadata_rejects_nested_object() -> None:
    """Nested structures are rejected, not silently accepted unchecked."""
    with pytest.raises(conversations.MessageRejected):
        conversations.validate_message_metadata({"role": {"nested": "x"}})


# --- pure validation: idempotency key ---------------------------------------- #

def test_validate_idempotency_key_accepts_boundary_200_chars() -> None:
    key = "k" * conversations.MAX_IDEMPOTENCY_KEY_LENGTH
    assert conversations.validate_idempotency_key(key) == key


def test_validate_idempotency_key_rejects_201_chars() -> None:
    with pytest.raises(conversations.MessageRejected) as excinfo:
        conversations.validate_idempotency_key(
            "k" * (conversations.MAX_IDEMPOTENCY_KEY_LENGTH + 1))
    assert excinfo.value.status_code == 400


def test_validate_idempotency_key_rejects_missing() -> None:
    with pytest.raises(conversations.MessageRejected) as excinfo:
        conversations.validate_idempotency_key(None)
    assert excinfo.value.status_code == 400


def test_validate_idempotency_key_rejects_empty() -> None:
    with pytest.raises(conversations.MessageRejected):
        conversations.validate_idempotency_key("")


# --- API layer: flag fence, ordering, envelope shape ------------------------- #

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


def _url(project_id: str = "proj-1", conversation_id: str = "conv-1") -> str:
    return f"/api/projects/{project_id}/conversations/{conversation_id}/messages"


def _create_message(org_id, project_id, conversation_id, binding_id,
                    content, metadata, idempotency_key):
    """Call the merged write path (B-C4 signature), returning the
    (message, inserted) pair this suite's assertions were written against."""
    result = conversations.create_message(
        org_id, project_id, conversation_id, binding_id,
        idempotency_key=idempotency_key, content=content, metadata=metadata,
    )
    return result["message"], not result["replayed"]


def _never_write(monkeypatch) -> Dict[str, int]:
    """Fail the test loudly if create_message is ever reached."""
    calls = {"count": 0}

    def _spy(*_args: Any, **_kwargs: Any):
        calls["count"] += 1
        raise AssertionError("create_message must not be called on this path")

    monkeypatch.setattr(conversations, "create_message", _spy)
    return calls


def test_flag_off_returns_404_disabled_and_writes_nothing(monkeypatch) -> None:
    monkeypatch.delenv(conversations.FLAG_CONV_DURABLE, raising=False)
    calls = _never_write(monkeypatch)
    client, _tenant = _client(monkeypatch)
    resp = client.post(_url(), json={"content": "hi"},
                        headers={"Idempotency-Key": "k1"})
    assert resp.status_code == 404
    assert resp.json()["error"]["error_code"] == "BAD_PARAMS"
    assert calls["count"] == 0


def test_flag_off_with_malformed_body_still_answers_the_fence_shape(monkeypatch) -> None:
    """Combined probe (fence + malformed): the fence gate runs BEFORE body
    parsing, so a malformed body under a disabled flag still answers 404, not
    a validation 4xx that would leak that the route parses bodies at all."""
    monkeypatch.delenv(conversations.FLAG_CONV_DURABLE, raising=False)
    calls = _never_write(monkeypatch)
    client, _tenant = _client(monkeypatch)
    resp = client.post(
        _url(), content=b"{not valid json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 404
    assert calls["count"] == 0


def test_malformed_json_body_returns_typed_400_and_writes_nothing(monkeypatch) -> None:
    monkeypatch.setenv(conversations.FLAG_CONV_DURABLE, "1")
    calls = _never_write(monkeypatch)
    client, _tenant = _client(monkeypatch)
    resp = client.post(
        _url(), content=b"{not valid json",
        headers={"content-type": "application/json", "Idempotency-Key": "k1"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["error_code"] == "BAD_PARAMS"
    assert calls["count"] == 0


def test_non_object_json_body_returns_typed_400_and_writes_nothing(monkeypatch) -> None:
    monkeypatch.setenv(conversations.FLAG_CONV_DURABLE, "1")
    calls = _never_write(monkeypatch)
    client, _tenant = _client(monkeypatch)
    resp = client.post(_url(), json=["not", "an", "object"],
                        headers={"Idempotency-Key": "k1"})
    assert resp.status_code == 400
    assert calls["count"] == 0


def test_oversized_transport_body_returns_413_and_writes_nothing(monkeypatch) -> None:
    monkeypatch.setenv(conversations.FLAG_CONV_DURABLE, "1")
    calls = _never_write(monkeypatch)
    client, _tenant = _client(monkeypatch)
    huge = "a" * (conversations_router.MAX_REQUEST_BODY_BYTES + 1)
    resp = client.post(_url(), json={"content": huge},
                        headers={"Idempotency-Key": "k1"})
    assert resp.status_code == 413
    assert calls["count"] == 0


def test_oversized_content_field_returns_413_and_writes_nothing(monkeypatch) -> None:
    monkeypatch.setenv(conversations.FLAG_CONV_DURABLE, "1")
    calls = _never_write(monkeypatch)
    client, _tenant = _client(monkeypatch)
    content = "a" * (conversations.MAX_MESSAGE_CONTENT_BYTES + 1)
    resp = client.post(_url(), json={"content": content},
                        headers={"Idempotency-Key": "k1"})
    assert resp.status_code == 413
    assert resp.json()["error"]["error_code"] == "BAD_PARAMS"
    assert calls["count"] == 0


def test_empty_content_returns_422_and_writes_nothing(monkeypatch) -> None:
    monkeypatch.setenv(conversations.FLAG_CONV_DURABLE, "1")
    calls = _never_write(monkeypatch)
    client, _tenant = _client(monkeypatch)
    resp = client.post(_url(), json={"content": ""},
                        headers={"Idempotency-Key": "k1"})
    assert resp.status_code == 422
    assert calls["count"] == 0


def test_unknown_metadata_keys_returns_422_and_writes_nothing_not_stripped(
    monkeypatch,
) -> None:
    monkeypatch.setenv(conversations.FLAG_CONV_DURABLE, "1")
    calls = _never_write(monkeypatch)
    client, _tenant = _client(monkeypatch)
    resp = client.post(
        _url(),
        json={"content": "hi", "metadata": {"role": "user", "secret": "x"}},
        headers={"Idempotency-Key": "k1"},
    )
    assert resp.status_code == 422
    assert calls["count"] == 0


def test_missing_idempotency_key_returns_400_and_writes_nothing(monkeypatch) -> None:
    monkeypatch.setenv(conversations.FLAG_CONV_DURABLE, "1")
    calls = _never_write(monkeypatch)
    client, _tenant = _client(monkeypatch)
    resp = client.post(_url(), json={"content": "hi"})
    assert resp.status_code == 400
    assert calls["count"] == 0


def test_valid_boundary_request_passes_validation_and_reaches_authority(
    monkeypatch,
) -> None:
    """Exactly 32768 bytes of content must clear validation: this is proven
    by observing the request reach (and fail at) the project-authority step
    -- a 404 from an unresolvable project, not a 413/422 validation error --
    rather than by requiring a live database for the full write."""
    monkeypatch.setenv(conversations.FLAG_CONV_DURABLE, "1")
    calls = _never_write(monkeypatch)

    def _fail_lookup(*_args: Any, **_kwargs: Any):
        raise LookupError("project session is unavailable")

    monkeypatch.setattr(platform_link, "require_project_access", _fail_lookup)
    content = "a" * conversations.MAX_MESSAGE_CONTENT_BYTES
    client, _tenant = _client(monkeypatch)
    resp = client.post(_url(), json={"content": content},
                        headers={"Idempotency-Key": "k1"})
    assert resp.status_code == 404
    assert calls["count"] == 0


def test_forbidden_role_denies_with_same_shape_as_missing_project(monkeypatch) -> None:
    """Same-shape denial: a same-tenant actor with the wrong/revoked role
    (403) never distinguishes itself in status or body from a project that
    does not exist (404) at a DIFFERENT axis -- both are typed BAD_PARAMS-or
    FORBIDDEN envelopes, never leaking which one is true beyond the code the
    role check itself is contractually allowed to return."""
    monkeypatch.setenv(conversations.FLAG_CONV_DURABLE, "1")
    calls = _never_write(monkeypatch)

    def _forbidden(*_args: Any, **_kwargs: Any):
        raise platform_link.ProjectSessionForbidden("revoked")

    monkeypatch.setattr(platform_link, "require_project_access", _forbidden)
    client, _tenant = _client(monkeypatch)
    resp = client.post(_url(), json={"content": "hi"},
                        headers={"Idempotency-Key": "k1"})
    assert resp.status_code == 403
    assert resp.json()["error"]["error_code"] == "FORBIDDEN"
    assert calls["count"] == 0


def test_unknown_conversation_in_scope_returns_404_and_writes_nothing(
    monkeypatch,
) -> None:
    monkeypatch.setenv(conversations.FLAG_CONV_DURABLE, "1")
    calls = _never_write(monkeypatch)
    org_id = str(uuid.uuid4())

    monkeypatch.setattr(
        platform_link, "require_project_access", lambda *a, **k: org_id)
    monkeypatch.setattr(
        conversations, "_actor_binding_id", lambda *a, **k: str(uuid.uuid4()))
    monkeypatch.setattr(
        conversations, "get_conversation", lambda *a, **k: None)

    client, _tenant = _client(monkeypatch)
    resp = client.post(_url(), json={"content": "hi"},
                        headers={"Idempotency-Key": "k1"})
    assert resp.status_code == 404
    assert calls["count"] == 0


def test_route_is_mounted_on_the_real_app(monkeypatch) -> None:
    """f2: server/app.py must actually include this router. Proven by
    importing the REAL app module (not this file's own bare FastAPI() +
    include_router harness every other test above uses) and observing the
    route answer its own typed shape -- our ``BAD_PARAMS``/"disabled"
    envelope -- rather than FastAPI's generic unmounted-route
    ``{"detail": "Not Found"}``, which is what this route answered before
    app.py included the router.
    """
    monkeypatch.delenv(conversations.FLAG_CONV_DURABLE, raising=False)
    import app as app_module  # noqa: PLC0415 -- local: the real app's import
                               # cost/side effects belong only to this test.
    from fastapi.testclient import TestClient

    tenant = _Tenant(str(uuid.uuid4()))
    tenant.subject = f"auth0|{uuid.uuid4().hex}"
    app_module.app.dependency_overrides[deps.require_active_tenant] = lambda: tenant
    try:
        with TestClient(app_module.app, raise_server_exceptions=False) as client:
            resp = client.post(_url(), json={"content": "hi"},
                                headers={"Idempotency-Key": "k1"})
    finally:
        app_module.app.dependency_overrides.pop(deps.require_active_tenant, None)

    assert resp.status_code == 404
    body = resp.json()
    assert "error" in body, body  # our envelope, not FastAPI's bare {"detail": ...}
    assert body["error"]["error_code"] == "BAD_PARAMS"


# --- route-level time bound (f3/f4) ------------------------------------------ #

def test_slow_authority_read_past_route_budget_returns_504_and_writes_nothing(
    monkeypatch,
) -> None:
    """f4: the route's own time bound. Neither ``platform_link.require_project_access``
    nor ``resolve_active_identity_binding`` carries its own statement timeout
    (they live outside this card's file budget), so ROUTE_TIME_BUDGET_SECONDS
    is the only thing standing between a stuck authority read and a hung
    request. Set the budget far below any real DB round trip so the test
    stays fast and deterministic."""
    monkeypatch.setenv(conversations.FLAG_CONV_DURABLE, "1")
    calls = _never_write(monkeypatch)
    monkeypatch.setattr(conversations_router, "ROUTE_TIME_BUDGET_SECONDS", 0.05)

    def _slow_lookup(*_args: Any, **_kwargs: Any):
        time.sleep(0.3)
        return str(uuid.uuid4())

    monkeypatch.setattr(platform_link, "require_project_access", _slow_lookup)
    client, _tenant = _client(monkeypatch)
    resp = client.post(_url(), json={"content": "hi"},
                        headers={"Idempotency-Key": "k1"})
    assert resp.status_code == 504
    assert resp.json()["error"]["error_code"] == "TIMEOUT"
    assert calls["count"] == 0


def test_slow_conversation_lookup_past_route_budget_returns_504_and_writes_nothing(
    monkeypatch,
) -> None:
    """Same bound, different stuck call: get_conversation itself now carries a
    real Postgres statement_timeout (see conversations.get_conversation), but
    the route-level budget is the backstop that catches everything upstream
    of the database too -- e.g. a stalled connection acquisition from the
    pool, which no statement_timeout can bound because no statement is
    executing yet."""
    monkeypatch.setenv(conversations.FLAG_CONV_DURABLE, "1")
    calls = _never_write(monkeypatch)
    monkeypatch.setattr(conversations_router, "ROUTE_TIME_BUDGET_SECONDS", 0.05)
    org_id = str(uuid.uuid4())

    monkeypatch.setattr(
        platform_link, "require_project_access", lambda *a, **k: org_id)
    monkeypatch.setattr(
        conversations, "_actor_binding_id", lambda *a, **k: str(uuid.uuid4()))

    def _slow_get(*_args: Any, **_kwargs: Any):
        time.sleep(0.3)
        return None

    monkeypatch.setattr(conversations, "get_conversation", _slow_get)
    client, _tenant = _client(monkeypatch)
    resp = client.post(_url(), json={"content": "hi"},
                        headers={"Idempotency-Key": "k1"})
    assert resp.status_code == 504
    assert resp.json()["error"]["error_code"] == "TIMEOUT"
    assert calls["count"] == 0


# --- DB-backed: authority, idempotency, N+1, timeout ------------------------- #
#
# See the module docstring's KNOWN GAP note: conversation_messages has no
# migration in this repo, so every test below skips without DATABASE_URL and
# would still fail (relation does not exist) if pointed at a database that
# has not applied the still-missing companion migration.

@pytest.fixture
def pg(monkeypatch):
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("PostgreSQL conversation message test requires DATABASE_URL")
    db = conversations.platform_db()
    db.apply_migration()
    monkeypatch.setenv(conversations.FLAG_CONV_DURABLE, "1")
    yield db
    db.reset_pool()


def _seed(store: Any) -> tuple[str, str, str, str]:
    org = store.create_org_with_identity(
        f"conv-msg-org-{uuid.uuid4().hex[:8]}", "auth0", f"auth0|{uuid.uuid4().hex}")
    project = store.create_project(org.org_id, "conv-msg-project")
    subject = f"auth0|{uuid.uuid4().hex}"
    binding_id = str(uuid.uuid4())
    with conversations.platform_db().cursor() as cur:
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
            "  invited_by_binding_id)"
            " VALUES (%s, %s, %s, %s, 'owner', 'active', %s)",
            (str(uuid.uuid4()), str(org.org_id), str(project.project_id),
             binding_id, binding_id),
        )
    return str(org.org_id), str(project.project_id), subject, binding_id


@requires_database
def test_idempotent_replay_returns_stored_result_with_no_duplicate_row(pg) -> None:
    store = conversations.platform_store()
    org_id, project_id, subject, binding_id = _seed(store)
    conversation = conversations.create_conversation(org_id, project_id, binding_id)
    conversation_id = conversation["conversation_id"]

    first, first_inserted = _create_message(
        org_id, project_id, conversation_id, binding_id,
        "hello", {"role": "user"}, "replay-key-1",
    )
    assert first_inserted is True
    second, second_inserted = _create_message(
        org_id, project_id, conversation_id, binding_id,
        "hello", {"role": "user"}, "replay-key-1",
    )
    assert second_inserted is False
    assert second["message_id"] == first["message_id"]
    assert second["created_at"] == first["created_at"]

    with pg.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM conversation_messages WHERE org_id = %s"
            " AND idempotency_key = %s", (org_id, "replay-key-1"),
        )
        assert cur.fetchone()["n"] == 1


@requires_database
def test_different_conversations_reuse_the_same_idempotency_key_independently(
    pg,
) -> None:
    store = conversations.platform_store()
    org_id, project_id, subject, binding_id = _seed(store)
    conv_a = conversations.create_conversation(org_id, project_id, binding_id)
    conv_b = conversations.create_conversation(org_id, project_id, binding_id)

    message_a, inserted_a = _create_message(
        org_id, project_id, conv_a["conversation_id"], binding_id,
        "a", {}, "same-key",
    )
    message_b, inserted_b = _create_message(
        org_id, project_id, conv_b["conversation_id"], binding_id,
        "b", {}, "same-key",
    )
    assert inserted_a is True
    assert inserted_b is True
    assert message_a["message_id"] != message_b["message_id"]


@requires_database
def test_api_path_cross_tenant_and_cross_project_denied_same_shape_zero_rows(
    pg,
) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    store = conversations.platform_store()
    org1, project1, subject1, binding1 = _seed(store)
    org2, project2, subject2, binding2 = _seed(store)

    conversation = conversations.create_conversation(org1, project1, binding1)
    conversation_id = conversation["conversation_id"]

    tenant2 = _Tenant(org2)
    tenant2.subject = subject2

    app = FastAPI()
    app.include_router(conversations_router.router)
    app.dependency_overrides[deps.require_active_tenant] = lambda: tenant2
    client = TestClient(app)

    resp = client.post(
        f"/api/projects/{project1}/conversations/{conversation_id}/messages",
        json={"content": "hi"}, headers={"Idempotency-Key": "cross-tenant"},
    )
    assert resp.status_code == 404
    assert "message_id" not in resp.json()

    with pg.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM conversation_messages"
            " WHERE conversation_id = %s", (conversation_id,),
        )
        assert cur.fetchone()["n"] == 0


@requires_database
def test_revoked_member_cannot_post_and_replaying_old_key_still_denies(pg) -> None:
    store = conversations.platform_store()
    org_id, project_id, subject, binding_id = _seed(store)
    conversation = conversations.create_conversation(org_id, project_id, binding_id)
    conversation_id = conversation["conversation_id"]

    with pg.cursor() as cur:
        cur.execute(
            "UPDATE project_member_bindings SET status = 'revoked',"
            " revoked_at = NOW() WHERE org_id = %s AND binding_id = %s",
            (org_id, binding_id),
        )

    tenant = _Tenant(org_id)
    tenant.subject = subject
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(conversations_router.router)
    app.dependency_overrides[deps.require_active_tenant] = lambda: tenant
    client = TestClient(app)

    resp = client.post(
        f"/api/projects/{project_id}/conversations/{conversation_id}/messages",
        json={"content": "hi"}, headers={"Idempotency-Key": "revoked-key"},
    )
    assert resp.status_code in (403, 404)

    with pg.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM conversation_messages"
            " WHERE conversation_id = %s", (conversation_id,),
        )
        assert cur.fetchone()["n"] == 0


@requires_database
def test_write_is_n1_free_fresh_insert_one_statement_replay_two(pg, monkeypatch) -> None:
    """N+1-free: the fresh-write path issues exactly one authority read and
    one data-mutating statement -- counted via a real cursor.execute spy
    filtered to SELECT/INSERT/UPDATE/DELETE so BEGIN/COMMIT/SET framing never
    inflates or deflates the count across drivers.

    The replay path issues two: the INSERT takes the ON CONFLICT DO NOTHING
    arm (no row from RETURNING), so a follow-up SELECT reads the stored row
    -- the documented tradeoff for replacing the WAL-churning
    ``DO UPDATE SET content = content`` conflict arm (see create_message's
    docstring). Still bounded and still no duplicate row; just two round
    trips on replay instead of one."""
    import re

    import psycopg

    store = conversations.platform_store()
    org_id, project_id, subject, binding_id = _seed(store)
    conversation = conversations.create_conversation(org_id, project_id, binding_id)
    conversation_id = conversation["conversation_id"]

    statements: list[str] = []
    real_execute = psycopg.Cursor.execute

    def _spy(self, query, *args, **kwargs):  # noqa: ANN001
        text = query if isinstance(query, str) else query.decode("utf-8", "ignore")
        if re.match(r"^\s*(SELECT|INSERT|UPDATE|DELETE)\b", text, re.IGNORECASE):
            statements.append(text)
        return real_execute(self, query, *args, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", _spy)

    org_id_resolved, actor_binding_id = (
        conversations._require_project(
            type("T", (str,), {"subject": subject})(org_id), project_id,
            write=True,
        ),
        conversations._actor_binding_id(
            type("T", (str,), {"subject": subject})(org_id)),
    )
    authority_statement_count = len(statements)
    statements.clear()

    _create_message(
        org_id_resolved, project_id, conversation_id, actor_binding_id,
        "hello", {}, "n1-key",
    )
    insert_statement_count = len(statements)
    assert insert_statement_count == 1, statements

    statements.clear()
    _create_message(
        org_id_resolved, project_id, conversation_id, actor_binding_id,
        "hello again", {}, "n1-key",
    )
    replay_statement_count = len(statements)
    assert replay_statement_count == 2, statements
    assert authority_statement_count >= 1


@requires_database
def test_message_insert_statement_timeout_is_set_on_the_executing_connection(
    pg,
) -> None:
    """SET LOCAL, not SET SESSION: a positive timeout is visible on the same
    connection the write runs on, and does not leak to the next pooled
    borrower once the transaction ends."""
    with pg.get_pool().connection() as conn:
        with conn.transaction():
            conn.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (str(conversations.MESSAGE_STATEMENT_TIMEOUT_MS),),
            )
            row = conn.execute("SHOW statement_timeout").fetchone()
            assert row[0] == f"{conversations.MESSAGE_STATEMENT_TIMEOUT_MS}ms"
        row_after = conn.execute("SHOW statement_timeout").fetchone()
        assert row_after[0] != f"{conversations.MESSAGE_STATEMENT_TIMEOUT_MS}ms"


def test_conversation_lookup_uses_parameter_safe_transaction_timeout() -> None:
    src = inspect.getsource(conversations.get_conversation)
    assert "SELECT set_config('statement_timeout', %s, true)" in src
    assert "SET LOCAL statement_timeout = %s" not in src
