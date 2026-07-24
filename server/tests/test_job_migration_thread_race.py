"""Concurrent first-connect from multiple THREADS in one process must build the
jobs.db connection exactly once.

Companion to test_job_migration_concurrent.py, which covers concurrent PROCESSES
(separate memory, separate _conn globals). This covers the in-process thread race
sol-critic flagged on PR #97: the jobs DB is lazy (first-connect happens on the
first request, and jobs.ensure_started() calls jobs._db() UNLOCKED), so several
FastAPI worker threads can enter jobs._db() with _conn is None at once.

Before the double-checked-lock hardening, each racing thread ran the full build
(sqlite3.connect + WAL + migrations) and then executed the UNCONDITIONAL
``_conn = conn``. That both leaked all-but-one connection AND let a slow second
builder flip _conn A->B after A was already handed out — so a caller that reads
_db() separately for execute and commit (_exec, update_progress, release_for_retry)
could execute on A but commit on B, silently stranding the write on the discarded
connection.

The fix serializes the build under _conn_lock with a re-check, so exactly one
connection is ever created and every thread returns that same object. This test
asserts BOTH invariants: sqlite3.connect is called exactly once, and all threads
observe the identical connection.

Run:  cd server && python -m pytest tests/test_job_migration_thread_race.py -q
"""
from __future__ import annotations

# Cache the STDLIB `platform` module before any project dir shadows it (the local
# platform/ package), matching the sibling job tests' defensive import order.
import platform as _stdlib_platform  # noqa: E402
_stdlib_platform.python_implementation()

import os  # noqa: E402
import sqlite3  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

os.environ.setdefault("LEAF_JOBS_STORE", "legacy")

import jobs  # noqa: E402

_N_THREADS = 8


def test_first_connect_thread_race_builds_one_connection(tmp_path, monkeypatch):
    # Point jobs at a fresh, never-opened DB and reset the singleton so this test
    # genuinely exercises the None -> built transition under a thread stampede.
    db_path = tmp_path / "jobs.db"
    monkeypatch.setattr(jobs, "DB_PATH", db_path)
    monkeypatch.setattr(jobs, "_conn", None)

    real_connect = sqlite3.connect
    connect_calls = 0
    connect_calls_lock = threading.Lock()

    def counting_connect(*args, **kwargs):
        nonlocal connect_calls
        with connect_calls_lock:
            connect_calls += 1
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(jobs.sqlite3, "connect", counting_connect)

    barrier = threading.Barrier(_N_THREADS)
    results: list[sqlite3.Connection] = [None] * _N_THREADS
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def worker(i: int) -> None:
        # Release all threads at the same instant so their _conn-is-None checks and
        # builds genuinely overlap.
        barrier.wait()
        try:
            results[i] = jobs._db()
        except BaseException as exc:  # capture, don't swallow — the test asserts none
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(_N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not any(t.is_alive() for t in threads), "a worker thread hung on _db()"
    assert not errors, f"_db() raised under the thread race: {errors[0]!r}"

    # Exactly one real connection was built: no leak, and _conn never flipped.
    assert connect_calls == 1, f"expected 1 connection build, got {connect_calls}"

    # Every thread observed the identical singleton (same object identity).
    assert all(r is jobs._conn for r in results), "threads saw divergent connections"

    # And the winner is a usable, migrated connection.
    cols = {row[1] for row in jobs._conn.execute("PRAGMA table_info(jobs)")}
    assert "dwg_version" in cols
