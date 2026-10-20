"""Durable, scoped custody of one server-produced solve exchange per job."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import re
import unicodedata
from typing import Literal
from uuid import UUID

from . import canonical_jobs, db, project_graph_store
from .project_graph_store import ProjectContextError

MAX_REQUEST_BYTES = 262144
MAX_RESPONSE_BYTES = 1048576
MAX_CANDIDATE_BYTES = 1048576
MAX_PROOF_BYTES = 262144

_HASH_RE = re.compile(r"[0-9a-f]{64}")
_GRANT_RE = re.compile(r"[a-zA-Z0-9_-]{1,64}")
_REFUSALS = frozenset({
    "cloud_request_invalid", "cloud_auth_missing", "cloud_tenant_unauthorized",
    "cloud_upstream_failure", "cloud_response_invalid",
})
_TERMINAL = frozenset({"completed", "refused", "outcome_unknown"})


@dataclass(frozen=True)
class ExchangeScope:
    org_id: UUID
    project_id: UUID
    drawing_id: UUID
    job_id: UUID


@dataclass(frozen=True)
class JobAttempt:
    worker_id: str
    attempt: int


@dataclass(frozen=True)
class ResumeResult:
    action: Literal[
        "send_ready", "use_response",
        "completed", "refused", "outcome_unknown"
    ]
    exchange: dict


class SolveExchangeError(ProjectContextError):
    pass


class ExchangeInputError(SolveExchangeError):
    def __init__(self):
        super().__init__("SIP_W1C_EXCHANGE_INPUT_INVALID")


class ExchangeNotFound(SolveExchangeError):
    def __init__(self):
        super().__init__("SIP_W1C_EXCHANGE_NOT_FOUND")


class ExchangeConflict(SolveExchangeError):
    def __init__(self):
        super().__init__("SIP_W1C_EXCHANGE_CONFLICT")


class ExchangeLeaseLost(SolveExchangeError):
    def __init__(self):
        super().__init__("SIP_W1C_EXCHANGE_NOT_OWNER")


class ExchangeStateError(SolveExchangeError):
    def __init__(self):
        super().__init__("SIP_W1C_EXCHANGE_STATE_INVALID")


SCOPE_SQL = (
    "e.org_id=%(org_id)s AND e.project_id=%(project_id)s "
    "AND e.drawing_id=%(drawing_id)s AND e.job_id=%(job_id)s"
)
JOB_SQL = (
    "j.org_id=%(org_id)s AND j.project_id=%(project_id)s "
    "AND j.job_id=%(job_id)s AND j.request_tenant_id=%(tenant)s "
    "AND j.kind='run' AND j.tool_name='solar-solve-proposal' "
    "AND j.deleted_at IS NULL "
    "AND j.execution_context->>'schema'=%(schema)s "
    "AND j.execution_context->>'drawing_id'=%(drawing_id_text)s"
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
    "SELECT e.* FROM project_solve_exchanges e "
    "JOIN jobs j ON j.job_id=e.job_id "
    "WHERE " + SCOPE_SQL + " AND " + JOB_SQL + " AND " + ATTEMPT_SQL
)
LOCK_JOB_SQL = (
    "SELECT j.* FROM jobs j WHERE " + JOB_SQL +
    " AND " + ATTEMPT_SQL + " FOR UPDATE OF j"
)
LOCK_EXCHANGE_SQL = (
    "SELECT e.* FROM project_solve_exchanges e WHERE " +
    SCOPE_SQL + " FOR UPDATE OF e"
)
_INPUT_FIELDS = (
    "input_version_id", "parent_intake_sha256", "graph_revision", "graph_sha256",
    "frame_id", "phase", "request_bytes", "request_sha256", "grant_ref", "classification"
)
_PREPARE_FIELDS = ("job_id", "org_id", "project_id", "drawing_id", *_INPUT_FIELDS)
PREPARE_SQL = (
    "INSERT INTO project_solve_exchanges (" + ",".join(_PREPARE_FIELDS) + ") "
    "SELECT " + ",".join("%(" + name + ")s" for name in _PREPARE_FIELDS) +
    " FROM jobs j WHERE " + LIVE_SQL +
    " ON CONFLICT (job_id) DO NOTHING RETURNING *"
)
BEGIN_SEND_SQL = (
    "UPDATE project_solve_exchanges e "
    "SET state='sending',sender_worker=%(worker)s,"
    "sender_attempt=%(attempt)s,updated_at=clock_timestamp() "
    "WHERE " + SCOPE_SQL +
    " AND e.state='prepared' AND " + LIVE_EXISTS_SQL + " RETURNING e.*"
)
CAPTURE_SQL = (
    "UPDATE project_solve_exchanges e "
    "SET state='responded',response_bytes=%(response_bytes)s,"
    "response_sha256=%(response_sha256)s,solver_endpoint=%(solver_endpoint)s,"
    "solver_version=%(solver_version)s,updated_at=clock_timestamp() "
    "WHERE " + SCOPE_SQL +
    " AND e.state='sending' AND e.response_bytes IS NULL "
    "AND e.sender_worker=%(worker)s AND e.sender_attempt=%(attempt)s "
    "AND " + LIVE_EXISTS_SQL + " RETURNING e.*"
)
COMPLETE_SQL = (
    "UPDATE project_solve_exchanges e SET state='completed',"
    "candidate_bytes=%(candidate_bytes)s,candidate_sha256=%(candidate_sha256)s,"
    "proof_bytes=%(proof_bytes)s,proof_sha256=%(proof_sha256)s,"
    "updated_at=clock_timestamp() WHERE " + SCOPE_SQL +
    " AND e.state='responded' AND " + LIVE_EXISTS_SQL + " RETURNING e.*"
)
REFUSE_SQL = (
    "UPDATE project_solve_exchanges e "
    "SET state='refused',refusal_code=%(reason_code)s,"
    "updated_at=clock_timestamp() WHERE " + SCOPE_SQL +
    " AND e.state IN ('prepared','sending','responded') "
    "AND (e.state<>'sending' OR "
    "(e.sender_worker=%(worker)s AND e.sender_attempt=%(attempt)s)) "
    "AND " + LIVE_EXISTS_SQL + " RETURNING e.*"
)
UNKNOWN_SQL = (
    "UPDATE project_solve_exchanges e "
    "SET state='outcome_unknown',refusal_code='SOLVE_OUTCOME_UNKNOWN',"
    "updated_at=clock_timestamp() WHERE " + SCOPE_SQL +
    " AND e.state='sending' AND e.response_bytes IS NULL "
    "AND " + LIVE_EXISTS_SQL + " RETURNING e.*"
)


def _parameters(scope, lease):
    if not isinstance(scope, ExchangeScope) or not isinstance(lease, JobAttempt):
        raise ExchangeInputError()
    if any(not isinstance(getattr(scope, name), UUID) for name in (
            "org_id", "project_id", "drawing_id", "job_id")):
        raise ExchangeInputError()
    if (type(lease.worker_id) is not str
            or lease.worker_id != lease.worker_id.strip()
            or not 1 <= len(lease.worker_id) <= 200
            or any(unicodedata.category(char) in {"Cc", "Cs"} for char in lease.worker_id)
            or type(lease.attempt) is not int
            or not 1 <= lease.attempt <= 2147483647):
        raise ExchangeInputError()
    return {**asdict(scope), "worker": lease.worker_id, "attempt": lease.attempt,
            "drawing_id_text": str(scope.drawing_id),
            "tenant": str(scope.org_id),
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
    for name in ("request_bytes", "response_bytes", "candidate_bytes", "proof_bytes"):
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
            cur.execute("SELECT 1 FROM jobs j WHERE " + JOB_SQL, args)
            if cur.fetchone() is None:
                raise ExchangeNotFound()
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


def get_exchange(scope: ExchangeScope, lease: JobAttempt, *, conn=None) -> dict:
    args = _parameters(scope, lease)
    if conn is None:
        with db.connection() as connection:
            return get_exchange(scope, lease, conn=connection)
    with conn.cursor() as cur:
        cur.execute(READ_SQL, args)
        exchange = _exchange(cur.fetchone())
        if exchange is None:
            raise ExchangeNotFound()
        return exchange


def prepare_exchange(
    scope: ExchangeScope, lease: JobAttempt, *,
    request_bytes: bytes, request_sha256: str,
    grant_ref: str, classification: Literal["live", "synthetic"],
    input_version_id: UUID, parent_intake_sha256: str, graph_revision: int,
    graph_sha256: str, frame_id: str, phase: Literal["initial", "background"],
    conn=None
) -> dict:
    args = _parameters(scope, lease)
    _validate_bytes(request_bytes, request_sha256, MAX_REQUEST_BYTES)
    if (not isinstance(grant_ref, str) or _GRANT_RE.fullmatch(grant_ref) is None
            or not isinstance(classification, str) or classification not in {"live", "synthetic"}):
        raise ExchangeInputError()
    if (not isinstance(input_version_id, UUID)
            or type(graph_revision) is not int or not 0 <= graph_revision <= 2147483647
            or any(not isinstance(value, str) or _HASH_RE.fullmatch(value) is None
                   for value in (parent_intake_sha256, graph_sha256))
            or not _bounded_text(frame_id, 200)
            or not isinstance(phase, str) or phase not in {"initial", "background"}):
        raise ExchangeInputError()
    args.update(request_bytes=request_bytes, request_sha256=request_sha256,
                grant_ref=grant_ref, classification=classification,
                input_version_id=input_version_id, parent_intake_sha256=parent_intake_sha256,
                graph_revision=graph_revision, graph_sha256=graph_sha256,
                frame_id=frame_id, phase=phase)

    def operation(conn):
        job, _ = _lock(conn, args, required=False)
        if (UUID(str(job["input_version_id"])) != input_version_id
                or job["execution_context"]["parent_intake_sha256"] != parent_intake_sha256):
            raise ExchangeConflict()
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
            if any(existing[name] != args[name] for name in _INPUT_FIELDS):
                raise ExchangeConflict()
            return existing

    return operation(conn) if conn is not None else db.run_transaction(operation)


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

    # run_transaction returns only after its context managers commit. Callers
    # must dispatch only after True; this API deliberately accepts no conn.
    return db.run_transaction(operation)


def capture_response(
    scope: ExchangeScope, lease: JobAttempt, *,
    response_bytes: bytes, response_sha256: str, solver_endpoint: str, solver_version: str,
    conn=None
) -> Literal["stored", "duplicate"]:
    args = _parameters(scope, lease)
    _validate_bytes(response_bytes, response_sha256, MAX_RESPONSE_BYTES)
    if not _bounded_text(solver_endpoint, 2048) or not _bounded_text(solver_version, 200):
        raise ExchangeInputError()
    args.update(response_bytes=response_bytes, response_sha256=response_sha256,
                solver_endpoint=solver_endpoint, solver_version=solver_version)

    def operation(conn):
        _, exchange = _lock(conn, args)
        if not _sender(exchange, args):
            raise ExchangeLeaseLost()
        if exchange["state"] in _TERMINAL:
            raise ExchangeStateError()
        if exchange["response_bytes"] is not None:
            if (exchange["response_bytes"] != response_bytes
                    or exchange["response_sha256"] != response_sha256
                    or exchange["solver_endpoint"] != solver_endpoint
                    or exchange["solver_version"] != solver_version):
                raise ExchangeConflict()
            return "duplicate"
        if exchange["state"] != "sending":
            raise ExchangeStateError()
        _write(conn, CAPTURE_SQL, args)
        return "stored"

    return operation(conn) if conn is not None else db.run_transaction(operation)


def complete_exchange(
    scope: ExchangeScope, lease: JobAttempt, *,
    candidate_bytes: bytes, candidate_sha256: str,
    proof_bytes: bytes, proof_sha256: str, conn=None
) -> Literal["completed"]:
    args = _parameters(scope, lease)
    _validate_bytes(candidate_bytes, candidate_sha256, MAX_CANDIDATE_BYTES)
    _validate_bytes(proof_bytes, proof_sha256, MAX_PROOF_BYTES)
    args.update(candidate_bytes=candidate_bytes, candidate_sha256=candidate_sha256,
                proof_bytes=proof_bytes, proof_sha256=proof_sha256)

    def operation(conn):
        _, exchange = _lock(conn, args)
        if exchange["state"] == "completed":
            if any(exchange[name] != args[name] for name in (
                    "candidate_bytes", "candidate_sha256", "proof_bytes", "proof_sha256")):
                raise ExchangeConflict()
            raise ExchangeStateError()
        if exchange["state"] != "responded":
            raise ExchangeStateError()
        _write(conn, COMPLETE_SQL, args)
        return "completed"

    return operation(conn) if conn is not None else db.run_transaction(operation)


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
        if exchange["state"] == "sending" and not _sender(exchange, args):
            raise ExchangeLeaseLost()
        _write(conn, REFUSE_SQL, args)
        return "refused"

    return db.run_transaction(operation)


def record_outcome_unknown(
    scope: ExchangeScope, lease: JobAttempt
) -> Literal["outcome_unknown"]:
    args = _parameters(scope, lease)

    def operation(conn):
        _, exchange = _lock(conn, args)
        if exchange["state"] != "sending" or exchange["response_bytes"] is not None:
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
        if state == "sending":
            exchange = _write(conn, UNKNOWN_SQL, args)
            return ResumeResult("outcome_unknown", exchange)
        if state == "responded":
            return ResumeResult("use_response", exchange)
        return ResumeResult(state, exchange)

    return db.run_transaction(operation)


def _bounded_text(value, bound):
    return (type(value) is str and 1 <= len(value) <= bound
            and value == value.strip()
            and not any(unicodedata.category(char) in {"Cc", "Cs"} for char in value))


def read_completed_exchange(org_id, project_id, drawing_id, job_id, *, conn) -> dict:
    """Lease-free commit reader; stored bytes, never a reconstructed proposal."""
    if any(not isinstance(value, UUID) for value in (org_id, project_id, drawing_id, job_id)):
        raise ExchangeInputError()
    args = dict(org_id=org_id, project_id=project_id, drawing_id=drawing_id, job_id=job_id)
    with conn.cursor() as cur:
        cur.execute("SELECT e.* FROM project_solve_exchanges e WHERE " + SCOPE_SQL, args)
        exchange = _exchange(cur.fetchone())
    if exchange is None:
        raise ExchangeNotFound()
    if exchange["state"] != "completed":
        raise ExchangeStateError()
    for name, bound in (("request", MAX_REQUEST_BYTES), ("response", MAX_RESPONSE_BYTES),
                        ("candidate", MAX_CANDIDATE_BYTES), ("proof", MAX_PROOF_BYTES)):
        _validate_bytes(exchange[name + "_bytes"], exchange[name + "_sha256"], bound)
    for name in ("parent_intake_sha256", "graph_sha256"):
        if not isinstance(exchange[name], str) or _HASH_RE.fullmatch(exchange[name]) is None:
            raise ExchangeInputError()
    return exchange
