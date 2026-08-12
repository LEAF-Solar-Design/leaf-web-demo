"""PostgreSQL authority for P5a message requests and queued turns.

The session event stream is still the transcript. This module owns the smaller
request lifecycle: immutable admission identity, one executing and one queued
request per session, lease-bound queue claims, crash settlement, terminal
replay, and active counts. It is active only when ``LEAF_SESSIONS_STORE`` is
exactly ``postgres``. Other modes keep their existing in-process behavior.

Only recoverable plain-text queue data is stored. Credentials, image bytes,
confirmation payloads, and bearer material are never accepted by this module.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import time
import uuid
from hashlib import sha256
from pathlib import Path
from typing import Any, Dict, Optional

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

import session_store

SERVER_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = SERVER_DIR.parent
_REQUEST_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_JWT_VALUE = re.compile(
    r"^[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}$"
)
_AUTHORIZATION_VALUE = re.compile(
    r"(?i)^\s*(?:authorization\s*:\s*)?(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}\s*$"
)
_CREDENTIAL_KEYS = frozenset({
    "api_key", "apikey", "oauth_token", "oauthtoken", "access_token",
    "accesstoken", "refresh_token", "refreshtoken", "client_secret",
    "clientsecret", "authorization", "password", "private_key", "privatekey",
    "secret", "cookie",
})
_TERMINAL = frozenset({"completed", "failed", "abandoned"})
_MAX_RESPONSE_BYTES = 64 * 1024
_MAX_RECOVERABLE_BYTES = 64 * 1024


class RequestConflict(RuntimeError):
    """A request ID exists with different immutable authority or payload."""


class RequestStateError(RuntimeError):
    """The requested lifecycle transition is not valid for the durable row."""


class CredentialMaterial(ValueError):
    """A queue recovery payload contains high-confidence credential material."""


def enabled() -> bool:
    """P5a follows the sessions authority and never creates another selector."""
    return os.environ.get("LEAF_SESSIONS_STORE", "legacy").strip().lower() == "postgres"


def platform_db():
    """Load the local database package without shadowing stdlib ``platform``."""
    loaded = sys.modules.get("leaf_platform")
    if loaded is None:
        pkg_dir = _PROJECT_ROOT / "platform"
        spec = importlib.util.spec_from_file_location(
            "leaf_platform", pkg_dir / "__init__.py",
            submodule_search_locations=[str(pkg_dir)],
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load the Leaf platform database package")
        loaded = importlib.util.module_from_spec(spec)
        sys.modules["leaf_platform"] = loaded
        spec.loader.exec_module(loaded)
    from leaf_platform import db
    return db


def new_request_id() -> str:
    return str(uuid.uuid4())


def canonical_request_id(value: Optional[str]) -> str:
    if value is None:
        return new_request_id()
    if type(value) is not str or _REQUEST_ID.fullmatch(value) is None:
        raise ValueError("request_id must be a canonical UUID")
    return value


def payload_digest(value: Dict[str, Any]) -> str:
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return sha256(raw).hexdigest()


def _json_object(value: Any, *, maximum: int, label: str) -> Dict[str, Any]:
    if type(value) is not dict:
        raise ValueError(f"{label} must be an object")
    raw = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if len(raw) > maximum:
        raise ValueError(f"{label} exceeds {maximum} bytes")
    return json.loads(raw.decode("utf-8"))


def contains_credential_material(value: Any, *, _credential_key: bool = False) -> bool:
    """Return true for recursive, high-confidence credential shapes only."""
    if _credential_key:
        return not (
            value is None or value is False or (type(value) is str and value == "")
        )
    if type(value) is dict:
        for key, child in value.items():
            normalized = ""
            if type(key) is str:
                normalized = re.sub(
                    r"[^a-z0-9]+", "_", key.lower(),
                ).strip("_")
            compact = normalized.replace("_", "")
            if contains_credential_material(
                child,
                _credential_key=normalized in _CREDENTIAL_KEYS or compact in _CREDENTIAL_KEYS,
            ):
                return True
        return False
    if type(value) in (list, tuple):
        return any(contains_credential_material(child) for child in value)
    if type(value) is not str:
        return False
    return (
        _AUTHORIZATION_VALUE.fullmatch(value) is not None
        or _JWT_VALUE.fullmatch(value.strip()) is not None
    )


def validate_recoverable_payload(value: Dict[str, Any]) -> Dict[str, Any]:
    """Clone a bounded queue payload only after secret-shape refusal."""
    if contains_credential_material(value):
        raise CredentialMaterial("queue payload contains credential material")
    return _json_object(
        value, maximum=_MAX_RECOVERABLE_BYTES, label="recoverable payload",
    )


def _row(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    row = dict(value)
    for key in ("recoverable_json", "response_json"):
        raw = row.get(key)
        if raw is not None and not isinstance(raw, (dict, list)):
            row[key] = json.loads(raw)
    claimed = row.pop("claimed_recoverable_json", None)
    if claimed is not None:
        if not isinstance(claimed, (dict, list)):
            claimed = json.loads(claimed)
        row["recoverable_json"] = claimed
    return row


def _abandoned_response() -> Dict[str, Any]:
    return {
        "status": "abandoned",
        "error": {
            "error_code": "INTERNAL",
            "message": "request interrupted by restart",
        },
    }


def _settle_expired_executions(
    conn: Any, *, now: float, request_id: Optional[str] = None,
    tenant_id: Optional[str] = None, drawing_id: Optional[str] = None,
) -> int:
    """Atomically close expired execution leases in one trusted scope."""
    clauses = ["state='executing'", "lease_expires_at < %s"]
    params: list[Any] = [Jsonb(_abandoned_response()), now, now, now]
    if request_id is not None:
        clauses.append("request_id=%s")
        params.append(request_id)
    if tenant_id is not None:
        clauses.append("tenant_id=%s")
        params.append(tenant_id)
    if drawing_id is not None:
        clauses.append("drawing_id=%s")
        params.append(drawing_id)
    rows = conn.execute(
        "UPDATE app_session_requests SET state='abandoned', response_status=503,"
        " response_json=%s, terminal_at=%s, updated_at=%s,"
        " lease_owner=NULL, lease_expires_at=NULL WHERE "
        + " AND ".join(clauses)
        + " RETURNING request_id, session_id, turn_id",
        tuple(params),
    ).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE app_sessions SET active_turn_id=NULL, turn_started_at=NULL,"
            " active_turn_tier=NULL, active_turn_subject=NULL, updated_at=%s"
            " WHERE session_id=%s AND active_turn_id=%s",
            (now, row["session_id"], row["turn_id"]),
        )
    return len(rows)


def validate_startup() -> None:
    if not enabled():
        return
    db = platform_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT to_regclass('app_session_requests') AS requests,"
            " to_regclass('idx_app_session_requests_one_executing') AS executing_index,"
            " to_regclass('idx_app_session_requests_one_queued') AS queued_index"
        )
        row = cur.fetchone()
    if not row or not all(row.values()):
        raise RuntimeError(
            "PostgreSQL request journal is unavailable; "
            "apply 0035_session_request_journal.sql"
        )


def get_request(request_id: str) -> Optional[Dict[str, Any]]:
    db = platform_db()
    with db.transaction() as conn:
        _settle_expired_executions(conn, now=time.time(), request_id=request_id)
        return _row(conn.execute(
            "SELECT * FROM app_session_requests WHERE request_id = %s",
            (request_id,),
        ).fetchone())


def admit_request(
    *, request_id: str, tenant_id: str, drawing_id: str, session_id: str,
    principal_key: str, digest: str,
) -> tuple[Dict[str, Any], bool]:
    """Insert once, then require exact immutable binding on every retry."""
    request_id = canonical_request_id(request_id)
    if type(tenant_id) is not str or not tenant_id:
        raise ValueError("tenant_id must be a non-empty string")
    if type(drawing_id) is not str or not drawing_id:
        raise ValueError("drawing_id must be a non-empty string")
    if type(session_id) is not str or not session_id:
        raise ValueError("session_id must be a non-empty string")
    if type(principal_key) is not str:
        raise ValueError("principal_key must be a string")
    if type(digest) is not str or _DIGEST.fullmatch(digest) is None:
        raise ValueError("payload digest must be lowercase SHA-256")
    now = time.time()
    db = platform_db()
    with db.transaction() as conn:
        inserted = conn.execute(
            "INSERT INTO app_session_requests"
            " (request_id, tenant_id, drawing_id, session_id, principal_key,"
            " payload_digest, state, created_at, updated_at)"
            " VALUES (%s,%s,%s,%s,%s,%s,'admitted',%s,%s)"
            " ON CONFLICT (request_id) DO NOTHING RETURNING request_id",
            (request_id, tenant_id, drawing_id, session_id, principal_key,
             digest, now, now),
        ).fetchone()
        row = _row(conn.execute(
            "SELECT * FROM app_session_requests WHERE request_id = %s FOR UPDATE",
            (request_id,),
        ).fetchone())
    if row is None:
        raise RuntimeError("request admission did not return a row")
    expected = (tenant_id, drawing_id, session_id, principal_key, digest)
    actual = tuple(row.get(key) for key in (
        "tenant_id", "drawing_id", "session_id", "principal_key", "payload_digest",
    ))
    if actual != expected:
        raise RequestConflict("request_id is already bound to another request")
    if row.get("state") == "executing":
        row = get_request(request_id)
        if row is None:
            raise RuntimeError("request disappeared after admission")
    return row, inserted is not None


def begin_request_and_turn(
    request_id: str, turn_id: str, *, lease_seconds: float,
    session_id: str, stale_after_s: float, tier: Optional[str] = None,
    subject: Optional[str] = None,
) -> bool:
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    now = time.time()
    owner = str(uuid.uuid4())
    db = platform_db()
    try:
        with db.transaction() as conn:
            candidate = conn.execute(
                "SELECT session_id FROM app_session_requests"
                " WHERE request_id=%s AND state='admitted' FOR UPDATE",
                (request_id,),
            ).fetchone()
            if candidate is None or str(candidate["session_id"]) != str(session_id):
                return False
            competing = conn.execute(
                "SELECT 1 FROM app_session_requests WHERE session_id=%s"
                " AND state='executing' AND request_id<>%s LIMIT 1",
                (session_id, request_id),
            ).fetchone()
            if competing is not None or not session_store.pg_try_begin_turn_in_transaction(
                conn, session_id, turn_id, stale_after_s, tier=tier,
                subject=subject, started_at=now,
            ):
                return False
            row = conn.execute(
                "UPDATE app_session_requests SET state='executing', turn_id=%s,"
                " lease_owner=%s, lease_expires_at=%s, updated_at=%s"
                " WHERE request_id=%s AND state='admitted' RETURNING request_id",
                (turn_id, owner, now + lease_seconds, now, request_id),
            ).fetchone()
    except UniqueViolation:
        return False
    return row is not None


def queue_request(request_id: str, recoverable: Dict[str, Any]) -> bool:
    safe_recoverable = validate_recoverable_payload(recoverable)
    now = time.time()
    db = platform_db()
    try:
        with db.transaction() as conn:
            row = conn.execute(
                "UPDATE app_session_requests AS candidate"
                " SET state='queued', recoverable_json=%s, turn_id=NULL, lease_owner=NULL,"
                " lease_expires_at=NULL, updated_at=%s"
                " WHERE request_id=%s AND state='admitted' AND NOT EXISTS ("
                "   SELECT 1 FROM app_session_requests AS queued"
                "   WHERE queued.session_id=candidate.session_id"
                "   AND queued.state='queued'"
                " ) RETURNING request_id",
                (Jsonb(safe_recoverable), now, request_id),
            ).fetchone()
    except UniqueViolation:
        return False
    return row is not None


def claim_next_queued_and_turn(
    *, lease_seconds: float, stale_after_s: float,
    session_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Atomically claim one queued request and its exact session turn."""
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    now = time.time()
    turn_id = str(uuid.uuid4())
    owner = str(uuid.uuid4())
    db = platform_db()
    with db.transaction() as conn:
        candidate = conn.execute(
            "SELECT queued.* FROM app_session_requests AS queued"
            " WHERE queued.state='queued'"
            " AND (CAST(%s AS TEXT) IS NULL OR queued.session_id=%s) AND NOT EXISTS ("
            "   SELECT 1 FROM app_session_requests AS active"
            "   WHERE active.session_id=queued.session_id AND active.state='executing'"
            " ) ORDER BY queued.created_at, queued.request_id"
            " FOR UPDATE OF queued SKIP LOCKED LIMIT 1",
            (session_id, session_id),
        ).fetchone()
        if candidate is None:
            return None
        payload = _row(candidate) or {}
        if not session_store.pg_try_begin_turn_in_transaction(
            conn, payload["session_id"], turn_id, stale_after_s,
            tier=(payload.get("recoverable_json") or {}).get("tier"),
            subject=(payload.get("recoverable_json") or {}).get("subject"),
            started_at=now,
        ):
            return None
        row = conn.execute(
            "UPDATE app_session_requests SET state='executing', recoverable_json=NULL,"
            " turn_id=%s, lease_owner=%s, lease_expires_at=%s, updated_at=%s"
            " WHERE request_id=%s AND state='queued' RETURNING *",
            (turn_id, owner, now + lease_seconds, now, payload["request_id"]),
        ).fetchone()
        result = _row(row)
        if result is not None:
            result["recoverable_json"] = payload.get("recoverable_json")
        return result


def queued_for_session(session_id: str) -> Optional[Dict[str, Any]]:
    db = platform_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM app_session_requests"
            " WHERE session_id=%s AND state='queued'",
            (session_id,),
        )
        return _row(cur.fetchone())


def next_queued_recovery_delay() -> Optional[float]:
    """Seconds until the earliest live execution stops blocking queued work."""
    db = platform_db()
    with db.cursor() as cur:
        cur.execute(
            "SELECT MIN(active.lease_expires_at) AS lease_expires_at"
            " FROM app_session_requests AS queued"
            " JOIN app_session_requests AS active"
            " ON active.session_id=queued.session_id"
            " WHERE queued.state='queued' AND active.state='executing'"
        )
        row = cur.fetchone()
    lease_expires_at = row.get("lease_expires_at") if row else None
    if lease_expires_at is None:
        return None
    # Settlement uses a strict less-than comparison. A small positive floor
    # ensures the follow-up pass runs after, not exactly on, the lease edge.
    return max(0.05, float(lease_expires_at) - time.time())


def finish_request(
    request_id: str, turn_id: str, *, state: str,
    response_status: int, response: Dict[str, Any],
) -> bool:
    if state not in _TERMINAL:
        raise ValueError("terminal request state is invalid")
    if type(response_status) is not int or not 100 <= response_status <= 599:
        raise ValueError("response_status is invalid")
    safe_response = _json_object(
        response, maximum=_MAX_RESPONSE_BYTES, label="terminal response",
    )
    now = time.time()
    db = platform_db()
    with db.transaction() as conn:
        row = conn.execute(
            "UPDATE app_session_requests SET state=%s, response_status=%s,"
            " response_json=%s, terminal_at=%s, updated_at=%s,"
            " recoverable_json=NULL, lease_owner=NULL, lease_expires_at=NULL"
            " WHERE request_id=%s AND state='executing' AND turn_id=%s"
            " RETURNING request_id",
            (
                state, response_status, Jsonb(safe_response), now, now,
                request_id, turn_id,
            ),
        ).fetchone()
    return row is not None


def finish_turn(
    session_id: str, turn_id: str, *, state: str,
    response_status: int, response: Dict[str, Any],
) -> bool:
    """Terminalize an orphaned turn when only its durable turn identity remains."""
    if state not in _TERMINAL:
        raise ValueError("terminal request state is invalid")
    safe_response = _json_object(
        response, maximum=_MAX_RESPONSE_BYTES, label="terminal response",
    )
    now = time.time()
    db = platform_db()
    with db.transaction() as conn:
        row = conn.execute(
            "UPDATE app_session_requests SET state=%s, response_status=%s,"
            " response_json=%s, terminal_at=%s, updated_at=%s,"
            " recoverable_json=NULL, lease_owner=NULL, lease_expires_at=NULL"
            " WHERE session_id=%s AND state='executing' AND turn_id=%s"
            " RETURNING request_id",
            (
                state, response_status, Jsonb(safe_response), now, now,
                session_id, turn_id,
            ),
        ).fetchone()
    return row is not None


def fail_admitted(
    request_id: str, *, response_status: int, response: Dict[str, Any],
) -> bool:
    safe_response = _json_object(
        response, maximum=_MAX_RESPONSE_BYTES, label="terminal response",
    )
    now = time.time()
    db = platform_db()
    with db.transaction() as conn:
        row = conn.execute(
            "UPDATE app_session_requests SET state='failed', response_status=%s,"
            " response_json=%s, terminal_at=%s, updated_at=%s, recoverable_json=NULL"
            " WHERE request_id=%s AND state='admitted' RETURNING request_id",
            (response_status, Jsonb(safe_response), now, now, request_id),
        ).fetchone()
    return row is not None


def fail_queued(
    request_id: str, *, response_status: int, response: Dict[str, Any],
) -> bool:
    safe_response = _json_object(
        response, maximum=_MAX_RESPONSE_BYTES, label="terminal response",
    )
    now = time.time()
    db = platform_db()
    with db.transaction() as conn:
        row = conn.execute(
            "UPDATE app_session_requests SET state='failed', response_status=%s,"
            " response_json=%s, terminal_at=%s, updated_at=%s, recoverable_json=NULL"
            " WHERE request_id=%s AND state='queued' RETURNING request_id",
            (response_status, Jsonb(safe_response), now, now, request_id),
        ).fetchone()
    return row is not None


def active_counts(tenant_id: str, drawing_id: str) -> Dict[str, int]:
    db = platform_db()
    with db.transaction() as conn:
        _settle_expired_executions(
            conn, now=time.time(), tenant_id=tenant_id, drawing_id=drawing_id,
        )
        rows = conn.execute(
            "SELECT state, COUNT(*) AS count FROM app_session_requests"
            " WHERE tenant_id=%s AND drawing_id=%s"
            " AND state IN ('queued','executing') GROUP BY state",
            (tenant_id, drawing_id),
        ).fetchall()
    counts = {"queued": 0, "executing": 0}
    for row in rows:
        state = row["state"]
        if state in counts:
            counts[state] = int(row["count"])
    counts["active"] = counts["queued"] + counts["executing"]
    return counts


def settle_abandoned(*, admitted_after_seconds: float = 30.0) -> int:
    """Close crash leftovers without retrying possibly effectful execution."""
    now = time.time()
    cutoff = now - admitted_after_seconds
    response = _abandoned_response()
    db = platform_db()
    with db.transaction() as conn:
        rows = conn.execute(
            "UPDATE app_session_requests SET state='abandoned', response_status=503,"
            " response_json=%s, terminal_at=%s, updated_at=%s,"
            " lease_owner=NULL, lease_expires_at=NULL"
            " WHERE (state='executing' AND lease_expires_at < %s)"
            " OR (state='admitted' AND created_at < %s)"
            " RETURNING request_id, session_id, turn_id",
            (Jsonb(response), now, now, now, cutoff),
        ).fetchall()
        for row in rows:
            if row.get("turn_id") is not None:
                conn.execute(
                    "UPDATE app_sessions SET active_turn_id=NULL, turn_started_at=NULL,"
                    " active_turn_tier=NULL, active_turn_subject=NULL, updated_at=%s"
                    " WHERE session_id=%s AND active_turn_id=%s",
                    (now, row["session_id"], row["turn_id"]),
                )
    return len(rows)
