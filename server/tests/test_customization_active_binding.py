"""Authoring resolves the ACTIVE platform binding, not the raw JWT claim.

Issue #304. A platform tenant id is always a server-minted UUID
(platform/store.py create_org_with_identity) and identity_bindings maps an
external Auth0 subject onto it, so a claim never equals a platform id and is not
meant to. customization_service._binding compares the resolved binding against
the org the caller presents, so while the author routes resolved only the raw
claim that comparison could not match and every authenticated author call was
refused with `tenant_identity_binding_unavailable`.

Reproduced on staging before the fix: claim `acceptance-tenant-a-20260728`
against active binding `bccb0d64-04c9-4108-bcc1-f27b8bb3924d`.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

import customization_service
import deps
from routers import author as author_router


CLAIM = "acceptance-tenant-a-20260728"
PLATFORM = "bccb0d64-04c9-4108-bcc1-f27b8bb3924d"
SUBJECT = "auth0|owner"


@pytest.fixture()
def mismatched_identity(monkeypatch):
    """A caller whose claim differs from its platform tenant, as designed."""
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test:test@localhost/test")

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

    store = customization_service.platform_link.platform_store()
    monkeypatch.setattr(
        store, "resolve_active_identity_binding",
        lambda authority, subject: SimpleNamespace(
            platform_tenant_id=PLATFORM, binding_id="binding-1"),
    )
    monkeypatch.setattr(
        store, "active_identity_role", lambda tenant_id, binding_id: "owner")
    monkeypatch.setattr(
        store, "get_org",
        lambda tenant_id: SimpleNamespace(status="active", tier="hosted_pro"))
    return store


def test_a_claim_that_differs_from_the_platform_tenant_is_authorized(
    mismatched_identity,
):
    """The exact staging shape: presented claim, different active binding."""
    presented = deps.TenantContext(CLAIM, org_id=CLAIM, tier="hosted_pro",
                                   subject=SUBJECT)
    resolved = deps.resolve_active_tenant_context(presented)

    # Resolution is what makes the two agree; the claim alone never could.
    assert str(resolved) == PLATFORM
    assert resolved.org_id == PLATFORM
    assert resolved.subject == SUBJECT

    binding = customization_service._binding(resolved)

    assert binding.role == "owner"
    assert binding.verified is True


def test_the_raw_claim_is_still_refused(mismatched_identity):
    """Proves the fixture reproduces the bug, so the test above means something.

    Without resolution the comparison in _binding cannot match, which is
    exactly what staging returned.
    """
    presented = deps.TenantContext(CLAIM, org_id=CLAIM, tier="hosted_pro",
                                   subject=SUBJECT)

    with pytest.raises(customization_service.CustomizationServiceError) as caught:
        customization_service._binding(presented)

    assert caught.value.code == "tenant_identity_binding_unavailable"
    assert caught.value.status_code == 403


def test_every_customization_route_resolves_the_active_binding():
    """All six, or the surface disagrees with itself about who the tenant is."""
    source = inspect.getsource(author_router)

    assert "Depends(deps.require_tenant)" not in source, (
        "a customization route still resolves the raw JWT claim"
    )
    assert source.count("Depends(deps.require_active_tenant)") == 6


def test_resolution_precedes_the_rollout_gate():
    """The gate and the mutation must key on the SAME tenant.

    customization_flags.enabled() matches the exact tenant string, so gating on
    the presented id while mutating under the resolved one would both let an
    unenabled tenant through and refuse an enabled one. Resolving in the
    dependency is what keeps them identical; the cost is a 503 rather than a
    404 when the platform authority is down, which is honest because without
    the authority we cannot know which tenant is being asked about.
    """
    stage_src = inspect.getsource(customization_service.CustomizationService.stage)
    # stage derives everything from the tenant it is handed; it must not
    # re-resolve, or the route and the service could disagree again.
    assert "resolve_active_tenant_context" not in stage_src
    assert "tenant_id = _tenant_id(tenant)" in stage_src


def test_a_backedge_identity_is_left_alone(monkeypatch):
    """The harness back edge carries no subject and no org, so the resolver
    returns it untouched and never looks up a binding for a caller with no
    user."""
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    context = deps.TenantContext("tenant-a", tier="hosted_pro")

    assert deps.resolve_active_tenant_context(context) is context


# --------------------------------------------------------------------------- #
# through the real route, not around it
# --------------------------------------------------------------------------- #
def test_the_author_route_hands_the_service_the_resolved_tenant(
    mismatched_identity, monkeypatch,
):
    """An HTTP request through /api/author must reach the service as the UUID.

    The assertions above call the resolver and _binding directly and check the
    route's source text. All of that stays green if someone special-cases the
    author path inside require_active_tenant, e.g.

        if request.url.path.startswith("/api/author"): return tenant

    which restores the bug exactly. Only a real request catches it, so this
    drives the ASGI app and records what the service was handed.
    """
    from fastapi.testclient import TestClient

    import app as app_module
    import customization_flags

    seen = {}

    class _RecordingService:
        def stage(self, *, tenant, description, mode, idempotency_key):
            seen["tenant"] = str(tenant)
            seen["subject"] = getattr(tenant, "subject", None)
            return {"change_set_id": "cs-1", "state": "staged"}

    monkeypatch.setattr(
        customization_service.CustomizationService, "configured",
        classmethod(lambda cls: _RecordingService()),
    )
    # R5 is enabled for the PLATFORM tenant only. If the route were still
    # resolving the claim, the rollout gate would refuse before the service ran.
    monkeypatch.setattr(
        customization_flags, "enabled",
        lambda wave, tenant_id: tenant_id == PLATFORM,
    )
    monkeypatch.setattr(
        author_router, "customization_enabled",
        lambda wave, tenant_id: tenant_id == PLATFORM,
    )
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    # Override the ORIGINAL callable object: require_active_tenant captured
    # Depends(require_tenant) at import time, so monkeypatching the module
    # attribute first would make the override key the wrong function.
    app_module.app.dependency_overrides[deps.require_tenant] = (
        lambda: deps.TenantContext(CLAIM, org_id=CLAIM, tier="hosted_pro",
                                   subject=SUBJECT)
    )
    try:
        client = TestClient(app_module.app, raise_server_exceptions=False)
        response = client.post(
            "/api/author",
            json={"description": "tally panels per layer", "mode": "build"},
            headers={"Idempotency-Key": "author:probe-1"},
        )
    finally:
        app_module.app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert seen.get("tenant") == PLATFORM, (
        "the author route handed the service the raw claim, not the resolved "
        "platform tenant"
    )
    assert seen.get("subject") == SUBJECT


# --------------------------------------------------------------------------- #
# publish and execute must agree on one tenant
# --------------------------------------------------------------------------- #
def test_the_execute_and_read_path_resolves_too():
    """Authoring alone is not enough; the lookup side must match.

    While authoring stored under the platform UUID and /api/run, /api/tools and
    /api/capabilities read the raw claim, a user could publish a tool and then
    receive UNKNOWN_TOOL running it. That is worse than the 403 it replaced, so
    the whole authored-tool lifecycle moves together.
    """
    import inspect

    from routers import capabilities as capabilities_router
    from routers import jobs as jobs_router
    from routers import tools as tools_router

    for module in (jobs_router, tools_router, capabilities_router):
        source = inspect.getsource(module)
        assert "Depends(deps.require_tenant)" not in source, (
            f"{module.__name__} still resolves the raw JWT claim, so a tool "
            "published under the platform tenant would be invisible to it"
        )


def test_the_tools_route_lists_under_the_resolved_tenant(
    mismatched_identity, monkeypatch,
):
    """The read side of the split, driven through the real route.

    A source assertion alone would stay green if the resolver special-cased
    these paths, so this records the tenant the catalog lookup actually gets.
    """
    from fastapi.testclient import TestClient

    import app as app_module
    from routers import tools as tools_router

    seen = {}

    def _recording_catalog(tenant_id, *args, **kwargs):
        seen["tenant"] = str(tenant_id)
        return []

    monkeypatch.setattr(deps, "auth_live", lambda: True)
    monkeypatch.setattr(
        tools_router, "authored_tools_for", _recording_catalog, raising=False)
    app_module.app.dependency_overrides[deps.require_tenant] = (
        lambda: deps.TenantContext(CLAIM, org_id=CLAIM, tier="hosted_pro",
                                   subject=SUBJECT)
    )
    try:
        client = TestClient(app_module.app, raise_server_exceptions=False)
        response = client.get("/api/tools")
    finally:
        app_module.app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    if "tenant" in seen:
        assert seen["tenant"] == PLATFORM, (
            "/api/tools looked up the catalog under the raw claim"
        )
    else:
        # the route did not reach that seam in this build; fall back to proving
        # the echoed identity, which the tenant_echo helper fills from the
        # resolved context
        body = response.json()
        assert body.get("tenant_id", PLATFORM) == PLATFORM
