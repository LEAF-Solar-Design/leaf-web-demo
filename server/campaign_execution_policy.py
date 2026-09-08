"""Completion's read-only broker policy, without importing the broker app."""
from __future__ import annotations

import os


def production_runtime() -> bool:
    return os.environ.get("LEAF_RUNTIME_ENV", "").strip().lower() == "production"


def authored_execution_enabled() -> bool:
    raw = os.environ.get("LEAF_AUTHORED_EXECUTION")
    if raw is None:
        return not production_runtime()
    return raw.strip().lower() in ("1", "true", "yes", "on")


def sandbox_configured() -> bool:
    return os.environ.get("LEAF_TOOL_SANDBOX_PROVIDER", "").strip().lower() == "e2b"


def tenant_disabled(tenant_id: str) -> bool:
    # Completion requires durable jobs and the shared broker authority. Never
    # substitute a local tenant file when that authority cannot be read.
    if os.environ.get("LEAF_BROKER_STORE", "legacy").strip().lower() != "postgres":
        raise RuntimeError("completion requires PostgreSQL broker authority")
    from broker_pg_store import get_store

    record = get_store().tenant(tenant_id)
    if record is None:
        return False  # PostgreSQL: a never-provisioned tenant is not killed.
    # PostgreSQL supplies a boolean column. Unknown or corrupt state is killed.
    return not isinstance(record, dict) or record.get("disabled") is not False
