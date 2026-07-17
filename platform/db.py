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
    """Split a simple DDL script into individual statements.

    Strips line/inline ``--`` comments then splits on ``;``. Adequate for this
    migration (no dollar-quoted bodies, no semicolons inside string literals).
    """
    cleaned_lines = []
    for line in sql.splitlines():
        idx = line.find("--")
        if idx != -1:
            line = line[:idx]
        cleaned_lines.append(line)
    body = "\n".join(cleaned_lines)
    return [s.strip() for s in body.split(";") if s.strip()]


def apply_migration(sql_path: Optional[Path] = None) -> None:
    """Apply a migration file. Idempotent for 0001 (CREATE TABLE IF NOT EXISTS)."""
    path = sql_path or (_PKG_DIR / "migrations" / "0001_project_job.sql")
    sql = Path(path).read_text(encoding="utf-8")
    with get_pool().connection() as conn:
        for stmt in _split_sql_statements(sql):
            conn.execute(stmt)
