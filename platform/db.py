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
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, List, Optional

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

_PKG_DIR = Path(__file__).resolve().parent
_ENV_LOCAL = _PKG_DIR / ".env.local"

_pool: Optional[ConnectionPool] = None
_pool_lock = threading.Lock()


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


def _configure(conn: psycopg.Connection) -> None:
    # Disable client-side prepared statements: safe across pgbouncer transaction pooling.
    conn.prepare_threshold = None


def get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = ConnectionPool(
                    conninfo=get_database_url(),
                    min_size=1,
                    max_size=5,
                    configure=_configure,
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


def _apply_one(path: Path) -> None:
    sql = Path(path).read_text(encoding="utf-8")
    with get_pool().connection() as conn:
        for stmt in _split_sql_statements(sql):
            conn.execute(stmt)


def apply_migration(sql_path: Optional[Path] = None) -> None:
    """Apply migrations. With no arg, apply EVERY ``NNNN_*.sql`` in ``migrations/``
    in sorted (numeric-prefix) order — so a fresh deploy gets 0001 (tables) AND
    0002 (deletion/purge columns) and any future migration, not just 0001. All
    migrations are idempotent (CREATE TABLE / ADD COLUMN IF NOT EXISTS), so
    re-running is safe. Pass ``sql_path`` to apply a single file (back-compat)."""
    if sql_path is not None:
        _apply_one(Path(sql_path))
        return
    mig_dir = _PKG_DIR / "migrations"
    for path in sorted(mig_dir.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        _apply_one(path)
