"""
Tests for server/session_store.py (S1 lane — sessions wire, Store API v1).

SESSIONS_DB is redirected to a fresh tmp path BEFORE the first import of
session_store (the module reads the env var once, at import time — identical
posture to jobs.py/JOBS_DB; see tests/test_wave2.py etc. for the precedent).
This guarantees the real server/sessions.db is never touched by this suite
(acceptance criterion (e)) regardless of collection order or working directory.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import threading
import time
from pathlib import Path

_TMP_DIR = Path(tempfile.mkdtemp(prefix="sessionstore-test-"))
_TMP_DB = _TMP_DIR / "sessions.db"
os.environ.setdefault("SESSIONS_DB", str(_TMP_DB))

import session_store  # noqa: E402  (import after env redirect, by design)

SERVER_DIR = Path(__file__).resolve().parent.parent
REAL_DB_PATH = SERVER_DIR / "sessions.db"

# The live :8130 stack legitimately owns a real server/sessions.db on a dev
# machine — its EXISTENCE is not test pollution. The invariant is that this
# SUITE never touches it: snapshot (mtime, size) at import, assert unchanged.
def _real_db_snapshot():
    try:
        st = REAL_DB_PATH.stat()
        return (st.st_mtime_ns, st.st_size)
    except FileNotFoundError:
        return None
_REAL_DB_AT_IMPORT = _real_db_snapshot()


# --------------------------------------------------------------------------- #
# (e) SESSIONS_DB tmp-path isolation
# --------------------------------------------------------------------------- #
def test_sessions_db_isolated_to_tmp_path():
    # Under a FULL-SUITE run another sessions-consuming test module may win the
    # import race and bind SESSIONS_DB to ITS tmp dir before this module's
    # setdefault runs — that is fine. The invariant that matters (and the only
    # one this asserts) is that the bound path is a redirected tmp path, never
    # the real server/sessions.db, and that the real DB is never created.
    assert session_store.DB_PATH != (SERVER_DIR / "sessions.db")
    assert "sessions.db" in session_store.DB_PATH.name
    session_store.ensure_started()
    assert session_store.DB_PATH.exists()
    # the real, non-test sessions.db must never be created by this suite
    assert _real_db_snapshot() == _REAL_DB_AT_IMPORT, (
        f"this suite MODIFIED the real DB at {REAL_DB_PATH} "
        f"(snapshot changed since import) — SESSIONS_DB redirect leaked"
    )


# --------------------------------------------------------------------------- #
# basic session CRUD-ish behavior
# --------------------------------------------------------------------------- #
def test_get_or_create_session_shape_and_lookup():
    sess = session_store.get_or_create_session("tenant-shape", "drawing-1")
    assert sess["tenant_id"] == "tenant-shape"
    assert sess["drawing_id"] == "drawing-1"
    assert sess["status"] == "active"
    assert sess["last_seq"] == 0
    assert sess["active_turn_id"] is None
    assert sess["turn_started_at"] is None
    assert isinstance(sess["session_id"], str) and sess["session_id"]

    fetched = session_store.get_session(sess["session_id"])
    assert fetched == sess


def test_get_or_create_session_idempotent_same_key():
    first = session_store.get_or_create_session("tenant-idem", "drawing-a")
    second = session_store.get_or_create_session("tenant-idem", "drawing-a")
    assert first["session_id"] == second["session_id"]

    third = session_store.get_or_create_session("tenant-idem", "drawing-b")
    assert third["session_id"] != first["session_id"]


def test_get_session_unknown_returns_none():
    assert session_store.get_session("no-such-session-id") is None


# --------------------------------------------------------------------------- #
# (b) 2-thread get_or_create_session race -> exactly one row
# --------------------------------------------------------------------------- #
def test_get_or_create_session_race_exactly_one_row():
    tenant_id, drawing_id = "tenant-race", "drawing-race"
    barrier = threading.Barrier(2)
    results = [None, None]
    errors = []

    def worker(idx):
        try:
            barrier.wait(timeout=5)
            results[idx] = session_store.get_or_create_session(tenant_id, drawing_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors, f"unexpected errors: {errors}"
    assert results[0] is not None and results[1] is not None
    assert results[0]["session_id"] == results[1]["session_id"]

    rows = session_store._query(
        "SELECT session_id FROM sessions WHERE tenant_id = ? AND drawing_id = ?",
        (tenant_id, drawing_id),
    )
    assert len(rows) == 1


# --------------------------------------------------------------------------- #
# (a) 16-thread append_event hammer -> gapless monotonic seq, zero PK violations
# --------------------------------------------------------------------------- #
def test_append_event_hammer_gapless_monotonic_seq():
    sess = session_store.get_or_create_session("tenant-hammer", "drawing-hammer")
    session_id = sess["session_id"]

    n_threads = 16
    events_per_thread = 40
    total = n_threads * events_per_thread

    seqs_lock = threading.Lock()
    seqs = []
    errors = []
    barrier = threading.Barrier(n_threads)

    def worker(thread_idx):
        try:
            barrier.wait(timeout=10)
            for i in range(events_per_thread):
                seq = session_store.append_event(
                    session_id, f"turn-{thread_idx}",
                    "text_delta", {"thread": thread_idx, "i": i},
                )
                with seqs_lock:
                    seqs.append(seq)
        except Exception as exc:  # noqa: BLE001
            with seqs_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f"append_event raised under concurrency: {errors}"
    assert len(seqs) == total, f"expected {total} appended events, got {len(seqs)}"

    # zero PK violations -> every seq unique
    assert len(set(seqs)) == total, "duplicate seq values allocated under concurrency"
    # gapless monotonic -> the set of seqs is exactly {1, ..., total}
    assert sorted(seqs) == list(range(1, total + 1))

    # the durable log agrees: same count, contiguous seq, ascending order
    stored = session_store.events_after(session_id, after_seq=0, limit=total + 10)
    assert len(stored) == total
    assert [e["seq"] for e in stored] == list(range(1, total + 1))
    for e in stored:
        assert e["v"] == 1
        assert e["session_id"] == session_id
        assert e["type"] == "text_delta"

    # sessions.last_seq reflects the final allocation
    final = session_store.get_session(session_id)
    assert final["last_seq"] == total


def test_events_after_and_recent_events():
    sess = session_store.get_or_create_session("tenant-events", "drawing-events")
    session_id = sess["session_id"]
    for i in range(5):
        session_store.append_event(session_id, "turn-1", "text_delta", {"i": i})

    after2 = session_store.events_after(session_id, after_seq=2)
    assert [e["seq"] for e in after2] == [3, 4, 5]

    recent3 = session_store.recent_events(session_id, limit=3)
    assert [e["seq"] for e in recent3] == [3, 4, 5]  # ascending, most-recent 3

    all_events = session_store.recent_events(session_id, limit=100)
    assert [e["seq"] for e in all_events] == [1, 2, 3, 4, 5]


def test_append_event_unknown_session_raises_keyerror():
    try:
        session_store.append_event("does-not-exist", "turn-x", "error", {})
        assert False, "expected KeyError"
    except KeyError:
        pass


# --------------------------------------------------------------------------- #
# (c) try_begin_turn CAS: second caller False, stale takeover True
# --------------------------------------------------------------------------- #
def test_try_begin_turn_cas_and_stale_takeover():
    sess = session_store.get_or_create_session("tenant-turn", "drawing-turn")
    session_id = sess["session_id"]

    ok1 = session_store.try_begin_turn(session_id, "turn-1", stale_after_s=0.05)
    assert ok1 is True

    mid = session_store.get_session(session_id)
    assert mid["active_turn_id"] == "turn-1"

    # a second caller, immediately, sees a live (non-stale) turn -> rejected
    ok2 = session_store.try_begin_turn(session_id, "turn-2", stale_after_s=0.05)
    assert ok2 is False
    still = session_store.get_session(session_id)
    assert still["active_turn_id"] == "turn-1"  # unchanged by the rejected caller

    # after the staleness window elapses, a new caller takes over
    time.sleep(0.15)
    ok3 = session_store.try_begin_turn(session_id, "turn-3", stale_after_s=0.05)
    assert ok3 is True
    took_over = session_store.get_session(session_id)
    assert took_over["active_turn_id"] == "turn-3"


def test_try_begin_turn_unknown_session_returns_false():
    assert session_store.try_begin_turn("no-such-session", "turn-x", stale_after_s=1.0) is False


def test_end_turn_clears_only_on_match():
    sess = session_store.get_or_create_session("tenant-endturn", "drawing-endturn")
    session_id = sess["session_id"]
    assert session_store.try_begin_turn(session_id, "turn-a", stale_after_s=60) is True

    # end_turn with a mismatched turn_id is a no-op
    session_store.end_turn(session_id, "turn-wrong")
    mid = session_store.get_session(session_id)
    assert mid["active_turn_id"] == "turn-a"

    # matching turn_id clears it
    session_store.end_turn(session_id, "turn-a")
    cleared = session_store.get_session(session_id)
    assert cleared["active_turn_id"] is None
    assert cleared["turn_started_at"] is None

    # once cleared, a fresh turn can begin immediately (no staleness needed)
    assert session_store.try_begin_turn(session_id, "turn-b", stale_after_s=60) is True


def test_active_turn_tier_is_bound_to_session_turn_and_tenant():
    sess = session_store.get_or_create_session("tenant-tier", "drawing-tier")
    session_id = sess["session_id"]
    assert session_store.try_begin_turn(
        session_id, "turn-tier", stale_after_s=60, tier="hosted_pro") is True

    assert session_store.active_turn_tier(
        session_id, "turn-tier", "tenant-tier") == "hosted_pro"
    assert session_store.active_turn_tier(
        session_id, "wrong-turn", "tenant-tier") is None
    assert session_store.active_turn_tier(
        session_id, "turn-tier", "wrong-tenant") is None

    session_store.end_turn(session_id, "turn-tier")
    assert session_store.active_turn_tier(
        session_id, "turn-tier", "tenant-tier") is None


def test_try_begin_turn_concurrent_only_one_winner():
    sess = session_store.get_or_create_session("tenant-turnrace", "drawing-turnrace")
    session_id = sess["session_id"]
    n = 8
    barrier = threading.Barrier(n)
    results = [None] * n
    errors = []

    def worker(idx):
        try:
            barrier.wait(timeout=5)
            results[idx] = session_store.try_begin_turn(
                session_id, f"turn-race-{idx}", stale_after_s=60
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    assert results.count(True) == 1
    assert results.count(False) == n - 1


# --------------------------------------------------------------------------- #
# approvals
# --------------------------------------------------------------------------- #
def test_create_and_get_approval_roundtrip():
    sess = session_store.get_or_create_session("tenant-appr", "drawing-appr")
    session_id = sess["session_id"]
    confirmation_id = "confirm-roundtrip-1"

    session_store.create_approval(
        confirmation_id, session_id, "tenant-appr", "turn-1",
        tool="write_wire_run", params={"length_ft": 12},
        capability="drawing.write", rationale="adds a home-run",
        kind="proposed_run", payload={"preview": "ok"}, ttl_s=60,
    )

    got = session_store.get_approval(confirmation_id)
    assert got is not None
    assert got["confirmation_id"] == confirmation_id
    assert got["session_id"] == session_id
    assert got["tool"] == "write_wire_run"
    assert got["params"] == {"length_ft": 12}
    assert got["capability"] == "drawing.write"
    assert got["decided"] is False
    assert got["approved"] is None
    assert got["expired"] is False


def test_get_approval_expired_flag_computed():
    sess = session_store.get_or_create_session("tenant-appr-exp", "drawing-appr-exp")
    confirmation_id = "confirm-expired-1"
    session_store.create_approval(
        confirmation_id, sess["session_id"], "tenant-appr-exp", "turn-1",
        tool="t", params={}, capability="c", rationale="r",
        kind="proposed_run", payload={}, ttl_s=0.05,
    )
    time.sleep(0.15)
    got = session_store.get_approval(confirmation_id)
    assert got["expired"] is True


def test_get_approval_not_found():
    assert session_store.get_approval("no-such-confirmation") is None


# --------------------------------------------------------------------------- #
# (d) decide_approval second call -> 'already_decided'
# --------------------------------------------------------------------------- #
def test_decide_approval_recorded_then_already_decided_then_not_found():
    sess = session_store.get_or_create_session("tenant-decide", "drawing-decide")
    confirmation_id = "confirm-decide-1"
    session_store.create_approval(
        confirmation_id, sess["session_id"], "tenant-decide", "turn-1",
        tool="t", params={}, capability="c", rationale="r",
        kind="proposed_run", payload={}, ttl_s=60,
    )

    outcome1 = session_store.decide_approval(confirmation_id, True, by="user-1")
    assert outcome1 == "recorded"

    got = session_store.get_approval(confirmation_id)
    assert got["decided"] is True
    assert got["approved"] is True
    assert got["decided_by"] == "user-1"

    outcome2 = session_store.decide_approval(confirmation_id, False, by="user-2")
    assert outcome2 == "already_decided"

    # the second call must NOT have overwritten the first decision
    still = session_store.get_approval(confirmation_id)
    assert still["approved"] is True
    assert still["decided_by"] == "user-1"

    outcome3 = session_store.decide_approval("no-such-confirmation", True, by="user-3")
    assert outcome3 == "not_found"


def test_decide_approval_concurrent_only_one_recorded():
    sess = session_store.get_or_create_session("tenant-decide-race", "drawing-decide-race")
    confirmation_id = "confirm-decide-race-1"
    session_store.create_approval(
        confirmation_id, sess["session_id"], "tenant-decide-race", "turn-1",
        tool="t", params={}, capability="c", rationale="r",
        kind="proposed_run", payload={}, ttl_s=60,
    )

    n = 10
    barrier = threading.Barrier(n)
    results = [None] * n
    errors = []

    def worker(idx):
        try:
            barrier.wait(timeout=5)
            results[idx] = session_store.decide_approval(
                confirmation_id, idx % 2 == 0, by=f"user-{idx}"
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not errors
    assert results.count("recorded") == 1
    assert results.count("already_decided") == n - 1


# --------------------------------------------------------------------------- #
# final isolation re-check (belt-and-suspenders, runs after everything above)
# --------------------------------------------------------------------------- #
def test_legacy_db_without_consumed_column_is_migrated(tmp_path):
    """A database created by the pre-`consumed` cut of the approvals table must be
    migrated additively on open: CREATE TABLE IF NOT EXISTS never retrofits a column
    onto an existing table, while consume_approval() SELECTs/UPDATEs `consumed` — an
    un-migrated DB would 500 every confirm. The migration must also leave a
    pre-existing decided-but-unconsumed row consumable exactly once."""
    legacy = tmp_path / "legacy-sessions.db"
    conn = sqlite3.connect(str(legacy))
    conn.execute(
        """CREATE TABLE approvals (
             confirmation_id TEXT PRIMARY KEY, session_id TEXT, tenant_id TEXT,
             turn_id TEXT, tool TEXT, params_json TEXT, capability TEXT,
             rationale TEXT, kind TEXT, payload_json TEXT,
             decided INTEGER DEFAULT 0, approved INTEGER, decided_by TEXT,
             created_at REAL, expires_at REAL)"""
    )
    conn.execute(
        "INSERT INTO approvals (confirmation_id, session_id, tenant_id, kind,"
        " decided, approved, created_at, expires_at)"
        " VALUES ('c-legacy', 's-legacy', 't-legacy', 'tool_run', 1, 1, ?, ?)",
        (time.time(), time.time() + 3600),
    )
    conn.commit()
    conn.close()

    saved_conn, saved_path = session_store._conn, session_store.DB_PATH
    session_store._conn = None
    session_store.DB_PATH = legacy
    try:
        session_store.ensure_started()
        cols = {
            r[1]
            for r in session_store._conn.execute("PRAGMA table_info(approvals)").fetchall()
        }
        assert "consumed" in cols, "migration did not add the consumed column"

        row = session_store.consume_approval("c-legacy", "s-legacy", "t-legacy")
        assert row["confirmation_id"] == "c-legacy"
        try:
            session_store.consume_approval("c-legacy", "s-legacy", "t-legacy")
            raise AssertionError("second consume of a migrated row must fail")
        except session_store.ApprovalConsumeError as e:
            assert e.reason == "already_consumed"

        # Idempotence: re-opening an ALREADY-migrated DB must not fail or double-add.
        session_store._conn.close()
        session_store._conn = None
        session_store.ensure_started()
        cols2 = [
            r[1]
            for r in session_store._conn.execute("PRAGMA table_info(approvals)").fetchall()
        ]
        assert cols2.count("consumed") == 1
    finally:
        if session_store._conn is not None:
            session_store._conn.close()
        session_store._conn = saved_conn
        session_store.DB_PATH = saved_path


def test_zzz_real_sessions_db_still_absent():
    assert _real_db_snapshot() == _REAL_DB_AT_IMPORT, (
        f"this suite MODIFIED the real DB at {REAL_DB_PATH} "
        f"(snapshot changed since import) — SESSIONS_DB redirect leaked"
    )
