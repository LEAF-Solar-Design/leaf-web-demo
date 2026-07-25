import json
import os
import sys
import hashlib
from pathlib import Path

import pytest


SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import platform_release_policy as policy_module


POLICY_FILE = SERVER_DIR / "platform_release_policy.json"
RELEASE_ID = "leaf-platform-2026.07.23"


def _raw_policy():
    return json.loads(POLICY_FILE.read_text(encoding="utf-8"))


def _write_policy(tmp_path, raw):
    target = tmp_path / "policy.json"
    target.write_text(json.dumps(raw), encoding="utf-8")
    return target


def _reference(**overrides):
    reference = {
        "contract": "leaf.workspace.v1",
        "workspace_contract": "leaf.workspace.v1",
        "desired_platform_release": RELEASE_ID,
    }
    reference.update(overrides)
    return reference


def test_shipped_policy_classifies_existing_tenant_tool_paths_and_protected_files():
    policy = policy_module.load_policy()
    assert policy_module.classify_path(policy, RELEASE_ID, "tools/panel_count/tool.py") == "tenant_owned"
    assert policy_module.classify_path(policy, RELEASE_ID, "config/panel_defaults.json") == "slushy"
    assert policy_module.classify_path(policy, RELEASE_ID, "registry.json") == "frozen"
    assert policy_module.classify_path(policy, RELEASE_ID, ".github/workflows/release.yml") == "frozen"
    assert policy_module.classify_path(policy, RELEASE_ID, "credentials/production.json") == "frozen"
    assert policy_module.classify_path(policy, RELEASE_ID, "requirements.txt") == "frozen"
    assert policy_module.classify_path(policy, RELEASE_ID, "unlisted/file.py") == policy_module.DENIED


def test_shipped_workspace_contract_digest_matches_the_frozen_schema():
    policy = policy_module.load_policy()
    schema = SERVER_DIR.parent / "contract" / "customization.v1.schema.json"
    # Hash the CANONICAL (LF) bytes, not the working copy's. Hashing raw bytes
    # makes this digest depend on the checkout's line endings, so a single
    # frozen constant cannot match on both a core.autocrlf=true clone and a
    # Linux runner: the shipped constant was frozen from a CRLF working copy
    # and could never match in CI. No runtime code recomputes this digest --
    # workspace_contract_sha256 is only passed through -- so this test is its
    # sole verifier, and anchoring it to the committed bytes is what makes the
    # drift guard mean the same thing on every host.
    digest = hashlib.sha256(schema.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
    assert policy.workspace_contracts["leaf.workspace.v1"] == digest


def test_workspace_reference_allows_only_the_three_contract_fields():
    policy = policy_module.load_policy()
    release = policy_module.validate_workspace_reference(policy, _reference())
    assert release.release_id == RELEASE_ID
    with pytest.raises(policy_module.PlatformReleasePolicyError):
        policy_module.validate_workspace_reference(policy, _reference(path_rules=[]))
    with pytest.raises(policy_module.PlatformReleasePolicyError):
        policy_module.validate_workspace_reference(policy, _reference(workspace_contract="other.workspace.v1"))


@pytest.mark.parametrize("bad_path", [
    "../tools/panel/tool.py", "tools/../registry.json", "tools//panel/tool.py", "/tools/panel/tool.py",
])
def test_path_traversal_and_absolute_paths_are_rejected(bad_path):
    policy = policy_module.load_policy()
    with pytest.raises(policy_module.PlatformReleasePolicyError):
        policy_module.classify_path(policy, RELEASE_ID, bad_path)


def test_separator_and_case_aliases_are_rejected():
    policy = policy_module.load_policy()
    for path in ("tools\\panel\\tool.py", "Tools/panel/tool.py"):
        with pytest.raises(policy_module.PlatformReleasePolicyError):
            policy_module.classify_path(policy, RELEASE_ID, path)


def test_unicode_alias_collisions_in_policy_are_rejected(tmp_path):
    raw = _raw_policy()
    raw["releases"][0]["rules"].append({"path": "tools/caf\u00e9.py", "mutability": "tenant_owned"})
    raw["releases"][0]["rules"].append({"path": "tools/cafe\u0301.py", "mutability": "tenant_owned"})
    with pytest.raises(policy_module.PlatformReleasePolicyError):
        policy_module.load_policy(_write_policy(tmp_path, raw))


def test_case_collision_and_ambiguous_patterns_are_rejected(tmp_path):
    raw = _raw_policy()
    raw["releases"][0]["rules"].append({"path": "TOOLS/other.py", "mutability": "tenant_owned"})
    with pytest.raises(policy_module.PlatformReleasePolicyError):
        policy_module.load_policy(_write_policy(tmp_path, raw))

    raw = _raw_policy()
    raw["releases"][0]["rules"].append({"path": "tools/example.py", "mutability": "tenant_owned"})
    with pytest.raises(policy_module.PlatformReleasePolicyError):
        policy_module.load_policy(_write_policy(tmp_path, raw))


def test_unknown_release_and_contract_digest_mismatch_fail_closed(tmp_path):
    policy = policy_module.load_policy()
    with pytest.raises(policy_module.PlatformReleasePolicyError):
        policy_module.classify_path(policy, "unknown-release", "tools/panel/tool.py")

    raw = _raw_policy()
    raw["releases"][0]["workspace_contract_sha256"] = "0" * 64
    with pytest.raises(policy_module.PlatformReleasePolicyError):
        policy_module.load_policy(_write_policy(tmp_path, raw))


def test_unknown_fields_and_missing_policy_fail_closed(tmp_path):
    raw = _raw_policy()
    raw["tenant_rules"] = []
    with pytest.raises(policy_module.PlatformReleasePolicyError):
        policy_module.load_policy(_write_policy(tmp_path, raw))
    with pytest.raises(policy_module.PlatformReleasePolicyError):
        policy_module.load_policy(tmp_path / "missing.json")


def test_request_time_loading_observes_policy_replacement(tmp_path, monkeypatch):
    policy_path = _write_policy(tmp_path, _raw_policy())
    monkeypatch.setenv("LEAF_PLATFORM_RELEASE_POLICY_FILE", str(policy_path))
    assert policy_module.load_policy().releases[RELEASE_ID].release_id == RELEASE_ID
    raw = _raw_policy()
    raw["releases"][0]["rules"] = [
        rule for rule in raw["releases"][0]["rules"] if rule["path"] != "tools/**"
    ]
    policy_path.write_text(json.dumps(raw), encoding="utf-8")
    assert policy_module.classify_path(policy_module.load_policy(), RELEASE_ID, "tools/panel/tool.py") == "denied"


def test_real_workspace_symlink_input_is_rejected(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "tools"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable in this environment")
    policy = policy_module.load_policy()
    with pytest.raises(policy_module.PlatformReleasePolicyError):
        policy_module.classify_path(policy, RELEASE_ID, "tools/panel/tool.py", root=root)
