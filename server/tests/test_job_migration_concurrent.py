"""Concurrent first-connect against a fresh jobs.db must not crash any process,
and the schema migration must apply exactly once.

Background: server/jobs.py `_apply_first_connect_migrations` runs the one-time
schema upgrade + dwg_version backfill the first time a process opens jobs.db.
Several processes opening the SAME brand-new DB at once each read the same
"pre-migration" state (columns missing, ledger marker absent) before any of them
commits, then all attempt the same DDL/INSERT. Before the hardening (PR #88
round-2 finding), a process that lost that race crashed startup one of three ways:
  1. plain ``INSERT INTO schema_migrations`` -> ``IntegrityError: UNIQUE
     constraint failed: schema_migrations.id``;
  2. second ``ALTER TABLE jobs ADD COLUMN`` -> ``OperationalError: duplicate
     column name``;
  3. write-lock contention outlasting busy_timeout -> ``OperationalError:
     database is locked`` with a stranded open transaction.

The existing single-connection test (test_job_dwg_version_persist.py) only proves
the uniqueness no-op on a SECOND sequential connect; it never runs first-connects
at once. Here N real subprocesses first-connect together against an OLD predecessor
schema (missing 13 columns), released from a tight busy-spin filesystem barrier so
their migrations overlap in the sub-millisecond DDL window. Each opens+PRAGMAs its
own connection (mirroring jobs._db()) BEFORE the barrier and then calls the real
jobs._apply_first_connect_migrations, so only the migration races (not connection
setup), which is what makes the concurrent ``duplicate column name`` race fire
reliably. The rows carry backfillable pins, so the run doubles as the concurrent
backfill-correctness proof.

Asserts: no process crashes, every child reads back the backfilled pin, and the
migration applies exactly once (one ledger marker, one dwg_version column).

The 40-row backfill is deliberately small: a realistic fresh/upgraded jobs.db is
small, and an artificially huge backfill would create write-lock contention that
outlasts busy_timeout and (correctly) trips the fix-3 rollback+re-raise path, i.e.
a legitimate clean abort that this "no crash" test should not manufacture.

Run:  cd server && python -m pytest tests/test_job_migration_concurrent.py -q
"""
from __future__ import annotations

# Cache the STDLIB `platform` module BEFORE anything puts a project dir on
# sys.path (the local `platform/` package otherwise shadows it), mirroring
# tests/test_job_dwg_version_persist.py and tests/test_job_lanes.py.
import platform as _stdlib_platform  # noqa: E402
_stdlib_platform.python_implementation()

import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import sqlite3  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parent.parent

# Number of processes that first-connect simultaneously. The task's minimum is
# two; more contenders make the sub-millisecond DDL race reliably observable (with
# a tight barrier, N-1 losers all read the pre-migration state and hit the guarded
# duplicate-column path).
_N_CONTENDERS = 6

_MARKER_ID = "backfill_dwg_version"

# OLD predecessor schema: the core columns plus execution_json (so the backfill has
# a pin source to read), but MISSING the 13 later _MIGRATIONS columns (attempt ...
# dwg_version). First connect must therefore ALTER in 13 columns; the many ADD
# COLUMN attempts per process are what make the concurrent "duplicate column name"
# race fire (a predecessor missing only one column leaves a window too narrow).
_OLD_SCHEMA = (
    "CREATE TABLE jobs ("
    " job_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, tool TEXT NOT NULL,"
    " params_json TEXT NOT NULL, dwg TEXT NOT NULL, status TEXT NOT NULL,"
    " progress TEXT, created_at REAL NOT NULL, started_at REAL,"
    " updated_at REAL NOT NULL, finished_at REAL, elapsed_ms INTEGER,"
    " result_json TEXT, error_json TEXT, execution_json TEXT)"
)

# The child driver: a standalone process that first-connects jobs.db. It does the
# heavy `import jobs` AND opens+PRAGMAs its own connection (mirroring jobs._db())
# BEFORE the barrier, so the only thing left to race after release is the migration
# itself. It then busy-spins until all N children are ready and calls the real
# jobs._apply_first_connect_migrations on its connection. Racing the migration
# function on a pre-opened connection (rather than get_job -> _db) removes
# connection-setup timing noise, which is what makes the concurrent race fire
# reliably. Every failure (crash included) is captured to the out-file so the
# parent sees the real traceback rather than a bare exit code.
_CHILD_SRC = r'''
import json, os, sys, time

db_path, server_dir, out_path, barrier_dir, n_expected, verify_job_id = sys.argv[1:7]
n_expected = int(n_expected)

# cache stdlib platform before a project dir lands on sys.path (local platform/
# package shadow), same defensive order as the parent test module.
import platform as _stdlib_platform
_stdlib_platform.python_implementation()

# force the SQLite ("legacy") job store and route it at the shared DB, both BEFORE
# importing jobs (jobs reads JOBS_DB + LEAF_JOBS_STORE at import / first use).
os.environ["LEAF_JOBS_STORE"] = "legacy"
os.environ["JOBS_DB"] = db_path
if server_dir not in sys.path:
    sys.path.insert(0, server_dir)

result = {"pid": os.getpid(), "ok": False, "error": None, "dwg_version": None}
try:
    import jobs
    import sqlite3

    # Open + PRAGMA the connection exactly as jobs._db() does, BEFORE the barrier,
    # so post-barrier only the migration races. (Kept in sync with jobs._db().)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")

    # tight filesystem barrier: announce ready, then BUSY-SPIN (no sleep) until all
    # N children are ready, so release skew stays below the migration's DDL window
    # and the first-connect migrations genuinely overlap.
    open(os.path.join(barrier_dir, "ready-%d" % os.getpid()), "w").close()
    deadline = time.time() + 30.0
    while time.time() < deadline:
        if len([f for f in os.listdir(barrier_dir) if f.startswith("ready-")]) >= n_expected:
            break

    jobs._apply_first_connect_migrations(conn)   # THE RACE: real code under test

    row = conn.execute(
        "SELECT dwg_version FROM jobs WHERE job_id = ?", (verify_job_id,)).fetchone()
    result["dwg_version"] = row[0] if row else None
    result["ok"] = True
    conn.close()
except BaseException as exc:            # capture the crash the hardening prevents
    import traceback
    result["error"] = "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__))

with open(out_path, "w") as fh:
    json.dump(result, fh)
sys.exit(0 if result["ok"] else 1)
'''


def _run_race(tmp: Path, db_path: Path, verify_job_id: str) -> list[dict]:
    """Spawn _N_CONTENDERS subprocesses that first-connect db_path together, wait
    for all, and return each child's parsed result dict. Fails the test on a hang,
    a hard crash (no result file), or a captured in-child traceback.
    """
    barrier_dir = tmp / "barrier"
    barrier_dir.mkdir()
    child_path = tmp / "concurrent_child.py"
    child_path.write_text(_CHILD_SRC, encoding="utf-8")

    env = dict(os.environ)
    env["LEAF_JOBS_STORE"] = "legacy"
    env["JOBS_DB"] = str(db_path)

    procs, out_paths = [], []
    for i in range(_N_CONTENDERS):
        out_path = tmp / f"result-{i}.json"
        out_paths.append(out_path)
        procs.append(subprocess.Popen(
            [sys.executable, str(child_path), str(db_path), str(SERVER_DIR),
             str(out_path), str(barrier_dir), str(_N_CONTENDERS), verify_job_id],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))

    results = []
    for idx, proc in enumerate(procs):
        try:
            stdout, stderr = proc.communicate(timeout=90)
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            pytest.fail(f"child {idx} hung on concurrent first-connect:\n{stdout}\n{stderr}")
        result = json.loads(out_paths[idx].read_text()) if out_paths[idx].exists() else None
        assert result is not None, (
            f"child {idx} produced no result file (hard crash)\n{stdout}\n{stderr}")
        assert result["error"] is None, (
            f"child {idx} crashed on concurrent first-connect:\n{result['error']}")
        assert result["ok"] is True, f"child {idx} did not complete: {result}"
        assert proc.returncode == 0, f"child {idx} exited {proc.returncode}\n{stdout}\n{stderr}"
        results.append(result)
    return results


def test_concurrent_first_connect_neither_crashes_and_migrates_once():
    """OLD predecessor (missing 13 columns): concurrent first-connect must ride out
    the ``duplicate column name`` race, backfill the pins correctly, and apply the
    migration exactly once."""
    tmp_path = Path(tempfile.mkdtemp(prefix="jobs-concurrent-"))
    try:
        _run_duplicate_column_race(tmp_path)
    finally:
        # subprocesses may leave WAL/-shm handles that Windows releases lazily;
        # ignore_errors avoids a spurious cleanup failure without leaking whole DBs.
        shutil.rmtree(tmp_path, ignore_errors=True)


def _run_duplicate_column_race(tmp_path: Path) -> None:
    db_path = tmp_path / "jobs.db"

    pinned: dict[str, int] = {}
    unpinned: list[str] = []
    conn = sqlite3.connect(str(db_path))
    conn.execute(_OLD_SCHEMA)
    now = time.time()
    cols = ("job_id, tenant_id, tool, params_json, dwg, status, progress,"
            " created_at, updated_at, execution_json")

    def _insert(job_id: str, execution_json: str) -> None:
        conn.execute(
            f"INSERT INTO jobs ({cols}) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (job_id, "demo-tenant", "legacy-tool", "{}", "demo.dwg",
             "succeeded", "done", now, now, execution_json))

    for i in range(40):
        jid = str(uuid.uuid4())
        pinned[jid] = 100 + i
        _insert(jid, json.dumps({"aps_live": False, "dwg_version": 100 + i}))
    for _ in range(5):
        jid = str(uuid.uuid4())
        unpinned.append(jid)
        _insert(jid, json.dumps({"aps_live": False}))
    conn.commit()
    conn.close()

    verify_job_id = next(iter(pinned))
    results = _run_race(tmp_path, db_path, verify_job_id)

    # 1) no crash, and every child read back the backfilled pin.
    for result in results:
        assert result["dwg_version"] == pinned[verify_job_id], (
            f"expected backfilled pin {pinned[verify_job_id]}, got {result['dwg_version']}")

    # 2) migration applied EXACTLY once, inspected on a fresh connection.
    audit = sqlite3.connect(str(db_path))
    try:
        marker_rows = audit.execute(
            "SELECT COUNT(*) FROM schema_migrations WHERE id = ?", (_MARKER_ID,)).fetchone()[0]
        assert marker_rows == 1, f"ledger marker present {marker_rows}x, expected exactly 1"
        col_count = sum(1 for row in audit.execute("PRAGMA table_info(jobs)")
                        if row[1] == "dwg_version")
        assert col_count == 1, f"dwg_version column present {col_count}x, expected exactly 1"

        # backfill touched exactly the pinned rows, nothing else.
        for jid, pin in pinned.items():
            got = audit.execute("SELECT dwg_version FROM jobs WHERE job_id = ?",
                                (jid,)).fetchone()[0]
            assert got == pin, f"pinned row {jid}: dwg_version={got}, expected {pin}"
        for jid in unpinned:
            got = audit.execute("SELECT dwg_version FROM jobs WHERE job_id = ?",
                                (jid,)).fetchone()[0]
            assert got is None, f"unpinned row {jid}: dwg_version={got}, expected None"
    finally:
        audit.close()
