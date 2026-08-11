"""PostgreSQL implementation of the frozen customization repository contract.

The operation methods remain in ``SQLiteCustomizationStore``. This adapter
supplies PostgreSQL connections and translates the small SQL dialect surface
used by those storage-independent methods. Schema creation is never a runtime
side effect. Migration 0020 must already be present.
"""
from __future__ import annotations

import re
import sqlite3
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from psycopg.errors import UniqueViolation

import platform_link
from customization_store import SQLiteCustomizationStore


_TABLES = {
    "customization_change_sets",
    "effective_catalogs",
    "customization_audit_events",
    "customization_confirmations",
    "customization_publication_requests",
    "customization_deployment_snapshots",
    "customization_deployment_audit",
    "customization_removal_requests",
}
_PG_NOW = (
    "to_char(clock_timestamp() AT TIME ZONE 'UTC', "
    '\'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"\')'
)
_SQLITE_NOW = "strftime('%Y-%m-%dT%H:%M:%fZ', 'now')"
_WRITE_FENCE = 0x4C45414643555354


def _postgres_sql(sql: str) -> str:
    translated = sql.replace(_SQLITE_NOW, _PG_NOW)
    translated = translated.replace(
        "ORDER BY created_at DESC, rowid DESC",
        "ORDER BY created_at DESC, confirmation_id DESC",
    )
    insert_ignore = translated.lstrip().upper().startswith("INSERT OR IGNORE INTO ")
    if insert_ignore:
        translated = re.sub(
            r"INSERT\s+OR\s+IGNORE\s+INTO",
            "INSERT INTO",
            translated,
            count=1,
            flags=re.IGNORECASE,
        )
        translated = translated.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
    return translated.replace("?", "%s")


class _PostgresConnection:
    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        try:
            return self._connection.execute(_postgres_sql(sql), params)
        except UniqueViolation as exc:
            raise sqlite3.IntegrityError("unique constraint failed") from exc

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()


class PostgresCustomizationStore(SQLiteCustomizationStore):
    """Shared customization authority over the platform PostgreSQL pool."""

    def __init__(self) -> None:
        self.database_path = "postgres"
        self._initialize_lock = threading.Lock()
        self._initialized = False

    def initialize(self) -> None:
        """Validate the migration-owned catalog without creating schema."""
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            database = platform_link.platform_db()
            database.assert_schema_current()
            with database.cursor() as cur:
                cur.execute(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = current_schema() "
                    "AND table_name = ANY(%s::text[])",
                    (sorted(_TABLES),),
                )
                found = {row["table_name"] for row in cur.fetchall()}
            missing = sorted(_TABLES - found)
            if missing:
                raise RuntimeError(
                    "customization PostgreSQL schema is incomplete: "
                    + ",".join(missing)
                )
            self._initialized = True

    ensure_started = initialize

    @contextmanager
    def _connection(self) -> Iterator[_PostgresConnection]:
        self.initialize()
        with platform_link.platform_db().connection() as conn:
            yield _PostgresConnection(conn)

    @contextmanager
    def _transaction(self) -> Iterator[_PostgresConnection]:
        self.initialize()
        # The transaction-wide advisory lock is the write fence. Read committed
        # takes each statement snapshot after the lock is acquired, so a waiting
        # writer observes the winning version and returns a domain conflict.
        with platform_link.platform_db().transaction(
            isolation="read committed"
        ) as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_WRITE_FENCE,))
            yield _PostgresConnection(conn)
