"""
Binary acceptance for the `converse` entitlement capability (S5).

Adds a fourth capability flag (`converse`) alongside {run_read, run_write, build} so
the sessions/turn-engine surface (S1-S4) can gate on tier the same way /api/run and
/api/author already do. Purely additive: CAPABILITIES gained "converse",
_HARDCODED_DEFAULTS and entitlements.json each gained a `converse` bool per tier.
Enforcement wiring (routers/tools.py callers) is out of scope for this lane — this
test asserts the policy surface (`entitlements_for`) is correct per tier.

Run:  cd server && python -m pytest tests/test_entitlements_converse.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import entitlements  # noqa: E402

EXPECTED_CONVERSE = {
    "demo": True,
    "self_hosted": True,
    "hosted_starter": True,
    "hosted_pro": True,
    "restricted": False,
}


def test_converse_in_capabilities_tuple():
    assert "converse" in entitlements.CAPABILITIES


def test_converse_per_tier_via_entitlements_for():
    for tier, expected in EXPECTED_CONVERSE.items():
        ent = entitlements.entitlements_for(tier)
        assert "converse" in ent, f"tier {tier!r} missing converse key"
        assert ent["converse"] is expected, f"tier {tier!r} converse={ent['converse']!r}, want {expected!r}"


def test_converse_restricted_is_false():
    assert entitlements.entitlements_for("restricted")["converse"] is False


def test_converse_unknown_tier_fails_closed():
    """An unrecognised tier falls back to the 'restricted' entry (F9 fail-closed) ->
    converse must be False, never True."""
    ent = entitlements.entitlements_for("no-such-tier")
    assert ent["converse"] is False


def test_hardcoded_defaults_mirror_json_file():
    """_HARDCODED_DEFAULTS and entitlements.json must carry identical converse values
    per tier (fail-safe parity), per the module's own documented invariant."""
    policy = entitlements.load_policy()
    for tier, expected in EXPECTED_CONVERSE.items():
        assert entitlements._HARDCODED_DEFAULTS[tier]["converse"] is expected
        assert policy[tier]["converse"] is expected


def test_entitlements_view_carries_converse():
    view = entitlements.entitlements_view("hosted_pro")
    assert view["entitlements"]["converse"] is True


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
