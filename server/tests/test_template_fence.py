"""Standalone flip-time fence proof for card C2-9: ``solar_template_beta`` OFF
makes EVERY route on ``routers.templates.router`` refuse -- checked in one
process, flag flipped live, never a value cached at import/startup.

Acceptance oracle:
    Negative control: solar_template_beta OFF makes every endpoint refuse
    (standalone flip-time fence proof).

Distinct from the per-card flag-off tests scattered across
``test_template_roles_read.py``/``test_template_roles_write.py``/
``test_template_write.py``/``test_template_undo.py`` (each proves flag-off
for ITS OWN route): this file is the single place that (a) enumerates every
route the router actually registers, so a new route added later without a
flag check fails this file on sight, and (b) proves the refusal is a LIVE
per-call check -- flag ON succeeds, flag OFF (same process, same call)
refuses, flag back ON succeeds again -- never a value latched once at
process start.

Offline, in-process (direct function calls against the real ``templates``
module and ``routers.templates``), mirroring ``tests/test_template_write.py``'s
sys.path setup and calling convention.

Run:  cd server && python -m pytest tests/test_template_fence.py -q
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

import templates  # noqa: E402
from customization_authority import TenantBinding  # noqa: E402
from routers import templates as templates_router  # noqa: E402

TENANT = "tenant-fence"
PROJECT = "project-fence"
TEMPLATE_ID = "rooftop-standard-string"


def _owner_binding(tenant_id: str = TENANT) -> TenantBinding:
    return TenantBinding(tenant_id=tenant_id, subject="author", role="owner", verified=True)


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Every test starts and ends with an empty clone/write/undo store, flag
    on -- matching the sibling test files' fixture convention."""
    monkeypatch.setenv(templates.FLAG_SOLAR_TEMPLATE_BETA, "1")
    monkeypatch.setattr(templates, "_PROJECT_CLONES", {})
    monkeypatch.setattr(templates, "_CLONE_WRITE_LOG", {})
    monkeypatch.setattr(templates, "_CLONE_UNDO_LOG", {})
    yield


def _seed_clone_and_write():
    """Seed a project clone with one write on it while the flag is ON, so the
    mutating routes below have a real ``clone_id``/``write_id`` to target."""
    clone = templates.clone_template_for_project(
        TEMPLATE_ID, tenant_id=TENANT, project_id=PROJECT, version="1.0.0",
        binding=_owner_binding(),
    )
    write = templates.write_project_clone_content(
        clone.clone_id, {**clone.content, "setback_ft": 7}, binding=_owner_binding(),
    )
    return clone, write


def _call(name: str, clone_id: str, write_id: str):
    """One (route, live invocation) pair per handler registered on
    ``templates_router.router``. Every call uses a caller who WOULD be
    authorized (owner role, own tenant, real ids) -- so a refusal below can
    only be attributed to the flag, never to a role/lookup denial that would
    also produce a 404/403 and mask a broken fence."""
    tenant = TENANT
    if name == "list_templates":
        return templates_router.list_templates(tenant=tenant)
    if name == "read_template":
        return templates_router.read_template(TEMPLATE_ID, tenant=tenant)
    if name == "clone_template":
        return templates_router.clone_template(
            TEMPLATE_ID, templates_router.CloneTemplateRequest(), tenant=tenant,
        )
    if name == "clone_template_into_project":
        return templates_router.clone_template_into_project(
            TEMPLATE_ID,
            templates_router.ProjectCloneRequest(project_id=PROJECT),
            tenant=tenant,
        )
    if name == "write_project_clone":
        return templates_router.write_project_clone(
            clone_id,
            templates_router.ProjectCloneWriteRequest(content={"a": 1}),
            tenant=tenant,
        )
    if name == "undo_project_clone_write":
        return templates_router.undo_project_clone_write(
            clone_id,
            templates_router.ProjectCloneUndoRequest(write_id=write_id),
            tenant=tenant,
        )
    if name == "update_project_clone_content":
        return templates_router.update_project_clone_content(
            clone_id,
            templates_router.UpdateProjectCloneContentRequest(
                content={"a": 1}, expected_content_version=2,
            ),
            tenant=tenant,
        )
    raise AssertionError(f"no invocation wired for handler {name!r}")


# --------------------------------------------------------------------------- #
# ORACLE (enumeration): every route the router actually registers is covered
# by ``_call`` above -- a NEW route lands here with zero fence proof, not
# silently green.
# --------------------------------------------------------------------------- #
def test_every_registered_route_has_a_wired_fence_invocation():
    handler_names = sorted(route.name for route in templates_router.router.routes)
    assert handler_names == sorted([
        "clone_template",
        "clone_template_into_project",
        "list_templates",
        "read_template",
        "undo_project_clone_write",
        "update_project_clone_content",
        "write_project_clone",
    ])


# --------------------------------------------------------------------------- #
# ORACLE: solar_template_beta OFF makes every endpoint refuse -- SAME
# flag-off shape (404, BAD_PARAMS, "solar template beta is not enabled") on
# every single one, and nothing lands.
# --------------------------------------------------------------------------- #
def test_every_endpoint_refuses_with_the_identical_flag_off_shape(monkeypatch):
    clone, write = _seed_clone_and_write()
    pre_clones = dict(templates._PROJECT_CLONES)
    pre_writes = {k: list(v) for k, v in templates._CLONE_WRITE_LOG.items()}
    pre_undos = dict(templates._CLONE_UNDO_LOG)

    monkeypatch.delenv(templates.FLAG_SOLAR_TEMPLATE_BETA, raising=False)

    handler_names = [route.name for route in templates_router.router.routes]
    assert handler_names, "router registered zero routes -- nothing to fence"

    reference_body = None
    for name in handler_names:
        resp = _call(name, clone.clone_id, write.write_id)
        assert resp.status_code == 404, f"{name} did not refuse with 404 when the flag is off"
        body = json.loads(resp.body)
        assert body["error"]["error_code"] == "BAD_PARAMS", (
            f"{name} refused with the wrong error code: {body}"
        )
        assert body["error"]["message"] == "solar template beta is not enabled", (
            f"{name} refused with a different message than the shared flag-off shape: {body}"
        )
        if reference_body is None:
            reference_body = body
        else:
            assert body == reference_body, (
                f"{name}'s flag-off body diverges from the shared shape -- every "
                "endpoint must refuse identically"
            )

    # nothing landed anywhere -- refusal happened before any lookup or mutation
    assert templates._PROJECT_CLONES == pre_clones
    assert {k: list(v) for k, v in templates._CLONE_WRITE_LOG.items()} == pre_writes
    assert templates._CLONE_UNDO_LOG == pre_undos


# --------------------------------------------------------------------------- #
# ORACLE (flip-time): the fence is a LIVE per-call check, not a value cached
# once at process/import start -- ON -> OFF -> ON, in the same process,
# same call, must actually track the flag each time.
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("name", [
    "list_templates",
    "read_template",
    "clone_template",
    "clone_template_into_project",
    "write_project_clone",
    "undo_project_clone_write",
    "update_project_clone_content",
])
def test_flag_flip_is_observed_live_within_a_single_process(name, monkeypatch):
    clone, write = _seed_clone_and_write()

    # ON: an authorized, well-formed call succeeds (never a flag-off shape)
    on_resp = _call(name, clone.clone_id, write.write_id)
    on_status = getattr(on_resp, "status_code", 200)
    assert on_status != 404 or (
        json.loads(on_resp.body)["error"]["message"] != "solar template beta is not enabled"
    ), f"{name} reported flag-off while the flag was ON"

    # OFF: the SAME call, same process, now refuses
    monkeypatch.delenv(templates.FLAG_SOLAR_TEMPLATE_BETA, raising=False)
    off_resp = _call(name, clone.clone_id, write.write_id)
    assert off_resp.status_code == 404
    off_body = json.loads(off_resp.body)
    assert off_body["error"]["message"] == "solar template beta is not enabled"

    # ON again: the identical call succeeds once more -- proves the check
    # reads the flag live on every call, never a value latched at import time
    monkeypatch.setenv(templates.FLAG_SOLAR_TEMPLATE_BETA, "1")
    back_on_resp = _call(name, clone.clone_id, write.write_id)
    back_on_status = getattr(back_on_resp, "status_code", 200)
    assert back_on_status != 404 or (
        json.loads(back_on_resp.body)["error"]["message"] != "solar template beta is not enabled"
    ), f"{name} stayed refused after the flag was flipped back on"
