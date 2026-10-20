"""Verified demo W1 tools never inherit general tier grants."""
from unittest.mock import Mock

import pytest

import demo_policy as policy
import deps
import solar_project_admission as admission


TOOLS = frozenset({
    "solar-settings", "solar-panels-from-drawing", "solar-size-strings",
    "solar-combiners", "solar-feeders", "solar-homeruns", "solar-schedule",
    "solar-string-add", "solar-assign-equipment", "solar-string-multi-add",
    "solar-string-conductors", "solar-panel-groups", "solar-correct-string",
})


def test_tool_set_is_explicit():
    assert policy.DEMO_SOLAR_TOOLS == TOOLS


@pytest.mark.parametrize("tool", sorted(TOOLS))
def test_local_tools_allowed(tool):
    tenant = deps.TenantContext("demo-test", verified_demo=True)
    assert policy.demo_run_verdict(tenant, tool, {}) is None


@pytest.mark.parametrize("mode,reason", [
    ("global", "demo_paid_service_not_allowed"), ("manual", None),
])
def test_sizing_mode(mode, reason):
    tenant = deps.TenantContext("demo-test", verified_demo=True)
    assert policy.demo_run_verdict(tenant, "solar-size-strings", {"mode": mode}) == reason


@pytest.mark.parametrize("tool", ["solar-solve-proposal", "solar-commit-solve", "drawing.inspect"])
def test_other_tools_refused(tool):
    tenant = deps.TenantContext("demo-test", verified_demo=True)
    assert policy.demo_run_verdict(tenant, tool, {}) == "demo_tool_not_allowed"


@pytest.mark.parametrize("tenant", ["demo-test", deps.TenantContext("account"),
                                     deps.TenantContext("demo-test", tier="demo_live")])
def test_unverified_context_never_allowed(tenant):
    assert policy.demo_run_verdict(tenant, "solar-settings", {}) == "demo_tool_not_allowed"


def test_non_demo_admission_does_not_consult_demo_policy(monkeypatch):
    monkeypatch.setenv("LEAF_SOLAR_PROJECT_RUN_ENABLED", "1")
    spy = Mock(side_effect=AssertionError("non-demo consulted demo policy"))
    monkeypatch.setattr(admission, "demo_run_verdict", spy)
    monkeypatch.setattr(admission.entitlements, "resolve_tier", lambda tenant: "restricted")
    monkeypatch.setattr(admission.entitlements, "resolve_roles", lambda tenant: ((), False))
    result = admission.project_tool_admission({"name": "solar-settings"},
        deps.TenantContext("account"), org_header=None, project_header=None,
        authorization=None, input_version_id=None, drawing_id=None, params={},
        catalog_digest=None, idempotency_key=None, checkout_capability=None, preview=False)
    assert result["status_code"] == 403
    spy.assert_not_called()


@pytest.mark.parametrize("tool,params,reason", [
    ("solar-size-strings", {"mode": "global"}, "demo_paid_service_not_allowed"),
    ("solar-solve-proposal", {}, "demo_tool_not_allowed"),
    ("solar-commit-solve", {}, "demo_tool_not_allowed"),
    ("drawing.inspect", {}, "demo_tool_not_allowed"),
])
def test_canonical_policy_precedes_all_storage_and_cloud_reads(monkeypatch, tool, params, reason):
    import solar_project_jobs as service
    import solar_project_context as project

    forbidden = Mock(side_effect=AssertionError("refused demo touched storage or cloud"))
    monkeypatch.setattr(service.project, "_access", forbidden)
    monkeypatch.setattr(service.cloud, "resolve_grant", forbidden)
    tenant = deps.TenantContext("demo-test", verified_demo=True)
    with pytest.raises(project.ProjectContextError) as caught:
        service.submit_project_graph_job(tenant, None, None, None, tool, params,
            tool_manifest_sha256=None, checkout_capability=None, idempotency_key="refused")
    assert caught.value.reason_code == reason
    forbidden.assert_not_called()
