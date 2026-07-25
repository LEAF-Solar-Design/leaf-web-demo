"""Database access: lazy shared connection pool + cursor helpers.

Mirrors the cadwalk-studio tenancy idiom (shared-pool lazy connect, injectable
client). Reads DATABASE_URL from the environment, falling back to
platform/.env.local (gitignored). Prepared statements are disabled
(prepare_threshold=None) so the store works over a pgbouncer/Neon pooled endpoint
as well as a direct one.
"""
from __future__ import annotations

import os
import threading
import time
from hashlib import sha256
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, TypeVar
from urllib.parse import urlsplit

import psycopg
from psycopg.errors import DeadlockDetected, SerializationFailure
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_PKG_DIR = Path(__file__).resolve().parent
_ENV_LOCAL = _PKG_DIR / ".env.local"

_pool: Optional[ConnectionPool] = None
_pool_lock = threading.Lock()
_T = TypeVar("_T")

_TRANSACTION_ISOLATION = {
    "read committed": "READ COMMITTED",
    "repeatable read": "REPEATABLE READ",
    "serializable": "SERIALIZABLE",
}

_MIGRATION_LEDGER_TABLE = "leaf_schema_migrations"
_MIGRATION_LEDGER_COLUMNS = {"name", "sha256", "applied_at"}

# The minimum schema required by the API and canonical worker. This is a
# compatibility contract, not a provider choice. Additions stay additive so an
# older application can continue to read a database prepared by a newer image.
_REQUIRED_COLUMNS = {
    "orgs": {"org_id", "tier", "status"},
    "projects": {"project_id", "org_id", "status"},
    "built_tools": {"tool_id", "project_id", "org_id", "name"},
    "drawing_versions": {
        "version_id", "project_id", "org_id", "drawing_id", "provenance",
        "idempotency_key", "import_fingerprint",
    },
    "jobs": {
        "job_id", "project_id", "org_id", "status", "request_tenant_id",
        "attempt", "max_attempts", "lease_owner", "lease_expires_at",
        "execution_context",
    },
    "tenant_authority_modes": {"org_id", "authority_mode"},
    "project_authority_modes": {"org_id", "project_id", "authority_mode"},
    "canonical_worker_heartbeats": {
        "worker_id", "tool_name", "source_revision", "source_sha256", "observed_at",
    },
    "drawing_artifacts": {"drawing_id", "org_id", "project_id"},
    "drawing_upload_attempts": {
        "tenant_id", "drawing_id", "attempt", "status", "marker",
    },
    "drawing_store_versions": {
        "tenant_id", "drawing_id", "version", "state", "object_key",
        "byte_count", "content_sha256", "intake_ref", "intake_sha256",
    },
    "identity_bindings": {
        "binding_id", "platform_tenant_id", "role", "status",
    },
    "history_operations": {"operation_id", "org_id", "project_id", "hash_value"},
    "history_edges": {"edge_id", "org_id", "project_id"},
    "branch_refs": {"ref_id", "org_id", "project_id", "operation_id"},
    "solve_records": {"solve_id", "org_id", "project_id", "hash_value"},
    "outbox_entries": {"outbox_id", "org_id", "project_id", "event_type"},
    "project_share_grants": {"grant_id", "org_id", "project_id", "token_digest"},
    "platform_snapshots": {"snapshot_id", "snapshot_kind", "content_sha256"},
    "snapshot_channels": {"snapshot_kind", "channel", "snapshot_id"},
    "job_snapshot_pins": {"org_id", "project_id", "job_id", "snapshot_id"},
    "solve_snapshot_pins": {"org_id", "project_id", "solve_id", "snapshot_id"},
    "compliance_runs": {"run_id", "org_id", "project_id", "solve_id"},
    "compliance_findings": {"finding_id", "org_id", "project_id", "run_id"},
    "compliance_waivers": {"waiver_id", "org_id", "project_id", "finding_id"},
    "compliance_waiver_events": {"event_id", "waiver_id", "sequence", "state"},
    "evidence_bundles": {"bundle_id", "org_id", "project_id", "root_sha256"},
    "evidence_entries": {"entry_id", "bundle_id", "path", "content_sha256"},
    "professional_credentials": {"credential_id", "org_id", "binding_id"},
    "professional_credential_events": {"event_id", "credential_id", "state"},
    "review_signatures": {"signature_id", "org_id", "project_id", "bundle_id"},
}

# Each selector adds the tables its PostgreSQL implementation reads or writes.
# Legacy values intentionally add nothing. Session shadow and dual-write modes
# still write PostgreSQL, so they require the same proof as canonical reads.
_AUTHORITY_REQUIRED_COLUMNS = {
    "jobs": {
        "async_jobs": {"job_id", "tenant_id", "status", "submission_fingerprint"},
        "async_job_terminal_conflicts": {"job_id", "fingerprint", "evidence_json"},
    },
    "callback_replay": {
        "callback_consumed_nonces": {"job_id", "nonce", "expires_at"},
    },
    "sessions": {
        "app_sessions": {"session_id", "tenant_id", "status", "model"},
        "app_session_events": {"session_id", "seq", "data_json"},
        "app_approvals": {"confirmation_id", "decided", "consumed"},
    },
    "agent": {
        "agent_approvals": {"confirmation_id", "tenant_id", "args_hash"},
        "agent_session_grants": {"tenant_id", "session_id", "action", "target_key"},
        "agent_rate_counters": {"namespace", "counter_key", "value"},
        "agent_fleet_state": {"state_key", "active"},
        "agent_gate_audit_events": {"event_id", "kind", "event"},
        "agent_tenant_state": {"tenant_id", "agent_disabled", "overlay", "revision"},
        "agent_usage_turns": {"usage_key", "tenant_id", "turn_id", "record"},
    },
    "broker": {
        "broker_tenants": {"tenant_id", "disabled"},
        "broker_usage_ledger": {"event_key", "tenant_id", "status"},
        "broker_run_admissions": {"event_key", "tenant_id", "state", "lease_token"},
        "broker_aps_slots": {"event_key", "tenant_id", "state"},
        "broker_admission_resolution_audit": {"audit_id", "event_key", "resolution"},
    },
    "guest_caps": {
        "guest_upload_counters": {"namespace", "counter_key", "value"},
    },
    "drawing": {
        "drawing_store_manifests": {"tenant_id", "drawing_id", "head", "checkout_fence"},
        "drawing_store_versions": {
            "tenant_id", "drawing_id", "version", "state", "object_key",
            "content_sha256", "intake_ref", "intake_sha256",
        },
    },
    "upload": {
        "drawing_store_manifests": {"tenant_id", "drawing_id", "head"},
        "drawing_store_versions": {"tenant_id", "drawing_id", "version", "state"},
        "drawing_upload_attempts": {"tenant_id", "drawing_id", "attempt", "status"},
        "drawing_purge_receipts": {"tenant_id", "drawing_id", "attempt", "status"},
    },
    "harness_sessions": {
        "harness_sessions": {"session_id", "tenant_id", "drawing_id", "status"},
        "harness_turns": {"turn_id", "session_id", "status"},
        "harness_events": {"session_id", "seq", "turn_id", "data"},
        "harness_confirmations": {"confirmation_id", "session_id", "status"},
        "harness_usage": {"usage_id", "session_id", "turn_id", "usage"},
        "harness_tenant_repo_leases": {"tenant_id", "owner_token", "generation"},
    },
}

_AUTHORITY_SELECTORS = {
    "LEAF_JOBS_STORE": {"postgres": "jobs"},
    "LEAF_CALLBACK_REPLAY_STORE": {"postgres": "callback_replay"},
    "LEAF_SESSIONS_STORE": {
        "dual_write": "sessions",
        "dual_write_shadow": "sessions",
        "shadow": "sessions",
        "postgres": "sessions",
    },
    "LEAF_AGENT_STORE": {"postgres": "agent"},
    "LEAF_BROKER_STORE": {"postgres": "broker"},
    "LEAF_GUEST_CAP_STORE": {"postgres": "guest_caps"},
    "LEAF_DRAWING_STORE": {"postgres": "drawing"},
    "LEAF_UPLOAD_STORE": {"postgres": "upload"},
    "LEAF_HARNESS_SESSION_STORE": {"postgres": "harness_sessions"},
}
_RECONCILIATION_TABLES = (
    "orgs", "projects", "drawing_artifacts", "drawing_versions", "jobs",
    "built_tools", "history_operations", "solve_records", "outbox_entries",
)


def required_columns_for_selected_authorities(
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, set[str]]:
    """Return the base schema plus tables used by selected PostgreSQL stores."""
    source = os.environ if environ is None else environ
    required = {table: set(columns) for table, columns in _REQUIRED_COLUMNS.items()}
    for selector, values in _AUTHORITY_SELECTORS.items():
        authority = values.get(str(source.get(selector, "")).strip().lower())
        if authority is None:
            continue
        for table, columns in _AUTHORITY_REQUIRED_COLUMNS[authority].items():
            required.setdefault(table, set()).update(columns)
    return required


def _load_env_local() -> None:
    """Populate os.environ from platform/.env.local for any keys not already set."""
    if not _ENV_LOCAL.exists():
        return
    for raw in _ENV_LOCAL.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def get_database_url() -> str:
    if not os.environ.get("DATABASE_URL"):
        _load_env_local()
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Provide a Postgres/Neon connection string via the "
            "environment or platform/.env.local."
        )
    return url


def validate_database_url(url: str) -> None:
    """Reject malformed or non-PostgreSQL URLs without exposing credentials."""
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise RuntimeError("DATABASE_URL is not a valid PostgreSQL URL") from exc
    if parsed.scheme not in {"postgres", "postgresql"}:
        raise RuntimeError("DATABASE_URL must use the postgres or postgresql scheme")
    if not parsed.hostname or not parsed.path.strip("/"):
        raise RuntimeError("DATABASE_URL must include a host and database name")
    if port is not None and not (1 <= port <= 65535):
        raise RuntimeError("DATABASE_URL contains an invalid port")


def _configure(conn: psycopg.Connection) -> None:
    # Disable client-side prepared statements: safe across pgbouncer transaction pooling.
    conn.prepare_threshold = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                database_url = get_database_url()
                validate_database_url(database_url)
                _pool = ConnectionPool(
                    conninfo=database_url,
                    min_size=1,
                    max_size=5,
                    max_idle=600,
                    configure=_configure,
                    check=ConnectionPool.check_connection,
                    kwargs={"row_factory": dict_row},
                    open=True,
                )
    return _pool


def reset_pool() -> None:
    """Close the shared pool (used by tests / integration teardown)."""
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """A pooled connection. Commits on clean exit, rolls back on exception."""
    with get_pool().connection() as conn:
        yield conn


@contextmanager
def cursor() -> Iterator[psycopg.Cursor]:
    """A dict-row cursor on a pooled connection (commit/rollback handled by the pool)."""
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            yield cur


def _transaction_statement(
    isolation: str, *, read_only: bool, deferrable: bool,
) -> str:
    """Build a SET TRANSACTION statement from a closed set of safe options."""
    normalized = " ".join(isolation.lower().split())
    try:
        level = _TRANSACTION_ISOLATION[normalized]
    except KeyError as exc:
        supported = ", ".join(sorted(_TRANSACTION_ISOLATION))
        raise ValueError(f"unsupported transaction isolation; use one of: {supported}") from exc
    if deferrable and (not read_only or normalized != "serializable"):
        raise ValueError("deferrable transactions must be serializable and read-only")
    access = "READ ONLY" if read_only else "READ WRITE"
    suffix = " DEFERRABLE" if deferrable else ""
    return f"SET TRANSACTION ISOLATION LEVEL {level} {access}{suffix}"


@contextmanager
def transaction(
    *, isolation: str = "read committed", read_only: bool = False,
    deferrable: bool = False,
) -> Iterator[psycopg.Connection]:
    """Yield one explicit database transaction from the shared pool.

    The transaction-level settings do not leak to the next pool borrower.
    Clean exit commits and an exception rolls back. Callers that need retry
    semantics should use :func:`run_transaction`.
    """
    statement = _transaction_statement(
        isolation, read_only=read_only, deferrable=deferrable,
    )
    with get_pool().connection() as conn:
        with conn.transaction():
            conn.execute(statement)
            yield conn


def run_transaction(
    operation: Callable[[psycopg.Connection], _T], *,
    isolation: str = "read committed", read_only: bool = False,
    deferrable: bool = False, max_attempts: int = 3,
    retry_delay_seconds: float = 0.01,
) -> _T:
    """Run ``operation`` and retry only serialization/deadlock failures.

    Each retry receives a new transaction. Therefore ``operation`` must keep
    external side effects outside this callback or make them idempotent.
    Other database and application errors are surfaced immediately.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if retry_delay_seconds < 0:
        raise ValueError("retry_delay_seconds must not be negative")

    for attempt in range(max_attempts):
        try:
            with transaction(
                isolation=isolation, read_only=read_only, deferrable=deferrable,
            ) as conn:
                return operation(conn)
        except (SerializationFailure, DeadlockDetected):
            if attempt + 1 >= max_attempts:
                raise
            if retry_delay_seconds:
                time.sleep(min(retry_delay_seconds * (2 ** attempt), 0.25))
    raise AssertionError("transaction retry loop exhausted without returning or raising")


def _split_sql_statements(sql: str) -> List[str]:
    """Split migration DDL without breaking quoted or dollar-quoted bodies.

    Migrations are intentionally plain SQL, but immutable-ledger triggers use
    PostgreSQL ``$$`` function bodies.  A naïve ``split(';')`` would execute a
    function one fragment at a time.  This small lexer is sufficient for SQL
    migrations: it recognizes line comments, single-quoted strings, and tagged
    dollar quotes while preserving every byte sent to PostgreSQL.
    """
    statements, current = [], []
    i, quote, dollar = 0, False, None
    while i < len(sql):
        if not quote and dollar is None and sql.startswith("--", i):
            end = sql.find("\n", i)
            if end == -1:
                break
            i = end
            continue
        if dollar is not None:
            if sql.startswith(dollar, i):
                current.append(dollar)
                i += len(dollar)
                dollar = None
            else:
                current.append(sql[i])
                i += 1
            continue
        char = sql[i]
        if quote:
            current.append(char)
            if char == "'":
                if i + 1 < len(sql) and sql[i + 1] == "'":
                    current.append("'")
                    i += 2
                    continue
                quote = False
            i += 1
            continue
        if char == "'":
            quote = True
            current.append(char)
            i += 1
            continue
        if char == "$":
            end = sql.find("$", i + 1)
            if end != -1:
                candidate = sql[i:end + 1]
                if candidate[1:-1].replace("_", "a").isalnum() or candidate == "$$":
                    dollar = candidate
                    current.append(candidate)
                    i = end + 1
                    continue
        if char == ";":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
        else:
            current.append(char)
        i += 1
    statement = "".join(current).strip()
    if statement:
        statements.append(statement)
    return statements


def _migration_record(path: Path) -> Dict[str, str]:
    return {"name": path.name, "sha256": sha256(path.read_bytes()).hexdigest()}


def _ensure_migration_ledger(conn: psycopg.Connection) -> None:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_MIGRATION_LEDGER_TABLE} ("
        "name TEXT PRIMARY KEY, "
        "sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'), "
        "applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW())"
    )


def _apply_one(path: Path, *, track: bool) -> None:
    path = Path(path)
    sql = path.read_text(encoding="utf-8")
    record = _migration_record(path)
    with get_pool().connection() as conn:
        if track:
            _ensure_migration_ledger(conn)
            conn.execute("SELECT pg_advisory_xact_lock(743862775351786737)")
            row = conn.execute(
                f"SELECT sha256 FROM {_MIGRATION_LEDGER_TABLE} WHERE name = %(name)s",
                {"name": record["name"]},
            ).fetchone()
            if row is not None:
                if row["sha256"] != record["sha256"]:
                    raise RuntimeError(
                        f"platform migration hash drift: {record['name']}")
                return
        for stmt in _split_sql_statements(sql):
            conn.execute(stmt)
        if track:
            conn.execute(
                f"INSERT INTO {_MIGRATION_LEDGER_TABLE} (name, sha256) "
                "VALUES (%(name)s, %(sha256)s)",
                record,
            )


def apply_migration(sql_path: Optional[Path] = None) -> None:
    """Apply migrations. With no arg, apply EVERY ``NNNN_*.sql`` in ``migrations/``
    in sorted (numeric-prefix) order — so a fresh deploy gets 0001 (tables) AND
    0002 (deletion/purge columns) and any future migration, not just 0001. All
    migrations are idempotent (CREATE TABLE / ADD COLUMN IF NOT EXISTS). Shipped
    migrations are recorded with their source hash and skipped after a matching
    application. Hash drift fails before any changed SQL runs. Pass ``sql_path``
    to apply a single file (back-compat); external SQL remains untracked."""
    mig_dir = _PKG_DIR / "migrations"
    if sql_path is not None:
        path = Path(sql_path)
        track = path.resolve().parent == mig_dir.resolve() and path.match(
            "[0-9][0-9][0-9][0-9]_*.sql")
        _apply_one(path, track=track)
        return
    for path in sorted(mig_dir.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        _apply_one(path, track=True)


def migration_manifest() -> List[Dict[str, str]]:
    """Return a credential-free, deterministic manifest of shipped migrations."""
    mig_dir = _PKG_DIR / "migrations"
    return [
        _migration_record(path)
        for path in sorted(mig_dir.glob("[0-9][0-9][0-9][0-9]_*.sql"))
    ]


def _migration_proof(applied_rows: List[Mapping[str, str]]) -> Dict[str, Any]:
    """Compare database ledger rows with the exact migrations in this image."""
    expected = {item["name"]: item["sha256"] for item in migration_manifest()}
    applied = {row["name"]: row["sha256"] for row in applied_rows}
    missing = sorted(set(expected) - set(applied))
    drift = sorted(
        name for name, digest in expected.items()
        if name in applied and applied[name] != digest
    )
    return {
        "applied_migration_count": len(set(expected) & set(applied)),
        "missing_migrations": missing,
        "migration_hash_drift": drift,
    }


def schema_status() -> Dict[str, Any]:
    """Read-only readiness result for the API and canonical worker schema."""
    required = required_columns_for_selected_authorities()
    required[_MIGRATION_LEDGER_TABLE] = set(_MIGRATION_LEDGER_COLUMNS)
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() AND table_name = ANY(%(tables)s)",
                {"tables": list(required)},
            )
            found: Dict[str, set[str]] = {}
            for row in cur.fetchall():
                found.setdefault(row["table_name"], set()).add(row["column_name"])
            missing = {
                table: sorted(columns - found.get(table, set()))
                for table, columns in required.items()
                if columns - found.get(table, set())
            }
            applied_rows = []
            if not (_MIGRATION_LEDGER_COLUMNS - found.get(_MIGRATION_LEDGER_TABLE, set())):
                cur.execute(
                    f"SELECT name, sha256 FROM {_MIGRATION_LEDGER_TABLE} ORDER BY name")
                applied_rows = cur.fetchall()
            migration_proof = _migration_proof(applied_rows)
            cur.execute("SELECT current_database() AS database, current_schema() AS schema")
            identity = cur.fetchone()
    ok = not missing and not migration_proof["missing_migrations"] \
        and not migration_proof["migration_hash_drift"]
    return {
        "ok": ok,
        "database": identity["database"],
        "schema": identity["schema"],
        "migration_count": len(migration_manifest()),
        "missing": missing,
        **migration_proof,
    }


def assert_schema_current() -> Dict[str, Any]:
    """Fail closed when a database is reachable but not ready for this image."""
    status = schema_status()
    if not status["ok"]:
        details = [
            f"{table}({','.join(columns)})"
            for table, columns in status["missing"].items()
        ]
        if status["missing_migrations"]:
            details.append(
                "missing migrations: " + ",".join(status["missing_migrations"]))
        if status["migration_hash_drift"]:
            details.append(
                "migration hash drift: " + ",".join(status["migration_hash_drift"]))
        raise RuntimeError(
            "platform PostgreSQL schema is incomplete: " + "; ".join(details))
    return status


def reconciliation_snapshot() -> Dict[str, Any]:
    """Return counts for shadow/backfill comparison without returning tenant data."""
    status = assert_schema_current()
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            counts: Dict[str, int] = {}
            for table in _RECONCILIATION_TABLES:
                cur.execute(f"SELECT COUNT(*) AS count FROM {table}")
                counts[table] = int(cur.fetchone()["count"])
            cur.execute(
                "SELECT authority_mode, COUNT(*) AS count FROM tenant_authority_modes "
                "GROUP BY authority_mode ORDER BY authority_mode")
            tenant_modes = {
                row["authority_mode"]: int(row["count"]) for row in cur.fetchall()}
            cur.execute(
                "SELECT authority_mode, COUNT(*) AS count FROM project_authority_modes "
                "GROUP BY authority_mode ORDER BY authority_mode")
            project_modes = {
                row["authority_mode"]: int(row["count"]) for row in cur.fetchall()}
    return {
        "schema": status,
        "record_counts": counts,
        "authority_modes": {"tenant": tenant_modes, "project": project_modes},
    }
