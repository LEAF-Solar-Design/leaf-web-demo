"""The tool customization lane cannot stage Lab document bytes."""
from types import SimpleNamespace

import pytest
import customization_service as service
from platform_release_policy import load_policy


@pytest.mark.parametrize("path", ["lab/graphs/x.json", "lab/widgets/x.json"])
def test_tool_stage_refuses_lab_paths(monkeypatch, path):
    policy = load_policy()
    change = SimpleNamespace(tenant_id="tenant-a", base_commit="a" * 40,
                             staged_commit="b" * 40,
                             desired_platform_release=next(iter(policy.releases)))
    monkeypatch.setattr(service, "_bare_repo", lambda tenant: "unused")
    monkeypatch.setattr(service, "_git", lambda *args: "registry.json\0" + path + "\0")
    with pytest.raises(service.CustomizationServiceError) as failure:
        service.CustomizationService._verify_stage_policy(change)
    assert failure.value.code == "unclassified_path_changed"
