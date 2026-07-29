"""PostgreSQL authority for the agent gate.

The module is imported only when ``LEAF_AGENT_STORE=postgres``. Every security
decision uses one fleet-visible database, and database errors propagate so the
gate fails closed instead of silently falling back to local files.
"""
from __future__ import annotations

import importlib.util
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from psycopg.types.json import Jsonb

_platform_modules: Optional[tuple] = None
_counter_store = None
_USAGE_FIELDS = frozenset({
    "kind", "ts", "tenant_id", "session_id", "turn_id", "turn", "grant_kind",
    "model", "tokens_in", "tokens_out", "cache_creation_tokens",
    "cache_read_tokens", "cost_tokens", "usd_est", "wall_seconds",
    "tools_called", "stop_reason", "degraded_mode",
})


def _load_platform():
    global _platform_modules
    if _platform_modules is not None:
        return _platform_modules
    if "leaf_platform" not in sys.modules:
        package_dir = Path(__file__).resolve().parent.parent / "platform"
        spec = importlib.util.spec_from_file_location(
            "leaf_platform", package_dir / "__init__.py",
            submodule_search_locations=[str(package_dir)],
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("platform package could not be loaded")
        module = importlib.util.module_from_spec(spec)
        sys.modules["leaf_platform"] = module
        spec.loader.exec_module(module)
    import leaf_platform.db as db  # noqa: PLC0415
    from leaf_platform.counters import SharedCounterStore  # noqa: PLC0415

    _platform_modules = (db, SharedCounterStore)
    return _platform_modules


def _counter():
    global _counter_store
    if _counter_store is None:
        _db, counter_type = _load_platform()
        _counter_store = counter_type("agent_rate_counters")
    return _counter_store


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def _approval_record(row: Any) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    record = {
        "confirmation_id": row["confirmation_id"],
        "tenant_id": row["tenant_id"],
        "session_id": row["session_id"],
        "turn_id": row["turn_id"],
        "action": row["action"],
        "args": dict(row["args"]),
        "args_hash": row["args_hash"],
        "policy": row["policy"],
        "rung": row["rung"],
        "created_at": _iso(row["created_at"]),
        "expires_at": _iso(row["expires_at"]),
        "granted": row["granted"],
        "denied": row["denied"],
        "decided_at": _iso(row["decided_at"]) if row["decided_at"] else None,
        "decided_by": row["decided_by"],
        "reason": row["reason"],
    }
    if row["consumed_at"] is not None:
        record["consumed_at"] = _iso(row["consumed_at"])
    return record


def create_pending(record: Dict[str, Any]) -> Dict[str, Any]:
    db, _counter_type = _load_platform()

    def operation(conn):
        _insert_pending_in_transaction(conn, record)
        _append_audit_in_transaction(conn, {
            "kind": "approval_created",
            "confirmation_id": record["confirmation_id"],
            "tenant_id": record["tenant_id"],
            "session_id": record["session_id"],
            "turn_id": record["turn_id"],
            "action": record["action"],
        })
        return record

    return db.run_transaction(operation, isolation="serializable")


def _insert_pending_in_transaction(conn: Any, record: Dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO agent_approvals
          (confirmation_id, tenant_id, session_id, turn_id, action, args,
           args_hash, policy, rung, created_at, expires_at, granted, denied,
           decided_at, decided_by, reason, consumed_at)
        VALUES
          (%(confirmation_id)s, %(tenant_id)s, %(session_id)s, %(turn_id)s,
           %(action)s, %(args)s, %(args_hash)s, %(policy)s, %(rung)s,
           %(created_at)s, %(expires_at)s, FALSE, FALSE, NULL, NULL, NULL, NULL)
        """,
        dict(record, args=Jsonb(record["args"])),
    )


def read_pending(confirmation_id: str) -> Optional[Dict[str, Any]]:
    db, _counter_type = _load_platform()
    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM agent_approvals WHERE confirmation_id = %(id)s",
            {"id": str(confirmation_id)},
        )
        return _approval_record(cur.fetchone())


def read_pending_strict(confirmation_id: str) -> tuple:
    record = read_pending(confirmation_id)
    return (record, "ok") if record is not None else (None, "absent")


def _append_audit_in_transaction(conn: Any, event: Dict[str, Any]) -> None:
    conn.execute(
        """
        INSERT INTO agent_gate_audit_events
          (tenant_id, session_id, turn_id, kind, event)
        VALUES
          (%(tenant_id)s, %(session_id)s, %(turn_id)s, %(kind)s, %(event)s)
        """,
        {
            "tenant_id": event.get("tenant_id"),
            "session_id": event.get("session_id"),
            "turn_id": event.get("turn_id"),
            "kind": str(event.get("kind") or "unknown"),
            "event": Jsonb(event),
        },
    )


def decide(
    confirmation_id: str, *, granted: bool, by: str, reason: str,
) -> Tuple[bool, Optional[Dict[str, Any]], str]:
    db, _counter_type = _load_platform()

    def operation(conn):
        row = conn.execute(
            """
            SELECT *, expires_at <= NOW() AS is_expired
            FROM agent_approvals
            WHERE confirmation_id = %(id)s
            FOR UPDATE
            """,
            {"id": str(confirmation_id)},
        ).fetchone()
        if row is None:
            return False, None, "not_found"
        record = _approval_record(row)
        if row["granted"] or row["denied"]:
            return False, record, "already_decided"
        expired = row["is_expired"] is True
        final_granted = bool(granted) and not expired
        final_reason = "expired" if expired else reason
        updated = conn.execute(
            """
            UPDATE agent_approvals
            SET granted = %(granted)s, denied = %(denied)s, decided_at = NOW(),
                decided_by = %(by)s, reason = %(reason)s
            WHERE confirmation_id = %(id)s
            RETURNING *
            """,
            {
                "id": str(confirmation_id),
                "granted": final_granted,
                "denied": not final_granted,
                "by": str(by),
                "reason": final_reason,
            },
        ).fetchone()
        status = "expired" if expired else ("granted" if granted else "denied")
        record = _approval_record(updated)
        event = {
            "kind": "approval_granted" if final_granted else "approval_denied",
            "confirmation_id": str(confirmation_id),
            "tenant_id": record["tenant_id"],
            "session_id": record["session_id"],
            "turn_id": record["turn_id"],
            "action": record["action"],
            "decided_by": str(by),
        }
        if not final_granted:
            event["reason"] = final_reason
        _append_audit_in_transaction(conn, event)
        return not expired, record, status

    return db.run_transaction(operation, isolation="serializable")


def redeem(
    confirmation_id: str, *, tenant_id: str, session_id: str,
    action: str, args_hash: str, audit_event: Optional[Dict[str, Any]] = None,
    subject: Optional[str] = None,
    subject_match_required: bool = False,
    session_grant_target: Optional[List[str]] = None,
    rate_category: Optional[str] = None, rate_limit: Optional[int] = None,
    rate_rejected_event: Optional[Dict[str, Any]] = None,
    outcome_events: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Tuple[bool, Optional[Dict[str, Any]], str]:
    db, _counter_type = _load_platform()

    def operation(conn):
        if rate_category is not None:
            if rate_limit is None:
                raise ValueError("rate_limit is required with rate_category")
            rate = _consume_rate_in_transaction(
                conn, tenant_id, rate_category, rate_limit)
            if not rate.accepted:
                event = dict(rate_rejected_event or {})
                if not event:
                    raise ValueError("rate_rejected_event is required with rate_category")
                event["reason"] = (
                    f"rate_limit_exceeded: {rate_category} "
                    f"({rate.value}/{rate_limit})")
                _append_audit_in_transaction(conn, event)
                return False, None, event["reason"]

        def reject(record, reason):
            event = (outcome_events or {}).get(reason)
            if reason == "approval_expired" and event is None:
                event = {
                    "kind": "approval_denied",
                    "confirmation_id": str(confirmation_id),
                    "tenant_id": str(tenant_id),
                    "session_id": str(session_id),
                    "action": str(action),
                    "reason": "approval_expired",
                    "decided_by": "system",
                }
            if rate_category is not None or reason == "approval_expired":
                if event is None:
                    raise ValueError(f"missing atomic audit event for {reason}")
                _append_audit_in_transaction(conn, event)
            return False, record, reason

        row = conn.execute(
            """
            SELECT *, expires_at <= NOW() AS is_expired
            FROM agent_approvals
            WHERE confirmation_id = %(id)s
            FOR UPDATE
            """,
            {"id": str(confirmation_id)},
        ).fetchone()
        if row is None:
            return reject(None, "approval_not_found")
        record = _approval_record(row)
        if (
            row["tenant_id"] != str(tenant_id)
            or row["session_id"] != str(session_id)
            or row["action"] != str(action)
            or row["args_hash"] != str(args_hash)
        ):
            return reject(record, "args_mismatch")
        if row["denied"] is not False:
            return reject(record, "approval_denied")
        if subject_match_required and (
                subject is None or row["decided_by"] != str(subject)):
            return reject(record, "approval_subject_mismatch")
        if row["is_expired"] is True:
            conn.execute(
                """
                UPDATE agent_approvals
                SET granted = FALSE, denied = TRUE, decided_at = NOW(),
                    decided_by = 'system', reason = 'expired'
                WHERE confirmation_id = %(id)s
                """,
                {"id": str(confirmation_id)},
            )
            return reject(record, "approval_expired")
        if row["granted"] is not True:
            return reject(record, "awaiting_approval")
        if row["consumed_at"] is not None:
            return reject(record, "approval_consumed")
        updated = conn.execute(
            """
            UPDATE agent_approvals
            SET consumed_at = NOW()
            WHERE confirmation_id = %(id)s AND consumed_at IS NULL
            RETURNING *
            """,
            {"id": str(confirmation_id)},
        ).fetchone()
        if updated is None:
            return reject(record, "approval_consumed")
        if session_grant_target is not None:
            conn.execute(
                """
                INSERT INTO agent_session_grants
                  (tenant_id, session_id, action, target_key)
                VALUES (%(tenant)s, %(session)s, %(action)s, %(target)s)
                ON CONFLICT (tenant_id, session_id, action, target_key)
                DO UPDATE SET granted_at = NOW()
                """,
                {
                    "tenant": str(tenant_id),
                    "session": str(session_id),
                    "action": str(action),
                    "target": json.dumps(
                        session_grant_target, separators=(",", ":")),
                },
            )
        evidence = dict(audit_event or {
            "kind": "approval_redeemed",
            "tenant_id": str(tenant_id),
            "session_id": str(session_id),
            "action": str(action),
            "confirmation_id": str(confirmation_id),
        })
        _append_audit_in_transaction(conn, evidence)
        return True, _approval_record(updated), "allow_via_approval"

    return db.run_transaction(operation, isolation="serializable")


def _consume_rate_in_transaction(
    conn: Any, tenant_id: str, category: str, limit: int,
    *, now: Optional[datetime] = None,
):
    """Consume one invocation's unit inside its final decision transaction.

    The current gate wire carries no stable per-tool-call operation id. Tenant,
    session, turn, action, and arguments are not an idempotency key because one
    turn may legitimately issue identical calls. Therefore a retry after the
    database transaction committed but its HTTP response was lost is charged
    as a new invocation. Audit failures before commit roll back the counter.
    Add deduplication only when the caller supplies an explicit operation id.
    """
    if now is None:
        row = conn.execute(
            """
            SELECT TO_CHAR(
              CURRENT_TIMESTAMP AT TIME ZONE 'UTC', 'YYYYMMDDHH'
            ) AS bucket
            """
        ).fetchone()
        bucket = row["bucket"]
    else:
        bucket = now.astimezone(timezone.utc).strftime("%Y%m%d%H")
    namespace = f"agent_rate:{category}"
    counter_key = f"{tenant_id}:{bucket}"
    return _counter().consume_in_transaction(
        conn,
        namespace=namespace,
        key=counter_key,
        limit=limit,
    )


def consume_rate_and_audit(
    tenant_id: str, category: str, limit: int, *,
    accepted_event: Dict[str, Any], rejected_event: Dict[str, Any],
    now: Optional[datetime] = None,
) -> Tuple[bool, Dict[str, Any]]:
    db, _counter_type = _load_platform()

    def operation(conn):
        result = _consume_rate_in_transaction(
            conn, tenant_id, category, limit, now=now)
        event = dict(accepted_event if result.accepted else rejected_event)
        reason_kind = "rate_limit_ok" if result.accepted else "rate_limit_exceeded"
        info = {
            "category": category,
            "count": result.value,
            "limit": limit,
            "reason": f"{reason_kind}: {category} ({result.value}/{limit})",
        }
        if not result.accepted:
            event["reason"] = info["reason"]
        _append_audit_in_transaction(conn, event)
        return result.accepted, info

    return db.run_transaction(operation, isolation="serializable")


def create_pending_with_rate(
    record: Dict[str, Any], *, category: str, limit: int,
    accepted_event: Dict[str, Any], rejected_event: Dict[str, Any],
) -> Tuple[bool, Optional[Dict[str, Any]], Dict[str, Any]]:
    db, _counter_type = _load_platform()

    def operation(conn):
        result = _consume_rate_in_transaction(
            conn, record["tenant_id"], category, limit)
        reason_kind = "rate_limit_ok" if result.accepted else "rate_limit_exceeded"
        info = {
            "category": category,
            "count": result.value,
            "limit": limit,
            "reason": f"{reason_kind}: {category} ({result.value}/{limit})",
        }
        if not result.accepted:
            event = dict(rejected_event)
            event["reason"] = info["reason"]
            _append_audit_in_transaction(conn, event)
            return False, None, info
        _insert_pending_in_transaction(conn, record)
        _append_audit_in_transaction(conn, accepted_event)
        return True, record, info

    return db.run_transaction(operation, isolation="serializable")


def has_session_grant(
    tenant_id: str, session_id: str, action: str, target: List[str],
) -> bool:
    db, _counter_type = _load_platform()
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT 1 FROM agent_session_grants
            WHERE tenant_id = %(tenant)s AND session_id = %(session)s
              AND action = %(action)s AND target_key = %(target)s
            """,
            {
                "tenant": str(tenant_id),
                "session": str(session_id),
                "action": str(action),
                "target": json.dumps(target, separators=(",", ":")),
            },
        )
        return cur.fetchone() is not None


def record_session_grant(
    tenant_id: str, session_id: str, action: str, target: List[str],
) -> None:
    db, _counter_type = _load_platform()

    def operation(conn):
        conn.execute(
            """
            INSERT INTO agent_session_grants
              (tenant_id, session_id, action, target_key)
            VALUES (%(tenant)s, %(session)s, %(action)s, %(target)s)
            ON CONFLICT (tenant_id, session_id, action, target_key)
            DO UPDATE SET granted_at = NOW()
            """,
            {
                "tenant": str(tenant_id),
                "session": str(session_id),
                "action": str(action),
                "target": json.dumps(target, separators=(",", ":")),
            },
        )
        _append_audit_in_transaction(conn, {
            "kind": "session_grant_recorded",
            "tenant_id": str(tenant_id),
            "session_id": str(session_id),
            "action": str(action),
        })

    db.run_transaction(operation, isolation="serializable")


def consume_rate(
    tenant_id: str, category: str, limit: int, *, now: Optional[datetime] = None,
) -> Tuple[bool, Dict[str, Any]]:
    # Kept as a focused-test/operator primitive. Production gate transitions
    # use their decision-specific event, while this direct entry point still
    # records evidence atomically and never creates an unaudited counter.
    event = {
        "kind": "rate_consumed",
        "tenant_id": str(tenant_id),
        "category": str(category),
    }
    rejected = dict(event, kind="rate_rejected")
    return consume_rate_and_audit(
        tenant_id, category, limit,
        accepted_event=event, rejected_event=rejected, now=now)


def kill_switch_details() -> Dict[str, Any]:
    db, _counter_type = _load_platform()
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT active, reason FROM agent_fleet_state
            WHERE state_key = 'global_kill'
            """
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("agent fleet kill state is missing")
    return {"active": row["active"], "reason": row["reason"][:200]}


def append_audit(event: Dict[str, Any]) -> None:
    evidence = dict(event)
    evidence.setdefault("ts", datetime.now(timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z"))
    db, _counter_type = _load_platform()
    with db.connection() as conn:
        _append_audit_in_transaction(conn, evidence)


def _audit_records(
    column: str, value: str, limit: int,
) -> List[Dict[str, Any]]:
    if column not in {"tenant_id", "session_id"}:
        raise ValueError("unsupported audit filter")
    db, _counter_type = _load_platform()
    bounded = max(1, min(int(limit), 1000))
    with db.cursor() as cur:
        cur.execute(
            f"""
            SELECT event FROM (
              SELECT event_id, event
              FROM agent_gate_audit_events
              WHERE {column} = %(value)s
              ORDER BY event_id DESC
              LIMIT %(limit)s
            ) AS recent
            ORDER BY event_id
            """,
            {"value": str(value), "limit": bounded},
        )
        return [dict(row["event"]) for row in cur.fetchall()]


def audit_for_tenant(tenant_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    return _audit_records("tenant_id", tenant_id, limit)


def audit_for_session(session_id: str, limit: int = 100) -> List[Dict[str, Any]]:
    return _audit_records("session_id", session_id, limit)


def tenant_state(tenant_id: str) -> Dict[str, Any]:
    db, _counter_type = _load_platform()
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT agent_disabled, overlay, revision
            FROM agent_tenant_state WHERE tenant_id = %(tenant_id)s
            """,
            {"tenant_id": str(tenant_id)},
        )
        row = cur.fetchone()
    if row is None:
        return {"agent_disabled": False, "overlay": {}, "revision": 0}
    return {
        "agent_disabled": row["agent_disabled"],
        "overlay": dict(row["overlay"]),
        "revision": int(row["revision"]),
    }


def set_tenant_state(
    tenant_id: str, *, disabled: Optional[bool] = None,
    overlay: Optional[Dict[str, Any]] = None,
    expected_revision: Optional[int] = None,
    audit_event: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """CAS one tenant row. None means the caller supplied a stale revision."""
    if disabled is None and overlay is None:
        raise ValueError("disabled or overlay is required")
    db, _counter_type = _load_platform()

    def operation(conn):
        # Lock only this tenant, including its not-yet-created row. A row lock
        # alone cannot protect two concurrent first writes.
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%(tenant_id)s, 0))",
            {"tenant_id": str(tenant_id)},
        )
        row = conn.execute(
            """
            SELECT agent_disabled, overlay, revision
            FROM agent_tenant_state WHERE tenant_id = %(tenant_id)s
            FOR UPDATE
            """,
            {"tenant_id": str(tenant_id)},
        ).fetchone()
        current_revision = int(row["revision"]) if row is not None else 0
        if expected_revision is not None and int(expected_revision) != current_revision:
            return None
        current_disabled = bool(row["agent_disabled"]) if row is not None else False
        current_overlay = dict(row["overlay"]) if row is not None else {}
        next_disabled = current_disabled if disabled is None else bool(disabled)
        next_overlay = current_overlay if overlay is None else dict(overlay)
        updated = conn.execute(
            """
            INSERT INTO agent_tenant_state
              (tenant_id, agent_disabled, overlay, revision, updated_at)
            VALUES
              (%(tenant_id)s, %(disabled)s, %(overlay)s, 1, NOW())
            ON CONFLICT (tenant_id) DO UPDATE
            SET agent_disabled = EXCLUDED.agent_disabled,
                overlay = EXCLUDED.overlay,
                revision = agent_tenant_state.revision + 1,
                updated_at = NOW()
            RETURNING agent_disabled, overlay, revision
            """,
            {
                "tenant_id": str(tenant_id),
                "disabled": next_disabled,
                "overlay": Jsonb(next_overlay),
            },
        ).fetchone()
        result = {
            "agent_disabled": updated["agent_disabled"],
            "overlay": dict(updated["overlay"]),
            "revision": int(updated["revision"]),
        }
        if audit_event is not None:
            evidence = dict(audit_event)
            evidence.setdefault("tenant_id", str(tenant_id))
            evidence["revision"] = result["revision"]
            _append_audit_in_transaction(conn, evidence)
        return result

    return db.run_transaction(operation, isolation="serializable")


def tenant_states() -> Dict[str, Dict[str, Any]]:
    db, _counter_type = _load_platform()
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT tenant_id, agent_disabled, overlay, revision
            FROM agent_tenant_state ORDER BY tenant_id
            """
        )
        return {
            str(row["tenant_id"]): {
                "agent_disabled": row["agent_disabled"],
                "overlay": dict(row["overlay"]),
                "revision": int(row["revision"]),
            }
            for row in cur.fetchall()
        }


def append_usage(record: Dict[str, Any]) -> bool:
    """Insert one exact turn once. Returns False for an identical retry."""
    required = ("tenant_id", "session_id", "turn_id")
    missing = [key for key in required if not str(record.get(key) or "")]
    if missing:
        raise ValueError(f"agent usage record missing {', '.join(missing)}")
    # This is a metering row, not a general event sink. The allowlist prevents
    # a caller from persisting credentials or grant material by mistake.
    entry = {key: record[key] for key in _USAGE_FIELDS if key in record}
    entry.setdefault("kind", "turn")
    timestamp_supplied = bool(entry.get("ts"))
    entry.setdefault("ts", datetime.now(timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z"))
    identity = "\0".join(str(entry[key]) for key in required)
    usage_key = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    db, _counter_type = _load_platform()

    def operation(conn):
        inserted = conn.execute(
            """
            INSERT INTO agent_usage_turns
              (usage_key, tenant_id, session_id, turn_id, ts, record)
            VALUES
              (%(usage_key)s, %(tenant_id)s, %(session_id)s, %(turn_id)s,
               %(ts)s, %(record)s)
            ON CONFLICT (usage_key) DO NOTHING
            RETURNING usage_key
            """,
            {
                "usage_key": usage_key,
                "tenant_id": str(entry["tenant_id"]),
                "session_id": str(entry["session_id"]),
                "turn_id": str(entry["turn_id"]),
                "ts": entry["ts"],
                "record": Jsonb(entry),
            },
        ).fetchone()
        if inserted is not None:
            return True
        existing = conn.execute(
            "SELECT record FROM agent_usage_turns WHERE usage_key = %(key)s",
            {"key": usage_key},
        ).fetchone()
        stored = dict(existing["record"]) if existing is not None else None
        candidate = dict(entry)
        if not timestamp_supplied and stored is not None:
            stored.pop("ts", None)
            candidate.pop("ts", None)
        if stored != candidate:
            raise RuntimeError("agent usage idempotency key reused with different content")
        return False

    return db.run_transaction(operation, isolation="serializable")


def _usage_bucket(rows: List[Any]) -> Dict[str, Any]:
    turns = len(rows)
    cost_tokens = 0
    usd_est = 0.0
    for row in rows:
        entry = dict(row["record"])
        try:
            cost_tokens += int(entry.get("cost_tokens") or 0)
        except (TypeError, ValueError):
            pass
        try:
            usd_est += float(entry.get("usd_est") or 0.0)
        except (TypeError, ValueError):
            pass
    return {
        "turns": turns,
        "cost_tokens": cost_tokens,
        "usd_est": round(usd_est, 6),
    }


def aggregate_usage(tenant_id: str) -> Dict[str, Any]:
    db, _counter_type = _load_platform()
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT record, ts FROM agent_usage_turns
            WHERE tenant_id = %(tenant_id)s
              AND record->>'kind' = 'turn'
              AND ts >= (
                DATE_TRUNC('month', CURRENT_TIMESTAMP AT TIME ZONE 'UTC')
                AT TIME ZONE 'UTC'
              )
            ORDER BY ts, usage_key
            """,
            {"tenant_id": str(tenant_id)},
        )
        cycle_rows = list(cur.fetchall())
    today = datetime.now(timezone.utc).date()
    today_rows = [
        row for row in cycle_rows
        if row["ts"].astimezone(timezone.utc).date() == today
    ]
    return {
        "today": _usage_bucket(today_rows),
        "cycle": _usage_bucket(cycle_rows),
        "estimate_basis": "self_metered",
    }


def usage_tenants() -> Dict[str, Dict[str, Any]]:
    db, _counter_type = _load_platform()
    with db.cursor() as cur:
        cur.execute(
            """
            SELECT tenant_id, record FROM agent_usage_turns
            WHERE record->>'kind' = 'turn'
            ORDER BY tenant_id, ts, usage_key
            """
        )
        rows = list(cur.fetchall())
    grouped: Dict[str, List[Any]] = {}
    for row in rows:
        grouped.setdefault(str(row["tenant_id"]), []).append(row)
    return {tenant_id: _usage_bucket(items) for tenant_id, items in grouped.items()}


def list_pending(tenant_id: Optional[str] = None) -> List[Dict[str, Any]]:
    db, _counter_type = _load_platform()
    query = """
        SELECT * FROM agent_approvals
        WHERE granted = FALSE AND denied = FALSE AND expires_at > NOW()
    """
    params: Dict[str, Any] = {}
    if tenant_id is not None:
        query += " AND tenant_id = %(tenant_id)s"
        params["tenant_id"] = str(tenant_id)
    query += " ORDER BY created_at, confirmation_id"
    with db.cursor() as cur:
        cur.execute(query, params)
        return [_approval_record(row) for row in cur.fetchall()]
