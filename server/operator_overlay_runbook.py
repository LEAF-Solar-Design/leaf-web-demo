"""Operator runbook: set a revision-guarded tenant agent-policy overlay
(contract/OPERATOR.md Lane F). Built on the single-transaction primitive
operator_runbook_tx.execute_atomic().

The overlay is TIGHTEN-ONLY: it may only move a tenant's agent-policy action
UP the auto -> confirm-once -> always-confirm ordering (or disable it), never
loosen. Validation reuses agent_policy.apply_tenant_overlay so the operator
runbook and the tenant gate agree on what "tighten" means. The authority is
bound to the exact overlay bytes (canonical args hash), so a post-approval
overlay swap fails redemption.
"""
from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

import agent_policy
import operator_authority
import operator_runbook_tx as tx
from operator_runbook_tx import RunbookError
from psycopg.types.json import Jsonb

ACTION = "operator.tenant_overlay_set"
_TENANT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")


def _validate_tenant(tenant_id: str) -> None:
    if not isinstance(tenant_id, str) or not _TENANT_ID.match(tenant_id):
        raise RunbookError("tenant_id_invalid")


def _validate_overlay(overlay: Any) -> None:
    """Refuse anything that is not a tighten-only overlay over KNOWN actions.
    Reuses the tenant gate's own validation so the two never disagree."""
    if not isinstance(overlay, dict) or not overlay:
        raise RunbookError("overlay_invalid")
    policy = agent_policy.load_policy()
    for name, entry in overlay.items():
        action = policy.actions.get(str(name))
        if action is None:
            raise RunbookError("overlay_unknown_action")
        try:
            agent_policy.apply_tenant_overlay(action, {str(name): entry})
        except agent_policy.PolicyError as exc:
            raise RunbookError(f"overlay_not_tightening: {exc}") from exc


def _require_no_loosening(current_overlay: Dict[str, Any],
                          new_overlay: Dict[str, Any]) -> None:
    """Authoritative tighten-only check RELATIVE to the tenant's CURRENT
    overlay (must be called under the row lock, inside the transaction). The
    new overlay REPLACES the current one, so for every action touched by
    either overlay the new effective policy may not move DOWN the
    auto -> confirm-once -> always-confirm order, and a currently-disabled
    action may not be re-enabled. Omitting an action that the current overlay
    tightened would loosen it, and is refused."""
    policy = agent_policy.load_policy()
    for name in set(current_overlay) | set(new_overlay):
        action = policy.actions.get(str(name))
        if action is None:
            raise RunbookError("overlay_unknown_action")
        cur_eff = agent_policy.apply_tenant_overlay(action, current_overlay)
        new_eff = agent_policy.apply_tenant_overlay(action, new_overlay)
        if (agent_policy.POLICY_ORDER[new_eff.policy]
                < agent_policy.POLICY_ORDER[cur_eff.policy]):
            raise RunbookError("overlay_would_loosen_policy")
        if new_eff.enabled and not cur_eff.enabled:
            raise RunbookError("overlay_would_reenable")


def resolve_target(tenant_id: str) -> Dict[str, Any]:
    _validate_tenant(tenant_id)
    state = agent_policy.load_tenant_state(tenant_id)
    return {"tenant_id": tenant_id, "overlay": state.get("overlay", {}),
            "revision": state.get("revision")}


def _args(tenant_id: str, overlay: Dict[str, Any]) -> Dict[str, Any]:
    return {"tenant_id": tenant_id, "overlay": overlay}


def propose(op, tenant_id: str, overlay: Dict[str, Any]) -> Dict[str, Any]:
    _validate_tenant(tenant_id)
    _validate_overlay(overlay)
    target = resolve_target(tenant_id)
    if target["revision"] is None:
        raise RunbookError("tenant_state_not_revisioned")
    minted = operator_authority.mint(
        op.subject, op.role_revision, op.profile, op.environment,
        session_id=f"runbook:{op.subject}", turn_id=None, action=ACTION,
        args=_args(tenant_id, overlay),
        target_revision=str(int(target["revision"])))
    return {"authority_id": minted["authority_id"], "action": ACTION,
            "tenant_id": tenant_id, "target_revision": target["revision"],
            "before": target, "expires_at": minted["expires_at"]}


def execute(op, tenant_id: str, overlay: Dict[str, Any],
            authority_id: str) -> Dict[str, Any]:
    _validate_tenant(tenant_id)
    _validate_overlay(overlay)  # re-validate at execute (defense in depth)
    args = _args(tenant_id, overlay)

    def _apply(cur, current_rev: int,
               row: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        before_overlay = (row["overlay"] if row is not None
                          and row.get("overlay") is not None else {})
        # Authoritative tighten-only check against the LOCKED current overlay:
        # a replacement that would drop an existing restriction is a loosening
        # and rolls the whole transaction back.
        _require_no_loosening(before_overlay, overlay)
        cur.execute(
            """
            INSERT INTO agent_tenant_state
              (tenant_id, agent_disabled, overlay, revision, updated_at)
            VALUES (%s, false, %s, %s, now())
            ON CONFLICT (tenant_id) DO UPDATE
              SET overlay = EXCLUDED.overlay,
                  revision = agent_tenant_state.revision + 1,
                  updated_at = now()
            RETURNING overlay, revision
            """,
            (tenant_id, Jsonb(overlay), current_rev + 1))
        applied = cur.fetchone()
        return ({"overlay": before_overlay, "revision": current_rev},
                {"overlay": applied["overlay"],
                 "revision": int(applied["revision"])})

    out = tx.execute_atomic(op, ACTION, tenant_id, authority_id, args,
                            apply=_apply)
    # Reversal data: restore the prior overlay (revision-guarded, same runbook).
    out["reversal"] = {"restore_overlay": out["before"]["overlay"]}
    return out
