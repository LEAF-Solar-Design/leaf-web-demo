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
import platform_link
from envelopes import ErrorCode, err_envelope, error_obj

SERVER_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("JOBS_DB", str(SERVER_DIR / "jobs.db")))

# Bound the run `params` blob so an unbounded body can't bloat the durable jobs row /
# broker payload (security-audit F15). 64 KiB is far above any real tool's params; an
# over-cap submission is rejected with an HTTP 400 before the job row is written.
MAX_PARAMS_BYTES = int(os.environ.get("JOB_MAX_PARAMS_BYTES", str(64 * 1024)))


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
# input bounding (security-audit F15)
# --------------------------------------------------------------------------- #
def _reject_oversized_params(params: Dict[str, Any]) -> None:
    """Reject a run whose ``params`` serialise to more than ``MAX_PARAMS_BYTES``.

    Raised as an HTTP 400 (mirrors the codebase's HTTPException idiom in auth.py /
    deps.require_tenant → the shared handler renders a structured error envelope). The
    check runs on the MERGED params (authored defaults + caller params) so neither
    source can smuggle an unbounded blob past the durable job row / broker payload.
    """
    from fastapi import HTTPException, status

    try:
        size = len(json.dumps(params, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        # non-serialisable params are a client error, not a 500 — reject cleanly.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST,
                            detail="run params are not JSON-serialisable")
    if size > MAX_PARAMS_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"run params too large: {size} bytes exceeds the {MAX_PARAMS_BYTES}-byte cap",
        )


# --------------------------------------------------------------------------- #
# public API (used by routers/jobs.py)
# --------------------------------------------------------------------------- #
def submit_job(tenant_id: str, tool: Dict[str, Any], params: Dict[str, Any], dwg: str,
               aps_live: bool, org_id: Optional[str] = None,
               project_id: Optional[str] = None,
               dwg_version: Optional[int] = None) -> str:
    """Insert the durable job row and hand it to the executor. Returns job_id.

    ``org_id`` / ``project_id`` carry the OPTIONAL project context from the
    ``X-Org-Id`` / ``X-Project-Id`` headers on ``POST /api/run``. When both are
    present AND a platform DB resolves, a canonical platform Job row is recorded
    (best-effort, env-gated; see platform_link). With no project context this is
    a no-op and the spine is byte-identical to before.

    ``dwg_version`` (None -> head, unchanged behaviour) pins the run to a specific
    immutable drawing version (da/store.py resolve_version); threaded through to
    the broker call ONLY (not persisted in the durable row — this is an execution
    parameter, not a stored job field).

    FOLLOW-UP (deliberate, recorded 2026-07-22): ``dwg_version`` provenance is NOT
    persisted on the job row (no ``dwg_version`` column in ``_SCHEMA``) — so
    ``GET /api/jobs/{id}`` cannot yet show which version a past run was pinned to.
    Adding that column requires a SQLite migration (``ALTER TABLE jobs ADD COLUMN``)
    for any already-deployed ``jobs.db``, which is out of scope for this minimal
    threading change; tracked as a follow-up, not silently dropped.

    Rejects an over-cap ``params`` blob (> ``MAX_PARAMS_BYTES``) with HTTP 400 before
    any durable row / broker payload is written (security-audit F15).
    """
    _reject_oversized_params(params)
    ensure_started()
    job_id = str(uuid.uuid4())
    now = time.time()
    _exec(
        "INSERT INTO jobs (job_id, tenant_id, tool, params_json, dwg, status, progress,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (job_id, tenant_id, tool["name"], json.dumps(params), dwg, "submitted",
         "queued", now, now),
    )
    # best-effort platform Job linkage (never raises, never affects the run)
    platform_link.on_submit(job_id, org_id, project_id, tool.get("name"), params)
    assert _executor is not None
    _executor.submit(_run_job, job_id, tenant_id, tool, params, dwg, aps_live, dwg_version)
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

# progress sentinel: an in-flight job whose owning tab/session was closed. The
# orphan reaper fails such jobs on its next sweep (tab-close -> reap seam).
CLOSED_PROGRESS = "closed"


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
def _progress_phase(tool: Dict[str, Any], aps_live: bool) -> str:
    """The progress phase a run ENTERS before the (blocking) broker call (Contract 5c,
    §15). Read tools -> 'executing'; write tools -> 'storing version' (mock APS_LIVE=0)
    / 'extracting' (live re-extract APS_LIVE=1). Short + stable strings; documented in
    §15 as the vocabulary SSE/poll consumers can render."""
    caps = (tool or {}).get("capabilities") or []
    if "drawing.write" in caps:
        return "extracting" if aps_live else "storing version"
    return "executing"


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
    # best-effort platform Job terminal sync (all terminal paths funnel here:
    # success, timeout, exception, and the orphan reaper). No-op if unlinked.
    platform_link.on_terminal(job_id, status, result_env, error)


def _run_job(job_id: str, tenant_id: str, tool: Dict[str, Any], params: Dict[str, Any],
             dwg: str, aps_live: bool, dwg_version: Optional[int] = None) -> None:
    started = time.time()
    max_s = job_max_s()
    _exec("UPDATE jobs SET status = 'running', progress = 'running', started_at = ?,"
          " updated_at = ? WHERE job_id = ?", (started, started, job_id))
    platform_link.on_running(job_id)  # best-effort; no-op if unlinked

    # Richer progress (Contract 5c, §15): mark the real phase this run is ENTERING
    # before the (blocking) broker call, so SSE/poll consumers see more than status flips.
    _heartbeat(job_id, _progress_phase(tool, aps_live))

    holder: Dict[str, Any] = {}

    def _call() -> None:
        try:
            holder["env"] = broker_client.run_via_broker(
                tenant_id, tool, params, dwg, aps_live, timeout_s=max_s + 30,
                dwg_version=dwg_version,
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
    """Fail in-flight jobs that are orphaned: heartbeat stale OR flagged closed
    (tab-close). Marking a closed job's row failed is the durable-spine half of
    orphan reaping; cancelling its APS WorkItem is the broker's half
    (POST /broker/reap, da/reaper.py)."""
    stale_before = time.time() - heartbeat_stale_s()
    rows = _query(
        "SELECT job_id, started_at, progress FROM jobs WHERE status IN ('submitted','running')"
        " AND (updated_at < ? OR progress = ?)", (stale_before, CLOSED_PROGRESS))
    for r in rows:
        reason = ("orphaned: session closed (tab-close)" if r["progress"] == CLOSED_PROGRESS
                  else "orphaned: heartbeat stale")
        _finish(r["job_id"], "failed", r["started_at"] or stale_before,
                error=error_obj(ErrorCode.INTERNAL, reason, retryable=True))
    return len(rows)


# --------------------------------------------------------------------------- #
# session-close seam (tab-close / session-end -> orphan reaping signal)
# --------------------------------------------------------------------------- #
def mark_job_closed(job_id: str) -> bool:
    """Flag ONE in-flight job's owner as gone (progress -> 'closed'). The orphan
    reaper fails it on its next sweep. No-op on terminal/unknown jobs. Returns
    True iff a live job was flagged."""
    rows = _query("SELECT status FROM jobs WHERE job_id = ?", (job_id,))
    if not rows or rows[0]["status"] in TERMINAL:
        return False
    _exec("UPDATE jobs SET progress = ?, updated_at = ? WHERE job_id = ?"
          " AND status IN ('submitted','running')",
          (CLOSED_PROGRESS, time.time(), job_id))
    return True


def close_tenant_jobs(tenant_id: str) -> int:
    """Flag ALL of a tenant's in-flight jobs closed (session-end). Returns count."""
    rows = _query("SELECT job_id FROM jobs WHERE tenant_id = ?"
                  " AND status IN ('submitted','running')", (tenant_id,))
    now = time.time()
    for r in rows:
        _exec("UPDATE jobs SET progress = ?, updated_at = ? WHERE job_id = ?",
              (CLOSED_PROGRESS, now, r["job_id"]))
    return len(rows)


def orphan_lease_records(tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Lease records for in-flight jobs that are closed OR heartbeat-stale — the
    payload the app POSTs to the broker's /broker/reap so their APS WorkItems get
    cancelled. workitem_id is broker-side (job_id is carried for correlation)."""
    stale_before = time.time() - heartbeat_stale_s()
    sql = ("SELECT job_id, tenant_id, progress FROM jobs WHERE status IN ('submitted','running')"
           " AND (updated_at < ? OR progress = ?)")
    args: list = [stale_before, CLOSED_PROGRESS]
    if tenant_id:
        sql += " AND tenant_id = ?"
        args.append(tenant_id)
    out: List[Dict[str, Any]] = []
    for r in _query(sql, tuple(args)):
        out.append({"job_id": r["job_id"], "tenant_id": r["tenant_id"],
                    "status": "inprogress", "workitem_id": None,
                    "session_closed": r["progress"] == CLOSED_PROGRESS})
    return out


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
