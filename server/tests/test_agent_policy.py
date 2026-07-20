"""
Binary acceptance for the agent policy catalog (server/agent_policy.py).

Contracts under test:
  * the SHIPPED server/agent_policy.json loads, carries the v1 catalog
    (9 actions, low 120 / medium 60 / high 10 per hour, approval_ttl_s 300),
    and is byte-equivalent to the hardcoded fail-safe defaults;
  * STRICT validation: unknown fields (top-level, action, override) are load
    errors; security booleans refuse truthiness coercion;
  * a MISSING file falls back to hardcoded defaults; a PRESENT-but-corrupt
    file raises (the gate fails closed on it — never a silent fallback);
  * tier_overrides may retune a tier EXCEPT loosening a tenant_tightenable:
    false action (register_tool is not skippable at ANY tier);
  * per-tenant overlays can only TIGHTEN (auto -> confirm-once ->
    always-confirm ordering; enabled can only go false).

Run:  cd server && python -m pytest tests/test_agent_policy.py -q
"""
from __future__ import annotations

# Cache the STDLIB `platform` module BEFORE PROJECT_ROOT lands on sys.path
# (repo-root platform/ package shadows it; mirrors test_wave4/5).
import platform as _stdlib_platform  # noqa: E402
_stdlib_platform.python_implementation()

import json  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import agent_policy  # noqa: E402
from agent_policy import PolicyError  # noqa: E402

SHIPPED = SERVER_DIR / "agent_policy.json"


def _shipped_raw() -> dict:
    return json.loads(SHIPPED.read_text(encoding="utf-8"))


def _write(tmp_path: Path, raw: dict) -> Path:
    p = tmp_path / "agent_policy.json"
    p.write_text(json.dumps(raw), encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# shipped catalog + fail-safe defaults
# --------------------------------------------------------------------------- #
def test_shipped_catalog_loads_with_v1_shape():
    pol = agent_policy.load_policy(SHIPPED)
    assert set(pol.actions) == {
        "request_confirmation", "read_platform_state", "run_read_tool",
        "run_write_tool", "submit_live_solve", "undo_drawing_version",
        "author_tool", "register_tool", "customize_platform",
    }
    assert pol.rate_limits == {"low": 120, "medium": 60, "high": 10}
    assert pol.approval_ttl_s == 300

    reg = pol.actions["register_tool"]
    assert reg.policy == "always-confirm"
    assert reg.tenant_tightenable is False
    assert reg.rung == 6
    assert reg.required_capability == "deploy"

    cust = pol.actions["customize_platform"]
    assert cust.enabled is False
    assert cust.rung == 7

    assert pol.actions["run_write_tool"].policy == "confirm-once"
    assert pol.actions["undo_drawing_version"].policy == "auto"  # safety valve
    assert pol.actions["read_platform_state"].required_capability == "converse"

    # the UI-consent tool (wire contract section 5) is rung 0 and auto —
    # converseLoop consults the gate for it, so it MUST exist in the catalog
    conf = pol.actions["request_confirmation"]
    assert conf.policy == "auto"
    assert conf.rung == 0
    assert conf.required_capability == "converse"
    assert conf.dispatch == {"kind": "app_api", "routes": []}


def test_hardcoded_defaults_mirror_shipped_json(tmp_path):
    """A deleted policy file must yield EXACTLY the shipped catalog — the
    fail-safe can never be weaker (or stronger) than the tracked file."""
    from_missing = agent_policy.load_policy(tmp_path / "nope.json")
    from_shipped = agent_policy.load_policy(SHIPPED)
    assert from_missing.actions == from_shipped.actions
    assert from_missing.rate_limits == from_shipped.rate_limits
    assert from_missing.approval_ttl_s == from_shipped.approval_ttl_s
    assert from_missing.tier_overrides == from_shipped.tier_overrides


def test_corrupt_file_raises_not_falls_back(tmp_path):
    p = tmp_path / "agent_policy.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(PolicyError):
        agent_policy.load_policy(p)


def test_env_override_wins(tmp_path, monkeypatch):
    raw = _shipped_raw()
    raw["approval_ttl_s"] = 60
    monkeypatch.setenv("LEAF_AGENT_POLICY_FILE", str(_write(tmp_path, raw)))
    assert agent_policy.load_policy().approval_ttl_s == 60


# --------------------------------------------------------------------------- #
# strict validation
# --------------------------------------------------------------------------- #
def test_unknown_action_field_is_error(tmp_path):
    raw = _shipped_raw()
    raw["actions"]["run_read_tool"]["enbaled"] = False  # typo of a security field
    with pytest.raises(PolicyError, match="unknown fields"):
        agent_policy.load_policy(_write(tmp_path, raw))


def test_unknown_top_level_field_is_error(tmp_path):
    raw = _shipped_raw()
    raw["rate_limtis"] = {}
    with pytest.raises(PolicyError, match="unknown top-level"):
        agent_policy.load_policy(_write(tmp_path, raw))


def test_security_bool_refuses_unparseable_values(tmp_path):
    for bad in ("ture", "", 2, ["true"]):
        raw = _shipped_raw()
        raw["actions"]["customize_platform"]["enabled"] = bad
        with pytest.raises(PolicyError, match="must be a boolean"):
            agent_policy.load_policy(_write(tmp_path, raw))


def test_security_bool_quoted_false_is_false_not_truthy(tmp_path):
    """bool("false") is True — the parser must NOT coerce by truthiness."""
    raw = _shipped_raw()
    raw["actions"]["customize_platform"]["enabled"] = "false"
    pol = agent_policy.load_policy(_write(tmp_path, raw))
    assert pol.actions["customize_platform"].enabled is False


def test_unknown_capability_is_error(tmp_path):
    raw = _shipped_raw()
    raw["actions"]["run_read_tool"]["required_capability"] = "run_reed"
    with pytest.raises(PolicyError, match="required_capability"):
        agent_policy.load_policy(_write(tmp_path, raw))


def test_invalid_policy_tier_is_error(tmp_path):
    raw = _shipped_raw()
    raw["actions"]["run_read_tool"]["policy"] = "sometimes"
    with pytest.raises(PolicyError, match="policy"):
        agent_policy.load_policy(_write(tmp_path, raw))


def test_bad_rate_limit_is_error(tmp_path):
    raw = _shipped_raw()
    raw["rate_limits"]["low_per_hour"] = "many"
    with pytest.raises(PolicyError, match="rate_limits"):
        agent_policy.load_policy(_write(tmp_path, raw))


# --------------------------------------------------------------------------- #
# tier_overrides
# --------------------------------------------------------------------------- #
def test_tier_override_applies_per_tier(tmp_path):
    """The agent_write_autopilot mechanism: a tier override may loosen a
    tenant_tightenable action (run_write_tool -> auto for hosted_pro)."""
    raw = _shipped_raw()
    raw["tier_overrides"] = {"hosted_pro": {"run_write_tool": {"policy": "auto"}}}
    pol = agent_policy.load_policy(_write(tmp_path, raw))
    assert agent_policy.effective_action(pol, "run_write_tool", tier="hosted_pro").policy == "auto"
    # other tiers keep the base policy
    assert agent_policy.effective_action(pol, "run_write_tool", tier="hosted_starter").policy == "confirm-once"
    assert agent_policy.effective_action(pol, "run_write_tool").policy == "confirm-once"


def test_tier_override_cannot_loosen_tightenable_false_action(tmp_path):
    raw = _shipped_raw()
    raw["tier_overrides"] = {"hosted_pro": {"register_tool": {"policy": "auto"}}}
    with pytest.raises(PolicyError, match="tenant_tightenable"):
        agent_policy.load_policy(_write(tmp_path, raw))


def test_tier_override_cannot_reenable_disabled_action(tmp_path):
    raw = _shipped_raw()
    raw["tier_overrides"] = {"demo": {"customize_platform": {"enabled": True}}}
    with pytest.raises(PolicyError, match="re-enable"):
        agent_policy.load_policy(_write(tmp_path, raw))


def test_tier_override_unknown_action_is_error(tmp_path):
    raw = _shipped_raw()
    raw["tier_overrides"] = {"demo": {"no_such_action": {"policy": "auto"}}}
    with pytest.raises(PolicyError, match="not a catalog action"):
        agent_policy.load_policy(_write(tmp_path, raw))


# --------------------------------------------------------------------------- #
# tenant overlay — tighten-only
# --------------------------------------------------------------------------- #
def test_tenant_overlay_tightens_policy():
    pol = agent_policy.load_policy(SHIPPED)
    eff = agent_policy.effective_action(
        pol, "run_read_tool", tenant_overlay={"run_read_tool": {"policy": "confirm-once"}})
    assert eff.policy == "confirm-once"
    eff2 = agent_policy.effective_action(
        pol, "run_write_tool", tenant_overlay={"run_write_tool": {"policy": "always-confirm"}})
    assert eff2.policy == "always-confirm"


def test_tenant_overlay_disables_action():
    pol = agent_policy.load_policy(SHIPPED)
    eff = agent_policy.effective_action(
        pol, "run_read_tool", tenant_overlay={"run_read_tool": {"enabled": False}})
    assert eff.enabled is False


def test_tenant_overlay_loosening_rejected():
    pol = agent_policy.load_policy(SHIPPED)
    with pytest.raises(PolicyError, match="TIGHTEN"):
        agent_policy.effective_action(
            pol, "run_write_tool", tenant_overlay={"run_write_tool": {"policy": "auto"}})


def test_tenant_overlay_cannot_reenable():
    pol = agent_policy.load_policy(SHIPPED)
    with pytest.raises(PolicyError, match="TIGHTEN"):
        agent_policy.effective_action(
            pol, "customize_platform", tenant_overlay={"customize_platform": {"enabled": True}})


def test_tenant_overlay_unknown_field_rejected():
    pol = agent_policy.load_policy(SHIPPED)
    with pytest.raises(PolicyError, match="unknown fields"):
        agent_policy.effective_action(
            pol, "run_read_tool", tenant_overlay={"run_read_tool": {"rate_limit_category": "high"}})


# --------------------------------------------------------------------------- #
# per-tenant state file
# --------------------------------------------------------------------------- #
def test_tenant_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_AGENT_TENANTS_FILE", str(tmp_path / "agent_tenants.json"))
    assert agent_policy.load_tenant_state("t1") == {"agent_disabled": False, "overlay": {}}
    agent_policy.set_tenant_agent_disabled("t1", True)
    assert agent_policy.load_tenant_state("t1")["agent_disabled"] is True
    agent_policy.set_tenant_agent_disabled("t1", False)
    assert agent_policy.load_tenant_state("t1")["agent_disabled"] is False
    # other tenants unaffected
    assert agent_policy.load_tenant_state("t2")["agent_disabled"] is False


# --------------------------------------------------------------------------- #
# stdlib-shadow regression (the app's real import order)
# --------------------------------------------------------------------------- #
def test_policy_loads_through_the_app_import_path():
    """The gate must load the catalog in the SERVER process, whose composition
    root (deps.py) puts PROJECT_ROOT on sys.path — where the repo's own
    platform/ package shadows stdlib `platform`. jsonschema reads
    platform.python_implementation() lazily while validating args_schema, so a
    lost stdlib module turned EVERY gate call into policy_load_failed while this
    suite stayed green (it caches stdlib platform at import, line 23).

    Runs in a subprocess so the caching above cannot mask the regression.
    """
    import subprocess
    src = (
        "import deps, agent_policy, sys;"
        "pol = agent_policy.load_policy();"
        "assert 'request_confirmation' in pol.actions;"
        "sys.stdout.write('OK')"
    )
    proc = subprocess.run(
        [sys.executable, "-c", src], cwd=str(SERVER_DIR),
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, f"policy load failed on the app import path:\n{proc.stderr}"
    assert proc.stdout.strip().endswith("OK")
