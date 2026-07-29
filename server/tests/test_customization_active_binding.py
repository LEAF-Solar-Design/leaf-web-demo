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
