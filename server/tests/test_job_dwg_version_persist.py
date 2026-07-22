"""dwg_version is persisted on the job row (closes the follow-up recorded at the
version-pinning merge: server/jobs.py previously threaded ``dwg_version`` to the
broker only, with no column in ``_SCHEMA``).

Proves, hermetically (no broker subprocess, no network):
  1. a ``jobs.db`` created by the DEPLOYED predecessor schema (everything except
     the ``dwg_version`` column) upgrades in place via the ``_MIGRATIONS`` path,
     and a historical row whose pin lives only in ``execution_json`` is
     BACKFILLED into the new column (review finding: without backfill, past
     pinned runs would misreport ``dwg_version: None``);
  2. predecessor rows with a null / absent execution pin read back ``None``;
  3. a submit with ``dwg_version=N`` reads back N through the public getter;
  4. a submit without a pin reads back None.

Run:  cd server && python -m pytest tests/test_job_dwg_version_persist.py -q
"""
from __future__ import annotations

# Cache the STDLIB `platform` module BEFORE anything puts PROJECT_ROOT on sys.path
# (the local `platform/` package otherwise shadows it — mirrors tests/test_job_lanes.py).
import platform as _stdlib_platform  # noqa: E402
_stdlib_platform.python_implementation()

import json  # noqa: E402
import os  # noqa: E402
import sqlite3  # noqa: E402
import sys  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402
from pathlib import Path  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parent.parent

# route the jobs SQLite DB to a throwaway dir BEFORE `jobs` is imported anywhere
# (jobs.py reads JOBS_DB at import time — mirrors tests/test_job_lanes.py).
_DB_PATH = Path(tempfile.mkdtemp(prefix="dwgver-jobs-")) / "jobs.db"
os.environ.setdefault("JOBS_DB", str(_DB_PATH))

if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

# Build a PREDECESSOR-SCHEMA db (the deployed release: full current schema MINUS
# the dwg_version column) at the path BEFORE jobs.py ever connects, so the first
# jobs._db() call must upgrade + backfill it.
_PREDECESSOR_SCHEMA = (
    "CREATE TABLE jobs ("
    " job_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, tool TEXT NOT NULL,"
    " params_json TEXT NOT NULL, dwg TEXT NOT NULL, status TEXT NOT NULL,"
    " progress TEXT, created_at REAL NOT NULL, started_at REAL,"
    " updated_at REAL NOT NULL, finished_at REAL, elapsed_ms INTEGER,"
    " result_json TEXT, error_json TEXT, execution_json TEXT,"
    " attempt INTEGER NOT NULL DEFAULT 0, lease_owner TEXT,"
    " lease_expires_at REAL, heartbeat_at REAL, provenance_json TEXT,"
    " terminal_fingerprint TEXT, terminal_conflict_json TEXT, org_id TEXT,"
    " project_id TEXT, authority_mode TEXT NOT NULL DEFAULT 'legacy_sqlite',"
    " idempotency_key TEXT, submission_fingerprint TEXT)"
)

_PINNED_JOB_ID = str(uuid.uuid4())      # execution_json carries dwg_version: 7
_NULLPIN_JOB_ID = str(uuid.uuid4())     # execution_json carries dwg_version: null
_NOEXEC_JOB_ID = str(uuid.uuid4())      # execution_json is NULL (ancient row)


def _insert_predecessor_row(conn: sqlite3.Connection, job_id: str,
                            execution_json: str | None) -> None:
    now = time.time()
    conn.execute(
        "INSERT INTO jobs (job_id, tenant_id, tool, params_json, dwg, status,"
        " progress, created_at, updated_at, execution_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (job_id, "demo-tenant", "legacy-tool", json.dumps({}), "demo.dwg",
         "succeeded", "done", now, now, execution_json),
    )


_legacy = sqlite3.connect(str(_DB_PATH))
_legacy.execute(_PREDECESSOR_SCHEMA)
_insert_predecessor_row(_legacy, _PINNED_JOB_ID, json.dumps(
    {"tool": {"name": "legacy-tool"}, "aps_live": False, "dwg_version": 7}))
_insert_predecessor_row(_legacy, _NULLPIN_JOB_ID, json.dumps(
    {"tool": {"name": "legacy-tool"}, "aps_live": False, "dwg_version": None}))
_insert_predecessor_row(_legacy, _NOEXEC_JOB_ID, None)
_legacy.commit()
_legacy.close()

import jobs  # noqa: E402

READ_TOOL = {"name": "fake-read", "engine_op": "count_by_layer",
             "capabilities": ["drawing.read"], "params": {}}


def _submit(monkeypatch, **kwargs) -> str:
    # persistence-only test: the executor body is stubbed so no broker/engine runs
    monkeypatch.setattr(jobs, "_run_job", lambda *a, **k: None)
    return jobs.submit_job("demo-tenant", READ_TOOL, {}, "demo.dwg", False, **kwargs)


def test_migration_backfills_pin_from_execution_json():
    rec = jobs.get_job(_PINNED_JOB_ID)
    assert rec is not None
    assert rec["dwg_version"] == 7  # historical provenance survives the upgrade
    raw = jobs._db().execute(
        "SELECT dwg_version FROM jobs WHERE job_id = ?", (_PINNED_JOB_ID,)).fetchone()
    assert raw[0] == 7  # the COLUMN carries it, not just execution_json


def test_migration_adds_column_and_unpinned_rows_read_none():
    cols = {row[1] for row in jobs._db().execute("PRAGMA table_info(jobs)")}
    assert "dwg_version" in cols  # added by the _MIGRATIONS upgrade path
    for job_id in (_NULLPIN_JOB_ID, _NOEXEC_JOB_ID):
        rec = jobs.get_job(job_id)
        assert rec is not None
        assert rec["dwg_version"] is None


def test_submit_with_pin_persists_version(monkeypatch):
    job_id = _submit(monkeypatch, dwg_version=7)
    rec = jobs.get_job(job_id)
    assert rec is not None
    assert rec["dwg_version"] == 7
    # the column itself carries the value (not just execution_json provenance)
    raw = jobs._db().execute(
        "SELECT dwg_version FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    assert raw[0] == 7


def test_submit_without_pin_reads_none(monkeypatch):
    job_id = _submit(monkeypatch)
    rec = jobs.get_job(job_id)
    assert rec is not None
    assert rec["dwg_version"] is None
