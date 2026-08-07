"""Reusable single-transaction primitive for operator tenant-state runbooks
(contract/OPERATOR.md Lane F, invariant 6).

execute_atomic() runs, on ONE leaf_platform.db.connection() (commits on clean
exit, rolls back on any exception):
  0a. pg_advisory_xact_lock(hashtextextended(tenant_id, 0)): serializes ALL
      operations on this tenant including the not-yet-created row, using the
      SAME key the canonical store uses (agent_pg_store.set_tenant_state), so
      the runbook and the ops surface serialize against each other.
  0b. SELECT the operator_principals row FOR UPDATE: authoritative principal
      re-check on a LOCKED row (closes the revoke-after-preflight TOCTOU).
  1.  SELECT ... FOR UPDATE the agent_tenant_state row (current revision).
  2.  operator_authority.consume_in_tx(...) bound to the locked revision.
  3.  the caller's `apply(cur, current_rev, row)` mutation, which performs the
      revision-guarded write and returns (before_summary, after_summary).
  4.  the applied security audit.
A drift/replay/precondition/apply failure rolls ALL of them back; the denial
is recorded in a separate best-effort transaction so the reason survives.

This is the proven pattern from the merged tenant pause/resume runbook (#527),
extracted so every new tenant-state runbook shares one audited, reviewed
transaction body instead of re-implementing it.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, Optional, Tuple

import operator_authority
import operator_principals
from operator_principals import _db
from psycopg.types.json import Jsonb


class RunbookError(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


ApplyFn = Callable[[Any, int, Optional[Dict[str, Any]]],
                   Tuple[Dict[str, Any], Dict[str, Any]]]


def audit_deny(op, action: str, reason: str, authority_id: str) -> None:
    """Record a denied redemption in its OWN transaction (survives the main
    rollback). Best-effort: never masks the original error."""
    try:
        db = _db()
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO operator_security_audit (subject, action,"
                " decision, reason, authority_id, environment)"
                " VALUES (%s, %s, 'deny', %s, %s, %s)",
                (op.subject, action, reason, authority_id, op.environment))
    except Exception:  # noqa: BLE001 - best-effort on the deny path
        pass


def resolve_tenant_state_row(cur, tenant_id: str) -> Tuple[Optional[Dict[str, Any]], int]:
    cur.execute(
        "SELECT agent_disabled, overlay, revision FROM agent_tenant_state"
        " WHERE tenant_id = %s FOR UPDATE",
        (tenant_id,))
    row = cur.fetchone()
    return row, (int(row["revision"]) if row is not None else 0)


def execute_atomic(op, action: str, tenant_id: str, authority_id: str,
                   args: Dict[str, Any], *, apply: ApplyFn) -> Dict[str, Any]:
    """Run the audited single-transaction body. `apply(cur, current_rev, row)`
    performs the specific revision-guarded mutation and returns
    (before_summary, after_summary) for the applied audit. Raises
    AuthorityDenied / RunbookError on any gate; the denial is audited."""
    # Fast-fail admission (authoritative checks are inside the transaction).
    if operator_authority.kill_switch_active():
        audit_deny(op, action, "kill_switch_active", authority_id)
        raise RunbookError("kill_switch_active")
    if not operator_principals.revalidate(op.subject, op.role_revision):
        audit_deny(op, action, "principal_drift", authority_id)
        raise RunbookError("principal_drift")

    db = _db()
    try:
        with db.connection() as conn, conn.cursor() as cur:
            # 0a. tenant-scoped advisory lock (covers the missing row).
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (tenant_id,))
            # 0b. authoritative principal check, LOCKED.
            cur.execute(
                "SELECT status, role_revision FROM operator_principals"
                " WHERE subject = %s FOR UPDATE", (op.subject,))
            prow = cur.fetchone()
            if (prow is None or prow["status"] != "active"
                    or int(prow["role_revision"]) != int(op.role_revision)):
                raise RunbookError("principal_drift")
            # 1. lock the tenant row, read current revision.
            row, current_rev = resolve_tenant_state_row(cur, tenant_id)
            # 2. consume the one-use authority bound to the locked revision.
            operator_authority.consume_in_tx(
                cur, authority_id, op.subject, op.role_revision,
                op.environment, action, args, target_revision=str(current_rev))
            # 3. caller's revision-guarded mutation.
            before, after = apply(cur, current_rev, row)
            # 4. applied audit (same transaction).
            cur.execute(
                "INSERT INTO operator_security_audit (subject, action,"
                " decision, reason, authority_id, environment, extra)"
                " VALUES (%s, %s, 'execute', 'runbook_applied', %s, %s, %s)",
                (op.subject, action, authority_id, op.environment,
                 Jsonb({"tenant_id": tenant_id, "approver": op.subject,
                        "before": before, "after": after})))
    except (operator_authority.AuthorityDenied, RunbookError) as exc:
        audit_deny(op, action, getattr(exc, "reason", "denied"), authority_id)
        raise

    return {"action": action, "tenant_id": tenant_id,
            "authority_id": authority_id, "before": before, "after": after}
