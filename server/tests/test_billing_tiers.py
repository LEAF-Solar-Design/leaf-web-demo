"""
Billing plan→tier mapping gate (contract/BILLING.md §2-§4).

Two duties:

  1. PARITY / DRIFT — server/billing_tiers.py is the canonical mapping; the
     Auth0 Post-Login Action carries a hand-pasted JS copy. These tests parse
     the JS and fail the moment the copies disagree, because an Action drift
     ships silently (dashboard re-paste is manual, docs/runbooks/
     auth0-post-login-paste.md).
  2. SEMANTICS — derive_tier() implements the Action's exact rules: plan
     lookup with DEFAULT_TIER fallback, explicit-lapse override to restricted,
     legacy-absent fields left intact, outputs always inside CLAIM_TIERS.

Run:  cd server && python -m pytest tests/test_billing_tiers.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import billing_tiers as bt  # noqa: E402


# --------------------------------------------------------------------------- #
# parity with the hand-pasted Auth0 Action
# --------------------------------------------------------------------------- #
def test_plan_tier_matches_action_js():
    action = bt.load_action_billing_constants()
    assert action["PLAN_TIER"] == bt.PLAN_TIER, (
        "PLAN_TIER drift between server/billing_tiers.py and the Auth0 Action JS — "
        "update both, then re-paste the Action (docs/runbooks/auth0-post-login-paste.md)"
    )


def test_default_tier_matches_action_js():
    action = bt.load_action_billing_constants()
    assert action["DEFAULT_TIER"] == bt.DEFAULT_TIER


def test_lapsed_statuses_match_action_js():
    action = bt.load_action_billing_constants()
    assert action["LAPSED_STATUSES"] == bt.LAPSED_STATUSES


def test_action_claim_ns_matches_server_default():
    import auth
    action = bt.load_action_billing_constants()
    assert action["CLAIM_NS"] == auth.DEFAULT_CLAIM_NS == "https://leafdesign.ai/"


def test_mapping_values_stay_inside_claim_tiers():
    assert set(bt.PLAN_TIER.values()) <= bt.CLAIM_TIERS
    assert bt.DEFAULT_TIER in bt.CLAIM_TIERS
    assert bt.RESTRICTED_TIER in bt.CLAIM_TIERS


# --------------------------------------------------------------------------- #
# derive_tier semantics
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("plan,expected", [
    ("free", "hosted_starter"),
    ("starter", "hosted_starter"),
    ("basic", "hosted_starter"),
    ("trial", "hosted_starter"),
    ("pro", "hosted_pro"),
    ("monthly", "hosted_pro"),
    ("yearly", "hosted_pro"),
    ("team", "hosted_pro"),
    ("business", "hosted_pro"),
    ("enterprise", "self_hosted"),
    ("self_hosted", "self_hosted"),
])
def test_plan_mapping(plan, expected):
    assert bt.derive_tier(plan) == expected


def test_unknown_or_absent_plan_defaults_not_restricted():
    assert bt.derive_tier("brand-new-plan") == bt.DEFAULT_TIER
    assert bt.derive_tier("") == bt.DEFAULT_TIER
    assert bt.derive_tier(None) == bt.DEFAULT_TIER


def test_plan_lookup_is_case_insensitive():
    assert bt.derive_tier("PRO") == "hosted_pro"
    assert bt.derive_tier("  Enterprise  ") == "self_hosted"


def test_explicit_inactive_overrides_plan():
    assert bt.derive_tier("pro", subscription_active=False) == "restricted"
    assert bt.derive_tier("enterprise", subscription_active=False) == "restricted"


@pytest.mark.parametrize("status", sorted(bt.LAPSED_STATUSES))
def test_lapsed_status_overrides_plan(status):
    assert bt.derive_tier("pro", subscription_active=True,
                          subscription_status=status) == "restricted"


def test_past_due_is_grace_not_lapse():
    # past_due alone does NOT force restricted at the mapping layer — the grace
    # window is driven by leaf_website flipping subscription_active=false
    # (contract/BILLING.md §4).
    assert bt.derive_tier("pro", subscription_status="past_due") == "hosted_pro"


def test_legacy_absent_fields_leave_plan_tier_intact():
    assert bt.derive_tier("pro") == "hosted_pro"
    assert bt.derive_tier("pro", None, None) == "hosted_pro"


def test_active_subscription_unaffected():
    assert bt.derive_tier("pro", subscription_active=True,
                          subscription_status="active") == "hosted_pro"


def test_outputs_always_claim_tiers():
    cases = [
        ("pro", True, "active"), ("weird", None, None), (None, False, None),
        ("enterprise", True, "canceled"), ("monthly", None, "past_due"),
    ]
    for plan, active, status in cases:
        assert bt.derive_tier(plan, active, status) in bt.CLAIM_TIERS
