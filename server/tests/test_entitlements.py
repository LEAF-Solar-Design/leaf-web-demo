"""
Binary acceptance for tier entitlements (server/entitlements.py).

Contracts under test:
  * the SHIPPED server/entitlements.json loads and matches the hardcoded
    fail-safe defaults for every tier (wire contract §9, 7 capabilities);
  * a capability value that is not a REAL boolean never grants: `bool("false")`
    is True, so truthiness coercion would hand a restricted tenant `converse`
    and the register-tool approval path;
  * absence and corruption are distinguished — an ABSENT tier falls back to
    `restricted`, but a tier written as `{}` means "nothing", not "restricted";
  * `resolve_tier` fails CLOSED on any claim it cannot read (empty, null,
    wrong type), and only a plain-str (auth-off) tenant reaches "demo";
  * there is ONE security-flag parser: agent_policy.security_bool delegates to
    entitlements.security_flag and only re-currencies the exception.

Run:  cd server && python -m pytest tests/test_entitlements.py -q
"""
from __future__ import annotations

# Cache the STDLIB `platform` module BEFORE PROJECT_ROOT lands on sys.path
# (repo-root platform/ package shadows it; mirrors test_agent_policy).
import platform as _stdlib_platform  # noqa: E402
_stdlib_platform.python_implementation()

import json  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import entitlements  # noqa: E402
from entitlements import CAPABILITIES, MISSING, SecurityFlagError, security_flag  # noqa: E402

SHIPPED = SERVER_DIR / "entitlements.json"


def _use(monkeypatch, tmp_path, raw: dict) -> Path:
    p = tmp_path / "entitlements.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setenv("LEAF_ENTITLEMENTS_FILE", str(p))
    return p


# --------------------------------------------------------------------------- #
# the shipped policy still works — over-tightening guard
# --------------------------------------------------------------------------- #
def test_shipped_policy_matches_hardcoded_defaults(monkeypatch):
    """Every tier in the TRACKED file must resolve exactly as the fail-safe
    defaults do. This is the outage guard for the strict per-key parse: a real
    operator record must not start failing validation."""
    monkeypatch.setenv("LEAF_ENTITLEMENTS_FILE", str(SHIPPED))
    shipped = json.loads(SHIPPED.read_text(encoding="utf-8"))
    tiers = [k for k in shipped if not k.startswith("_")]
    assert set(tiers) == set(entitlements._HARDCODED_DEFAULTS)
    for tier in tiers:
        assert entitlements.entitlements_for(tier) == entitlements._HARDCODED_DEFAULTS[tier], tier
        assert set(entitlements.entitlements_for(tier)) == set(CAPABILITIES)


def test_demo_stays_full_access_and_restricted_stays_read_only(monkeypatch):
    monkeypatch.setenv("LEAF_ENTITLEMENTS_FILE", str(SHIPPED))
    demo = entitlements.entitlements_for("demo")
    assert demo["run_read"] and demo["run_write"] and demo["build"] and demo["converse"]
    assert demo["platform_customize"] is False
    restricted = entitlements.entitlements_for("restricted")
    assert restricted["run_read"] is True
    assert not any(restricted[c] for c in CAPABILITIES if c != "run_read")


# --------------------------------------------------------------------------- #
# BLOCKER: truthiness coercion of a per-key capability value
# --------------------------------------------------------------------------- #
def test_quoted_false_capability_does_not_grant(monkeypatch, tmp_path):
    """`bool("false")` is True — the reviewer's reproduction: a restricted tier
    whose `converse` was written as the STRING "false" could converse and reach
    the register-tool approval path."""
    _use(monkeypatch, tmp_path, {"restricted": {
        "run_read": True, "converse": "false", "deploy": "false",
        "run_write": "no", "build": "off", "agent_write_autopilot": "0",
        "platform_customize": False,
    }})
    ent = entitlements.entitlements_for("restricted")
    assert ent["converse"] is False
    assert ent["deploy"] is False
    assert not any(ent[c] for c in CAPABILITIES if c != "run_read")
    assert ent["run_read"] is True


def test_unparseable_capability_value_denies_that_capability(monkeypatch, tmp_path):
    """A present-but-malformed value is neither true nor false to the operator —
    it must resolve to DENY, never to the permissive reading, and it must not
    take down the rest of the tier."""
    for bad in (None, "ture", "", 2, ["true"], {"true": True}):
        _use(monkeypatch, tmp_path,
             {"weird": {"run_read": True, "converse": bad, "run_write": True}})
        ent = entitlements.entitlements_for("weird")
        assert ent["converse"] is False, bad
        assert ent["run_read"] is True and ent["run_write"] is True, bad


def test_quoted_true_still_grants(monkeypatch, tmp_path):
    """Strictness is about REFUSING to guess, not about rejecting the spellings
    an operator may reasonably write — a hand-edited "true" keeps working."""
    _use(monkeypatch, tmp_path, {"t": {"run_read": "true", "converse": "yes", "build": 1}})
    ent = entitlements.entitlements_for("t")
    assert ent["run_read"] is True and ent["converse"] is True and ent["build"] is True


# --------------------------------------------------------------------------- #
# absence vs corruption vs "deliberately nothing"
# --------------------------------------------------------------------------- #
def test_empty_tier_entry_means_nothing_not_restricted(monkeypatch, tmp_path):
    """An `or` chain fell THROUGH a falsy `{}` entry to the restricted entry,
    which grants run_read — a tier the operator zeroed out came back with a
    capability. Membership keeps "{}" meaning what it says."""
    _use(monkeypatch, tmp_path, {"locked": {}, "restricted": {"run_read": True}})
    assert entitlements.entitlements_for("locked") == {c: False for c in CAPABILITIES}


def test_unknown_tier_falls_to_restricted_not_demo(monkeypatch):
    monkeypatch.setenv("LEAF_ENTITLEMENTS_FILE", str(SHIPPED))
    ent = entitlements.entitlements_for("totally-unknown-tier-xyz")
    assert ent["run_read"] is True
    assert not any(ent[c] for c in CAPABILITIES if c != "run_read")


def test_per_key_omission_defaults_false(monkeypatch, tmp_path):
    _use(monkeypatch, tmp_path, {"partial": {"run_read": True}})
    ent = entitlements.entitlements_for("partial")
    assert ent == dict({c: False for c in CAPABILITIES}, run_read=True)


def test_non_mapping_tier_entry_reads_as_absent(monkeypatch, tmp_path):
    """A corrupt tier entry must not be coerced: it drops out of the policy and
    the tier falls to restricted, never to the entry it was supposed to be."""
    _use(monkeypatch, tmp_path,
         {"demo": "full-access", "restricted": {"run_read": True}})
    ent = entitlements.entitlements_for("demo")
    assert ent["run_write"] is False and ent["build"] is False and ent["converse"] is False


def test_missing_file_falls_back_to_hardcoded_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_ENTITLEMENTS_FILE", str(tmp_path / "nope.json"))
    assert entitlements.entitlements_for("demo") == entitlements._HARDCODED_DEFAULTS["demo"]


# --------------------------------------------------------------------------- #
# resolve_tier — fail closed on anything unreadable
# --------------------------------------------------------------------------- #
class _Ctx:
    def __init__(self, tier):
        self.tier = tier


def test_offauth_plain_string_tenant_is_demo():
    assert entitlements.resolve_tier("demo-tenant") == entitlements.DEFAULT_TIER


@pytest.mark.parametrize("claim", ["", None, 0, 1, True, 123, ["hosted_pro"], {"tier": "demo"}])
def test_unreadable_tier_claim_is_restricted(claim):
    """An authenticated tenant whose claim is empty or of the wrong TYPE must
    never be handed a fabricated tier name (`str(True)` -> "True") — it resolves
    restricted."""
    assert entitlements.resolve_tier(_Ctx(claim)) == entitlements.RESTRICTED_TIER


def test_real_tier_claim_is_used_verbatim():
    assert entitlements.resolve_tier(_Ctx("hosted_starter")) == "hosted_starter"
    # ...and a padded one is NOT normalized into a match (that would loosen)
    assert entitlements.resolve_tier(_Ctx("  demo  ")) != "demo"


def test_padded_tier_does_not_inherit_demo(monkeypatch):
    monkeypatch.setenv("LEAF_ENTITLEMENTS_FILE", str(SHIPPED))
    ent = entitlements.entitlements_for(entitlements.resolve_tier(_Ctx(" demo ")))
    assert ent["run_write"] is False and ent["build"] is False


# --------------------------------------------------------------------------- #
# the shared primitive
# --------------------------------------------------------------------------- #
def test_security_flag_semantics():
    assert security_flag(MISSING, field="f", default=True) is True
    assert security_flag(MISSING, field="f") is False
    assert security_flag(True, field="f") is True
    assert security_flag(0, field="f") is False
    assert security_flag("FALSE", field="f") is False
    assert security_flag(" on ", field="f") is True
    with pytest.raises(SecurityFlagError, match="null"):
        security_flag(None, field="f", default=True)
    for bad in ("ture", "", 2, ["true"]):
        with pytest.raises(SecurityFlagError, match="must be a boolean"):
            security_flag(bad, field="f")


def test_agent_policy_delegates_to_the_one_parser():
    """Two copies of "parse a security flag" can drift; agent_policy keeps only
    its PolicyError currency and the shared MISSING sentinel."""
    import agent_policy

    assert agent_policy.MISSING is MISSING
    assert agent_policy.security_bool("false", field="f") is False
    with pytest.raises(agent_policy.PolicyError, match="must be a boolean"):
        agent_policy.security_bool("ture", field="f")
    with pytest.raises(agent_policy.PolicyError, match="null"):
        agent_policy.security_bool(None, field="f")


def test_present_unreadable_policy_never_restores_permissive_defaults(tmp_path, monkeypatch):
    """ABSENT means the operator never wrote a policy, so the shipped defaults
    are the intended answer. PRESENT-but-unreadable is a different fact: falling
    back to defaults there silently re-grants every capability a TIGHTENED
    policy was withholding, precisely when the file is damaged."""
    p = tmp_path / "entitlements.json"
    monkeypatch.setenv("LEAF_ENTITLEMENTS_FILE", str(p))
    # absent -> defaults, unchanged
    assert entitlements.load_policy() == dict(entitlements._HARDCODED_DEFAULTS)

    p.write_text("{truncated", encoding="utf-8")
    with pytest.raises(entitlements.EntitlementsError, match="invalid JSON"):
        entitlements.load_policy()

    p.write_text('["not", "a", "mapping"]', encoding="utf-8")
    with pytest.raises(entitlements.EntitlementsError, match="must be a mapping"):
        entitlements.load_policy()


def test_malformed_tool_capabilities_is_not_read_only(monkeypatch):
    """`or []` treated a PRESENT malformed capabilities value as an absent
    declaration, classifying a write tool as read-only so it passed a tier
    lacking run_write. We cannot tell what the tool does -> take the
    MORE-restrictive capability. A genuinely absent declaration is read-only."""
    for bogus in (None, "", 0, False, {}, 7):
        assert entitlements.tool_required_capability({"capabilities": bogus}) == "run_write", bogus
    # absent stays read-only, and the real shapes still classify correctly
    assert entitlements.tool_required_capability({}) == "run_read"
    assert entitlements.tool_required_capability({"capabilities": []}) == "run_read"
    assert entitlements.tool_required_capability({"capabilities": ["drawing.read"]}) == "run_read"
    assert entitlements.tool_required_capability({"capabilities": ["drawing.write"]}) == "run_write"


def test_entitlements_view_shape(monkeypatch):
    monkeypatch.setenv("LEAF_ENTITLEMENTS_FILE", str(SHIPPED))
    view = entitlements.entitlements_view("hosted_starter")
    assert view["tier"] == "hosted_starter" and view["source"] == "policy"
    assert set(view["entitlements"]) == set(CAPABILITIES)
    assert view["entitlements"]["build"] is False
