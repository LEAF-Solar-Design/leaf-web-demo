"""Operator runbook: a scoped external write (contract/OPERATOR.md section 4.1,
operator.external_write; O5, Wave 3). Always-confirm, spend=usd, ships dark.

Flow (split, always-confirm):
  propose  -> verify the destination is ALLOWLISTED and non-production, verify
              the token handle is broker-scoped and non-production, and mint a
              one-use authority. The USD spend is reserved in the mint
              transaction (operator_authority._reserve_spend).
  execute  -> ONE PostgreSQL transaction that (0b) re-checks the principal on a
              LOCKED row, (1) consumes the one-use authority bound to the exact
              args (destination + handle + adapter + payload), (2) performs
              EXACTLY ONE outbound write through the registered adapter, with a
              short-lived token injected by the secret broker's with_injected
              (the token is never returned or logged), and (3) writes the
              security audit. db.connection() commits on clean exit and rolls
              back on any exception, so authority redemption and the audit are
              atomic. Invariant 6.

DARK by construction. No adapter ships in v1 (contract reversal: "per-adapter
documented reversal, or the adapter does not ship"), so execute fails closed
with `no_adapter`; and even with an adapter, the broker mints no token until a
minter is registered, so with_injected fails closed with `no_minter`. The
one-use authority (max_uses=1, args-bound) is the exactly-once guard, so no
per-destination revision or advisory lock is needed. The outbound write runs
inside the transaction while the token is injected: a failure guarantees the
authority is not consumed and no audit is written (the same at-least-once
external caveat as any side effect crossing a DB commit; the reversal is the
adapter's documented reversal).
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


def _audit_deny(op, reason: str, authority_id: str) -> None:
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


def execute(op, destination: str, token_handle: str, adapter: str,
            authority_id: str,
            payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Atomic: principal re-check + authority consume + one scoped outbound
    write + audit in ONE PostgreSQL transaction. Any failure rolls it all back;
    the denial is recorded separately."""
    self_meta = _verify(destination, token_handle, adapter, op.environment)  # re-check
    args = _args(destination, token_handle, adapter, payload)

    if operator_authority.kill_switch_active():
        _audit_deny(op, "kill_switch_active", authority_id)
        raise RunbookError("kill_switch_active")
    if not operator_principals.revalidate(op.subject, op.role_revision):
        _audit_deny(op, "principal_drift", authority_id)
        raise RunbookError("principal_drift")

    reversal = ""
    db = _db()
    try:
        with db.connection() as conn, conn.cursor() as cur:
            # 0b. Authoritative principal check, LOCKED. FOR UPDATE on the
            #     principal serializes a subject's operator actions and closes
            #     the revoke-after-preflight TOCTOU; combined with the one-use
            #     conditional consume it also gives exactly-once redemption.
            cur.execute(
                "SELECT status, role_revision FROM operator_principals"
                " WHERE subject = %s FOR UPDATE", (op.subject,))
            prow = cur.fetchone()
            if (prow is None or prow["status"] != "active"
                    or int(prow["role_revision"]) != int(op.role_revision)):
                raise RunbookError("principal_drift")

            # 1. Consume the one-use authority bound to the exact args. No
            #    target_revision: external_write has no revisioned target, so
            #    max_uses=1 + args_hash is the guard.
            operator_authority.consume_in_tx(
                cur, authority_id, op.subject, op.role_revision, op.environment,
                ACTION, args)

            # 2. The adapter must be registered (DARK by default -> no_adapter,
            #    fail closed, rolls back the consume).
            registered = ext.get_adapter(adapter)
            if registered is None:
                raise RunbookError("no_adapter")
            reversal = registered.reversal

            # 3. Perform EXACTLY ONE outbound write with a short-lived token the
            #    broker injects. with_injected returns a constant and never
            #    surfaces the token; a broker/adapter failure (incl. dark broker
            #    -> no_minter) raises SecretBrokerError, mapped to a RunbookError
            #    so the whole transaction rolls back.
            def _use(token: str) -> None:
                registered.write(destination, payload or {}, token)
            try:
                broker.with_injected(token_handle, op.environment, _use,
                                     subject=op.subject)
            except broker.SecretBrokerError as exc:
                raise RunbookError(exc.reason) from None

            # 4. Security audit, in the SAME transaction. Records the adapter's
            #    documented reversal; no token or payload secret is written.
            cur.execute(
                "INSERT INTO operator_security_audit (subject, action,"
                " decision, reason, authority_id, environment, extra)"
                " VALUES (%s, %s, 'execute', 'runbook_applied', %s, %s, %s)",
                (op.subject, ACTION, authority_id, op.environment,
                 Jsonb({"destination": destination, "adapter": adapter,
                        "token_handle": token_handle, "approver": op.subject,
                        "reversal": reversal})))
            # clean exit: consume + audit commit atomically.
    except (operator_authority.AuthorityDenied, RunbookError) as exc:
        _audit_deny(op, getattr(exc, "reason", "denied"), authority_id)
        raise

    return {"action": ACTION, "destination": destination, "adapter": adapter,
            "authority_id": authority_id, "environment": self_meta["environment"],
            "reversal": {"adapter_reversal": reversal}}
