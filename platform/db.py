"""Database access: lazy shared connection pool + cursor helpers.

Mirrors the cadwalk-studio tenancy idiom (shared-pool lazy connect, injectable
client). Reads DATABASE_URL from the environment, falling back to
platform/.env.local (gitignored). Prepared statements are disabled
(prepare_threshold=None) so the store works over a pgbouncer/Neon pooled endpoint
as well as a direct one.
"""
from __future__ import annotations

import os
import re
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
        "async_jobs": {
            "job_id", "tenant_id", "tool", "params_json", "dwg", "status",
            "progress", "created_at", "started_at", "updated_at", "finished_at",
            "elapsed_ms", "result_json", "error_json", "execution_json", "attempt",
            "lease_owner", "lease_expires_at", "heartbeat_at", "provenance_json",
            "terminal_fingerprint", "terminal_conflict_json", "org_id", "project_id",
            "authority_mode", "idempotency_key", "submission_fingerprint", "dwg_version",
        },
        "async_job_terminal_conflicts": {
            "job_id", "fingerprint", "evidence_json", "received_at",
        },
    },
    "callback_replay": {
        "callback_consumed_nonces": {
            "job_id", "nonce", "expires_at", "consumed_at",
        },
    },
    "sessions": {
        "app_sessions": {
            "session_id", "tenant_id", "drawing_id", "status", "created_at",
            "updated_at", "last_seq", "active_turn_id", "turn_started_at",
            "active_turn_tier", "model",
        },
        "app_session_events": {
            "session_id", "seq", "turn_id", "type", "data_json", "created_at",
        },
        "app_approvals": {
            "confirmation_id", "session_id", "tenant_id", "turn_id", "tool",
            "params_json", "capability", "rationale", "kind", "payload_json",
            "decided", "approved", "decided_by", "created_at", "expires_at", "consumed",
        },
    },
    "agent": {
        "agent_approvals": {
            "confirmation_id", "tenant_id", "session_id", "turn_id", "action", "args",
            "args_hash", "policy", "rung", "created_at", "expires_at", "granted",
            "denied", "decided_at", "decided_by", "reason", "consumed_at",
        },
        "agent_session_grants": {
            "tenant_id", "session_id", "action", "target_key", "granted_at",
        },
        "agent_rate_counters": {"namespace", "counter_key", "value", "updated_at"},
        "agent_fleet_state": {"state_key", "active", "reason", "updated_at", "updated_by"},
        "agent_gate_audit_events": {
            "event_id", "tenant_id", "session_id", "turn_id", "kind", "event", "created_at",
        },
        "agent_tenant_state": {
            "tenant_id", "agent_disabled", "overlay", "revision", "updated_at",
        },
        "agent_usage_turns": {
            "usage_key", "tenant_id", "session_id", "turn_id", "ts", "record", "created_at",
        },
    },
    "broker": {
        "broker_tenants": {"tenant_id", "disabled", "tier", "updated_at"},
        "broker_usage_ledger": {
            "event_key", "ts", "tenant_id", "tool", "engine_op", "aps_endpoint",
            "aps_live", "engine_seconds", "usd_est", "status", "inserted_at",
        },
        "broker_run_admissions": {
            "event_key", "tenant_id", "request_fingerprint", "state", "lease_token",
            "lease_expires_at", "aps_live", "accounted_work", "reserved_usd",
            "execution_started_at", "result_json", "http_status", "created_at",
            "updated_at", "terminal_at",
        },
        "broker_aps_slots": {
            "event_key", "tenant_id", "state", "acquired_at", "lease_expires_at",
            "released_at", "release_reason",
        },
        "broker_admission_resolution_audit": {
            "audit_id", "event_key", "tenant_id", "resolution", "operator_id", "reason",
            "evidence_ref", "prior_state", "terminal_status", "created_at",
        },
    },
    "guest_caps": {
        "guest_upload_counters": {"namespace", "counter_key", "value", "updated_at"},
    },
    "drawing": {
        "drawing_store_manifests": {
            "tenant_id", "drawing_id", "head", "latest", "checkout_holder",
            "checkout_fence", "checkout_acquired_at", "checkout_expires_at",
            "created_at", "updated_at",
        },
        "drawing_store_versions": {
            "tenant_id", "drawing_id", "version", "parent_version", "object_key",
            "byte_count", "content_sha256", "workitem_id", "tool", "note", "state",
            "reservation_token", "reservation_expires_at", "created_at", "ready_at",
            "intake_ref", "intake_sha256",
        },
    },
    "upload": {
        "drawing_store_manifests": {
            "tenant_id", "drawing_id", "head", "latest", "checkout_holder",
            "checkout_fence", "checkout_acquired_at", "checkout_expires_at",
            "created_at", "updated_at",
        },
        "drawing_store_versions": {
            "tenant_id", "drawing_id", "version", "parent_version", "object_key",
            "byte_count", "content_sha256", "workitem_id", "tool", "note", "state",
            "reservation_token", "reservation_expires_at", "created_at", "ready_at",
            "intake_ref", "intake_sha256",
        },
        "drawing_upload_attempts": {
            "tenant_id", "drawing_id", "attempt", "marker", "status",
            "retention_expires_at", "extraction_owner", "extraction_fence",
            "extraction_expires_at", "purge_owner", "purge_fence", "purge_expires_at",
            "updated_at",
        },
        "drawing_purge_receipts": {
            "tenant_id", "drawing_id", "attempt", "purge_fence", "status",
            "freed_bytes", "detail", "created_at",
        },
    },
    "harness_sessions": {
        "harness_sessions": {
            "session_id", "tenant_id", "drawing_id", "sdk_session_id", "status",
            "summary", "created_at", "updated_at",
        },
        "harness_turns": {
            "turn_id", "session_id", "seq_start", "status", "stop_reason",
            "started_at", "ended_at",
        },
        "harness_events": {"session_id", "seq", "turn_id", "type", "data", "ts"},
        "harness_confirmations": {
            "confirmation_id", "session_id", "turn_id", "action", "args_json", "kind",
            "status", "created_at", "expires_at", "decided_at", "decided_by",
        },
        "harness_usage": {"usage_id", "session_id", "turn_id", "usage", "ts"},
        "harness_tenant_repo_leases": {
            "tenant_id", "owner_token", "generation", "acquired_at", "heartbeat_at", "expires_at",
        },
    },
}

# Each catalog contract names the owning relation and stable semantic fragments
# from PostgreSQL's canonical pg_get_*def output.  A same-name object elsewhere
# in the schema, or an object whose definition drifted, must not satisfy
# readiness.
def _catalog_contract(relation: str, *definition_fragments: str) -> Dict[str, Any]:
    if not definition_fragments:
        raise ValueError("catalog contracts require a definition")
    return {
        "relation": relation,
        "definition_fragments": definition_fragments,
    }


# Names below are stable database API. They enforce idempotency, fencing, state
# shape, referential integrity, and immutable audit records used by callers.
_AUTHORITY_REQUIRED_CONSTRAINTS = {
    "jobs": {
        "async_jobs_pkey": _catalog_contract("async_jobs", "PRIMARY KEY (job_id)"),
        "async_job_terminal_conflicts_pkey": _catalog_contract(
            "async_job_terminal_conflicts", "PRIMARY KEY (job_id, fingerprint)"),
        "async_job_terminal_conflicts_job_id_fkey": _catalog_contract(
            "async_job_terminal_conflicts",
            "FOREIGN KEY (job_id) REFERENCES async_jobs(job_id) ON DELETE CASCADE"),
    },
    "callback_replay": {
        "callback_consumed_nonces_pkey": _catalog_contract(
            "callback_consumed_nonces", "PRIMARY KEY (job_id, nonce)"),
    },
    "sessions": {
        "app_sessions_pkey": _catalog_contract(
            "app_sessions", "PRIMARY KEY (session_id)"),
        "app_sessions_tenant_id_drawing_id_key": _catalog_contract(
            "app_sessions", "UNIQUE (tenant_id, drawing_id)"),
        "app_session_events_pkey": _catalog_contract(
            "app_session_events", "PRIMARY KEY (session_id, seq)"),
        "app_session_events_session_id_fkey": _catalog_contract(
            "app_session_events",
            "FOREIGN KEY (session_id) REFERENCES app_sessions(session_id) ON DELETE CASCADE"),
        "app_approvals_pkey": _catalog_contract(
            "app_approvals", "PRIMARY KEY (confirmation_id)"),
    },
    "agent": {
        "agent_approvals_pkey": _catalog_contract(
            "agent_approvals", "PRIMARY KEY (confirmation_id)"),
        "agent_session_grants_pkey": _catalog_contract(
            "agent_session_grants",
            "PRIMARY KEY (tenant_id, session_id, action, target_key)"),
        "agent_rate_counters_pkey": _catalog_contract(
            "agent_rate_counters", "PRIMARY KEY (namespace, counter_key)"),
        "agent_fleet_state_pkey": _catalog_contract(
            "agent_fleet_state", "PRIMARY KEY (state_key)"),
        "agent_gate_audit_events_pkey": _catalog_contract(
            "agent_gate_audit_events", "PRIMARY KEY (event_id)"),
        "agent_tenant_state_pkey": _catalog_contract(
            "agent_tenant_state", "PRIMARY KEY (tenant_id)"),
        "agent_usage_turns_pkey": _catalog_contract(
            "agent_usage_turns", "PRIMARY KEY (usage_key)"),
        "agent_usage_turns_tenant_id_session_id_turn_id_key": _catalog_contract(
            "agent_usage_turns", "UNIQUE (tenant_id, session_id, turn_id)"),
    },
    "broker": {
        "broker_run_admissions_event_tenant_uq": _catalog_contract(
            "broker_run_admissions", "UNIQUE (event_key, tenant_id)"),
        "broker_run_admissions_state_allowed": _catalog_contract(
            "broker_run_admissions", "CHECK", "state", "leased", "executing", "terminal"),
        "broker_run_admissions_state_shape": _catalog_contract(
            "broker_run_admissions", "CHECK", "state = 'leased'",
            "state = 'executing'", "state = 'terminal'"),
        "broker_usage_ledger_admission_fk": _catalog_contract(
            "broker_usage_ledger",
            "FOREIGN KEY (event_key, tenant_id)",
            "REFERENCES broker_run_admissions(event_key, tenant_id)"),
        "broker_usage_ledger_engine_seconds_nonnegative_finite": _catalog_contract(
            "broker_usage_ledger", "CHECK", "engine_seconds >= 0", "infinity"),
        "broker_usage_ledger_usd_est_nonnegative_finite": _catalog_contract(
            "broker_usage_ledger", "CHECK", "usd_est >= 0", "infinity"),
        "broker_aps_slots_admission_fk": _catalog_contract(
            "broker_aps_slots", "FOREIGN KEY (event_key, tenant_id)",
            "REFERENCES broker_run_admissions(event_key, tenant_id)"),
        "broker_aps_slots_state_allowed": _catalog_contract(
            "broker_aps_slots", "CHECK", "state", "held", "released"),
        "broker_aps_slots_state_shape": _catalog_contract(
            "broker_aps_slots", "CHECK", "state = 'held'", "state = 'released'"),
        "broker_admission_resolution_allowed": _catalog_contract(
            "broker_admission_resolution_audit", "CHECK", "resolution",
            "confirmed_failed_no_charge", "verified_terminal"),
        "broker_admission_resolution_prior_state": _catalog_contract(
            "broker_admission_resolution_audit", "CHECK", "prior_state = 'executing'"),
        "broker_admission_resolution_admission_fk": _catalog_contract(
            "broker_admission_resolution_audit",
            "FOREIGN KEY (event_key, tenant_id)",
            "REFERENCES broker_run_admissions(event_key, tenant_id)"),
    },
    "guest_caps": {
        "guest_upload_counters_pkey": _catalog_contract(
            "guest_upload_counters", "PRIMARY KEY (namespace, counter_key)"),
    },
    "drawing": {
        "drawing_store_manifests_pkey": _catalog_contract(
            "drawing_store_manifests", "PRIMARY KEY (tenant_id, drawing_id)"),
        "drawing_store_versions_pkey": _catalog_contract(
            "drawing_store_versions", "PRIMARY KEY (tenant_id, drawing_id, version)"),
        "drawing_store_versions_object_key_key": _catalog_contract(
            "drawing_store_versions", "UNIQUE (object_key)"),
        "drawing_store_versions_tenant_id_drawing_id_fkey": _catalog_contract(
            "drawing_store_versions", "FOREIGN KEY (tenant_id, drawing_id)",
            "REFERENCES drawing_store_manifests(tenant_id, drawing_id)",
            "ON DELETE CASCADE"),
        "drawing_store_versions_state_check": _catalog_contract(
            "drawing_store_versions", "CHECK", "state", "reserved", "ready", "orphaned"),
    },
    "upload": {
        "drawing_store_manifests_pkey": _catalog_contract(
            "drawing_store_manifests", "PRIMARY KEY (tenant_id, drawing_id)"),
        "drawing_store_versions_pkey": _catalog_contract(
            "drawing_store_versions", "PRIMARY KEY (tenant_id, drawing_id, version)"),
        "drawing_store_versions_object_key_key": _catalog_contract(
            "drawing_store_versions", "UNIQUE (object_key)"),
        "drawing_store_versions_tenant_id_drawing_id_fkey": _catalog_contract(
            "drawing_store_versions", "FOREIGN KEY (tenant_id, drawing_id)",
            "REFERENCES drawing_store_manifests(tenant_id, drawing_id)",
            "ON DELETE CASCADE"),
        "drawing_store_versions_state_check": _catalog_contract(
            "drawing_store_versions", "CHECK", "state", "reserved", "ready", "orphaned"),
        "drawing_upload_attempts_pkey": _catalog_contract(
            "drawing_upload_attempts", "PRIMARY KEY (tenant_id, drawing_id)"),
        "drawing_upload_attempts_status_check": _catalog_contract(
            "drawing_upload_attempts", "CHECK", "status", "extracting", "ready",
            "failed", "purging", "purged"),
        "drawing_purge_receipts_pkey": _catalog_contract(
            "drawing_purge_receipts",
            "PRIMARY KEY (tenant_id, drawing_id, attempt, purge_fence)"),
        "drawing_purge_receipts_status_check": _catalog_contract(
            "drawing_purge_receipts", "CHECK", "status", "deleted", "failed"),
    },
    "harness_sessions": {
        "harness_sessions_pkey": _catalog_contract(
            "harness_sessions", "PRIMARY KEY (session_id)"),
        "harness_turns_pkey": _catalog_contract(
            "harness_turns", "PRIMARY KEY (session_id, turn_id)"),
        "harness_turns_session_id_fkey": _catalog_contract(
            "harness_turns", "FOREIGN KEY (session_id)",
            "REFERENCES harness_sessions(session_id)", "ON DELETE CASCADE"),
        "harness_events_pkey": _catalog_contract(
            "harness_events", "PRIMARY KEY (session_id, seq)"),
        "harness_events_session_id_fkey": _catalog_contract(
            "harness_events", "FOREIGN KEY (session_id)",
            "REFERENCES harness_sessions(session_id)", "ON DELETE CASCADE"),
        "harness_confirmations_pkey": _catalog_contract(
            "harness_confirmations", "PRIMARY KEY (confirmation_id)"),
        "harness_confirmations_session_id_fkey": _catalog_contract(
            "harness_confirmations", "FOREIGN KEY (session_id)",
            "REFERENCES harness_sessions(session_id)", "ON DELETE CASCADE"),
        "harness_usage_pkey": _catalog_contract(
            "harness_usage", "PRIMARY KEY (usage_id)"),
        "harness_usage_session_id_fkey": _catalog_contract(
            "harness_usage", "FOREIGN KEY (session_id)",
            "REFERENCES harness_sessions(session_id)", "ON DELETE CASCADE"),
        "harness_tenant_repo_leases_pkey": _catalog_contract(
            "harness_tenant_repo_leases", "PRIMARY KEY (tenant_id)"),
    },
}

_AUTHORITY_REQUIRED_INDEXES = {
    "jobs": {
        "async_jobs_project_idempotency_uq": _catalog_contract(
            "async_jobs", "CREATE UNIQUE INDEX", "(tenant_id, project_id, idempotency_key)",
            "WHERE", "project_id IS NOT NULL", "idempotency_key IS NOT NULL"),
        "async_jobs_reclaim_idx": _catalog_contract(
            "async_jobs", "(status, lease_expires_at, updated_at)",
            "WHERE", "status", "submitted", "running"),
    },
    "callback_replay": {
        "callback_consumed_nonces_expiry_idx": _catalog_contract(
            "callback_consumed_nonces", "(expires_at)"),
    },
    "sessions": {
        "idx_app_session_events_recent": _catalog_contract(
            "app_session_events", "(session_id, seq DESC)"),
        "idx_app_approvals_session": _catalog_contract(
            "app_approvals", "(session_id, created_at DESC)"),
    },
    "agent": {
        "idx_agent_approvals_pending": _catalog_contract(
            "agent_approvals", "(tenant_id, expires_at)", "WHERE",
            "granted = false", "denied = false"),
        "idx_agent_gate_audit_tenant_created": _catalog_contract(
            "agent_gate_audit_events", "(tenant_id, created_at)"),
        "idx_agent_gate_audit_session_created": _catalog_contract(
            "agent_gate_audit_events", "(session_id, created_at)"),
        "idx_agent_usage_tenant_ts": _catalog_contract(
            "agent_usage_turns", "(tenant_id, ts)"),
    },
    "broker": {
        "broker_usage_ledger_tenant_ts_idx": _catalog_contract(
            "broker_usage_ledger", "(tenant_id, ts)"),
        "broker_run_admissions_tenant_state_idx": _catalog_contract(
            "broker_run_admissions", "(tenant_id, state)"),
        "broker_aps_slots_held_idx": _catalog_contract(
            "broker_aps_slots", "(state, lease_expires_at)"),
    },
    "guest_caps": {
        "idx_guest_upload_counters_updated": _catalog_contract(
            "guest_upload_counters", "(updated_at)"),
    },
    "drawing": {
        "drawing_store_versions_ready_idx": _catalog_contract(
            "drawing_store_versions", "(tenant_id, drawing_id, version)",
            "WHERE", "state = 'ready'"),
    },
    "upload": {
        "drawing_store_versions_ready_idx": _catalog_contract(
            "drawing_store_versions", "(tenant_id, drawing_id, version)",
            "WHERE", "state = 'ready'"),
        "drawing_upload_expiry_idx": _catalog_contract(
            "drawing_upload_attempts", "(retention_expires_at)", "WHERE",
            "status <> 'purged'", "retention_expires_at IS NOT NULL"),
    },
    "harness_sessions": {
        "harness_one_active_session": _catalog_contract(
            "harness_sessions", "CREATE UNIQUE INDEX", "(tenant_id, drawing_id)",
            "WHERE", "status <> 'archived'"),
        "harness_one_active_turn": _catalog_contract(
            "harness_turns", "CREATE UNIQUE INDEX", "(session_id)",
            "WHERE", "status = 'active'"),
        "idx_harness_events_turn": _catalog_contract(
            "harness_events", "(session_id, turn_id, seq)"),
        "idx_harness_confirmations_session": _catalog_contract(
            "harness_confirmations", "(session_id, status, expires_at)"),
        "idx_harness_usage_session": _catalog_contract(
            "harness_usage", "(session_id, ts)"),
        "idx_harness_tenant_repo_leases_expiry": _catalog_contract(
            "harness_tenant_repo_leases", "(expires_at)"),
    },
}

_AUTHORITY_REQUIRED_TRIGGERS = {
    "broker": {
        "broker_usage_ledger_immutable": _catalog_contract(
            "broker_usage_ledger", "BEFORE UPDATE OR DELETE",
            "EXECUTE FUNCTION leaf_reject_broker_ledger_mutation()"),
        "broker_admission_resolution_audit_immutable": _catalog_contract(
            "broker_admission_resolution_audit", "BEFORE UPDATE OR DELETE",
            "EXECUTE FUNCTION leaf_reject_broker_ledger_mutation()"),
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


def required_catalog_for_selected_authorities(
    environ: Optional[Mapping[str, str]] = None,
) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """Return runtime-critical constraints, indexes, and triggers by selector."""
    source = os.environ if environ is None else environ
    catalog: Dict[str, Dict[str, Dict[str, Any]]] = {
        "constraints": {}, "indexes": {}, "triggers": {},
    }
    contracts = {
        "constraints": _AUTHORITY_REQUIRED_CONSTRAINTS,
        "indexes": _AUTHORITY_REQUIRED_INDEXES,
        "triggers": _AUTHORITY_REQUIRED_TRIGGERS,
    }
    for selector, values in _AUTHORITY_SELECTORS.items():
        authority = values.get(str(source.get(selector, "")).strip().lower())
        if authority is None:
            continue
        for kind, authority_contracts in contracts.items():
            catalog[kind].update(authority_contracts.get(authority, {}))
    return catalog


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


def _normalize_catalog_definition(value: Any) -> str:
    """Normalize pg_get_*def output without weakening semantic comparison."""
    normalized = str(value or "").replace('"', "").lower()
    normalized = re.sub(r"\b[a-z_][a-z0-9_]*\.", "", normalized)
    # PostgreSQL may add redundant grouping parentheses and type casts around
    # literals when it deparses pg_node_tree. Contracts compare stable tokens,
    # so remove only whitespace and grouping punctuation from both sides.
    normalized = re.sub(r"\s+", "", normalized)
    normalized = normalized.replace("(", "").replace(")", "")
    return normalized


def _catalog_contract_errors(
    required_catalog: Mapping[str, Mapping[str, Mapping[str, Any]]],
    found_catalog: Mapping[str, List[Mapping[str, Any]]],
) -> Dict[str, List[str]]:
    """Return missing or malformed selected catalog objects by object kind."""
    errors: Dict[str, List[str]] = {}
    for kind, contracts in required_catalog.items():
        rows_by_name: Dict[str, List[Mapping[str, Any]]] = {}
        name_field = {
            "constraints": "conname",
            "indexes": "indexname",
            "triggers": "tgname",
        }[kind]
        for row in found_catalog.get(kind, []):
            rows_by_name.setdefault(str(row[name_field]), []).append(row)

        invalid: List[str] = []
        for name, contract in contracts.items():
            candidates = rows_by_name.get(name, [])
            matching_relation = [
                row for row in candidates
                if str(row.get("relation_name")) == contract["relation"]
            ]
            if not matching_relation:
                invalid.append(f"{name}:missing-or-wrong-relation")
                continue

            row = matching_relation[0]
            if kind == "constraints" and not bool(row.get("validated")):
                invalid.append(f"{name}:not-validated")
                continue
            if kind == "indexes" and (
                not bool(row.get("valid")) or not bool(row.get("ready"))
            ):
                invalid.append(f"{name}:not-valid-ready")
                continue
            if kind == "triggers" and str(row.get("enabled")) not in {"O", "A"}:
                invalid.append(f"{name}:disabled")
                continue

            definition = _normalize_catalog_definition(row.get("definition"))
            fragments = [
                _normalize_catalog_definition(fragment)
                for fragment in contract["definition_fragments"]
            ]
            if any(fragment not in definition for fragment in fragments):
                invalid.append(f"{name}:definition-mismatch")
        errors[f"invalid_{kind}"] = sorted(invalid)
    return errors


def schema_status() -> Dict[str, Any]:
    """Read-only readiness result for the API and canonical worker schema."""
    required = required_columns_for_selected_authorities()
    required_catalog = required_catalog_for_selected_authorities()
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
            cur.execute(
                "SELECT c.conname, r.relname AS relation_name, "
                "c.convalidated AS validated, "
                "pg_get_constraintdef(c.oid, true) AS definition "
                "FROM pg_constraint AS c "
                "JOIN pg_class AS r ON r.oid = c.conrelid "
                "WHERE c.connamespace = current_schema()::regnamespace "
                "AND c.conname = ANY(%(constraints)s)",
                {"constraints": list(required_catalog["constraints"])},
            )
            constraint_rows = cur.fetchall()
            cur.execute(
                "SELECT idx.relname AS indexname, tbl.relname AS relation_name, "
                "i.indisvalid AS valid, i.indisready AS ready, "
                "pg_get_indexdef(i.indexrelid, 0, true) AS definition "
                "FROM pg_index AS i "
                "JOIN pg_class AS idx ON idx.oid = i.indexrelid "
                "JOIN pg_class AS tbl ON tbl.oid = i.indrelid "
                "JOIN pg_namespace AS ns ON ns.oid = idx.relnamespace "
                "WHERE ns.nspname = current_schema() "
                "AND idx.relname = ANY(%(indexes)s)",
                {"indexes": list(required_catalog["indexes"])},
            )
            index_rows = cur.fetchall()
            cur.execute(
                "SELECT t.tgname, r.relname AS relation_name, "
                "t.tgenabled AS enabled, pg_get_triggerdef(t.oid, true) AS definition "
                "FROM pg_trigger AS t "
                "JOIN pg_class AS r ON r.oid = t.tgrelid "
                "JOIN pg_namespace AS ns ON ns.oid = r.relnamespace "
                "WHERE ns.nspname = current_schema() AND NOT t.tgisinternal "
                "AND t.tgname = ANY(%(triggers)s)",
                {"triggers": list(required_catalog["triggers"])},
            )
            trigger_rows = cur.fetchall()
            catalog_errors = _catalog_contract_errors(
                required_catalog,
                {
                    "constraints": constraint_rows,
                    "indexes": index_rows,
                    "triggers": trigger_rows,
                },
            )
            applied_rows = []
            if not (_MIGRATION_LEDGER_COLUMNS - found.get(_MIGRATION_LEDGER_TABLE, set())):
                cur.execute(
                    f"SELECT name, sha256 FROM {_MIGRATION_LEDGER_TABLE} ORDER BY name")
                applied_rows = cur.fetchall()
            migration_proof = _migration_proof(applied_rows)
            cur.execute("SELECT current_database() AS database, current_schema() AS schema")
            identity = cur.fetchone()
    ok = not missing and not any(catalog_errors.values()) \
        and not migration_proof["missing_migrations"] \
        and not migration_proof["migration_hash_drift"]
    return {
        "ok": ok,
        "database": identity["database"],
        "schema": identity["schema"],
        "migration_count": len(migration_manifest()),
        "missing": missing,
        **catalog_errors,
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
        for field, label in (
            ("invalid_constraints", "invalid constraints"),
            ("invalid_indexes", "invalid indexes"),
            ("invalid_triggers", "invalid triggers"),
        ):
            if status.get(field):
                details.append(label + ": " + ",".join(status[field]))
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
