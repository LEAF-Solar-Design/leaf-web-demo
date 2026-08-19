"""Card B-C4: recovery idempotency + poison-request fence.

Oracle (frozen):
  - A request that repeatedly fails mid-write cannot wedge the conversation:
    partial writes are transactional, retries converge to exactly-once.
  - A poison payload (fails deserialization post-store) is quarantined with a
    receipt row, stream continues; recovery skips it and reports the gap.

Two tiers, matching test_conversation_model.py's own convention:
  - Pure-Python tests (validation, poison detection) always run.
  - A minimal in-memory fake for ``platform_db()`` exercises the real
    ``create_message`` / ``recover_conversation_messages`` control flow
    (transactional rollback-on-exception, ON CONFLICT convergence, quarantine
    side effect suppression on replay) without a live database, always run.
  - PostgreSQL integration tests (concurrent duplicates, real ROLLBACK,
    cross-tenant storage boundary) require an explicit DATABASE_URL and skip
    cleanly otherwise, exactly like the rest of this suite.
"""
from __future__ import annotations

import os
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SERVER_DIR.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import conversations  # noqa: E402

requires_database = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="PostgreSQL conversation persistence test requires explicit DATABASE_URL",
)


# --- pure validation / poison detection ------------------------------------ #

def test_validate_idempotency_key_rejects_empty_and_oversized() -> None:
    with pytest.raises(ValueError):
        conversations._validate_idempotency_key("")
    with pytest.raises(ValueError):
        conversations._validate_idempotency_key("x" * 201)
    assert conversations._validate_idempotency_key("  ok-key  ") == "ok-key"


def test_validate_message_content_rejects_empty_and_oversized() -> None:
    with pytest.raises(ValueError):
        conversations._validate_message_content("")
    with pytest.raises(ValueError):
        conversations._validate_message_content("x" * (conversations.MAX_MESSAGE_CONTENT_BYTES + 1))
    assert conversations._validate_message_content("hello") == "hello"


def test_validate_message_metadata_rejects_non_dict_and_oversized() -> None:
    with pytest.raises(ValueError):
        conversations._validate_message_metadata(["not", "a", "dict"])  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        conversations._validate_message_metadata(
            {"blob": "x" * conversations.MAX_MESSAGE_METADATA_BYTES})
    assert conversations._validate_message_metadata(None) == {}
    assert conversations._validate_message_metadata({"a": 1}) == {"a": 1}


def test_post_store_poison_reason_clean_when_structured_key_absent_or_valid() -> None:
    assert conversations._post_store_poison_reason({}) is None
    assert conversations._post_store_poison_reason({"structured": "{\"a\": 1}"}) is None


def test_post_store_poison_reason_flags_invalid_json_or_wrong_type() -> None:
    assert conversations._post_store_poison_reason(
        {"structured": "{not json"}) == "structured_payload_invalid_json"
    assert conversations._post_store_poison_reason(
        {"structured": 12345}) == "structured_payload_not_a_string"


def test_post_store_poison_reason_never_echoes_the_raw_payload() -> None:
    """The gap report must reference the failure, never embed the payload
    (trap 11): the reason code must never contain the poison content."""
    secret_payload = "{ 'leaked-secret-marker': true, malformed"
    reason = conversations._post_store_poison_reason({"structured": secret_payload})
    assert reason is not None
    assert "leaked-secret-marker" not in reason
    assert secret_payload not in conversations._QUARANTINE_PLACEHOLDER_CONTENT


# --- fake platform_db(): exercises create_message's real control flow ------ #

class _FakeTimestamp:
    def __init__(self, seq: int) -> None:
        self.seq = seq

    def isoformat(self) -> str:
        return f"2026-01-01T00:00:{self.seq:02d}Z"


class _FakeCursor:
    """Supports exactly the query shapes conversations.py issues against
    conversation_messages: the ON CONFLICT insert, the point replay SELECT,
    and the ordered range SELECT used by recovery."""

    def __init__(self, committed: Dict[Any, Dict[str, Any]],
                 pending: Dict[Any, Dict[str, Any]],
                 fail_countdown: Optional[List[int]] = None) -> None:
        self._committed = committed
        self._pending = pending
        self._fail_countdown = fail_countdown
        self.last_row: Optional[Dict[str, Any]] = None
        self.last_rows: List[Dict[str, Any]] = []

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False

    def execute(self, sql: str, params: Any) -> None:
        stripped = sql.strip()
        if stripped.startswith("INSERT INTO conversation_messages"):
            (message_id, org_id, project_id, conversation_id, content, metadata,
             actor_binding_id, idempotency_key) = params
            key = (org_id, project_id, conversation_id, idempotency_key)
            if key in self._committed or key in self._pending:
                self.last_row = None
            else:
                row = {
                    "message_id": message_id, "org_id": org_id, "project_id": project_id,
                    "conversation_id": conversation_id, "content": content,
                    "metadata": metadata.obj, "created_by_binding_id": actor_binding_id,
                    "idempotency_key": idempotency_key,
                    "created_at": _FakeTimestamp(len(self._committed) + len(self._pending)),
                }
                self._pending[key] = row
                self.last_row = row
                if self._fail_countdown and self._fail_countdown[0] > 0:
                    self._fail_countdown[0] -= 1
                    raise RuntimeError("simulated mid-write failure")
        elif stripped.startswith("SELECT * FROM conversation_messages") and "idempotency_key = %s" in stripped:
            org_id, project_id, conversation_id, idempotency_key = params
            key = (org_id, project_id, conversation_id, idempotency_key)
            self.last_row = self._pending.get(key) or self._committed.get(key)
        elif stripped.startswith("SELECT * FROM conversation_messages") and "ORDER BY" in stripped:
            org_id, project_id, conversation_id, limit = params
            merged = {**self._committed, **self._pending}
            rows = [r for k, r in merged.items()
                    if k[0] == org_id and k[1] == project_id and k[2] == conversation_id]
            rows.sort(key=lambda r: r["created_at"].seq)
            self.last_rows = rows[:limit]
        else:
            raise AssertionError(f"unexpected fake SQL: {sql!r}")

    def fetchone(self) -> Optional[Dict[str, Any]]:
        return self.last_row

    def fetchall(self) -> List[Dict[str, Any]]:
        return self.last_rows


class _FakeConn:
    def __init__(self, committed: Dict[Any, Dict[str, Any]],
                 pending: Dict[Any, Dict[str, Any]],
                 fail_countdown: Optional[List[int]] = None) -> None:
        self._committed = committed
        self._pending = pending
        self._fail_countdown = fail_countdown

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self._committed, self._pending, self._fail_countdown)


class _FakeDB:
    """Models exactly the two guarantees the real pool gives conversations.py:
    ``run_transaction`` merges pending writes into committed state only on a
    clean return, and never on an exception (real ROLLBACK); ``cursor()``
    reads committed state only."""

    def __init__(self) -> None:
        self.committed: Dict[Any, Dict[str, Any]] = {}
        self.run_transaction_calls = 0
        # A real unique index serializes conflicting concurrent inserts at the
        # database level; this lock is this fake's equivalent so a concurrency
        # test exercises create_message's real ON CONFLICT convergence logic
        # rather than an artifact of two Python dicts racing unguarded.
        self._lock = threading.Lock()

    def run_transaction(self, operation: Any, **_kwargs: Any) -> Any:
        with self._lock:
            self.run_transaction_calls += 1
            pending: Dict[Any, Dict[str, Any]] = {}
            conn = _FakeConn(self.committed, pending, self._next_fail_countdown)
            result = operation(conn)  # an exception here must skip the merge below
            self.committed.update(pending)
            return result

    def cursor(self) -> _FakeCursor:
        return _FakeCursor(self.committed, {})

    _next_fail_countdown: Optional[List[int]] = None


@pytest.fixture
def fake_db(monkeypatch: Any) -> _FakeDB:
    db = _FakeDB()
    monkeypatch.setattr(conversations, "platform_db", lambda: db)
    return db


def test_create_message_is_transactional_and_retries_converge_to_exactly_one_row(
    fake_db: _FakeDB,
) -> None:
    """Oracle clause 1: repeated mid-write failures cannot wedge the
    conversation. Injects failures at two different write attempts (trap 24)
    before letting the third succeed."""
    org_id, project_id, conversation_id = "org-1", "proj-1", "conv-1"
    actor_id = "actor-1"
    idem_key = "req-42"

    fake_db._next_fail_countdown = [2]  # first two attempts fail post-insert, pre-commit
    for _ in range(2):
        with pytest.raises(RuntimeError, match="simulated mid-write failure"):
            conversations.create_message(
                org_id, project_id, conversation_id, actor_id, idem_key, "hello world")
        # each failed attempt must leave NO row committed -- the partial write
        # rolled back completely, not partially.
        assert fake_db.committed == {}

    result = conversations.create_message(
        org_id, project_id, conversation_id, actor_id, idem_key, "hello world")
    assert result["replayed"] is False
    assert len(fake_db.committed) == 1
    first_message = result["message"]

    # A further retry with the same idempotency key converges to the SAME
    # row -- no duplicate, and the stored original is replayed verbatim.
    replay = conversations.create_message(
        org_id, project_id, conversation_id, actor_id, idem_key, "hello world")
    assert replay["replayed"] is True
    assert replay["message"] == first_message
    assert len(fake_db.committed) == 1


def test_create_message_concurrent_duplicate_keys_converge_to_one_row(
    fake_db: _FakeDB,
) -> None:
    """Trap 25: a sequential-only suite hides a check-then-insert TOCTOU. The
    fake models the DB's ON CONFLICT arbiter itself, so two threads racing the
    exact same idempotency key must still leave exactly one row."""
    org_id, project_id, conversation_id = "org-1", "proj-1", "conv-race"
    actor_id = "actor-1"
    idem_key = "race-key"
    results: List[Dict[str, Any]] = []
    lock = threading.Lock()

    def attempt() -> None:
        result = conversations.create_message(
            org_id, project_id, conversation_id, actor_id, idem_key, "race payload")
        with lock:
            results.append(result)

    threads = [threading.Thread(target=attempt) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(fake_db.committed) == 1
    assert sum(1 for r in results if not r["replayed"]) == 1
    assert sum(1 for r in results if r["replayed"]) == 7
    message_ids = {r["message"]["message_id"] for r in results}
    assert message_ids == {results[0]["message"]["message_id"]}


def test_create_message_idempotency_key_is_scoped_per_conversation(
    fake_db: _FakeDB,
) -> None:
    """Trap 18: a globally-unique key would wedge unrelated streams; a
    per-sender-only key would mis-dedupe reused keys. Verifies the DB-level
    scope is (org, project, conversation, key), matching 0048's constraint."""
    same_key = "shared-client-key"
    result_a = conversations.create_message(
        "org-1", "proj-1", "conv-a", "actor-1", same_key, "message in conv a")
    result_b = conversations.create_message(
        "org-1", "proj-1", "conv-b", "actor-1", same_key, "message in conv b")
    assert result_a["replayed"] is False
    assert result_b["replayed"] is False
    assert result_a["message"]["message_id"] != result_b["message"]["message_id"]
    assert len(fake_db.committed) == 2


def test_create_message_quarantines_poison_payload_and_stream_continues(
    fake_db: _FakeDB,
) -> None:
    """Oracle clause 2, first half: a poison payload is quarantined with a
    receipt row, and the stream continues (a later good message still
    lands)."""
    org_id, project_id, conversation_id = "org-1", "proj-1", "conv-1"
    actor_id = "actor-1"

    poison_result = conversations.create_message(
        org_id, project_id, conversation_id, actor_id, "poison-key", "raw stored fine",
        metadata={"structured": "{not valid json"},
    )
    assert poison_result["quarantine"] is not None
    quarantine = poison_result["quarantine"]
    assert quarantine["metadata"]["kind"] == conversations._QUARANTINE_KIND
    assert quarantine["metadata"]["poison_message_id"] == poison_result["message"]["message_id"]
    assert quarantine["metadata"]["reason"] == "structured_payload_invalid_json"
    # The receipt never embeds the raw poison payload.
    assert "not valid json" not in quarantine["content"]
    assert "not valid json" not in str(quarantine["metadata"])

    # The stream continues: a later, clean message still lands normally.
    good_result = conversations.create_message(
        org_id, project_id, conversation_id, actor_id, "good-key", "a normal message")
    assert good_result["quarantine"] is None
    assert len(fake_db.committed) == 3  # poison row + receipt row + good row


def test_create_message_replay_does_not_refire_quarantine(fake_db: _FakeDB) -> None:
    """Trap 20/23: a retry that lands on the replay path must not re-run the
    poison check or write a second receipt."""
    org_id, project_id, conversation_id = "org-1", "proj-1", "conv-1"
    actor_id = "actor-1"
    idem_key = "poison-retry-key"
    metadata = {"structured": "{also not json"}

    first = conversations.create_message(
        org_id, project_id, conversation_id, actor_id, idem_key, "content", metadata=metadata)
    assert first["replayed"] is False
    assert first["quarantine"] is not None

    second = conversations.create_message(
        org_id, project_id, conversation_id, actor_id, idem_key, "content", metadata=metadata)
    assert second["replayed"] is True
    assert second["quarantine"] is None  # replay path never re-fires the side effect

    receipt_rows = [
        r for r in fake_db.committed.values()
        if r["metadata"].get("kind") == conversations._QUARANTINE_KIND
    ]
    assert len(receipt_rows) == 1


def test_recover_conversation_messages_skips_poison_and_reports_the_gap(
    fake_db: _FakeDB,
) -> None:
    """Oracle clause 2, second half: recovery skips the poisoned message and
    reports the gap; running recovery twice does not re-process it (trap 26)."""
    org_id, project_id, conversation_id = "org-1", "proj-1", "conv-1"
    actor_id = "actor-1"

    conversations.create_message(
        org_id, project_id, conversation_id, actor_id, "before", "message before")
    poisoned = conversations.create_message(
        org_id, project_id, conversation_id, actor_id, "poison", "poisoned content",
        metadata={"structured": "{broken"},
    )
    conversations.create_message(
        org_id, project_id, conversation_id, actor_id, "after", "message after")

    for _ in range(2):  # a second recovery must not re-process or duplicate the gap
        recovery = conversations.recover_conversation_messages(
            org_id, project_id, conversation_id)
        contents = [m["content"] for m in recovery["messages"]]
        assert contents == ["message before", "message after"]
        assert len(recovery["gaps"]) == 1
        gap = recovery["gaps"][0]
        assert gap["message_id"] == poisoned["message"]["message_id"]
        assert gap["reason"] == "structured_payload_invalid_json"
        # the gap report references the poisoned row, never embeds its content
        assert "poisoned content" not in str(gap)


def test_recover_conversation_messages_is_scoped_to_org_project_conversation(
    fake_db: _FakeDB,
) -> None:
    conversations.create_message(
        "org-1", "proj-1", "conv-1", "actor-1", "k1", "tenant one message")
    conversations.create_message(
        "org-2", "proj-2", "conv-1", "actor-2", "k1", "tenant two message")

    recovery = conversations.recover_conversation_messages("org-1", "proj-1", "conv-1")
    assert [m["content"] for m in recovery["messages"]] == ["tenant one message"]

    recovery_missing = conversations.recover_conversation_messages(
        "org-1", "proj-1", "conv-does-not-exist")
    assert recovery_missing == {"messages": [], "gaps": []}


def test_recover_conversation_messages_rejects_out_of_range_limit(
    fake_db: _FakeDB,
) -> None:
    with pytest.raises(ValueError):
        conversations.recover_conversation_messages("org-1", "proj-1", "conv-1", limit=0)
    with pytest.raises(ValueError):
        conversations.recover_conversation_messages("org-1", "proj-1", "conv-1", limit=501)


# --- PostgreSQL: real transaction rollback, real concurrency, real scope --- #

@pytest.fixture
def pg(monkeypatch: Any) -> Any:
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("PostgreSQL conversation persistence test requires DATABASE_URL")
    db = conversations.platform_db()
    db.apply_migration()
    monkeypatch.setenv(conversations.FLAG_CONV_DURABLE, "1")
    yield db
    db.reset_pool()


def _seed_conversation(store: Any, db: Any) -> tuple[str, str, str, str]:
    org = store.create_org_with_identity(
        f"conv-poison-org-{uuid.uuid4().hex[:8]}", "auth0", f"auth0|{uuid.uuid4().hex}",
    )
    project = store.create_project(org.org_id, "conv-poison-project")
    with db.cursor() as cur:
        cur.execute(
            "SELECT binding_id FROM identity_bindings"
            " WHERE platform_tenant_id = %s LIMIT 1", (str(org.org_id),),
        )
        binding_id = str(cur.fetchone()["binding_id"])
    conversation = conversations.create_conversation(
        str(org.org_id), str(project.project_id), binding_id, "poison test conversation")
    return (str(org.org_id), str(project.project_id), conversation["conversation_id"],
            binding_id)


@requires_database
def test_pg_mid_write_failure_leaves_no_partial_row_and_retry_succeeds(pg: Any) -> None:
    store = conversations.platform_store()
    org_id, project_id, conversation_id, binding_id = _seed_conversation(store, pg)

    real_run_transaction = pg.run_transaction
    call_count = {"n": 0}

    def flaky_run_transaction(operation: Any, **kwargs: Any) -> Any:
        call_count["n"] += 1
        if call_count["n"] <= 2:
            def failing_operation(conn: Any) -> Any:
                operation(conn)  # perform the real INSERT inside the real transaction
                raise RuntimeError("simulated mid-write failure")
            with pytest.raises(RuntimeError, match="simulated mid-write failure"):
                real_run_transaction(failing_operation, **kwargs)
            return None
        return real_run_transaction(operation, **kwargs)

    import conversations as _c
    original_platform_db = _c.platform_db

    class _Wrapped:
        def __getattr__(self, name: str) -> Any:
            if name == "run_transaction":
                return flaky_run_transaction
            return getattr(pg, name)

    _c.platform_db = lambda: _Wrapped()
    try:
        result = _c.create_message(
            org_id, project_id, conversation_id, binding_id, "pg-retry-key",
            "durable content")
    finally:
        _c.platform_db = original_platform_db

    assert result["replayed"] is False
    with pg.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM conversation_messages"
            " WHERE org_id = %s AND project_id = %s AND conversation_id = %s",
            (org_id, project_id, conversation_id),
        )
        assert cur.fetchone()["n"] == 1


@requires_database
def test_pg_concurrent_duplicate_idempotency_key_converges_to_one_row(pg: Any) -> None:
    store = conversations.platform_store()
    org_id, project_id, conversation_id, binding_id = _seed_conversation(store, pg)

    errors: List[BaseException] = []
    results: List[Dict[str, Any]] = []
    lock = threading.Lock()

    def attempt() -> None:
        try:
            result = conversations.create_message(
                org_id, project_id, conversation_id, binding_id, "pg-race-key",
                "race content")
            with lock:
                results.append(result)
        except BaseException as exc:  # pragma: no cover - failure path only
            with lock:
                errors.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert sum(1 for r in results if not r["replayed"]) == 1
    with pg.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM conversation_messages"
            " WHERE org_id = %s AND project_id = %s AND conversation_id = %s"
            " AND idempotency_key = %s",
            (org_id, project_id, conversation_id, "pg-race-key"),
        )
        assert cur.fetchone()["n"] == 1


@requires_database
def test_pg_poison_quarantine_and_recovery_gap_round_trip(pg: Any) -> None:
    store = conversations.platform_store()
    org_id, project_id, conversation_id, binding_id = _seed_conversation(store, pg)

    conversations.create_message(
        org_id, project_id, conversation_id, binding_id, "pg-before", "before message")
    poisoned = conversations.create_message(
        org_id, project_id, conversation_id, binding_id, "pg-poison", "poisoned body",
        metadata={"structured": "{unparseable"},
    )
    conversations.create_message(
        org_id, project_id, conversation_id, binding_id, "pg-after", "after message")

    assert poisoned["quarantine"] is not None

    recovery = conversations.recover_conversation_messages(
        org_id, project_id, conversation_id)
    assert [m["content"] for m in recovery["messages"]] == ["before message", "after message"]
    assert len(recovery["gaps"]) == 1
    assert recovery["gaps"][0]["message_id"] == poisoned["message"]["message_id"]


@requires_database
def test_pg_storage_boundary_rejects_wrong_org_for_recovery_directly(pg: Any) -> None:
    """Defense in depth: even bypassing any router, the store layer alone
    must not resolve another org's messages."""
    store = conversations.platform_store()
    org_id, project_id, conversation_id, binding_id = _seed_conversation(store, pg)
    conversations.create_message(
        org_id, project_id, conversation_id, binding_id, "scoped-key", "scoped content")

    other_org_id, other_project_id, other_conversation_id, _other_binding = (
        _seed_conversation(store, pg))

    cross = conversations.recover_conversation_messages(
        other_org_id, other_project_id, conversation_id)
    assert cross == {"messages": [], "gaps": []}
