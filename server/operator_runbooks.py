"""Operator reversible runbooks (contract/OPERATOR.md sections 4-5; Wave 2
Lane F). First runbook: tenant agent pause/resume.

Flow (split, always-confirm):
  propose  -> server-owned target resolution + mint a one-use authority bound
              to the tenant's CURRENT agent-state revision.
  execute  -> ONE PostgreSQL transaction that (1) locks the tenant row, (2)
              consumes the one-use authority bound to that locked revision,
              (3) applies the revision-guarded agent-disabled change, and (4)
              writes the security audit. db.connection() commits on clean exit
              and rolls back on any exception, so authority redemption, tenant-
              state mutation, and the audit are atomic: a drift, replay, or
              apply failure rolls ALL of them back (the authority stays
              granted, tenant state is unchanged, no audit row). Invariant 6.

The runbook REQUIRES revisioned tenant state (PostgreSQL-backed agent store).
Without a revision there is nothing to bind, so it fails closed rather than
apply an unguarded change. The tenant agent-disabled flag lives in
`agent_tenant_state`, the SAME table the tenant gate reads, in the SAME
database as the operator tables (both via leaf_platform.db), which is what
makes the single transaction possible.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

import agent_policy
import operator_authority
import operator_principals
from operator_principals import _db
from psycopg.types.json import Jsonb

PAUSE = "operator.tenant_agent_pause"
RESUME = "operator.tenant_agent_resume"

_WANT_DISABLED = {PAUSE: True, RESUME: False}
_EXPECTED_STATE = {PAUSE: "enabled", RESUME: "disabled"}
_REVERSAL = {PAUSE: RESUME, RESUME: PAUSE}

# Canonical tenant id (contract/AUTH.md M2M rule) — bounds arbitrary ids.
_TENANT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


class RunbookError(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _validate_tenant(tenant_id: str) -> None:
    if not isinstance(tenant_id, str) or not _TENANT_ID.match(tenant_id):
        raise RunbookError("tenant_id_invalid")


def resolve_tenant_agent_target(tenant_id: str) -> Dict[str, Any]:
    """Server-owned target resolution (read-only): the tenant's current
    agent-disabled flag and the revision that guards a compare-and-set. Never
    trusts a client-supplied state."""
    _validate_tenant(tenant_id)
    state = agent_policy.load_tenant_state(tenant_id)
    return {
        "tenant_id": tenant_id,
        "agent_disabled": bool(state.get("agent_disabled")),
        "revision": state.get("revision"),
    }


def _args(action: str, tenant_id: str) -> Dict[str, Any]:
    return {"tenant_id": tenant_id, "expected_state": _EXPECTED_STATE[action]}


def _require_revisioned(target: Dict[str, Any]) -> int:
    if target["revision"] is None:
        raise RunbookError("tenant_state_not_revisioned")
    return int(target["revision"])


def propose(op, action: str, tenant_id: str) -> Dict[str, Any]:
    """Resolve the target, verify the precondition, and mint a one-use
    authority bound to the current revision."""
    if action not in _WANT_DISABLED:
        raise RunbookError("unknown_runbook_action")
    target = resolve_tenant_agent_target(tenant_id)
    revision = _require_revisioned(target)
    if target["agent_disabled"] == _WANT_DISABLED[action]:
        raise RunbookError("precondition_state_conflict")
    minted = operator_authority.mint(
        op.subject, op.role_revision, op.profile, op.environment,
        session_id=f"runbook:{op.subject}", turn_id=None, action=action,
        args=_args(action, tenant_id), target_revision=str(revision))
    return {
        "authority_id": minted["authority_id"], "action": action,
        "tenant_id": tenant_id, "target_revision": revision,
        "before": target, "reversal_action": _REVERSAL[action],
        "expires_at": minted["expires_at"],
    }


def _audit_deny(op, action: str, reason: str, authority_id: str) -> None:
    """Record a denied redemption in its OWN transaction so the record
    survives the rollback of the main transaction (contract/OPERATOR.md: every
    redemption denial writes a distinct audit reason). Best-effort: never masks
    the original error."""
    try:
        db = _db()
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO operator_security_audit (subject, action,"
                " decision, reason, authority_id, environment)"
                " VALUES (%s, %s, 'deny', %s, %s, %s)",
                (op.subject, action, reason, authority_id, op.environment))
    except Exception:  # noqa: BLE001 - audit is best-effort on the deny path
        pass


def execute(op, action: str, tenant_id: str, authority_id: str) -> Dict[str, Any]:
    """Atomic: principal re-check + authority consume + tenant-state CAS +
    audit in ONE PostgreSQL transaction. Any failure rolls the whole thing
    back; the denial is recorded separately."""
    if action not in _WANT_DISABLED:
        raise RunbookError("unknown_runbook_action")
    _validate_tenant(tenant_id)
    want_disabled = _WANT_DISABLED[action]
    args = _args(action, tenant_id)

    # Fast-fail admission (the AUTHORITATIVE checks are inside the transaction,
    # on locked rows).
    if operator_authority.kill_switch_active():
        _audit_deny(op, action, "kill_switch_active", authority_id)
        raise RunbookError("kill_switch_active")
    if not operator_principals.revalidate(op.subject, op.role_revision):
        _audit_deny(op, action, "principal_drift", authority_id)
        raise RunbookError("principal_drift")

    db = _db()
    try:
        with db.connection() as conn, conn.cursor() as cur:
            # 0a. Serialize ALL operations on this tenant, INCLUDING the
            #     not-yet-created row. A row lock alone cannot protect two
            #     concurrent first writes (revision 0), so use the SAME
            #     transaction-scoped advisory lock key the canonical store uses
            #     (agent_pg_store.set_tenant_state), so the runbook and the ops
            #     surface serialize against each other too.
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (tenant_id,))

            # 0b. Authoritative principal check, LOCKED, inside the transaction.
            #    FOR UPDATE serializes with a concurrent revoke/suspend (which
            #    bumps role_revision): either the revoke committed first and we
            #    see it here (deny), or it waits for this transaction. Closes
            #    the revoke-after-preflight TOCTOU.
            cur.execute(
                "SELECT status, role_revision FROM operator_principals"
                " WHERE subject = %s FOR UPDATE", (op.subject,))
            prow = cur.fetchone()
            if (prow is None or prow["status"] != "active"
                    or int(prow["role_revision"]) != int(op.role_revision)):
                raise RunbookError("principal_drift")

            # 1. Lock the tenant row and read the current revision. FOR UPDATE
            #    holds the row for the whole transaction so nothing can change
            #    it between the consume and the apply.
            cur.execute(
                "SELECT agent_disabled, revision FROM agent_tenant_state"
                " WHERE tenant_id = %s FOR UPDATE",
                (tenant_id,))
            row = cur.fetchone()
            current_rev = int(row["revision"]) if row is not None else 0
            before_disabled = bool(row["agent_disabled"]) if row is not None else False

            # 2. Consume the one-use authority BOUND to the locked revision. A
            #    drift/replay/mismatch fails the WHERE -> AuthorityDenied -> the
            #    whole tx rolls back (authority stays granted, tenant unchanged,
            #    no applied audit).
            operator_authority.consume_in_tx(
                cur, authority_id, op.subject, op.role_revision, op.environment,
                action, args, target_revision=str(current_rev))

            # 3. Precondition inside the lock: refuse if already in the target
            #    state (rolls back the consume).
            if before_disabled == want_disabled:
                raise RunbookError("precondition_state_conflict")

            # 4. Apply the reversible change, bumping the revision.
            cur.execute(
                """
                INSERT INTO agent_tenant_state
                  (tenant_id, agent_disabled, overlay, revision, updated_at)
                VALUES (%s, %s, '{}'::jsonb, %s, now())
                ON CONFLICT (tenant_id) DO UPDATE
                  SET agent_disabled = EXCLUDED.agent_disabled,
                      revision = agent_tenant_state.revision + 1,
                      updated_at = now()
                RETURNING agent_disabled, revision
                """,
                (tenant_id, want_disabled, current_rev + 1))
            applied = cur.fetchone()
            after_disabled = bool(applied["agent_disabled"])
            after_rev = int(applied["revision"])

            # 5. Security audit, in the SAME transaction.
            cur.execute(
                "INSERT INTO operator_security_audit (subject, action,"
                " decision, reason, authority_id, environment, extra)"
                " VALUES (%s, %s, 'execute', 'runbook_applied', %s, %s, %s)",
                (op.subject, action, authority_id, op.environment,
                 Jsonb({"tenant_id": tenant_id,
                        "reversal_action": _REVERSAL[action],
                        "approver": op.subject,
                        "before": {"agent_disabled": before_disabled,
                                   "revision": current_rev},
                        "after": {"agent_disabled": after_disabled,
                                  "revision": after_rev}})))
            # clean exit: db.connection() commits steps 0/2/4/5 atomically.
    except (operator_authority.AuthorityDenied, RunbookError) as exc:
        # The main transaction rolled back; record the denial durably.
        _audit_deny(op, action, getattr(exc, "reason", "denied"), authority_id)
        raise

    return {
        "action": action, "tenant_id": tenant_id, "authority_id": authority_id,
        "before": {"agent_disabled": before_disabled, "revision": current_rev},
        "after": {"agent_disabled": after_disabled, "revision": after_rev},
        "reversal_action": _REVERSAL[action],
    }
