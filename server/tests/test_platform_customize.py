"""W14 admin self-edit lane (R7) — branch-only writes, co-sign, landing.

Covers the four invariants the lane exists to hold:

  1. BRANCH-ONLY: every ref write and push refspec passes one chokepoint that
     refuses anything outside ``refs/heads/admin-customize/<uuid>``; a propose
     never moves ``main``.
  2. ADMIN-ONLY: the route gate needs live auth + the `platform_customize`
     capability (shipped only on tier `admin`) + the R7 internal allowlist
     (mode `all` deliberately reads as off).
  3. CO-SIGN: fundamental paths (or ANY path when the manifest is absent)
     require the out-of-band approval secret, bound to the exact commit, with
     self-approval refused; a corrupt manifest refuses service.
  4. LANDING IS A HANDOFF: land() marks the record and (only when enabled)
     pushes the lane ref; the receipt names the standing
     branch->PR->review->merge->canary->prod pipeline and its rollback.

Run:  cd server && python -m pytest tests/test_platform_customize.py -q
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import customization_flags  # noqa: E402
import deps  # noqa: E402
import entitlements  # noqa: E402
import platform_customize as lane  # noqa: E402

TENANT = "org_admin_demo"
OTHER_TENANT = "org_other"


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return result.stdout.strip()


@pytest.fixture()
def platform_repo(tmp_path):
    """A real platform-repo stand-in with one commit on main."""
    repo = tmp_path / "platform-repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    (repo / "server").mkdir()
    (repo / "server" / "auth.py").write_text("AUTH = 1\n", encoding="utf-8")
    (repo / "docs").mkdir()
    (repo / "docs" / "note.md").write_text("note\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


@pytest.fixture()
def lane_env(tmp_path, platform_repo, monkeypatch):
    manifest = tmp_path / "fundamental.json"
    manifest.write_text(json.dumps({
        "version": 1,
        "areas": {"auth": ["server/auth.py", "server/auth0-actions/**"]},
    }), encoding="utf-8")
    monkeypatch.setenv("LEAF_PLATFORM_REPO_DIR", str(platform_repo))
    monkeypatch.setenv("LEAF_PLATFORM_CUSTOMIZE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("LEAF_PLATFORM_FUNDAMENTAL_PATHS_FILE", str(manifest))
    monkeypatch.setenv("LEAF_CUSTOMIZATION_APPROVAL_SECRET", "approval-secret")
    monkeypatch.delenv("LEAF_PLATFORM_REPO_PUSH", raising=False)
    monkeypatch.delenv("LEAF_PLATFORM_REPO_BASE_REF", raising=False)
    return {"repo": platform_repo, "manifest": manifest, "tmp": tmp_path}


def _propose(title="tweak docs", edits=None):
    return lane.propose(
        tenant_id=TENANT, subject="auth0|author-1", title=title,
        edits=edits if edits is not None else [
            {"path": "docs/note.md", "content": "edited\n"}],
    )


# --------------------------------------------------------------------------- #
# 1. branch-only chokepoint
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("ref", [
    "refs/heads/main",
    "refs/heads/master",
    "refs/heads/release/1.0",
    "refs/tags/v1",
    "refs/heads/admin-customize/../main",
    "refs/heads/admin-customize/not-a-uuid",
    "admin-customize/00000000-0000-0000-0000-000000000000",  # short form refused
    "",
])
def test_protected_refs_refused(ref):
    with pytest.raises(lane.PlatformCustomizeError) as exc:
        lane._assert_branch_only(ref)
    assert exc.value.code == "protected_ref_refused"


def test_lane_ref_accepted():
    ref = "refs/heads/admin-customize/12345678-1234-1234-1234-1234567890ab"
    assert lane._assert_branch_only(ref) == ref


def test_propose_never_moves_main(lane_env):
    repo = lane_env["repo"]
    main_before = _git(repo, "rev-parse", "refs/heads/main")
    view = _propose()
    assert _git(repo, "rev-parse", "refs/heads/main") == main_before
    branch_sha = _git(repo, "rev-parse", view["branch"])
    assert branch_sha == view["commit_sha"]
    assert view["branch"].startswith("admin-customize/")
    assert view["base_sha"] == main_before
    # HEAD of the repo is untouched too (still main, still the old tip)
    assert _git(repo, "rev-parse", "HEAD") == main_before


def test_propose_rejects_traversal_and_git_paths(lane_env):
    for bad in ["../escape.txt", "a//b.txt", ".git/hooks/pre-commit",
                "sub/.git/config", "/abs.txt", "C:/win.txt", "a\\b.txt"]:
        with pytest.raises(lane.PlatformCustomizeError) as exc:
            _propose(edits=[{"path": bad, "content": "x"}])
        assert exc.value.code == "edit_path_invalid", bad


def test_propose_noop_diff_refused(lane_env):
    with pytest.raises(lane.PlatformCustomizeError) as exc:
        _propose(edits=[{"path": "docs/note.md", "content": "note\n"}])
    assert exc.value.code == "edits_noop"


# --------------------------------------------------------------------------- #
# 2. fundamental classification + co-sign
# --------------------------------------------------------------------------- #
def test_non_fundamental_proposal_is_approved_immediately(lane_env):
    view = _propose()
    assert view["state"] == "approved"
    assert view["fundamental_paths"] == []


def test_fundamental_path_requires_cosign(lane_env):
    view = _propose(edits=[{"path": "server/auth.py", "content": "AUTH = 2\n"}])
    assert view["state"] == "awaiting_cosign"
    assert view["fundamental_paths"] == ["server/auth.py"]
    with pytest.raises(lane.PlatformCustomizeError) as exc:
        lane.land(change_id=view["change_id"], tenant_id=TENANT)
    assert exc.value.code == "cosign_required"


def test_absent_manifest_makes_everything_fundamental(lane_env, monkeypatch):
    monkeypatch.setenv("LEAF_PLATFORM_FUNDAMENTAL_PATHS_FILE",
                       str(lane_env["tmp"] / "nope.json"))
    view = _propose()
    assert view["state"] == "awaiting_cosign"
    assert view["fundamental_paths"] == ["docs/note.md"]


def test_corrupt_manifest_refuses_service(lane_env):
    lane_env["manifest"].write_text("{not json", encoding="utf-8")
    with pytest.raises(lane.PlatformCustomizeError) as exc:
        _propose()
    assert exc.value.code == "fundamental_manifest_unavailable"
    assert exc.value.status_code == 503


def test_cosign_binds_to_exact_commit_and_refuses_self_approval(lane_env):
    view = _propose(edits=[{"path": "server/auth.py", "content": "AUTH = 2\n"}])
    cid = view["change_id"]
    with pytest.raises(lane.PlatformCustomizeError) as exc:
        lane.cosign(change_id=cid, approver_subject="auth0|reviewer",
                    commit_sha="0" * 40, approve=True)
    assert exc.value.code == "cosign_commit_mismatch"
    with pytest.raises(lane.PlatformCustomizeError) as exc:
        lane.cosign(change_id=cid, approver_subject="auth0|author-1",
                    commit_sha=view["commit_sha"], approve=True)
    assert exc.value.code == "cosign_self_approval"
    approved = lane.cosign(change_id=cid, approver_subject="auth0|reviewer",
                           commit_sha=view["commit_sha"], approve=True)
    assert approved["state"] == "approved"
    assert approved["cosign"]["approver_subject"] == "auth0|reviewer"
    # a second co-sign on a settled record is refused
    with pytest.raises(lane.PlatformCustomizeError) as exc:
        lane.cosign(change_id=cid, approver_subject="auth0|reviewer",
                    commit_sha=view["commit_sha"], approve=True)
    assert exc.value.code == "cosign_not_pending"


def test_cosign_deny_is_terminal(lane_env):
    view = _propose(edits=[{"path": "server/auth.py", "content": "AUTH = 3\n"}])
    denied = lane.cosign(change_id=view["change_id"],
                         approver_subject="auth0|reviewer",
                         commit_sha=view["commit_sha"], approve=False)
    assert denied["state"] == "denied"
    with pytest.raises(lane.PlatformCustomizeError) as exc:
        lane.land(change_id=view["change_id"], tenant_id=TENANT)
    assert exc.value.code == "change_not_landable"


def test_approval_secret_verification_is_strict(lane_env, monkeypatch):
    assert lane.verify_approval_secret("approval-secret") is True
    assert lane.verify_approval_secret("wrong") is False
    assert lane.verify_approval_secret("") is False
    assert lane.verify_approval_secret(None) is False
    monkeypatch.setenv("LEAF_CUSTOMIZATION_APPROVAL_SECRET", "")
    # unset secret can NEVER verify, even against an empty presentation
    assert lane.verify_approval_secret("") is False


# --------------------------------------------------------------------------- #
# 3. landing (handoff, not deploy)
# --------------------------------------------------------------------------- #
def test_land_without_push_reports_pending_handoff(lane_env):
    view = _propose()
    landed = lane.land(change_id=view["change_id"], tenant_id=TENANT)
    assert landed["state"] == "landed"
    assert landed["push"] == {"pushed": False, "remote": None,
                              "at": landed["push"]["at"]}
    assert landed["landing_path"]["rollback"] == "previous ECS task-definition revision"
    assert "sol-critic review gate" in landed["landing_path"]["pipeline"]
    # idempotent
    again = lane.land(change_id=view["change_id"], tenant_id=TENANT)
    assert again["state"] == "landed"


def test_land_pushes_only_the_lane_ref(lane_env, tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    repo = lane_env["repo"]
    _git(repo, "remote", "add", "origin", str(remote))
    monkeypatch.setenv("LEAF_PLATFORM_REPO_PUSH", "1")
    view = _propose()
    landed = lane.land(change_id=view["change_id"], tenant_id=TENANT)
    assert landed["push"]["pushed"] is True
    remote_refs = subprocess.run(
        ["git", "--git-dir", str(remote), "for-each-ref",
         "--format=%(refname)"], check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout.split()
    assert remote_refs == [f"refs/heads/{view['branch']}"]  # never main


def test_land_refuses_foreign_tenant(lane_env):
    view = _propose()
    with pytest.raises(lane.PlatformCustomizeError) as exc:
        lane.land(change_id=view["change_id"], tenant_id=OTHER_TENANT)
    assert exc.value.code == "change_not_found"


def test_land_refuses_diverged_branch(lane_env):
    repo = lane_env["repo"]
    view = _propose()
    # someone rewrote the lane ref out-of-band -> the record no longer names it
    _git(repo, "update-ref", f"refs/heads/{view['branch']}",
         _git(repo, "rev-parse", "refs/heads/main"))
    with pytest.raises(lane.PlatformCustomizeError) as exc:
        lane.land(change_id=view["change_id"], tenant_id=TENANT)
    assert exc.value.code == "branch_diverged"


# --------------------------------------------------------------------------- #
# 4. route gate (admin tier + R7 internal allowlist)
# --------------------------------------------------------------------------- #
def _admitted(tenant, monkeypatch, *, auth=True):
    from routers import platform_customize as router_mod
    monkeypatch.setattr(deps, "auth_live", lambda: auth)
    return router_mod._gate(tenant)


def test_gate_requires_live_auth(lane_env, monkeypatch):
    out = _admitted(TENANT, monkeypatch, auth=False)
    assert not isinstance(out, tuple)
    assert json.loads(out.body)["reason_code"] == "platform_customize_auth_required"


def test_gate_denies_every_non_admin_tier(lane_env, monkeypatch):
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R7_MODE", "internal")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_INTERNAL_TENANTS", TENANT)
    for tier in ["demo", "guest", "restricted", "self_hosted",
                 "hosted_starter", "hosted_pro"]:
        tenant = deps.TenantContext(TENANT, tier=tier, subject="auth0|u")
        out = _admitted(tenant, monkeypatch)
        assert not isinstance(out, tuple), tier
        body = json.loads(out.body)
        assert body["entitlement_required"] is True, tier
        assert body["required"] == "platform_customize", tier


def test_gate_requires_internal_mode_and_allowlist(lane_env, monkeypatch):
    tenant = deps.TenantContext(TENANT, tier="admin", subject="auth0|u")
    # no mode set -> off
    monkeypatch.delenv("LEAF_CUSTOMIZATION_R7_MODE", raising=False)
    out = _admitted(tenant, monkeypatch)
    assert json.loads(out.body)["reason_code"] == "platform_customize_disabled"
    # mode=all deliberately reads as OFF for R7
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R7_MODE", "all")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_INTERNAL_TENANTS", TENANT)
    out = _admitted(tenant, monkeypatch)
    assert json.loads(out.body)["reason_code"] == "platform_customize_disabled"
    # internal + allowlisted -> admitted
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R7_MODE", "internal")
    out = _admitted(tenant, monkeypatch)
    assert out == (TENANT, "admin")
    # internal but NOT allowlisted -> off
    monkeypatch.setenv("LEAF_CUSTOMIZATION_INTERNAL_TENANTS", "someone_else")
    out = _admitted(tenant, monkeypatch)
    assert json.loads(out.body)["reason_code"] == "platform_customize_disabled"


def test_r7_flag_is_internal_only():
    assert customization_flags.enabled(7, TENANT) is False


def test_admin_tier_is_sole_platform_customize_grant():
    for tier in ["demo", "guest", "restricted", "self_hosted",
                 "hosted_starter", "hosted_pro"]:
        assert entitlements.entitlements_for(tier)["platform_customize"] is False, tier
    assert entitlements.entitlements_for("admin")["platform_customize"] is True
