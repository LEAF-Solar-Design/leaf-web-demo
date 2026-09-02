"""Durable actor-scoped async jobs for all allowed Glug Mushy powers."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import secrets
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Mapping

from glug_executor import GlugExecutor, GlugExecutorError
from glug_live_adapters import SQLiteApprovalStore


AUTHOR_POWERS = frozenset({
    "code_question", "announcement_draft", "schedule_draft", "stage_change",
})
PUBLICATION_POWERS = frozenset({"create_review_branch", "create_pull_request"})
ALLOWED_POWERS = AUTHOR_POWERS | PUBLICATION_POWERS
SAFE_RECOVERY_POWERS = AUTHOR_POWERS
TERMINAL = frozenset({"completed", "failed"})


class GlugJobStore:
    def __init__(self, path: Path | str, *, clock=None, id_factory=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or (lambda: dt.datetime.now(dt.timezone.utc))
        self.id_factory = id_factory or (lambda: "job-" + secrets.token_hex(16))
        self._lock = threading.Lock()
        self._initialize()
        self.recover_after_restart()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path), timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS glug_mushy_jobs (
                    id TEXT PRIMARY KEY, job_type TEXT NOT NULL, status TEXT NOT NULL,
                    input_json TEXT NOT NULL, input_digest TEXT NOT NULL,
                    actor_digest TEXT NOT NULL, idempotency_key TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 0, max_attempts INTEGER NOT NULL,
                    claim_token_hash TEXT, created_at TEXT NOT NULL, started_at TEXT,
                    completed_at TEXT, expires_at TEXT NOT NULL,
                    result_json TEXT, error_json TEXT,
                    UNIQUE(actor_digest, idempotency_key)
                )
            """)

    def create(self, *, actor_id: str, job_type: str, job_input: Mapping[str, Any],
               idempotency_key: str, max_attempts: int = 1) -> tuple[Mapping[str, Any], bool]:
        if job_type not in ALLOWED_POWERS or not idempotency_key or len(idempotency_key) > 200:
            raise GlugExecutorError("job_invalid", "Job request is invalid", 422)
        actor_digest = _digest(actor_id)
        input_json = _json(dict(job_input))
        input_digest = _digest(input_json)
        now = _utc(self.clock())
        job_id = self.id_factory()
        expires = now + dt.timedelta(hours=24)
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT * FROM glug_mushy_jobs WHERE actor_digest=? AND idempotency_key=?",
                (actor_digest, idempotency_key),
            ).fetchone()
            created = prior is None
            if prior is None:
                connection.execute(
                    "INSERT INTO glug_mushy_jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (job_id, job_type, "queued", input_json, input_digest, actor_digest,
                     idempotency_key, 0, max_attempts, None, _format(now), None, None,
                     _format(expires), None, None),
                )
                prior = connection.execute(
                    "SELECT * FROM glug_mushy_jobs WHERE id=?", (job_id,)
                ).fetchone()
            elif prior["job_type"] != job_type or prior["input_digest"] != input_digest:
                connection.execute("ROLLBACK")
                raise GlugExecutorError("idempotency_conflict", "Job idempotency key was reused", 409)
            connection.execute("COMMIT")
        return self._public(prior), created

    def get(self, job_id: str, *, actor_id: str) -> Mapping[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM glug_mushy_jobs WHERE id=? AND actor_digest=?",
                (job_id, _digest(actor_id)),
            ).fetchone()
        return self._public(row) if row else None

    def get_internal(self, job_id: str) -> sqlite3.Row | None:
        with self._connect() as connection:
            return connection.execute(
                "SELECT * FROM glug_mushy_jobs WHERE id=?", (job_id,)
            ).fetchone()

    def claim(self, job_id: str) -> tuple[int, str, Mapping[str, Any], str]:
        token = secrets.token_urlsafe(32)
        now = _utc(self.clock())
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM glug_mushy_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if row is None or row["status"] != "queued" or row["attempt"] >= row["max_attempts"]:
                connection.execute("ROLLBACK")
                raise GlugExecutorError("job_unavailable", "Job is not claimable", 409)
            if _parse(row["expires_at"]) <= now:
                connection.execute(
                    "UPDATE glug_mushy_jobs SET status='failed', completed_at=?, error_json=? WHERE id=? AND status='queued'",
                    (_format(now), _json({"code": "job_expired"}), job_id),
                )
                connection.execute("COMMIT")
                raise GlugExecutorError("job_expired", "Job authorization expired", 409)
            attempt = row["attempt"] + 1
            connection.execute(
                "UPDATE glug_mushy_jobs SET status='running', attempt=?, claim_token_hash=?, started_at=? WHERE id=? AND status='queued'",
                (attempt, _digest(token), _format(now), job_id),
            )
            connection.execute("COMMIT")
        return attempt, token, json.loads(row["input_json"]), row["actor_digest"]

    def settle_success(self, job_id: str, *, attempt: int, claim: str,
                       result: Mapping[str, Any]) -> None:
        self._settle(job_id, attempt=attempt, claim=claim, result=result, error=None)

    def settle_failure(self, job_id: str, *, attempt: int, claim: str,
                       code: str) -> None:
        self._settle(job_id, attempt=attempt, claim=claim, result=None,
                     error={"code": code})

    def _settle(self, job_id: str, *, attempt: int, claim: str,
                result: Mapping[str, Any] | None, error: Mapping[str, Any] | None) -> None:
        now = _utc(self.clock())
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM glug_mushy_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if (
                row is None or row["status"] != "running" or row["attempt"] != attempt
                or row["claim_token_hash"] is None
                or not secrets.compare_digest(row["claim_token_hash"], _digest(claim))
            ):
                connection.execute("ROLLBACK")
                raise GlugExecutorError("job_claim_stale", "Job claim is stale", 409)
            status = "completed" if error is None else "failed"
            connection.execute(
                "UPDATE glug_mushy_jobs SET status=?, claim_token_hash=NULL, completed_at=?, result_json=?, error_json=? WHERE id=?",
                (status, _format(now), _json(dict(result)) if result is not None else None,
                 _json(dict(error)) if error is not None else None, job_id),
            )
            connection.execute("COMMIT")

    def recover_after_restart(self) -> None:
        now = _format(_utc(self.clock()))
        with self._lock, self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            placeholders = ",".join("?" for _ in SAFE_RECOVERY_POWERS)
            connection.execute(
                f"UPDATE glug_mushy_jobs SET status='queued', claim_token_hash=NULL, started_at=NULL WHERE status='running' AND job_type IN ({placeholders}) AND attempt < max_attempts",
                tuple(sorted(SAFE_RECOVERY_POWERS)),
            )
            connection.execute(
                f"UPDATE glug_mushy_jobs SET status='failed', claim_token_hash=NULL, completed_at=?, error_json=? WHERE status='running' AND (job_type NOT IN ({placeholders}) OR attempt >= max_attempts)",
                (now, _json({"code": "restart_recovery_required"}), *tuple(sorted(SAFE_RECOVERY_POWERS))),
            )
            connection.execute("COMMIT")

    def queued_ids(self) -> list[str]:
        with self._connect() as connection:
            return [row[0] for row in connection.execute(
                "SELECT id FROM glug_mushy_jobs WHERE status='queued' ORDER BY created_at, id"
            ).fetchall()]

    @staticmethod
    def _public(row: sqlite3.Row) -> Mapping[str, Any]:
        value = {
            "contract": "glug.mushy-job.v1", "id": row["id"],
            "job_type": row["job_type"], "status": row["status"],
            "attempt": row["attempt"], "max_attempts": row["max_attempts"],
            "created_at": row["created_at"], "started_at": row["started_at"],
            "completed_at": row["completed_at"], "expires_at": row["expires_at"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "error": json.loads(row["error_json"]) if row["error_json"] else None,
        }
        return value


class GlugJobService:
    def __init__(self, *, store: GlugJobStore, executor: GlugExecutor,
                 approvals: SQLiteApprovalStore, background: bool = True):
        self.store = store
        self.executor = executor
        self.approvals = approvals
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="glug-mushy") if background else None
        if self.pool:
            for job_id in self.store.queued_ids():
                self.pool.submit(self.run, job_id)

    def create(self, *, actor_id: str, requested_power: str,
               instruction: str | None, origin_job_id: str | None,
               approval_id: str | None, idempotency_key: str) -> tuple[Mapping[str, Any], bool]:
        if requested_power in AUTHOR_POWERS:
            if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 20_000:
                raise GlugExecutorError("job_invalid", "Instruction is invalid", 422)
            if origin_job_id is not None or approval_id is not None:
                raise GlugExecutorError("job_invalid", "Author job fields are invalid", 422)
            job_input = {"instruction": instruction.strip()}
        elif requested_power in PUBLICATION_POWERS:
            if instruction is not None or not origin_job_id or not approval_id:
                raise GlugExecutorError("job_invalid", "Publication job fields are invalid", 422)
            origin = self.store.get(origin_job_id, actor_id=actor_id)
            if not origin or origin["status"] != "completed" or origin["job_type"] != "stage_change":
                raise GlugExecutorError("origin_job_invalid", "Completed stage job is required", 409)
            job_input = {"origin_job_id": origin_job_id, "approval_id": approval_id}
        else:
            raise GlugExecutorError("power_unavailable", "Requested power is unavailable", 403)
        job, created = self.store.create(
            actor_id=actor_id, job_type=requested_power, job_input=job_input,
            idempotency_key=idempotency_key,
            max_attempts=2 if requested_power in AUTHOR_POWERS else 1,
        )
        if created and self.pool:
            self.pool.submit(self.run, job["id"])
        return job, created

    def run(self, job_id: str) -> None:
        try:
            attempt, claim_token, job_input, actor_digest = self.store.claim(job_id)
        except GlugExecutorError:
            return
        row = self.store.get_internal(job_id)
        if row is None:
            return
        try:
            if row["job_type"] in AUTHOR_POWERS:
                claim = self.executor.issue_claim(
                    {"workspace_id": "glug", "requested_power": row["job_type"]},
                    actor_id=actor_digest,
                )
                result = self.executor.execute({
                    "workspace_id": "glug", "requested_power": row["job_type"],
                    "instruction": job_input["instruction"], "claim": claim,
                }, actor_id=actor_digest)
            else:
                origin = self.store.get_internal(job_input["origin_job_id"])
                if origin is None or origin["status"] != "completed" or origin["actor_digest"] != actor_digest:
                    raise GlugExecutorError("origin_job_invalid", "Completed stage job is required", 409)
                origin_result = json.loads(origin["result_json"])
                result = self.executor.publish({
                    "workspace_id": "glug", "requested_power": row["job_type"],
                    "approval_id": job_input["approval_id"],
                    "stage_receipt": origin_result["receipt"],
                }, actor_id=actor_digest)
            self.store.settle_success(job_id, attempt=attempt, claim=claim_token, result=result)
        except Exception as exc:
            code = exc.code if isinstance(exc, GlugExecutorError) else "job_failed"
            self.store.settle_failure(job_id, attempt=attempt, claim=claim_token, code=code)

    def issue_approval(self, *, actor_id: str, origin_job_id: str,
                       publication_power: str, idempotency_key: str) -> Mapping[str, Any]:
        if publication_power not in PUBLICATION_POWERS:
            raise GlugExecutorError("power_unavailable", "Requested power is unavailable", 403)
        origin = self.store.get(origin_job_id, actor_id=actor_id)
        if not origin or origin["status"] != "completed" or origin["job_type"] != "stage_change":
            raise GlugExecutorError("origin_job_invalid", "Completed stage job is required", 409)
        receipt = origin["result"].get("receipt") if isinstance(origin["result"], dict) else None
        if not isinstance(receipt, dict):
            raise GlugExecutorError("origin_job_invalid", "Stage receipt is unavailable", 409)
        expires = _utc(self.store.clock()) + dt.timedelta(minutes=10)
        return self.approvals.issue(
            actor_digest=_digest(actor_id), origin_job_id=origin_job_id,
            repository=receipt["repository"], commit=receipt["commit"],
            power=publication_power, idempotency_key=idempotency_key,
            expires_at=_format(expires),
        )


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is None:
        raise GlugExecutorError("time_invalid", "Job time is invalid", 500)
    return value.astimezone(dt.timezone.utc)


def _format(value: dt.datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse(value: str) -> dt.datetime:
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except (AttributeError, ValueError) as exc:
        raise GlugExecutorError("time_invalid", "Job time is invalid", 500) from exc
