"""PostgreSQL authority checks for the agent gate.

The structural tests always run. Fleet-concurrency tests use DATABASE_URL and
skip cleanly in the database-free server test environment.
"""
from __future__ import annotations

import os
import platform as _stdlib_platform
import sys
import uuid
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_stdlib_platform.python_implementation()

SERVER_DIR = Path(__file__).resolve().parent.parent
PROJECT_ROOT = SERVER_DIR.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import agent_gate  # noqa: E402
import agent_pg_store  # noqa: E402


def test_postgres_authority_is_explicit_and_legacy_is_default(monkeypatch):
    monkeypatch.delenv("LEAF_AGENT_STORE", raising=False)
    assert agent_gate._using_postgres() is False
    monkeypatch.setenv("LEAF_AGENT_STORE", "postgres")
    assert agent_gate._using_postgres() is True
    monkeypatch.setenv("LEAF_AGENT_STORE", "typo")
    with pytest.raises(RuntimeError, match="LEAF_AGENT_STORE"):
        agent_gate._using_postgres()


def test_agent_migration_covers_every_shared_authority():
    sql = (PROJECT_ROOT / "platform" / "migrations" /
           "0013_agent_state.sql").read_text(encoding="utf-8")
    for table in (
        "agent_approvals",
        "agent_session_grants",
        "agent_rate_counters",
        "agent_fleet_state",
        "agent_gate_audit_events",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql


def test_rate_path_binds_the_shared_counter_primitive():
    source = (SERVER_DIR / "agent_pg_store.py").read_text(encoding="utf-8")
    assert "from leaf_platform.counters import SharedCounterStore" in source
    assert 'counter_type("agent_rate_counters")' in source
    assert "_counter().consume_in_transaction(" in source


def test_locking_approval_reads_name_their_table():
    source = (SERVER_DIR / "agent_pg_store.py").read_text(encoding="utf-8")
    locking_read = (
        "SELECT *, expires_at <= NOW() AS is_expired\n"
        "            FROM agent_approvals\n"
        "            WHERE confirmation_id = %(id)s\n"
        "            FOR UPDATE"
    )
    assert source.count(locking_read) == 2


def test_same_direction_grant_retry_emits_no_false_denial(monkeypatch):
    class ExistingGrantStore:
        @staticmethod
        def decide(*_args, **_kwargs):
            return False, {"granted": True, "denied": False}, "already_decided"

    monkeypatch.setenv("LEAF_AGENT_STORE", "postgres")
    monkeypatch.setattr(agent_gate, "_pg_store", lambda: ExistingGrantStore)
    monkeypatch.setattr(
        agent_gate, "_audit_append",
        lambda _event: pytest.fail("same-direction retry must not emit a denial"),
    )
    ok, record, reason = agent_gate.grant_approval("same-direction")
    assert ok is False
    assert record["granted"] is True
    assert reason == "already_decided"


def _database_row(*, granted: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "confirmation_id": "atomic-approval",
        "tenant_id": "atomic-tenant",
        "session_id": "atomic-session",
        "turn_id": "atomic-turn",
        "action": "run_write_tool",
        "args": {"tool": "add-panel"},
        "args_hash": agent_gate.canonical_args_hash({"tool": "add-panel"}),
        "policy": "confirm-once",
        "rung": 3,
        "created_at": now,
        "expires_at": now + timedelta(minutes=5),
        "granted": granted,
        "denied": False,
        "decided_at": now if granted else None,
        "decided_by": "atomic-tenant" if granted else None,
        "reason": "approved" if granted else None,
        "consumed_at": None,
    }


class _Result:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return deepcopy(self._row)


class _AtomicConnection:
    def __init__(self, database):
        self.database = database

    def execute(self, query, params=None):
        statement = " ".join(str(query).split())
        if statement.startswith("SELECT *, expires_at <= NOW() AS is_expired"):
            if self.database.row is None:
                return _Result(None)
            row = dict(
                self.database.row,
                is_expired=(
                    self.database.row["expires_at"] <= datetime.now(timezone.utc)),
            )
            return _Result(row)
        if statement.startswith("SELECT TO_CHAR("):
            return _Result({"bucket": "2026072312"})
        if statement.startswith("INSERT INTO agent_approvals"):
            self.database.row = {
                **_database_row(),
                "confirmation_id": params["confirmation_id"],
                "tenant_id": params["tenant_id"],
                "session_id": params["session_id"],
                "turn_id": params["turn_id"],
                "action": params["action"],
                "args_hash": params["args_hash"],
                "policy": params["policy"],
                "rung": params["rung"],
            }
            return _Result(None)
        if (
            statement.startswith("UPDATE agent_approvals SET granted = FALSE")
            and set(params) == {"id"}
        ):
            self.database.row.update({
                "granted": False,
                "denied": True,
                "decided_at": datetime.now(timezone.utc),
                "decided_by": "system",
                "reason": "expired",
            })
            return _Result(None)
        if statement.startswith("UPDATE agent_approvals SET granted"):
            self.database.row.update({
                "granted": bool(params["granted"]),
                "denied": bool(params["denied"]),
                "decided_at": datetime.now(timezone.utc),
                "decided_by": params["by"],
                "reason": params["reason"],
            })
            return _Result(self.database.row)
        if statement.startswith("UPDATE agent_approvals SET consumed_at"):
            self.database.row["consumed_at"] = datetime.now(timezone.utc)
            return _Result(self.database.row)
        if statement.startswith("INSERT INTO agent_session_grants"):
            self.database.grants.add((
                params["tenant"], params["session"], params["action"], params["target"]))
            return _Result(None)
        raise AssertionError(f"unexpected SQL in transaction test: {statement}")


class _AtomicDatabase:
    def __init__(self, row):
        self.row = deepcopy(row)
        self.grants = set()
        self.counter = 0
        self.rolled_back = False

    def run_transaction(self, operation, **_kwargs):
        before_row = deepcopy(self.row)
        before_grants = set(self.grants)
        before_counter = self.counter
        try:
            return operation(_AtomicConnection(self))
        except Exception:
            self.row = before_row
            self.grants = before_grants
            self.counter = before_counter
            self.rolled_back = True
            raise


class _AtomicCounter:
    def __init__(self, database):
        self.database = database

    def consume_in_transaction(
        self, _conn, *, namespace, key, amount=1, limit=None,
    ):
        del namespace, key
        accepted = limit is None or self.database.counter + amount <= limit
        if accepted:
            self.database.counter += amount
        return type("CounterResult", (), {
            "accepted": accepted,
            "value": self.database.counter,
            "limit": limit,
        })()


class _KeyedCounter:
    def __init__(self):
        self.values = {}

    def consume_in_transaction(
        self, _conn, *, namespace, key, amount=1, limit=None,
    ):
        identity = (namespace, key)
        current = self.values.get(identity, 0)
        accepted = limit is None or current + amount <= limit
        if accepted:
            current += amount
            self.values[identity] = current
        return type("CounterResult", (), {
            "accepted": accepted, "value": current, "limit": limit,
        })()


def test_decision_and_audit_failure_roll_back_together(monkeypatch):
    database = _AtomicDatabase(_database_row())
    monkeypatch.setattr(agent_pg_store, "_load_platform", lambda: (database, object))
    monkeypatch.setattr(
        agent_pg_store, "_append_audit_in_transaction",
        lambda _conn, _event: (_ for _ in ()).throw(RuntimeError("audit down")),
    )
    with pytest.raises(RuntimeError, match="audit down"):
        agent_pg_store.decide(
            "atomic-approval", granted=True, by="atomic-tenant", reason="approved")
    assert database.rolled_back is True
    assert database.row["granted"] is False
    assert database.row["decided_at"] is None


def test_redemption_grant_and_audit_failure_roll_back_together(monkeypatch):
    database = _AtomicDatabase(_database_row(granted=True))
    monkeypatch.setattr(agent_pg_store, "_load_platform", lambda: (database, object))
    monkeypatch.setattr(
        agent_pg_store, "_append_audit_in_transaction",
        lambda _conn, _event: (_ for _ in ()).throw(RuntimeError("audit down")),
    )
    with pytest.raises(RuntimeError, match="audit down"):
        agent_pg_store.redeem(
            "atomic-approval",
            tenant_id="atomic-tenant",
            session_id="atomic-session",
            action="run_write_tool",
            args_hash=database.row["args_hash"],
            audit_event={"kind": "allowed", "tenant_id": "atomic-tenant"},
            session_grant_target=["tool", "add-panel"],
        )
    assert database.rolled_back is True
    assert database.row["consumed_at"] is None
    assert database.grants == set()


def test_expiry_denial_rate_and_audit_failure_roll_back_together(monkeypatch):
    row = _database_row(granted=True)
    row["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    database = _AtomicDatabase(row)
    monkeypatch.setattr(agent_pg_store, "_load_platform", lambda: (database, object))
    monkeypatch.setattr(agent_pg_store, "_counter", lambda: _AtomicCounter(database))
    monkeypatch.setattr(
        agent_pg_store, "_append_audit_in_transaction",
        lambda _conn, _event: (_ for _ in ()).throw(RuntimeError("audit down")),
    )
    with pytest.raises(RuntimeError, match="audit down"):
        agent_pg_store.redeem(
            "atomic-approval",
            tenant_id="atomic-tenant",
            session_id="atomic-session",
            action="run_write_tool",
            args_hash=database.row["args_hash"],
            rate_category="medium",
            rate_limit=5,
            rate_rejected_event={"kind": "denied"},
            outcome_events={
                "approval_expired": {
                    "kind": "denied", "reason": "approval_expired"},
            },
        )
    assert database.rolled_back is True
    assert database.row["granted"] is True
    assert database.row["denied"] is False
    assert database.counter == 0


def test_pending_creation_rate_and_audit_failure_roll_back_together(monkeypatch):
    database = _AtomicDatabase(None)
    monkeypatch.setattr(agent_pg_store, "_load_platform", lambda: (database, object))
    monkeypatch.setattr(agent_pg_store, "_counter", lambda: _AtomicCounter(database))
    monkeypatch.setattr(
        agent_pg_store, "_append_audit_in_transaction",
        lambda _conn, _event: (_ for _ in ()).throw(RuntimeError("audit down")),
    )
    with pytest.raises(RuntimeError, match="audit down"):
        agent_pg_store.create_pending_with_rate(
            _record("atomic-tenant"),
            category="medium",
            limit=5,
            accepted_event={"kind": "approval_requested"},
            rejected_event={"kind": "denied"},
        )
    assert database.rolled_back is True
    assert database.row is None
    assert database.counter == 0


def test_rate_consumption_and_decision_audit_failure_roll_back_together(monkeypatch):
    database = _AtomicDatabase(None)
    monkeypatch.setattr(agent_pg_store, "_load_platform", lambda: (database, object))
    monkeypatch.setattr(agent_pg_store, "_counter", lambda: _AtomicCounter(database))
    monkeypatch.setattr(
        agent_pg_store, "_append_audit_in_transaction",
        lambda _conn, _event: (_ for _ in ()).throw(RuntimeError("audit down")),
    )
    with pytest.raises(RuntimeError, match="audit down"):
        agent_pg_store.consume_rate_and_audit(
            "atomic-tenant", "low", 5,
            accepted_event={"kind": "allowed"},
            rejected_event={"kind": "denied"},
        )
    assert database.rolled_back is True
    assert database.counter == 0


def test_identical_calls_are_distinct_and_hour_and_tenant_buckets_do_not_alias(
    monkeypatch,
):
    counter = _KeyedCounter()
    monkeypatch.setattr(agent_pg_store, "_counter", lambda: counter)
    hour_one = datetime(2026, 7, 23, 12, tzinfo=timezone.utc)
    hour_two = hour_one + timedelta(hours=1)

    first = agent_pg_store._consume_rate_in_transaction(
        object(), "tenant-a", "low", 10, now=hour_one)
    second_identical = agent_pg_store._consume_rate_in_transaction(
        object(), "tenant-a", "low", 10, now=hour_one)
    other_tenant = agent_pg_store._consume_rate_in_transaction(
        object(), "tenant-b", "low", 10, now=hour_one)
    later_hour = agent_pg_store._consume_rate_in_transaction(
        object(), "tenant-a", "low", 10, now=hour_two)

    assert first.value == 1
    assert second_identical.value == 2
    assert other_tenant.value == 1
    assert later_hour.value == 1


def test_atomic_redemption_does_not_run_a_second_stranding_audit(
    monkeypatch, tmp_path,
):
    class RedeemedStore:
        @staticmethod
        def tenant_state(_tenant):
            return {"agent_disabled": False, "overlay": {}, "revision": 0}

        @staticmethod
        def kill_switch_details():
            return {"active": False, "reason": ""}

        @staticmethod
        def consume_rate(_tenant, category, limit):
            return True, {
                "category": category, "count": 1, "limit": limit,
                "reason": f"rate_limit_ok: {category} (1/{limit})",
            }

        @staticmethod
        def redeem(*_args, **kwargs):
            assert kwargs["audit_event"]["kind"] == "allowed"
            assert kwargs["audit_event"]["policy_outcome"] == "allow_via_approval"
            assert kwargs["session_grant_target"] == ["tool", "add-panel"]
            return True, {"consumed_at": "now"}, "allow_via_approval"

    monkeypatch.setenv("LEAF_AGENT_STORE", "postgres")
    monkeypatch.setenv(
        "LEAF_AGENT_TENANTS_FILE", str(tmp_path / "missing-tenants.json"))
    monkeypatch.delenv("LEAF_AGENT_POLICY_FILE", raising=False)
    monkeypatch.setattr(agent_gate, "_pg_store", lambda: RedeemedStore)
    monkeypatch.setattr(
        agent_gate, "_audit_append",
        lambda _event: pytest.fail(
            "atomic redemption evidence must replace the later general audit"),
    )
    result = agent_gate.gate(
        "atomic-tenant", "atomic-session", "atomic-turn", "submit_live_solve",
        {"tool": "add-panel", "confirmation_id": "atomic-approval"},
        {"run_write": True},
    )
    assert result["decision"] == "allow"
    assert result["reason"] == "allow_via_approval"


def test_postgres_auto_decision_uses_one_atomic_rate_transition(
    monkeypatch, tmp_path,
):
    calls = []

    class AutoStore:
        @staticmethod
        def tenant_state(_tenant):
            return {"agent_disabled": False, "overlay": {}, "revision": 0}

        @staticmethod
        def kill_switch_details():
            return {"active": False, "reason": ""}

        @staticmethod
        def consume_rate_and_audit(
            tenant, category, limit, **kwargs,
        ):
            calls.append((tenant, category, limit, kwargs))
            return True, {
                "category": category, "count": 1, "limit": limit,
                "reason": f"rate_limit_ok: {category} (1/{limit})",
            }

    monkeypatch.setenv("LEAF_AGENT_STORE", "postgres")
    monkeypatch.setenv(
        "LEAF_AGENT_TENANTS_FILE", str(tmp_path / "missing-tenants.json"))
    monkeypatch.delenv("LEAF_AGENT_POLICY_FILE", raising=False)
    monkeypatch.setattr(agent_gate, "_pg_store", lambda: AutoStore)
    monkeypatch.setattr(
        agent_gate, "_audit_append",
        lambda _event: pytest.fail("auto decision must audit inside rate transaction"),
    )
    result = agent_gate.gate(
        "atomic-tenant", "atomic-session", "atomic-turn", "run_read_tool",
        {"tool": "layer-report"}, {"run_read": True},
    )
    assert result["decision"] == "allow"
    assert len(calls) == 1
    assert calls[0][3]["accepted_event"]["kind"] == "allowed"


def test_postgres_pending_creation_is_atomic_with_rate_and_audit(
    monkeypatch, tmp_path,
):
    calls = []

    class PendingStore:
        @staticmethod
        def tenant_state(_tenant):
            return {"agent_disabled": False, "overlay": {}, "revision": 0}

        @staticmethod
        def kill_switch_details():
            return {"active": False, "reason": ""}

        @staticmethod
        def has_session_grant(*_args):
            return False

        @staticmethod
        def create_pending_with_rate(record, **kwargs):
            calls.append((record, kwargs))
            return True, record, {
                "category": kwargs["category"], "count": 1,
                "limit": kwargs["limit"], "reason": "rate_limit_ok",
            }

    monkeypatch.setenv("LEAF_AGENT_STORE", "postgres")
    monkeypatch.setenv(
        "LEAF_AGENT_TENANTS_FILE", str(tmp_path / "missing-tenants.json"))
    monkeypatch.delenv("LEAF_AGENT_POLICY_FILE", raising=False)
    monkeypatch.setattr(agent_gate, "_pg_store", lambda: PendingStore)
    monkeypatch.setattr(
        agent_gate, "_audit_append",
        lambda _event: pytest.fail("pending creation must include its audit"),
    )
    result = agent_gate.gate(
        "atomic-tenant", "atomic-session", "atomic-turn", "run_write_tool",
        {"tool": "add-panel"}, {"run_write": True},
    )
    assert result["decision"] == "awaiting_approval"
    assert len(calls) == 1
    assert calls[0][1]["accepted_event"]["kind"] == "approval_requested"


@pytest.fixture
def postgres_agent_store(monkeypatch):
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("DATABASE_URL is not set")
    agent_pg_store._platform_modules = None
    agent_pg_store._counter_store = None
    db, _counter_type = agent_pg_store._load_platform()
    db.apply_migration(
        PROJECT_ROOT / "platform" / "migrations" / "0013_agent_state.sql")
    prefix = f"agent-pg-test-{uuid.uuid4().hex}"
    monkeypatch.setenv("LEAF_AGENT_STORE", "postgres")
    yield db, prefix
    with db.cursor() as cur:
        cur.execute(
            "DELETE FROM agent_gate_audit_events WHERE tenant_id LIKE %(prefix)s",
            {"prefix": prefix + "%"},
        )
        cur.execute(
            "DELETE FROM agent_session_grants WHERE tenant_id LIKE %(prefix)s",
            {"prefix": prefix + "%"},
        )
        cur.execute(
            "DELETE FROM agent_rate_counters WHERE counter_key LIKE %(prefix)s",
            {"prefix": prefix + "%"},
        )
        cur.execute(
            "DELETE FROM agent_approvals WHERE tenant_id LIKE %(prefix)s",
            {"prefix": prefix + "%"},
        )
        cur.execute(
            """
            UPDATE agent_fleet_state
            SET active = FALSE, reason = '', updated_at = NOW(), updated_by = 'test-cleanup'
            WHERE state_key = 'global_kill'
            """
        )


def _record(prefix: str) -> dict:
    confirmation_id = uuid.uuid4().hex
    now = datetime.now(timezone.utc)
    return {
        "confirmation_id": confirmation_id,
        "tenant_id": prefix,
        "session_id": "session",
        "turn_id": "turn",
        "action": "run_write_tool",
        "args": {"tool": "add-panel"},
        "args_hash": agent_gate.canonical_args_hash({"tool": "add-panel"}),
        "policy": "confirm-once",
        "rung": 3,
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=5)).isoformat(),
        "granted": False,
        "denied": False,
        "decided_at": None,
        "decided_by": None,
        "reason": None,
    }


def test_two_writers_cannot_redeem_one_approval_twice(postgres_agent_store):
    _db, prefix = postgres_agent_store
    record = _record(prefix)
    agent_pg_store.create_pending(record)
    ok, _decided, status = agent_pg_store.decide(
        record["confirmation_id"], granted=True, by=prefix, reason="approved")
    assert ok is True and status == "granted"

    def redeem():
        return agent_pg_store.redeem(
            record["confirmation_id"],
            tenant_id=prefix,
            session_id="session",
            action="run_write_tool",
            args_hash=record["args_hash"],
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: redeem(), range(2)))
    assert sorted(result[0] for result in results) == [False, True]
    assert {result[2] for result in results} == {
        "allow_via_approval", "approval_consumed"}


def test_two_writers_share_one_rate_limit(postgres_agent_store):
    _db, prefix = postgres_agent_store
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _index: agent_pg_store.consume_rate(prefix, "low", 1),
            range(2),
        ))
    assert sorted(result[0] for result in results) == [False, True]
    assert all(result[1]["count"] == 1 for result in results)


def test_two_readers_see_the_same_fleet_kill_state(postgres_agent_store):
    db, prefix = postgres_agent_store
    with db.cursor() as cur:
        cur.execute(
            """
            UPDATE agent_fleet_state
            SET active = TRUE, reason = %(reason)s, updated_at = NOW(),
                updated_by = %(by)s
            WHERE state_key = 'global_kill'
            """,
            {"reason": "fleet test", "by": prefix},
        )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _index: agent_pg_store.kill_switch_details(), range(2)))
    assert results == [
        {"active": True, "reason": "fleet test"},
        {"active": True, "reason": "fleet test"},
    ]


def test_postgres_redemption_records_the_subject_bound_grant(monkeypatch, tmp_path):
    """The Postgres redemption must store the SAME target the lookup asks for.

    has_session_grant always queries the subject-bound target, and the Postgres
    store compares the serialized target exactly. If redemption recorded the
    unbound one, every confirm-once approval would silently become single-use
    instead of a durable grant.
    """
    recorded = {}

    class RedeemedStore:
        @staticmethod
        def tenant_state(_tenant):
            return {"agent_disabled": False, "overlay": {}, "revision": 0}

        @staticmethod
        def kill_switch_details():
            return {"active": False, "reason": ""}

        @staticmethod
        def consume_rate(_tenant, category, limit):
            return True, {
                "category": category, "count": 1, "limit": limit,
                "reason": f"rate_limit_ok: {category} (1/{limit})",
            }

        @staticmethod
        def redeem(*_args, **kwargs):
            recorded["target"] = kwargs["session_grant_target"]
            return True, {"consumed_at": "now"}, "allow_via_approval"

    monkeypatch.setenv("LEAF_AGENT_STORE", "postgres")
    monkeypatch.setenv(
        "LEAF_AGENT_TENANTS_FILE", str(tmp_path / "missing-tenants.json"))
    monkeypatch.delenv("LEAF_AGENT_POLICY_FILE", raising=False)
    monkeypatch.setattr(agent_gate, "_pg_store", lambda: RedeemedStore)
    monkeypatch.setattr(agent_gate, "_audit_append", lambda _event: None)

    result = agent_gate.gate(
        "atomic-tenant", "atomic-session", "atomic-turn", "submit_live_solve",
        {"tool": "add-panel", "confirmation_id": "atomic-approval"},
        {"run_write": True},
        subject="auth0|alice",
    )

    assert result["decision"] == "allow"
    assert recorded["target"] == agent_gate.grant_target(
        {"tool": "add-panel"}, "auth0|alice")
    assert "auth0|alice" in recorded["target"]
