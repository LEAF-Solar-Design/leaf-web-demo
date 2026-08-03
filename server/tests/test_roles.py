"""
Role-overlay gate suite (contract/AUTH.md §11.5, server/roles.py).

Covers the four laws the role system must never break:

  1. ADDITIVE-ONLY — a role can turn a capability on over the tier baseline,
     never off; a True from billing survives anything a role entry says.
  2. FAIL-CLOSED (additive polarity) — unreadable file/entry/key/value grants
     NOTHING and never raises onto a request path.
  3. TWO-FACTOR elevated grants — `elevated_grants` apply only when the
     subject passed the LEAF_PLATFORM_ADMIN_SUBJECTS allowlist AND the
     verified claim carries the role.
  4. VOCABULARY — role names are shape-validated, deduped, sorted, capped;
     unknown names are inert.

Run:  cd server && python -m pytest tests/test_roles.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import entitlements  # noqa: E402
import roles  # noqa: E402


# --------------------------- name normalization ---------------------------- #

def test_normalize_accepts_valid_names_case_folded_deduped_sorted():
    assert roles.normalize_role_names(
        ["Platform_Admin", " org_admin ", "org_admin", "b-1"]
    ) == ("b-1", "org_admin", "platform_admin")


def test_normalize_drops_invalid_members_and_non_lists():
    assert roles.normalize_role_names("platform_admin") == ()   # bare string: not a list
    assert roles.normalize_role_names(None) == ()
    assert roles.normalize_role_names({"platform_admin": True}) == ()
    assert roles.normalize_role_names(
        ["bad name!", "-leading-dash", "", 42, None, {"x": 1}, "ok_role"]
    ) == ("ok_role",)


def test_normalize_caps_at_max_roles():
    many = [f"role-{i:03d}" for i in range(40)]
    out = roles.normalize_role_names(many)
    assert len(out) == roles.MAX_ROLES
    assert out == tuple(sorted(many))[:roles.MAX_ROLES]


# ------------------------------ policy loading ----------------------------- #

def test_absent_file_uses_hardcoded_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_ROLES_FILE", str(tmp_path / "nope.json"))
    policy = roles.load_role_policy()
    assert roles.PLATFORM_ADMIN_ROLE in policy
    assert policy[roles.PLATFORM_ADMIN_ROLE]["elevated_grants"] == {"platform_customize": True}


def test_unreadable_file_grants_nothing(monkeypatch, tmp_path):
    bad = tmp_path / "roles.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("LEAF_ROLES_FILE", str(bad))
    assert roles.load_role_policy() == {}
    # And the overlay degrades to the tier baseline, never an exception.
    base = entitlements.entitlements_for("restricted")
    assert entitlements.entitlements_for("restricted", ("platform_admin",), True) == base


def test_non_dict_top_level_and_malformed_entries_dropped(monkeypatch, tmp_path):
    f = tmp_path / "roles.json"
    f.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    monkeypatch.setenv("LEAF_ROLES_FILE", str(f))
    assert roles.load_role_policy() == {}
    f.write_text(json.dumps({
        "_note": "ignored",
        "Bad Name": {"grants": {"build": True}},
        "listy": ["grants"],
        "ok": {"grants": {"build": True}},
    }), encoding="utf-8")
    policy = roles.load_role_policy()
    assert set(policy) == {"ok"}
    assert policy["ok"]["grants"] == {"build": True}


def test_unknown_capability_keys_and_unparseable_values_grant_nothing(monkeypatch, tmp_path):
    f = tmp_path / "roles.json"
    f.write_text(json.dumps({
        "r1": {"grants": {"become_root": True, "build": "yes", "solve": None,
                          "run_write": "false", "converse": True}},
    }), encoding="utf-8")
    monkeypatch.setenv("LEAF_ROLES_FILE", str(f))
    g = roles.role_grants(("r1",))
    # become_root: unknown key ignored; solve: null unparseable -> nothing;
    # run_write: explicit false -> not a grant; build "yes"/converse true -> granted.
    assert g == {"build": True, "converse": True}


# ------------------------------ overlay laws ------------------------------- #

def test_additive_only_roles_never_downgrade():
    base = {"run_read": True, "run_write": True}
    out = roles.apply_role_grants(base, ("org_admin",))       # stub grants nothing
    assert out == base
    # Even a hostile grants block carrying False cannot flip a base True:
    # explicit False is dropped at load time, so only True survives merging.
    assert roles.apply_role_grants(base, ("platform_admin",))["run_read"] is True


def test_platform_admin_grants_everything_but_customize_without_elevation():
    caps = entitlements.entitlements_for("restricted", ("platform_admin",), False)
    for cap in entitlements.CAPABILITIES:
        if cap == "platform_customize":
            assert caps[cap] is False, "platform_customize requires the second factor"
        else:
            assert caps[cap] is True, f"platform_admin should grant {cap}"


def test_platform_admin_elevated_is_everything_on():
    caps = entitlements.entitlements_for("restricted", ("platform_admin",), True)
    assert all(caps[cap] is True for cap in entitlements.CAPABILITIES)


def test_elevation_without_the_role_grants_nothing():
    base = entitlements.entitlements_for("restricted")
    assert entitlements.entitlements_for("restricted", (), True) == base
    assert entitlements.entitlements_for("restricted", ("unknown_role",), True) == base


def test_reserved_org_stubs_grant_nothing():
    base = entitlements.entitlements_for("hosted_starter")
    for stub in ("org_admin", "org_member"):
        assert entitlements.entitlements_for("hosted_starter", (stub,), True) == base


def test_tier_only_resolution_byte_identical_to_historical():
    """The defaulted signature is the pre-roles behavior exactly — every
    caller that has no verified role context is untouched by this feature."""
    for tier in ("demo", "guest", "restricted", "self_hosted",
                 "hosted_starter", "hosted_pro", "admin", "unknown-tier"):
        assert (entitlements.entitlements_for(tier)
                == entitlements.entitlements_for(tier, (), False))


# ------------------------- identity resolution ----------------------------- #

def test_resolve_roles_plain_str_and_missing_attrs():
    assert entitlements.resolve_roles("demo-tenant") == ((), False)


def test_resolve_roles_reads_and_renormalizes_attrs():
    class Ctx(str):
        pass
    t = Ctx("t1")
    t.roles = ["Platform_Admin", "bad name!"]
    t.elevated = True
    assert entitlements.resolve_roles(t) == (("platform_admin",), True)
    t.elevated = "yes"           # non-True truthy must NOT elevate
    assert entitlements.resolve_roles(t) == (("platform_admin",), False)


def test_tenant_context_carries_and_defaults_roles():
    import deps
    plain = deps.TenantContext("t1", tier="hosted_pro")
    assert plain.roles == () and plain.elevated is False
    rich = deps.TenantContext("t1", tier="hosted_pro",
                              roles=("platform_admin",), elevated=True)
    assert rich.roles == ("platform_admin",) and rich.elevated is True
    # resolve_active_tenant_context must not lose subject-level identity: the
    # str value is unchanged and attribute carry-over is covered there.
    assert str(rich) == "t1"


# ------------------------------ view shape --------------------------------- #

def test_entitlements_view_reports_effective_caps_and_roles():
    view = entitlements.entitlements_view("restricted", ("platform_admin",), False)
    assert view["tier"] == "restricted"
    assert view["roles"] == ["platform_admin"]
    assert view["entitlements"]["run_write"] is True
    assert view["entitlements"]["platform_customize"] is False
    assert view["source"] == "policy"
    legacy = entitlements.entitlements_view("restricted")
    assert legacy["roles"] == []


# ------------------------- shipped-file discipline ------------------------- #

def test_roles_json_mirror_parity(monkeypatch, tmp_path):
    """The shipped roles.json and the hardcoded defaults must resolve to the
    SAME cleaned policy — enforcement identical with or without the file (the
    entitlements.json mirror discipline, role edition)."""
    monkeypatch.setenv("LEAF_ROLES_FILE", str(SERVER_DIR / "roles.json"))
    from_file = roles.load_role_policy()
    monkeypatch.setenv("LEAF_ROLES_FILE", str(tmp_path / "absent.json"))
    from_defaults = roles.load_role_policy()
    assert from_file == from_defaults


def test_shipped_platform_customize_is_elevated_only():
    raw = json.loads((SERVER_DIR / "roles.json").read_text(encoding="utf-8"))
    for name, entry in raw.items():
        if name.startswith("_"):
            continue
        grants = entry.get("grants") or {}
        assert grants.get("platform_customize") is not True, (
            f"role {name} must never carry platform_customize in plain grants")
    assert raw["platform_admin"]["elevated_grants"] == {"platform_customize": True}
