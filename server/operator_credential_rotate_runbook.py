"""Operator runbook: rotate a broker-scoped worker credential
(contract/OPERATOR.md section 4.1, operator.worker_credential_rotate; O4, Wave
3). Always-confirm, gated by a one-use authority; ships dark.

Flow (split, always-confirm):
  propose  -> broker-verify the handle is NON-PRODUCTION scoped (the O4
              precondition), read the handle's CURRENT rotation revision
              (server-owned), and mint a one-use authority bound to it.
  execute  -> ONE PostgreSQL transaction that (0) serializes on the handle,
              (0b) re-checks the principal on a LOCKED row, (1) locks the
              handle's rotation row and reads the revision, (2) consumes the
              one-use authority bound to that locked revision, (3) performs the
              external rotation through the broker (DARK by default -> fails
              closed and rolls back), (4) bumps the rotation revision, and (5)
              writes the security audit. db.connection() commits on clean exit
              and rolls back on any exception, so authority redemption, the
              rotation-state bump, and the audit are atomic. Invariant 6.

The secret itself is never handled here: the broker mints/rotates it out of
band and never returns it. The reversible unit is the SCOPE, not the secret:
the reversal receipt records the scope so an operator can re-issue a fresh
credential for the previous scope (contract reversal: "re-issue the previous
scope, not the previous secret"). The external rotation runs INSIDE the
transaction while the handle's advisory lock is held, which is intentional: it
serializes concurrent rotations of the same handle, and a rotator failure
guarantees no revision bump, no audit, and an un-consumed authority. A commit
that fails AFTER a successful external rotation leaves the authority replayable
(at-least-once external rotation); a re-rotation only mints another fresh
secret, which is acceptable because the operator re-confirms and the reversal
is a re-issue anyway.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional

import operator_authority
import operator_principals
import operator_secret_broker as broker
from operator_principals import _db
from psycopg.types.json import Jsonb

ACTION = "operator.worker_credential_rotate"
_NON_PRODUCTION = {"staging", "development"}
# The handle names a broker registry entry, which the broker already bounds to
# this identifier charset; re-bound here so a malformed handle is refused before
# it reaches the DB or the advisory-lock key.
_HANDLE = re.compile(r"^[a-z0-9_.:-]{1,64}$")
# Namespace the advisory-lock input so a handle can never collide with a tenant
# lock key (tenant runbooks lock on hashtextextended(tenant_id, 0)).
_LOCK_PREFIX = "operator_credential:"


class RunbookError(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _validate_handle(handle: str) -> None:
    if not isinstance(handle, str) or not _HANDLE.match(handle):
        raise RunbookError("credential_handle_invalid")


def _broker_verify(handle: str, environment: str) -> Dict[str, Any]:
    """Server-owned precondition: the handle exists and is NON-PRODUCTION
    scoped, matching the operator's environment. Never trusts a client claim."""
    meta = broker.describe(handle)
    if meta is None:
        raise RunbookError("credential_handle_unknown")
    if (meta.get("environment") not in _NON_PRODUCTION
            or environment not in _NON_PRODUCTION):
        raise RunbookError("production_scope_refused")
    if meta.get("environment") != environment:
        raise RunbookError("environment_mismatch")
    return meta


def _args(handle: str) -> Dict[str, Any]:
    # Exactly the catalog args_schema for this action ({credential_handle}).
    return {"credential_handle": handle}


def resolve_rotation_state(handle: str) -> Dict[str, Any]:
    """Server-owned read of the handle's current rotation revision (0 if it has
    never been rotated). This revision guards the compare-and-set."""
    _validate_handle(handle)
    db = _db()
    with db.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT revision, rotated_at FROM operator_credential_rotations"
            " WHERE handle = %s", (handle,))
        row = cur.fetchone()
    revision = int(row["revision"]) if row is not None else 0
    return {"handle": handle, "revision": revision,
            "rotated_at": (row["rotated_at"].isoformat()
                           if row is not None and row["rotated_at"] else None)}


def propose(op, credential_handle: str) -> Dict[str, Any]:
    """Broker-verify the precondition, read the current revision, and mint a
    one-use authority bound to it."""
    _validate_handle(credential_handle)
    meta = _broker_verify(credential_handle, op.environment)
    state = resolve_rotation_state(credential_handle)
    minted = operator_authority.mint(
        op.subject, op.role_revision, op.profile, op.environment,
        session_id=f"runbook:{op.subject}", turn_id=None, action=ACTION,
        args=_args(credential_handle),
        target_revision=str(state["revision"]))
    return {"authority_id": minted["authority_id"], "action": ACTION,
            "credential_handle": credential_handle,
            "scope": meta.get("scope"), "environment": meta.get("environment"),
            "target_revision": state["revision"],
            "before": state, "expires_at": minted["expires_at"]}


def _audit_deny(op, reason: str, authority_id: str) -> None:
    """Record a denied redemption in its OWN transaction so it survives the
    main rollback. Best-effort: never masks the original error."""
    try:
        db = _db()
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO operator_security_audit (subject, action,"
                " decision, reason, authority_id, environment)"
                " VALUES (%s, %s, 'deny', %s, %s, %s)",
                (op.subject, ACTION, reason, authority_id, op.environment))
    except Exception:  # noqa: BLE001 - best-effort on the deny path
        pass


def execute(op, credential_handle: str, authority_id: str) -> Dict[str, Any]:
    """Atomic: principal re-check + authority consume + external rotation +
    revision bump + audit in ONE PostgreSQL transaction. Any failure rolls the
    whole thing back; the denial is recorded separately."""
    _validate_handle(credential_handle)
    meta = _broker_verify(credential_handle, op.environment)  # re-check (defense in depth)
    scope = meta.get("scope")
    args = _args(credential_handle)

    # Fast-fail admission (the AUTHORITATIVE checks are inside the transaction,
    # on locked rows).
    if operator_authority.kill_switch_active():
        _audit_deny(op, "kill_switch_active", authority_id)
        raise RunbookError("kill_switch_active")
    if not operator_principals.revalidate(op.subject, op.role_revision):
        _audit_deny(op, "principal_drift", authority_id)
        raise RunbookError("principal_drift")

    db = _db()
    try:
        with db.connection() as conn, conn.cursor() as cur:
            # 0a. Serialize ALL rotations of this handle, INCLUDING the first
            #     (no row yet). A namespaced, transaction-scoped advisory lock
            #     covers the not-yet-created row and cannot collide with a
            #     tenant lock key.
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (_LOCK_PREFIX + credential_handle,))

            # 0b. Authoritative principal check, LOCKED (closes the
            #     revoke-after-preflight TOCTOU).
            cur.execute(
                "SELECT status, role_revision FROM operator_principals"
                " WHERE subject = %s FOR UPDATE", (op.subject,))
            prow = cur.fetchone()
            if (prow is None or prow["status"] != "active"
                    or int(prow["role_revision"]) != int(op.role_revision)):
                raise RunbookError("principal_drift")

            # 1. Lock the rotation row and read the current revision.
            cur.execute(
                "SELECT revision FROM operator_credential_rotations"
                " WHERE handle = %s FOR UPDATE", (credential_handle,))
            row = cur.fetchone()
            current_rev = int(row["revision"]) if row is not None else 0

            # 2. Consume the one-use authority BOUND to the locked revision. A
            #    drift/replay/mismatch fails the WHERE -> AuthorityDenied -> the
            #    whole tx rolls back (authority stays granted, nothing rotated).
            operator_authority.consume_in_tx(
                cur, authority_id, op.subject, op.role_revision, op.environment,
                ACTION, args, target_revision=str(current_rev))

            # 3. Perform the external rotation through the broker. DARK by
            #    default (no rotator) -> SecretBrokerError -> RunbookError ->
            #    rollback: fail-closed, nothing bumped, authority un-consumed.
            try:
                broker.rotate(credential_handle, op.environment, subject=op.subject)
            except broker.SecretBrokerError as exc:
                raise RunbookError(exc.reason) from None

            # 4. Bump the rotation revision (the reversible state).
            cur.execute(
                """
                INSERT INTO operator_credential_rotations
                  (handle, revision, rotated_at, updated_at)
                VALUES (%s, %s, now(), now())
                ON CONFLICT (handle) DO UPDATE
                  SET revision = operator_credential_rotations.revision + 1,
                      rotated_at = now(),
                      updated_at = now()
                RETURNING revision
                """,
                (credential_handle, current_rev + 1))
            after_rev = int(cur.fetchone()["revision"])

            # 5. Security audit, in the SAME transaction. Records the scope so
            #    the reversal ("re-issue the previous scope") is auditable; no
            #    secret value is ever written.
            cur.execute(
                "INSERT INTO operator_security_audit (subject, action,"
                " decision, reason, authority_id, environment, extra)"
                " VALUES (%s, %s, 'execute', 'runbook_applied', %s, %s, %s)",
                (op.subject, ACTION, authority_id, op.environment,
                 Jsonb({"credential_handle": credential_handle, "scope": scope,
                        "approver": op.subject,
                        "reversal": "reissue_previous_scope",
                        "before": {"revision": current_rev},
                        "after": {"revision": after_rev}})))
            # clean exit: db.connection() commits steps 0/2/4/5 atomically.
    except (operator_authority.AuthorityDenied, RunbookError) as exc:
        _audit_deny(op, getattr(exc, "reason", "denied"), authority_id)
        raise

    return {
        "action": ACTION, "credential_handle": credential_handle,
        "authority_id": authority_id, "scope": scope,
        "before": {"revision": current_rev},
        "after": {"revision": after_rev},
        # Reversal: the secret is gone, but a fresh credential can be re-issued
        # for this scope (contract reversal for O4 credential rotation).
        "reversal": {"reissue_scope": scope},
    }
