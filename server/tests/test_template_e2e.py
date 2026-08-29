"""E2E acceptance test for card C2-9: solar_template_beta clone -> one write
-> undo -> role checks.

Acceptance oracle:
    E2E: clone -> one write -> undo -> role checks, disposable-task shape.

The "one write" is C2-4's CAS PATCH (``templates.update_project_clone_content``
/ ``routers.templates.update_project_clone_content``, the
``/api/templates/clones/{clone_id}`` route), never the legacy POST
``/api/project-clones/{clone_id}/writes`` (C2-7) -- driving the legacy route
never exercises C2-4's CAS surface at all.

C2-5's dedicated undo route (``POST .../undo``) reverts a write recorded in
the LEGACY write log, which ``update_project_clone_content`` never appends
to -- a PATCH write has no ``write_id`` for that route to act on. "Undo" of a
CAS write is therefore itself a CAS write: a second PATCH restoring the
pre-write content, submitted with the just-bumped ``content_version`` as
``expected_content_version``. This exercises the exact same CAS surface the
write did, and per C2-4's merged contract the undo bumps (never resets) the
``content_version`` lineage -- proven here by a THIRD PATCH that reuses the
now-superseded write-time version and must 409, not silently apply.

``server/app.py`` mounts ``routers.templates`` so the same fail-closed handlers
are reachable in the deployed application. The lifecycle checks below still
drive the handlers directly to keep their state and authority assertions
focused.

Run:  cd server && python -m pytest tests/test_template_e2e.py -q
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

import app as application  # noqa: E402
import deps  # noqa: E402
import templates  # noqa: E402
from customization_authority import TenantBinding  # noqa: E402
from routers import templates as templates_router  # noqa: E402
from route_flatten import leaf_paths  # noqa: E402

TEMPLATE_ID = "rooftop-standard-string"
TENANT = "tenant-e2e"
PROJECT = "project-e2e"


def test_application_mounts_every_solar_template_route():
    mounted = set(leaf_paths(application.app))
    expected = set(leaf_paths(templates_router.router))
    assert expected
    assert expected <= mounted


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Every test starts and ends with an empty clone/write/undo store, flag
    on -- matching the sibling test files' fixture convention."""
    monkeypatch.setenv(templates.FLAG_SOLAR_TEMPLATE_BETA, "1")
    monkeypatch.setattr(templates, "_PROJECT_CLONES", {})
    monkeypatch.setattr(templates, "_CLONE_WRITE_LOG", {})
    monkeypatch.setattr(templates, "_CLONE_UNDO_LOG", {})
    yield


def _ctx(role: str) -> deps.TenantContext:
    roles = () if role == "" else (role,)
    return deps.TenantContext(TENANT, subject="author", roles=roles)


def _resp_json(resp):
    """Router handlers return either a plain envelope dict (success) or a
    JSONResponse (error) -- normalize both to (status_code, body)."""
    if isinstance(resp, dict):
        return 200, resp
    return resp.status_code, json.loads(resp.body)


def _clone_as_owner():
    status, body = _resp_json(templates_router.clone_template_into_project(
        TEMPLATE_ID,
        templates_router.ProjectCloneRequest(project_id=PROJECT),
        tenant=_ctx("owner"),
    ))
    assert status == 201
    return body


# --------------------------------------------------------------------------- #
# ORACLE: clone -> one write (CAS) -> undo, driven through C2-4's PATCH route
# --------------------------------------------------------------------------- #
def test_clone_then_one_cas_write_then_undo_round_trips_content_and_bumps_cas():
    clone_body = _clone_as_owner()
    clone_id = clone_body["clone_id"]
    pre_write_content = clone_body["content"]
    pre_write_version = templates.get_project_clone(clone_id).content_version
    assert pre_write_version == 1

    # the ONE write -- C2-4's CAS PATCH, not the legacy POST .../writes route
    write_status, write_body = _resp_json(templates_router.update_project_clone_content(
        clone_id,
        templates_router.UpdateProjectCloneContentRequest(
            content={**pre_write_content, "setback_ft": 999},
            expected_content_version=pre_write_version,
        ),
        tenant=_ctx("owner"),
    ))
    assert write_status == 200
    assert write_body["content_version"] == pre_write_version + 1
    assert write_body["content"]["setback_ft"] == 999
    stored_after_write = templates.get_project_clone(clone_id)
    assert stored_after_write.content_version == pre_write_version + 1
    assert stored_after_write.content == write_body["content"]

    # undo -- a second CAS PATCH restoring the pre-write content, submitted
    # with the write's bumped version as the new expected_content_version
    undo_status, undo_body = _resp_json(templates_router.update_project_clone_content(
        clone_id,
        templates_router.UpdateProjectCloneContentRequest(
            content=pre_write_content,
            expected_content_version=write_body["content_version"],
        ),
        tenant=_ctx("owner"),
    ))
    assert undo_status == 200
    # CAS lineage advances on undo too -- never resets to the write's version
    # or back to 1 (C2-4's merged contract: undo is itself a mutation).
    assert undo_body["content_version"] == write_body["content_version"] + 1
    assert undo_body["content"] == pre_write_content
    stored_after_undo = templates.get_project_clone(clone_id)
    assert stored_after_undo.content_version == write_body["content_version"] + 1
    assert stored_after_undo.content == pre_write_content


def test_stale_patch_reusing_the_write_time_version_409s_after_undo_and_lands_nothing():
    clone_body = _clone_as_owner()
    clone_id = clone_body["clone_id"]
    pre_write_content = clone_body["content"]
    pre_write_version = templates.get_project_clone(clone_id).content_version

    _, write_body = _resp_json(templates_router.update_project_clone_content(
        clone_id,
        templates_router.UpdateProjectCloneContentRequest(
            content={**pre_write_content, "setback_ft": 999},
            expected_content_version=pre_write_version,
        ),
        tenant=_ctx("owner"),
    ))
    write_version = write_body["content_version"]

    undo_status, undo_body = _resp_json(templates_router.update_project_clone_content(
        clone_id,
        templates_router.UpdateProjectCloneContentRequest(
            content=pre_write_content, expected_content_version=write_version,
        ),
        tenant=_ctx("owner"),
    ))
    assert undo_status == 200
    undo_version = undo_body["content_version"]

    # a stale PATCH that reuses the write-time version -- must 409, not
    # silently apply, and must leave the store exactly as the undo left it
    stale_status, stale_body = _resp_json(templates_router.update_project_clone_content(
        clone_id,
        templates_router.UpdateProjectCloneContentRequest(
            content={**pre_write_content, "setback_ft": 111},
            expected_content_version=write_version,
        ),
        tenant=_ctx("owner"),
    ))
    assert stale_status == 409
    assert stale_body["error"]["error_code"] == "BAD_PARAMS"
    stored = templates.get_project_clone(clone_id)
    assert stored.content_version == undo_version
    assert stored.content == pre_write_content


# --------------------------------------------------------------------------- #
# ORACLE: role checks -- clone-into-project / CAS write / CAS-undo share the
# SAME write-grade (editor/owner) gate; viewer and no-role are denied,
# same-shape, on every one of them.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", ["owner", "editor"])
def test_write_role_can_clone_cas_write_and_cas_undo(role):
    clone = templates.clone_template_for_project(
        TEMPLATE_ID, tenant_id=TENANT, project_id=PROJECT, version="1.0.0",
        binding=TenantBinding(tenant_id=TENANT, subject="author", role="owner", verified=True),
    )
    clone_status, clone_body = _resp_json(templates_router.clone_template_into_project(
        TEMPLATE_ID, templates_router.ProjectCloneRequest(project_id=PROJECT), tenant=_ctx(role),
    ))
    assert clone_status == 201

    write_status, write_body = _resp_json(templates_router.update_project_clone_content(
        clone.clone_id,
        templates_router.UpdateProjectCloneContentRequest(
            content={**clone.content, "setback_ft": 5}, expected_content_version=1,
        ),
        tenant=_ctx(role),
    ))
    assert write_status == 200

    undo_status, undo_body = _resp_json(templates_router.update_project_clone_content(
        clone.clone_id,
        templates_router.UpdateProjectCloneContentRequest(
            content=clone.content, expected_content_version=write_body["content_version"],
        ),
        tenant=_ctx(role),
    ))
    assert undo_status == 200
    assert undo_body["content"] == clone.content


@pytest.mark.parametrize("role", ["viewer", ""])
def test_non_write_role_is_denied_clone_cas_write_and_cas_undo_same_shape(role):
    clone = templates.clone_template_for_project(
        TEMPLATE_ID, tenant_id=TENANT, project_id=PROJECT, version="1.0.0",
        binding=TenantBinding(tenant_id=TENANT, subject="author", role="owner", verified=True),
    )

    clone_resp = templates_router.clone_template_into_project(
        TEMPLATE_ID, templates_router.ProjectCloneRequest(project_id=PROJECT), tenant=_ctx(role),
    )
    assert clone_resp.status_code == 403
    assert json.loads(clone_resp.body)["error"]["error_code"] == "FORBIDDEN"

    write_req = templates_router.UpdateProjectCloneContentRequest(
        content={**clone.content, "setback_ft": 5}, expected_content_version=1,
    )
    write_resp = templates_router.update_project_clone_content(
        clone.clone_id, write_req, tenant=_ctx(role),
    )
    assert write_resp.status_code == 403
    assert json.loads(write_resp.body)["error"]["error_code"] == "FORBIDDEN"

    # "undo" (a second PATCH) is denied the same way -- the gate runs before
    # any CAS check, so a denied caller never even learns the true version.
    undo_resp = templates_router.update_project_clone_content(
        clone.clone_id,
        templates_router.UpdateProjectCloneContentRequest(
            content=clone.content, expected_content_version=1,
        ),
        tenant=_ctx(role),
    )
    assert undo_resp.status_code == 403
    assert json.loads(undo_resp.body)["error"]["error_code"] == "FORBIDDEN"

    # nothing landed for any of the three denied attempts
    assert templates.get_project_clone(clone.clone_id).content_version == 1
    assert templates.get_project_clone(clone.clone_id).content == clone.content
