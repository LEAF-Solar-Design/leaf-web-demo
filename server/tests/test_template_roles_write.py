"""Acceptance tests for card C2-7: role matrix, clone + write authority.

Acceptance oracle:
    Only editor/owner of the TARGET project may clone into it; only they may
    write/undo on the instance; reviewer/read-only denied same-shape.
    RED-TEAM PROOF REQUIRED (A2 executing reviewer).

Offline, in-process (direct function calls against ``routers.templates``),
mirroring ``tests/test_template_roles_read.py``'s sys.path setup, fixture
convention, and calling style.

Run:  cd server && python -m pytest tests/test_template_roles_write.py -q
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
from fastapi.responses import JSONResponse  # noqa: E402

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


def _ctx(tenant_id: str, roles: tuple = (), subject: str = "author") -> deps.TenantContext:
    return deps.TenantContext(tenant_id, subject=subject, roles=roles)


def _clone_body(tenant, project_id="project-1", template_id="rooftop-standard-string",
                version="1.0.0"):
    return templates_router.clone_template_into_project(
        template_id,
        templates_router.ProjectCloneRequest(project_id=project_id, version=version),
        tenant=tenant,
    )


def _body(resp):
    return json.loads(resp.body)


# --------------------------------------------------------------------------- #
# ORACLE: only editor/owner of the TARGET (caller's own) tenant may clone
# into it
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", ["editor", "owner"])
def test_editor_and_owner_can_clone_into_their_own_project(role):
    tenant = _ctx("tenant-a", roles=(role,))
    resp = _clone_body(tenant)
    assert resp.status_code == 201
    body = _body(resp)
    assert body["tenant_id"] == "tenant-a"
    assert body["project_id"] == "project-1"
    assert body["template_id"] == "rooftop-standard-string"
    assert body["clone_id"]


@pytest.mark.parametrize("role", ["reviewer", "read_only", "viewer", "builder", "unknown"])
def test_reviewer_and_read_only_and_other_non_write_roles_are_denied_cloning(role):
    tenant = _ctx("tenant-a", roles=(role,))
    resp = _clone_body(tenant)
    assert resp.status_code == 403
    assert _body(resp)["error"]["error_code"] == "FORBIDDEN"
    assert templates.list_project_clones("tenant-a", "project-1") == []


def test_no_role_at_all_is_denied_cloning():
    tenant = _ctx("tenant-a", roles=())
    resp = _clone_body(tenant)
    assert resp.status_code == 403
    assert templates.list_project_clones("tenant-a", "project-1") == []


def test_denial_is_the_same_shape_regardless_of_role_held():
    reviewer_body = _body(_clone_body(_ctx("tenant-a", roles=("reviewer",))))
    no_role_body = _body(_clone_body(_ctx("tenant-a", roles=())))
    unknown_body = _body(_clone_body(_ctx("tenant-a", roles=("some_made_up_role",))))
    assert reviewer_body == no_role_body == unknown_body


def test_substring_collision_role_is_rejected_not_matched():
    """'reviewer' ends with 'viewer' -- a substring check would wrongly admit
    it against a 'viewer'-shaped allow list. Exact-set membership must
    reject it regardless."""
    resp = _clone_body(_ctx("tenant-a", roles=("reviewer",)))
    assert resp.status_code == 403


def test_case_variant_write_role_is_normalized_not_defaulted_to_allow_or_deny():
    resp = _clone_body(_ctx("tenant-a", roles=("Owner",)))
    assert resp.status_code == 201


# --------------------------------------------------------------------------- #
# ORACLE: a caller can never clone into a DIFFERENT tenant's project -- the
# clone request body carries only project_id, never tenant_id; the landed
# tenant is always the caller's own verified claim.
# --------------------------------------------------------------------------- #
def test_clone_never_lands_under_a_caller_supplied_tenant():
    """The router's clone request model has no tenant_id field at all -- a
    caller cannot even ATTEMPT to redirect the landing tenant."""
    assert "tenant_id" not in templates_router.ProjectCloneRequest.model_fields


def test_clone_always_lands_under_the_callers_own_verified_tenant():
    owner_a = _ctx("tenant-a", roles=("owner",))
    resp = _clone_body(owner_a)
    body = _body(resp)
    assert body["tenant_id"] == "tenant-a"
    assert templates.list_project_clones("tenant-b", "project-1") == []


# --------------------------------------------------------------------------- #
# ORACLE: role gate runs before any registry lookup -- an unauthorized
# caller cannot use a template/version probe to learn what exists.
# --------------------------------------------------------------------------- #
def test_denial_is_identical_shape_for_real_and_unknown_template_or_version():
    denied = _ctx("tenant-a", roles=())
    real = _body(_clone_body(denied, template_id="rooftop-standard-string", version="1.0.0"))
    unknown_template = _body(_clone_body(denied, template_id="does-not-exist"))
    unknown_version = _body(_clone_body(
        denied, template_id="rooftop-standard-string", version="9.9.9"))
    assert real == unknown_template == unknown_version


def test_flag_off_denial_wins_over_role_denial_and_stays_404(monkeypatch):
    monkeypatch.delenv(templates.FLAG_SOLAR_TEMPLATE_BETA, raising=False)
    no_role = _clone_body(_ctx("tenant-a", roles=()))
    owner = _clone_body(_ctx("tenant-a", roles=("owner",)))
    assert no_role.status_code == owner.status_code == 404
    assert _body(no_role) == _body(owner)


# --------------------------------------------------------------------------- #
# ORACLE: only editor/owner may write on the instance; reviewer/read-only
# denied same-shape.
# --------------------------------------------------------------------------- #
def test_editor_and_owner_can_write_a_clone_they_own():
    for role in ("owner", "editor"):
        clone_body = _body(_clone_body(
            _ctx("tenant-a", roles=("owner",)), project_id=f"project-{role}"))
        clone_id = clone_body["clone_id"]
        resp = templates_router.write_project_clone(
            clone_id,
            templates_router.ProjectCloneWriteRequest(content={"setback_ft": 5}),
            tenant=_ctx("tenant-a", roles=(role,)),
        )
        assert isinstance(resp, dict), "an authorized write must not be denied"
        assert resp["clone_id"] == clone_id
        assert resp["write_id"]


@pytest.mark.parametrize("role", ["reviewer", "read_only", "viewer", "builder", "unknown"])
def test_reviewer_and_read_only_denied_write_same_shape_as_no_role(role):
    clone_id = _body(_clone_body(_ctx("tenant-a", roles=("owner",))))["clone_id"]
    denied_resp = templates_router.write_project_clone(
        clone_id,
        templates_router.ProjectCloneWriteRequest(content={"setback_ft": 999}),
        tenant=_ctx("tenant-a", roles=(role,)),
    )
    no_role_resp = templates_router.write_project_clone(
        clone_id,
        templates_router.ProjectCloneWriteRequest(content={"setback_ft": 999}),
        tenant=_ctx("tenant-a", roles=()),
    )
    assert denied_resp.status_code == no_role_resp.status_code == 403
    assert _body(denied_resp) == _body(no_role_resp)
    # nothing mutated
    assert templates.get_project_clone(clone_id).content["setback_ft"] != 999


# --------------------------------------------------------------------------- #
# RED-TEAM PROOF: an authenticated reviewer (A2's adversary persona) probes
# raw HTTP-shaped calls directly -- write and undo must fail closed with no
# distinguishable signal between "role denied" and "clone_id belongs to a
# different tenant" vs "clone_id never existed".
# --------------------------------------------------------------------------- #
def test_redteam_reviewer_write_denied_and_undo_denied_same_shape_as_role_denial():
    owner_clone_id = _body(_clone_body(_ctx("tenant-a", roles=("owner",))))["clone_id"]
    reviewer = _ctx("tenant-a", roles=("reviewer",), subject="adversary")

    write_resp = templates_router.write_project_clone(
        owner_clone_id,
        templates_router.ProjectCloneWriteRequest(content={"setback_ft": 1}),
        tenant=reviewer,
    )
    undo_resp = templates_router.undo_project_clone_write(
        owner_clone_id,
        templates_router.ProjectCloneUndoRequest(write_id="whatever"),
        tenant=reviewer,
    )
    assert write_resp.status_code == undo_resp.status_code == 403
    assert _body(write_resp) == _body(undo_resp)


def test_redteam_reviewer_cannot_distinguish_unknown_clone_from_foreign_tenant_clone():
    """The C2-7 404-vs-403 enumeration trap: a reviewer probing an unknown
    clone_id and a real clone_id that belongs to a DIFFERENT tenant must get
    byte-identical denials -- neither leaks which is true."""
    foreign_clone_id = _body(_clone_body(_ctx("tenant-b", roles=("owner",))))["clone_id"]
    reviewer_own_tenant = _ctx("tenant-a", roles=("reviewer",), subject="adversary")

    unknown_resp = templates_router.write_project_clone(
        "does-not-exist-at-all",
        templates_router.ProjectCloneWriteRequest(content={"setback_ft": 1}),
        tenant=reviewer_own_tenant,
    )
    foreign_resp = templates_router.write_project_clone(
        foreign_clone_id,
        templates_router.ProjectCloneWriteRequest(content={"setback_ft": 1}),
        tenant=reviewer_own_tenant,
    )
    assert unknown_resp.status_code == foreign_resp.status_code == 403
    assert _body(unknown_resp) == _body(foreign_resp)


def test_redteam_owner_cannot_distinguish_own_unknown_clone_from_foreign_tenant_clone():
    """Even an AUTHORIZED (owner) caller gets the identical denial for their
    own never-existed clone_id and a real foreign-tenant clone_id -- the
    collapse is unconditional on role, not just applied to deniable roles."""
    foreign_clone_id = _body(_clone_body(_ctx("tenant-b", roles=("owner",))))["clone_id"]
    owner_a = _ctx("tenant-a", roles=("owner",))

    unknown_resp = templates_router.write_project_clone(
        "never-existed",
        templates_router.ProjectCloneWriteRequest(content={"setback_ft": 1}),
        tenant=owner_a,
    )
    foreign_resp = templates_router.write_project_clone(
        foreign_clone_id,
        templates_router.ProjectCloneWriteRequest(content={"setback_ft": 1}),
        tenant=owner_a,
    )
    assert unknown_resp.status_code == foreign_resp.status_code == 403
    assert _body(unknown_resp) == _body(foreign_resp)
    assert templates.get_project_clone(foreign_clone_id).tenant_id == "tenant-b"


# --------------------------------------------------------------------------- #
# ORACLE: undo shares the SAME write-grade gate as write -- never a weaker
# read-grade "any authenticated" dependency.
# --------------------------------------------------------------------------- #
def test_undo_allows_editor_and_owner_who_own_the_clone():
    for role in ("owner", "editor"):
        clone_id = _body(_clone_body(
            _ctx("tenant-a", roles=("owner",)), project_id=f"undo-{role}"))["clone_id"]
        write_resp = templates_router.write_project_clone(
            clone_id,
            templates_router.ProjectCloneWriteRequest(content={"setback_ft": 42}),
            tenant=_ctx("tenant-a", roles=(role,)),
        )
        write_id = write_resp["write_id"] if isinstance(write_resp, dict) else _body(write_resp)["write_id"]
        undo_resp = templates_router.undo_project_clone_write(
            clone_id,
            templates_router.ProjectCloneUndoRequest(write_id=write_id),
            tenant=_ctx("tenant-a", roles=(role,)),
        )
        body = undo_resp if isinstance(undo_resp, dict) else _body(undo_resp)
        assert body["undone"] is True
        assert body["already_undone"] is False


@pytest.mark.parametrize("role", ["reviewer", "read_only", "viewer", "builder", "unknown"])
def test_undo_denies_reviewer_and_read_only_and_mutates_nothing(role):
    clone_id = _body(_clone_body(_ctx("tenant-a", roles=("owner",))))["clone_id"]
    write_resp = templates_router.write_project_clone(
        clone_id,
        templates_router.ProjectCloneWriteRequest(content={"setback_ft": 77}),
        tenant=_ctx("tenant-a", roles=("owner",)),
    )
    write_id = write_resp["write_id"] if isinstance(write_resp, dict) else _body(write_resp)["write_id"]

    resp = templates_router.undo_project_clone_write(
        clone_id,
        templates_router.ProjectCloneUndoRequest(write_id=write_id),
        tenant=_ctx("tenant-a", roles=(role,)),
    )
    assert resp.status_code == 403
    assert templates.get_project_clone(clone_id).content["setback_ft"] == 77
    assert write_id not in templates._CLONE_UNDO_LOG


def test_undo_denied_with_no_write_role_never_reaches_the_write_history_lookup(monkeypatch):
    """The write-role gate runs BEFORE templates.undo_last_write is even
    called -- a denied caller cannot use a stale-vs-unknown write_id error to
    learn anything about the clone's write history."""
    clone_id = _body(_clone_body(_ctx("tenant-a", roles=("owner",))))["clone_id"]

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("undo_last_write must not run for a denied role")

    monkeypatch.setattr(templates, "undo_last_write", _fail_if_called)

    resp = templates_router.undo_project_clone_write(
        clone_id,
        templates_router.ProjectCloneUndoRequest(write_id="whatever"),
        tenant=_ctx("tenant-a", roles=("reviewer",)),
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# ORACLE: role is re-checked per request, never snapshotted at clone time --
# a mid-session editor -> reviewer downgrade is honored on the next write.
# --------------------------------------------------------------------------- #
def test_role_downgrade_after_clone_is_honored_on_the_next_write():
    clone_id = _body(_clone_body(_ctx("tenant-a", roles=("editor",))))["clone_id"]
    downgraded = _ctx("tenant-a", roles=("reviewer",), subject="author")
    resp = templates_router.write_project_clone(
        clone_id,
        templates_router.ProjectCloneWriteRequest(content={"setback_ft": 1}),
        tenant=downgraded,
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Flag fence: solar_template_beta gates write/undo too, before any mutation.
# --------------------------------------------------------------------------- #
def test_write_refuses_when_flag_is_off(monkeypatch):
    clone_id = _body(_clone_body(_ctx("tenant-a", roles=("owner",))))["clone_id"]
    monkeypatch.delenv(templates.FLAG_SOLAR_TEMPLATE_BETA, raising=False)
    resp = templates_router.write_project_clone(
        clone_id,
        templates_router.ProjectCloneWriteRequest(content={"setback_ft": 1}),
        tenant=_ctx("tenant-a", roles=("owner",)),
    )
    assert resp.status_code == 404


def test_undo_refuses_when_flag_is_off(monkeypatch):
    clone_id = _body(_clone_body(_ctx("tenant-a", roles=("owner",))))["clone_id"]
    write_resp = templates_router.write_project_clone(
        clone_id,
        templates_router.ProjectCloneWriteRequest(content={"setback_ft": 1}),
        tenant=_ctx("tenant-a", roles=("owner",)),
    )
    write_id = write_resp["write_id"] if isinstance(write_resp, dict) else _body(write_resp)["write_id"]
    monkeypatch.delenv(templates.FLAG_SOLAR_TEMPLATE_BETA, raising=False)
    resp = templates_router.undo_project_clone_write(
        clone_id,
        templates_router.ProjectCloneUndoRequest(write_id=write_id),
        tenant=_ctx("tenant-a", roles=("owner",)),
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# legacy / auth-off byte-identical path (regression guard, not the oracle)
# --------------------------------------------------------------------------- #
def test_legacy_plain_str_tenant_can_still_clone_write_and_undo():
    """Pre-auth demo mode (LEAF_AUTH_LIVE off, tenant is a plain str) has no
    role claims at all -- byte-identical to the pre-C2-7 exemption C2-6
    already established for reads."""
    clone_resp = templates_router.clone_template_into_project(
        "rooftop-standard-string",
        templates_router.ProjectCloneRequest(project_id="legacy-project", version="1.0.0"),
        tenant="demo-tenant",
    )
    assert clone_resp.status_code == 201
    clone_id = _body(clone_resp)["clone_id"]

    write_resp = templates_router.write_project_clone(
        clone_id,
        templates_router.ProjectCloneWriteRequest(content={"setback_ft": 1}),
        tenant="demo-tenant",
    )
    write_id = write_resp["write_id"] if isinstance(write_resp, dict) else _body(write_resp)["write_id"]

    undo_resp = templates_router.undo_project_clone_write(
        clone_id,
        templates_router.ProjectCloneUndoRequest(write_id=write_id),
        tenant="demo-tenant",
    )
    body = undo_resp if isinstance(undo_resp, dict) else _body(undo_resp)
    assert body["undone"] is True
