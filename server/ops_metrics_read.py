"""Read-model for the APS observability ops surface — the leaf-platform half of
the APS observability plane (peer contract: C:/tmp/aps-observability/CONTRACT.md).

Division of labor with the unified-observability plane:
  * That plane owns the EMF metric emit + CloudWatch alarms/dashboards — real-time
    fleet trends, retention, and alerting.
  * THIS read-model owns the two things CloudWatch/EMF are poor at:
      1. the live in-flight job tail (per-job lease/heartbeat/progress), and
      2. billing-grade drill-down against the DURABLE Postgres attribution ledger
         (`broker_usage_ledger`), joined to the job spine (`async_jobs`).

READ-ONLY. It reuses the same `leaf_platform.db` pool the write-side stores use;
it never writes and never touches the frozen 9-key ledger contract. Postgres
authority only (`LEAF_BROKER_STORE=postgres`); the router gates on that upstream.

Correlation. `broker.py` writes exactly one `broker_usage_ledger` row per run,
keyed by `event_key = "{job_id}:broker-run"` (and `"{job_id}:broker-fallback"` on
a local fallback), so ONE job can own MULTIPLE ledger rows. There is no shared id
column, so the join is `async_jobs.job_id = split_part(event_key, ':', 1)`
(1-to-many). A ledger row whose prefix matches no job (e.g. a non-job broker call)
is reported with `job_correlated=false` — never a fabricated join.

Timestamps. `broker_usage_ledger.ts` and every `async_jobs.*_at` are
DOUBLE PRECISION UNIX epoch seconds, so ages are `EXTRACT(EPOCH FROM NOW()) - col`.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

_DAY_SECONDS = 86_400.0


def _db():
    """Return the shared ``leaf_platform.db`` module — the SAME pool the pg
    stores use. Loader mirrors job_pg_store/broker_pg_store so it never depends
    on sys.path order."""
    import importlib.util
    import sys
    from pathlib import Path

    loaded = sys.modules.get("leaf_platform")
    if loaded is None:
        root = Path(__file__).resolve().parent.parent
        package = root / "platform"
        spec = importlib.util.spec_from_file_location(
            "leaf_platform", package / "__init__.py",
            submodule_search_locations=[str(package)],
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load the Leaf platform database package")
        loaded = importlib.util.module_from_spec(spec)
        sys.modules["leaf_platform"] = loaded
        spec.loader.exec_module(loaded)
    from leaf_platform import db
    return db


def _round_opt(value: Any, ndigits: int = 3) -> Optional[float]:
    return round(float(value), ndigits) if value is not None else None


# --------------------------------------------------------------------------- #
# fleet / tenant rollup (broker_usage_ledger)
# --------------------------------------------------------------------------- #
# Denials are policy rejections, not executions. Match the billing authority
# EXACTLY (broker_pg_store.aggregate_usage / spent_usd, casing included) so cost
# and "runs" here reconcile with /api/usage. A chargeable FAILED WorkItem (e.g.
# WORKITEM_FAILED carrying a cost block) IS counted; only these two denial
# statuses are excluded from runs + cost.
_EXECUTED = "status NOT IN ('quota_exceeded', 'TENANT_DISABLED')"


def _agg_row(conn, since: float, tenant_id: Optional[str]) -> Dict[str, Any]:
    """One aggregate pass over the ledger for ts >= since (optionally one tenant).
    FILTER-based so a single scan yields counts, cost, and engine percentiles.
    ``runs``/``usd_est`` exclude denials to match the billing authority; ``attempts``
    keeps the raw total (incl. denials) for ops volume."""
    where = "WHERE ts >= %(since)s"
    params: Dict[str, Any] = {"since": float(since)}
    if tenant_id:
        where += " AND tenant_id = %(tenant_id)s"
        params["tenant_id"] = str(tenant_id)
    row = conn.execute(
        f"""
        SELECT
          COUNT(*)                                              AS attempts,
          COUNT(*) FILTER (WHERE {_EXECUTED})                   AS runs,
          COUNT(*) FILTER (WHERE NOT ({_EXECUTED}))             AS denied,
          COUNT(*) FILTER (WHERE aps_live AND {_EXECUTED})      AS live_runs,
          COUNT(*) FILTER (WHERE status = 'ok')                 AS ok_runs,
          COUNT(*) FILTER (WHERE {_EXECUTED} AND status <> 'ok') AS error_runs,
          COALESCE(SUM(usd_est) FILTER (
              WHERE usd_est IS NOT NULL AND {_EXECUTED}), 0)    AS usd_est,
          COALESCE(SUM(engine_seconds) FILTER (
              WHERE engine_seconds IS NOT NULL), 0)             AS engine_seconds_sum,
          PERCENTILE_CONT(0.50) WITHIN GROUP (ORDER BY engine_seconds)
              FILTER (WHERE engine_seconds IS NOT NULL)         AS p50,
          PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY engine_seconds)
              FILTER (WHERE engine_seconds IS NOT NULL)         AS p95,
          PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY engine_seconds)
              FILTER (WHERE engine_seconds IS NOT NULL)         AS p99,
          MAX(ts)                                               AS last_ts
        FROM broker_usage_ledger
        {where}
        """,
        params,
    ).fetchone()
    return dict(row) if row else {}


def _error_taxonomy(conn, since: float, tenant_id: Optional[str]) -> List[Dict[str, Any]]:
    where = "WHERE ts >= %(since)s AND status <> 'ok'"
    params: Dict[str, Any] = {"since": float(since)}
    if tenant_id:
        where += " AND tenant_id = %(tenant_id)s"
        params["tenant_id"] = str(tenant_id)
    rows = conn.execute(
        f"SELECT status, COUNT(*) AS n FROM broker_usage_ledger {where} "
        "GROUP BY status ORDER BY n DESC, status",
        params,
    ).fetchall()
    return [{"status": r["status"], "count": int(r["n"])} for r in rows]


def fleet_metrics(window_seconds: int = 86_400,
                  tenant_id: Optional[str] = None) -> Dict[str, Any]:
    """Throughput / reliability / cost / engine-latency rollup over a trailing
    window (default 24h), plus a today (UTC-day) split. ``tenant_id`` scopes to
    one tenant; omit for the fleet view."""
    now = time.time()
    window_seconds = max(60, min(int(window_seconds), 30 * int(_DAY_SECONDS)))
    since = now - window_seconds
    today_start = now - (now % _DAY_SECONDS)  # epoch is UTC, so this is UTC midnight
    db = _db()
    with db.get_pool().connection() as conn:
        window = _agg_row(conn, since, tenant_id)
        today = _agg_row(conn, today_start, tenant_id)
        taxonomy = _error_taxonomy(conn, since, tenant_id)

    runs = int(window.get("runs") or 0)  # executed (denials excluded), billing-consistent
    ok_runs = int(window.get("ok_runs") or 0)
    live_runs = int(window.get("live_runs") or 0)
    return {
        "scope": tenant_id or "fleet",
        "window_seconds": window_seconds,
        "generated_at": now,
        "throughput": {
            "attempts": int(window.get("attempts") or 0),  # all broker calls incl. denials
            "runs": runs,                                   # executed only
            "denied": int(window.get("denied") or 0),       # quota / tenant-disabled
            "live_runs": live_runs,
            "mock_runs": runs - live_runs,
            "today_runs": int(today.get("runs") or 0),
        },
        "reliability": {
            "ok_runs": ok_runs,
            "error_runs": int(window.get("error_runs") or 0),  # executed and not ok
            "success_rate": round(ok_runs / runs, 4) if runs else None,  # over executed
            "error_taxonomy": taxonomy,
        },
        "cost": {
            "usd_window": round(float(window.get("usd_est") or 0.0), 6),
            "usd_today": round(float(today.get("usd_est") or 0.0), 6),
        },
        "engine_seconds": {
            "sum": round(float(window.get("engine_seconds_sum") or 0.0), 3),
            "p50": _round_opt(window.get("p50")),
            "p95": _round_opt(window.get("p95")),
            "p99": _round_opt(window.get("p99")),
        },
        "freshness": {"last_run_ts": _round_opt(window.get("last_ts"), 3)},
    }


# --------------------------------------------------------------------------- #
# live in-flight job tail (async_jobs)
# --------------------------------------------------------------------------- #
def inflight(limit: int = 100, tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """In-flight jobs (status submitted/running), OLDEST first — the stalest
    in-flight job (closest to the reaper's stale window) surfaces at the top,
    matching broker list_executing. Ages are seconds-since. Uses the
    `async_jobs_reclaim_idx` partial index."""
    limit = max(1, min(int(limit), 500))
    where = "WHERE status IN ('submitted', 'running')"
    params: Dict[str, Any] = {"limit": limit}
    if tenant_id:
        where += " AND tenant_id = %(tenant_id)s"
        params["tenant_id"] = str(tenant_id)
    db = _db()
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            f"""
            SELECT job_id, tenant_id, tool, status, progress, attempt,
                   lease_owner, provenance_json,
                   EXTRACT(EPOCH FROM NOW()) - created_at              AS age_seconds,
                   CASE WHEN heartbeat_at IS NULL THEN NULL
                        ELSE EXTRACT(EPOCH FROM NOW()) - heartbeat_at
                   END                                                 AS heartbeat_age_seconds
            FROM async_jobs
            {where}
            ORDER BY created_at ASC
            LIMIT %(limit)s
            """,
            params,
        ).fetchall()

    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        prov = d.get("provenance_json")
        exec_path = prov.get("execution_path") if isinstance(prov, dict) else None
        out.append({
            "job_id": d["job_id"],
            "tenant_id": d["tenant_id"],
            "tool": d["tool"],
            "status": d["status"],
            "progress": d["progress"],
            "attempt": int(d["attempt"] or 0),
            "execution_path": exec_path,
            "lease_owner": d["lease_owner"],
            "age_seconds": _round_opt(d["age_seconds"], 1),
            "heartbeat_age_seconds": _round_opt(d["heartbeat_age_seconds"], 1),
        })
    return out


# --------------------------------------------------------------------------- #
# billing-grade drill-down (broker_usage_ledger <- async_jobs)
# --------------------------------------------------------------------------- #
def run_drilldown(limit: int = 50, tenant_id: Optional[str] = None,
                  status: Optional[str] = None,
                  tool: Optional[str] = None) -> List[Dict[str, Any]]:
    """Recent ledger rows (the durable, billing-grade attribution record), each
    LEFT-JOINed to its owning job on the event_key prefix. `job_correlated` is
    false when no job owns the run (correlation genuinely unavailable)."""
    limit = max(1, min(int(limit), 500))
    clauses: List[str] = []
    params: Dict[str, Any] = {"limit": limit}
    if tenant_id:
        clauses.append("l.tenant_id = %(tenant_id)s")
        params["tenant_id"] = str(tenant_id)
    if status:
        clauses.append("l.status = %(status)s")
        params["status"] = str(status)
    if tool:
        clauses.append("l.tool = %(tool)s")
        params["tool"] = str(tool)
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    db = _db()
    with db.get_pool().connection() as conn:
        rows = conn.execute(
            f"""
            SELECT l.event_key, l.ts, l.tenant_id, l.tool, l.engine_op,
                   l.aps_live, l.engine_seconds, l.usd_est, l.status,
                   j.job_id   AS job_id,
                   j.status   AS job_status,
                   j.progress AS job_progress,
                   j.attempt  AS job_attempt
            FROM broker_usage_ledger l
            LEFT JOIN async_jobs j
                   ON j.job_id = split_part(l.event_key, ':', 1)
                  AND j.tenant_id = l.tenant_id
            {where}
            ORDER BY l.ts DESC
            LIMIT %(limit)s
            """,
            params,
        ).fetchall()

    out: List[Dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        correlated = d["job_id"] is not None
        out.append({
            "event_key": d["event_key"],
            "ts": _round_opt(d["ts"], 3),
            "tenant_id": d["tenant_id"],
            "tool": d["tool"],
            "engine_op": d["engine_op"],
            "aps_live": bool(d["aps_live"]),
            "engine_seconds": _round_opt(d["engine_seconds"], 3),
            "usd_est": _round_opt(d["usd_est"], 6),
            "status": d["status"],
            "job_correlated": correlated,
            "job": ({
                "job_id": d["job_id"],
                "status": d["job_status"],
                "progress": d["job_progress"],
                "attempt": int(d["job_attempt"] or 0),
            } if correlated else None),
        })
    return out
