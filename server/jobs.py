"""
Async job spine (CONTRACT-ADDENDUM section 7).

Durable, tab-survivable background jobs: a POST /api/run submits a row into
SQLite (server/jobs.db) and returns immediately; two bounded ThreadPoolExecutor
lanes (§10: "fast" for mock read runs, "slow" for drawing.write or live-APS runs,
so a 10ms read never queues behind a 540s write) execute the tool THROUGH THE
BROKER (broker_client — this module never imports da.* / never sees the APS
credential), heartbeating updated_at while it waits.

Timeout: JOB_MAX_S (default 540, env-overridable) — an over-limit job is marked
failed/TIMEOUT; the underlying broker call is abandoned best-effort (its HTTP
timeout reaps the worker thread shortly after).

Orphan reaper: a daemon thread marks submitted/running rows whose heartbeat is
staler than HEARTBEAT_STALE_S as failed/INTERNAL ("orphaned: heartbeat stale").
Other sessions extend this hook for APS WorkItem reaping.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sqlite3
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

import broker_client
import platform_link
from envelopes import ErrorCode, err_envelope, error_obj
from job_pg_store import PostgresJobStore

try:  # APS domain metrics (CloudWatch EMF); best-effort, optional
    import emf_metrics
except Exception:  # pragma: no cover
    emf_metrics = None  # type: ignore[assignment]

try:  # P2 product events (BigQuery sink); best-effort, optional
    import telemetry_sink
except Exception:  # pragma: no cover
    telemetry_sink = None  # type: ignore[assignment]


def _emit_job_terminal(status: str) -> None:
    """Best-effort JobTerminal EMF emit. NEVER raises: called on the terminal
    path and must not corrupt job completion."""
    if emf_metrics is None:
        return
    try:
        emf_metrics.emit_job_terminal(status)
    except Exception:  # noqa: BLE001 - metrics must never break job completion
        pass


def _emit_job_terminal_event(job_id: str, status: str,
                             error: Optional[Dict[str, Any]],
                             provenance: Optional[Dict[str, Any]],
                             tenant_id: Optional[str] = None,
                             tool: Optional[str] = None,
                             elapsed_ms: Optional[int] = None) -> None:
    """Best-effort `job.terminal` PRODUCT event (P2 telemetry): the identity-
    carrying twin of the EMF JobTerminal count. NEVER raises; a lookup miss
    emits nothing rather than a row with fabricated identity.

    The sqlite call site captures tenant/tool INSIDE its locked region and
    passes VALUES (no connection crosses the lock boundary). The pg call
    site passes nothing and this helper does one indexed read — gated on an
    ENABLED sink so a dark deployment pays zero extra round trips."""
    if telemetry_sink is None:
        return
    try:
        if telemetry_sink.disabled_reason() is not None:
            return
        if tenant_id is None and job_store_mode() == "postgres":
            ctx = _pg_store.event_context(job_id)
            if ctx:
                tenant_id, tool, elapsed_ms = ctx["tenant_id"], ctx["tool"], ctx["elapsed_ms"]
        if not tenant_id:
            return
        labels: Dict[str, Any] = {"job_id": job_id, "status": status}
        if tool:
            labels["tool"] = tool
        if elapsed_ms is not None:
            labels["duration_ms"] = elapsed_ms
        if isinstance(error, dict) and error.get("code"):
            labels["error_code"] = error["code"]
        if isinstance(provenance, dict):
            if provenance.get("attempt") is not None:
                labels["attempts"] = provenance["attempt"]
            if provenance.get("execution_path"):
                labels["execution_path"] = provenance["execution_path"]
        telemetry_sink.emit(
            "job.terminal",
            tenant_id=str(tenant_id),
            tenant_kind="guest" if str(tenant_id).startswith("guest-") else "account",
            session_id="server",
            labels=labels,
        )
    except Exception:  # noqa: BLE001 - telemetry must never break job completion
        pass

logger = logging.getLogger(__name__)

SERVER_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("JOBS_DB", str(SERVER_DIR / "jobs.db")))

# Bound the run `params` blob so an unbounded body can't bloat the durable jobs row /
# broker payload (security-audit F15). 64 KiB is far above any real tool's params; an
# over-cap submission is rejected with an HTTP 400 before the job row is written.
MAX_PARAMS_BYTES = int(os.environ.get("JOB_MAX_PARAMS_BYTES", str(64 * 1024)))

PLAN_TOOL_NAME = "cad-edit-plan"
PLAN_TOOL = {"name": PLAN_TOOL_NAME, "version": "1.0.0",
             "capabilities": ["drawing.write"], "kind": "plan"}


def job_max_s() -> float:
    return float(os.environ.get("JOB_MAX_S", "540"))


def heartbeat_stale_s() -> float:
    return float(os.environ.get("HEARTBEAT_STALE_S", "60"))


REAPER_INTERVAL_S = float(os.environ.get("REAPER_INTERVAL_S", "10"))
MAX_WORKERS = int(os.environ.get("JOB_WORKERS", "4"))


REAPER_LOG_THROTTLE_DEFAULT_S = 300.0

# Hard ceilings, independent of how many distinct fault classes a streak throws.
# The budget is what actually bounds CloudWatch volume: at most
# _MAX_VERBOSE_PER_WINDOW full tracebacks plus one terse reminder per quiet
# window, whatever the class mix. _MAX_TRACKED_FAULT_CLASSES bounds the memory
# the bookkeeping itself can hold during a long outage.
_MAX_VERBOSE_PER_WINDOW = 3
_MAX_TRACKED_FAULT_CLASSES = 16

# Set when REAPER_LOG_THROTTLE_S could not be parsed. Read and cleared by
# _note_reaper_failure so the warning is emitted OUTSIDE _reaper_log_lock;
# logging under the lock is what this module is careful not to do.
_reaper_throttle_bad_raw: Optional[str] = None
_reaper_throttle_warned = False


def reaper_log_throttle_s() -> float:
    """Quiet window between reminders about a STILL-failing reaper sweep.

    A permanently failing sweep logged a full traceback every REAPER_INTERVAL_S
    (default 10s): 360 tracebacks/hour/process shipped to CloudWatch by the ECS
    awslogs driver, all describing one repeating fault. The throttle bounds the
    VOLUME, never the SIGNAL -- see _note_reaper_failure. 0 disables the quiet
    window (every failure logs).

    A value that is unparseable, negative, NaN, or infinite falls back to the
    default rather than raising, and flags itself so the caller can say so once.
    Each of those silently breaks the bound in a different direction: a raise is
    absorbed by the caller's swallow into "no reminder logged"; NaN makes every
    comparison False, which restores full-volume logging; infinity suppresses
    every reminder forever. All three turn one typo into a silent observability
    failure, which is the exact class this change exists to prevent.
    """
    global _reaper_throttle_bad_raw
    raw = os.environ.get("REAPER_LOG_THROTTLE_S")
    if raw is None or raw.strip() == "":
        return REAPER_LOG_THROTTLE_DEFAULT_S
    try:
        value = float(raw)
    except (TypeError, ValueError):
        _reaper_throttle_bad_raw = raw
        return REAPER_LOG_THROTTLE_DEFAULT_S
    if not math.isfinite(value) or value < 0.0:
        _reaper_throttle_bad_raw = raw
        return REAPER_LOG_THROTTLE_DEFAULT_S
    return value


def max_attempts() -> int:
    """Bound automatic delivery attempts; a bad solver must not loop forever."""
    return max(1, int(os.environ.get("JOB_MAX_ATTEMPTS", "3")))


def lease_duration_s() -> float:
    """A worker owns a job only while it continues to heartbeat this lease."""
    return max(1.0, float(os.environ.get("JOB_LEASE_S", "60")))

# Worker lanes (CONTRACT-ADDENDUM §10 / wire contract 10): "fast" runs mock reads
# (tools without drawing.write, APS_LIVE=0) so they never queue behind long writes;
# "slow" runs drawing.write or live-APS jobs and must stay ≤ the APS concurrency
# grant. With JOB_WORKERS_FAST/SLOW unset, the slow lane is exactly today's
# JOB_WORKERS pool — write behavior is unchanged.
LANE_FAST = "fast"
LANE_SLOW = "slow"

_lock = threading.Lock()
# Serializes the first-connect build ONLY (see _db). Deliberately separate from
# _lock: _db() is called both under _lock (_exec/_query/claim/...) and unlocked
# (ensure_started -> line ~1120), so a single reentrant lock would deadlock. The
# ordering is always _lock -> _conn_lock or _conn_lock alone, never the reverse,
# so no cycle exists.
_conn_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_executors: Dict[str, ThreadPoolExecutor] = {}
_reaper_started = False
# Serializes ensure_started()'s check-and-set of the two process singletons above.
# Deliberately separate from _lock, which ensure_started() takes indirectly via
# _query() for the submitted-row scan: reusing _lock would deadlock. That scan
# stays OUTSIDE this lock, so _start_lock is never held while _lock or _conn_lock
# is taken, and nothing holding those calls ensure_started() -- no cycle exists.
_start_lock = threading.Lock()
_pg_store = PostgresJobStore()


def job_store_mode() -> str:
    """Return the async-job authority, rejecting typos and implicit fallback."""
    mode = os.environ.get("LEAF_JOBS_STORE", "legacy").strip().lower()
    if mode not in {"legacy", "postgres"}:
        raise RuntimeError("LEAF_JOBS_STORE must be 'legacy' or 'postgres'")
    return mode


def validate_store_startup() -> None:
    """Fail before serving when the selected job authority is not ready."""
    if job_store_mode() == "postgres":
        _pg_store.ensure_ready()

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
  error_json  TEXT,
  execution_json TEXT,
  attempt INTEGER NOT NULL DEFAULT 0,
  lease_owner TEXT,
  lease_expires_at REAL,
  heartbeat_at REAL,
  provenance_json TEXT,
  terminal_fingerprint TEXT,
  terminal_conflict_json TEXT,
  org_id TEXT,
  project_id TEXT,
  authority_mode TEXT NOT NULL DEFAULT 'legacy_sqlite',
  idempotency_key TEXT,
  submission_fingerprint TEXT,
  dwg_version INTEGER,
  platform_mirror_pending INTEGER NOT NULL DEFAULT 0
)
"""

_MIGRATIONS = {
    "execution_json": "TEXT",
    "attempt": "INTEGER NOT NULL DEFAULT 0",
    "lease_owner": "TEXT",
    "lease_expires_at": "REAL",
    "heartbeat_at": "REAL",
    "provenance_json": "TEXT",
    "terminal_fingerprint": "TEXT",
    "terminal_conflict_json": "TEXT",
    "org_id": "TEXT",
    "project_id": "TEXT",
    "authority_mode": "TEXT NOT NULL DEFAULT 'legacy_sqlite'",
    "idempotency_key": "TEXT",
    "submission_fingerprint": "TEXT",
    "dwg_version": "INTEGER",
    "platform_mirror_pending": "INTEGER NOT NULL DEFAULT 0",
}

_DWG_VERSION_BACKFILL_MIGRATION = "backfill_dwg_version"


def _apply_first_connect_migrations(conn: sqlite3.Connection) -> None:
    """Bring a freshly opened jobs DB up to the current schema, idempotently and
    safely under CONCURRENT first-connect from a second process.

    Two processes opening the same brand-new DB at once each read the same
    "pre-migration" state (column missing, ledger marker absent) before either
    commits, so both attempt the same DDL/INSERT. Each step tolerates the loser
    losing that race:
      * ``ADD COLUMN`` swallows ``duplicate column name`` (the other process
        committed the column between our ``table_info`` read and this ALTER);
      * the ledger marker uses ``INSERT OR IGNORE`` so a second insert of the same
        id is a no-op, not a ``UNIQUE`` IntegrityError;
      * any remaining write-lock error that outlasts busy_timeout is rolled back
        for cleanup (so we never strand ``in_transaction=True``) and re-raised, so
        the caller drops the half-migrated connection and a later start retries.
    The backfill touches only ``dwg_version IS NULL`` rows, so both processes
    running it converge to the same result.
    """
    try:
        conn.execute(_SCHEMA)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        for name, ddl in _MIGRATIONS.items():
            if name not in columns:
                try:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}")
                except sqlite3.OperationalError as exc:
                    # A concurrent first-connect committed this column between
                    # our table_info read and now: treat as already applied.
                    if "duplicate column name" not in str(exc).lower():
                        raise
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations ("
            "id TEXT PRIMARY KEY, applied_at REAL NOT NULL)")
        # Backfill dwg_version for rows written by releases that stored the pin
        # only inside execution_json. The ledger is written only after the scan
        # completes, so a prior interrupted backfill is retried rather than
        # permanently skipped (the ALTER above may already have committed
        # the column before a mid-backfill failure). Python-side parse, no
        # JSON1 dependency; malformed or non-object payloads are skipped.
        applied = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE id = ?",
            (_DWG_VERSION_BACKFILL_MIGRATION,)).fetchone()
        if applied is None:
            for row in conn.execute(
                    "SELECT job_id, execution_json FROM jobs "
                    "WHERE dwg_version IS NULL AND execution_json IS NOT NULL").fetchall():
                try:
                    decoded = json.loads(row["execution_json"])
                except (ValueError, TypeError):
                    continue
                pin = decoded.get("dwg_version") if isinstance(decoded, dict) else None
                if isinstance(pin, int) and not isinstance(pin, bool):
                    conn.execute("UPDATE jobs SET dwg_version = ? WHERE job_id = ?",
                                 (pin, row["job_id"]))
            # OR IGNORE: a concurrent first-connect that already committed the
            # marker (both ran the idempotent backfill) must not fault the loser
            # with UNIQUE constraint failed on schema_migrations.id.
            conn.execute(
                "INSERT OR IGNORE INTO schema_migrations (id, applied_at) VALUES (?, ?)",
                (_DWG_VERSION_BACKFILL_MIGRATION, time.time()))
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS jobs_project_idempotency_uq "
            "ON jobs(tenant_id, project_id, idempotency_key) "
            "WHERE project_id IS NOT NULL AND idempotency_key IS NOT NULL")
        conn.commit()
    except sqlite3.OperationalError:
        # Write-lock contention (e.g. "database is locked" / "database table is
        # locked") that outlasted busy_timeout can leave the connection
        # in_transaction=True; roll back for cleanup so we never strand an open
        # transaction, then re-raise for the caller to drop this connection.
        conn.rollback()
        raise


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Double-checked locking on the SINGLETON PROMOTION. Without it two threads
        # racing the None window (the DB is lazy — first-connect happens on the
        # first request, and ensure_started() calls _db() UNLOCKED) each build,
        # migrate, and assign a connection. The unconditional `_conn = conn` below
        # would then let a slow second builder flip _conn A->B *after* A was in use,
        # so a caller that reads _db() separately for execute and commit (_exec,
        # update_progress, release_for_retry) could execute on A but commit B —
        # stranding the write on the discarded connection. Serializing the build so
        # exactly one connection is ever created closes both the leak and that
        # lost-write. Threads that lose the race re-check _conn and reuse the winner.
        with _conn_lock:
            if _conn is None:
                deadline = time.monotonic() + 10.0
                while _conn is None:
                    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
                    conn.row_factory = sqlite3.Row
                    try:
                        conn.execute("PRAGMA busy_timeout = 10000")
                        # WAL negotiation itself takes a SQLite lock and can race
                        # before the migration guard runs. Retry the complete
                        # first-connect path so a losing process does not crash
                        # during startup.
                        conn.execute("PRAGMA journal_mode = WAL")
                        _apply_first_connect_migrations(conn)
                    except sqlite3.OperationalError as exc:
                        conn.close()
                        if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                            raise
                        time.sleep(0.05)
                        continue
                    except BaseException:
                        # Don't strand a half-migrated connection as the module
                        # singleton: close it (which rolls back any open transaction
                        # and frees the write lock) and leave _conn None for a later
                        # retry.
                        conn.close()
                        raise
                    _conn = conn
    return _conn


def reset_connection() -> None:
    """Drop the module's SQLite singleton, closing the handle we are dropping.

    THIS MODULE OWNS ITS CONNECTION. ``_db()`` hands the SAME object to every
    caller, so a closed handle left in ``_conn`` is not a local problem: every
    later job read or write raises ``sqlite3.ProgrammingError: Cannot operate on
    a closed database`` and the reaper daemon spins on the dead handle every
    interval. Callers that need the connection rebuilt (a test re-pointing
    ``DB_PATH``, say) must come through here rather than closing ``_conn`` in
    place, because closing in place leaves exactly that dead handle behind.

    BOTH locks, in the documented ``_lock -> _conn_lock`` order. ``_conn_lock``
    alone serializes CONSTRUCTION, not USE, and that is not enough: ``_exec``
    (and ``update_progress``, ``release_for_retry``) read ``_db()`` separately
    for the execute and the commit, so a reset landing between them closes the
    connection the write is sitting on -- rolling that write back -- and then
    the commit lands on a freshly built second connection with no transaction
    and returns success. That is a silent lost write, the same failure the
    double-checked locking in ``_db()`` was added to prevent; holding ``_lock``
    keeps a reset from ever landing mid-statement-pair. No caller takes
    ``_lock`` before calling this, so the plain (non-reentrant) ``_lock``
    cannot deadlock here.

    Clearing is done BEFORE the close, so a ``close()`` that raises (SQLite
    refuses to close over unfinalized statements) still leaves the singleton
    clean for the next ``_db()`` to rebuild. The close stays INSIDE both locks
    so no second connection is built while this one is still open.

    A reference already borrowed from ``_db()`` and used outside ``_lock`` is
    still the caller's problem: nothing here can revive a handle someone else
    is holding.
    """
    global _conn
    with _lock:
        with _conn_lock:
            conn, _conn = _conn, None
            if conn is not None:
                conn.close()


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
        # Additive Marathon fields; legacy clients can continue to use the original
        # record shape unchanged.
        "attempt": row["attempt"] if "attempt" in row.keys() else 0,
        "lease": ({"owner": row["lease_owner"], "expires_at": row["lease_expires_at"],
                   "heartbeat_at": row["heartbeat_at"]}
                  if "lease_owner" in row.keys() and row["lease_owner"] else None),
        "provenance": (json.loads(row["provenance_json"])
                       if "provenance_json" in row.keys() and row["provenance_json"] else None),
        "org_id": row["org_id"] if "org_id" in row.keys() else None,
        "project_id": row["project_id"] if "project_id" in row.keys() else None,
        "authority_mode": (row["authority_mode"] if "authority_mode" in row.keys()
                           else "legacy_sqlite"),
        "idempotency_key": row["idempotency_key"] if "idempotency_key" in row.keys() else None,
        "dwg_version": row["dwg_version"] if "dwg_version" in row.keys() else None,
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
# worker lanes (§10)
# --------------------------------------------------------------------------- #
def lane_for(tool: Dict[str, Any], aps_live: bool) -> str:
    """Lane selection at submit time from the resolved tool dict (§10): a
    drawing.write tool OR any live-APS run -> slow; everything else (mock reads,
    ~10ms broker round-trips) -> fast."""
    caps = (tool or {}).get("capabilities") or []
    if aps_live or "drawing.write" in caps:
        return LANE_SLOW
    return LANE_FAST


def lane_workers() -> Dict[str, int]:
    """Per-lane pool sizes, read from env at executor creation. JOB_WORKERS_SLOW
    defaults to the legacy JOB_WORKERS value so unset env == today's write
    behavior; JOB_WORKERS_FAST defaults to 8."""
    return {
        LANE_FAST: int(os.environ.get("JOB_WORKERS_FAST", "8")),
        LANE_SLOW: int(os.environ.get("JOB_WORKERS_SLOW", str(MAX_WORKERS))),
    }


# --------------------------------------------------------------------------- #
# public API (used by routers/jobs.py)
# --------------------------------------------------------------------------- #
def submit_job(tenant_id: str, tool: Dict[str, Any], params: Dict[str, Any], dwg: str,
               aps_live: bool, org_id: Optional[str] = None,
               project_id: Optional[str] = None,
               dwg_version: Optional[int] = None, *, idempotency_key: Optional[str] = None,
               authority_mode: str = "legacy_sqlite",
               platform_context: Optional[Dict[str, Any]] = None,
               checkout_holder: Optional[str] = None,
               checkout_fence: Optional[int] = None) -> str:
    """Insert the durable job row and hand it to the executor. Returns job_id.

    ``org_id`` / ``project_id`` carry the OPTIONAL project context from the
    ``X-Org-Id`` / ``X-Project-Id`` headers on ``POST /api/run``. When both are
    present AND a platform DB resolves, a canonical platform Job row is recorded
    (best-effort, env-gated; see platform_link). With no project context this is
    a no-op and the spine is byte-identical to before.

    ``dwg_version`` (None -> head, unchanged behaviour) pins the run to a specific
    immutable drawing version (da/store.py resolve_version). It is threaded to the
    broker call AND persisted on the job row as an additive ``dwg_version`` column
    (resolved 2026-07-22, closing the follow-up recorded at the pinning merge), so
    ``GET /api/jobs/{id}`` shows which version a past run was pinned to. Already-
    deployed ``jobs.db`` files gain the column via the ``_MIGRATIONS`` upgrade
    path, and rows written by releases that stored the pin only in
    ``execution_json`` are backfilled from it at the migration moment; rows with
    no recorded pin read back ``dwg_version: None``.

    ``checkout_holder`` / ``checkout_fence`` carry the SUBMITTING session's
    single-writer identity down to the store, which refuses a version published
    under another session's checkout. They are part of the durable execution
    context (like ``dwg_version``) so a restart-recovered job is authorized as
    the session that actually submitted it, never as an anonymous writer. They
    are deliberately NOT part of ``submission_fingerprint``: the fingerprint
    identifies the WORK, and reusing an idempotency key from a different session
    is a key-reuse question, not a different run input.
    """
    _reject_oversized_params(params)
    return _submit_job(
        tenant_id, tool, params, dwg, aps_live, org_id, project_id, dwg_version,
        idempotency_key=idempotency_key, authority_mode=authority_mode,
        platform_context=platform_context, checkout_holder=checkout_holder,
        checkout_fence=checkout_fence,
    )


def submit_plan_job(tenant_id: str, plan: Dict[str, Any], dwg: str, *,
                    checkout_holder: Optional[str], checkout_fence: Optional[int]) -> str:
    """Put a browser's full data plan on the durable live-write job lane."""
    _reject_oversized_params(plan)
    return _submit_job(
        tenant_id, PLAN_TOOL, plan, dwg, True,
        dwg_version=int(plan["parent_version"]), checkout_holder=checkout_holder,
        checkout_fence=checkout_fence, plan=plan,
    )


def _submit_job(tenant_id: str, tool: Dict[str, Any], params: Dict[str, Any], dwg: str,
                aps_live: bool, org_id: Optional[str] = None,
                project_id: Optional[str] = None, dwg_version: Optional[int] = None, *,
                idempotency_key: Optional[str] = None,
                authority_mode: str = "legacy_sqlite",
                platform_context: Optional[Dict[str, Any]] = None,
                checkout_holder: Optional[str] = None,
                checkout_fence: Optional[int] = None,
                plan: Optional[Dict[str, Any]] = None) -> str:
    """Shared durable insert and executor hand-off for tool and data-plan jobs."""
    if project_id and not idempotency_key:
        raise ValueError("Idempotency-Key is required for project-scoped runs")
    fingerprint_payload = {
        "tenantId": str(tenant_id), "orgId": org_id, "projectId": project_id,
        "tool": tool, "params": params, "dwg": dwg, "apsLive": bool(aps_live),
        "authorityMode": authority_mode,
        # The pinned version is part of run identity: reusing an idempotency key
        # with a different dwg_version is different input and must be rejected,
        # not silently deduped to the prior job.
        "dwgVersion": dwg_version,
    }
    if plan is not None:
        fingerprint_payload["plan"] = True
    submission_fingerprint = hashlib.sha256(json.dumps(
        fingerprint_payload, sort_keys=True, separators=(",", ":"), default=str,
    ).encode("utf-8")).hexdigest()
    ensure_started()
    job_id = str(uuid.uuid4())
    now = time.time()
    execution = {"tool": tool, "aps_live": bool(aps_live), "dwg_version": dwg_version,
                 "checkout_holder": checkout_holder, "checkout_fence": checkout_fence}
    if plan is not None:
        execution["plan"] = plan
    created = True
    if job_store_mode() == "postgres":
        job_id, created = _pg_store.submit({
            "job_id": job_id,
            "tenant_id": str(tenant_id),
            "tool": tool["name"],
            "params": json.dumps(params),
            "dwg": dwg,
            "created_at": now,
            "execution": json.dumps(execution),
            "org_id": str(org_id) if org_id is not None else None,
            "project_id": str(project_id) if project_id is not None else None,
            "authority_mode": authority_mode,
            "idempotency_key": idempotency_key,
            "submission_fingerprint": submission_fingerprint,
            "dwg_version": dwg_version,
        })
    else:
        with _lock:
            conn = _db()
            if project_id and idempotency_key:
                existing = conn.execute(
                    "SELECT job_id, submission_fingerprint FROM jobs "
                    "WHERE tenant_id = ? AND project_id = ? AND idempotency_key = ?",
                    (str(tenant_id), str(project_id), idempotency_key),
                ).fetchone()
                if existing is not None:
                    if existing["submission_fingerprint"] != submission_fingerprint:
                        raise ValueError("idempotency key already exists with different run input")
                    return str(existing["job_id"])
            try:
                conn.execute(
                    "INSERT INTO jobs (job_id, tenant_id, tool, params_json, dwg, status, progress, "
                    "created_at, updated_at, execution_json, org_id, project_id, authority_mode, "
                    "idempotency_key, submission_fingerprint, dwg_version) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (job_id, str(tenant_id), tool["name"], json.dumps(params), dwg, "submitted",
                     "queued", now, now, json.dumps(execution),
                     org_id, project_id, authority_mode, idempotency_key, submission_fingerprint,
                     dwg_version),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()
                existing = conn.execute(
                    "SELECT job_id, submission_fingerprint FROM jobs "
                    "WHERE tenant_id = ? AND project_id = ? AND idempotency_key = ?",
                    (str(tenant_id), str(project_id), idempotency_key),
                ).fetchone()
                if existing is None or existing["submission_fingerprint"] != submission_fingerprint:
                    raise ValueError("idempotency key already exists with different run input")
                return str(existing["job_id"])
    if not created:
        return job_id
    # In legacy_sqlite this is a diagnostic mirror. postgres_canonical requests
    # are rejected by resolve_submission_context until the Postgres dispatcher is
    # connected, so this path can never silently become a second authority.
    platform_link.on_submit(job_id, org_id, project_id, tool.get("name"), params,
                            context=platform_context)
    executor = _executors.get(lane_for(tool, aps_live))
    assert executor is not None
    executor.submit(_run_job, job_id, tenant_id, tool, params, dwg, aps_live, dwg_version,
                    checkout_holder, checkout_fence,
                    **({"plan": plan} if plan is not None else {}))
    return job_id


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    if job_store_mode() == "postgres":
        return _pg_store.get(job_id)
    rows = _query("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
    return _row_to_record(rows[0]) if rows else None


def list_jobs(tenant_id: Optional[str] = None, limit: int = 20) -> List[Dict[str, Any]]:
    limit = max(1, min(int(limit), 200))
    if job_store_mode() == "postgres":
        return _pg_store.list(tenant_id, limit)
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
# Marathon delivery ownership and terminal callback idempotency
# --------------------------------------------------------------------------- #
def claim_lease(job_id: str, worker_id: str, now: Optional[float] = None) -> Optional[int]:
    """Atomically acquire a submitted or expired lease and return its attempt number.

    The compare-and-set UPDATE is deliberately the ownership authority: a second
    worker sees zero changed rows, including after a process restart where only the
    SQLite row remains.  SQLite's write transaction plus this predicate prevents
    two workers from executing the same attempt.
    """
    stamp = time.time() if now is None else now
    expires = stamp + lease_duration_s()
    if job_store_mode() == "postgres":
        return _pg_store.claim(job_id, worker_id, stamp, expires, max_attempts())
    with _lock:
        conn = _db()
        cur = conn.execute(
            "UPDATE jobs SET status = 'running', progress = 'running', "
            "started_at = COALESCE(started_at, ?), updated_at = ?, "
            "attempt = attempt + 1, lease_owner = ?, lease_expires_at = ?, heartbeat_at = ? "
            "WHERE job_id = ? AND (status = 'submitted' OR "
            "(status = 'running' AND (lease_expires_at IS NULL OR lease_expires_at <= ?))) "
            "AND progress <> ? AND attempt < ?",
            (
                stamp, stamp, worker_id, expires, stamp, job_id, stamp,
                CLOSED_PROGRESS, max_attempts(),
            ))
        conn.commit()
        if cur.rowcount != 1:
            return None
        row = conn.execute("SELECT attempt FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return int(row[0])


def heartbeat_lease(job_id: str, worker_id: str, progress: Optional[str] = None) -> bool:
    """Extend only the current holder's lease; a reclaimed worker cannot revive it."""
    now = time.time()
    if job_store_mode() == "postgres":
        return _pg_store.heartbeat(
            job_id, worker_id, now, now + lease_duration_s(), progress)
    sets = "updated_at = ?, lease_expires_at = ?, heartbeat_at = ?"
    args: List[Any] = [now, now + lease_duration_s(), now]
    if progress is not None:
        sets += ", progress = ?"
        args.append(progress)
    args.extend([job_id, worker_id, now])
    with _lock:
        cur = _db().execute(
            f"UPDATE jobs SET {sets} WHERE job_id = ? AND status = 'running' "
            "AND lease_owner = ? AND lease_expires_at >= ?",
            tuple(args))
        _db().commit()
        return cur.rowcount == 1


def _terminal_fingerprint(status: str, result_env: Optional[Dict[str, Any]],
                          error: Optional[Dict[str, Any]],
                          provenance: Optional[Dict[str, Any]]) -> str:
    payload = json.dumps({"status": status, "result": result_env, "error": error,
                          "provenance": provenance},
                         sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_terminal_context(
    status: str, result_env: Optional[Dict[str, Any]],
    provenance: Optional[Dict[str, Any]], durable_attempt: int,
    execution: Dict[str, Any],
) -> None:
    if status != "complete":
        # A FAILURE THAT NAMES AN ATTEMPT IS BOUND TO IT, exactly like a success.
        # This used to return unconditionally, so the attempt comparison below
        # covered successes only. The callback seam reads the job's attempt, then
        # calls in here; a lease reclaim landing in that window let a STALE
        # attempt's failure mark a newer, still-running attempt as failed. The
        # success path was already safe because this function re-reads the durable
        # attempt; the failure path had nothing.
        #
        # Guarded on "carries an attempt", NOT made mandatory: failures are also
        # raised by callers that legitimately have no provenance at all, notably
        # the orphan reaper (`_reap_orphans_once`), and requiring provenance here
        # would break them. So this binds the emitters that DO claim an attempt and
        # leaves the others exactly as they were.
        if isinstance(provenance, dict) and "attempt" in provenance:
            failed_attempt = provenance["attempt"]
            if failed_attempt != durable_attempt:
                raise ValueError(
                    "terminal failure provenance attempt does not match durable attempt")
        return
    assert isinstance(provenance, dict)
    attempt = provenance["attempt"]
    execution_path = provenance["execution_path"]
    if attempt != durable_attempt:
        raise ValueError("successful terminal provenance attempt does not match durable attempt")
    aps_live = bool(execution.get("aps_live", False))
    fallback = provenance.get("fallback") is True
    if aps_live and execution_path == "local" and not fallback:
        raise ValueError("APS-live local success must declare fallback")
    if aps_live and execution_path == "cloud" and fallback:
        raise ValueError("cloud success cannot declare local fallback")
    if not aps_live and (execution_path != "local" or fallback):
        raise ValueError("non-APS success requires a non-fallback local execution_path")
    if fallback:
        fallback_reason = provenance.get("fallback_reason")
        cloud_failure = provenance.get("cloud_failure")
        failure = cloud_failure.get("failure") if isinstance(cloud_failure, dict) else None
        cloud_attempt = (
            cloud_failure.get("attempt") if isinstance(cloud_failure, dict) else None)
        meaningful_failure = (
            isinstance(failure, str) and bool(failure.strip())
        ) or (
            isinstance(failure, dict)
            and isinstance(failure.get("code"), str)
            and bool(failure["code"].strip())
            and isinstance(failure.get("message"), str)
            and bool(failure["message"].strip())
        )
        if (
            not isinstance(fallback_reason, str) or not fallback_reason.strip()
            or not isinstance(cloud_failure, dict) or not cloud_failure
            or not isinstance(cloud_attempt, int) or isinstance(cloud_attempt, bool)
            or cloud_attempt != durable_attempt
            or cloud_failure.get("execution_path") != "cloud"
            or not meaningful_failure
        ):
            raise ValueError(
                "fallback success requires a reason and matching meaningful cloud_failure")


def complete_callback(job_id: str, status: str, *, result_env: Optional[Dict[str, Any]] = None,
                      error: Optional[Dict[str, Any]] = None,
                      worker_id: Optional[str] = None,
                      provenance: Optional[Dict[str, Any]] = None,
                      _allow_closed: bool = False) -> str:
    """Apply one terminal callback exactly once.

    Returns ``applied``, ``duplicate``, ``conflict``, or ``not_owner``. A terminal
    result is immutable: a different later callback is retained as conflict audit
    metadata and never changes the first outcome.
    """
    if status not in TERMINAL:
        raise ValueError("terminal status must be complete or failed")
    if status == "complete":
        if not isinstance(result_env, dict) or result_env.get("ok") is not True:
            raise ValueError("successful terminal callback requires result.ok true")
        if error is not None:
            raise ValueError("successful terminal callback cannot include terminal error")
        if not isinstance(provenance, dict):
            raise ValueError("successful terminal callback requires provenance")
        attempt = provenance.get("attempt")
        execution_path = provenance.get("execution_path")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise ValueError("successful terminal provenance requires a positive attempt")
        if execution_path not in {"cloud", "local"}:
            raise ValueError("successful terminal provenance requires cloud or local execution_path")
        if "fallback" in provenance and not isinstance(provenance["fallback"], bool):
            raise ValueError("successful terminal fallback flag must be boolean")
        embedded_provenance = result_env.get("execution_provenance")
        if embedded_provenance is not None and json.dumps(
                embedded_provenance, sort_keys=True, separators=(",", ":"), default=str
        ) != json.dumps(provenance, sort_keys=True, separators=(",", ":"), default=str):
            raise ValueError("result execution_provenance contradicts terminal provenance")
    now = time.time()
    if job_store_mode() == "postgres":
        durable = _pg_store.durable_context(job_id)
        if durable is None:
            return "missing"
        durable_attempt = durable["attempt"]
        _validate_terminal_context(
            status, result_env, provenance, durable_attempt, durable["execution"])
        fingerprint = _terminal_fingerprint(status, result_env, error, provenance)
        outcome = _pg_store.complete(
            job_id, durable_attempt, status, result_env, error, provenance,
            fingerprint, worker_id, now, allow_closed=_allow_closed,
        )
        if outcome == "applied":
            # The platform mirror already landed inside _pg_store.complete()'s own
            # transaction, atomically with the authority row. Only the in-process
            # correlation housekeeping is left to do here.
            platform_link.forget(job_id)
            _emit_job_terminal(status)
            _emit_job_terminal_event(job_id, status, error, provenance)
            _write_terminal_receipt(job_id)
        return outcome
    applied = False
    with _lock:
        conn = _db()
        durable = conn.execute(
            "SELECT attempt, execution_json, org_id, project_id, tenant_id, tool "
            "FROM jobs WHERE job_id = ?", (job_id,)
        ).fetchone()
        if durable is None:
            return "missing"
        durable_attempt = int(durable["attempt"] or 0)
        # Captured under the lock for the post-lock telemetry emit: no
        # connection may cross the lock boundary (review #420 round 1).
        event_tenant_id, event_tool = durable["tenant_id"], durable["tool"]
        _validate_terminal_context(
            status, result_env, provenance, durable_attempt,
            json.loads(durable["execution_json"] or "{}"),
        )
        fingerprint = _terminal_fingerprint(status, result_env, error, provenance)
        # The PostgreSQL mirror lives in a DIFFERENT database, so it cannot join
        # this transaction the way _pg_store.complete() joins its own. Commit an
        # outstanding-mirror marker WITH the terminal row instead: an undelivered
        # mirror then survives as durable state that the sweep drains, rather than
        # as a swallowed exception that leaves the platform Job nonterminal forever.
        # Project linkage is durable job state. Do not derive retry intent from
        # the current process configuration: a restart with a temporarily
        # missing DATABASE_URL must not erase a mirror that becomes deliverable
        # after configuration is restored.
        mirror_pending = 1 if durable["org_id"] and durable["project_id"] else 0
        owner_clause = "" if _allow_closed else " AND progress <> ?"
        args: List[Any] = [
            status, "done" if status == "complete" else "error", now, now, now, now,
            json.dumps(result_env) if result_env is not None else None,
            json.dumps(error) if error is not None else None,
            json.dumps(provenance) if provenance is not None else None,
            fingerprint, mirror_pending, job_id, durable_attempt,
        ]
        if not _allow_closed:
            args.append(CLOSED_PROGRESS)
        if worker_id is not None:
            owner_clause += " AND lease_owner = ? AND lease_expires_at >= ?"
            args.extend([worker_id, now])
        cur = conn.execute(
            "UPDATE jobs SET status = ?, progress = ?, updated_at = ?, finished_at = ?, "
            "elapsed_ms = CAST((? - COALESCE(started_at, ?)) * 1000 AS INTEGER), "
            "result_json = ?, error_json = ?, provenance_json = ?, "
            "terminal_fingerprint = ?, platform_mirror_pending = ?, "
            "lease_owner = NULL, lease_expires_at = NULL "
            "WHERE job_id = ? AND attempt = ? AND status NOT IN ('complete', 'failed')" + owner_clause,
            tuple(args))
        conn.commit()
        if cur.rowcount == 1:
            applied = True
        else:
            row = conn.execute(
                "SELECT status, terminal_fingerprint FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
            if row is None:
                return "missing"
            if row["status"] not in TERMINAL:
                return "not_owner"
            if row["terminal_fingerprint"] == fingerprint:
                return "duplicate"
            conflict = {"status": status, "result": result_env, "error": error,
                        "provenance": provenance, "received_at": now}
            conn.execute(
                "UPDATE jobs SET terminal_conflict_json = ?, updated_at = ? "
                "WHERE job_id = ? AND status IN ('complete', 'failed')",
                (json.dumps(conflict), now, job_id))
            conn.commit()
            return "conflict"
    if applied:
        # Clear the marker only on a confirmed mirror. A missing configuration
        # or raised delivery failure leaves durable work for the sweep; neither
        # may escape and double-report a run whose authority write did land.
        if not mirror_pending:
            platform_link.forget(job_id)
        elif not platform_link.mirror_configured():
            pass  # durable marker remains until configuration is restored
        else:
            try:
                platform_link.try_terminal(job_id, status, result_env, error)
            except Exception as exc:  # noqa: BLE001
                print(f"[leaf-jobs] terminal platform mirror deferred for {job_id}: "
                      f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            else:
                _clear_platform_mirror_pending(job_id)
        _emit_job_terminal(status)
        _emit_job_terminal_event(job_id, status, error, provenance,
                                 tenant_id=event_tenant_id, tool=event_tool)
        _write_terminal_receipt(job_id)
        return "applied"
    return "not_owner"  # pragma: no cover - defensive


def _write_terminal_receipt(job_id: str) -> None:
    """Slice 11a: leave the SHA-stamped receipt.json beside the record once a
    terminal outcome is APPLIED (never on duplicate / conflict / not_owner, so
    the first outcome is the one the receipt records). Runs OUTSIDE _lock, after
    the row committed, and is best effort by contract: build_receipts swallows
    its own failures and this wrapper covers the import, so a receipt can never
    turn an applied terminal callback into an error."""
    try:
        import build_receipts  # noqa: PLC0415 - lazy, keeps jobs importable alone

        build_receipts.write_terminal_receipt(get_job(job_id))
    except Exception as exc:  # noqa: BLE001 - a receipt must never break the job
        print(f"[leaf-jobs] terminal receipt skipped for {job_id}: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #
def _progress_phase(
    tool: Dict[str, Any],
    aps_live: bool,
    params: Optional[Dict[str, Any]] = None,
) -> str:
    """The progress phase a run ENTERS before the (blocking) broker call (Contract 5c,
    §15). Read tools -> 'executing'; write tools -> 'storing version' (mock APS_LIVE=0)
    / 'extracting' (live re-extract APS_LIVE=1). Short + stable strings; documented in
    §15 as the vocabulary SSE/poll consumers can render."""
    if (tool or {}).get("name") == PLAN_TOOL_NAME:
        return "applying plan"
    caps = (tool or {}).get("capabilities") or []
    if isinstance(params, dict) and params.get("dry_run") is True:
        return "executing"
    if "drawing.write" in caps:
        return "extracting" if aps_live else "storing version"
    return "executing"


def _heartbeat(job_id: str, worker_id: str, progress: Optional[str] = None) -> bool:
    return heartbeat_lease(job_id, worker_id, progress)


def _finish(job_id: str, status: str, started: float,
            result_env: Optional[Dict[str, Any]] = None,
            error: Optional[Dict[str, Any]] = None,
            worker_id: Optional[str] = None,
            provenance: Optional[Dict[str, Any]] = None) -> str:
    # ``started`` remains part of this private compatibility seam for current
    # callers. complete_callback reads the durable started_at instead, which is
    # necessary after a dead process has been reclaimed.
    return complete_callback(job_id, status, result_env=result_env, error=error,
                             worker_id=worker_id, provenance=provenance)


def _retry_or_finish(job_id: str, worker_id: str, tenant_id: str, tool: Dict[str, Any],
                     params: Dict[str, Any], dwg: str, aps_live: bool,
                     error: Dict[str, Any], provenance: Dict[str, Any],
                     dwg_version: Optional[int] = None,
                     checkout_holder: Optional[str] = None,
                     checkout_fence: Optional[int] = None,
                     plan: Optional[Dict[str, Any]] = None) -> None:
    """Release a retryable attempt back to submitted, or record its final failure."""
    rec = get_job(job_id)
    if rec is None or not isinstance(rec.get("lease"), dict) or rec["lease"].get("owner") != worker_id:
        return
    now = time.time()
    if rec["lease"].get("expires_at") is None or float(rec["lease"]["expires_at"]) < now:
        return
    if error.get("retryable") and int(rec.get("attempt") or 0) < max_attempts():
        attempt = int(rec.get("attempt") or 0)
        if job_store_mode() == "postgres":
            released = _pg_store.release_for_retry(
                job_id, worker_id, attempt, now, error, provenance)
        else:
            with _lock:
                cur = _db().execute(
                    "UPDATE jobs SET status = 'submitted', progress = 'retrying', "
                    "updated_at = ?, error_json = ?, provenance_json = ?, "
                    "lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL "
                    "WHERE job_id = ? AND status = 'running' AND attempt = ? "
                    "AND lease_owner = ? AND lease_expires_at >= ? AND progress <> ?",
                    (
                        now, json.dumps(error), json.dumps(provenance), job_id,
                        attempt, worker_id, now, CLOSED_PROGRESS,
                    ))
                _db().commit()
                released = cur.rowcount == 1
        if released:
            executor = _executors.get(lane_for(tool, aps_live))
            if executor is not None:
                # Thread the pin through the in-process retry: dropping it here
                # would silently rerun a pinned job against head. Same for the
                # submitting session's checkout identity — a retry that dropped
                # it would re-run the job as an unnamed writer and skip the
                # single-writer check the first attempt was subject to.
                executor.submit(_run_job, job_id, tenant_id, tool, params, dwg, aps_live,
                                dwg_version, checkout_holder, checkout_fence,
                                **({"plan": plan} if plan is not None else {}))
        return
    _finish(job_id, "failed", time.time(), error=error, worker_id=worker_id,
            provenance=provenance)


def _run_job(job_id: str, tenant_id: str, tool: Dict[str, Any], params: Dict[str, Any],
             dwg: str, aps_live: bool, dwg_version: Optional[int] = None,
             checkout_holder: Optional[str] = None,
             checkout_fence: Optional[int] = None,
             plan: Optional[Dict[str, Any]] = None) -> None:
    # NOTE: `holder` below is the thread-result box, unrelated to the checkout
    # holder — hence the qualified `checkout_holder` name on this lane.
    worker_id = str(uuid.uuid4())
    attempt = claim_lease(job_id, worker_id)
    if attempt is None:
        return
    started = time.time()
    max_s = job_max_s()
    platform_link.on_running(job_id)  # best-effort; no-op if unlinked

    # Richer progress (Contract 5c, §15): mark the real phase this run is ENTERING
    # before the (blocking) broker call, so SSE/poll consumers see more than status flips.
    _heartbeat(job_id, worker_id, _progress_phase(tool, aps_live, params))

    holder: Dict[str, Any] = {}

    def _call() -> None:
        try:
            if plan is not None:
                holder["env"] = broker_client.run_plan_via_broker(
                    tenant_id, plan, dwg, timeout_s=max_s + 30,
                    dwg_version=dwg_version, ledger_event_key=f"{job_id}:broker-run",
                    checkout_holder=checkout_holder, checkout_fence=checkout_fence,
                    job_id=job_id,
                )
                return
            holder["env"] = broker_client.run_via_broker(
                tenant_id, tool, params, dwg, aps_live, timeout_s=max_s + 30,
                dwg_version=dwg_version,
                # Stable across delivery attempts. If a worker dies after APS
                # accepts work, a later attempt must observe the existing
                # executing admission, never buy the same run again.
                ledger_event_key=f"{job_id}:broker-run",
                checkout_holder=checkout_holder, checkout_fence=checkout_fence,
                # Correlates this job row with the live WorkItem inside the
                # broker, so a tab closed mid-run has an id to cancel.
                job_id=job_id,
            )
        except Exception as exc:  # noqa: BLE001
            holder["exc"] = exc

    inner = threading.Thread(target=_call, daemon=True, name=f"job-{job_id[:8]}")
    inner.start()
    deadline = started + max_s
    while inner.is_alive() and time.time() < deadline:
        inner.join(timeout=1.0)
        if inner.is_alive():
            if not _heartbeat(job_id, worker_id):
                return  # lease was reclaimed; never let an old worker complete it

    if inner.is_alive():
        # TIMEOUT — abandon the broker call (best-effort cancel: the broker
        # client's own HTTP timeout reaps the daemon thread soon after).
        err = error_obj(ErrorCode.TIMEOUT, f"job exceeded JOB_MAX_S={max_s:g}s", retryable=True)
        _retry_or_finish(job_id, worker_id, tenant_id, tool, params, dwg, aps_live, err,
                         {"attempt": attempt, "execution_path": "cloud" if aps_live else "local",
                          "failure": "timeout"}, dwg_version=dwg_version,
                         checkout_holder=checkout_holder, checkout_fence=checkout_fence,
                         plan=plan)
        return

    if "exc" in holder:
        exc = holder["exc"]
        if isinstance(exc, broker_client.BrokerUnreachable):
            err = error_obj(ErrorCode.BROKER_UNREACHABLE, str(exc), retryable=True)
        else:
            err = error_obj(ErrorCode.INTERNAL, f"{type(exc).__name__}: {exc}", retryable=False)
        cloud_failure = {"attempt": attempt, "execution_path": "cloud" if aps_live else "local",
                         "failure": {"code": err["error_code"], "message": err["message"]}}
        # A transport exception may be response loss after the broker crossed
        # the irreversible execution boundary. Replay the stable cloud event
        # key through retry handling. Never buy a separate local execution.
        _retry_or_finish(job_id, worker_id, tenant_id, tool, params, dwg, aps_live, err, cloud_failure, dwg_version=dwg_version,
                         checkout_holder=checkout_holder, checkout_fence=checkout_fence,
                         plan=plan)
        return

    env = holder.get("env") or {}
    if env.get("ok"):
        provenance = {"attempt": attempt, "execution_path": "cloud" if aps_live else "local"}
        embedded = env.get("execution_provenance")
        cad_timing = embedded.get("cad_timing") if isinstance(embedded, dict) else None
        if (isinstance(cad_timing, dict)
                and cad_timing.get("contract") == "leaf.cad-timing.v1"
                and isinstance(cad_timing.get("spans_ms"), dict)):
            provenance["cad_timing"] = cad_timing
        env = dict(env)
        env["execution_provenance"] = provenance
        _finish(job_id, "complete", started, result_env=env, worker_id=worker_id,
                provenance=provenance)
    else:
        err = env.get("error") or error_obj(ErrorCode.INTERNAL, "broker returned no error detail",
                                            retryable=False)
        provenance = {"attempt": attempt, "execution_path": "cloud" if aps_live else "local",
                      "failure": {"code": err.get("error_code"), "message": err.get("message")}}
        if (
            aps_live
            and plan is None
            and err.get("error_code") != ErrorCode.TURN_IN_PROGRESS
            and _allows_local_fallback(tool)
        ):
            _run_local_fallback(job_id, worker_id, tenant_id, tool, params, dwg, attempt, provenance,
                                dwg_version, checkout_holder, checkout_fence)
            return
        _retry_or_finish(job_id, worker_id, tenant_id, tool, params, dwg, aps_live, err, provenance, dwg_version=dwg_version,
                         checkout_holder=checkout_holder, checkout_fence=checkout_fence,
                         plan=plan)


def _allows_local_fallback(tool: Dict[str, Any]) -> bool:
    """Fallback is opt-in only in trusted authored tool policy, never by callers."""
    policy = tool.get("marathon") if isinstance(tool.get("marathon"), dict) else {}
    return bool(policy.get("allow_local_fallback") or tool.get("allow_local_fallback"))


def _run_local_fallback(job_id: str, worker_id: str, tenant_id: str, tool: Dict[str, Any],
                        params: Dict[str, Any], dwg: str, attempt: int,
                        cloud_failure: Dict[str, Any],
                        dwg_version: Optional[int] = None,
                        checkout_holder: Optional[str] = None,
                        checkout_fence: Optional[int] = None) -> None:
    """Run local only after recording a cloud-path failure in the success provenance."""
    holder: Dict[str, Any] = {}

    def _call() -> None:
        try:
            holder["env"] = broker_client.run_via_broker(
                tenant_id, tool, params, dwg, False, timeout_s=job_max_s() + 30,
                dwg_version=dwg_version,
                # Distinct from the cloud fingerprint, stable across delivery
                # retries of this job's one authorized fallback execution.
                ledger_event_key=f"{job_id}:broker-fallback",
                # The fallback publishes a version too, so it must be authorized
                # as the same session the cloud attempt was.
                checkout_holder=checkout_holder, checkout_fence=checkout_fence,
            )
        except Exception as exc:  # noqa: BLE001
            holder["exc"] = exc

    inner = threading.Thread(target=_call, daemon=True, name=f"fallback-{job_id[:8]}")
    inner.start()
    deadline = time.time() + job_max_s()
    while inner.is_alive() and time.time() < deadline:
        inner.join(timeout=1.0)
        if inner.is_alive() and not heartbeat_lease(job_id, worker_id, "local fallback"):
            return
    if inner.is_alive():
        err = error_obj(ErrorCode.TIMEOUT, "local fallback timed out", True)
        _retry_or_finish(job_id, worker_id, tenant_id, tool, params, dwg, True, err,
                         {"attempt": attempt, "execution_path": "local",
                          "fallback_from": cloud_failure, "failure": "local fallback timeout"}, dwg_version=dwg_version,
                         checkout_holder=checkout_holder, checkout_fence=checkout_fence)
        return
    if "exc" in holder:
        exc = holder["exc"]
        err = error_obj(ErrorCode.INTERNAL, f"local fallback {type(exc).__name__}: {exc}", True)
        _retry_or_finish(job_id, worker_id, tenant_id, tool, params, dwg, True, err,
                         {"attempt": attempt, "execution_path": "local",
                          "fallback_from": cloud_failure, "failure": "local fallback failed"}, dwg_version=dwg_version,
                         checkout_holder=checkout_holder, checkout_fence=checkout_fence)
        return
    env = holder.get("env") or {}
    if env.get("ok"):
        provenance = {"attempt": attempt, "execution_path": "local", "fallback": True,
                      "fallback_reason": "cloud execution failed", "cloud_failure": cloud_failure}
        out = dict(env)
        out["execution_provenance"] = provenance
        _finish(job_id, "complete", time.time(), result_env=out, worker_id=worker_id,
                provenance=provenance)
        return
    err = env.get("error") or error_obj(ErrorCode.INTERNAL, "local fallback returned no error", True)
    _retry_or_finish(job_id, worker_id, tenant_id, tool, params, dwg, True, err,
                     {"attempt": attempt, "execution_path": "local",
                      "fallback_from": cloud_failure, "failure": "local fallback failed"}, dwg_version=dwg_version,
                         checkout_holder=checkout_holder, checkout_fence=checkout_fence)


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
    """Redispatch stale work from its serialized execution context.

    A prior process may still have a thread in flight, so this routine does not
    terminate the row first.  A new worker must win ``claim_lease`` after expiry;
    the old worker's heartbeat/completion is then rejected by lease ownership.
    Closed browser sessions remain terminal retryable failures, preserving the
    pre-existing close contract.
    """
    stale_before = time.time() - heartbeat_stale_s()
    if job_store_mode() == "postgres":
        rows = _pg_store.reclaimable(stale_before, CLOSED_PROGRESS, time.time())
    else:
        rows = _query(
            "SELECT * FROM jobs WHERE status IN ('submitted','running') AND "
            "(updated_at < ? OR progress = ? OR "
            "(lease_expires_at IS NOT NULL AND lease_expires_at < ?))",
            (stale_before, CLOSED_PROGRESS, time.time()))
    _retry_pending_reaps()
    if job_store_mode() != "postgres":
        # Legacy authority only: the PostgreSQL authority already mirrors inside
        # its own transaction, so it never leaves a marker to drain.
        _retry_pending_platform_mirrors()
    handled = 0
    for row in rows:
        if row["progress"] == CLOSED_PROGRESS:
            complete_callback(row["job_id"], "failed",
                              error=error_obj(ErrorCode.INTERNAL,
                                              "orphaned: session closed (tab-close)", True),
                              _allow_closed=True)
            _cancel_remote_workitem(row["job_id"], _row_get(row, "tenant_id"))
            _emit_orphan_reaped_event(row, "session_closed")
            handled += 1
            continue
        if _redispatch_record(row["job_id"]):
            _emit_orphan_reaped_event(row, "stale_redispatched")
            handled += 1
    return handled


def _emit_orphan_reaped_event(row: Any, reason: str) -> None:
    """Best-effort `job.orphan_reaped` product event (P2): the silent-failure
    class, per tenant. NEVER raises. The pg sweep projection carries only
    job_id+progress, so identity comes from the store lookup, gated on an
    ENABLED sink; a lookup miss emits nothing rather than fabricated
    identity."""
    if telemetry_sink is None:
        return
    try:
        if telemetry_sink.disabled_reason() is not None:
            return
        job_id = row["job_id"]
        tenant_id = _row_get(row, "tenant_id")
        tool = _row_get(row, "tool")
        if not tenant_id and job_store_mode() == "postgres":
            ctx = _pg_store.event_context(job_id)
            if ctx:
                tenant_id, tool = ctx["tenant_id"], ctx["tool"]
        if not tenant_id:
            return
        labels: Dict[str, Any] = {"job_id": job_id, "reason": reason}
        if tool:
            labels["tool"] = tool
        updated_at = _row_get(row, "updated_at")
        if updated_at is not None:
            labels["staleness_s"] = round(max(0.0, time.time() - float(updated_at)), 1)
        telemetry_sink.emit(
            "job.orphan_reaped",
            tenant_id=str(tenant_id),
            tenant_kind="guest" if str(tenant_id).startswith("guest-") else "account",
            session_id="server",
            labels=labels,
        )
    except Exception:  # noqa: BLE001 - telemetry must never break the reaper
        pass


def _row_get(row: Any, key: str) -> Any:
    """Read `key` from a sqlite3.Row or a dict row, None when absent.

    The two stores return different shapes for the same sweep: sqlite selects
    whole rows, while the PostgreSQL `reclaimable` projection is job_id+progress
    only. Missing columns must not raise -- sqlite3.Row raises IndexError and a
    dict raises KeyError for the same lookup.
    """
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


# Tab-close reaps the broker could not accept yet (broker down, restarting,
# transient network). The job row is ALREADY terminal by then and will never be
# selected by the sweep again, so without this the single failed attempt is the
# end of it and APS bills the abandoned WorkItem to completion. Retried on every
# later sweep until the broker takes it. Bounded so a long outage cannot grow it
# without limit; the cap is far above any plausible burst of closed tabs.
_pending_reaps: Dict[str, Dict[str, Any]] = {}
_pending_reaps_lock = threading.Lock()
PENDING_REAP_MAX = int(os.environ.get("PENDING_REAP_MAX", "512"))
PENDING_REAP_BATCH = int(os.environ.get("PENDING_REAP_BATCH", "32"))
# Short on purpose: every reap is a small call, and it runs inside the sweep,
# where a slow broker would hold up the rows closing right now.
PENDING_REAP_TIMEOUT_S = float(os.environ.get("PENDING_REAP_TIMEOUT_S", "5"))
# A reap that has failed this many times is not going to start working. Giving
# up LOUDLY beats retrying forever and starving the jobs behind it.
PENDING_REAP_MAX_ATTEMPTS = int(os.environ.get("PENDING_REAP_MAX_ATTEMPTS", "20"))
# The queue is persisted for the same reason the broker persists correlations:
# the job row is already terminal when the reap fails, so the sweep will never
# select it again and an app restart would otherwise lose the ONLY remaining
# signal that a live WorkItem still needs cancelling.
PENDING_REAPS_PATH = Path(os.environ.get(
    "PENDING_REAPS_PATH", str(DB_PATH.parent / "pending_reaps.jsonl")))


def _persist_pending_reaps_locked() -> None:
    """Rewrite the queue. CALLER MUST HOLD `_pending_reaps_lock`.

    The whole queue is rewritten rather than appended: it is bounded by
    PENDING_REAP_MAX, so this stays small, and a single snapshot can never be
    read back in an order that contradicts memory.
    """
    try:
        PENDING_REAPS_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = PENDING_REAPS_PATH.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            for job_id, rec in _pending_reaps.items():
                fh.write(json.dumps({"job_id": job_id, **rec}) + "\n")
        tmp.replace(PENDING_REAPS_PATH)
    except Exception as exc:  # noqa: BLE001
        print(f"[leaf-jobs] could not persist the pending-reap queue: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


def _load_pending_reaps() -> Dict[str, Dict[str, Any]]:
    """Restore the queue left by a previous process."""
    out: Dict[str, Dict[str, Any]] = {}
    try:
        if not PENDING_REAPS_PATH.exists():
            return out
        with PENDING_REAPS_PATH.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                job_id = rec.pop("job_id", None)
                if job_id:
                    out[job_id] = {"tenant_id": rec.get("tenant_id"),
                                   "attempts": int(rec.get("attempts") or 0)}
    except Exception as exc:  # noqa: BLE001
        print(f"[leaf-jobs] could not restore the pending-reap queue: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    return out


def _remember_pending_reap(job_id: str, tenant_id: Optional[str]) -> None:
    with _pending_reaps_lock:
        existing = _pending_reaps.get(job_id)
        if existing is None and len(_pending_reaps) >= PENDING_REAP_MAX:
            print(f"[leaf-jobs] pending-reap queue full ({PENDING_REAP_MAX}); "
                  f"dropping job {job_id} - its APS WorkItem may bill to "
                  f"completion", file=sys.stderr, flush=True)
            return
        attempts = int((existing or {}).get("attempts") or 0) + 1
        if attempts > PENDING_REAP_MAX_ATTEMPTS:
            _pending_reaps.pop(job_id, None)
            _persist_pending_reaps_locked()
            print(f"[leaf-jobs] giving up on the tab-close reap for job {job_id} "
                  f"after {PENDING_REAP_MAX_ATTEMPTS} attempts - its APS WorkItem "
                  f"may bill to completion", file=sys.stderr, flush=True)
            return
        # Re-insert at the TAIL so the batch below rotates: an entry that can
        # never succeed must not sit at the head and starve everything after it.
        _pending_reaps.pop(job_id, None)
        _pending_reaps[job_id] = {"tenant_id": tenant_id, "attempts": attempts}
        _persist_pending_reaps_locked()


def _forget_pending_reap(job_id: str) -> None:
    with _pending_reaps_lock:
        if _pending_reaps.pop(job_id, None) is not None:
            _persist_pending_reaps_locked()


_pending_reaps.update(_load_pending_reaps())


PLATFORM_MIRROR_BATCH = 25


def _clear_platform_mirror_pending(job_id: str) -> None:
    """Mark this row's platform mirror delivered. Never raises."""
    try:
        with _lock:
            conn = _db()
            conn.execute(
                "UPDATE jobs SET platform_mirror_pending = 0 WHERE job_id = ?", (job_id,))
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        # Worst case the marker survives a delivered mirror and the sweep replays
        # it. _update_by_spine is spine_ref-keyed and refuses already-terminal
        # rows, so a replay is a harmless 0-row UPDATE, never a relabel.
        print(f"[leaf-jobs] could not clear the platform-mirror marker for {job_id}: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


def _retry_pending_platform_mirrors() -> None:
    """Deliver terminal mirrors that failed when their authority row committed.

    The SQLite authority and the PostgreSQL linkage are separate databases, so a
    terminal write cannot carry the mirror in one transaction the way the
    PostgreSQL authority can. It commits ``platform_mirror_pending`` alongside the
    terminal row instead, and this drains that marker.

    Bounded exactly like the reap queue, and for the same reason: at most
    PLATFORM_MIRROR_BATCH per sweep, oldest first, and the batch is abandoned on
    the first failure, because linkage that is down for one row is down for all
    and grinding through the rest would only stall the sweep behind it.
    """
    if not platform_link.mirror_configured():
        return
    rows = _query(
        "SELECT job_id, status, result_json, error_json FROM jobs "
        "WHERE platform_mirror_pending = 1 AND status IN ('complete', 'failed') "
        "ORDER BY updated_at LIMIT ?",
        (PLATFORM_MIRROR_BATCH,))
    for row in rows:
        try:
            result_env = json.loads(row["result_json"]) if row["result_json"] else None
            error = json.loads(row["error_json"]) if row["error_json"] else None
        except (TypeError, ValueError) as exc:
            # A row we can never rebuild would otherwise sit at the head forever
            # and starve every mirror behind it (ORDER BY updated_at is stable).
            print(f"[leaf-jobs] dropping an unreplayable platform mirror for "
                  f"{row['job_id']}: {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
            _clear_platform_mirror_pending(row["job_id"])
            continue
        try:
            platform_link.try_terminal(row["job_id"], row["status"], result_env, error)
        except Exception as exc:  # noqa: BLE001
            print(f"[leaf-jobs] deferred platform mirror still unavailable for "
                  f"{row['job_id']}: {type(exc).__name__}: {exc}",
                  file=sys.stderr, flush=True)
            break
        _clear_platform_mirror_pending(row["job_id"])


def _retry_pending_reaps() -> None:
    """Re-attempt reaps a previous sweep could not deliver.

    Bounded three ways, because this runs INSIDE the sweep and everything behind
    it waits: at most PENDING_REAP_BATCH per sweep, the batch is abandoned on the
    first unreachable broker (down for one is down for all, and grinding through
    hundreds of timeouts would stall the tabs closing right now), and each entry
    rotates to the tail so a permanently stuck job cannot starve the queue.
    """
    with _pending_reaps_lock:
        batch = [(job_id, rec.get("tenant_id"))
                 for job_id, rec in list(_pending_reaps.items())[:PENDING_REAP_BATCH]]
    for job_id, tenant_id in batch:
        if not _cancel_remote_workitem(job_id, tenant_id, _retry=True):
            break


def _cancel_remote_workitem(job_id: str, tenant_id: Optional[str],
                            _retry: bool = False) -> bool:
    """Ask the broker to cancel this job's APS WorkItem (tab-close path ONLY).

    Returns whether the BROKER was reachable, which is what tells the retry loop
    to keep going: False means the broker is down for everyone and the rest of
    the batch would only burn timeouts.

    Marking the row terminal stops US from waiting; it does NOT stop APS, which
    keeps running and BILLING the abandoned WorkItem to completion. This is the
    call that actually reaps it.

    Restricted to the CLOSED_PROGRESS branch on purpose. A heartbeat-stale row is
    REDISPATCHED, and its prior worker may still be mid-flight, so cancelling by
    job_id there could kill the WorkItem the retry is about to adopt. A closed
    tab has no such race: the row is terminal and nothing will redispatch it.

    Best-effort by contract. The row is already terminal before this runs, and an
    unreachable broker must not undo that or stop the sweep's remaining rows. It
    is NOT silent: a failure here means real money is still burning on APS, so it
    is reported on stderr (the convention elsewhere in this codebase).
    """
    try:
        result = broker_client.reap_via_broker(
            [{
                "job_id": job_id,
                "tenant_id": tenant_id,
                "status": "inprogress",
                "workitem_id": None,   # broker resolves it from job_id
                "session_closed": True,
            }],
            timeout_s=PENDING_REAP_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001  (broker down != job not finished)
        _remember_pending_reap(job_id, tenant_id)
        print(f"[leaf-jobs] tab-close reap failed for job {job_id} "
              f"(queued for retry): {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)
        return False
    # A 200 means the broker ACCEPTED the reap, not that a WorkItem was
    # cancelled. When live reaping is on, only `cancelled_jobs` says the DELETE
    # actually succeeded; anything else is still burning money and is worth
    # another sweep. When live reaping is off, no cancel was ever going to
    # happen, so retrying would queue every closed tab forever.
    # Fail CLOSED on anything short of an explicit acknowledgement. A broker that
    # reports live reaping but names no cancelled_jobs -- an older broker mid
    # rolling-update, for instance -- has NOT told us the WorkItem stopped, and
    # treating that as done is how the billing quietly continues. The attempt cap
    # in _remember_pending_reap is what stops that retrying forever.
    if isinstance(result, dict) and result.get("live"):
        if job_id not in (result.get("cancelled_jobs") or []):
            _remember_pending_reap(job_id, tenant_id)
            print(f"[leaf-jobs] tab-close reap accepted but job {job_id} was not "
                  f"acknowledged as cancelled (queued for retry)",
                  file=sys.stderr, flush=True)
            return True
    _forget_pending_reap(job_id)
    return True


def _redispatch_record(job_id: str) -> bool:
    """Schedule persisted work after a restart; malformed legacy context fails safely."""
    rec = get_job(job_id)
    if rec is None or rec["status"] in TERMINAL:
        return False
    if int(rec.get("attempt") or 0) >= max_attempts():
        complete_callback(
            job_id, "failed",
            error=error_obj(ErrorCode.TIMEOUT, "job exhausted its delivery attempts", False),
            provenance={"attempt": int(rec.get("attempt") or 0),
                        "execution_path": "unknown", "failure": "attempt limit exhausted"},
        )
        return True
    try:
        if job_store_mode() == "postgres":
            execution = _pg_store.execution(job_id)
            if execution is None:
                raise IndexError
        else:
            rows = _query("SELECT execution_json FROM jobs WHERE job_id = ?", (job_id,))
            execution = json.loads(rows[0]["execution_json"] or "{}")
        tool = execution["tool"]
        if not isinstance(tool, dict):
            raise KeyError("tool")
        plan = execution.get("plan")
        if rec.get("tool") == PLAN_TOOL_NAME or tool.get("name") == PLAN_TOOL_NAME:
            if (rec.get("tool") != PLAN_TOOL_NAME or tool.get("name") != PLAN_TOOL_NAME
                    or not isinstance(plan, dict)):
                raise KeyError("plan")
        aps_live = bool(execution.get("aps_live", False))
        # Recover the version pin from the durable execution context so a
        # restart-recovered pinned job does not silently rerun against head.
        # This version ALWAYS writes the key (null for unpinned), so its
        # ABSENCE means the row predates pin persistence and its pin is
        # unrecoverable -- fail closed rather than default to head.
        if "dwg_version" not in execution:
            raise KeyError("dwg_version")
        dwg_version = execution["dwg_version"]
        # Recover the submitting session's checkout identity so a restart-
        # recovered write is authorized as the session that asked for it.
        #
        # A row written BEFORE this field existed has no such key, and reads back
        # as None. That is NOT an exemption: write_loop normalizes a missing
        # identity to store.ANONYMOUS_HOLDER at the write chokepoint, so a job
        # queued just before a rolling deploy is refused against a lock another
        # session took while it waited, instead of publishing under it. Absence
        # is normalized there rather than here so that EVERY route into the
        # write path is covered — recovery, retry, local fallback, and an older
        # app or broker that sends no identity at all.
        checkout_holder = execution.get("checkout_holder")
        checkout_fence = execution.get("checkout_fence")
    except (IndexError, KeyError, TypeError, ValueError):
        complete_callback(job_id, "failed", error=error_obj(
            ErrorCode.INTERNAL, "cannot recover job: missing execution context", False))
        return False
    executor = _executors.get(lane_for(tool, aps_live))
    if executor is None:
        return False
    executor.submit(_run_job, job_id, rec["tenant_id"], tool, rec["params"], rec["dwg"],
                    aps_live, dwg_version, checkout_holder, checkout_fence,
                    **({"plan": plan} if plan is not None else {}))
    return True


# --------------------------------------------------------------------------- #
# session-close seam (tab-close / session-end -> orphan reaping signal)
# --------------------------------------------------------------------------- #
def mark_job_closed(job_id: str) -> bool:
    """Flag ONE in-flight job's owner as gone (progress -> 'closed'). The orphan
    reaper fails it on its next sweep. No-op on terminal/unknown jobs. Returns
    True iff a live job was flagged."""
    if job_store_mode() == "postgres":
        return _pg_store.mark_closed(job_id, CLOSED_PROGRESS, time.time())
    rows = _query("SELECT status FROM jobs WHERE job_id = ?", (job_id,))
    if not rows or rows[0]["status"] in TERMINAL:
        return False
    _exec("UPDATE jobs SET progress = ?, updated_at = ?, lease_owner = NULL, "
          "lease_expires_at = NULL, heartbeat_at = NULL WHERE job_id = ?"
          " AND status IN ('submitted','running')",
          (CLOSED_PROGRESS, time.time(), job_id))
    return True


def close_tenant_jobs(tenant_id: str) -> int:
    """Flag ALL of a tenant's in-flight jobs closed (session-end). Returns count."""
    if job_store_mode() == "postgres":
        return _pg_store.close_tenant(tenant_id, CLOSED_PROGRESS, time.time())
    rows = _query("SELECT job_id FROM jobs WHERE tenant_id = ?"
                  " AND status IN ('submitted','running')", (tenant_id,))
    now = time.time()
    for r in rows:
        _exec("UPDATE jobs SET progress = ?, updated_at = ?, lease_owner = NULL, "
              "lease_expires_at = NULL, heartbeat_at = NULL WHERE job_id = ?",
              (CLOSED_PROGRESS, now, r["job_id"]))
    return len(rows)


def orphan_lease_records(tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """Lease records for in-flight jobs that are closed OR heartbeat-stale — the
    payload the app POSTs to the broker's /broker/reap so their APS WorkItems get
    cancelled. workitem_id is broker-side (job_id is carried for correlation)."""
    stale_before = time.time() - heartbeat_stale_s()
    if job_store_mode() == "postgres":
        rows = _pg_store.orphan_leases(stale_before, CLOSED_PROGRESS, tenant_id)
        return [
            {
                "job_id": row["job_id"], "tenant_id": row["tenant_id"],
                "status": "inprogress", "workitem_id": None,
                "session_closed": row["progress"] == CLOSED_PROGRESS,
            }
            for row in rows
        ]
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


# Failure-log throttle bookkeeping for the reaper sweep. Process-local: a fresh
# process starts silent and reports its first failure in full. One Uvicorn worker
# runs per container, so separate ECS tasks keep separate streaks.
_reaper_log_lock = threading.Lock()
_reaper_failure_state: Dict[str, Any] = {
    "consecutive": 0,        # failures since the last successful sweep, ALL classes
    "seen_types": set(),     # fault classes reported this streak, capped for memory
    "class_overflow": False, # more distinct classes than seen_types can hold
    "verbose_in_window": 0,  # full tracebacks already spent in the current window
    "suppressed": 0,         # failures counted but not logged since the last line
    "last_type": None,       # most recent fault class, for the recovery line
    "last_logged_at": None,  # time.monotonic() stamp of the last emitted line
}


def _exc_key(exc: BaseException) -> str:
    """Identify a fault CLASS unambiguously.

    `__qualname__` alone collides: `package_a.Error` and `package_b.Error` both
    render as `Error`, so a genuine change of fault class would look like a
    repeat and be throttled away. Qualifying by module makes distinct classes
    distinct.
    """
    cls = type(exc)
    return f"{getattr(cls, '__module__', '?')}.{cls.__qualname__}"


def _reset_reaper_failure_state() -> None:
    """Restore fresh-process throttle bookkeeping (used by tests)."""
    with _reaper_log_lock:
        _reaper_failure_state.update(
            consecutive=0, seen_types=set(), class_overflow=False,
            verbose_in_window=0, suppressed=0, last_type=None, last_logged_at=None)


def _note_reaper_failure(exc: BaseException) -> None:
    """Report a failed sweep under a HARD per-window emission budget.

    The bound is on the LOG LINES, not on the fault classes, and that is the whole
    design. Two earlier versions bounded the wrong thing and both had a bypass: one
    keyed the escape on "the class CHANGED", so two alternating classes logged
    every interval; the next keyed it on "this class is NEW to the streak", so a
    fault throwing a fresh class every sweep logged every interval. Each fix
    closed one shape of the same hole. So the rule here is not about classes at
    all: per quiet window, at most _MAX_VERBOSE_PER_WINDOW full tracebacks and at
    most one terse reminder, no matter what the exceptions do. At the defaults a
    CONTINUOUS outage is <=48 lines/hour against the 360 this replaced, and no
    exception stream of any shape can inflate it.

    That ceiling bounds a failing STREAK, not the wall clock. A sweep that
    alternates failure and success ends its streak on every success, so the next
    failure is a first sighting again: a full traceback plus a recovery line, and
    an hour of flapping therefore exceeds 48. The reset is deliberate, since it
    is what re-arms reporting for the next failure, so a flapping sweep is meant
    to stay loud.

    Within that budget, a fault class not yet seen this streak still gets priority
    for a full traceback, because a new class is the highest-signal thing that can
    happen. It is a priority, not an exemption: once the window's verbose budget
    is spent, a new class is counted and named by the next line like anything else.

    Suppression is never silent. Every emitted line carries the running streak
    length, how many distinct classes it spans, and how many failures were
    suppressed since the last line, so a quiet log means "healthy", never
    "throttled into invisibility".

    `consecutive` counts EVERY failure since the last successful sweep regardless
    of class, so the number an operator reads is the true length of the outage.
    `seen_types` is capped at _MAX_TRACKED_FAULT_CLASSES: an outage throwing
    unique classes every 10s would otherwise grow it by 8,640 entries a day.

    NEVER raises: this runs on the daemon's failure path, so a fault in the
    bookkeeping would kill the very thread the swallow exists to protect.
    """
    try:
        exc_key = _exc_key(exc)
        now = time.monotonic()
        with _reaper_log_lock:
            state = _reaper_failure_state
            state["consecutive"] += 1
            state["last_type"] = exc_key

            first_of_streak = state["last_logged_at"] is None
            is_new_class = exc_key not in state["seen_types"]
            if is_new_class:
                if len(state["seen_types"]) < _MAX_TRACKED_FAULT_CLASSES:
                    state["seen_types"].add(exc_key)
                else:
                    # bounded memory: stop growing, remember that we stopped
                    state["class_overflow"] = True

            # The window check is what makes the bound hard. `first_of_streak`
            # short-circuits it so the very first failure reports even if the
            # throttle knob itself is broken.
            emit = True
            if not first_of_streak:
                if now - state["last_logged_at"] < reaper_log_throttle_s():
                    # new class with budget left still gets through; otherwise
                    # this failure is counted and nothing is logged
                    emit = (is_new_class
                            and state["verbose_in_window"] < _MAX_VERBOSE_PER_WINDOW)
                    if not emit:
                        state["suppressed"] += 1
                else:
                    state["verbose_in_window"] = 0  # a fresh window re-arms the budget

            verbose = emit and (first_of_streak or is_new_class)
            if emit:
                if verbose:
                    state["verbose_in_window"] += 1
                state["last_logged_at"] = now
                suppressed = state["suppressed"]
                state["suppressed"] = 0
                count = state["consecutive"]
                classes = f"{len(state['seen_types'])}+" if state["class_overflow"] \
                    else str(len(state["seen_types"]))

        # Outside the lock: this module never logs while holding it.
        if emit:
            if verbose:
                # full traceback: first failure of the streak, or a class new to it
                logger.exception(
                    "job-reaper sweep failing: %d consecutive, %s fault class(es), "
                    "%d suppressed since last report: %s",
                    count, classes, suppressed, exc)
            else:
                logger.error(
                    "job-reaper sweep still failing: %d consecutive, %s fault class(es), "
                    "%d suppressed since last report, last %s: %s",
                    count, classes, suppressed, exc_key, exc)
        _warn_once_about_a_bad_throttle_value()
    except Exception:  # noqa: BLE001 - logging must never kill the reaper
        pass


def _warn_once_about_a_bad_throttle_value() -> None:
    """Say so, once, if REAPER_LOG_THROTTLE_S could not be used.

    Falling back to the default is the safe behaviour, but doing it silently hides
    an operator error: the env var reads as configured while having no effect.
    Called after _reaper_log_lock is released, because this module never logs
    while holding it.
    """
    global _reaper_throttle_bad_raw, _reaper_throttle_warned
    raw = _reaper_throttle_bad_raw
    if raw is None or _reaper_throttle_warned:
        return
    _reaper_throttle_warned = True
    _reaper_throttle_bad_raw = None
    logger.warning(
        "REAPER_LOG_THROTTLE_S=%r is not a finite, non-negative number; "
        "using the %.0fs default", raw, REAPER_LOG_THROTTLE_DEFAULT_S)


def _note_reaper_success() -> None:
    """Announce recovery exactly once after a failing streak; otherwise silent.

    Recovery is the other half of a throttled signal: without it, a streak that
    stops reporting is indistinguishable from a fault that quietly resolved. The
    reset also re-arms the full-traceback path for the next failure.

    The count reported is the whole streak across every fault class, so a mixed
    outage does not shrink to just its final class.
    """
    try:
        with _reaper_log_lock:
            state = _reaper_failure_state
            count = state["consecutive"]
            if not count:
                return  # the quiet, overwhelmingly common path
            last_type = state["last_type"]
            n_classes = len(state["seen_types"])
            overflow = state["class_overflow"]
            suppressed = state["suppressed"]
            state.update(
                consecutive=0, seen_types=set(), class_overflow=False,
                verbose_in_window=0, suppressed=0, last_type=None, last_logged_at=None)
        classes = f"{n_classes}+" if overflow else str(n_classes)
        if overflow or n_classes > 1:
            logger.warning(
                "job-reaper sweep recovered after %d consecutive failures across %s "
                "fault classes (%d never logged), last %s",
                count, classes, suppressed, last_type)
        else:
            logger.warning(
                "job-reaper sweep recovered after %d consecutive %s failures "
                "(%d never logged)", count, last_type, suppressed)
    except Exception:  # noqa: BLE001 - logging must never kill the reaper
        pass


def _reaper_sweep_once() -> None:
    """One reaper sweep, best-effort-wrapped. NEVER raises.

    A sweep failure LOGS and retries next interval; it never kills the daemon
    thread -- the same guarantee the guest-purge daemon documents
    (guest_uploads.start_purge_daemon). Before this logged, every sweep failure
    was discarded silently, so the reaper could fail on every interval
    indefinitely with nothing anywhere reporting it.

    The logging is RATE-LIMITED (_note_reaper_failure); the CONTROL FLOW is not.
    A failing sweep is still swallowed and still retried on the next interval
    whether or not this particular failure produced a log line.
    """
    try:
        _reap_orphans_once()
    except Exception as exc:  # noqa: BLE001 - daemon must survive
        _note_reaper_failure(exc)
    else:
        _note_reaper_success()


def _reaper_loop() -> None:
    while True:
        time.sleep(REAPER_INTERVAL_S)
        _reaper_sweep_once()


def ensure_started() -> None:
    """Idempotent: open the DB, start the lane executors + reaper daemon."""
    global _reaper_started
    if job_store_mode() == "postgres":
        _pg_store.ensure_ready()
    else:
        _db()
    executors_started = False
    # Check-and-set under _start_lock. Unsynchronized, two concurrent first
    # submits both read _executors as empty and _reaper_started as False, so each
    # builds its own lane pools (leaking a whole pool set) and each starts an
    # orphan-reaper daemon. _reaper_log_lock keeps _reaper_failure_state's own
    # updates atomic, but it cannot make two independent sweep schedules coherent:
    # duplicate reapers interleave their log lines and reset each other's failure
    # streak, so the log budget no longer describes one sweep history.
    with _start_lock:
        if not _executors:
            for lane, workers in lane_workers().items():
                _executors[lane] = ThreadPoolExecutor(
                    max_workers=workers, thread_name_prefix=f"jobworker-{lane}")
            executors_started = True
        if not _reaper_started:
            threading.Thread(target=_reaper_loop, daemon=True, name="job-reaper").start()
            _reaper_started = True
    if executors_started:
        # A submitted row can outlive a process before its executor starts. Scan
        # exactly once when this process creates its pools; scanning on every
        # submit would enqueue duplicate contenders (the lease would reject them,
        # but it would still waste executor capacity).
        if job_store_mode() == "postgres":
            submitted_ids = _pg_store.submitted_ids()
        else:
            submitted_ids = [
                row["job_id"]
                for row in _query("SELECT job_id FROM jobs WHERE status = 'submitted'")
            ]
        for job_id in submitted_ids:
            _redispatch_record(job_id)
