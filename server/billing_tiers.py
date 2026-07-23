"""
server/billing_tiers.py — the CANONICAL billing plan → platform tier mapping.

One question, one module: *which frozen tier does a subscription state resolve
to?* This is the Python source of truth for the mapping the Auth0 Post-Login
Action (auth0-actions/post-login-add-tenant-claim.js) applies at login time.
The Action is pasted into the Auth0 dashboard BY HAND (redeploy is manual), so
the two copies can drift silently — `load_action_billing_constants()` parses
the JS file and the gate suite (tests/test_billing_tiers.py) fails the build
the moment the copies disagree. Change the mapping HERE first; the test then
points at the exact JS constant an operator must re-paste.

Consumed by two legs of the same billing state (contract/BILLING.md):

  * login leg  — the Action mints the `https://leafdesign.ai/tier` claim
    (JS copy of this table);
  * stored leg — POST /api/orgs/{org_id}/billing/tier-sync (platform lane)
    updates the org row's stored tier, which the platform jobs lane branches
    on (platform/entitlements.py). That route loads THIS module.

INVARIANT (contract/AUTH.md §0): billing state never carries or references a
Claude/Anthropic credential. This module maps subscription facts to a tier
string, nothing else — LLM supply stays swappable.

Dependency-free on purpose (stdlib only): importable by the server lane, the
platform lane (importlib seam), and static tests without FastAPI or a DB.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, FrozenSet, Optional

# --------------------------------------------------------------------------- #
# FROZEN vocabulary (contract/AUTH.md §11). Additions are an operator-promotion
# ritual; the freeze gate (tests/test_auth_vocab_freeze.py) pins these sets.
# --------------------------------------------------------------------------- #

# Every tier the entitlement policy knows (entitlements.json keys).
TIER_VOCABULARY: FrozenSet[str] = frozenset({
    "demo", "guest", "restricted", "self_hosted", "hosted_starter", "hosted_pro",
})

# The subset a VERIFIED IDENTITY can carry (Auth0 claim / stored org tier).
# `demo` and `guest` are server-resolved off-auth identities — never minted
# into a JWT claim and never stored as an org's billing tier.
CLAIM_TIERS: FrozenSet[str] = frozenset({
    "restricted", "self_hosted", "hosted_starter", "hosted_pro",
})

# --------------------------------------------------------------------------- #
# plan -> tier (MUST equal the Action's PLAN_TIER — parity-gated).
# Keys are leaf_website `app_metadata.leaf.plan` values (lib/stripe.ts
# getPlanFromPriceId today emits "monthly"/"yearly"; the rest are the agreed
# forward vocabulary for named products — see contract/BILLING.md §2).
# --------------------------------------------------------------------------- #
PLAN_TIER: Dict[str, str] = {
    "free": "hosted_starter",
    "starter": "hosted_starter",
    "basic": "hosted_starter",
    "trial": "hosted_starter",
    "pro": "hosted_pro",
    "monthly": "hosted_pro",
    "yearly": "hosted_pro",
    "single_monthly": "hosted_pro",
    "single_yearly": "hosted_pro",
    "team_monthly": "hosted_pro",
    "team_yearly": "hosted_pro",
    "team": "hosted_pro",
    "business": "hosted_pro",
    "enterprise": "self_hosted",
    "self_hosted": "self_hosted",
}

DEFAULT_TIER = "hosted_starter"
RESTRICTED_TIER = "restricted"

# subscription_status values that force tier=restricted regardless of plan.
# NOTE `past_due` is deliberately ABSENT: payment failure enters the grace
# window via leaf_website setting subscription_active=false (which trips the
# override below), not via a hard status match — contract/BILLING.md §4.
LAPSED_STATUSES: FrozenSet[str] = frozenset({
    "canceled", "unpaid", "incomplete_expired",
})


def derive_tier(
    plan: Optional[str],
    subscription_active: Optional[bool] = None,
    subscription_status: Optional[str] = None,
) -> str:
    """The tier a subscription state resolves to — SAME semantics as the Action.

    * ``plan`` is case-insensitive; unknown/absent plans resolve to
      ``DEFAULT_TIER`` (a paying-intent identity is never bounced to
      ``restricted`` because a new plan name shipped before this table).
    * The lapse override fires only on an EXPLICIT ``subscription_active is
      False`` or a status in ``LAPSED_STATUSES`` — absent fields (legacy
      metadata) leave the plan-derived tier intact, byte-for-byte the Action's
      backward-compat rule.
    * Always returns a member of ``CLAIM_TIERS``.
    """
    plan_key = (plan or "").strip().lower()
    tier = PLAN_TIER.get(plan_key, DEFAULT_TIER)
    status = (subscription_status or "").strip().lower()
    if subscription_active is False or status in LAPSED_STATUSES:
        tier = RESTRICTED_TIER
    return tier


# --------------------------------------------------------------------------- #
# drift gate support — parse the hand-pasted Action's constants out of the JS.
# --------------------------------------------------------------------------- #

_ACTION_JS = Path(__file__).resolve().parent / "auth0-actions" / "post-login-add-tenant-claim.js"


def load_action_billing_constants(js_path: Optional[Path] = None) -> Dict[str, object]:
    """The Action's PLAN_TIER / DEFAULT_TIER / LAPSED_STATUSES / CLAIM_NS,
    parsed from the JS source. Exists for the parity tests (and any future
    pre-paste check) — never called on a request path."""
    text = (js_path or _ACTION_JS).read_text(encoding="utf-8")

    m = re.search(r"const PLAN_TIER = \{(.*?)\};", text, re.S)
    if not m:
        raise ValueError("PLAN_TIER block not found in Action JS")
    plan_tier = dict(re.findall(r"([A-Za-z0-9_]+)\s*:\s*'([^']+)'", m.group(1)))

    m = re.search(r"const DEFAULT_TIER = '([^']+)';", text)
    if not m:
        raise ValueError("DEFAULT_TIER not found in Action JS")
    default_tier = m.group(1)

    m = re.search(r"const LAPSED_STATUSES = new Set\(\[(.*?)\]\);", text, re.S)
    if not m:
        raise ValueError("LAPSED_STATUSES not found in Action JS")
    lapsed = frozenset(re.findall(r"'([^']+)'", m.group(1)))

    m = re.search(r"const CLAIM_NS = '([^']+)';", text)
    if not m:
        raise ValueError("CLAIM_NS not found in Action JS")
    claim_ns = m.group(1)

    return {
        "PLAN_TIER": plan_tier,
        "DEFAULT_TIER": default_tier,
        "LAPSED_STATUSES": lapsed,
        "CLAIM_NS": claim_ns,
    }
