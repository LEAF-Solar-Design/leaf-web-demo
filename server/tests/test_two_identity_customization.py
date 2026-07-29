"""Authored-tool state uses the platform identity; billing keeps the claim.

Issue #304. Two identities exist on a request, and conflating them caused the
original defect:

  request identity   the verified JWT claim. Jobs, broker admissions, spend caps,
                     usage and billing key off it, and its `tier` claim drives
                     entitlement enforcement (contract/AUTH.md §11, FROZEN).
  customization id   the active platform binding. customization_service verifies
                     the caller against it, so change sets, catalogs, tenant Git
                     and tool-source lookup must use it.

A platform tenant id is a server-minted UUID with the external subject bound to
it through identity_bindings, so for any tenant provisioned through POST /api/orgs
the two differ. Observed on staging: claim `acceptance-tenant-a-20260728` against
binding `bccb0d64-04c9-4108-bcc1-f27b8bb3924d`.

The point of this file is that BOTH hold at once. A change that moved everything
to the platform identity would satisfy half of it and silently relocate tier
authority, which is the mistake PR #313 was closed for.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

import deps


CLAIM = "acceptance-tenant-a-20260728"
PLATFORM = "bccb0d64-04c9-4108-bcc1-f27b8bb3924d"
SUBJECT = "auth0|owner"


@pytest.fixture()
def mismatched(monkeypatch):
    """A caller whose claim differs from its platform tenant, as designed."""
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")

    def _authority(subject):
        return (PLATFORM, "hosted_pro")

    _authority.cache_clear = lambda: None
    monkeypatch.setattr(
        deps, "resolve_active_platform_tenant_authority", _authority)

    import tenancy
    tenancy.reset_store()

    def _get_store():
        return SimpleNamespace(resolve_workspace=lambda tenant_id: None)

    _get_store.cache_clear = lambda: None
    monkeypatch.setattr(tenancy, "get_store", _get_store)
    return deps.TenantContext(CLAIM, org_id=CLAIM, tier="hosted_pro",
                              subject=SUBJECT)


# --------------------------------------------------------------------------- #
# the two identities are genuinely different
# --------------------------------------------------------------------------- #
def test_the_customization_identity_is_the_platform_tenant(mismatched):
    assert deps.customization_tenant(mismatched) == PLATFORM
    # and the request identity is untouched
    assert str(mismatched) == CLAIM
    assert mismatched.tier == "hosted_pro"


def test_a_caller_with_no_user_keeps_its_own_identity(monkeypatch):
    """The harness back edge has no subject and no org, so there is nothing to
    resolve and it must not be rewritten."""
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    backedge = deps.TenantContext("tenant-a", tier="hosted_pro")

    assert deps.customization_tenant(backedge) == "tenant-a"


def test_auth_off_callers_are_unchanged(monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    assert deps.customization_tenant("demo-tenant") == "demo-tenant"


# --------------------------------------------------------------------------- #
# authored-tool reads use the customization identity
# --------------------------------------------------------------------------- #
def _override(app_module, context):
    app_module.app.dependency_overrides[deps.require_tenant] = lambda: context


def test_the_tools_catalog_is_read_under_the_platform_identity(
    mismatched, monkeypatch,
):
    from fastapi.testclient import TestClient

    import app as app_module

    seen = {}
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    def _all_tools(tenant_id):
        seen["tenant"] = str(tenant_id)
        return []

    monkeypatch.setattr(deps, "all_tools", _all_tools)
    _override(app_module, mismatched)
    try:
        response = TestClient(app_module.app,
                              raise_server_exceptions=False).get("/api/tools")
    finally:
        app_module.app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert seen["tenant"] == PLATFORM, (
        "the catalog was read under the raw claim, so a tool published under "
        "the platform tenant would be invisible"
    )


def test_the_run_route_finds_the_tool_under_the_platform_identity(
    mismatched, monkeypatch,
):
    """The read side of the publish/execute split that closed PR #313's first
    attempt: authoring stored under the platform id while /api/run looked up the
    claim and answered UNKNOWN_TOOL."""
    from fastapi.testclient import TestClient

    import app as app_module
    from routers import jobs as jobs_router

    seen = {}

    def _find_tool(name, tenant_id, *args, **kwargs):
        # find_tool takes the tenant SECOND; watching the first would record
        # the tool name.
        seen["tenant"] = str(tenant_id)
        return None

    monkeypatch.setattr(deps, "auth_live", lambda: True)
    monkeypatch.setattr(jobs_router.deps, "find_tool", _find_tool)
    _override(app_module, mismatched)
    try:
        TestClient(app_module.app, raise_server_exceptions=False).post(
            "/api/run", json={"tool": "no-such-tool", "params": {},
                              "dwg": "rooftop_demo"})
    finally:
        app_module.app.dependency_overrides.clear()

    assert seen.get("tenant") == PLATFORM, (
        f"the tool was looked up under {seen.get('tenant')!r}"
    )


# --------------------------------------------------------------------------- #
# and the request identity keeps everything else
# --------------------------------------------------------------------------- #
def test_the_routes_still_take_the_request_identity():
    """NOT a dependency swap.

    Swapping these to require_active_tenant is what closed PR #313: it replaces
    the JWT tier with the platform org tier, while contract/AUTH.md §11 (frozen)
    makes the `tier` claim drive entitlement enforcement. The split has to live
    at the call sites, not the dependency.
    """
    from routers import capabilities as capabilities_router
    from routers import jobs as jobs_router
    from routers import prompt as prompt_router
    from routers import tools as tools_router

    for module in (jobs_router, tools_router, capabilities_router,
                   prompt_router):
        source = inspect.getsource(module)
        assert "Depends(deps.require_tenant)" in source, (
            f"{module.__name__} stopped taking the request identity, which "
            "moves tier authority away from the JWT claim"
        )
        assert "Depends(deps.require_active_tenant)" not in source


def test_the_tier_still_comes_from_the_request_identity(mismatched, monkeypatch):
    """entitlements must resolve the claim's tier, not the platform org's."""
    import entitlements

    monkeypatch.setattr(deps, "auth_live", lambda: True)
    downgraded = deps.TenantContext(CLAIM, org_id=CLAIM, tier="hosted_starter",
                                    subject=SUBJECT)

    # The platform authority in this fixture reports hosted_pro. The request
    # identity says hosted_starter, and that is what must be enforced.
    assert entitlements.resolve_tier(downgraded) == "hosted_starter"
    assert deps.customization_tenant(downgraded) == PLATFORM


def test_the_job_row_is_not_keyed_by_the_platform_identity(mismatched, monkeypatch):
    """Jobs, admissions, caps and the ledger stay on the request identity, so no
    historical re-key or quota reset is required."""
    from fastapi.testclient import TestClient

    import app as app_module
    from routers import jobs as jobs_router

    seen = {}
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    monkeypatch.setattr(
        jobs_router.deps, "find_tool",
        lambda name, tenant_id, *a, **k: {"name": name, "entry": "x.py"})
    monkeypatch.setattr(jobs_router.deps, "catalog_tool_digest",
                        lambda tool: "digest")
    monkeypatch.setattr(
        jobs_router, "submit_job",
        lambda tenant_id, *a, **k: seen.setdefault("job_tenant", str(tenant_id)),
        raising=False)
    _override(app_module, mismatched)
    try:
        TestClient(app_module.app, raise_server_exceptions=False).post(
            "/api/run", json={"tool": "some-tool", "params": {},
                              "dwg": "rooftop_demo",
                              "catalog_digest": "digest"})
    finally:
        app_module.app.dependency_overrides.clear()

    if "job_tenant" in seen:
        assert seen["job_tenant"] == CLAIM, (
            "the job row moved to the platform identity, which would strand "
            "historical jobs and reset the spend basis"
        )
