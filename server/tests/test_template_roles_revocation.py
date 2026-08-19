"""Acceptance tests for card C2-8: role matrix, cross-tenant + revocation.

Acceptance oracle:
    Revoked member loses template-instance access on next read AND write;
    cross-tenant probes with a real second tenant deny same-shape.
    RED-TEAM PROOF REQUIRED (A2 executing reviewer).

Offline, in-process (direct function calls against ``routers.templates``),
mirroring ``tests/test_template_roles_read.py`` and
``tests/test_template_roles_write.py``'s fixture convention and calling
style. Every call constructs a FRESH ``deps.TenantContext`` -- exactly what a
real per-request ``deps.require_tenant`` call produces from the CURRENT JWT
claim -- so a "revoked" caller is modeled as the SAME subject/tenant calling
again with roles that no longer include the held grant, never as a stale
context object reused across calls or a ``dependency_overrides`` pin (the
C2-8 vacuous-test trap: a pinned pre-revocation principal would exercise
nothing).

Run:  cd server && python -m pytest tests/test_template_roles_revocation.py -q
"""
from __future__ import annotations

import inspect
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
def _clean_state(monkeypatch):
    """Every test starts and ends with an empty clone/write/undo store, flag on."""
    monkeypatch.setenv(templates.FLAG_SOLAR_TEMPLATE_BETA, "1")
    monkeypatch.setattr(templates, "_PROJECT_CLONES", {})
    monkeypatch.setattr(templates, "_CLONE_WRITE_LOG", {})
    monkeypatch.setattr(templates, "_CLONE_UNDO_LOG", {})
    yield


def _ctx(tenant_id: str, roles: tuple = (), subject: str = "member",
         elevated: bool = False) -> deps.TenantContext:
    """A FRESH tenant identity, exactly as one ``require_tenant`` call would
    resolve from the CURRENT JWT claim -- never reused across "before" and
    "after" probes in the same test, so a passing test proves the gate
    re-reads role state on every call rather than a cached/snapshotted
    identity object."""
    return deps.TenantContext(tenant_id, subject=subject, roles=roles, elevated=elevated)


def _clone(tenant, project_id="project-1", template_id="rooftop-standard-string",
          version="1.0.0"):
    return templates_router.clone_template_into_project(
        template_id,
        templates_router.ProjectCloneRequest(project_id=project_id, version=version),
        tenant=tenant,
    )


def _body(resp):
    return json.loads(resp.body)


# --------------------------------------------------------------------------- #
# ORACLE (read half): a revoked member loses template-instance READ access on
# the very next call -- the gate re-derives from ``tenant.roles`` fresh each
# time (C2-6 contract), so revocation is a same-subject, same-tenant call
# whose CURRENT role claim no longer carries a read-grade role.
# --------------------------------------------------------------------------- #
def test_member_reads_then_is_revoked_and_denied_on_the_next_list_call():
    held = _ctx("tenant-a", roles=("viewer",))
    ok = templates_router.list_templates(tenant=held)
    assert isinstance(ok, dict) and ok["templates"], "must read while membership holds"

    revoked = _ctx("tenant-a", roles=(), subject="member")
    denied = templates_router.list_templates(tenant=revoked)
    assert denied.status_code == 403
    assert _body(denied)["error"]["error_code"] == "FORBIDDEN"


def test_member_reads_then_is_revoked_and_denied_on_the_next_read_template_call():
    held = _ctx("tenant-a", roles=("editor",))
    ok = templates_router.read_template(
        "rooftop-standard-string", version="1.0.0", tenant=held)
    assert ok["template_id"] == "rooftop-standard-string"

    revoked = _ctx("tenant-a", roles=(), subject="member")
    denied = templates_router.read_template(
        "rooftop-standard-string", version="1.0.0", tenant=revoked)
    assert denied.status_code == 403


def test_revoked_read_denial_is_the_same_shape_as_never_having_held_a_role():
    revoked = _ctx("tenant-a", roles=(), subject="member")
    never_held = _ctx("tenant-a", roles=(), subject="stranger")
    revoked_resp = templates_router.list_templates(tenant=revoked)
    never_held_resp = templates_router.list_templates(tenant=never_held)
    assert revoked_resp.status_code == never_held_resp.status_code == 403
    assert _body(revoked_resp) == _body(never_held_resp)


# --------------------------------------------------------------------------- #
# ORACLE (write half): a revoked member loses template-instance WRITE access
# on the very next call -- clone-into-project, write, AND undo all share the
# same write-grade gate, so revocation must close every one of them, not just
# the write route (the C2-8 trap: read re-checks but write/undo stay on a
# weaker pre-matrix dependency that never re-reads membership).
# --------------------------------------------------------------------------- #
def test_member_writes_then_is_revoked_and_denied_on_the_next_write():
    editor = _ctx("tenant-a", roles=("editor",), subject="member")
    clone_id = _body(_clone(editor))["clone_id"]

    revoked = _ctx("tenant-a", roles=(), subject="member")
    resp = templates_router.write_project_clone(
        clone_id,
        templates_router.ProjectCloneWriteRequest(content={"setback_ft": 999}),
        tenant=revoked,
    )
    assert resp.status_code == 403
    assert templates.get_project_clone(clone_id).content["setback_ft"] != 999


def test_member_writes_then_is_revoked_and_denied_on_the_next_undo():
    owner = _ctx("tenant-a", roles=("owner",), subject="member")
    clone_id = _body(_clone(owner))["clone_id"]
    write_resp = templates_router.write_project_clone(
        clone_id,
        templates_router.ProjectCloneWriteRequest(content={"setback_ft": 7}),
        tenant=owner,
    )
    write_id = write_resp["write_id"]

    revoked = _ctx("tenant-a", roles=(), subject="member")
    resp = templates_router.undo_project_clone_write(
        clone_id,
        templates_router.ProjectCloneUndoRequest(write_id=write_id),
        tenant=revoked,
    )
    assert resp.status_code == 403
    assert templates.get_project_clone(clone_id).content["setback_ft"] == 7
    assert write_id not in templates._CLONE_UNDO_LOG


def test_member_clones_then_is_revoked_and_denied_on_the_next_clone_into_project():
    owner = _ctx("tenant-a", roles=("owner",), subject="member")
    _clone(owner, project_id="project-1")

    revoked = _ctx("tenant-a", roles=(), subject="member")
    resp = _clone(revoked, project_id="project-2")
    assert resp.status_code == 403
    assert templates.list_project_clones("tenant-a", "project-2") == []


def test_revoked_write_denial_never_reaches_the_store_mutation_functions(monkeypatch):
    """The write-role gate runs BEFORE ``templates.write_project_clone_content``/
    ``undo_last_write`` are even called -- membership is re-checked strictly
    in front of the mutation, not inside or after it (guards the C2-8 trap
    that the check runs outside the write transaction, letting a
    write-after-revoke slip through)."""
    owner = _ctx("tenant-a", roles=("owner",), subject="member")
    clone_id = _body(_clone(owner))["clone_id"]

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("mutation must not run for a revoked caller")

    monkeypatch.setattr(templates, "write_project_clone_content", _fail_if_called)
    monkeypatch.setattr(templates, "undo_last_write", _fail_if_called)

    revoked = _ctx("tenant-a", roles=(), subject="member")
    write_resp = templates_router.write_project_clone(
        clone_id,
        templates_router.ProjectCloneWriteRequest(content={"setback_ft": 1}),
        tenant=revoked,
    )
    undo_resp = templates_router.undo_project_clone_write(
        clone_id,
        templates_router.ProjectCloneUndoRequest(write_id="whatever"),
        tenant=revoked,
    )
    assert write_resp.status_code == undo_resp.status_code == 403


def test_revoked_write_denial_is_the_same_shape_as_no_role_from_start():
    owner = _ctx("tenant-a", roles=("owner",), subject="member")
    clone_id = _body(_clone(owner))["clone_id"]

    revoked = _ctx("tenant-a", roles=(), subject="member")
    never_held = _ctx("tenant-a", roles=(), subject="stranger")
    revoked_resp = templates_router.write_project_clone(
        clone_id, templates_router.ProjectCloneWriteRequest(content={"setback_ft": 1}),
        tenant=revoked,
    )
    never_held_resp = templates_router.write_project_clone(
        clone_id, templates_router.ProjectCloneWriteRequest(content={"setback_ft": 1}),
        tenant=never_held,
    )
    assert revoked_resp.status_code == never_held_resp.status_code == 403
    assert _body(revoked_resp) == _body(never_held_resp)


# --------------------------------------------------------------------------- #
# ORACLE (cross-tenant half): probes from a REAL second tenant -- its own
# genuine, distinct membership, never an empty/absent tenant -- deny
# SAME-SHAPE as an in-tenant role/revocation denial.
# --------------------------------------------------------------------------- #
def test_real_second_tenant_owner_cannot_write_tenant_as_clone():
    tenant_a_owner = _ctx("tenant-a", roles=("owner",), subject="a-owner")
    clone_id = _body(_clone(tenant_a_owner))["clone_id"]

    # A REAL, distinct tenant -- its own owner, its own membership, never an
    # empty fixture (the C2-8 trap: a trivially-empty "second tenant" would
    # never actually cross a boundary).
    tenant_b_owner = _ctx("tenant-b", roles=("owner",), subject="b-owner")
    resp = templates_router.write_project_clone(
        clone_id,
        templates_router.ProjectCloneWriteRequest(content={"setback_ft": 1}),
        tenant=tenant_b_owner,
    )
    assert resp.status_code == 403
    assert templates.get_project_clone(clone_id).content["setback_ft"] != 1
    assert templates.get_project_clone(clone_id).tenant_id == "tenant-a"


def test_real_second_tenant_owner_cannot_undo_tenant_as_clone():
    tenant_a_owner = _ctx("tenant-a", roles=("owner",), subject="a-owner")
    clone_id = _body(_clone(tenant_a_owner))["clone_id"]
    write_resp = templates_router.write_project_clone(
        clone_id, templates_router.ProjectCloneWriteRequest(content={"setback_ft": 3}),
        tenant=tenant_a_owner,
    )
    write_id = write_resp["write_id"]

    tenant_b_owner = _ctx("tenant-b", roles=("owner",), subject="b-owner")
    resp = templates_router.undo_project_clone_write(
        clone_id,
        templates_router.ProjectCloneUndoRequest(write_id=write_id),
        tenant=tenant_b_owner,
    )
    assert resp.status_code == 403
    assert templates.get_project_clone(clone_id).content["setback_ft"] == 3
    assert write_id not in templates._CLONE_UNDO_LOG


def test_elevated_platform_admin_of_second_tenant_still_cannot_cross_write():
    """A ``platform_admin``/elevated role held for tenant-b confers nothing
    on tenant-a's clone -- the C2-8 trap that a staff/admin role class
    bypasses tenant scoping with no matching denial. ``elevated`` and
    ``platform_admin`` are not in ``_TEMPLATE_WRITE_ROLES`` and never
    substitute for the target tenant's own owner/editor binding."""
    tenant_a_owner = _ctx("tenant-a", roles=("owner",), subject="a-owner")
    clone_id = _body(_clone(tenant_a_owner))["clone_id"]

    tenant_b_admin = _ctx(
        "tenant-b", roles=("platform_admin", "owner"), subject="b-admin", elevated=True)
    resp = templates_router.write_project_clone(
        clone_id, templates_router.ProjectCloneWriteRequest(content={"setback_ft": 1}),
        tenant=tenant_b_admin,
    )
    assert resp.status_code == 403
    assert templates.get_project_clone(clone_id).content["setback_ft"] != 1


def test_cross_tenant_denial_is_the_same_shape_as_revocation_denial():
    """The C2-8 shape-split trap: revocation and cross-tenant probing must be
    INDISTINGUISHABLE to the caller -- byte-identical status and body,
    regardless of which of the two boundary classes actually applies."""
    tenant_a_owner = _ctx("tenant-a", roles=("owner",), subject="a-owner")
    clone_id = _body(_clone(tenant_a_owner))["clone_id"]

    revoked_same_tenant = _ctx("tenant-a", roles=(), subject="a-owner")
    real_second_tenant = _ctx("tenant-b", roles=("owner",), subject="b-owner")

    revoked_resp = templates_router.write_project_clone(
        clone_id, templates_router.ProjectCloneWriteRequest(content={"setback_ft": 1}),
        tenant=revoked_same_tenant,
    )
    cross_tenant_resp = templates_router.write_project_clone(
        clone_id, templates_router.ProjectCloneWriteRequest(content={"setback_ft": 1}),
        tenant=real_second_tenant,
    )
    assert revoked_resp.status_code == cross_tenant_resp.status_code == 403
    assert _body(revoked_resp) == _body(cross_tenant_resp)


def test_cross_tenant_probe_cannot_distinguish_unknown_clone_from_revoked_own_clone():
    """A real second tenant's unknown-clone probe and a revoked caller's own
    (now-inaccessible) clone_id must be byte-identical denials -- neither
    leaks whether the clone exists at all."""
    tenant_a_owner = _ctx("tenant-a", roles=("owner",), subject="a-owner")
    clone_id = _body(_clone(tenant_a_owner))["clone_id"]

    revoked_own = _ctx("tenant-a", roles=(), subject="a-owner")
    real_second_tenant = _ctx("tenant-b", roles=("owner",), subject="b-owner")

    revoked_resp = templates_router.write_project_clone(
        clone_id, templates_router.ProjectCloneWriteRequest(content={"setback_ft": 1}),
        tenant=revoked_own,
    )
    unknown_resp = templates_router.write_project_clone(
        "never-existed-at-all",
        templates_router.ProjectCloneWriteRequest(content={"setback_ft": 1}),
        tenant=real_second_tenant,
    )
    assert revoked_resp.status_code == unknown_resp.status_code == 403
    assert _body(revoked_resp) == _body(unknown_resp)


def test_second_tenant_id_is_a_genuinely_distinct_claim_not_a_normalization_collision():
    """Guards the C2-8 trap of a "second tenant" that only APPEARS distinct
    (case/UUID-vs-slug variance folding back to the same claim). tenant-a and
    tenant-b must resolve to different echoed identities and different clone
    ownership -- a real, non-colliding boundary."""
    tenant_a_owner = _ctx("tenant-a", roles=("owner",), subject="a-owner")
    tenant_b_owner = _ctx("tenant-b", roles=("owner",), subject="b-owner")
    assert str(tenant_a_owner) != str(tenant_b_owner)

    clone_a = _body(_clone(tenant_a_owner, project_id="shared-project-id"))
    clone_b = _body(_clone(tenant_b_owner, project_id="shared-project-id"))
    assert clone_a["clone_id"] != clone_b["clone_id"]
    assert clone_a["tenant_id"] == "tenant-a"
    assert clone_b["tenant_id"] == "tenant-b"
    assert templates.list_project_clones("tenant-a", "shared-project-id") == [
        templates.get_project_clone(clone_a["clone_id"])
    ]


# --------------------------------------------------------------------------- #
# RED-TEAM PROOF: an adversary who WAS a legitimate owner, now revoked, tries
# every write-grade route against their former clone in one sweep -- every
# single one must fail closed, same shape.
# --------------------------------------------------------------------------- #
def test_redteam_fully_revoked_former_owner_denied_on_every_write_route():
    owner = _ctx("tenant-a", roles=("owner",), subject="ex-owner")
    clone_id = _body(_clone(owner))["clone_id"]
    write_resp = templates_router.write_project_clone(
        clone_id, templates_router.ProjectCloneWriteRequest(content={"setback_ft": 5}),
        tenant=owner,
    )
    write_id = write_resp["write_id"]

    revoked = _ctx("tenant-a", roles=(), subject="ex-owner")
    denials = [
        _clone(revoked, project_id="another-project"),
        templates_router.write_project_clone(
            clone_id, templates_router.ProjectCloneWriteRequest(content={"setback_ft": 6}),
            tenant=revoked,
        ),
        templates_router.undo_project_clone_write(
            clone_id, templates_router.ProjectCloneUndoRequest(write_id=write_id),
            tenant=revoked,
        ),
    ]
    for resp in denials:
        assert resp.status_code == 403
    bodies = [_body(resp) for resp in denials]
    assert bodies[0] == bodies[1] == bodies[2]
    # nothing mutated by any of the three denied attempts
    assert templates.get_project_clone(clone_id).content["setback_ft"] == 5


# --------------------------------------------------------------------------- #
# Guard: the gate takes only the freshly-resolved ``tenant`` argument -- no
# signature that could admit a cached lookup key (clone_id, subject, or any
# other memoized identity), which would let a TTL/process cache silently
# re-admit a revoked caller (C2-8 trap: membership cached with a TTL).
# --------------------------------------------------------------------------- #
def test_write_role_gate_signature_takes_only_tenant_no_cache_key():
    sig = inspect.signature(templates_router._write_role)
    assert list(sig.parameters) == ["tenant"]


def test_read_role_gate_signature_takes_only_tenant_no_cache_key():
    sig = inspect.signature(templates_router._authorized_for_read)
    assert list(sig.parameters) == ["tenant"]
