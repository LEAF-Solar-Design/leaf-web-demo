"""PostgreSQL authority for the application async-job delivery spine."""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional, Tuple


REQUIRED_COLUMNS = {
    "job_id", "tenant_id", "tool", "params_json", "dwg", "status",
    "created_at", "updated_at", "execution_json", "attempt", "lease_owner",
    "lease_expires_at", "terminal_fingerprint", "submission_fingerprint",
}


def _db():
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


def _json(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list, int, float, bool)):
        return value
    return json.loads(value)


def _record(row: Dict[str, Any]) -> Dict[str, Any]:
    rec = {
        "job_id": row["job_id"],
        "tenant_id": row["tenant_id"],
        "tool": row["tool"],
        "params": _json(row["params_json"]) or {},
        "dwg": row["dwg"],
        "status": row["status"],
        "progress": row["progress"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "updated_at": row["updated_at"],
        "finished_at": row["finished_at"],
        "elapsed_ms": row["elapsed_ms"],
        "result": _json(row["result_json"]),
        "error": _json(row["error_json"]),
        "attempt": int(row["attempt"] or 0),
        "lease": (
            {
                "owner": row["lease_owner"],
                "expires_at": row["lease_expires_at"],
                "heartbeat_at": row["heartbeat_at"],
            }
            if row["lease_owner"] else None
        ),
        "provenance": _json(row["provenance_json"]),
        "org_id": row["org_id"],
        "project_id": row["project_id"],
        "authority_mode": row["authority_mode"],
        "idempotency_key": row["idempotency_key"],
        "dwg_version": row["dwg_version"],
    }
    from campaign_capability_job import record_context
    capability = record_context(_json(row.get("execution_json")) or {}, rec)
    if capability is not None:
        rec["capability_provenance"] = capability
    from campaign_transform_job import record_context as completion_record_context
    completion = completion_record_context(_json(row.get("execution_json")) or {}, rec)
    if completion is not None:
        rec["completion_provenance"] = completion
    return rec


class PostgresJobStore:
    def linked_capability_job(self, canonical_id: str, tenant_id: str):
        """Resolve mirror linkage from stored rows, never a client spine selector."""
        with _db().cursor() as cur:
            cur.execute(
                "SELECT a.* FROM jobs j JOIN async_jobs a ON a.job_id = j.spine_ref "
                "WHERE j.job_id::text = %s AND a.tenant_id = %s "
                "AND (a.execution_json ? 'capability_provenance' OR a.execution_json ? 'completion_provenance')",
                (canonical_id, tenant_id),
            )
            row = cur.fetchone()
        return _record(row) if row is not None else None

    def ensure_ready(self) -> None:
        db = _db()
        with db.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = 'async_jobs'"
            )
            found = {row["column_name"] for row in cur.fetchall()}
            cur.execute(
                "SELECT to_regclass('async_job_terminal_conflicts') AS conflicts"
            )
            conflicts = cur.fetchone()
        missing = REQUIRED_COLUMNS - found
        if missing or not conflicts or conflicts["conflicts"] is None:
            detail = ", ".join(sorted(missing)) if missing else "terminal conflict table"
            raise RuntimeError(
                "PostgreSQL async job schema is unavailable; apply "
                "0011_jobs_callbacks.sql (missing: " + detail + ")"
            )

    def submit(self, row: Dict[str, Any]) -> Tuple[str, bool]:
        db = _db()
        with db.transaction() as conn:
            if row["project_id"] is not None and row["idempotency_key"] is not None:
                existing = conn.execute(
                    "SELECT job_id, submission_fingerprint FROM async_jobs "
                    "WHERE tenant_id = %s AND project_id = %s AND idempotency_key = %s "
                    "FOR UPDATE",
                    (row["tenant_id"], row["project_id"], row["idempotency_key"]),
                ).fetchone()
                if existing is not None:
                    if existing["submission_fingerprint"] != row["submission_fingerprint"]:
                        raise ValueError(
                            "idempotency key already exists with different run input")
                    return str(existing["job_id"]), False
            inserted = conn.execute(
                """
                INSERT INTO async_jobs
                  (job_id, tenant_id, tool, params_json, dwg, status, progress,
                   created_at, updated_at, execution_json, org_id, project_id,
                   authority_mode, idempotency_key, submission_fingerprint, dwg_version)
                VALUES
                  (%(job_id)s, %(tenant_id)s, %(tool)s, %(params)s::jsonb, %(dwg)s,
                   'submitted', 'queued', %(created_at)s, %(created_at)s,
                   %(execution)s::jsonb, %(org_id)s, %(project_id)s,
                   %(authority_mode)s, %(idempotency_key)s,
                   %(submission_fingerprint)s, %(dwg_version)s)
                ON CONFLICT (tenant_id, project_id, idempotency_key)
                  WHERE project_id IS NOT NULL AND idempotency_key IS NOT NULL
                DO NOTHING
                RETURNING job_id
                """,
                row,
            ).fetchone()
            if inserted is not None:
                return str(inserted["job_id"]), True
            existing = conn.execute(
                "SELECT job_id, submission_fingerprint FROM async_jobs "
                "WHERE tenant_id = %s AND project_id = %s AND idempotency_key = %s",
                (row["tenant_id"], row["project_id"], row["idempotency_key"]),
            ).fetchone()
            if (
                existing is None
                or existing["submission_fingerprint"] != row["submission_fingerprint"]
            ):
                raise ValueError("idempotency key already exists with different run input")
            return str(existing["job_id"]), False

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        db = _db()
        with db.cursor() as cur:
            cur.execute("SELECT * FROM async_jobs WHERE job_id = %s", (job_id,))
            row = cur.fetchone()
        return _record(row) if row else None

    def list(self, tenant_id: Optional[str], limit: int) -> List[Dict[str, Any]]:
        db = _db()
        with db.cursor() as cur:
            if tenant_id:
                cur.execute(
                    "SELECT * FROM async_jobs WHERE tenant_id = %s "
                    "ORDER BY created_at DESC LIMIT %s",
                    (tenant_id, limit),
                )
            else:
                cur.execute(
                    "SELECT * FROM async_jobs ORDER BY created_at DESC LIMIT %s",
                    (limit,),
                )
            rows = cur.fetchall()
        return [_record(row) for row in rows]

    def claim(
        self, job_id: str, worker_id: str, stamp: float, expires: float,
        max_attempts: int,
    ) -> Optional[int]:
        db = _db()
        with db.transaction() as conn:
            row = conn.execute(
                """
                UPDATE async_jobs
                SET status = 'running', progress = 'running',
                    started_at = COALESCE(started_at, %s), updated_at = %s,
                    attempt = attempt + 1, lease_owner = %s,
                    lease_expires_at = %s, heartbeat_at = %s
                WHERE job_id = %s
                  AND progress <> 'closed'
                  AND (status = 'submitted' OR
                       (status = 'running' AND
                        (lease_expires_at IS NULL OR lease_expires_at <= %s)))
                  AND attempt < %s
                RETURNING attempt
                """,
                (stamp, stamp, worker_id, expires, stamp, job_id, stamp, max_attempts),
            ).fetchone()
        return int(row["attempt"]) if row else None

    def heartbeat(
        self, job_id: str, worker_id: str, now: float, expires: float,
        progress: Optional[str],
    ) -> bool:
        db = _db()
        with db.transaction() as conn:
            row = conn.execute(
                """
                UPDATE async_jobs
                SET updated_at = %s, lease_expires_at = %s, heartbeat_at = %s,
                    progress = COALESCE(%s, progress)
                WHERE job_id = %s AND status = 'running'
                  AND lease_owner = %s AND lease_expires_at >= %s
                RETURNING job_id
                """,
                (now, expires, now, progress, job_id, worker_id, now),
            ).fetchone()
        return row is not None

    def release_for_retry(
        self, job_id: str, worker_id: str, attempt: int, now: float,
        error: Dict[str, Any], provenance: Dict[str, Any],
    ) -> bool:
        """Release only the current live attempt back to the submitted queue."""
        db = _db()
        with db.transaction() as conn:
            row = conn.execute(
                """
                UPDATE async_jobs
                SET status = 'submitted', progress = 'retrying', updated_at = %s,
                    error_json = %s::jsonb, provenance_json = %s::jsonb,
                    lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL
                WHERE job_id = %s AND status = 'running' AND attempt = %s
                  AND lease_owner = %s AND lease_expires_at >= %s
                  AND progress <> 'closed'
                RETURNING job_id
                """,
                (
                    now, json.dumps(error), json.dumps(provenance), job_id,
                    attempt, worker_id, now,
                ),
            ).fetchone()
        return row is not None

    def durable_context(self, job_id: str) -> Optional[Dict[str, Any]]:
        db = _db()
        with db.cursor() as cur:
            cur.execute(
                "SELECT attempt, execution_json, params_json, tenant_id, org_id, project_id, tool "
                "FROM async_jobs WHERE job_id = %s",
                (job_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {"attempt": int(row["attempt"] or 0), "execution": _json(row["execution_json"]) or {},
                "params": _json(row.get("params_json")) or {},
                **{key: row[key] for key in ("tenant_id", "org_id", "project_id", "tool")}}

    def event_context(self, job_id: str) -> Optional[Dict[str, Any]]:
        """Identity/attribution fields for the terminal product event (P2
        telemetry). Read-only, one row; the caller treats any failure as
        best-effort absence."""
        db = _db()
        with db.cursor() as cur:
            cur.execute(
                "SELECT tenant_id, tool, elapsed_ms FROM async_jobs WHERE job_id = %s",
                (job_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return {
            "tenant_id": row["tenant_id"],
            "tool": row["tool"],
            "elapsed_ms": row["elapsed_ms"],
        }

    def complete(
        self, job_id: str, durable_attempt: int, status: str, result: Any,
        error: Any, provenance: Any, fingerprint: str, worker_id: Optional[str],
        now: float, *, allow_closed: bool = False,
    ) -> str:
        db = _db()
        with db.transaction() as conn:
            row = conn.execute(
                """
                UPDATE async_jobs
                SET status = %(status)s,
                    progress = CASE WHEN %(status)s = 'complete' THEN 'done' ELSE 'error' END,
                    updated_at = %(now)s, finished_at = %(now)s,
                    elapsed_ms = CAST((%(now)s - COALESCE(started_at, %(now)s)) * 1000 AS BIGINT),
                    result_json = %(result)s::jsonb, error_json = %(error)s::jsonb,
                    provenance_json = %(provenance)s::jsonb,
                    terminal_fingerprint = %(fingerprint)s,
                    lease_owner = NULL, lease_expires_at = NULL
                WHERE job_id = %(job_id)s AND attempt = %(attempt)s
                  AND status NOT IN ('complete', 'failed')
                  AND (%(allow_closed)s OR progress <> 'closed')
                  AND (%(worker_id)s::text IS NULL OR
                       (lease_owner = %(worker_id)s AND lease_expires_at >= %(now)s))
                RETURNING job_id
                """,
                {
                    "status": status, "now": now,
                    "result": json.dumps(result) if result is not None else None,
                    "error": json.dumps(error) if error is not None else None,
                    "provenance": (
                        json.dumps(provenance) if provenance is not None else None),
                    "fingerprint": fingerprint, "job_id": job_id,
                    "attempt": durable_attempt, "worker_id": worker_id,
                    "allow_closed": allow_closed,
                },
            ).fetchone()
            if row is not None:
                # The linked platform Job is synced HERE, on this same connection, so
                # the authority row and its mirror commit or roll back together. Doing
                # it after this transaction closed is what left a caller polling a
                # nonterminal platform Job forever when the mirror write failed.
                import platform_link  # noqa: PLC0415  (late: avoids an import cycle)
                platform_link.terminal_in_transaction(
                    conn, job_id, status, result, error)
                return "applied"
            current = conn.execute(
                "SELECT status, terminal_fingerprint FROM async_jobs WHERE job_id = %s "
                "FOR UPDATE",
                (job_id,),
            ).fetchone()
            if current is None:
                return "missing"
            if current["status"] not in {"complete", "failed"}:
                return "not_owner"
            if current["terminal_fingerprint"] == fingerprint:
                return "duplicate"
            conflict = {
                "status": status, "result": result, "error": error,
                "provenance": provenance, "received_at": now,
            }
            conn.execute(
                "INSERT INTO async_job_terminal_conflicts "
                "(job_id, fingerprint, evidence_json, received_at) "
                "VALUES (%s, %s, %s::jsonb, %s) ON CONFLICT DO NOTHING",
                (job_id, fingerprint, json.dumps(conflict), now),
            )
            conn.execute(
                "UPDATE async_jobs SET terminal_conflict_json = %s::jsonb, updated_at = %s "
                "WHERE job_id = %s",
                (json.dumps(conflict), now, job_id),
            )
            return "conflict"

    def execution(self, job_id: str) -> Optional[Dict[str, Any]]:
        context = self.durable_context(job_id)
        return context["execution"] if context else None

    def reclaimable(self, stale_before: float, closed: str, now: float) -> List[Dict[str, Any]]:
        db = _db()
        with db.cursor() as cur:
            cur.execute(
                "SELECT job_id, progress FROM async_jobs "
                "WHERE status IN ('submitted','running') AND "
                "(updated_at < %s OR progress = %s OR "
                "(lease_expires_at IS NOT NULL AND lease_expires_at < %s))",
                (stale_before, closed, now),
            )
            return list(cur.fetchall())

    def mark_closed(self, job_id: str, closed: str, now: float) -> bool:
        db = _db()
        with db.transaction() as conn:
            row = conn.execute(
                "UPDATE async_jobs SET progress = %s, updated_at = %s, "
                "lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL "
                "WHERE job_id = %s AND status IN ('submitted','running') "
                "RETURNING job_id",
                (closed, now, job_id),
            ).fetchone()
        return row is not None

    def close_tenant(self, tenant_id: str, closed: str, now: float) -> int:
        db = _db()
        with db.transaction() as conn:
            rows = conn.execute(
                "UPDATE async_jobs SET progress = %s, updated_at = %s, "
                "lease_owner = NULL, lease_expires_at = NULL, heartbeat_at = NULL "
                "WHERE tenant_id = %s AND status IN ('submitted','running') "
                "RETURNING job_id",
                (closed, now, tenant_id),
            ).fetchall()
        return len(rows)

    def orphan_leases(
        self, stale_before: float, closed: str, tenant_id: Optional[str],
    ) -> List[Dict[str, Any]]:
        db = _db()
        with db.cursor() as cur:
            cur.execute(
                "SELECT job_id, tenant_id, progress FROM async_jobs "
                "WHERE status IN ('submitted','running') "
                "AND (updated_at < %(stale)s OR progress = %(closed)s) "
                "AND (%(tenant)s IS NULL OR tenant_id = %(tenant)s)",
                {"stale": stale_before, "closed": closed, "tenant": tenant_id},
            )
            return list(cur.fetchall())

    def submitted_ids(self) -> List[str]:
        db = _db()
        with db.cursor() as cur:
            cur.execute("SELECT job_id FROM async_jobs WHERE status = 'submitted'")
            return [str(row["job_id"]) for row in cur.fetchall()]


class PostgresCallbackReplayStore:
    def ensure_ready(self) -> None:
        db = _db()
        with db.cursor() as cur:
            cur.execute("SELECT to_regclass('callback_consumed_nonces') AS table_name")
            row = cur.fetchone()
        if not row or row["table_name"] is None:
            raise RuntimeError(
                "PostgreSQL callback replay schema is unavailable; apply "
                "0011_jobs_callbacks.sql"
            )

    def consume(self, job_id: str, nonce: str, expires_at: float, now: float) -> bool:
        db = _db()
        with db.transaction() as conn:
            conn.execute(
                "DELETE FROM callback_consumed_nonces WHERE expires_at < %s", (now,))
            row = conn.execute(
                "INSERT INTO callback_consumed_nonces "
                "(job_id, nonce, expires_at, consumed_at) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (job_id, nonce) DO NOTHING RETURNING nonce",
                (job_id, nonce, expires_at, now),
            ).fetchone()
        return row is not None
