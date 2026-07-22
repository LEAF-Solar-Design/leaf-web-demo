"""Durable PostgreSQL lease/attempt worker and atomic solve completion."""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from psycopg.types.json import Jsonb

from . import entitlements
from .db import connection
from .models import canonical_hash, new_uuid
from .store import _insert_outbox

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_TERMINAL = {"succeeded", "failed", "cancelled"}
_SNAPSHOT_KINDS = ("catalog", "standards", "ahj")


def _now(value: Optional[datetime]) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("worker timestamps must be timezone-aware")
    return current


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _fingerprint(domain: str, value: Any) -> str:
    return hashlib.sha256((domain + "\x00" + _canonical_json(value)).encode("utf-8")).hexdigest()


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime,)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    return value


def _record(row: Any) -> Dict[str, Any]:
    if row is None:
        return None
    return {key: _json_value(value) for key, value in dict(row).items()}


def _job_columns() -> str:
    return (
        "job_id, project_id, org_id, request_tenant_id, kind, tool_name, status, params, "
        "result, error, input_version_id, output_version_id, attempt, max_attempts, "
        "lease_owner, lease_expires_at, heartbeat_at, provenance, terminal_fingerprint, "
        "terminal_conflict, execution_context, created_at, updated_at, started_at, finished_at"
    )


def _resolve_snapshot_pins(cur: Any, channel: str = "local-candidate") -> Dict[str, Dict[str, str]]:
    cur.execute(
        "SELECT s.snapshot_kind, s.snapshot_id, s.content_sha256, s.source_sha256, "
        "s.review_state FROM snapshot_channels c JOIN platform_snapshots s "
        "ON s.snapshot_id = c.snapshot_id AND s.snapshot_kind = c.snapshot_kind "
        "WHERE c.channel = %(channel)s ORDER BY s.snapshot_kind", {"channel": channel})
    pins = {
        row["snapshot_kind"]: {
            "snapshot_id": str(row["snapshot_id"]),
            "content_sha256": row["content_sha256"],
            "source_sha256": row["source_sha256"],
            "review_state": row["review_state"],
        }
        for row in cur.fetchall()
    }
    missing = set(_SNAPSHOT_KINDS) - set(pins)
    if missing:
        raise ValueError(f"snapshot channel {channel!r} is missing {sorted(missing)}")
    return pins


def submit_solve_job(org_id: uuid.UUID, project_id: uuid.UUID, request_tenant_id: str,
                     tool_name: str, params: Dict[str, Any], idempotency_key: Optional[str], *,
                     input_version_id: Optional[uuid.UUID] = None,
                     max_attempts: int = 3) -> Dict[str, Any]:
    if not request_tenant_id or not tool_name:
        raise ValueError("request tenant and tool name are required")
    if not idempotency_key:
        raise ValueError("Idempotency-Key is required for project-scoped runs")
    if not isinstance(params, dict):
        raise ValueError("job params must be an object")
    if not isinstance(max_attempts, int) or isinstance(max_attempts, bool) or max_attempts < 1:
        raise ValueError("max_attempts must be a positive integer")
    submission = {
        "orgId": str(org_id), "projectId": str(project_id),
        "requestTenantId": str(request_tenant_id), "toolName": tool_name,
        "params": params, "inputVersionId": str(input_version_id) if input_version_id else None,
        "maxAttempts": max_attempts,
    }
    fingerprint = _fingerprint("canonical-job-submission", submission)

    # IDEMPOTENT REPLAY FIRST: an already-accepted key returns its original
    # job even if the org's tier has since been downgraded — the job exists;
    # denying the lookup would only break client retry semantics, never
    # un-create it. Entitlement gates NEW submissions only. (The identical
    # in-transaction lookup below still covers the two-concurrent-submits
    # race on the same key.)
    if idempotency_key:
        with connection() as conn:
            with conn.cursor() as cur:
                replay = _existing_by_idempotency_key(cur, org_id, project_id, idempotency_key)
                if replay is not None:
                    if replay["submission_fingerprint"] != fingerprint:
                        raise ValueError("idempotency key already exists with different run input")
                    return _record(replay)

    # ENTITLEMENT (P1 floor): the STORED org's tier/status decides, here at the
    # single choke point every canonical submission crosses (the POST /api/run
    # spine path included) — the request-side JWT/demo tier gate upstream is not
    # this check and cannot substitute for it. Fail closed; the denial carries
    # the documented HTTP envelope up through the typed exception. The evaluated
    # tier is PINNED into the INSERT below so a concurrent downgrade between
    # this read and the insert cannot slip a job through (TOCTOU guard).
    denial, org = entitlements.stored_job_entitlement_verdict(org_id, "solve")
    if denial is not None:
        raise entitlements.EntitlementDenied(denial)
    pinned_tier = org.tier if org is not None else None

    with connection() as conn:
        with conn.cursor() as cur:
            snapshot_pins = _resolve_snapshot_pins(cur)
            execution_context = {
                "authority_mode": "postgres_canonical",
                "execution_path": "local",
                "snapshot_channel": "local-candidate",
                "snapshot_pins": snapshot_pins,
                "capability_state": "connected_degraded",
            }
            cur.execute(
                "INSERT INTO jobs (job_id, project_id, org_id, request_tenant_id, kind, "
                "tool_name, status, params, input_version_id, idempotency_key, "
                "submission_fingerprint, execution_context, max_attempts) "
                "SELECT %(job_id)s, p.project_id, p.org_id, %(tenant)s, 'solve', %(tool)s, "
                "'queued', %(params)s, %(input_version_id)s, %(key)s, %(fingerprint)s, "
                "%(execution)s, %(max_attempts)s FROM projects p "
                "WHERE p.org_id = %(org_id)s AND p.project_id = %(project_id)s "
                "AND p.deleted_at IS NULL AND p.status = 'active' "
                "AND (%(input_version_id)s::uuid IS NULL OR EXISTS (SELECT 1 FROM drawing_versions dv "
                "WHERE dv.org_id = p.org_id AND dv.project_id = p.project_id "
                "AND dv.version_id = %(input_version_id)s AND dv.deleted_at IS NULL)) "
                "AND COALESCE((SELECT authority_mode FROM project_authority_modes "
                "WHERE org_id = p.org_id AND project_id = p.project_id), "
                "(SELECT authority_mode FROM tenant_authority_modes WHERE org_id = p.org_id), "
                "'legacy_sqlite') = 'postgres_canonical' "
                # TOCTOU guard: the insert commits only if the org is STILL
                # active with the exact tier the entitlement evaluation used —
                # both re-read atomically with the insert itself.
                "AND EXISTS (SELECT 1 FROM orgs o WHERE o.org_id = p.org_id "
                "AND o.status = 'active' AND o.tier IS NOT DISTINCT FROM %(pinned_tier)s) "
                "ON CONFLICT (org_id, project_id, idempotency_key) "
                "WHERE idempotency_key IS NOT NULL DO NOTHING RETURNING " + _job_columns(),
                {"job_id": new_uuid(), "org_id": org_id, "project_id": project_id,
                 "tenant": str(request_tenant_id), "tool": tool_name, "params": Jsonb(params),
                 "input_version_id": input_version_id, "key": idempotency_key,
                 "fingerprint": fingerprint,
                 "execution": Jsonb(execution_context),
                 "max_attempts": max_attempts, "pinned_tier": pinned_tier},
            )
            row = cur.fetchone()
            if row is not None:
                cur.executemany(
                    "INSERT INTO job_snapshot_pins "
                    "(org_id, project_id, job_id, snapshot_kind, snapshot_id) "
                    "VALUES (%(org)s, %(project)s, %(job)s, %(kind)s, %(snapshot)s)",
                    [{"org": org_id, "project": project_id, "job": row["job_id"],
                      "kind": kind, "snapshot": uuid.UUID(pin["snapshot_id"])}
                     for kind, pin in snapshot_pins.items()],
                )
                return _record(row)
            existing = _existing_by_idempotency_key(cur, org_id, project_id, idempotency_key)
            if existing is None:
                # No row and no idempotent twin: either a project predicate
                # failed, or the org's status/tier changed between evaluation
                # and insert (the TOCTOU guard fired). Re-evaluate so a real
                # mid-flight downgrade denies with the documented envelope
                # instead of the generic project error.
                recheck, _org = entitlements.stored_job_entitlement_verdict(org_id, "solve")
                if recheck is not None:
                    raise entitlements.EntitlementDenied(recheck)
                raise ValueError("project is unavailable or not postgres_canonical")
            if existing["submission_fingerprint"] != fingerprint:
                raise ValueError("idempotency key already exists with different run input")
            return _record(existing)


def _existing_by_idempotency_key(cur: Any, org_id: uuid.UUID, project_id: uuid.UUID,
                                 idempotency_key: str) -> Optional[Dict[str, Any]]:
    cur.execute(
        "SELECT " + _job_columns() + ", submission_fingerprint FROM jobs "
        "WHERE org_id = %(org_id)s AND project_id = %(project_id)s "
        "AND idempotency_key = %(key)s AND deleted_at IS NULL",
        {"org_id": org_id, "project_id": project_id, "key": idempotency_key},
    )
    return cur.fetchone()


def claim_next(worker_id: str, *, lease_seconds: float = 30,
               now: Optional[datetime] = None,
               request_tenant_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    if not worker_id or lease_seconds <= 0:
        raise ValueError("worker_id and a positive lease are required")
    current = _now(now)
    expires = current + timedelta(seconds=lease_seconds)
    with connection() as conn:
        with conn.cursor() as cur:
            # Exhausted expired work becomes a durable failure instead of staying claimable forever.
            cur.execute(
                "UPDATE jobs SET status = 'failed', error = %(error)s, finished_at = %(now)s, "
                "updated_at = %(now)s, lease_owner = NULL, lease_expires_at = NULL "
                "WHERE status = 'running' AND lease_expires_at < %(now)s "
                "AND attempt >= max_attempts AND deleted_at IS NULL",
                {"now": current, "error": Jsonb({"error_code": "ATTEMPTS_EXHAUSTED",
                                                  "message": "maximum attempts exhausted",
                                                  "retryable": False})},
            )
            cur.execute(
                "WITH candidate AS (SELECT j.job_id FROM jobs j "
                "WHERE j.deleted_at IS NULL AND j.request_tenant_id IS NOT NULL "
                "AND (%(tenant)s::text IS NULL OR j.request_tenant_id = %(tenant)s::text) "
                "AND j.attempt < j.max_attempts "
                "AND (j.status = 'queued' OR (j.status = 'running' AND j.lease_expires_at < %(now)s)) "
                "AND COALESCE((SELECT authority_mode FROM project_authority_modes "
                "WHERE org_id = j.org_id AND project_id = j.project_id), "
                "(SELECT authority_mode FROM tenant_authority_modes WHERE org_id = j.org_id), "
                "'legacy_sqlite') = 'postgres_canonical' "
                "ORDER BY j.created_at, j.job_id FOR UPDATE OF j SKIP LOCKED LIMIT 1) "
                "UPDATE jobs j SET status = 'running', attempt = j.attempt + 1, "
                "lease_owner = %(worker)s, lease_expires_at = %(expires)s, "
                "heartbeat_at = %(now)s, started_at = COALESCE(j.started_at, %(now)s), "
                "updated_at = %(now)s FROM candidate c WHERE j.job_id = c.job_id "
                "RETURNING " + ", ".join("j." + c.strip() for c in _job_columns().split(",")),
                {"now": current, "expires": expires, "worker": worker_id,
                 "tenant": request_tenant_id},
            )
            return _record(cur.fetchone())


def heartbeat(job_id: uuid.UUID, worker_id: str, *, lease_seconds: float = 30,
              now: Optional[datetime] = None) -> bool:
    current = _now(now)
    expires = current + timedelta(seconds=lease_seconds)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE jobs SET heartbeat_at = %(now)s, lease_expires_at = %(expires)s, "
                "updated_at = %(now)s WHERE job_id = %(job_id)s AND status = 'running' "
                "AND lease_owner = %(worker)s AND lease_expires_at >= %(now)s",
                {"now": current, "expires": expires, "job_id": job_id, "worker": worker_id},
            )
            return cur.rowcount == 1


def _validate_success(row: Any, result: Dict[str, Any], provenance: Dict[str, Any]) -> None:
    if not isinstance(result, dict) or not isinstance(result.get("solver_result"), dict):
        raise ValueError("successful solve requires an object solver_result")
    if result.get("solver") != row["tool_name"]:
        raise ValueError("solver identity does not match durable job tool")
    if not isinstance(result.get("solver_input"), dict):
        raise ValueError("successful solve requires an object solver_input")
    for name in ("request_sha256", "input_sha256", "result_sha256"):
        if not isinstance(result.get(name), str) or not _HASH_RE.fullmatch(result[name]):
            raise ValueError(f"successful solve requires a lowercase SHA-256 {name}")
    expected_request_hash = hashlib.sha256(
        json.dumps(row["params"], sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode("utf-8")).hexdigest()
    if result["request_sha256"] != expected_request_hash:
        raise ValueError("request_sha256 does not match durable job params")
    expected_input_hash = hashlib.sha256(
        json.dumps(result["solver_input"], sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode("utf-8")).hexdigest()
    if result["input_sha256"] != expected_input_hash:
        raise ValueError("input_sha256 does not match solver_input")
    expected_result_hash = hashlib.sha256(
        json.dumps(result["solver_result"], sort_keys=True, separators=(",", ":"),
                   ensure_ascii=False).encode("utf-8")).hexdigest()
    if result["result_sha256"] != expected_result_hash:
        raise ValueError("result_sha256 does not match solver_result")
    if not isinstance(provenance, dict):
        raise ValueError("successful solve requires provenance")
    attempt = provenance.get("attempt")
    if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt != row["attempt"]:
        raise ValueError("provenance attempt does not match the durable attempt")
    if provenance.get("execution_path") != "local":
        raise ValueError("first canonical solver requires local execution_path")
    for name in ("solver_revision", "source_sha256", "runtime"):
        if not isinstance(provenance.get(name), str) or not provenance[name].strip():
            raise ValueError(f"successful solve requires nonblank {name}")
        if result.get(name) != provenance[name]:
            raise ValueError(f"result {name} does not match provenance")
    if not _HASH_RE.fullmatch(provenance["source_sha256"]):
        raise ValueError("successful solve requires a lowercase SHA-256 source_sha256")


def complete_solve(job_id: uuid.UUID, worker_id: str, result_payload: Dict[str, Any],
                   provenance: Dict[str, Any], *, now: Optional[datetime] = None) -> str:
    current = _now(now)
    fingerprint = _fingerprint("canonical-job-success",
                               {"result": result_payload, "provenance": provenance})
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM jobs WHERE job_id = %(job_id)s FOR UPDATE", {"job_id": job_id})
            row = cur.fetchone()
            if row is None:
                return "missing"
            _validate_success(row, result_payload, provenance)
            if row["status"] in _TERMINAL:
                if row["terminal_fingerprint"] == fingerprint:
                    return "duplicate"
                cur.execute(
                    "UPDATE jobs SET terminal_conflict = %(conflict)s, updated_at = %(now)s "
                    "WHERE job_id = %(job_id)s",
                    {"conflict": Jsonb({"result": result_payload, "provenance": provenance,
                                        "receivedAt": current.isoformat()}),
                     "now": current, "job_id": job_id},
                )
                return "conflict"
            if row["status"] != "running" or row["lease_owner"] != worker_id \
                    or row["lease_expires_at"] is None or row["lease_expires_at"] < current:
                return "not_owner"
            solve_payload = {"jobId": str(job_id), "toolName": row["tool_name"],
                             "inputVersionId": (str(row["input_version_id"])
                                                if row["input_version_id"] else None),
                             **result_payload, "provenance": provenance}
            cur.execute(
                "SELECT p.snapshot_kind, p.snapshot_id, s.content_sha256, s.source_sha256, "
                "s.review_state FROM job_snapshot_pins p JOIN platform_snapshots s "
                "ON s.snapshot_id = p.snapshot_id AND s.snapshot_kind = p.snapshot_kind "
                "WHERE p.org_id = %(org)s AND p.project_id = %(project)s AND p.job_id = %(job)s "
                "ORDER BY p.snapshot_kind",
                {"org": row["org_id"], "project": row["project_id"], "job": job_id},
            )
            pin_rows = cur.fetchall()
            if {pin["snapshot_kind"] for pin in pin_rows} != set(_SNAPSHOT_KINDS):
                raise ValueError("successful solve requires catalog, standards, and AHJ pins")
            solve_payload["snapshotPins"] = {
                pin["snapshot_kind"]: {
                    "snapshot_id": str(pin["snapshot_id"]),
                    "content_sha256": pin["content_sha256"],
                    "source_sha256": pin["source_sha256"],
                    "review_state": pin["review_state"],
                }
                for pin in pin_rows
            }
            solve_digest = canonical_hash("solve-record", solve_payload)
            solve_id = new_uuid()
            solve_key = f"job:{job_id}:solve"
            cur.execute(
                "INSERT INTO solve_records (solve_id, org_id, project_id, payload, idempotency_key, "
                "hash_algorithm, hash_canonicalization, hash_domain, hash_value) "
                "VALUES (%(id)s, %(org)s, %(project)s, %(payload)s, %(key)s, %(algorithm)s, "
                "%(canonicalization)s, %(domain)s, %(value)s) RETURNING solve_id",
                {"id": solve_id, "org": row["org_id"], "project": row["project_id"],
                 "payload": Jsonb(solve_payload), "key": solve_key, **solve_digest.to_dict()},
            )
            _insert_outbox(cur, row["org_id"], row["project_id"], "solve_record", solve_id,
                           "solve.recorded", {"solveId": str(solve_id), "jobId": str(job_id)})
            cur.executemany(
                "INSERT INTO solve_snapshot_pins "
                "(org_id, project_id, solve_id, snapshot_kind, snapshot_id) "
                "VALUES (%(org)s, %(project)s, %(solve)s, %(kind)s, %(snapshot)s)",
                [{"org": row["org_id"], "project": row["project_id"], "solve": solve_id,
                  "kind": pin["snapshot_kind"], "snapshot": pin["snapshot_id"]}
                 for pin in pin_rows],
            )
            history_payload = {"jobId": str(job_id), "solveId": str(solve_id),
                               "solveHash": solve_digest.value,
                               "resultHash": result_payload["result_sha256"]}
            history_digest = canonical_hash(
                "history-operation", {"operationType": "solve.completed",
                                      "payload": history_payload})
            operation_id = new_uuid()
            history_key = f"job:{job_id}:history"
            cur.execute(
                "INSERT INTO history_operations (operation_id, org_id, project_id, operation_type, "
                "payload, idempotency_key, hash_algorithm, hash_canonicalization, hash_domain, hash_value) "
                "VALUES (%(id)s, %(org)s, %(project)s, 'solve.completed', %(payload)s, %(key)s, "
                "%(algorithm)s, %(canonicalization)s, %(domain)s, %(value)s)",
                {"id": operation_id, "org": row["org_id"], "project": row["project_id"],
                 "payload": Jsonb(history_payload), "key": history_key,
                 **history_digest.to_dict()},
            )
            _insert_outbox(cur, row["org_id"], row["project_id"], "history_operation",
                           operation_id, "history.operation.appended",
                           {"operationId": str(operation_id), "jobId": str(job_id)})
            stored_result = {**result_payload, "solve_id": str(solve_id),
                             "solve_hash": solve_digest.value,
                             "history_operation_id": str(operation_id),
                             "history_hash": history_digest.value,
                             "snapshotPins": solve_payload["snapshotPins"],
                             "execution_provenance": provenance}
            cur.execute(
                "UPDATE jobs SET status = 'succeeded', result = %(result)s, error = NULL, "
                "provenance = %(provenance)s, terminal_fingerprint = %(fingerprint)s, "
                "finished_at = %(now)s, updated_at = %(now)s, lease_owner = NULL, "
                "lease_expires_at = NULL WHERE job_id = %(job_id)s AND status = 'running' "
                "AND lease_owner = %(worker)s AND lease_expires_at >= %(now)s "
                "AND attempt = %(attempt)s",
                {"result": Jsonb(stored_result), "provenance": Jsonb(provenance),
                 "fingerprint": fingerprint, "now": current, "job_id": job_id,
                 "worker": worker_id, "attempt": row["attempt"]},
            )
            if cur.rowcount != 1:
                raise RuntimeError("terminal compare-and-set lost after row lock")
            return "applied"


def fail_or_retry(job_id: uuid.UUID, worker_id: str, error: Dict[str, Any],
                  provenance: Dict[str, Any], *, now: Optional[datetime] = None) -> str:
    current = _now(now)
    if not isinstance(error, dict) or not isinstance(error.get("message"), str) \
            or not error["message"].strip():
        raise ValueError("worker failure requires a nonblank error message")
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM jobs WHERE job_id = %(job_id)s FOR UPDATE", {"job_id": job_id})
            row = cur.fetchone()
            if row is None:
                return "missing"
            attempt = provenance.get("attempt") if isinstance(provenance, dict) else None
            if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt != row["attempt"]:
                raise ValueError("failure provenance attempt does not match the durable attempt")
            if row["status"] in _TERMINAL:
                return "duplicate" if row["status"] == "failed" and row["error"] == error else "conflict"
            if row["status"] != "running" or row["lease_owner"] != worker_id \
                    or row["lease_expires_at"] is None or row["lease_expires_at"] < current:
                return "not_owner"
            retry = error.get("retryable") is True and row["attempt"] < row["max_attempts"]
            status = "queued" if retry else "failed"
            cur.execute(
                "UPDATE jobs SET status = %(status)s, error = %(error)s, provenance = %(provenance)s, "
                "updated_at = %(now)s, finished_at = %(finished)s, lease_owner = NULL, "
                "lease_expires_at = NULL, terminal_fingerprint = %(fingerprint)s "
                "WHERE job_id = %(job_id)s AND status = 'running' AND lease_owner = %(worker)s "
                "AND lease_expires_at >= %(now)s AND attempt = %(attempt)s",
                {"status": status, "error": Jsonb(error), "provenance": Jsonb(provenance),
                 "now": current, "finished": None if retry else current,
                 "fingerprint": (None if retry else _fingerprint(
                     "canonical-job-failure", {"error": error, "provenance": provenance})),
                 "job_id": job_id, "worker": worker_id, "attempt": row["attempt"]},
            )
            return "retry" if retry else "failed"


def get_job_for_tenant(job_id: uuid.UUID, request_tenant_id: str) -> Optional[Dict[str, Any]]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT " + _job_columns() + " FROM jobs WHERE job_id = %(job_id)s "
                        "AND request_tenant_id = %(tenant)s AND deleted_at IS NULL",
                        {"job_id": job_id, "tenant": str(request_tenant_id)})
            return _record(cur.fetchone())


def list_jobs_for_tenant(request_tenant_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    bounded = min(max(int(limit), 1), 100)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT " + _job_columns() + " FROM jobs "
                        "WHERE request_tenant_id = %(tenant)s AND deleted_at IS NULL "
                        "ORDER BY created_at DESC LIMIT %(limit)s",
                        {"tenant": str(request_tenant_id), "limit": bounded})
            return [_record(row) for row in cur.fetchall()]


def record_worker_heartbeat(worker_id: str, tool_name: str, runtime: str,
                            source_revision: str, source_sha256: str, *,
                            now: Optional[datetime] = None) -> None:
    current = _now(now)
    if not all(isinstance(value, str) and value.strip()
               for value in (worker_id, tool_name, runtime, source_revision, source_sha256)):
        raise ValueError("worker heartbeat fields must be nonblank strings")
    if not _HASH_RE.fullmatch(source_sha256):
        raise ValueError("worker source_sha256 must be a lowercase SHA-256")
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO canonical_worker_heartbeats "
                "(worker_id, tool_name, runtime, source_revision, source_sha256, observed_at) "
                "VALUES (%(worker)s, %(tool)s, %(runtime)s, %(revision)s, %(source)s, %(now)s) "
                "ON CONFLICT (worker_id) DO UPDATE SET tool_name = EXCLUDED.tool_name, "
                "runtime = EXCLUDED.runtime, source_revision = EXCLUDED.source_revision, "
                "source_sha256 = EXCLUDED.source_sha256, "
                "observed_at = EXCLUDED.observed_at",
                {"worker": worker_id, "tool": tool_name, "runtime": runtime,
                 "revision": source_revision, "source": source_sha256, "now": current},
            )


def worker_health(tool_name: str, *, max_age_seconds: float = 15,
                  now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    current = _now(now)
    cutoff = current - timedelta(seconds=max_age_seconds)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT worker_id, tool_name, runtime, source_revision, source_sha256, observed_at "
                "FROM canonical_worker_heartbeats WHERE tool_name = %(tool)s "
                "AND observed_at >= %(cutoff)s ORDER BY observed_at DESC LIMIT 1",
                {"tool": tool_name, "cutoff": cutoff},
            )
            return _record(cur.fetchone())
