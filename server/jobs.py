"""
Async job spine (CONTRACT-ADDENDUM section 7).

Durable, tab-survivable background jobs: a POST /api/run submits a row into
SQLite (server/jobs.db) and returns immediately; a bounded ThreadPoolExecutor
executes the tool THROUGH THE BROKER (broker_client — this module never imports
da.* / never sees the APS credential), heartbeating updated_at while it waits.

Timeout: JOB_MAX_S (default 540, env-overridable) — an over-limit job is marked
failed/TIMEOUT; the underlying broker call is abandoned best-effort (its HTTP
timeout reaps the worker thread shortly after).

Orphan reaper: a daemon thread marks submitted/running rows whose heartbeat is
staler than HEARTBEAT_STALE_S as failed/INTERNAL ("orphaned: heartbeat stale").
Other sessions extend this hook for APS WorkItem reaping.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

import broker_client
from envelopes import ErrorCode, err_envelope, error_obj

SERVER_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("JOBS_DB", str(SERVER_DIR / "jobs.db")))


def job_max_s() -> float:
    return float(os.environ.get("JOB_MAX_S", "540"))


def heartbeat_stale_s() -> float:
    return float(os.environ.get("HEARTBEAT_STALE_S", "60"))


REAPER_INTERVAL_S = float(os.environ.get("REAPER_INTERVAL_S", "10"))
MAX_WORKERS = int(os.environ.get("JOB_WORKERS", "4"))

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_executor: Optional[ThreadPoolExecutor] = None
_reaper_started = False

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  job_id      TEXT PRIMARY KEY,
  tenant_id   TEXT NOT NULL,
  tool        TEXT NOT NULL,
  params_json TEXT NOT NULL,
  dwg         TEXT NOT NULL,
  status      TEXT NOT NULL,
  progress    TEXT,
  created_at  REAL NOT NULL,
  started_at  REAL,
  updated_at  REAL NOT NULL,
  finished_at REAL,
  elapsed_ms  INTEGER,
  result_json TEXT,
  error_json  TEXT
)
"""


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        _conn.execute("PRAGMA busy_timeout = 5000")
        _conn.execute("PRAGMA journal_mode = WAL")
        _conn.execute(_SCHEMA)
        _conn.commit()
    return _conn


def _exec(sql: str, args: tuple = ()) -> None:
    with _lock:
        _db().execute(sql, args)
        _db().commit()


def _query(sql: str, args: tuple = ()) -> List[sqlite3.Row]:
    with _lock:
        conn = _db()
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql, args)
        return cur.fetchall()


def _row_to_record(row: sqlite3.Row) -> Dict[str, Any]:
    rec: Dict[str, Any] = {
        "job_id": row["job_id"],
        "tenant_id": row["tenant_id"],
        "tool": row["tool"],
        "params": json.loads(row["params_json"]),
        "dwg": row["dwg"],
        "status": row["status"],
        "progress": row["progress"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "updated_at": row["updated_at"],
        "finished_at": row["finished_at"],
        "elapsed_ms": row["elapsed_ms"],
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "error": json.loads(row["error_json"]) if row["error_json"] else None,
    }
    return rec


# --------------------------------------------------------------------------- #
# public API (used by routers/jobs.py)
# --------------------------------------------------------------------------- #
def submit_job(tenant_id: str, tool: Dict[str, Any], params: Dict[str, Any], dwg: str,
               aps_live: bool) -> str:
    """Insert the durable job row and hand it to the executor. Returns job_id."""
    ensure_started()
    job_id = str(uuid.uuid4())
    now = time.time()
    _exec(
        "INSERT INTO jobs (job_id, tenant_id, tool, params_json, dwg, status, progress,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (job_id, tenant_id, tool["name"], json.dumps(params), dwg, "submitted",
         "queued", now, now),
    )
    assert _executor is not None
    _executor.submit(_run_job, job_id, tenant_id, tool, params, dwg, aps_live)
    return job_id


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    rows = _query("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
    return _row_to_record(rows[0]) if rows else None


def list_jobs(tenant_id: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 200))
    if tenant_id:
        rows = _query(
            "SELECT * FROM jobs WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ?",
            (tenant_id, limit),
        )
    else:
        rows = _query("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
    return [_row_to_record(r) for r in rows]


TERMINAL = ("complete", "failed")


def wait_for_terminal(job_id: str, timeout_s: float, poll_s: float = 0.15) -> Optional[Dict[str, Any]]:
    """Poll until the job is terminal (or timeout). Returns the final record."""
    deadline = time.time() + timeout_s
    rec = get_job(job_id)
    while rec is not None and rec["status"] not in TERMINAL and time.time() < deadline:
        time.sleep(poll_s)
        rec = get_job(job_id)
    return rec


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #
def _heartbeat(job_id: str, progress: Optional[str] = None) -> None:
    if progress is None:
        _exec("UPDATE jobs SET updated_at = ? WHERE job_id = ?", (time.time(), job_id))
    else:
        _exec("UPDATE jobs SET updated_at = ?, progress = ? WHERE job_id = ?",
              (time.time(), progress, job_id))


def _finish(job_id: str, status: str, started: float,
            result_env: Optional[Dict[str, Any]] = None,
            error: Optional[Dict[str, Any]] = None) -> None:
    now = time.time()
    _exec(
        "UPDATE jobs SET status = ?, progress = ?, updated_at = ?, finished_at = ?,"
        " elapsed_ms = ?, result_json = ?, error_json = ? WHERE job_id = ?",
        (status, "done" if status == "complete" else "error", now, now,
         int((now - started) * 1000),
         json.dumps(result_env) if result_env is not None else None,
         json.dumps(error) if error is not None else None,
         job_id),
    )


def _run_job(job_id: str, tenant_id: str, tool: Dict[str, Any], params: Dict[str, Any],
             dwg: str, aps_live: bool) -> None:
    started = time.time()
    max_s = job_max_s()
    _exec("UPDATE jobs SET status = 'running', progress = 'running', started_at = ?,"
          " updated_at = ? WHERE job_id = ?", (started, started, job_id))

    holder: Dict[str, Any] = {}

    def _call() -> None:
        try:
            holder["env"] = broker_client.run_via_broker(
                tenant_id, tool, params, dwg, aps_live, timeout_s=max_s + 30
            )
        except Exception as exc:  # noqa: BLE001
            holder["exc"] = exc

    inner = threading.Thread(target=_call, daemon=True, name=f"job-{job_id[:8]}")
    inner.start()
    deadline = started + max_s
    while inner.is_alive() and time.time() < deadline:
        inner.join(timeout=1.0)
        if inner.is_alive():
            _heartbeat(job_id)  # long broker poll still heartbeats

    if inner.is_alive():
        # TIMEOUT — abandon the broker call (best-effort cancel: the broker
        # client's own HTTP timeout reaps the daemon thread soon after).
        _finish(job_id, "failed", started,
                error=error_obj(ErrorCode.TIMEOUT,
                                f"job exceeded JOB_MAX_S={max_s:g}s", retryable=True))
        return

    if "exc" in holder:
        exc = holder["exc"]
        if isinstance(exc, broker_client.BrokerUnreachable):
            err = error_obj(ErrorCode.BROKER_UNREACHABLE, str(exc), retryable=True)
        else:
            err = error_obj(ErrorCode.INTERNAL, f"{type(exc).__name__}: {exc}", retryable=False)
        _finish(job_id, "failed", started, error=err)
        return

    env = holder.get("env") or {}
    if env.get("ok"):
        _finish(job_id, "complete", started, result_env=env)
    else:
        err = env.get("error") or error_obj(ErrorCode.INTERNAL, "broker returned no error detail",
                                            retryable=False)
        _finish(job_id, "failed", started, error=err)


def failed_envelope_from(record: Dict[str, Any]) -> Dict[str, Any]:
    """Rebuild a section-3 err envelope from a failed job record (for ?wait=1)."""
    err = record.get("error") or error_obj(ErrorCode.INTERNAL, "unknown failure", False)
    return err_envelope(err["error_code"], err["message"], err["retryable"],
                        tool=record.get("tool"),
                        timing_ms=record.get("elapsed_ms") or 0)


# --------------------------------------------------------------------------- #
# orphan reaper
# --------------------------------------------------------------------------- #
def _reap_orphans_once() -> int:
    stale_before = time.time() - heartbeat_stale_s()
    rows = _query(
        "SELECT job_id, started_at FROM jobs WHERE status IN ('submitted','running')"
        " AND updated_at < ?", (stale_before,))
    for r in rows:
        _finish(r["job_id"], "failed", r["started_at"] or stale_before,
                error=error_obj(ErrorCode.INTERNAL, "orphaned: heartbeat stale", retryable=True))
    return len(rows)


def _reaper_loop() -> None:
    while True:
        time.sleep(REAPER_INTERVAL_S)
        try:
            _reap_orphans_once()
        except Exception:  # noqa: BLE001  pragma: no cover
            pass


def ensure_started() -> None:
    """Idempotent: open the DB, start the executor + reaper daemon."""
    global _executor, _reaper_started
    _db()
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="jobworker")
    if not _reaper_started:
        threading.Thread(target=_reaper_loop, daemon=True, name="job-reaper").start()
        _reaper_started = True
