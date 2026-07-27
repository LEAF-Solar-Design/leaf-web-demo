#!/usr/bin/env python3
"""Backfill or compare the SQLite and PostgreSQL customization authorities.

The command prints counts and one aggregate digest only. It never prints tenant
rows, signatures, confirmation payloads, or the database URL.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]


def _platform_db() -> Any:
    package_dir = ROOT / "platform"
    if "leaf_platform" not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            "leaf_platform",
            package_dir / "__init__.py",
            submodule_search_locations=[str(package_dir)],
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load the platform package")
        module = importlib.util.module_from_spec(spec)
        sys.modules["leaf_platform"] = module
        spec.loader.exec_module(module)
    import leaf_platform.db as database

    return database


TABLE_COLUMNS: dict[str, tuple[str, ...]] = {
    "customization_change_sets": (
        "change_set_id", "tenant_id", "idempotency_key", "state", "version",
        "base_commit", "staged_commit", "catalog_digest",
        "desired_platform_release", "workspace_contract_digest",
        "author_subject", "approver_subject", "created_at", "updated_at",
    ),
    "customization_confirmations": (
        "confirmation_id", "tenant_id", "change_set_id", "payload_json",
        "signature", "consumed", "created_at",
    ),
    "customization_publication_requests": (
        "tenant_id", "change_set_id", "confirmation_id", "status",
        "reason_code", "created_at", "updated_at",
    ),
    "effective_catalogs": (
        "tenant_id", "change_set_id", "catalog_commit", "catalog_digest",
        "effective_platform_release", "workspace_contract_digest", "updated_at",
    ),
    "customization_audit_events": (
        "event_id", "tenant_id", "change_set_id", "prior_state", "next_state",
        "author_subject", "approver_subject", "base_commit", "staged_commit",
        "catalog_digest", "platform_release", "workspace_contract_digest",
        "idempotency_key", "result", "reason_code", "payload_json", "created_at",
    ),
    "customization_deployment_snapshots": (
        "snapshot_id", "payload_json", "effective_catalog_digest",
        "platform_release", "idempotency_key", "created_at",
    ),
    "customization_deployment_audit": (
        "audit_id", "snapshot_id", "action", "result", "idempotency_key",
        "created_at",
    ),
}
_PRIMARY_KEYS: dict[str, tuple[str, ...]] = {
    "customization_change_sets": ("change_set_id",),
    "customization_confirmations": ("confirmation_id",),
    "customization_publication_requests": ("tenant_id", "change_set_id"),
    "effective_catalogs": ("tenant_id",),
    "customization_audit_events": ("event_id",),
    "customization_deployment_snapshots": ("snapshot_id",),
    "customization_deployment_audit": ("audit_id",),
}
_WRITE_FENCE = 0x4C45414643555354


def authority_digest(snapshot: Mapping[str, Sequence[Mapping[str, Any]]]) -> str:
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def authority_counts(
    snapshot: Mapping[str, Sequence[Mapping[str, Any]]],
) -> dict[str, int]:
    return {table: len(snapshot.get(table, ())) for table in TABLE_COLUMNS}


def _sqlite_snapshot(path: Path) -> dict[str, list[dict[str, Any]]]:
    resolved = path.resolve(strict=True)
    if resolved.is_symlink() or not resolved.is_file():
        raise RuntimeError("SQLite customization source must be one regular file")
    connection = sqlite3.connect(
        f"file:{resolved.as_posix()}?mode=ro", uri=True, isolation_level=None
    )
    connection.row_factory = sqlite3.Row
    try:
        found = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        missing = sorted(set(TABLE_COLUMNS) - found)
        if missing:
            raise RuntimeError(
                "SQLite customization source is incomplete: " + ",".join(missing)
            )
        return {
            table: [
                dict(row)
                for row in connection.execute(
                    f"SELECT {','.join(columns)} FROM {table} "
                    f"ORDER BY {','.join(_PRIMARY_KEYS[table])}"
                ).fetchall()
            ]
            for table, columns in TABLE_COLUMNS.items()
        }
    finally:
        connection.close()


def _postgres_snapshot(connection: Any) -> dict[str, list[dict[str, Any]]]:
    return {
        table: [
            dict(row)
            for row in connection.execute(
                f"SELECT {','.join(columns)} FROM {table} "
                f"ORDER BY {','.join(_PRIMARY_KEYS[table])}"
            ).fetchall()
        ]
        for table, columns in TABLE_COLUMNS.items()
    }


def _insert_snapshot(
    connection: Any, snapshot: Mapping[str, Sequence[Mapping[str, Any]]]
) -> None:
    for table, columns in TABLE_COLUMNS.items():
        placeholders = ",".join(["%s"] * len(columns))
        statement = (
            f"INSERT INTO {table} ({','.join(columns)}) VALUES ({placeholders})"
        )
        for row in snapshot[table]:
            connection.execute(statement, tuple(row[column] for column in columns))


def reconcile(*, sqlite_path: Path, mode: str) -> dict[str, Any]:
    source = _sqlite_snapshot(sqlite_path)
    source_digest = authority_digest(source)
    database = _platform_db()
    database.assert_schema_current()
    with database.transaction(isolation="serializable") as connection:
        connection.execute("SELECT pg_advisory_xact_lock(%s)", (_WRITE_FENCE,))
        target = _postgres_snapshot(connection)
        target_digest = authority_digest(target)
        if mode == "backfill":
            if any(authority_counts(target).values()):
                if target_digest != source_digest:
                    raise RuntimeError(
                        "PostgreSQL customization authority is nonempty and differs"
                    )
            else:
                _insert_snapshot(connection, source)
                target = _postgres_snapshot(connection)
                target_digest = authority_digest(target)
        if target_digest != source_digest:
            raise RuntimeError("customization authority parity failed")
        return {
            "schema": "leaf.customization-authority-reconciliation.v1",
            "mode": mode,
            "digest": source_digest,
            "counts": authority_counts(source),
            "parity": True,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("backfill", "parity"), required=True)
    parser.add_argument("--sqlite", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = reconcile(sqlite_path=args.sqlite, mode=args.mode)
    except (OSError, RuntimeError, sqlite3.DatabaseError) as exc:
        print(f"customization authority reconciliation failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
