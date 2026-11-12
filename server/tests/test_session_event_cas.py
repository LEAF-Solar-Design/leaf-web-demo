"""Public event CAS contract: disposable SQLite and inert PostgreSQL only."""
from __future__ import annotations

import copy
import importlib.util
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest


@pytest.fixture
def stores(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "legacy")
    modules = []

    def load():
        # Separate module locks AND connections model independent processes.
        spec = importlib.util.spec_from_file_location(
            "session_event_cas_store", Path(__file__).resolve().parents[1] / "session_store.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.DB_PATH = tmp_path / "sessions.db"
        modules.append(module)
        module.ensure_started()
        return module

    yield load
    for module in modules:
        if module._conn is not None:
            module._conn.close()


def snapshot(store, sid):
    return (
        dict(store._conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (sid,),
        ).fetchone()),
        [tuple(row) for row in store._conn.execute(
            "SELECT * FROM session_events WHERE session_id = ? ORDER BY seq", (sid,),
        )],
    )


@pytest.mark.parametrize("conditional", [False, True])
def test_append_preserves_event_title_count_and_terminal_effects(stores, conditional):
    store = stores()
    sid = store.get_or_create_session("tenant", "drawing")["session_id"]
    assert store.try_begin_turn(sid, "turn", 300, "hosted_pro", "subject")
    kwargs = {"expected_seq": 1} if conditional else {}
    assert store.append_event(sid, "turn", "turn_started", {"text": "First title"}, **kwargs) == 1
    row, events = snapshot(store, sid)
    assert row["title"] == "First title"
    assert row["turn_count"] == 1
    assert row["active_turn_id"] == "turn"
    assert len(events) == 1
    kwargs = {"expected_seq": 2} if conditional else {}
    assert store.append_event(sid, "turn", "turn_complete", {"ok": True}, **kwargs) == 2
    row, events = snapshot(store, sid)
    assert row["last_seq"] == 2
    assert row["turn_count"] == 1
    assert row["title"] == "First title"
    for key in ("active_turn_id", "turn_started_at", "active_turn_tier", "active_turn_subject"):
        assert row[key] is None
    assert [event[1] for event in events] == [1, 2]
    assert store.events_after(sid, 0)[1]["data"] == {"ok": True}


@pytest.mark.parametrize("event_type", ["turn_started", "turn_complete", "error"])
def test_stale_claim_changes_nothing_and_releases_transaction(stores, event_type):
    store = stores()
    sid = store.get_or_create_session("tenant", "drawing")["session_id"]
    assert store.try_begin_turn(sid, "turn", 300, "hosted_pro", "subject")
    store.append_event(sid, "turn", "claim", {})
    before = snapshot(store, sid)
    with pytest.raises(RuntimeError, match="sequence mismatch"):
        store.append_event(sid, "turn", event_type, {"text": "Must not title"}, expected_seq=1)
    assert snapshot(store, sid) == before
    assert not store._conn.in_transaction
    assert store.append_event(sid, "turn", "claim", {}, expected_seq=2) == 2


def test_sqlite_insert_exception_rolls_back_claim(stores):
    store = stores()
    sid = store.get_or_create_session("tenant", "drawing")["session_id"]
    before = snapshot(store, sid)
    # Abort after INSERT so rollback must discard a row, not merely a read.
    store._conn.execute(
        "CREATE TEMP TRIGGER reject_event AFTER INSERT ON session_events "
        "BEGIN SELECT RAISE(FAIL, 'injected failure'); END",
    )
    with pytest.raises(sqlite3.IntegrityError, match="injected failure"):
        store.append_event(sid, "turn", "turn_started", {"text": "Title"}, expected_seq=1)
    assert not store._conn.in_transaction
    assert snapshot(store, sid) == before


def test_sqlite_exception_after_cursor_update_rolls_back_metadata(stores, monkeypatch):
    store = stores()
    sid = store.get_or_create_session("tenant", "drawing")["session_id"]
    before = snapshot(store, sid)

    def fail_title(data):
        raise RuntimeError("injected title failure")

    monkeypatch.setattr(store, "title_from_turn_started", fail_title)
    with pytest.raises(RuntimeError, match="injected title failure"):
        store.append_event(sid, "turn", "turn_started", {"text": "Title"}, expected_seq=1)
    assert not store._conn.in_transaction
    assert snapshot(store, sid) == before


@pytest.mark.parametrize("mode", ["legacy", "shadow", "postgres", "dual_write", "dual_write_shadow"])
@pytest.mark.parametrize("invalid", [True, False, 0, -1, 1.0, "1", [], {}])
def test_invalid_sequence_rejected_before_backend_access(stores, monkeypatch, mode, invalid):
    store = stores()
    monkeypatch.setenv("LEAF_SESSIONS_STORE", mode)

    def touched(*args, **kwargs):
        pytest.fail("invalid sequence touched a backend")

    for name in ("_db", "_platform_db", "_legacy_append_event", "_pg_append_event", "_pg_ensure_started"):
        monkeypatch.setattr(store, name, touched)
    with pytest.raises(ValueError, match="positive integer"):
        store.append_event("missing", None, "claim", {}, expected_seq=invalid)


@pytest.mark.parametrize("expected", [None, 1])
def test_missing_sqlite_session_remains_key_error(stores, expected):
    store = stores()
    with pytest.raises(KeyError, match="unknown session_id"):
        store.append_event("missing", None, "claim", {}, expected_seq=expected)
    assert not store._conn.in_transaction


def test_same_sequence_independent_sqlite_connections_have_one_winner(stores):
    first, second = stores(), stores()
    assert first._lock is not second._lock
    assert first._conn is not second._conn
    sid = first.get_or_create_session("tenant", "drawing")["session_id"]
    barrier = threading.Barrier(2)
    results, failures = [], []
    traces = [[], []]
    for store, trace in zip((first, second), traces):
        store._conn.set_trace_callback(trace.append)

    def append(store):
        try:
            barrier.wait(timeout=5)
            results.append(store.append_event(sid, None, "claim", {}, expected_seq=1))
        except Exception as exc:
            failures.append(exc)

    threads = [threading.Thread(target=append, args=(store,)) for store in (first, second)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert results == [1]
    assert len(failures) == 1
    assert isinstance(failures[0], RuntimeError)
    assert "sequence mismatch" in str(failures[0])
    for trace in traces:
        assert trace[0] == "BEGIN IMMEDIATE"
        assert trace[1].startswith("SELECT last_seq")
    row, events = snapshot(first, sid)
    assert row["last_seq"] == 1
    assert len(events) == 1
    assert not first._conn.in_transaction and not second._conn.in_transaction


@pytest.mark.parametrize("mode", ["dual_write", "dual_write_shadow"])
def test_dual_write_refuses_before_either_store_is_touched(stores, monkeypatch, mode):
    store = stores()
    monkeypatch.setenv("LEAF_SESSIONS_STORE", mode)

    def touched(*args, **kwargs):
        pytest.fail("conditional dual-write reached a backend")

    for name in ("_db", "_platform_db", "_legacy_append_event", "_pg_append_event",
                 "_pg_ensure_started", "_pg_get_session", "_legacy_get_session"):
        monkeypatch.setattr(store, name, touched)
    with pytest.raises(RuntimeError, match="dual-write"):
        store.append_event("session", None, "claim", {}, expected_seq=1)


def test_shadow_conditional_append_only_writes_legacy(stores, monkeypatch):
    store = stores()
    sid = store.get_or_create_session("tenant", "drawing")["session_id"]
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "shadow")
    monkeypatch.setattr(store, "_platform_db", lambda: pytest.fail("shadow must not write PG"))
    assert store.append_event(sid, None, "claim", {}, expected_seq=1) == 1
    assert snapshot(store, sid)[0]["last_seq"] == 1


@pytest.mark.parametrize("mode", ["dual_write", "dual_write_shadow"])
def test_unconditional_dual_write_keeps_existing_mirror_protocol(stores, monkeypatch, mode):
    store = stores()
    sid = store.get_or_create_session("tenant", "drawing")["session_id"]
    calls = []
    monkeypatch.setenv("LEAF_SESSIONS_STORE", mode)
    monkeypatch.setattr(store, "_pg_ensure_started", lambda: calls.append("ready"))
    monkeypatch.setattr(store, "_pg_get_session", lambda sid: {"session_id": sid})
    monkeypatch.setattr(store, "_pg_append_event", lambda *args, **kwargs: calls.append((args, kwargs)))
    assert store.append_event(sid, None, "claim", {}) == 1
    assert calls == ["ready", ((sid, None, "claim", {}), {
        "expected_seq": 1, "updated_at": snapshot(store, sid)[0]["updated_at"],
    })]


class TransactionalPG:
    """Inert transaction double; no platform import or database URL access."""

    def __init__(self, *, missing=False, fail_insert=False):
        self.row = None if missing else {"last_seq": 0, "updated_at": 12.0}
        self.events = []
        self.statements = []
        self.outcomes = []
        self.fail_insert = fail_insert

    @contextmanager
    def transaction(self):
        before = copy.deepcopy((self.row, self.events))
        try:
            yield self
        except BaseException:
            self.row, self.events = before
            self.outcomes.append("rollback")
            raise
        else:
            self.outcomes.append("commit")

    def execute(self, sql, args):
        self.statements.append((sql, args))
        if sql.startswith("UPDATE app_sessions SET last_seq"):
            if self.row is not None:
                self.row["last_seq"] += 1
                self.row["updated_at"] = args[0]
        elif sql.startswith("INSERT INTO app_session_events"):
            self.events.append(args)
            if self.fail_insert:
                raise RuntimeError("injected PG insert failure")
        else:
            pytest.fail(f"unexpected SQL: {sql}")
        return self

    def fetchone(self):
        return copy.deepcopy(self.row)


@pytest.mark.parametrize("expected", [None, 1, 2])
def test_public_postgres_forwarding_and_transaction_rollback(stores, monkeypatch, expected):
    store = stores()
    pg = TransactionalPG()
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "postgres")
    monkeypatch.setattr(store, "_platform_db", lambda: pg)
    monkeypatch.setattr(store, "_legacy_append_event", lambda *a, **k: pytest.fail("legacy fallback"))
    before = copy.deepcopy(pg.row)
    if expected == 2:
        with pytest.raises(RuntimeError, match="sequence mismatch"):
            store.append_event("session", None, "claim", {}, expected_seq=expected)
        assert pg.row == before
        assert pg.events == []
        assert pg.outcomes == ["rollback"]
        assert len(pg.statements) == 1  # mismatch exits before event INSERT
    else:
        assert store.append_event("session", None, "claim", {}, expected_seq=expected) == 1
        assert pg.row["last_seq"] == 1
        assert len(pg.events) == 1
        assert pg.events[0][:4] == ("session", 1, None, "claim")
        assert pg.outcomes == ["commit"]


@pytest.mark.parametrize("missing", [False, True])
def test_postgres_missing_or_insert_failure_rolls_back(stores, monkeypatch, missing):
    store = stores()
    pg = TransactionalPG(missing=missing, fail_insert=not missing)
    monkeypatch.setenv("LEAF_SESSIONS_STORE", "postgres")
    monkeypatch.setattr(store, "_platform_db", lambda: pg)
    before = copy.deepcopy(pg.row)
    with pytest.raises(KeyError if missing else RuntimeError):
        store.append_event("session", None, "claim", {}, expected_seq=1)
    assert pg.row == before
    assert pg.events == []
    assert pg.outcomes == ["rollback"]
