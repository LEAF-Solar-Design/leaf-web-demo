"""Best-effort linkage: mirror an async-spine run into a canonical platform Job row.

This is the piece that makes the Projects workspace non-hollow: when a run is
submitted WITH project context (the new optional ``X-Org-Id`` + ``X-Project-Id``
headers on ``POST /api/run``), the async spine records a canonical
``leaf_platform`` **Job** row (``kind="run"``, ``tool_name``, ``params``,
``spine_ref=<spine job_id>``) whose ``status`` tracks the run
(``queued → running → succeeded|failed``), with ``result`` = the §3 envelope on
success and ``cost_usd`` = the envelope's ``cost.usd_est``.

STRICTLY BEST-EFFORT + ENV-GATED. Every entry point is a no-op unless BOTH:

  * the caller supplied project context (both headers present), AND
  * a platform ``DATABASE_URL`` resolves (env or ``platform/.env.local``).

Any DB / import error logs **exactly one line** and NEVER propagates — a linkage
failure must never affect the run. So the DB-less demo (no headers OR no DB) is
BYTE-IDENTICAL to before (``server/tests/test_backbone.py`` depends on this): the
spine INSERT, execution, and HTTP bodies are untouched.

Correlation is in-process: ``on_submit`` records ``spine_job_id ->
(platform_job_id, org_id)``; the terminal transition looks it up and UPDATEs
status/result/cost, then drops the mapping. The row is CREATED via
``leaf_platform.store.create_job`` (which is org-scoped + ``spine_ref``-carrying);
the terminal UPDATE is issued here — ``store.py`` has no update path and is not
this lane's to edit — and is org-scoped (``WHERE org_id = ... AND job_id = ...``).

Imports the platform package under the ``leaf_platform`` alias, exactly as
``server/app.py`` does (the directory name ``platform/`` shadows the stdlib).
"""
from __future__ import annotations

import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

_lock = threading.Lock()
# spine_job_id -> {"platform_job_id": uuid.UUID, "org_id": uuid.UUID}
_MAP: Dict[str, Dict[str, Any]] = {}

_pkg: Optional[tuple] = None  # (store, db) once loaded

# spine terminal status -> platform JOB_STATUSES
_STATUS_MAP = {"complete": "succeeded", "failed": "failed"}


def _log(msg: str) -> None:
    print(f"[platform-link] {msg}", file=sys.stderr)


def _load_platform():
    """Load leaf_platform.{store,db} under the non-colliding alias (memoized).

    Raises on any import problem — callers wrap this. Never connects to the DB
    (the pool is lazy); import only pulls in psycopg/dataclasses.
    """
    global _pkg
    if _pkg is not None:
        return _pkg
    import importlib.util

    if "leaf_platform" not in sys.modules:
        pkg_dir = Path(__file__).resolve().parent.parent / "platform"
        spec = importlib.util.spec_from_file_location(
            "leaf_platform", pkg_dir / "__init__.py",
            submodule_search_locations=[str(pkg_dir)])
        mod = importlib.util.module_from_spec(spec)
        sys.modules["leaf_platform"] = mod
        spec.loader.exec_module(mod)
    import leaf_platform.db as db  # noqa: PLC0415
    import leaf_platform.store as store  # noqa: PLC0415

    _pkg = (store, db)
    return _pkg


def _db_configured() -> bool:
    """True iff the platform package imports AND a DATABASE_URL resolves (env or
    platform/.env.local). Never raises; a False here is the silent no-op path
    (the DB-less demo) — no per-run logging."""
    try:
        _store, db = _load_platform()
        db.get_database_url()  # RuntimeError if unset
        return True
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# entry points (all no-op-safe; called from server/jobs.py)
# --------------------------------------------------------------------------- #
def on_submit(spine_job_id: str, org_id: Optional[str], project_id: Optional[str],
              tool_name: Optional[str], params: Optional[Dict[str, Any]]) -> None:
    """Record a canonical platform Job for a run submitted WITH project context.

    No-op unless both headers are present AND the DB resolves. On success the
    spine_job_id -> platform-job mapping is stored so the terminal sync can find
    it. Any error logs one line and is swallowed — the run is never affected.
    """
    if not org_id or not project_id:
        return  # no project context -> byte-identical legacy path
    if not _db_configured():
        return  # no DB -> byte-identical legacy path (silent)
    try:
        store, _db = _load_platform()
        oid = uuid.UUID(str(org_id))
        pid = uuid.UUID(str(project_id))
        job = store.create_job(
            oid, pid, "run",
            tool_name=tool_name,
            params=dict(params or {}),
            spine_ref=str(spine_job_id),
        )
        with _lock:
            _MAP[str(spine_job_id)] = {"platform_job_id": job.job_id, "org_id": oid}
    except Exception as exc:  # noqa: BLE001
        _log(f"submit link failed (run unaffected): {type(exc).__name__}: {exc}")


def on_running(spine_job_id: str) -> None:
    """Flip the linked platform Job to 'running'. No-op if unlinked."""
    entry = _get(spine_job_id)
    if entry is None:
        return
    try:
        _update(entry, status="running")
    except Exception as exc:  # noqa: BLE001
        _log(f"running link failed (run unaffected): {type(exc).__name__}: {exc}")


def on_terminal(spine_job_id: str, spine_status: str,
                result_env: Optional[Dict[str, Any]] = None,
                error: Optional[Dict[str, Any]] = None) -> None:
    """Sync the linked platform Job to its terminal state and drop the mapping.

    succeeded/failed from the spine's complete/failed; on success ``result`` = the
    §3 envelope and ``cost_usd`` = ``result_env['cost']['usd_est']`` (may be null
    for a mock run). No-op if unlinked. Never raises.
    """
    entry = _pop(spine_job_id)
    if entry is None:
        return
    try:
        status = _STATUS_MAP.get(spine_status, "failed")
        result_json = None
        cost_usd = None
        if status == "succeeded" and isinstance(result_env, dict):
            result_json = result_env
            cost = result_env.get("cost")
            if isinstance(cost, dict):
                usd = cost.get("usd_est")
                if isinstance(usd, (int, float)):
                    cost_usd = float(usd)
        _update(entry, status=status, result=result_json, cost_usd=cost_usd)
    except Exception as exc:  # noqa: BLE001
        _log(f"terminal link failed (run unaffected): {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# internals
# --------------------------------------------------------------------------- #
def _get(spine_job_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        e = _MAP.get(str(spine_job_id))
        return dict(e) if e else None


def _pop(spine_job_id: str) -> Optional[Dict[str, Any]]:
    with _lock:
        return _MAP.pop(str(spine_job_id), None)


def _update(entry: Dict[str, Any], *, status: str,
            result: Optional[Dict[str, Any]] = None,
            cost_usd: Optional[float] = None) -> None:
    """Issue the org-scoped terminal/running UPDATE via the platform pool. The
    ``result``/``cost_usd`` columns are only touched on a terminal sync (they are
    passed None for the 'running' flip and left as-is by COALESCE-free assignment
    only when provided)."""
    from psycopg.types.json import Jsonb  # noqa: PLC0415

    _store, db = _load_platform()
    sets = ["status = %(status)s", "updated_at = NOW()"]
    args: Dict[str, Any] = {"status": status,
                            "org_id": entry["org_id"],
                            "job_id": entry["platform_job_id"]}
    if result is not None:
        sets.append("result = %(result)s")
        args["result"] = Jsonb(result)
    if cost_usd is not None:
        sets.append("cost_usd = %(cost_usd)s")
        args["cost_usd"] = cost_usd
    sql = ("UPDATE jobs SET " + ", ".join(sets)
           + " WHERE org_id = %(org_id)s AND job_id = %(job_id)s")
    with db.cursor() as cur:
        cur.execute(sql, args)
