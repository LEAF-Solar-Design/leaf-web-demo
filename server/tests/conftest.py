"""Global test-process isolation for import-time SQLite defaults.

Several application modules bind their database path when first imported.
Pytest imports test modules during collection, so a broad suite can import the
application before a module-specific ``setdefault`` runs. Route those defaults
to one fresh session directory before collection can touch the real developer
databases.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest


_TEST_STATE = Path(tempfile.mkdtemp(prefix="leaf-server-pytest-"))
os.environ.setdefault("SESSIONS_DB", str(_TEST_STATE / "sessions.db"))
os.environ.setdefault("JOBS_DB", str(_TEST_STATE / "jobs.db"))
os.environ.setdefault("LEAF_UPLOAD_IMPORT_MUTATIONS_ENABLED", "1")


def _jobs_connection_is_closed(conn: sqlite3.Connection) -> bool:
    """True when `conn` is a closed handle. `in_transaction` has no side effects."""
    try:
        conn.in_transaction
    except sqlite3.ProgrammingError:
        return True
    return False


@pytest.fixture(autouse=True)
def _drop_closed_jobs_connection():
    """Never hand the next test a CLOSED ``jobs._conn``.

    Many modules isolate their own SQLite file by monkeypatching ``jobs.DB_PATH``
    plus ``jobs._conn``, then closing the connection they built. On teardown
    monkeypatch restores the PRIOR ``jobs._conn`` -- and when that prior value is
    itself an already-closed handle, the dead handle propagates forward instead of
    being cleared. ``jobs._db()`` returns the module singleton whenever it is not
    None, so every later test that touches the job store raises
    ``sqlite3.ProgrammingError: Cannot operate on a closed database``, and the
    job-reaper daemon spins on the same dead handle every interval.

    This is autouse, so it is set up before each test's own explicitly requested
    fixtures and therefore finalized AFTER their teardown -- including after
    monkeypatch's undo, which is the step that reinstates the dead handle.
    Clearing it to None makes the next ``_db()`` rebuild the connection.
    """
    yield
    jobs = sys.modules.get("jobs")
    if jobs is None:
        return
    conn = getattr(jobs, "_conn", None)
    if conn is not None and _jobs_connection_is_closed(conn):
        jobs._conn = None
