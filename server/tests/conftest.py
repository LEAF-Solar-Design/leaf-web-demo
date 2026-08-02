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
# app.py starts the guest-purge daemon at import time. It must stay out of the
# shared pytest process because its delayed sweeps otherwise race test stores.
os.environ.setdefault("LEAF_GUEST_PURGE_DISABLED", "1")
os.environ.setdefault("LEAF_CUSTOMIZATION_STAGE_WORKER_DISABLED", "1")


def _jobs_connection_is_closed(conn: sqlite3.Connection) -> bool:
    """True when `conn` is a closed handle. `in_transaction` has no side effects."""
    try:
        conn.in_transaction
    except sqlite3.ProgrammingError:
        return True
    return False


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item, nextitem):
    """Never hand the next test a CLOSED ``jobs._conn``.

    ``jobs._db()`` returns the module singleton whenever it is not None, so a
    dead handle left in ``jobs._conn`` makes every later test that touches the
    job store raise ``sqlite3.ProgrammingError: Cannot operate on a closed
    database``, and the job-reaper daemon spins on the same dead handle every
    interval. Tests own their isolation, not the module's connection: they must
    call ``jobs.reset_connection()``, which closes and clears in one step. This
    is the net for the ones that do not.

    A HOOK, not an autouse fixture. A fixture only finalizes at its own scope,
    so a function-scoped one is already gone by the time a module-, class- or
    session-scoped finalizer runs. This wrapper's post-yield body runs after
    ``teardown_exact`` has popped EVERY finalizer this item triggers, including
    the higher-scoped ones that fire because ``nextitem`` is in another module,
    and including monkeypatch's undo -- which is the step that reinstates a dead
    handle when a test closed ``_conn`` in place before monkeypatching it.

    The clear is reported rather than absorbed: a silent net is how the
    ownership violation in test_da_callback.py survived a green CI.
    """
    yield
    jobs = sys.modules.get("jobs")
    if jobs is None:
        return
    conn = getattr(jobs, "_conn", None)
    if conn is not None and _jobs_connection_is_closed(conn):
        jobs._conn = None
        item.warn(pytest.PytestWarning(
            "teardown left a CLOSED handle in jobs._conn; cleared it so later "
            "tests can rebuild. Use jobs.reset_connection() instead of closing "
            "jobs._conn in place."))
