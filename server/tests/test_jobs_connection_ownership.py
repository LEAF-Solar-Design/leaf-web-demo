"""
The jobs module OWNS its SQLite singleton; no test may close it in place.

THE BUG THIS PINS. `jobs._db()` hands the SAME connection object to every
caller, so a closed handle left in `jobs._conn` is not local damage: every later
read or write raises `sqlite3.ProgrammingError: Cannot operate on a closed
database`. `tests/test_da_callback.py` used to do

    if jobs._conn is not None:
        jobs._conn.close()                      # dead handle stays IN _conn
    monkeypatch.setattr(jobs, "DB_PATH", ...)
    monkeypatch.setattr(jobs, "_conn", None)    # records the dead handle

so monkeypatch's undo REINSTATED the dead handle at teardown, and every module
that later re-pointed `_conn` restored it again. In one process the poison
travelled from tests/test_da_callback.py all the way to tests/test_wave5.py:
5 tests in tests/test_job_lanes.py, tests/test_ui_wave.py::
test_close_accepts_bodyless_post and 4 /api/run tests in tests/test_wave5.py
returned HTTP 500. Every one of those files passed on its own, and CI runs each
file in its own pytest process, so only the monolithic `pytest tests/` run ever
saw it.

Three layers are pinned here:
  reset_connection  the module-owned close-and-clear that replaces every
                    in-place `jobs._conn.close()`
  the source rule   no test file closes `jobs._conn` in place again
  the conftest net  a dead handle reinstated after monkeypatch's undo is still
                    cleared before the next test runs (subprocess, because the
                    net acts BETWEEN items)

Run:  cd server && python -m pytest tests/test_jobs_connection_ownership.py -q
"""
from __future__ import annotations

import ast
import shutil
import subprocess
import sqlite3
import sys
import textwrap
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import jobs  # noqa: E402

TESTS_DIR = Path(__file__).resolve().parent
CONFTEST = TESTS_DIR / "conftest.py"


def _is_closed(conn: sqlite3.Connection) -> bool:
    try:
        conn.in_transaction
    except sqlite3.ProgrammingError:
        return True
    return False


# --------------------------------------------------------------------------- #
# reset_connection: the module-owned close-and-clear
# --------------------------------------------------------------------------- #
def test_reset_connection_closes_the_handle_it_drops(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "DB_PATH", tmp_path / "own.db")
    monkeypatch.setattr(jobs, "_conn", None)

    conn = jobs._db()
    assert not _is_closed(conn)

    jobs.reset_connection()

    assert jobs._conn is None          # nothing dead left in the singleton
    assert _is_closed(conn)            # and the handle really was closed


def test_reset_connection_is_a_noop_when_there_is_no_singleton(monkeypatch):
    monkeypatch.setattr(jobs, "_conn", None)
    jobs.reset_connection()
    assert jobs._conn is None


def test_db_rebuilds_a_usable_connection_after_reset(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "DB_PATH", tmp_path / "rebuild.db")
    monkeypatch.setattr(jobs, "_conn", None)

    first = jobs._db()
    jobs.reset_connection()
    second = jobs._db()

    assert second is not first
    assert second.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# the source rule: nobody closes the singleton in place again
# --------------------------------------------------------------------------- #
def _closes_the_jobs_singleton(node: ast.AST) -> bool:
    """True for `<anything>.<jobs-ish>._conn.close()` — the banned shape."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "close":
        return False
    target = func.value
    return isinstance(target, ast.Attribute) and target.attr == "_conn" and (
        isinstance(target.value, ast.Name) and "jobs" in target.value.id)


def test_no_test_module_closes_the_jobs_singleton_in_place():
    """`jobs.reset_connection()` is the only sanctioned way to drop it.

    Static, because the dynamic symptom shows up in a DIFFERENT file from the
    one at fault, several modules later, and only in the monolithic run.
    """
    offenders = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if _closes_the_jobs_singleton(node):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], (
        "these close jobs._conn in place, leaving a dead handle in the module "
        "singleton (and monkeypatch will reinstate it on teardown). Call "
        f"jobs.reset_connection() instead: {offenders}")


def test_the_ban_would_catch_the_shape_it_bans():
    """The detector is load-bearing, not a filter that matches nothing."""
    banned = ast.parse("jobs._conn.close()").body[0].value
    allowed = ast.parse("jobs.reset_connection()").body[0].value
    local = ast.parse("conn.close()").body[0].value
    assert _closes_the_jobs_singleton(banned)
    assert not _closes_the_jobs_singleton(allowed)
    assert not _closes_the_jobs_singleton(local)


# --------------------------------------------------------------------------- #
# the conftest net: still clears a handle reinstated by monkeypatch's undo
# --------------------------------------------------------------------------- #
_POISON_SUITE = '''\
import sys
from pathlib import Path

sys.path.insert(0, r"{server_dir}")
import jobs

jobs.DB_PATH = Path(r"{db_path}")


def test_a_poisons_the_singleton(monkeypatch, tmp_path):
    """The exact pre-fix shape: close in place, THEN let monkeypatch record it."""
    jobs._db()
    jobs._conn.close()                                  # dead handle stays in _conn
    monkeypatch.setattr(jobs, "DB_PATH", tmp_path / "iso.db")
    monkeypatch.setattr(jobs, "_conn", None)            # records the dead handle
    jobs._db()                                          # teardown restores the dead one


def test_b_still_gets_a_live_singleton():
    jobs._db().execute("SELECT COUNT(*) FROM jobs").fetchone()
'''


def test_a_dead_handle_reinstated_by_monkeypatch_is_cleared_before_the_next_test(tmp_path):
    """Two ITEMS are needed: the net acts between them, so one process, two tests.

    Runs the REAL tests/conftest.py (copied, not re-implemented) over a suite
    whose first test reproduces the ownership violation verbatim. Without the
    net, `test_b` dies on `Cannot operate on a closed database`.
    """
    shutil.copy(CONFTEST, tmp_path / "conftest.py")
    (tmp_path / "test_poison.py").write_text(
        _POISON_SUITE.format(server_dir=SERVER_DIR,
                             db_path=tmp_path / "shared.db"),
        encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         str(tmp_path / "test_poison.py")],
        cwd=tmp_path, capture_output=True, text=True, timeout=300)

    assert "Cannot operate on a closed database" not in proc.stdout, proc.stdout
    assert proc.returncode == 0, proc.stdout + proc.stderr
    # ...and it was the net that saved it, not luck: the clear is reported.
    assert "CLOSED handle in jobs._conn" in proc.stdout, textwrap.shorten(
        "the net never fired, so this suite no longer reproduces the bug it "
        f"pins: {proc.stdout}", 2000)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
