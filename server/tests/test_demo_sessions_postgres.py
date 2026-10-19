"""Real independent-connection mint races; skips require explicit DATABASE_URL.

Each test owns a random schema. No shared sessions, counters, or orgs are
truncated, and production data is never used as a test fixture.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from types import SimpleNamespace
import uuid

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from fastapi import HTTPException
import pytest
from starlette.requests import Request

import demo_sessions as demo


requires_database = pytest.mark.skipif(
    not os.environ.get("DATABASE_URL"),
    reason="PostgreSQL race test requires explicit DATABASE_URL")
pytestmark = requires_database


def request(address):
    return Request({"type": "http", "method": "POST", "scheme": "http",
                    "path": "/api/site/demo-session", "query_string": b"",
                    "client": (address, 1234), "server": ("test", 80), "headers": []})


@pytest.fixture
def postgres_registry(monkeypatch):
    if not os.environ.get("DATABASE_URL"):
        pytest.skip("PostgreSQL race test requires explicit DATABASE_URL")
    # Deliberately not importorskip: a configured DB with a missing driver is a failure.
    import psycopg
    from psycopg.rows import dict_row
    from psycopg import sql

    schema = "demo_test_" + uuid.uuid4().hex
    url = os.environ["DATABASE_URL"]
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))

    def connect():
        conn = psycopg.connect(url, row_factory=dict_row)
        conn.execute(sql.SQL("SET search_path TO {}").format(sql.Identifier(schema)))
        conn.commit()
        return conn

    state = SimpleNamespace(connect=connect, schema=schema, barrier=None,
                            backend_ids=set(), lock=threading.Lock())

    def run_transaction(operation, *, read_only=False, **kwargs):
        with connect() as conn:
            with state.lock:
                state.backend_ids.add(conn.info.backend_pid)
            if state.barrier is not None:
                state.barrier.wait(timeout=30)
            with conn.transaction():
                if read_only:
                    conn.execute("SET TRANSACTION READ ONLY")
                return operation(conn)

    try:
        with connect() as conn:
            migration = Path(__file__).resolve().parents[2] / "platform/migrations/0076_demo_sessions.sql"
            conn.execute(migration.read_text(), prepare=False)
        monkeypatch.setenv("LEAF_DEMO_LIVE_ENABLED", "1")
        monkeypatch.setenv("LEAF_DEMO_SESSION_SECRET", "postgres-test-signing")
        monkeypatch.setenv("LEAF_DEMO_IP_HASH_SECRET", "postgres-test-address")
        monkeypatch.setenv("LEAF_DEMO_PER_IP_DAILY_CAP", "3")
        monkeypatch.setenv("LEAF_DEMO_GLOBAL_DAILY_CAP", "5")
        monkeypatch.delenv("LEAF_DEMO_TRUSTED_PROXIES", raising=False)
        monkeypatch.setenv("FORWARDED_ALLOW_IPS", "")
        monkeypatch.setattr(demo, "_now", lambda: datetime(2031, 2, 3, 12, tzinfo=timezone.utc))
        monkeypatch.setattr(demo, "_db", lambda: SimpleNamespace(run_transaction=run_transaction))
        yield state
    finally:
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def totals(state):
    with state.connect() as conn:
        sessions = conn.execute("SELECT count(*) AS n FROM demo_sessions").fetchone()["n"]
        counters = {row["scope"]: row["count"] for row in conn.execute(
            "SELECT scope, count FROM demo_mint_counters").fetchall()}
    return sessions, counters


@pytest.mark.parametrize("race", ["same_ip", "global"])
def test_caps_atomic_with_independent_connections(postgres_registry, race):
    state = postgres_registry
    attempts = 12
    state.barrier = threading.Barrier(attempts)

    def attempt(index):
        try:
            return demo.mint(request("198.51.100.7" if race == "same_ip" else f"198.51.100.{index + 1}"))
        except HTTPException as exc:
            return exc.status_code

    with ThreadPoolExecutor(max_workers=attempts) as pool:
        outcomes = list(pool.map(attempt, range(attempts)))
    state.barrier = None
    expected = 3 if race == "same_ip" else 5
    successes = [result for result in outcomes if isinstance(result, demo.MintedSession)]
    assert len(successes) == expected
    assert outcomes.count(429) == attempts - expected, outcomes
    assert len(state.backend_ids) == attempts
    sessions, counters = totals(state)
    assert sessions == expected
    assert counters["global"] == expected
    assert sum(count for scope, count in counters.items() if scope.startswith("ip:")) == expected
    assert len(counters) == (2 if race == "same_ip" else expected + 1)
    for minted in successes:
        assert demo.verify(minted.token).tenant_id.startswith("demo-")


@pytest.mark.parametrize("scope", ["ip", "global"])
def test_cap_refusal_rolls_back_all_counters_and_session(postgres_registry, monkeypatch, scope):
    monkeypatch.setenv("LEAF_DEMO_PER_IP_DAILY_CAP", "1" if scope == "ip" else "10")
    monkeypatch.setenv("LEAF_DEMO_GLOBAL_DAILY_CAP", "10" if scope == "ip" else "1")
    demo.mint(request("192.0.2.1"))
    before = totals(postgres_registry)
    with pytest.raises(HTTPException) as error:
        demo.mint(request("192.0.2.1" if scope == "ip" else "192.0.2.2"))
    assert error.value.status_code == 429
    assert totals(postgres_registry) == before


def test_insert_failure_rolls_back_both_counters(postgres_registry, monkeypatch):
    duplicate = uuid.uuid4()
    monkeypatch.setattr(demo, "uuid", SimpleNamespace(uuid4=lambda: duplicate))
    demo.mint(request("192.0.2.1"))
    before = totals(postgres_registry)
    with pytest.raises(HTTPException) as error:
        demo.mint(request("192.0.2.2"))
    assert error.value.status_code == 503
    assert totals(postgres_registry) == before


def test_committed_totals_persist_in_fresh_process(postgres_registry):
    demo.mint(request("192.0.2.1"))
    demo.mint(request("192.0.2.2"))
    program = """
import json, os, sys
import psycopg
from psycopg import sql
with psycopg.connect(os.environ['DATABASE_URL']) as conn:
    conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(sys.argv[1])))
    sessions = conn.execute('SELECT count(*) FROM demo_sessions').fetchone()[0]
    counters = dict(conn.execute('SELECT scope, count FROM demo_mint_counters').fetchall())
    print(json.dumps([sessions, counters]))
"""
    result = subprocess.run([sys.executable, "-P", "-c", program, postgres_registry.schema],
                            capture_output=True, text=True, timeout=30, check=True,
                            cwd=str(Path(__file__).resolve().parents[2] / "server"))
    count, counters = json.loads(result.stdout)
    assert count == 2
    assert counters["global"] == 2
    assert sorted(value for scope, value in counters.items() if scope != "global") == [1, 1]
    assert (count, counters) == totals(postgres_registry)


def test_database_failure_refuses_without_committed_changes(postgres_registry, monkeypatch):
    import psycopg
    before = totals(postgres_registry)
    def unavailable():
        raise psycopg.OperationalError("test database unavailable")
    monkeypatch.setattr(demo, "_db", unavailable)
    with pytest.raises(HTTPException) as error:
        demo.mint(request("192.0.2.1"))
    assert error.value.status_code == 503
    assert totals(postgres_registry) == before


def test_migrated_schema_columns_and_constraints(postgres_registry):
    import psycopg
    with postgres_registry.connect() as conn:
        columns = {}
        for row in conn.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = %s", (postgres_registry.schema,)).fetchall():
            columns.setdefault(row["table_name"], set()).add(row["column_name"])
        assert columns["demo_sessions"] == set(demo.DemoSession._fields)
        assert columns["demo_mint_counters"] == {"day", "scope", "count"}
        constraints = conn.execute(
            "SELECT contype, conrelid::regclass::text AS table_name FROM pg_constraint "
            "WHERE connamespace = %s::regnamespace", (postgres_registry.schema,)).fetchall()
        assert sum(row["contype"] == "p" for row in constraints) == 2
        assert sum(row["contype"] == "u" for row in constraints) == 2
        assert sum(row["contype"] == "c" for row in constraints) >= 6
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.transaction():
                conn.execute("INSERT INTO demo_mint_counters VALUES ('2031-02-03', 'bogus', 1)")


def test_revocation_visible_on_next_independent_connection(postgres_registry):
    minted = demo.mint(request("192.0.2.1"))
    record = demo.verify(minted.token)
    with postgres_registry.connect() as conn:
        conn.execute("UPDATE demo_sessions SET revoked_at = now() WHERE id = %s", (record.id,))
    with pytest.raises(HTTPException) as error:
        demo.verify(minted.token)
    assert error.value.status_code == 401
