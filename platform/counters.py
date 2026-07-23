"""Atomic PostgreSQL counters for fleet-wide quotas and rate buckets.

This module deliberately does not own a migration. Authority-specific
migrations can bind :class:`SharedCounterStore` to a table with this contract:

    namespace TEXT NOT NULL
    counter_key TEXT NOT NULL
    value BIGINT NOT NULL
    updated_at TIMESTAMPTZ NOT NULL
    PRIMARY KEY (namespace, counter_key)

The owning authority decides retention and tenant foreign keys. The primitive
provides safe identifiers, atomic limit enforcement, and serializable retries.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from psycopg import sql

from . import db

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class CounterResult:
    """Result of one atomic counter consumption attempt."""

    accepted: bool
    value: int
    limit: Optional[int]


def _qualified_identifier(name: str) -> sql.Composed:
    parts = name.split(".")
    if not 1 <= len(parts) <= 2 or any(not _IDENTIFIER.fullmatch(part) for part in parts):
        raise ValueError("counter table must be a plain table or schema.table identifier")
    return sql.SQL(".").join(sql.Identifier(part) for part in parts)


def _row_value(row: Any, key: str, position: int) -> Any:
    if isinstance(row, Mapping):
        return row[key]
    return row[position]


class SharedCounterStore:
    """Bind atomic counter operations to one authority-owned table."""

    def __init__(self, table: str):
        self._table = _qualified_identifier(table)

    def consume_in_transaction(
        self, conn: Any, *, namespace: str, key: str,
        amount: int = 1, limit: Optional[int] = None,
    ) -> CounterResult:
        """Consume capacity inside the caller's existing transaction."""
        if not namespace.strip() or not key.strip():
            raise ValueError("counter namespace and key must not be empty")
        if amount < 1:
            raise ValueError("counter amount must be at least 1")
        if limit is not None and limit < 0:
            raise ValueError("counter limit must not be negative")

        query = sql.SQL(
            """
            INSERT INTO {table} AS target
                (namespace, counter_key, value, updated_at)
            SELECT %(namespace)s, %(key)s, %(amount)s, NOW()
            WHERE %(limit)s IS NULL OR %(amount)s <= %(limit)s
            ON CONFLICT (namespace, counter_key) DO UPDATE
            SET value = target.value + EXCLUDED.value,
                updated_at = NOW()
            WHERE %(limit)s IS NULL
               OR target.value + EXCLUDED.value <= %(limit)s
            RETURNING value
            """
        ).format(table=self._table)
        row = conn.execute(
            query,
            {"namespace": namespace, "key": key, "amount": amount, "limit": limit},
        ).fetchone()
        if row is None:
            # ON CONFLICT can wait for a concurrent first insert, then reject
            # the update because it would exceed the limit. A SELECT inside the
            # INSERT statement would still use its original Read Committed
            # snapshot and could miss that newly committed row. This later
            # statement gets a fresh snapshot and observes the committed value.
            row = conn.execute(
                sql.SQL(
                    "SELECT value FROM {table} "
                    "WHERE namespace = %(namespace)s AND counter_key = %(key)s"
                ).format(table=self._table),
                {"namespace": namespace, "key": key},
            ).fetchone()
            value = 0 if row is None else int(_row_value(row, "value", 0))
            return CounterResult(accepted=False, value=value, limit=limit)
        return CounterResult(
            accepted=True,
            value=int(_row_value(row, "value", 0)),
            limit=limit,
        )

    def consume(
        self, *, namespace: str, key: str, amount: int = 1,
        limit: Optional[int] = None, max_attempts: int = 3,
    ) -> CounterResult:
        """Atomically consume capacity in a retryable serializable transaction."""
        return db.run_transaction(
            lambda conn: self.consume_in_transaction(
                conn, namespace=namespace, key=key, amount=amount, limit=limit,
            ),
            isolation="serializable",
            max_attempts=max_attempts,
        )
