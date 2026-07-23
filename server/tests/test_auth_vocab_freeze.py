"""
Claim-namespace + tier-vocabulary freeze gate (contract/AUTH.md §11).

Census item 5 required this freeze BEFORE enterprise onboarding: every layer
that speaks the identity vocabulary — the RS256 verifier, the hand-pasted
Auth0 Actions, the entitlement policy (JSON + hardcoded mirror), the billing
mapping, and the platform lane's fail-closed literal — must agree on:

  * the claim namespace  `https://leafdesign.ai/`
  * the 6-tier vocabulary  {demo, guest, restricted, self_hosted,
    hosted_starter, hosted_pro}  (and the 4-tier claim-mintable subset)
  * the 9-capability vocabulary

The frozen sets are stated LITERALLY here (the test IS the freeze). Growing
any of them is an operator-promotion ritual: amend contract/AUTH.md §11, then
this file, in the same PR.

Run:  cd server && python -m pytest tests/test_auth_vocab_freeze.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
REPO_ROOT = SERVER_DIR.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import auth  # noqa: E402
import billing_tiers as bt  # noqa: E402
import entitlements as ents  # noqa: E402

# ------------------------- THE FROZEN SETS (§11) --------------------------- #
FROZEN_NS = "https://leafdesign.ai/"
FROZEN_TIERS = {
    "demo", "guest", "restricted", "self_hosted", "hosted_starter", "hosted_pro",
}
FROZEN_CLAIM_TIERS = {"restricted", "self_hosted", "hosted_starter", "hosted_pro"}
FROZEN_CAPABILITIES = {
    "run_read", "run_write", "solve", "build",
    "converse", "agent_write_autopilot", "deploy", "platform_customize",
    "upload",
}
# --------------------------------------------------------------------------- #


def test_claim_namespace_frozen_everywhere():
    assert auth.DEFAULT_CLAIM_NS == FROZEN_NS
    action = bt.load_action_billing_constants()
    assert action["CLAIM_NS"] == FROZEN_NS
    # credentials-exchange (M2M) Action mints under the SAME namespace.
    m2m = (SERVER_DIR / "auth0-actions" /
           "credentials-exchange-add-tenant-claim.js").read_text(encoding="utf-8")
    assert FROZEN_NS in m2m, "M2M Action must mint under the frozen namespace"


def test_entitlements_json_keys_are_the_frozen_tier_vocabulary():
    policy = json.loads((SERVER_DIR / "entitlements.json").read_text(encoding="utf-8"))
    tiers = {k for k in policy if not k.startswith("_")}
    assert tiers == FROZEN_TIERS


def test_entitlements_json_capabilities_are_frozen_per_tier():
    policy = json.loads((SERVER_DIR / "entitlements.json").read_text(encoding="utf-8"))
    for tier, caps in policy.items():
        if tier.startswith("_"):
            continue
        assert set(caps) == FROZEN_CAPABILITIES, f"tier {tier} capability drift"


def test_hardcoded_mirror_matches_json_policy_exactly():
    policy = json.loads((SERVER_DIR / "entitlements.json").read_text(encoding="utf-8"))
    policy = {k: v for k, v in policy.items() if not k.startswith("_")}
    assert ents._HARDCODED_DEFAULTS == policy, (
        "server/entitlements.py _HARDCODED_DEFAULTS must mirror entitlements.json "
        "byte-for-byte — enforcement must be identical with or without the file"
    )


def test_entitlements_module_constants_frozen():
    assert set(ents.CAPABILITIES) == FROZEN_CAPABILITIES
    assert ents.RESTRICTED_TIER == "restricted"
    assert ents.DEFAULT_TIER == "demo"
    assert {ents.RESTRICTED_TIER, ents.DEFAULT_TIER} <= FROZEN_TIERS


def test_billing_tiers_vocabulary_frozen():
    assert bt.TIER_VOCABULARY == frozenset(FROZEN_TIERS)
    assert bt.CLAIM_TIERS == frozenset(FROZEN_CLAIM_TIERS)
    assert FROZEN_CLAIM_TIERS < FROZEN_TIERS


def test_action_plan_tier_values_stay_claim_mintable():
    action = bt.load_action_billing_constants()
    minted = set(action["PLAN_TIER"].values()) | {action["DEFAULT_TIER"], "restricted"}
    assert minted <= FROZEN_CLAIM_TIERS, (
        "the Post-Login Action must never mint a tier outside the claim subset "
        "(demo/guest are server-resolved identities, not claims)"
    )


def test_platform_lane_fallback_literal_is_restricted():
    # The platform lane's policy-seam-down literal (platform/entitlements.py
    # _fallback_tier) must stay the frozen fail-closed tier. Text pin — the
    # platform package cannot be imported here without re-shadowing stdlib
    # `platform` (see platform/tests/conftest.py).
    text = (REPO_ROOT / "platform" / "entitlements.py").read_text(encoding="utf-8")
    assert 'return "restricted"' in text
