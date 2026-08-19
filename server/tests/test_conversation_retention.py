"""Card B-C5: Retention/GC bounded and receipted.

Oracle (frozen):
  - GC pass is bounded per run (row cap + wall-clock cap), tenant-scoped,
    deletes only past the retention window, writes a sanitized receipt.
  - GC never touches a project with an in-flight request journal entry.

Two tiers, matching test_conversation_poison.py's own convention:
  - Pure-Python tests (env var parsing) always run.
  - A minimal in-memory fake for ``platform_db()`` exercises the real
    ``run_retention_gc`` control flow (row cap, wall-clock cap, retention
    cutoff, tenant scope, in-flight fence, receipt shape) without a live
    database, always run.
  - PostgreSQL integration tests require an explicit DATABASE_URL and skip
    cleanly otherwise, exactly like the rest of this suite.

Run:  cd server && python -m pytest tests/test_conversation_retention.py -q
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
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
    reason="PostgreSQL conversation retention test requires explicit DATABASE_URL",
)


# --- pure env-var parsing ---------------------------------------------------- #

def test_retention_days_defaults_and_rejects_garbage(monkeypatch) -> None:
    monkeypatch.delenv("LEAF_CONV_RETENTION_DAYS", raising=False)
    assert conversations.retention_days() == conversations.GC_DEFAULT_RETENTION_DAYS
    monkeypatch.setenv("LEAF_CONV_RETENTION_DAYS", "not-a-number")
    assert conversations.retention_days() == conversations.GC_DEFAULT_RETENTION_DAYS
    monkeypatch.setenv("LEAF_CONV_RETENTION_DAYS", "-5")
    assert conversations.retention_days() == conversations.GC_DEFAULT_RETENTION_DAYS
    monkeypatch.setenv("LEAF_CONV_RETENTION_DAYS", "30")
    assert conversations.retention_days() == 30.0


def test_gc_row_cap_defaults_and_rejects_garbage(monkeypatch) -> None:
    monkeypatch.delenv("LEAF_CONV_GC_ROW_CAP", raising=False)
    assert conversations.gc_row_cap() == conversations.GC_DEFAULT_ROW_CAP
    monkeypatch.setenv("LEAF_CONV_GC_ROW_CAP", "nope")
    assert conversations.gc_row_cap() == conversations.GC_DEFAULT_ROW_CAP
    monkeypatch.setenv("LEAF_CONV_GC_ROW_CAP", "0")
    assert conversations.gc_row_cap() == conversations.GC_DEFAULT_ROW_CAP
    monkeypatch.setenv("LEAF_CONV_GC_ROW_CAP", "10")
    assert conversations.gc_row_cap() == 10


def test_gc_wall_clock_s_defaults_and_rejects_garbage(monkeypatch) -> None:
    monkeypatch.delenv("LEAF_CONV_GC_WALL_CLOCK_S", raising=False)
    assert conversations.gc_wall_clock_s() == conversations.GC_DEFAULT_WALL_CLOCK_S
    monkeypatch.setenv("LEAF_CONV_GC_WALL_CLOCK_S", "nope")
    assert conversations.gc_wall_clock_s() == conversations.GC_DEFAULT_WALL_CLOCK_S
    monkeypatch.setenv("LEAF_CONV_GC_WALL_CLOCK_S", "-1")
    assert conversations.gc_wall_clock_s() == conversations.GC_DEFAULT_WALL_CLOCK_S
    monkeypatch.setenv("LEAF_CONV_GC_WALL_CLOCK_S", "2.5")
    assert conversations.gc_wall_clock_s() == 2.5


# --- fake platform_db(): exercises run_retention_gc's real control flow ---- #

class _FakeCursorResult:
    def __init__(self, rows: List[Dict[str, Any]]) -> None:
        self._rows = rows

    def fetchall(self) -> List[Dict[str, Any]]:
        return self._rows


class _FakeConn:
    """Models exactly the one statement shape ``_gc_delete_batch`` issues:
    the candidates-CTE delete, joined in-process against an in-memory
    ``app_session_requests`` model so the in-flight fence is exercised for
    real rather than assumed."""

    def __init__(self, messages: List[Dict[str, Any]],
                 journal: List[Dict[str, Any]]) -> None:
        self._messages = messages
        self._journal = journal

    def execute(self, sql: str, params: Any) -> _FakeCursorResult:
        stripped = sql.strip()
        if not stripped.startswith("WITH candidates AS ("):
            raise AssertionError(f"unexpected fake SQL: {sql!r}")
        org_id, cutoff, terminal_states, limit = params
        in_flight_projects = {
            row["project_id"] for row in self._journal
            if row["org_id"] == org_id and row["state"] not in terminal_states
        }
        candidates = [
            m for m in self._messages
            if m["org_id"] == org_id and m["created_at"] < cutoff
            and m["project_id"] not in in_flight_projects
        ]
        candidates.sort(key=lambda m: (m["created_at"], m["message_id"]))
        chosen = candidates[:limit]
        chosen_ids = {c["message_id"] for c in chosen}
        self._messages[:] = [m for m in self._messages if m["message_id"] not in chosen_ids]
        return _FakeCursorResult([
            {"message_id": c["message_id"], "project_id": c["project_id"],
             "conversation_id": c["conversation_id"]}
            for c in chosen
        ])


class _FakeDB:
    def __init__(self) -> None:
        self.messages: List[Dict[str, Any]] = []
        self.journal: List[Dict[str, Any]] = []
        self.run_transaction_calls = 0

    def run_transaction(self, operation: Any, **_kwargs: Any) -> Any:
        self.run_transaction_calls += 1
        return operation(_FakeConn(self.messages, self.journal))


@pytest.fixture
def fake_db(monkeypatch: Any) -> _FakeDB:
    db = _FakeDB()
    monkeypatch.setattr(conversations, "platform_db", lambda: db)
    monkeypatch.setenv(conversations.FLAG_CONV_DURABLE, "1")
    return db


def _msg(org: str, project: str, conversation: str, age_days: float,
         *, now: datetime) -> Dict[str, Any]:
    return {
        "message_id": str(uuid.uuid4()),
        "org_id": org,
        "project_id": project,
        "conversation_id": conversation,
        "created_at": now - timedelta(days=age_days),
    }


def _journal_row(org: str, project: str, state: str) -> Dict[str, Any]:
    return {"org_id": org, "project_id": project, "state": state}


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_gc_deletes_only_rows_past_the_retention_window(fake_db, monkeypatch) -> None:
    monkeypatch.setenv("LEAF_CONV_RETENTION_DAYS", "30")
    old = _msg("org-1", "proj-1", "conv-1", 31, now=NOW)
    fresh = _msg("org-1", "proj-1", "conv-1", 1, now=NOW)
    fake_db.messages.extend([old, fresh])

    receipt = conversations.run_retention_gc("org-1", now=NOW)

    assert receipt["deleted_message_count"] == 1
    assert [m["message_id"] for m in fake_db.messages] == [fresh["message_id"]]
    assert receipt["retention_days"] == 30.0
    assert receipt["truncated_by_row_cap"] is False
    assert receipt["truncated_by_wall_clock"] is False


def test_gc_is_tenant_scoped(fake_db, monkeypatch) -> None:
    monkeypatch.setenv("LEAF_CONV_RETENTION_DAYS", "1")
    org1 = _msg("org-1", "proj-1", "conv-1", 10, now=NOW)
    org2 = _msg("org-2", "proj-2", "conv-2", 10, now=NOW)
    fake_db.messages.extend([org1, org2])

    receipt = conversations.run_retention_gc("org-1", now=NOW)

    assert receipt["org_id"] == "org-1"
    assert receipt["deleted_message_count"] == 1
    remaining = {m["message_id"] for m in fake_db.messages}
    assert remaining == {org2["message_id"]}  # another tenant's row untouched


def test_gc_never_touches_a_project_with_an_in_flight_journal_entry(
    fake_db, monkeypatch,
) -> None:
    """Oracle clause 2. Two projects in the same org, both past the
    retention window: one has a non-terminal journal row and must survive;
    the other has none (or only terminal rows) and must be deleted."""
    monkeypatch.setenv("LEAF_CONV_RETENTION_DAYS", "1")
    in_flight_msg = _msg("org-1", "proj-in-flight", "conv-a", 10, now=NOW)
    clear_msg = _msg("org-1", "proj-clear", "conv-b", 10, now=NOW)
    fake_db.messages.extend([in_flight_msg, clear_msg])
    fake_db.journal.append(_journal_row("org-1", "proj-in-flight", "executing"))
    fake_db.journal.append(_journal_row("org-1", "proj-clear", "completed"))

    receipt = conversations.run_retention_gc("org-1", now=NOW)

    remaining = {m["message_id"] for m in fake_db.messages}
    assert remaining == {in_flight_msg["message_id"]}
    assert receipt["deleted_message_count"] == 1
    assert receipt["projects_touched"] == ["proj-clear"]


@pytest.mark.parametrize("state", ["admitted", "executing", "queued"])
def test_gc_treats_every_non_terminal_state_as_in_flight(
    fake_db, monkeypatch, state,
) -> None:
    monkeypatch.setenv("LEAF_CONV_RETENTION_DAYS", "1")
    msg = _msg("org-1", "proj-1", "conv-1", 10, now=NOW)
    fake_db.messages.append(msg)
    fake_db.journal.append(_journal_row("org-1", "proj-1", state))

    receipt = conversations.run_retention_gc("org-1", now=NOW)

    assert receipt["deleted_message_count"] == 0
    assert len(fake_db.messages) == 1


@pytest.mark.parametrize("state", ["completed", "failed", "abandoned"])
def test_gc_proceeds_when_the_only_journal_rows_are_terminal(
    fake_db, monkeypatch, state,
) -> None:
    monkeypatch.setenv("LEAF_CONV_RETENTION_DAYS", "1")
    msg = _msg("org-1", "proj-1", "conv-1", 10, now=NOW)
    fake_db.messages.append(msg)
    fake_db.journal.append(_journal_row("org-1", "proj-1", state))

    receipt = conversations.run_retention_gc("org-1", now=NOW)

    assert receipt["deleted_message_count"] == 1
    assert fake_db.messages == []


def test_gc_is_bounded_by_row_cap(fake_db, monkeypatch) -> None:
    monkeypatch.setenv("LEAF_CONV_RETENTION_DAYS", "1")
    monkeypatch.setenv("LEAF_CONV_GC_ROW_CAP", "5")
    for _ in range(12):
        fake_db.messages.append(_msg("org-1", "proj-1", "conv-1", 10, now=NOW))

    receipt = conversations.run_retention_gc("org-1", now=NOW)

    assert receipt["deleted_message_count"] == 5
    assert receipt["row_cap"] == 5
    assert receipt["truncated_by_row_cap"] is True
    assert receipt["truncated_by_wall_clock"] is False
    assert len(fake_db.messages) == 7  # 12 - 5, never more than the cap deleted

    # A second pass drains the remainder, still respecting the same cap.
    receipt2 = conversations.run_retention_gc("org-1", now=NOW)
    assert receipt2["deleted_message_count"] == 5
    assert len(fake_db.messages) == 2


def test_gc_is_bounded_by_wall_clock_cap(fake_db, monkeypatch) -> None:
    """The wall-clock cap is checked BETWEEN batches: force a small batch
    size via a tiny row cap so multiple batches run, and fake the clock so
    the second batch never starts."""
    monkeypatch.setenv("LEAF_CONV_RETENTION_DAYS", "1")
    monkeypatch.setenv("LEAF_CONV_GC_ROW_CAP", "1000")
    monkeypatch.setenv("LEAF_CONV_GC_WALL_CLOCK_S", "10")
    monkeypatch.setattr(conversations, "GC_DEFAULT_BATCH_SIZE", 2)
    for _ in range(6):
        fake_db.messages.append(_msg("org-1", "proj-1", "conv-1", 10, now=NOW))

    clock = {"t": 0.0}

    def fake_monotonic() -> float:
        return clock["t"]

    real_run_transaction = fake_db.run_transaction

    def counting_run_transaction(operation: Any, **kwargs: Any) -> Any:
        clock["t"] += 20.0  # first batch alone blows past the 10s cap
        return real_run_transaction(operation, **kwargs)

    monkeypatch.setattr(conversations.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(fake_db, "run_transaction", counting_run_transaction)

    receipt = conversations.run_retention_gc("org-1", now=NOW)

    assert receipt["batches"] == 1
    assert receipt["deleted_message_count"] == 2  # one batch of GC_DEFAULT_BATCH_SIZE=2
    assert receipt["truncated_by_wall_clock"] is True
    assert receipt["truncated_by_row_cap"] is False
    assert len(fake_db.messages) == 4


def test_gc_receipt_is_sanitized(fake_db, monkeypatch) -> None:
    """The receipt must reference ids and counts only -- never message
    content, metadata, or any per-message field beyond identity."""
    monkeypatch.setenv("LEAF_CONV_RETENTION_DAYS", "1")
    secret = "super-secret-message-body-should-never-leak"
    msg = _msg("org-1", "proj-1", "conv-1", 10, now=NOW)
    msg["content"] = secret  # not read by the GC query, but guards the receipt shape
    fake_db.messages.append(msg)

    receipt = conversations.run_retention_gc("org-1", now=NOW)

    assert secret not in str(receipt)
    assert set(receipt.keys()) == {
        "org_id", "flag_enabled", "cutoff", "retention_days", "row_cap",
        "wall_clock_cap_s", "deleted_message_count", "projects_touched",
        "conversations_touched_count", "batches", "truncated_by_row_cap",
        "truncated_by_wall_clock", "started_at", "finished_at",
    }
    assert receipt["projects_touched"] == ["proj-1"]


def test_gc_is_a_noop_when_the_flag_is_disabled(fake_db, monkeypatch) -> None:
    """Trap: a scheduled GC pass must obey ``conv_durable`` exactly like every
    route above, checked BEFORE any store call -- not only the POST route
    a shallow negative control would probe."""
    monkeypatch.delenv(conversations.FLAG_CONV_DURABLE, raising=False)
    fake_db.messages.append(_msg("org-1", "proj-1", "conv-1", 400, now=NOW))

    receipt = conversations.run_retention_gc("org-1", now=NOW)

    assert receipt["flag_enabled"] is False
    assert receipt["deleted_message_count"] == 0
    assert receipt["projects_touched"] == []
    assert fake_db.run_transaction_calls == 0  # no store call at all
    assert len(fake_db.messages) == 1  # nothing was touched


def test_gc_is_idempotent_when_nothing_left_to_delete(fake_db, monkeypatch) -> None:
    monkeypatch.setenv("LEAF_CONV_RETENTION_DAYS", "1")
    fake_db.messages.append(_msg("org-1", "proj-1", "conv-1", 10, now=NOW))

    first = conversations.run_retention_gc("org-1", now=NOW)
    second = conversations.run_retention_gc("org-1", now=NOW)

    assert first["deleted_message_count"] == 1
    assert second["deleted_message_count"] == 0
    assert second["projects_touched"] == []


# --- PostgreSQL: real journal table, real tenant/in-flight boundary -------- #

@pytest.fixture
def pg(monkeypatch: Any) -> Any:
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("PostgreSQL conversation retention test requires DATABASE_URL")
    db = conversations.platform_db()
    db.apply_migration()
    monkeypatch.setenv(conversations.FLAG_CONV_DURABLE, "1")
    yield db
    db.reset_pool()


def _seed_project(store: Any, db: Any) -> tuple[str, str, str, str]:
    org = store.create_org_with_identity(
        f"conv-gc-org-{uuid.uuid4().hex[:8]}", "auth0", f"auth0|{uuid.uuid4().hex}",
    )
    project = store.create_project(org.org_id, "conv-gc-project")
    with db.cursor() as cur:
        cur.execute(
            "SELECT binding_id FROM identity_bindings"
            " WHERE platform_tenant_id = %s LIMIT 1", (str(org.org_id),),
        )
        binding_id = str(cur.fetchone()["binding_id"])
    conversation = conversations.create_conversation(
        str(org.org_id), str(project.project_id), binding_id, "gc test conversation")
    return (str(org.org_id), str(project.project_id), conversation["conversation_id"],
            binding_id)


def _seed_journal_row(
    db: Any, *, org_id: str, project_id: str, session_id: str, state: str,
) -> None:
    with db.cursor() as cur:
        cur.execute(
            "INSERT INTO app_session_requests"
            " (request_id, tenant_id, drawing_id, session_id, principal_key,"
            "  payload_digest, state, created_at, updated_at, org_id, project_id)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,NOW(),NOW(),%s,%s)",
            (str(uuid.uuid4()), org_id, "gc-drawing", session_id, "gc-principal",
             "0" * 64, state, org_id, project_id),
        )


@requires_database
def test_pg_gc_deletes_past_retention_and_respects_in_flight_fence(pg: Any) -> None:
    store = conversations.platform_store()
    org_id, project_id, conversation_id, binding_id = _seed_project(store, pg)
    other_org_id, other_project_id, other_conversation_id, other_binding_id = (
        _seed_project(store, pg))

    old_key = "pg-gc-old"
    conversations.create_message(
        org_id, project_id, conversation_id, binding_id, old_key, "old content")
    conversations.create_message(
        other_org_id, other_project_id, other_conversation_id, other_binding_id,
        "pg-gc-in-flight", "in flight org content")

    with pg.cursor() as cur:
        cur.execute(
            "UPDATE conversation_messages SET created_at = NOW() - INTERVAL '400 days'"
            " WHERE org_id = %s", (org_id,),
        )
        cur.execute(
            "UPDATE conversation_messages SET created_at = NOW() - INTERVAL '400 days'"
            " WHERE org_id = %s", (other_org_id,),
        )
    _seed_journal_row(
        pg, org_id=other_org_id, project_id=other_project_id,
        session_id=str(uuid.uuid4()), state="executing",
    )

    receipt = conversations.run_retention_gc(org_id)
    assert receipt["deleted_message_count"] == 1
    with pg.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM conversation_messages WHERE org_id = %s",
            (org_id,),
        )
        assert cur.fetchone()["n"] == 0

    other_receipt = conversations.run_retention_gc(other_org_id)
    assert other_receipt["deleted_message_count"] == 0
    with pg.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) AS n FROM conversation_messages WHERE org_id = %s",
            (other_org_id,),
        )
        assert cur.fetchone()["n"] == 1  # the in-flight project's row survives
