"""PostgreSQL authority for broker tenant state and usage accounting.

The module is imported only when ``LEAF_BROKER_STORE=postgres``. This keeps the
database-free broker demo unchanged and avoids loading platform dependencies in
legacy mode.
"""
from __future__ import annotations

import importlib.util
import json
import math
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

_store: Optional["PostgresBrokerStore"] = None


def _load_db():
    if "leaf_platform" not in sys.modules:
        pkg_dir = Path(__file__).resolve().parent.parent / "platform"
        spec = importlib.util.spec_from_file_location(
            "leaf_platform",
            pkg_dir / "__init__.py",
            submodule_search_locations=[str(pkg_dir)],
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load the leaf_platform package")
        module = importlib.util.module_from_spec(spec)
        sys.modules["leaf_platform"] = module
        spec.loader.exec_module(module)
    import leaf_platform.db as db  # noqa: PLC0415

    return db


class PostgresBrokerStore:
    """Small synchronous store used by the synchronous broker routes."""

    def __init__(self, db: Any = None) -> None:
        self._db = db or _load_db()

    def validate_schema(self) -> None:
        self._db.assert_schema_current()
        manifest = {
            item["name"] for item in self._db.migration_manifest()
        }
        if "0014_broker.sql" not in manifest:
            raise RuntimeError(
                "PostgreSQL broker migration 0014 is not shipped with this image")
        required_columns = {
            "broker_tenants": {"tenant_id", "disabled", "tier", "updated_at"},
            "broker_run_admissions": {
                "event_key", "tenant_id", "request_fingerprint", "state",
                "lease_token", "lease_expires_at", "aps_live", "accounted_work",
                "reserved_usd", "execution_started_at", "result_json",
                "http_status", "created_at", "updated_at", "terminal_at",
            },
            "broker_usage_ledger": {
                "event_key", "ts", "tenant_id", "tool", "engine_op",
                "aps_endpoint", "aps_live", "engine_seconds", "usd_est",
                "status", "inserted_at",
            },
            "broker_aps_slots": {
                "event_key", "tenant_id", "state", "acquired_at",
                "lease_expires_at", "released_at", "release_reason",
            },
            "broker_admission_resolution_audit": {
                "audit_id", "event_key", "tenant_id", "resolution",
                "operator_id", "reason", "evidence_ref", "prior_state",
                "terminal_status", "created_at",
            },
        }
        required_constraints = {
            "broker_run_admissions_event_tenant_uq",
            "broker_run_admissions_state_allowed",
            "broker_run_admissions_state_shape",
            "broker_usage_ledger_admission_fk",
            "broker_usage_ledger_engine_seconds_nonnegative_finite",
            "broker_usage_ledger_usd_est_nonnegative_finite",
            "broker_aps_slots_admission_fk",
            "broker_aps_slots_state_allowed",
            "broker_aps_slots_state_shape",
            "broker_admission_resolution_allowed",
            "broker_admission_resolution_prior_state",
            "broker_admission_resolution_admission_fk",
        }
        required_triggers = {
            "broker_usage_ledger_immutable",
            "broker_admission_resolution_audit_immutable",
        }
        with self._db.get_pool().connection() as conn:
            column_rows = conn.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = ANY(%(tables)s)",
                {"tables": list(required_columns)},
            ).fetchall()
            constraint_rows = conn.execute(
                "SELECT conname FROM pg_constraint "
                "WHERE connamespace = current_schema()::regnamespace "
                "AND conname = ANY(%(constraints)s)",
                {"constraints": list(required_constraints)},
            ).fetchall()
            trigger_rows = conn.execute(
                "SELECT tgname FROM pg_trigger "
                "WHERE NOT tgisinternal AND tgname = ANY(%(triggers)s)",
                {"triggers": list(required_triggers)},
            ).fetchall()
        found_columns: Dict[str, set[str]] = {}
        for row in column_rows:
            found_columns.setdefault(str(row["table_name"]), set()).add(
                str(row["column_name"]))
        missing_columns = {
            table: sorted(columns - found_columns.get(table, set()))
            for table, columns in required_columns.items()
            if columns - found_columns.get(table, set())
        }
        found_constraints = {str(row["conname"]) for row in constraint_rows}
        found_triggers = {str(row["tgname"]) for row in trigger_rows}
        missing_constraints = sorted(required_constraints - found_constraints)
        missing_triggers = sorted(required_triggers - found_triggers)
        if missing_columns or missing_constraints or missing_triggers:
            detail = {
                "columns": missing_columns,
                "constraints": missing_constraints,
                "triggers": missing_triggers,
            }
            raise RuntimeError(
                "PostgreSQL broker schema is incomplete: "
                + json.dumps(detail, sort_keys=True))

    def validate_drawing_schema(self) -> None:
        """Fail closed unless the current image's drawing authority is ready."""
        self._db.assert_schema_current()
        manifest = {
            item["name"] for item in self._db.migration_manifest()
        }
        if "0016_drawing_upload_authority.sql" not in manifest:
            raise RuntimeError(
                "PostgreSQL drawing migration 0016 is not shipped with this image")
        required_columns = {
            "drawing_store_manifests": {
                "tenant_id", "drawing_id", "head", "latest",
                "checkout_holder", "checkout_fence", "checkout_acquired_at",
                "checkout_expires_at", "created_at", "updated_at",
            },
            "drawing_store_versions": {
                "tenant_id", "drawing_id", "version", "parent_version",
                "object_key", "byte_count", "content_sha256", "workitem_id",
                "tool", "note", "state", "reservation_token",
                "reservation_expires_at", "created_at", "ready_at",
            },
            "drawing_upload_attempts": {
                "tenant_id", "drawing_id", "attempt", "marker", "status",
                "retention_expires_at", "extraction_owner",
                "extraction_fence", "extraction_expires_at", "purge_owner",
                "purge_fence", "purge_expires_at", "updated_at",
            },
        }
        required_constraints = {
            "drawing_store_manifests_pkey",
            "drawing_store_versions_pkey",
            "drawing_store_versions_object_key_key",
            "drawing_store_versions_tenant_id_drawing_id_fkey",
            "drawing_store_versions_state_check",
            "drawing_upload_attempts_pkey",
            "drawing_upload_attempts_status_check",
        }
        with self._db.get_pool().connection() as conn:
            column_rows = conn.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema() "
                "AND table_name = ANY(%(tables)s)",
                {"tables": list(required_columns)},
            ).fetchall()
            constraint_rows = conn.execute(
                "SELECT conname FROM pg_constraint "
                "WHERE connamespace = current_schema()::regnamespace "
                "AND conname = ANY(%(constraints)s)",
                {"constraints": list(required_constraints)},
            ).fetchall()
        found_columns: Dict[str, set[str]] = {}
        for row in column_rows:
            found_columns.setdefault(str(row["table_name"]), set()).add(
                str(row["column_name"]))
        missing_columns = {
            table: sorted(columns - found_columns.get(table, set()))
            for table, columns in required_columns.items()
            if columns - found_columns.get(table, set())
        }
        found_constraints = {str(row["conname"]) for row in constraint_rows}
        missing_constraints = sorted(required_constraints - found_constraints)
        if missing_columns or missing_constraints:
            detail = {
                "columns": missing_columns,
                "constraints": missing_constraints,
            }
            raise RuntimeError(
                "PostgreSQL drawing schema is incomplete: "
                + json.dumps(detail, sort_keys=True))

    @staticmethod
    def _validate_ledger_numbers(entry: Dict[str, Any]) -> None:
        for field in ("engine_seconds", "usd_est"):
            value = entry.get(field)
            if value is None:
                continue
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                raise ValueError(
                    f"broker ledger {field} must be finite and nonnegative")

    def tenant(self, tenant_id: str) -> Optional[Dict[str, Any]]:
        with self._db.get_pool().connection() as conn:
            row = conn.execute(
                "SELECT tenant_id, disabled, tier, "
                "EXTRACT(EPOCH FROM updated_at) AS updated_at "
                "FROM broker_tenants WHERE tenant_id = %(tenant_id)s",
                {"tenant_id": str(tenant_id)},
            ).fetchone()
        return dict(row) if row is not None else None

    def set_tenant_disabled(self, tenant_id: str, disabled: bool) -> None:
        with self._db.get_pool().connection() as conn:
            conn.execute(
                "INSERT INTO broker_tenants (tenant_id, disabled, updated_at) "
                "VALUES (%(tenant_id)s, %(disabled)s, NOW()) "
                "ON CONFLICT (tenant_id) DO UPDATE "
                "SET disabled = EXCLUDED.disabled, updated_at = NOW()",
                {"tenant_id": str(tenant_id), "disabled": bool(disabled)},
            )

    def disabled_tenant_ids(self) -> list[str]:
        with self._db.get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT tenant_id FROM broker_tenants "
                "WHERE disabled = TRUE ORDER BY tenant_id"
            ).fetchall()
        return [str(row["tenant_id"]) for row in rows]

    def admit_run(
        self, event_key: str, tenant_id: str, *, aps_live: bool,
        estimated_usd: float, spend_cap: Optional[float],
        daily_limit: Optional[int], request_fingerprint: str,
        lease_seconds: int = 30,
    ) -> Dict[str, Any]:
        """Atomically deduplicate and reserve one tenant's quota.

        Tenant-scoped advisory locking makes the distinct-key quota check and
        reservation one serial operation across every broker task.
        """
        key = str(event_key).strip()
        if not key:
            raise ValueError("broker run event_key must not be empty")
        if lease_seconds < 1 or lease_seconds > 300:
            raise ValueError("broker admission lease must be between 1 and 300 seconds")
        estimate = max(0.0, float(estimated_usd))
        token = str(uuid.uuid4())
        tenant = str(tenant_id)
        fingerprint = str(request_fingerprint)

        def operation(conn):
            reclaim = False
            conn.execute(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended('event:' || %(event_key)s, 0))",
                {"event_key": key},
            )
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%(tenant_id)s, 0))",
                {"tenant_id": tenant},
            )
            existing = conn.execute(
                "SELECT event_key, tenant_id, request_fingerprint, state, lease_token, "
                "lease_expires_at, (lease_expires_at > NOW()) AS lease_active, "
                "result_json, http_status "
                "FROM broker_run_admissions WHERE event_key = %(event_key)s "
                "FOR UPDATE",
                {"event_key": key},
            ).fetchone()
            if existing is not None:
                if str(existing["tenant_id"]) != tenant:
                    return {"status": "collision"}
                if str(existing["request_fingerprint"]) != fingerprint:
                    return {"status": "mismatch"}
                if existing["state"] == "terminal":
                    result = existing["result_json"]
                    if isinstance(result, str):
                        result = json.loads(result)
                    return {
                        "status": "replay",
                        "result": dict(result),
                        "http_status": int(existing["http_status"]),
                    }
                if existing["state"] == "executing":
                    return {"status": "executing"}
                if bool(existing["lease_active"]):
                    return {"status": "leased"}
                reclaim = True

            spent_row = conn.execute(
                "SELECT COALESCE(SUM(usd_est), 0) AS spent "
                "FROM broker_usage_ledger WHERE tenant_id = %(tenant_id)s "
                "AND status NOT IN ('quota_exceeded', 'TENANT_DISABLED')",
                {"tenant_id": tenant},
            ).fetchone()
            reserved_row = conn.execute(
                "SELECT COALESCE(SUM(reserved_usd), 0) AS reserved "
                "FROM broker_run_admissions WHERE tenant_id = %(tenant_id)s "
                "AND (state = 'executing' OR "
                "(state = 'leased' AND lease_expires_at > NOW()))",
                {"tenant_id": tenant},
            ).fetchone()
            spent = float(spent_row["spent"])
            reserved = float(reserved_row["reserved"])

            parameters = {
                "event_key": key, "tenant_id": tenant, "token": token,
                "lease_seconds": lease_seconds, "aps_live": bool(aps_live),
                "estimated_usd": estimate,
                "request_fingerprint": fingerprint,
            }

            def publish_lease(reserved_usd: float) -> None:
                parameters["reserved_usd"] = reserved_usd
                if reclaim:
                    row = conn.execute(
                        "UPDATE broker_run_admissions SET lease_token = %(token)s, "
                        "lease_expires_at = NOW() + "
                        "(%(lease_seconds)s * INTERVAL '1 second'), "
                        "aps_live = %(aps_live)s, reserved_usd = %(reserved_usd)s, "
                        "created_at = NOW(), updated_at = NOW() "
                        "WHERE event_key = %(event_key)s AND state = 'leased' "
                        "RETURNING event_key",
                        parameters,
                    ).fetchone()
                    if row is None:
                        raise RuntimeError("expired broker lease could not be reclaimed")
                else:
                    conn.execute(
                        "INSERT INTO broker_run_admissions "
                        "(event_key, tenant_id, request_fingerprint, state, "
                        "lease_token, lease_expires_at, aps_live, reserved_usd) "
                        "VALUES (%(event_key)s, %(tenant_id)s, "
                        "%(request_fingerprint)s, 'leased', %(token)s, "
                        "NOW() + (%(lease_seconds)s * INTERVAL '1 second'), "
                        "%(aps_live)s, %(reserved_usd)s)",
                        parameters,
                    )

            if spend_cap is not None and spent + reserved + estimate > float(spend_cap) + 1e-12:
                publish_lease(0.0)
                return {
                    "status": "spend_quota", "spent": spent,
                    "reserved": reserved, "estimated_usd": estimate,
                    "cap": float(spend_cap), "lease_token": token,
                }
            if aps_live and daily_limit is not None:
                daily_row = conn.execute(
                    "SELECT COUNT(*) AS used FROM broker_run_admissions "
                    "WHERE tenant_id = %(tenant_id)s AND aps_live = TRUE "
                    "AND created_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC') "
                    "  AT TIME ZONE 'UTC' "
                    "AND (accounted_work = TRUE OR "
                    "(state = 'leased' AND lease_expires_at > NOW()))",
                    {"tenant_id": tenant},
                ).fetchone()
                used = int(daily_row["used"])
                if used >= int(daily_limit):
                    publish_lease(0.0)
                    return {
                        "status": "daily_quota", "used": used,
                        "limit": int(daily_limit), "lease_token": token,
                    }
            publish_lease(estimate)
            return {
                "status": "acquired", "lease_token": token, "reclaimed": reclaim,
            }

        return self._db.run_transaction(operation, isolation="serializable")

    def mark_execution_started(
        self, event_key: str, tenant_id: str, lease_token: str, *,
        aps_live: bool = False, max_concurrency: int = 1,
        slot_lease_seconds: int = 900,
    ) -> bool:
        """Irreversibly cross the boundary after which automatic retry is unsafe."""
        if max_concurrency < 1:
            raise ValueError("APS_MAX_CONCURRENCY must be at least 1")

        def operation(conn):
            if aps_live:
                conn.execute(
                    "SELECT pg_advisory_xact_lock("
                    "hashtextextended('aps-concurrency-slots', 0))"
                )
                count = conn.execute(
                    "SELECT COUNT(*) AS held FROM broker_aps_slots "
                    "WHERE state = 'held'"
                ).fetchone()
                if int(count["held"]) >= int(max_concurrency):
                    conn.execute(
                        "UPDATE broker_run_admissions SET lease_expires_at = NOW(), "
                        "updated_at = NOW() WHERE event_key = %(event_key)s "
                        "AND tenant_id = %(tenant_id)s AND state = 'leased' "
                        "AND lease_token = %(lease_token)s",
                        {
                            "event_key": str(event_key), "tenant_id": str(tenant_id),
                            "lease_token": str(lease_token),
                        },
                    )
                    return False
            row = conn.execute(
                "UPDATE broker_run_admissions SET state = 'executing', "
                "lease_expires_at = NULL, execution_started_at = NOW(), "
                "accounted_work = %(aps_live)s, "
                "updated_at = NOW() WHERE event_key = %(event_key)s "
                "AND tenant_id = %(tenant_id)s AND state = 'leased' "
                "AND lease_token = %(lease_token)s AND lease_expires_at > NOW() "
                "RETURNING event_key",
                {
                    "event_key": str(event_key), "tenant_id": str(tenant_id),
                    "lease_token": str(lease_token), "aps_live": bool(aps_live),
                },
            ).fetchone()
            if row is None:
                raise RuntimeError(
                    "broker execution lease is missing, expired, or not owned")
            if aps_live:
                conn.execute(
                    "INSERT INTO broker_aps_slots "
                    "(event_key, tenant_id, state, lease_expires_at) "
                    "VALUES (%(event_key)s, %(tenant_id)s, 'held', "
                    "NOW() + (%(slot_lease_seconds)s * INTERVAL '1 second'))",
                    {
                        "event_key": str(event_key), "tenant_id": str(tenant_id),
                        "slot_lease_seconds": int(slot_lease_seconds),
                    },
                )
            return True

        return self._db.run_transaction(operation, isolation="serializable")

    def complete_run(
        self, event_key: str, tenant_id: str, lease_token: str,
        entry: Dict[str, Any], result: Dict[str, Any], http_status: int,
    ) -> None:
        """Atomically append accounting and publish the replayable terminal result."""
        key = str(event_key).strip()
        values = dict(entry)
        self._validate_ledger_numbers(values)
        values["event_key"] = key
        values.update({
            "lease_token": str(lease_token),
            "result_json": json.dumps(result, separators=(",", ":"), allow_nan=False),
            "http_status": int(http_status),
        })

        def operation(conn):
            conn.execute(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended('event:' || %(event_key)s, 0))",
                {"event_key": key},
            )
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%(tenant_id)s, 0))",
                {"tenant_id": str(tenant_id)},
            )
            admission = conn.execute(
                "SELECT tenant_id, state, lease_token FROM broker_run_admissions "
                "WHERE event_key = %(event_key)s FOR UPDATE",
                {"event_key": key},
            ).fetchone()
            if admission is None or str(admission["tenant_id"]) != str(tenant_id):
                raise RuntimeError("broker terminal write does not own this event")
            if admission["state"] == "terminal":
                return
            if str(admission["lease_token"]) != str(lease_token):
                raise RuntimeError("broker terminal write has a stale lease token")
            conn.execute(
                "INSERT INTO broker_usage_ledger "
                "(event_key, ts, tenant_id, tool, engine_op, aps_endpoint, "
                "aps_live, engine_seconds, usd_est, status) "
                "VALUES (%(event_key)s, %(ts)s, %(tenant_id)s, %(tool)s, "
                "%(engine_op)s, %(aps_endpoint)s, %(aps_live)s, "
                "%(engine_seconds)s, %(usd_est)s, %(status)s) "
                "ON CONFLICT (event_key) DO NOTHING",
                values,
            )
            updated = conn.execute(
                "UPDATE broker_run_admissions SET state = 'terminal', "
                "lease_expires_at = NULL, reserved_usd = 0, "
                "result_json = %(result_json)s::jsonb, "
                "http_status = %(http_status)s, terminal_at = NOW(), "
                "updated_at = NOW() WHERE event_key = %(event_key)s "
                "AND tenant_id = %(tenant_id)s AND lease_token = %(lease_token)s "
                "AND state IN ('leased', 'executing') RETURNING event_key",
                values,
            ).fetchone()
            if updated is None:
                raise RuntimeError("broker terminal state transition failed")
            conn.execute(
                "UPDATE broker_aps_slots SET state = 'released', "
                "released_at = NOW(), release_reason = 'known_terminal' "
                "WHERE event_key = %(event_key)s AND state = 'held'",
                {"event_key": key},
            )

        self._db.run_transaction(operation, isolation="serializable")

    def spent_usd(self, tenant_id: str) -> float:
        with self._db.get_pool().connection() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(usd_est), 0) AS spent "
                "FROM broker_usage_ledger "
                "WHERE tenant_id = %(tenant_id)s "
                "AND status NOT IN ('quota_exceeded', 'TENANT_DISABLED')",
                {"tenant_id": str(tenant_id)},
            ).fetchone()
        return round(float(row["spent"]), 6)

    def daily_live_run_count(self, tenant_id: str) -> int:
        with self._db.get_pool().connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS used FROM broker_usage_ledger "
                "WHERE tenant_id = %(tenant_id)s AND aps_live = TRUE "
                "AND status NOT IN ('quota_exceeded', 'TENANT_DISABLED', "
                "'RECONCILED_FAILED_NO_CHARGE') "
                "AND (to_timestamp(ts) AT TIME ZONE 'UTC')::date = "
                "(NOW() AT TIME ZONE 'UTC')::date",
                {"tenant_id": str(tenant_id)},
            ).fetchone()
        return int(row["used"])

    def aggregate_usage(self, tenant_id: str) -> Dict[str, Any]:
        """Frozen tenant usage shape, matching da.usage.aggregate_usage."""
        with self._db.get_pool().connection() as conn:
            row = conn.execute(
                "SELECT "
                "COUNT(*) FILTER (WHERE status NOT IN "
                "('quota_exceeded', 'TENANT_DISABLED')) AS total_runs, "
                "COALESCE(SUM(usd_est) FILTER (WHERE status NOT IN "
                "('quota_exceeded', 'TENANT_DISABLED')), 0) AS total_usd, "
                "COUNT(*) FILTER (WHERE status NOT IN "
                "('quota_exceeded', 'TENANT_DISABLED') AND "
                "(to_timestamp(ts) AT TIME ZONE 'UTC')::date = "
                "(NOW() AT TIME ZONE 'UTC')::date) AS today_runs, "
                "COALESCE(SUM(usd_est) FILTER (WHERE status NOT IN "
                "('quota_exceeded', 'TENANT_DISABLED') AND "
                "(to_timestamp(ts) AT TIME ZONE 'UTC')::date = "
                "(NOW() AT TIME ZONE 'UTC')::date), 0) AS today_usd "
                "FROM broker_usage_ledger WHERE tenant_id = %(tenant_id)s",
                {"tenant_id": str(tenant_id)},
            ).fetchone()
        return {
            "today": {
                "runs": int(row["today_runs"]),
                "usd_est": round(float(row["today_usd"]), 6),
            },
            "total": {
                "runs": int(row["total_runs"]),
                "usd_est": round(float(row["total_usd"]), 6),
            },
        }

    def usage_tenant_ids(self) -> list[str]:
        """Every tenant represented in the broker ledger, including denials."""
        with self._db.get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT tenant_id FROM broker_usage_ledger "
                "ORDER BY tenant_id"
            ).fetchall()
        return [str(row["tenant_id"]) for row in rows]

    def list_executing(self, limit: int = 100) -> list[Dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 500))
        with self._db.get_pool().connection() as conn:
            rows = conn.execute(
                "SELECT admission.tenant_id, admission.event_key, "
                "admission.request_fingerprint, admission.aps_live, "
                "admission.reserved_usd, admission.execution_started_at, "
                "EXTRACT(EPOCH FROM (NOW() - admission.execution_started_at)) "
                "AS age_seconds, "
                "slot.lease_expires_at AS slot_lease_expires_at, "
                "(slot.state = 'held' AND slot.lease_expires_at <= NOW()) AS slot_stuck "
                "FROM broker_run_admissions admission "
                "LEFT JOIN broker_aps_slots slot USING (event_key, tenant_id) "
                "WHERE admission.state = 'executing' "
                "ORDER BY execution_started_at ASC LIMIT %(limit)s",
                {"limit": safe_limit},
            ).fetchall()
        return [dict(row) for row in rows]

    def admission_status(
        self, event_key: str, tenant_id: str,
    ) -> Optional[Dict[str, Any]]:
        with self._db.get_pool().connection() as conn:
            row = conn.execute(
                "SELECT admission.tenant_id, admission.event_key, "
                "admission.request_fingerprint, admission.state, admission.aps_live, "
                "admission.reserved_usd, admission.execution_started_at, "
                "admission.terminal_at, admission.http_status, "
                "CASE WHEN admission.execution_started_at IS NULL THEN NULL ELSE "
                "EXTRACT(EPOCH FROM (NOW() - admission.execution_started_at)) "
                "END AS age_seconds, "
                "slot.state AS slot_state, slot.lease_expires_at AS slot_lease_expires_at, "
                "(slot.state = 'held' AND slot.lease_expires_at <= NOW()) AS slot_stuck "
                "FROM broker_run_admissions admission "
                "LEFT JOIN broker_aps_slots slot USING (event_key, tenant_id) "
                "WHERE admission.event_key = %(event_key)s "
                "AND admission.tenant_id = %(tenant_id)s",
                {"event_key": str(event_key), "tenant_id": str(tenant_id)},
            ).fetchone()
            if row is None:
                return None
            audits = conn.execute(
                "SELECT audit_id, resolution, operator_id, reason, evidence_ref, "
                "prior_state, terminal_status, created_at "
                "FROM broker_admission_resolution_audit "
                "WHERE event_key = %(event_key)s AND tenant_id = %(tenant_id)s "
                "ORDER BY created_at",
                {"event_key": str(event_key), "tenant_id": str(tenant_id)},
            ).fetchall()
        result = dict(row)
        result["resolution_audit"] = [dict(audit) for audit in audits]
        return result

    def reconcile_executing(
        self, event_key: str, tenant_id: str, *,
        resolution: str, operator_id: str, reason: str, evidence_ref: str,
        entry: Dict[str, Any], result: Dict[str, Any], http_status: int,
    ) -> Dict[str, Any]:
        """Terminalize an executing admission with immutable operator evidence."""
        key, tenant = str(event_key), str(tenant_id)
        values = dict(entry)
        self._validate_ledger_numbers(values)
        values.update({
            "event_key": key,
            "result_json": json.dumps(result, separators=(",", ":"), allow_nan=False),
            "http_status": int(http_status),
            "audit_id": str(uuid.uuid4()),
            "resolution": str(resolution),
            "operator_id": str(operator_id),
            "reason": str(reason),
            "evidence_ref": str(evidence_ref),
        })

        def operation(conn):
            conn.execute(
                "SELECT pg_advisory_xact_lock("
                "hashtextextended('event:' || %(event_key)s, 0))",
                {"event_key": key},
            )
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%(tenant_id)s, 0))",
                {"tenant_id": tenant},
            )
            admission = conn.execute(
                "SELECT tenant_id, state FROM broker_run_admissions "
                "WHERE event_key = %(event_key)s FOR UPDATE",
                {"event_key": key},
            ).fetchone()
            if admission is None or str(admission["tenant_id"]) != tenant:
                raise RuntimeError("executing broker admission was not found")
            if admission["state"] != "executing":
                raise RuntimeError(
                    f"broker admission is {admission['state']!r}, not 'executing'")
            conn.execute(
                "INSERT INTO broker_usage_ledger "
                "(event_key, ts, tenant_id, tool, engine_op, aps_endpoint, "
                "aps_live, engine_seconds, usd_est, status) "
                "VALUES (%(event_key)s, %(ts)s, %(tenant_id)s, %(tool)s, "
                "%(engine_op)s, %(aps_endpoint)s, %(aps_live)s, "
                "%(engine_seconds)s, %(usd_est)s, %(status)s)",
                values,
            )
            updated = conn.execute(
                "UPDATE broker_run_admissions SET state = 'terminal', "
                "reserved_usd = 0, accounted_work = "
                "(%(resolution)s <> 'confirmed_failed_no_charge'), "
                "result_json = %(result_json)s::jsonb, "
                "http_status = %(http_status)s, terminal_at = NOW(), "
                "updated_at = NOW() WHERE event_key = %(event_key)s "
                "AND tenant_id = %(tenant_id)s AND state = 'executing' "
                "RETURNING event_key",
                values,
            ).fetchone()
            if updated is None:
                raise RuntimeError("executing broker admission changed during resolution")
            conn.execute(
                "UPDATE broker_aps_slots SET state = 'released', "
                "released_at = NOW(), release_reason = %(resolution)s "
                "WHERE event_key = %(event_key)s AND state = 'held'",
                values,
            )
            conn.execute(
                "INSERT INTO broker_admission_resolution_audit "
                "(audit_id, event_key, tenant_id, resolution, operator_id, "
                "reason, evidence_ref, prior_state, terminal_status) "
                "VALUES (%(audit_id)s, %(event_key)s, %(tenant_id)s, "
                "%(resolution)s, %(operator_id)s, %(reason)s, %(evidence_ref)s, "
                "'executing', %(http_status)s)",
                values,
            )
            return {
                "event_key": key, "tenant_id": tenant, "state": "terminal",
                "resolution": resolution, "audit_id": values["audit_id"],
                "http_status": int(http_status),
            }

        return self._db.run_transaction(operation, isolation="serializable")


def get_store() -> PostgresBrokerStore:
    global _store
    if _store is None:
        _store = PostgresBrokerStore()
    return _store


def reset_store() -> None:
    global _store
    _store = None
