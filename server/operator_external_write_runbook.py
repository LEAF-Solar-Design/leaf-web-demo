"""Operator runbook: a scoped external write (contract/OPERATOR.md section 4.1,
operator.external_write; O5, Wave 3). Always-confirm, spend=usd, ships dark.

Flow (split, always-confirm):
  propose  -> verify the destination is ALLOWLISTED and non-production, verify
              the token handle is broker-scoped and non-production, and mint a
              one-use authority. The USD spend is reserved in the mint
              transaction (operator_authority._reserve_spend).
  execute  -> AT-MOST-ONCE, in two phases. Phase 1 is ONE PostgreSQL
              transaction that (0b) re-checks the principal on a LOCKED row,
              (1) consumes the one-use authority bound to the exact args
              (destination + handle + adapter + payload), and (2) records the
              redemption audit. db.connection() commits on clean exit and rolls
              back on any exception, so authority redemption and its audit are
              atomic (invariant 6). ONLY AFTER phase 1 commits, phase 2 performs
              EXACTLY ONE outbound write through the registered adapter with a
              short-lived token injected by the secret broker's with_injected
              (the token is never returned or logged), then audits the outcome.

Why two phases. An external side effect cannot join a DB commit atomically, so
if the write ran inside the transaction an adapter that LANDS the write and then
raises (a response timeout), or a commit that fails after a successful write,
would roll the consume back and leave the still-granted authority able to REPEAT
the write. For a paid (spend=usd) write a silent duplicate is worse than a
dropped one, so the authority is spent FIRST: once phase 1 commits, the write
runs at most once and an ambiguous failure is NOT auto-retried. A landed-but-
failed write is covered by the adapter's documented reversal, recorded in the
audit.

DARK by construction. No adapter ships in v1 (contract reversal: "per-adapter
documented reversal, or the adapter does not ship"), so execute fails closed
with `no_adapter` BEFORE the authority is consumed (the authority is preserved);
and even with an adapter, the broker mints no token until a minter is
registered, so with_injected fails closed with `no_minter`. The one-use
authority (max_uses=1, args-bound) is the exactly-once redemption guard, so no
per-destination revision or advisory lock is needed.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

import operator_authority
import operator_external_adapters as ext
import operator_principals
import operator_secret_broker as broker
from operator_principals import _db
from psycopg.types.json import Jsonb

ACTION = "operator.external_write"
_NON_PRODUCTION = {"staging", "development"}


class RunbookError(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _broker_verify(token_handle: str, environment: str) -> Dict[str, Any]:
    meta = broker.describe(token_handle)
    if meta is None:
        raise RunbookError("token_handle_unknown")
    if (meta.get("environment") not in _NON_PRODUCTION
            or environment not in _NON_PRODUCTION):
        raise RunbookError("production_scope_refused")
    if meta.get("environment") != environment:
        raise RunbookError("environment_mismatch")
    return meta


def _verify(destination: str, token_handle: str, adapter: str,
            environment: str) -> Dict[str, Any]:
    """Server-owned precondition: allowlisted destination + broker-scoped token
    handle, both non-production. Maps the allowlist error to a RunbookError."""
    try:
        allowed = ext.verify_allowed(destination, adapter, environment)
    except ext.ExternalWriteError as exc:
        raise RunbookError(exc.reason) from None
    _broker_verify(token_handle, environment)
    return allowed


def _args(destination: str, token_handle: str, adapter: str,
          payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    args: Dict[str, Any] = {"destination": destination,
                            "token_handle": token_handle, "adapter": adapter}
    if payload is not None:
        args["payload"] = payload
    return args


def propose(op, destination: str, token_handle: str, adapter: str,
            payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    self_meta = _verify(destination, token_handle, adapter, op.environment)
    minted = operator_authority.mint(
        op.subject, op.role_revision, op.profile, op.environment,
        session_id=f"runbook:{op.subject}", turn_id=None, action=ACTION,
        args=_args(destination, token_handle, adapter, payload))
    return {"authority_id": minted["authority_id"], "action": ACTION,
            "destination": destination, "adapter": adapter,
            "token_handle": token_handle, "environment": self_meta["environment"],
            "expires_at": minted["expires_at"]}


def _audit_row(op, decision: str, reason: str, authority_id: str,
               extra: Optional[Dict[str, Any]] = None) -> None:
    """Write ONE audit row in its OWN transaction. Best-effort: never masks the
    caller's error, and never writes a token or payload secret."""
    try:
        db = _db()
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO operator_security_audit (subject, action,"
                " decision, reason, authority_id, environment, extra)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (op.subject, ACTION, decision, reason, authority_id,
                 op.environment, Jsonb(extra or {})))
    except Exception:  # noqa: BLE001 - audit is best-effort
        pass


def _audit_deny(op, reason: str, authority_id: str) -> None:
    _audit_row(op, "deny", reason, authority_id)


def execute(op, destination: str, token_handle: str, adapter: str,
            authority_id: str,
            payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """AT-MOST-ONCE. The one-use authority is consumed and audited in ONE
    PostgreSQL transaction (invariant 6), and ONLY AFTER that commits does the
    single outbound write run.

    Why not one transaction around the write too: an external side effect cannot
    join a DB commit atomically. If the write ran inside the transaction, an
    adapter that LANDS the write and then raises (a response timeout), or a
    commit that fails after a successful write, would roll the consume back and
    leave the still-granted authority able to REPEAT the write. For a paid
    (spend=usd) write, a silent duplicate is worse than a dropped one, so the
    safe default is to spend the authority FIRST: once phase 1 commits the
    authority is gone, the write below runs at most once, and an ambiguous
    failure is NOT auto-retried. A landed-but-failed write is covered by the
    adapter's documented reversal, recorded in the audit."""
    self_meta = _verify(destination, token_handle, adapter, op.environment)  # re-check
    args = _args(destination, token_handle, adapter, payload)

    if operator_authority.kill_switch_active():
        _audit_deny(op, "kill_switch_active", authority_id)
        raise RunbookError("kill_switch_active")
    if not operator_principals.revalidate(op.subject, op.role_revision):
        _audit_deny(op, "principal_drift", authority_id)
        raise RunbookError("principal_drift")

    # The adapter must be registered BEFORE the authority is consumed, so a dark
    # deployment (no adapter) fails closed WITHOUT spending the authority.
    registered = ext.get_adapter(adapter)
    if registered is None:
        _audit_deny(op, "no_adapter", authority_id)
        raise RunbookError("no_adapter")
    reversal = registered.reversal

    # Phase 1 (atomic, invariant 6): consume the one-use authority + record the
    # redemption in ONE transaction. FOR UPDATE on the principal closes the
    # revoke-after-preflight TOCTOU and, with the conditional consume, gives
    # exactly-once redemption. On any failure the whole tx rolls back (authority
    # stays granted, no audit) and the denial is recorded separately.
    db = _db()
    try:
        with db.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status, role_revision FROM operator_principals"
                " WHERE subject = %s FOR UPDATE", (op.subject,))
            prow = cur.fetchone()
            if (prow is None or prow["status"] != "active"
                    or int(prow["role_revision"]) != int(op.role_revision)):
                raise RunbookError("principal_drift")
            # No target_revision: external_write has no revisioned target, so
            # max_uses=1 + args_hash is the exactly-once guard.
            operator_authority.consume_in_tx(
                cur, authority_id, op.subject, op.role_revision, op.environment,
                ACTION, args)
            cur.execute(
                "INSERT INTO operator_security_audit (subject, action,"
                " decision, reason, authority_id, environment, extra)"
                " VALUES (%s, %s, 'execute', 'authority_consumed', %s, %s, %s)",
                (op.subject, ACTION, authority_id, op.environment,
                 Jsonb({"destination": destination, "adapter": adapter,
                        "token_handle": token_handle, "approver": op.subject,
                        "reversal": reversal})))
            # clean exit: the authority is now SPENT (at-most-once boundary).
    except (operator_authority.AuthorityDenied, RunbookError) as exc:
        _audit_deny(op, getattr(exc, "reason", "denied"), authority_id)
        raise

    # Phase 2 (post-commit, AT MOST ONCE): perform EXACTLY ONE outbound write
    # with a short-lived token the broker injects. The authority is already
    # spent, so a failure here cannot be replayed into a duplicate write; it
    # requires a fresh approval to retry. with_injected returns a constant and
    # never surfaces the token; a broker/adapter failure (incl. dark broker ->
    # no_minter) raises SecretBrokerError, recorded and re-raised value-free.
    def _use(token: str) -> None:
        registered.write(destination, payload or {}, token)
    try:
        broker.with_injected(token_handle, op.environment, _use,
                             subject=op.subject)
    except broker.SecretBrokerError as exc:
        _audit_row(op, "execute", f"write_failed:{exc.reason}", authority_id,
                   {"destination": destination, "adapter": adapter})
        raise RunbookError(exc.reason) from None

    _audit_row(op, "execute", "runbook_applied", authority_id,
               {"destination": destination, "adapter": adapter,
                "reversal": reversal})
    return {"action": ACTION, "destination": destination, "adapter": adapter,
            "authority_id": authority_id, "environment": self_meta["environment"],
            "reversal": {"adapter_reversal": reversal}}
