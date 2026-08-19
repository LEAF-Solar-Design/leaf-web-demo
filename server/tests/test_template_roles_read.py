"""Acceptance tests for card C2-6: role matrix, template read surface.

Acceptance oracle:
    viewer/editor/owner x template read: all roles read templates their
    tenant is entitled to; no role reads another tenant's clones; denial is
    same-shape (no existence leak). RED-TEAM PROOF REQUIRED (kimi adversary
    paired with the gate, A2 executing reviewer).

Offline, in-process (direct function calls against ``routers.templates``),
mirroring ``tests/test_template_pinning.py``'s sys.path setup and calling
convention.

Run:  cd server && python -m pytest tests/test_template_roles_read.py -q
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("LEAF_AUTH_LIVE", "0")

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import pytest  # noqa: E402

import deps  # noqa: E402
import templates  # noqa: E402
from routers import templates as templates_router  # noqa: E402


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setenv(templates.FLAG_SOLAR_TEMPLATE_BETA, "1")


def _ctx(tenant_id: str, roles: tuple = (), elevated: bool = False) -> deps.TenantContext:
    return deps.TenantContext(tenant_id, roles=roles, elevated=elevated)


# --------------------------------------------------------------------------- #
# ORACLE: all three roles read templates their tenant is entitled to
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", ["viewer", "editor", "owner"])
def test_each_matrix_role_can_list_templates(role):
    tenant = _ctx("tenant-a", roles=(role,))
    body = templates_router.list_templates(tenant=tenant)
    assert body["templates"], f"role {role!r} must be able to list templates"


@pytest.mark.parametrize("role", ["viewer", "editor", "owner"])
def test_each_matrix_role_can_read_a_template(role):
    tenant = _ctx("tenant-a", roles=(role,))
    body = templates_router.read_template(
        "rooftop-standard-string", version="1.0.0", tenant=tenant)
    assert body["template_id"] == "rooftop-standard-string"
    assert body["version"] == "1.0.0"


def test_holding_multiple_roles_still_reads():
    tenant = _ctx("tenant-a", roles=("viewer", "owner"))
    body = templates_router.list_templates(tenant=tenant)
    assert body["templates"]


# --------------------------------------------------------------------------- #
# ORACLE: denial is same-shape -- no existence leak
# --------------------------------------------------------------------------- #
def test_no_role_denies_list_with_403_forbidden():
    tenant = _ctx("tenant-a", roles=())
    resp = templates_router.list_templates(tenant=tenant)
    assert resp.status_code == 403
    body = json.loads(resp.body)
    assert body["error"]["error_code"] == "FORBIDDEN"


def test_unknown_role_denies_same_shape_as_no_role():
    """A role name that is not in the matrix (not a case variant, not a
    substring collision) must be denied identically to holding none."""
    no_role = json.loads(templates_router.list_templates(tenant=_ctx("t1", roles=())).body)
    unknown_role = json.loads(
        templates_router.list_templates(tenant=_ctx("t1", roles=("reviewer",))).body)
    assert no_role == unknown_role


def test_substring_collision_role_is_rejected_not_matched():
    """'reviewer' ends with 'viewer' -- a substring check would wrongly admit
    it. Exact-set membership must reject it."""
    resp = templates_router.list_templates(tenant=_ctx("t1", roles=("reviewer",)))
    assert resp.status_code == 403


def test_case_variant_role_is_normalized_not_defaulted_to_allow_or_deny():
    """A verified 'Editor' claim must resolve the SAME as 'editor' (the
    normalization deps.require_tenant already applies upstream) rather than
    silently falling through to a default-allow OR a default-deny."""
    resp = templates_router.list_templates(tenant=_ctx("t1", roles=("Editor",)))
    # Success returns a plain dict (no JSONResponse wrapper) -- see
    # test_router_list_returns_the_registry_when_flag_on in
    # test_template_pinning.py for the same convention.
    assert isinstance(resp, dict)
    assert resp["templates"]


def test_denial_is_identical_shape_for_real_unknown_and_bad_version_targets():
    """The role gate must run BEFORE any registry lookup: whether the caller
    named a real template, an unknown template, or an unknown version, the
    denial status and body are byte-identical -- no existence leak through
    the role gate (the C2-6 get-then-403 trap)."""
    no_role = _ctx("tenant-a", roles=())
    real = templates_router.read_template(
        "rooftop-standard-string", version="1.0.0", tenant=no_role)
    unknown_template = templates_router.read_template(
        "does-not-exist", tenant=no_role)
    unknown_version = templates_router.read_template(
        "rooftop-standard-string", version="9.9.9", tenant=no_role)
    for resp in (real, unknown_template, unknown_version):
        assert resp.status_code == 403
    real_body = json.loads(real.body)
    assert real_body == json.loads(unknown_template.body)
    assert real_body == json.loads(unknown_version.body)


def test_list_and_read_denials_share_the_same_shape():
    no_role = _ctx("tenant-a", roles=())
    list_resp = templates_router.list_templates(tenant=no_role)
    read_resp = templates_router.read_template(
        "rooftop-standard-string", tenant=no_role)
    assert list_resp.status_code == read_resp.status_code == 403
    assert json.loads(list_resp.body) == json.loads(read_resp.body)


def test_flag_off_denial_wins_over_role_denial_and_stays_404(monkeypatch):
    """Flag-off must dominate: a caller with no role gets the SAME flag-off
    404 as an entitled owner, never a 403 that would leak whether the flag
    is actually on."""
    monkeypatch.delenv(templates.FLAG_SOLAR_TEMPLATE_BETA, raising=False)
    no_role = _ctx("tenant-a", roles=())
    owner = _ctx("tenant-a", roles=("owner",))
    resp_no_role = templates_router.list_templates(tenant=no_role)
    resp_owner = templates_router.list_templates(tenant=owner)
    assert resp_no_role.status_code == resp_owner.status_code == 404
    assert json.loads(resp_no_role.body) == json.loads(resp_owner.body)


# --------------------------------------------------------------------------- #
# ORACLE: no role reads another tenant's clones (cross-tenant isolation)
# --------------------------------------------------------------------------- #
def test_own_tenant_identity_is_echoed_never_a_different_one():
    """The response's tenant echo must always match the CALLER's own
    verified tenant, regardless of role -- a role held under tenant A's
    context can never surface tenant B's identity or data."""
    tenant_a = _ctx("tenant-a", roles=("owner",))
    tenant_b = _ctx("tenant-b", roles=("owner",))
    body_a = templates_router.list_templates(tenant=tenant_a)
    body_b = templates_router.list_templates(tenant=tenant_b)
    assert body_a["tenant_id"] == "tenant-a"
    assert body_b["tenant_id"] == "tenant-b"
    assert body_a["tenant_id"] != body_b["tenant_id"]


def test_role_gate_never_consults_a_caller_supplied_tenant_selector():
    """The gate takes only ``tenant.roles`` off the dependency-resolved
    identity -- it accepts no other parameter that could let a caller name a
    different tenant to read against. Guards against a future regression
    that threads a query/header tenant override into ``_authorized_for_read``."""
    import inspect

    sig = inspect.signature(templates_router._authorized_for_read)
    assert list(sig.parameters) == ["tenant"]


# --------------------------------------------------------------------------- #
# legacy / auth-off byte-identical path (regression guard, not the oracle)
# --------------------------------------------------------------------------- #
def test_legacy_plain_str_tenant_is_unaffected_by_the_role_gate():
    """A pre-C2-6 caller -- LEAF_AUTH_LIVE off, tenant is a plain str, no
    role claim ever exists -- keeps reading exactly as before."""
    body = templates_router.list_templates(tenant="demo-tenant")
    assert body["templates"]
    body = templates_router.read_template(
        "rooftop-standard-string", version="1.0.0", tenant="demo-tenant")
    assert body["version"] == "1.0.0"
