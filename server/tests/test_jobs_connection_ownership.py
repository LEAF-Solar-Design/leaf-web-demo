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
import threading
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


def test_reset_cannot_land_between_a_writers_execute_and_its_commit(monkeypatch, tmp_path):
    """PR #215 review finding: a reset that only took `_conn_lock` lost writes.

    `_exec` reads `_db()` separately for the execute and the commit, both under
    `_lock`. A reset landing between them closes the connection the write sits
    on (rolling it back), and the commit then lands on a freshly built second
    connection with no transaction -- silent data loss, reported success.
    `reset_connection()` must therefore take `_lock` too.

    Asserted as an IMPLICATION, not a stopwatch: while `_lock` is held the
    singleton must still be INTACT (a reset that ignored `_lock` would have
    already cleared it by now), and once released the reset must complete.
    """
    monkeypatch.setattr(jobs, "DB_PATH", tmp_path / "race.db")
    monkeypatch.setattr(jobs, "_conn", None)
    original = jobs._db()

    finished = threading.Event()

    def contender():
        jobs.reset_connection()
        finished.set()

    with jobs._lock:                      # stand in for _exec's critical section
        thread = threading.Thread(target=contender, daemon=True)
        thread.start()
        finished.wait(timeout=1.0)
        # THE ASSERTION. Under the pre-fix `_conn_lock`-only reset this is None.
        assert jobs._conn is original, "reset_connection() ran while _lock was held"

    thread.join(timeout=10)
    assert finished.is_set(), "reset_connection() never completed after _lock was released"
    assert jobs._conn is None
    assert _is_closed(original)


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
def _jobs_aliases(tree: ast.AST) -> set[str]:
    """Every local name bound to the `jobs` module in this file.

    Resolved per-file rather than matched by substring: `import jobs as j`
    binds a name with no "jobs" in it, and `session_store._conn.close()` is a
    DIFFERENT module's singleton that this rule has no business policing.
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "jobs":
                    names.add(alias.asname or "jobs")
    return names


def _closes_the_jobs_singleton(node: ast.AST, aliases: set[str]) -> bool:
    """True for `<jobs-alias>._conn.close()` — the banned shape."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr != "close":
        return False
    target = func.value
    return (isinstance(target, ast.Attribute) and target.attr == "_conn"
            and isinstance(target.value, ast.Name) and target.value.id in aliases)


def test_no_test_module_closes_the_jobs_singleton_in_place():
    """`jobs.reset_connection()` is the only sanctioned way to drop it.

    Static, because the dynamic symptom shows up in a DIFFERENT file from the
    one at fault, several modules later, and only in the monolithic run.
    """
    offenders = []
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        aliases = _jobs_aliases(tree)
        for node in ast.walk(tree):
            if _closes_the_jobs_singleton(node, aliases):
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == [], (
        "these close jobs._conn in place, leaving a dead handle in the module "
        "singleton (and monkeypatch will reinstate it on teardown). Call "
        f"jobs.reset_connection() instead: {offenders}")


@pytest.mark.parametrize("source, banned", [
    ("import jobs\njobs._conn.close()", True),
    ("import jobs as j\nj._conn.close()", True),          # alias, no "jobs" in it
    ("import jobs as jobs_mod\njobs_mod._conn.close()", True),
    ("import jobs\njobs.reset_connection()", False),
    ("import jobs\nconn = jobs._db()\nconn.close()", False),   # a borrowed local
    ("import session_store\nsession_store._conn.close()", False),  # another module
])
def test_the_ban_catches_the_shape_it_bans(source, banned):
    """The detector is load-bearing, not a filter that matches nothing."""
    tree = ast.parse(source)
    aliases = _jobs_aliases(tree)
    hits = [n for n in ast.walk(tree) if _closes_the_jobs_singleton(n, aliases)]
    assert bool(hits) is banned, source


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
