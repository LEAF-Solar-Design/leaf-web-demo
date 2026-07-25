"""Dependency-free contract tests plus an optional PostgreSQL integration proof."""
from __future__ import annotations

import os
import queue
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
from psycopg.errors import SerializationFailure

from leaf_platform import db
from leaf_platform.counters import CounterResult, SharedCounterStore


class _FakeConnection:
    def __init__(self):
        self.statements = []
        self.transaction_entries = 0

    @contextmanager
    def transaction(self):
        self.transaction_entries += 1
        yield

    def execute(self, statement):
        self.statements.append(statement)


class _FakePool:
    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def connection(self):
        yield self.conn


def test_pool_checks_connections_and_retires_them_before_proxy_idle_timeout(
    monkeypatch,
):
    """A closed RDS Proxy connection must not be handed to a request."""
    captured = {}

    class CapturingPool:
        @staticmethod
        def check_connection(conn):
            return conn

        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setenv(
        "DATABASE_URL", "postgresql://user:password@db.example.test/platform",
    )
    monkeypatch.setattr(db, "_pool", None)
    monkeypatch.setattr(db, "ConnectionPool", CapturingPool)

    pool = db.get_pool()

    assert isinstance(pool, CapturingPool)
    assert captured["check"] is CapturingPool.check_connection
    assert captured["max_idle"] == 600
    assert captured["max_idle"] < 900


def test_transaction_uses_non_leaking_transaction_settings(monkeypatch):
    conn = _FakeConnection()
    monkeypatch.setattr(db, "get_pool", lambda: _FakePool(conn))

    with db.transaction(
        isolation="serializable", read_only=True, deferrable=True,
    ) as yielded:
        assert yielded is conn

    assert conn.transaction_entries == 1
    assert conn.statements == [
        "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE READ ONLY DEFERRABLE",
    ]


@pytest.mark.parametrize("isolation", ["snapshot", "", "read uncommitted"])
def test_transaction_rejects_unsupported_isolation(isolation):
    with pytest.raises(ValueError, match="unsupported transaction isolation"):
        db._transaction_statement(isolation, read_only=False, deferrable=False)


def test_transaction_rejects_unsafe_deferrable_combinations():
    with pytest.raises(ValueError, match="serializable and read-only"):
        db._transaction_statement(
            "read committed", read_only=True, deferrable=True,
        )
    with pytest.raises(ValueError, match="serializable and read-only"):
        db._transaction_statement(
            "serializable", read_only=False, deferrable=True,
        )


def test_run_transaction_retries_only_serialization_failure(monkeypatch):
    attempts = []
    sleeps = []

    @contextmanager
    def fake_transaction(**options):
        attempts.append(options)
        yield object()

    def operation(_conn):
        if len(attempts) < 3:
            raise SerializationFailure("retry")
        return "ok"

    monkeypatch.setattr(db, "transaction", fake_transaction)
    monkeypatch.setattr(db.time, "sleep", sleeps.append)

    assert db.run_transaction(
        operation, isolation="serializable", max_attempts=3,
        retry_delay_seconds=0.01,
    ) == "ok"
    assert len(attempts) == 3
    assert sleeps == [0.01, 0.02]


@pytest.mark.parametrize("table", [
    "counter; DROP TABLE orgs",
    "counter table",
    "schema.table.extra",
    "",
])
def test_counter_rejects_unsafe_table_identifier(table):
    with pytest.raises(ValueError, match="counter table"):
        SharedCounterStore(table)


def test_counter_owned_transaction_is_serializable(monkeypatch):
    store = SharedCounterStore("authority_counters")
    expected = CounterResult(accepted=True, value=3, limit=5)
    seen = {}

    def fake_consume(conn, **kwargs):
        seen["conn"] = conn
        seen["kwargs"] = kwargs
        return expected

    def fake_run(operation, **options):
        seen["options"] = options
        return operation("connection")

    monkeypatch.setattr(store, "consume_in_transaction", fake_consume)
    monkeypatch.setattr(db, "run_transaction", fake_run)

    assert store.consume(
        namespace="guest", key="daily:org", amount=3, limit=5,
        max_attempts=4,
    ) == expected
    assert seen == {
        "conn": "connection",
        "kwargs": {
            "namespace": "guest",
            "key": "daily:org",
            "amount": 3,
            "limit": 5,
        },
        "options": {"isolation": "serializable", "max_attempts": 4},
    }


@pytest.mark.parametrize(
    ("namespace", "key", "amount", "limit", "message"),
    [
        ("", "key", 1, None, "namespace and key"),
        ("namespace", " ", 1, None, "namespace and key"),
        ("namespace", "key", 0, None, "amount"),
        ("namespace", "key", 1, -1, "limit"),
    ],
)
def test_counter_rejects_invalid_consumption(namespace, key, amount, limit, message):
    store = SharedCounterStore("authority_counters")
    with pytest.raises(ValueError, match=message):
        store.consume_in_transaction(
            object(), namespace=namespace, key=key, amount=amount, limit=limit,
        )


def test_shared_counter_postgres_multiwriter_limit():
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("PostgreSQL integration test requires DATABASE_URL")

    table = f"wave0_counter_{uuid.uuid4().hex}"
    store = SharedCounterStore(table)
    with db.transaction() as conn:
        conn.execute(
            f'CREATE TABLE "{table}" ('
            "namespace TEXT NOT NULL, counter_key TEXT NOT NULL, "
            "value BIGINT NOT NULL, updated_at TIMESTAMPTZ NOT NULL, "
            "PRIMARY KEY (namespace, counter_key))"
        )
    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(executor.map(
                lambda _index: store.consume(
                    namespace="integration", key="shared", limit=5,
                    max_attempts=8,
                ),
                range(12),
            ))
        assert sum(result.accepted for result in results) == 5
        assert max(result.value for result in results) == 5
        with db.transaction(read_only=True) as conn:
            row = conn.execute(
                f'SELECT value FROM "{table}" '
                "WHERE namespace = 'integration' AND counter_key = 'shared'"
            ).fetchone()
        assert row["value"] == 5
    finally:
        with db.transaction() as conn:
            conn.execute(f'DROP TABLE IF EXISTS "{table}"')


def test_shared_counter_read_committed_conflict_observes_committed_value():
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("PostgreSQL integration test requires DATABASE_URL")

    table = f"wave0_counter_conflict_{uuid.uuid4().hex}"
    store = SharedCounterStore(table)
    with db.transaction() as conn:
        conn.execute(
            f'CREATE TABLE "{table}" ('
            "namespace TEXT NOT NULL, counter_key TEXT NOT NULL, "
            "value BIGINT NOT NULL, updated_at TIMESTAMPTZ NOT NULL, "
            "PRIMARY KEY (namespace, counter_key))"
        )

    second_pid = queue.Queue()

    def rejected_consume():
        with db.transaction(isolation="read committed") as conn:
            second_pid.put(conn.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"])
            return store.consume_in_transaction(
                conn, namespace="integration", key="first-insert",
                amount=1, limit=5,
            )

    try:
        with db.get_pool().connection() as first_conn:
            with ThreadPoolExecutor(max_workers=1) as executor:
                with first_conn.transaction():
                    first_conn.execute(
                        f'INSERT INTO "{table}" '
                        "(namespace, counter_key, value, updated_at) "
                        "VALUES ('integration', 'first-insert', 5, NOW())"
                    )
                    future = executor.submit(rejected_consume)
                    pid = second_pid.get(timeout=2)

                    deadline = time.monotonic() + 2
                    while time.monotonic() < deadline:
                        waiting = first_conn.execute(
                            "SELECT wait_event_type = 'Lock' AS waiting "
                            "FROM pg_stat_activity WHERE pid = %(pid)s",
                            {"pid": pid},
                        ).fetchone()
                        if waiting and waiting["waiting"]:
                            break
                        time.sleep(0.01)
                    else:
                        pytest.fail("second counter connection did not wait on first insert")
                result = future.result(timeout=2)

        assert result == CounterResult(accepted=False, value=5, limit=5)
    finally:
        with db.transaction() as conn:
            conn.execute(f'DROP TABLE IF EXISTS "{table}"')
