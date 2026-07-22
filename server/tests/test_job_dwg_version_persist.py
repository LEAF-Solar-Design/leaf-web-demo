"""dwg_version is persisted on the job row (closes the follow-up recorded at the
version-pinning merge: server/jobs.py previously threaded ``dwg_version`` to the
broker only, with no column in ``_SCHEMA``).

Proves, hermetically (no broker subprocess, no network):
  1. a legacy ``jobs.db`` created WITHOUT the column upgrades in place via the
     ``_MIGRATIONS`` path and its rows read back ``dwg_version: None``;
  2. a submit with ``dwg_version=N`` reads back N through the public getter;
  3. a submit without a pin reads back None.

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

# Build a LEGACY-SCHEMA db (pre-marathon, no dwg_version column) at the path
# BEFORE jobs.py ever connects, so the first jobs._db() call must upgrade it.
_LEGACY_JOB_ID = str(uuid.uuid4())
_legacy = sqlite3.connect(str(_DB_PATH))
_legacy.execute(
    "CREATE TABLE jobs ("
    " job_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, tool TEXT NOT NULL,"
    " params_json TEXT NOT NULL, dwg TEXT NOT NULL, status TEXT NOT NULL,"
    " progress TEXT, created_at REAL NOT NULL, started_at REAL,"
    " updated_at REAL NOT NULL, finished_at REAL, elapsed_ms INTEGER,"
    " result_json TEXT, error_json TEXT)"
)
_now = time.time()
_legacy.execute(
    "INSERT INTO jobs (job_id, tenant_id, tool, params_json, dwg, status, progress,"
    " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
    (_LEGACY_JOB_ID, "demo-tenant", "legacy-tool", json.dumps({}), "demo.dwg",
     "succeeded", "done", _now, _now),
)
_legacy.commit()
_legacy.close()

import jobs  # noqa: E402

READ_TOOL = {"name": "fake-read", "engine_op": "count_by_layer",
             "capabilities": ["drawing.read"], "params": {}}


def _submit(monkeypatch, **kwargs) -> str:
    # persistence-only test: the executor body is stubbed so no broker/engine runs
    monkeypatch.setattr(jobs, "_run_job", lambda *a, **k: None)
    return jobs.submit_job("demo-tenant", READ_TOOL, {}, "demo.dwg", False, **kwargs)


def test_legacy_db_upgrades_and_reads_none():
    rec = jobs.get_job(_LEGACY_JOB_ID)
    assert rec is not None
    assert rec["dwg_version"] is None
    cols = {row[1] for row in jobs._db().execute("PRAGMA table_info(jobs)")}
    assert "dwg_version" in cols  # added by the _MIGRATIONS upgrade path


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
