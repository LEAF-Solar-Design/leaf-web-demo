"""One-use execution authority (contract/OPERATOR.md section 5).

Mint: admission (kill switches -> catalog -> args schema -> principal
revalidation -> rate/budget reservation) + authority insert + security-audit
row in ONE PostgreSQL transaction. Redeem: a conditional single-row consume
inside the handler transaction; concurrent redemptions admit exactly one.
Every deny path writes a distinct audit reason. Database or audit
unavailability denies (the exception propagates to the caller's 503).
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from psycopg.types.json import Jsonb

import operator_policy
from operator_principals import _db


class AuthorityDenied(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _kill_file() -> str:
    return os.environ.get("LEAF_OPERATOR_KILL_FILE",
                          str(os.path.join("data", "operator.disabled")))


def kill_switch_active() -> bool:
    return os.path.exists(_kill_file())


def _audit(cur, subject: str, session_id: Optional[str],
           turn_id: Optional[str], action: str, decision: str, reason: str,
           authority_id: Optional[str] = None, args_hash: Optional[str] = None,
           policy_rev: Optional[str] = None,
           environment: Optional[str] = None,
           extra: Optional[Dict[str, Any]] = None) -> None:
    cur.execute(
        """
        INSERT INTO operator_security_audit
          (subject, session_id, turn_id, action, decision, reason,
           authority_id, args_hash, policy_revision, environment, extra)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (subject, session_id, turn_id, action, decision, reason,
         authority_id, args_hash, policy_rev, environment,
         Jsonb(extra or {})))


def _reserve_rate(cur, subject: str, action: str, category: str,
                  catalog: Dict[str, Any]) -> bool:
    ceiling = int(catalog.get("rate_limits_per_hour", {}).get(category, 0))
    if ceiling <= 0:
        return False
    hour_key = datetime.now(timezone.utc).strftime(f"{action}@%Y%m%d%H")
    # Adopt the CURRENT ceiling (EXCLUDED.ceiling) on every reservation and
    # guard against it, so a tightened policy (e.g. 240 -> 12) takes effect
    # immediately within the running hour rather than only at the next hour
    # key. A loosened ceiling likewise applies at once.
    cur.execute(
        """
        INSERT INTO operator_budgets (subject, scope, scope_key, used, ceiling)
        VALUES (%s, 'action_hour', %s, 1, %s)
        ON CONFLICT (subject, scope, scope_key) DO UPDATE
          SET used = operator_budgets.used + 1,
              ceiling = EXCLUDED.ceiling,
              updated_at = now()
          WHERE operator_budgets.used < EXCLUDED.ceiling
        RETURNING used
        """,
        (subject, hour_key, ceiling))
    return cur.fetchone() is not None


def _reserve_spend(cur, subject: str, action: str, entry: Dict[str, Any],
                   catalog: Dict[str, Any]) -> Optional[str]:
    """Reserve this action's bounded spend against a per-principal-day ceiling,
    in the SAME transaction as the mint (contract/OPERATOR.md section 5:
    admission includes spend reservation; invariant 6: it commits with the
    authority insert). Returns None on success, or a distinct deny reason.

    Only USD spend is reserved here. `none` reserves nothing. `cost_tokens` is
    the live worker lane, metered by the worker/broker rather than this mint
    path, so it is allowed WITHOUT a reservation here (denying it would regress
    the enabled O2 action). The parser requires every action to declare a spend
    type in {none, cost_tokens, usd}, so there is no unknown type to reach this
    function. The reservation is coarse and conservative: the per-action
    `max_spend_cents` is reserved at
    mint and NOT refunded if redemption is later denied, exactly as the rate
    counter is consumed at mint. A missing/zero ceiling or per-action cap denies
    (fail-closed), so an operator cannot enable a usd action without also
    configuring its spend limits."""
    spend_type = entry.get("spend") or "none"
    if spend_type in ("none", "cost_tokens"):
        # `none` reserves nothing. `cost_tokens` is the live worker lane, whose
        # token spend is metered by the worker/broker rather than this mint
        # path; reserving nothing here is correct and does NOT deny it. The
        # parser guarantees spend is one of {none, cost_tokens, usd}, so there
        # is no unknown type to fall through to.
        return None
    amount = int(entry.get("max_spend_cents", 0))
    ceiling = int(catalog.get("spend_limits", {}).get(
        "usd_principal_day_cents", 0))
    if amount <= 0 or ceiling <= 0 or amount > ceiling:
        return "spend_unconfigured"
    day_key = datetime.now(timezone.utc).strftime("usd@%Y%m%d")
    # Adopt the CURRENT ceiling on every reservation (a tightened cap applies at
    # once) and guard the running total, including the first insert, against it.
    cur.execute(
        """
        INSERT INTO operator_budgets (subject, scope, scope_key, used, ceiling)
        VALUES (%s, 'spend_principal_day', %s, %s, %s)
        ON CONFLICT (subject, scope, scope_key) DO UPDATE
          SET used = operator_budgets.used + EXCLUDED.used,
              ceiling = EXCLUDED.ceiling,
              updated_at = now()
          WHERE operator_budgets.used + EXCLUDED.used <= EXCLUDED.ceiling
        RETURNING used
        """,
        (subject, day_key, amount, ceiling))
    return None if cur.fetchone() is not None else "spend_exhausted"


def mint(subject: str, role_revision: int, profile: str, environment: str,
         session_id: str, turn_id: Optional[str], action: str,
         args: Dict[str, Any],
         target_revision: Optional[str] = None,
         idempotency_key: Optional[str] = None) -> Dict[str, Any]:
    """Admission + mint + audit in one transaction. Raises AuthorityDenied
    with a distinct reason on every closed gate; DB errors propagate."""
    import operator_principals

    catalog = operator_policy.load_catalog()
    policy_rev = operator_policy.policy_revision(catalog)
    args_hash = operator_policy.canonical_args_hash(args)
    db = _db()
    with db.connection() as conn, conn.cursor() as cur:
        def deny(reason: str) -> AuthorityDenied:
            _audit(cur, subject, session_id, turn_id, action, "deny", reason,
                   args_hash=args_hash, policy_rev=policy_rev,
                   environment=environment)
            conn.commit()
            return AuthorityDenied(reason)

        if kill_switch_active():
            raise deny("kill_switch_active")
        if catalog is None:
            raise deny("policy_catalog_absent")
        entry = catalog["actions"].get(action)
        if entry is None or entry.get("enabled", True) is not True:
            raise deny("action_not_in_catalog")
        try:
            import jsonschema
            jsonschema.validate(args, entry["args_schema"])
        except ImportError:
            raise deny("schema_validator_unavailable")
        except Exception:
            raise deny("args_schema_violation")
        if not operator_principals.revalidate(subject, role_revision):
            raise deny("principal_drift")
        if not _reserve_rate(cur, subject, action, entry.get("rate", "high"),
                             catalog):
            raise deny("rate_exhausted")
        spend_denial = _reserve_spend(cur, subject, action, entry, catalog)
        if spend_denial is not None:
            raise deny(spend_denial)

        authority_id = f"opauth-{uuid.uuid4()}"
        ttl = int(catalog.get("authority_ttl_s", 300))
        expires = datetime.now(timezone.utc) + timedelta(seconds=ttl)
        cur.execute(
            """
            INSERT INTO operator_authorities
              (authority_id, subject, role_revision, profile, session_id,
               turn_id, action, args_hash, target_revision, policy_revision,
               environment, expires_at, nonce, idempotency_key)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (authority_id, subject, role_revision, profile, session_id,
             turn_id, action, args_hash, target_revision, policy_rev,
             environment, expires, uuid.uuid4().hex, idempotency_key))
        _audit(cur, subject, session_id, turn_id, action, "mint",
               "authority_minted", authority_id=authority_id,
               args_hash=args_hash, policy_rev=policy_rev,
               environment=environment)
        conn.commit()
    return {"authority_id": authority_id, "action": action,
            "args_hash": args_hash, "policy_revision": policy_rev,
            "expires_at": expires.isoformat()}


def redeem(authority_id: str, subject: str, role_revision: int,
           environment: str, action: str, args: Dict[str, Any],
           target_revision: Optional[str] = None) -> Dict[str, Any]:
    """Atomic one-use consume. The conditional UPDATE is the admission: of N
    concurrent redeemers exactly one sees a row. Every mismatch re-checked
    here denies with its own reason even if the mint-time check passed."""
    import operator_principals

    args_hash = operator_policy.canonical_args_hash(args)
    policy_rev = operator_policy.policy_revision()
    db = _db()
    with db.connection() as conn, conn.cursor() as cur:
        def deny(reason: str) -> AuthorityDenied:
            _audit(cur, subject, None, None, action, "deny", reason,
                   authority_id=authority_id, args_hash=args_hash,
                   policy_rev=policy_rev, environment=environment)
            conn.commit()
            return AuthorityDenied(reason)

        if kill_switch_active():
            raise deny("kill_switch_active")
        if not operator_principals.revalidate(subject, role_revision):
            raise deny("principal_drift")

        # Every binding sits in the WHERE clause: a mismatched caller can
        # never consume the row, so a legitimate redeemer is not denial-of-
        # servable by a forged redemption attempt.
        cur.execute(
            """
            UPDATE operator_authorities SET
              used_count = used_count + 1,
              status = 'consumed'
            WHERE authority_id = %s AND used_count < max_uses
              AND status = 'granted' AND expires_at > now()
              AND subject = %s AND role_revision = %s AND action = %s
              AND args_hash = %s AND policy_revision = %s
              AND environment = %s
              AND COALESCE(target_revision, '') = COALESCE(%s, '')
            RETURNING authority_id
            """,
            (authority_id, subject, int(role_revision), action, args_hash,
             policy_rev, environment, target_revision))
        row = cur.fetchone()
        if row is None:
            # Classify without consuming, for the distinct audit reason.
            cur.execute(
                "SELECT subject, role_revision, action, args_hash,"
                " target_revision, policy_revision, environment, status,"
                " used_count, max_uses, expires_at <= now() AS expired"
                " FROM operator_authorities WHERE authority_id = %s",
                (authority_id,))
            probe = cur.fetchone()
            if probe is None:
                raise deny("authority_absent")
            (p_subject, p_rev, p_action, p_hash, p_policy, p_env,
             p_status, p_used, p_max, p_expired) = (
                probe["subject"], probe["role_revision"], probe["action"],
                probe["args_hash"], probe["policy_revision"],
                probe["environment"], probe["status"], probe["used_count"],
                probe["max_uses"], probe["expired"])
            if p_status != "granted" or p_used >= p_max:
                raise deny("authority_replayed")
            if p_expired:
                raise deny("authority_expired")
            if p_subject != subject:
                raise deny("subject_mismatch")
            if int(p_rev) != int(role_revision):
                raise deny("role_revision_mismatch")
            if p_action != action:
                raise deny("action_mismatch")
            if p_hash != args_hash:
                raise deny("args_mismatch")
            if p_policy != policy_rev:
                raise deny("policy_revision_drift")
            if p_env != environment:
                raise deny("environment_mismatch")
            raise deny("target_drift")

        _audit(cur, subject, None, None, action, "redeem",
               "authority_consumed", authority_id=authority_id,
               args_hash=args_hash, policy_rev=policy_rev,
               environment=environment)
        conn.commit()
    return {"authority_id": authority_id, "redeemed": True}


def consume_in_tx(cur, authority_id: str, subject: str, role_revision: int,
                  environment: str, action: str, args: Dict[str, Any],
                  target_revision: Optional[str] = None) -> Dict[str, Any]:
    """Consume the one-use authority on the CALLER's cursor, WITHOUT committing.

    The caller's transaction provides single-transaction atomicity with the
    downstream reversible action and the security audit: an exception raised
    here (AuthorityDenied) rolls the whole transaction back, so the authority
    is NOT consumed unless the action and audit also commit. Same conditional
    UPDATE and deny classification as redeem(); every binding sits in the
    WHERE clause, so a mismatched caller can never consume the row.
    """
    args_hash = operator_policy.canonical_args_hash(args)
    policy_rev = operator_policy.policy_revision()
    cur.execute(
        """
        UPDATE operator_authorities SET
          used_count = used_count + 1,
          status = 'consumed'
        WHERE authority_id = %s AND used_count < max_uses
          AND status = 'granted' AND expires_at > now()
          AND subject = %s AND role_revision = %s AND action = %s
          AND args_hash = %s AND policy_revision = %s
          AND environment = %s
          AND COALESCE(target_revision, '') = COALESCE(%s, '')
        RETURNING authority_id
        """,
        (authority_id, subject, int(role_revision), action, args_hash,
         policy_rev, environment, target_revision))
    if cur.fetchone() is not None:
        return {"authority_id": authority_id, "args_hash": args_hash,
                "policy_revision": policy_rev}
    # Classify the failure with a distinct reason (no commit; the caller's
    # transaction rolls back regardless).
    cur.execute(
        "SELECT subject, role_revision, action, args_hash, policy_revision,"
        " environment, status, used_count, max_uses,"
        " expires_at <= now() AS expired"
        " FROM operator_authorities WHERE authority_id = %s",
        (authority_id,))
    probe = cur.fetchone()
    if probe is None:
        raise AuthorityDenied("authority_absent")
    if probe["status"] != "granted" or probe["used_count"] >= probe["max_uses"]:
        raise AuthorityDenied("authority_replayed")
    if probe["expired"]:
        raise AuthorityDenied("authority_expired")
    if probe["subject"] != subject:
        raise AuthorityDenied("subject_mismatch")
    if int(probe["role_revision"]) != int(role_revision):
        raise AuthorityDenied("role_revision_mismatch")
    if probe["action"] != action:
        raise AuthorityDenied("action_mismatch")
    if probe["args_hash"] != args_hash:
        raise AuthorityDenied("args_mismatch")
    if probe["policy_revision"] != policy_rev:
        raise AuthorityDenied("policy_revision_drift")
    if probe["environment"] != environment:
        raise AuthorityDenied("environment_mismatch")
    raise AuthorityDenied("target_drift")
