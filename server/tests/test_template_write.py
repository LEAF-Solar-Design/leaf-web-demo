"""Acceptance tests for card C2-4: the ONE write path -- bounded, validated,
fails closed.

Acceptance oracle:
    - Exactly one mutation endpoint on the cloned instance; payload
      schema-validated with caps; anything malformed -> typed 4xx, no write.
    - Write is transactional with a statement timeout; concurrent writes on
      the same instance serialize (no lost update: second writer gets
      conflict).

Offline, in-process (direct function calls against the real ``templates``
module and ``routers.templates``), mirroring ``tests/test_template_clone.py``
and ``tests/test_template_pinning.py``'s sys.path setup and calling
convention.

Run:  cd server && python -m pytest tests/test_template_write.py -q
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

os.environ.setdefault("LEAF_AUTH_LIVE", "0")

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

import deps  # noqa: E402
import templates  # noqa: E402
from customization_authority import TenantBinding  # noqa: E402
from routers import templates as templates_router  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Every test starts and ends with an empty clone store, flag enabled,
    and the production lock-timeout constant restored."""
    monkeypatch.setenv(templates.FLAG_SOLAR_TEMPLATE_BETA, "1")
    monkeypatch.setattr(templates, "_PROJECT_CLONES", {})
    yield


def _owner_binding(tenant_id: str = "tenant-a") -> TenantBinding:
    return TenantBinding(tenant_id=tenant_id, subject="author", role="owner", verified=True)


def _seed_clone(
    tenant_id: str = "tenant-a", project_id: str = "project-1",
    template_id: str = "rooftop-standard-string", version: str = "1.0.0",
) -> templates.ProjectTemplateClone:
    return templates.clone_template_for_project(
        template_id, tenant_id=tenant_id, project_id=project_id, version=version,
        binding=_owner_binding(tenant_id),
    )


def _hold_write_lock():
    """Start a thread that holds ``templates._PROJECT_CLONES_LOCK`` until
    released, returning (thread, release_event) once the lock is actually
    held -- so a test can force a REAL contention window rather than trusting
    a config string."""
    holder_ready = threading.Event()
    release_holder = threading.Event()

    def _hold():
        templates._PROJECT_CLONES_LOCK.acquire()
        holder_ready.set()
        release_holder.wait(timeout=5)
        templates._PROJECT_CLONES_LOCK.release()

    thread = threading.Thread(target=_hold)
    thread.start()
    assert holder_ready.wait(timeout=5), "lock holder thread never acquired the lock"
    return thread, release_holder


# --------------------------------------------------------------------------- #
# ORACLE: exactly one mutation endpoint on the cloned instance
# --------------------------------------------------------------------------- #
def test_exactly_one_mutation_endpoint_on_the_cloned_instance():
    clone_routes = [
        route for route in templates_router.router.routes
        if getattr(route, "path", "").startswith("/api/templates/clones")
    ]
    assert len(clone_routes) == 1
    mutating = {"POST", "PUT", "PATCH", "DELETE"}
    assert clone_routes[0].methods & mutating == {"PATCH"}


def test_the_template_registry_preview_route_is_not_a_cloned_instance_mutation():
    """The pre-existing ``/api/templates/{template_id}/clone`` route (C2-1..3)
    never lands a project-owned clone -- it is out of THIS oracle's scope,
    which is scoped to the cloned instance."""
    preview_routes = [
        route for route in templates_router.router.routes
        if getattr(route, "path", "") == "/api/templates/{template_id}/clone"
    ]
    assert len(preview_routes) == 1


# --------------------------------------------------------------------------- #
# ORACLE: payload schema-validated with caps; malformed -> typed 4xx, no write
# --------------------------------------------------------------------------- #
def test_request_model_rejects_an_unknown_field():
    with pytest.raises(ValidationError):
        templates_router.UpdateProjectCloneContentRequest(
            content={"a": 1}, expected_content_version=1, unexpected="x",
        )


def test_request_model_rejects_a_non_positive_expected_version():
    with pytest.raises(ValidationError):
        templates_router.UpdateProjectCloneContentRequest(
            content={"a": 1}, expected_content_version=0,
        )


def test_request_model_rejects_non_object_content():
    with pytest.raises(ValidationError):
        templates_router.UpdateProjectCloneContentRequest(
            content=["not", "an", "object"], expected_content_version=1,
        )


@pytest.mark.parametrize(
    "bad_content",
    [
        "not-a-dict",
        [],
        {},
        {str(i): i for i in range(65)},  # too many top-level keys
        {"s": "x" * 4001},  # string value too long
        {"items": list(range(257))},  # array too many items
        {"n": {"a": {"b": {"c": {"d": {"e": {"f": 1}}}}}}},  # depth 7 > cap 6
        {"n": float("nan")},  # non-finite number
    ],
)
def test_malformed_content_is_a_typed_validation_error_and_lands_nothing(bad_content):
    clone = _seed_clone()
    with pytest.raises(templates.ProjectTemplateCloneWriteValidationError):
        templates.update_project_clone_content(
            clone.clone_id, clone.tenant_id, bad_content, clone.content_version,
            binding=_owner_binding(clone.tenant_id),
        )
    stored = templates.get_project_clone(clone.clone_id)
    assert stored.content_version == clone.content_version
    assert stored.content == clone.content


def test_content_exceeding_the_encoded_byte_cap_is_rejected_and_lands_nothing():
    clone = _seed_clone()
    # Under every per-field cap (60 keys < 64, 300 chars < 4000) but the
    # encoded total blows the 16 KiB cap -- proves the BYTE cap is enforced
    # independently of the per-key/per-string caps.
    oversized = {f"k{i}": "x" * 300 for i in range(60)}
    with pytest.raises(templates.ProjectTemplateCloneWriteValidationError):
        templates.update_project_clone_content(
            clone.clone_id, clone.tenant_id, oversized, clone.content_version,
            binding=_owner_binding(clone.tenant_id),
        )
    assert templates.get_project_clone(clone.clone_id).content_version == 1


def test_non_integer_expected_version_is_a_typed_validation_error():
    clone = _seed_clone()
    for bad_version in ("1", 1.5, True, None):
        with pytest.raises(templates.ProjectTemplateCloneWriteValidationError):
            templates.update_project_clone_content(
                clone.clone_id, clone.tenant_id, {"a": 1}, bad_version,
                binding=_owner_binding(clone.tenant_id),
            )
    assert templates.get_project_clone(clone.clone_id).content_version == 1


def test_malformed_payload_is_rejected_without_waiting_on_a_held_lock():
    """Validation runs BEFORE the atomic section is entered: a malformed
    payload must fail immediately even while another writer holds the lock,
    never block on it (a hang here would fail the test's own timeout)."""
    clone = _seed_clone()
    thread, release_holder = _hold_write_lock()
    try:
        with pytest.raises(templates.ProjectTemplateCloneWriteValidationError):
            templates.update_project_clone_content(
                clone.clone_id, clone.tenant_id, "not-a-dict", clone.content_version,
                binding=_owner_binding(clone.tenant_id),
            )
    finally:
        release_holder.set()
        thread.join(timeout=5)


def test_router_maps_a_malformed_payload_to_400_with_no_write():
    clone = _seed_clone()
    resp = templates_router.update_project_clone_content(
        clone.clone_id,
        templates_router.UpdateProjectCloneContentRequest(
            content={}, expected_content_version=clone.content_version,
        ),
        tenant=clone.tenant_id,
    )
    assert resp.status_code == 400
    body = json.loads(resp.body)
    assert body["error"]["error_code"] == "BAD_PARAMS"
    assert templates.get_project_clone(clone.clone_id).content_version == 1


# --------------------------------------------------------------------------- #
# ORACLE: write is transactional; concurrent writes on the same instance
# serialize (no lost update -- second writer gets a typed conflict)
# --------------------------------------------------------------------------- #
def test_successful_write_replaces_content_bumps_version_and_is_isolated():
    clone = _seed_clone()
    submitted = {"setback_ft": 42, "rows": [{"tilt": 5}]}
    updated = templates.update_project_clone_content(
        clone.clone_id, clone.tenant_id, submitted, clone.content_version,
        binding=_owner_binding(clone.tenant_id),
    )
    assert updated.content_version == clone.content_version + 1
    assert updated.content == submitted
    submitted["rows"][0]["tilt"] = 999  # mutate the caller's own dict after the call
    assert updated.content["rows"][0]["tilt"] == 5, "clone content must be deep-copy isolated"
    assert updated.receipt["action"] == "update"
    assert updated.receipt["content_version"] == updated.content_version

    # never touches the template registry (card C2-1/C2-3 invariant)
    original = templates._REGISTRY["rooftop-standard-string"].versions["1.0.0"].content
    assert original == {
        "array_type": "rooftop", "default_tilt_deg": 20, "default_azimuth_deg": 180,
        "string_length_max": 12, "setback_ft": 3,
    }


def test_stale_expected_version_after_a_real_write_is_a_typed_conflict():
    clone = _seed_clone()
    first = templates.update_project_clone_content(
        clone.clone_id, clone.tenant_id, {"a": 1}, clone.content_version,
        binding=_owner_binding(clone.tenant_id),
    )
    with pytest.raises(templates.ProjectTemplateCloneConflictError):
        templates.update_project_clone_content(
            clone.clone_id, clone.tenant_id, {"a": 2}, clone.content_version,  # stale (1)
            binding=_owner_binding(clone.tenant_id),
        )
    # the loser's attempt did not land -- store is exactly the winner's write
    stored = templates.get_project_clone(clone.clone_id)
    assert stored.content_version == first.content_version
    assert stored.content == {"a": 1}

    second = templates.update_project_clone_content(
        clone.clone_id, clone.tenant_id, {"a": 2}, first.content_version,
        binding=_owner_binding(clone.tenant_id),
    )
    assert second.content_version == first.content_version + 1


def test_concurrent_writers_on_the_same_instance_serialize_second_gets_conflict():
    """REAL threads, both racing to enter the atomic section with the SAME
    stale ``expected_content_version`` captured before either wrote -- not a
    sequential call dressed up as concurrent. Exactly one must win."""
    clone = _seed_clone()
    barrier = threading.Barrier(2)
    outcomes: dict[str, tuple] = {}

    def _writer(name: str, content: dict):
        barrier.wait(timeout=5)
        try:
            updated = templates.update_project_clone_content(
                clone.clone_id, clone.tenant_id, content, clone.content_version,
                binding=_owner_binding(clone.tenant_id),
            )
            outcomes[name] = ("ok", updated.content_version)
        except templates.ProjectTemplateCloneConflictError as exc:
            outcomes[name] = ("conflict", str(exc))

    t1 = threading.Thread(target=_writer, args=("a", {"writer": "a"}))
    t2 = threading.Thread(target=_writer, args=("b", {"writer": "b"}))
    t1.start()
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert not t1.is_alive() and not t2.is_alive()

    statuses = [outcomes["a"][0], outcomes["b"][0]]
    assert statuses.count("ok") == 1, "exactly one writer must win"
    assert statuses.count("conflict") == 1, "exactly one writer must get a typed conflict"

    final = templates.get_project_clone(clone.clone_id)
    assert final.content_version == clone.content_version + 1, (
        "the version must advance by exactly one -- both-succeed (lost update) "
        "and both-fail must be impossible"
    )
    winner = "a" if outcomes["a"][0] == "ok" else "b"
    assert final.content == {"writer": winner}


def test_router_maps_conflict_to_409():
    clone = _seed_clone()
    resp = templates_router.update_project_clone_content(
        clone.clone_id,
        templates_router.UpdateProjectCloneContentRequest(
            content={"a": 1}, expected_content_version=999,
        ),
        tenant=clone.tenant_id,
    )
    assert resp.status_code == 409
    assert templates.get_project_clone(clone.clone_id).content_version == 1


# --------------------------------------------------------------------------- #
# ORACLE: write is transactional with a statement timeout
# --------------------------------------------------------------------------- #
def test_write_times_out_when_it_cannot_enter_the_atomic_section_and_lands_nothing():
    """A REAL competing lock holder, not a config string nobody fires: the
    write must fail with a typed timeout, not hang and not silently succeed
    after the fact, and must land nothing."""
    clone = _seed_clone()
    thread, release_holder = _hold_write_lock()
    try:
        with pytest.raises(templates.ProjectTemplateCloneWriteTimeoutError):
            templates.update_project_clone_content(
                clone.clone_id, clone.tenant_id, {"a": 999}, clone.content_version,
                binding=_owner_binding(clone.tenant_id), lock_timeout_seconds=0.05,
            )
    finally:
        release_holder.set()
        thread.join(timeout=5)
    stored = templates.get_project_clone(clone.clone_id)
    assert stored.content_version == clone.content_version
    assert stored.content == clone.content


def test_timeout_is_never_reported_as_a_conflict_or_success():
    """Trap sheet #17: a timed-out write must be its OWN typed error, never
    aliased to the conflict shape or silently swallowed as success."""
    clone = _seed_clone()
    thread, release_holder = _hold_write_lock()
    try:
        with pytest.raises(templates.ProjectTemplateCloneWriteTimeoutError) as excinfo:
            templates.update_project_clone_content(
                clone.clone_id, clone.tenant_id, {"a": 1}, clone.content_version,
                binding=_owner_binding(clone.tenant_id), lock_timeout_seconds=0.05,
            )
    finally:
        release_holder.set()
        thread.join(timeout=5)
    assert not isinstance(excinfo.value, templates.ProjectTemplateCloneConflictError)


def test_router_maps_timeout_to_504(monkeypatch):
    monkeypatch.setattr(templates, "_CLONE_WRITE_LOCK_TIMEOUT_SECONDS", 0.05)
    clone = _seed_clone()
    thread, release_holder = _hold_write_lock()
    try:
        resp = templates_router.update_project_clone_content(
            clone.clone_id,
            templates_router.UpdateProjectCloneContentRequest(
                content={"a": 1}, expected_content_version=clone.content_version,
            ),
            tenant=clone.tenant_id,
        )
    finally:
        release_holder.set()
        thread.join(timeout=5)
    assert resp.status_code == 504
    body = json.loads(resp.body)
    assert body["error"]["error_code"] == "TIMEOUT"
    assert templates.get_project_clone(clone.clone_id).content_version == 1


# --------------------------------------------------------------------------- #
# ORACLE (trap sheet #9-#11): the lookup is tenant-scoped -- an unknown
# clone_id and a real clone belonging to a DIFFERENT tenant are indistinguishable
# --------------------------------------------------------------------------- #
def test_unknown_and_foreign_tenant_clone_ids_raise_the_same_exception_type():
    clone = _seed_clone(tenant_id="tenant-a")
    with pytest.raises(templates.ProjectTemplateCloneNotFoundError):
        templates.update_project_clone_content(
            "does-not-exist", "tenant-b", {"a": 1}, 1,
            binding=_owner_binding("tenant-b"),
        )
    with pytest.raises(templates.ProjectTemplateCloneNotFoundError):
        templates.update_project_clone_content(
            clone.clone_id, "tenant-b", {"a": 1}, clone.content_version,
            binding=_owner_binding("tenant-b"),
        )
    # tenant-a's clone is untouched by tenant-b's attempt
    assert templates.get_project_clone(clone.clone_id).content_version == 1


def test_router_unknown_and_foreign_tenant_get_byte_identical_404_body():
    clone = _seed_clone(tenant_id="tenant-a")
    req = templates_router.UpdateProjectCloneContentRequest(
        content={"a": 1}, expected_content_version=1,
    )
    resp_missing = templates_router.update_project_clone_content(
        "does-not-exist", req, tenant="tenant-b",
    )
    resp_foreign = templates_router.update_project_clone_content(
        clone.clone_id, req, tenant="tenant-b",
    )
    assert resp_missing.status_code == resp_foreign.status_code == 404
    body_missing = dict(json.loads(resp_missing.body))
    body_foreign = dict(json.loads(resp_foreign.body))
    # both messages legitimately echo the caller's own clone_id -- strip it
    # before comparing so the SHAPE (not the coincidental id string) is checked
    body_missing["error"]["message"] = body_foreign["error"]["message"] = ""
    assert body_missing == body_foreign


# --------------------------------------------------------------------------- #
# flag fence: solar_template_beta gates the write, checked before any landing
# --------------------------------------------------------------------------- #
def test_write_refuses_when_the_flag_is_off_and_lands_nothing(monkeypatch):
    clone = _seed_clone()
    monkeypatch.delenv(templates.FLAG_SOLAR_TEMPLATE_BETA, raising=False)
    with pytest.raises(templates.TemplateBetaDisabledError):
        templates.update_project_clone_content(
            clone.clone_id, clone.tenant_id, {"a": 1}, clone.content_version,
            binding=_owner_binding(clone.tenant_id),
        )
    monkeypatch.setenv(templates.FLAG_SOLAR_TEMPLATE_BETA, "1")
    assert templates.get_project_clone(clone.clone_id).content_version == 1


def test_router_write_refuses_when_flag_off_and_lands_nothing(monkeypatch):
    clone = _seed_clone()
    monkeypatch.delenv(templates.FLAG_SOLAR_TEMPLATE_BETA, raising=False)
    resp = templates_router.update_project_clone_content(
        clone.clone_id,
        templates_router.UpdateProjectCloneContentRequest(
            content={"a": 1}, expected_content_version=clone.content_version,
        ),
        tenant=clone.tenant_id,
    )
    assert resp.status_code == 404
    monkeypatch.setenv(templates.FLAG_SOLAR_TEMPLATE_BETA, "1")
    assert templates.get_project_clone(clone.clone_id).content_version == 1


# --------------------------------------------------------------------------- #
# ORACLE (closes PR #704 finding 7): the PATCH content route requires an
# editor/owner binding for the clone's own project -- the SAME
# ``_write_role``/``_binding_for``/``_write_denied`` gate the C2-7 clone/
# write/undo routes use (``tests/test_template_roles_write.py`` covers those
# routes; this endpoint had ZERO role coverage before this card).
# --------------------------------------------------------------------------- #
def _write_ctx(role: str, tenant_id: str = "tenant-a") -> deps.TenantContext:
    return deps.TenantContext(tenant_id, subject="author", roles=(role,))


def _patch_req(content=None, expected_content_version: int = 1):
    return templates_router.UpdateProjectCloneContentRequest(
        content=content or {"setback_ft": 1}, expected_content_version=expected_content_version,
    )


@pytest.mark.parametrize("role", ["owner", "editor"])
def test_editor_and_owner_are_allowed_through_the_patch_endpoint(role):
    clone = _seed_clone(tenant_id="tenant-a")
    resp = templates_router.update_project_clone_content(
        clone.clone_id, _patch_req(expected_content_version=clone.content_version),
        tenant=_write_ctx(role, "tenant-a"),
    )
    assert isinstance(resp, dict), "an authorized write must not be denied"
    assert resp["clone_id"] == clone.clone_id
    assert resp["content_version"] == clone.content_version + 1


def test_viewer_is_denied_through_the_patch_endpoint_same_shape_as_write_denied():
    clone = _seed_clone(tenant_id="tenant-a")
    viewer_resp = templates_router.update_project_clone_content(
        clone.clone_id, _patch_req(expected_content_version=clone.content_version),
        tenant=_write_ctx("viewer", "tenant-a"),
    )
    # a real editor/owner-only mutation route's denial (write_project_clone)
    # must be BYTE-IDENTICAL to the PATCH route's denial -- proof both share
    # the same ``_write_denied()`` machinery, not two independently-shaped
    # 403s that happen to look similar.
    reference_denial = templates_router.write_project_clone(
        clone.clone_id,
        templates_router.ProjectCloneWriteRequest(content={"setback_ft": 1}),
        tenant=_write_ctx("viewer", "tenant-a"),
    )
    assert viewer_resp.status_code == reference_denial.status_code == 403
    assert json.loads(viewer_resp.body) == json.loads(reference_denial.body)
    assert templates.get_project_clone(clone.clone_id).content_version == 1


@pytest.mark.parametrize("role", ["reviewer", "read_only", "builder", "unknown"])
def test_reviewer_and_other_non_write_roles_are_denied_and_land_nothing(role):
    clone = _seed_clone(tenant_id="tenant-a")
    resp = templates_router.update_project_clone_content(
        clone.clone_id, _patch_req(expected_content_version=clone.content_version),
        tenant=_write_ctx(role, "tenant-a"),
    )
    no_role_resp = templates_router.update_project_clone_content(
        clone.clone_id, _patch_req(expected_content_version=clone.content_version),
        tenant=deps.TenantContext("tenant-a", subject="author", roles=()),
    )
    assert resp.status_code == no_role_resp.status_code == 403
    assert json.loads(resp.body) == json.loads(no_role_resp.body)
    assert templates.get_project_clone(clone.clone_id).content_version == 1


def test_role_downgrade_after_clone_is_honored_on_the_next_patch_write():
    """Role is re-derived from the CURRENT ``tenant.roles`` every call, never
    cached from clone time -- a mid-session editor -> viewer downgrade is
    honored on the very next PATCH."""
    clone = _seed_clone(tenant_id="tenant-a")
    resp = templates_router.update_project_clone_content(
        clone.clone_id, _patch_req(expected_content_version=clone.content_version),
        tenant=_write_ctx("viewer", "tenant-a"),
    )
    assert resp.status_code == 403
    assert templates.get_project_clone(clone.clone_id).content_version == 1


def test_update_project_clone_content_denies_a_viewer_binding_directly():
    """Defense in depth: ``templates.update_project_clone_content`` itself
    re-verifies ``binding`` inside its critical section, so a caller that
    bypasses the router (calls the function directly, as this file already
    does throughout) still cannot write with a viewer-role binding."""
    clone = _seed_clone(tenant_id="tenant-a")
    viewer_binding = TenantBinding(
        tenant_id="tenant-a", subject="author", role="viewer", verified=True)
    with pytest.raises(templates.AuthorityError):
        templates.update_project_clone_content(
            clone.clone_id, clone.tenant_id, {"a": 1}, clone.content_version,
            binding=viewer_binding,
        )
    assert templates.get_project_clone(clone.clone_id).content_version == 1


def test_update_project_clone_content_denies_a_revoked_unverified_binding_same_exception():
    """A binding whose session has been revoked (``verified=False``) is
    denied with the SAME exception type as a wrong-role binding -- neither
    reason is distinguishable to a caller, and neither lands a write."""
    clone = _seed_clone(tenant_id="tenant-a")
    revoked_binding = TenantBinding(
        tenant_id="tenant-a", subject="author", role="owner", verified=False)
    with pytest.raises(templates.AuthorityError) as revoked_excinfo:
        templates.update_project_clone_content(
            clone.clone_id, clone.tenant_id, {"a": 1}, clone.content_version,
            binding=revoked_binding,
        )
    viewer_binding = TenantBinding(
        tenant_id="tenant-a", subject="author", role="viewer", verified=True)
    with pytest.raises(templates.AuthorityError) as viewer_excinfo:
        templates.update_project_clone_content(
            clone.clone_id, clone.tenant_id, {"a": 1}, clone.content_version,
            binding=viewer_binding,
        )
    assert type(revoked_excinfo.value) is type(viewer_excinfo.value)
    assert templates.get_project_clone(clone.clone_id).content_version == 1
