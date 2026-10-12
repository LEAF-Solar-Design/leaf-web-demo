"""Durable, scoped custody of one server-produced sizing exchange per job."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import re
import unicodedata
from typing import Any, Callable, Literal
from uuid import UUID

from . import canonical_jobs, db, project_graph_store
from .project_graph_store import ProjectContextError

MAX_REQUEST_BYTES = 65536
MAX_RESPONSE_BYTES = 65536
MAX_EVIDENCE_BYTES = 262144

_TARGET_RE = re.compile(
    r"leaf:[a-z][a-z0-9-]*:[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_HASH_RE = re.compile(r"[0-9a-f]{64}")
_GRANT_RE = re.compile(r"[a-zA-Z0-9_-]{1,64}")
_REFUSALS = frozenset({
    "cloud_request_invalid", "cloud_auth_missing", "cloud_tenant_unauthorized",
    "cloud_upstream_failure", "cloud_response_invalid",
})
_TERMINAL = frozenset({"published", "refused", "outcome_unknown"})


@dataclass(frozen=True)
class ExchangeScope:
    org_id: UUID
    project_id: UUID
    drawing_id: UUID
    job_id: UUID
    actor_binding_id: UUID
    target_settings_id: str
    admitted_tenant: str


@dataclass(frozen=True)
class JobAttempt:
    worker_id: str
    attempt: int


@dataclass(frozen=True)
class ResumeResult:
    action: Literal[
        "send_ready", "awaiting_response", "use_response",
        "published", "refused", "outcome_unknown"
    ]
    exchange: dict


class SizingExchangeError(ProjectContextError):
    pass


class ExchangeInputError(SizingExchangeError):
    def __init__(self):
        super().__init__("SIP_R14_EXCHANGE_INPUT_INVALID")


class ExchangeNotFound(SizingExchangeError):
    def __init__(self):
        super().__init__("SIP_R14_EXCHANGE_NOT_FOUND")


class ExchangeConflict(SizingExchangeError):
    def __init__(self):
        super().__init__("SIP_R14_EXCHANGE_CONFLICT")


class ExchangeLeaseLost(SizingExchangeError):
    def __init__(self):
        super().__init__("SIP_R14_EXCHANGE_NOT_OWNER")


class ExchangeStateError(SizingExchangeError):
    def __init__(self):
        super().__init__("SIP_R14_EXCHANGE_STATE_INVALID")


SCOPE_SQL = (
    "e.org_id=%(org_id)s AND e.project_id=%(project_id)s "
    "AND e.drawing_id=%(drawing_id)s AND e.job_id=%(job_id)s "
    "AND e.actor_binding_id=%(actor_binding_id)s "
    "AND e.target_settings_id=%(target_settings_id)s "
    "AND e.admitted_tenant=%(admitted_tenant)s"
)
JOB_SQL = (
    "j.org_id=%(org_id)s AND j.project_id=%(project_id)s "
    "AND j.job_id=%(job_id)s AND j.request_tenant_id=%(admitted_tenant)s "
    "AND j.kind='run' AND j.tool_name='solar-size-strings' "
    "AND j.deleted_at IS NULL "
    "AND j.execution_context->>'schema'=%(schema)s "
    "AND j.execution_context->>'drawing_id'=%(drawing_id_text)s "
    "AND j.execution_context->>'actor_binding_id'=%(actor_binding_id_text)s "
    "AND j.params->>'mode'='global'"
)
ATTEMPT_SQL = (
    "j.attempt=%(attempt)s AND ("
    "(j.status='running' AND j.lease_owner=%(worker)s) OR "
    "(j.status IN ('succeeded','failed','cancelled') "
    "AND j.provenance->>'worker_id'=%(worker)s))"
)
LIVE_SQL = (
    JOB_SQL +
    " AND j.status='running' AND j.lease_owner=%(worker)s "
    "AND j.attempt=%(attempt)s AND j.lease_expires_at>clock_timestamp()"
)
LIVE_EXISTS_SQL = "EXISTS (SELECT 1 FROM jobs j WHERE " + LIVE_SQL + ")"
READ_SQL = (
    "SELECT e.* FROM project_sizing_exchanges e "
    "JOIN jobs j ON j.job_id=e.job_id "
    "WHERE " + SCOPE_SQL + " AND " + JOB_SQL + " AND " + ATTEMPT_SQL
)
LOCK_JOB_SQL = (
    "SELECT j.* FROM jobs j WHERE " + JOB_SQL +
    " AND " + ATTEMPT_SQL + " FOR UPDATE OF j"
)
LOCK_EXCHANGE_SQL = (
    "SELECT e.* FROM project_sizing_exchanges e WHERE " +
    SCOPE_SQL + " FOR UPDATE OF e"
)
PREPARE_SQL = (
    "INSERT INTO project_sizing_exchanges ("
    "job_id,org_id,project_id,drawing_id,actor_binding_id,target_settings_id,"
    "admitted_tenant,grant_ref,classification,request_bytes,request_sha256) "
    "SELECT %(job_id)s,%(org_id)s,%(project_id)s,%(drawing_id)s,"
    "%(actor_binding_id)s,%(target_settings_id)s,%(admitted_tenant)s,"
    "%(grant_ref)s,%(classification)s,%(request_bytes)s,%(request_sha256)s "
    "FROM jobs j WHERE " + LIVE_SQL +
    " ON CONFLICT (job_id) DO NOTHING RETURNING *"
)
BEGIN_SEND_SQL = (
    "UPDATE project_sizing_exchanges e "
    "SET state='send_started',sender_worker=%(worker)s,"
    "sender_attempt=%(attempt)s,updated_at=clock_timestamp() "
    "WHERE " + SCOPE_SQL +
    " AND e.state='prepared' AND " + LIVE_EXISTS_SQL + " RETURNING e.*"
)
CAPTURE_SQL = (
    "UPDATE project_sizing_exchanges e "
    "SET state='response_stored',response_bytes=%(response_bytes)s,"
    "response_sha256=%(response_sha256)s,updated_at=clock_timestamp() "
    "WHERE " + SCOPE_SQL +
    " AND e.state='send_started' AND e.response_bytes IS NULL "
    "AND e.sender_worker=%(worker)s AND e.sender_attempt=%(attempt)s "
    "AND " + LIVE_EXISTS_SQL + " RETURNING e.*"
)
ATTACH_EVIDENCE_SQL = (
    "UPDATE project_sizing_exchanges e "
    "SET evidence_bytes=%(evidence_bytes)s,evidence_sha256=%(evidence_sha256)s,"
    "updated_at=clock_timestamp() WHERE " + SCOPE_SQL +
    " AND e.state='response_stored' AND e.evidence_bytes IS NULL "
    "AND " + LIVE_EXISTS_SQL + " RETURNING e.*"
)
REFUSE_SQL = (
    "UPDATE project_sizing_exchanges e "
    "SET state='refused',refusal_code=%(reason_code)s,"
    "updated_at=clock_timestamp() WHERE " + SCOPE_SQL +
    " AND e.state IN ('prepared','send_started','response_stored') "
    "AND (e.state<>'send_started' OR "
    "(e.sender_worker=%(worker)s AND e.sender_attempt=%(attempt)s)) "
    "AND " + LIVE_EXISTS_SQL + " RETURNING e.*"
)
UNKNOWN_SQL = (
    "UPDATE project_sizing_exchanges e "
    "SET state='outcome_unknown',refusal_code='SIZING_OUTCOME_UNKNOWN',"
    "updated_at=clock_timestamp() WHERE " + SCOPE_SQL +
    " AND e.state='send_started' AND e.response_bytes IS NULL "
    "AND " + LIVE_EXISTS_SQL + " RETURNING e.*"
)
OUTPUT_SQL = (
    "SELECT version_id FROM drawing_versions "
    "WHERE org_id=%(org_id)s AND project_id=%(project_id)s "
    "AND drawing_id=%(drawing_id)s AND version_id=%(output_version_id)s "
    "AND deleted_at IS NULL FOR SHARE"
)
PUBLISH_SQL = (
    "UPDATE project_sizing_exchanges e "
    "SET state='published',output_version_id=%(output_version_id)s,"
    "published_worker=%(worker)s,published_attempt=%(attempt)s,"
    "updated_at=clock_timestamp() WHERE " + SCOPE_SQL +
    " AND e.state='response_stored' AND e.evidence_bytes IS NOT NULL "
    "AND " + LIVE_EXISTS_SQL + " RETURNING e.*"
)


def _parameters(scope, lease):
    if not isinstance(scope, ExchangeScope) or not isinstance(lease, JobAttempt):
        raise ExchangeInputError()
    if any(not isinstance(getattr(scope, name), UUID) for name in (
            "org_id", "project_id", "drawing_id", "job_id", "actor_binding_id")):
        raise ExchangeInputError()
    if (not isinstance(scope.admitted_tenant, str)
            or scope.admitted_tenant != str(scope.org_id)
            or not isinstance(scope.target_settings_id, str)
            or len(scope.target_settings_id) > 128
            or _TARGET_RE.fullmatch(scope.target_settings_id) is None
            or type(lease.worker_id) is not str
            or lease.worker_id != lease.worker_id.strip()
            or not 1 <= len(lease.worker_id) <= 200
            or any(unicodedata.category(char) in {"Cc", "Cs"} for char in lease.worker_id)
            or type(lease.attempt) is not int
            or not 1 <= lease.attempt <= 2147483647):
        raise ExchangeInputError()
    return {**asdict(scope), "worker": lease.worker_id, "attempt": lease.attempt,
            "drawing_id_text": str(scope.drawing_id),
            "actor_binding_id_text": str(scope.actor_binding_id),
            "schema": canonical_jobs.PROJECT_GRAPH_JOB_SCHEMA}


def _validate_bytes(value, digest, bound):
    if (type(value) is not bytes or not 1 <= len(value) <= bound
            or not isinstance(digest, str) or _HASH_RE.fullmatch(digest) is None
            or digest != hashlib.sha256(value).hexdigest()):
        raise ExchangeInputError()


def _exchange(row):
    if row is None:
        return None
    result = dict(row)
    for name in ("request_bytes", "response_bytes", "evidence_bytes"):
        if result[name] is not None:
            result[name] = bytes(result[name])
    return result


def _live(row, args, cur):
    observed = project_graph_store._clock(cur)
    if (row["status"] != "running" or row["lease_owner"] != args["worker"]
            or row["attempt"] != args["attempt"] or row["lease_expires_at"] is None
            or row["lease_expires_at"] <= observed):
        raise ExchangeLeaseLost()


def _lock(conn, args, *, live=True, required=True):
    # Shared lock order with R4: job, graph scope (artifact), exchange.
    with conn.cursor() as cur:
        cur.execute(LOCK_JOB_SQL, args)
        job = cur.fetchone()
        if job is None:
            raise ExchangeLeaseLost()
        try:
            canonical_jobs._graph_ids(job)
            canonical_jobs._graph_scope(job, conn)
        except ProjectContextError as exc:
            raise ExchangeLeaseLost() from exc
        cur.execute(LOCK_EXCHANGE_SQL, args)
        exchange = _exchange(cur.fetchone())
        if live:
            _live(job, args, cur)
        if required and exchange is None:
            raise ExchangeNotFound()
        return job, exchange


def _write(conn, sql, args):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        if cur.rowcount != 1:
            raise ExchangeLeaseLost()
        return _exchange(cur.fetchone())


def _sender(exchange, args):
    return (exchange["sender_worker"] == args["worker"]
            and exchange["sender_attempt"] == args["attempt"])


def get_exchange(scope: ExchangeScope, lease: JobAttempt, *, conn=None) -> dict | None:
    args = _parameters(scope, lease)
    if conn is None:
        with db.connection() as connection:
            return get_exchange(scope, lease, conn=connection)
    with conn.cursor() as cur:
        cur.execute(READ_SQL, args)
        return _exchange(cur.fetchone())


def prepare_exchange(
    scope: ExchangeScope, lease: JobAttempt, *,
    request_bytes: bytes, request_sha256: str,
    grant_ref: str, classification: Literal["live", "synthetic"]
) -> dict:
    args = _parameters(scope, lease)
    _validate_bytes(request_bytes, request_sha256, MAX_REQUEST_BYTES)
    if (not isinstance(grant_ref, str) or _GRANT_RE.fullmatch(grant_ref) is None
            or not isinstance(classification, str) or classification not in {"live", "synthetic"}):
        raise ExchangeInputError()
    args.update(request_bytes=request_bytes, request_sha256=request_sha256,
                grant_ref=grant_ref, classification=classification)

    def operation(conn):
        job, _ = _lock(conn, args, required=False)
        with conn.cursor() as cur:
            cur.execute(PREPARE_SQL, args)
            inserted = _exchange(cur.fetchone())
            if inserted is not None:
                return inserted
            cur.execute(LOCK_EXCHANGE_SQL, args)
            existing = _exchange(cur.fetchone())
            _live(job, args, cur)
            if existing is None:
                # The conflicting job identity belongs to another full scope.
                raise ExchangeNotFound()
            if any(existing[name] != args[name] for name in (
                    "request_bytes", "request_sha256", "grant_ref", "classification")):
                raise ExchangeConflict()
            return existing

    return db.run_transaction(operation)


def begin_send(scope: ExchangeScope, lease: JobAttempt) -> bool:
    args = _parameters(scope, lease)

    def operation(conn):
        try:
            _lock(conn, args)
        except (ExchangeLeaseLost, ExchangeNotFound):
            return False
        with conn.cursor() as cur:
            cur.execute(BEGIN_SEND_SQL, args)
            return cur.rowcount == 1

    # run_transaction returns only after its context managers commit.
    return db.run_transaction(operation)


def capture_response(
    scope: ExchangeScope, lease: JobAttempt, *,
    response_bytes: bytes, response_sha256: str
) -> Literal["stored", "duplicate"]:
    args = _parameters(scope, lease)
    _validate_bytes(response_bytes, response_sha256, MAX_RESPONSE_BYTES)
    args.update(response_bytes=response_bytes, response_sha256=response_sha256)

    def operation(conn):
        _, exchange = _lock(conn, args)
        if not _sender(exchange, args):
            raise ExchangeLeaseLost()
        if exchange["state"] in _TERMINAL:
            raise ExchangeStateError()
        if exchange["response_bytes"] is not None:
            if (exchange["response_bytes"] != response_bytes
                    or exchange["response_sha256"] != response_sha256):
                raise ExchangeConflict()
            return "duplicate"
        if exchange["state"] != "send_started":
            raise ExchangeStateError()
        _write(conn, CAPTURE_SQL, args)
        return "stored"

    return db.run_transaction(operation)


def attach_evidence(
    scope: ExchangeScope, lease: JobAttempt, *,
    evidence_bytes: bytes, evidence_sha256: str
) -> Literal["stored", "duplicate"]:
    args = _parameters(scope, lease)
    _validate_bytes(evidence_bytes, evidence_sha256, MAX_EVIDENCE_BYTES)
    args.update(evidence_bytes=evidence_bytes, evidence_sha256=evidence_sha256)

    def operation(conn):
        _, exchange = _lock(conn, args)
        if exchange["state"] != "response_stored":
            raise ExchangeStateError()
        if exchange["evidence_bytes"] is not None:
            if (exchange["evidence_bytes"] != evidence_bytes
                    or exchange["evidence_sha256"] != evidence_sha256):
                raise ExchangeConflict()
            return "duplicate"
        _write(conn, ATTACH_EVIDENCE_SQL, args)
        return "stored"

    return db.run_transaction(operation)


def refuse_exchange(
    scope: ExchangeScope, lease: JobAttempt, *, reason_code: str
) -> Literal["refused", "duplicate"]:
    args = _parameters(scope, lease)
    if not isinstance(reason_code, str) or reason_code not in _REFUSALS:
        raise ExchangeInputError()
    args["reason_code"] = reason_code

    def operation(conn):
        _, exchange = _lock(conn, args)
        if exchange["state"] == "refused" and exchange["refusal_code"] == reason_code:
            return "duplicate"
        if exchange["state"] in _TERMINAL:
            raise ExchangeStateError()
        if exchange["state"] == "send_started" and not _sender(exchange, args):
            raise ExchangeLeaseLost()
        _write(conn, REFUSE_SQL, args)
        return "refused"

    return db.run_transaction(operation)


def record_outcome_unknown(
    scope: ExchangeScope, lease: JobAttempt
) -> Literal["outcome_unknown", "duplicate"]:
    args = _parameters(scope, lease)

    def operation(conn):
        _, exchange = _lock(conn, args)
        if exchange["state"] == "outcome_unknown":
            return "duplicate"
        if exchange["state"] != "send_started" or exchange["response_bytes"] is not None:
            raise ExchangeStateError()
        _write(conn, UNKNOWN_SQL, args)
        return "outcome_unknown"

    return db.run_transaction(operation)


def resume_exchange(scope: ExchangeScope, lease: JobAttempt) -> ResumeResult:
    args = _parameters(scope, lease)

    def operation(conn):
        # Observation of settled custody needs neither current authority nor expiry.
        exchange = get_exchange(scope, lease, conn=conn)
        if exchange is not None and exchange["state"] in _TERMINAL:
            return ResumeResult(exchange["state"], exchange)
        _, exchange = _lock(conn, args)
        state = exchange["state"]
        if state == "prepared":
            return ResumeResult("send_ready", exchange)
        if state == "send_started":
            if _sender(exchange, args):
                return ResumeResult("awaiting_response", exchange)
            exchange = _write(conn, UNKNOWN_SQL, args)
            return ResumeResult("outcome_unknown", exchange)
        if state == "response_stored":
            return ResumeResult("use_response", exchange)
        return ResumeResult(state, exchange)

    return db.run_transaction(operation)


def complete_exchange(
    scope: ExchangeScope, lease: JobAttempt, completion: dict, *,
    publish_and_prove: Callable[[dict, Any], dict], conn
) -> tuple[str, dict | None]:
    args = _parameters(scope, lease)
    job, exchange = _lock(conn, args, live=False)
    if exchange["state"] == "published":
        if (exchange["published_worker"] != lease.worker_id
                or exchange["published_attempt"] != lease.attempt
                or exchange["output_version_id"] != job["output_version_id"]
                or job["status"] != "succeeded"):
            raise ExchangeConflict()
    else:
        with conn.cursor() as cur:
            _live(job, args, cur)
        if (exchange["state"] != "response_stored"
                or exchange["response_bytes"] is None or exchange["evidence_bytes"] is None):
            raise ExchangeStateError()

    def publish_with_exchange(row, transaction_conn):
        published = publish_and_prove(row, transaction_conn)
        try:
            output_id = UUID(published["output_version_id"])
        except (ValueError, TypeError, KeyError, AttributeError):
            raise ExchangeConflict() from None
        publish_args = {**args, "output_version_id": output_id}
        with transaction_conn.cursor() as cur:
            cur.execute(OUTPUT_SQL, publish_args)
            if cur.fetchone() is None:
                raise ExchangeConflict()
            cur.execute(PUBLISH_SQL, publish_args)
            if cur.rowcount != 1:
                raise canonical_jobs._GraphLeaseLost()
        return published

    return canonical_jobs.complete_project_graph_job(
        scope.job_id,
        lease.worker_id,
        lease.attempt,
        completion,
        publish_and_prove=publish_with_exchange,
        conn=conn,
    )
