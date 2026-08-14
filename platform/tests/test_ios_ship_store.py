"""PostgreSQL integration tests for the migrated Wave D iOS authority."""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from leaf_platform import ios_ship, store

UTC = timezone.utc
SOURCE_SHA = "a" * 64


def _readiness(org, project, tenant="tenant-a", **overrides):
    record = {
        "record_kind": ios_ship.READINESS_KIND,
        "healthy": True,
        "reported_at": datetime.now(UTC).isoformat(),
        "org_id": str(org.org_id),
        "project_id": str(project.project_id),
        "tenant_id": tenant,
        "dispatch": {"available": True, "action": ios_ship.SETUP_ACTION},
    }
    record.update(overrides)
    return record


def _seed(make_org, tenant="tenant-a"):
    org = make_org()
    project = store.create_project(org.org_id, "ios project")
    binding = store.create_identity_binding(
        org.org_id, "auth0", f"auth0|ios-{uuid.uuid4()}", role="owner")
    with ios_ship.connection() as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO project_member_bindings "
            "(membership_id, org_id, project_id, binding_id, role, invited_by_binding_id) "
            "VALUES (%s, %s, %s, %s, 'owner', %s)",
            (uuid.uuid4(), org.org_id, project.project_id, binding.binding_id,
             binding.binding_id))
    ios_ship.upsert_readiness(_readiness(org, project, tenant))
    ios_ship.record_grant(org.org_id, tenant, {
        "grant_id": str(uuid.uuid4()), "status": "healthy",
        "expires_at": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
    })
    approval_id = ios_ship.record_approval(
        org.org_id, project.project_id, "r1", source_revision="83bbde1",
        source_sha256=SOURCE_SHA, bundle_identifier="com.leaf.soundbeam",
        marketing_version="1.2", build_number="19", approved_by="reviewer")
    return org, project, tenant, str(binding.binding_id), approval_id


def _launch(org, project, tenant, principal_id, approval_id, dispatch, **overrides):
    args = {
        "org_id": org.org_id, "project_id": project.project_id,
        "tenant_id": tenant, "principal_id": principal_id, "approval_id": approval_id,
        "revision": "r1", "source_revision": "83bbde1", "source_sha256": SOURCE_SHA,
        "bundle_identifier": "com.leaf.soundbeam", "marketing_version": "1.2",
        "build_number": "19", "app_color": "primary", "idempotency_key": "launch-1",
        "dispatch": dispatch,
    }
    args.update(overrides)
    return ios_ship.launch_execution(**args)


def _controller_receipt(project_id, tenant="tenant-a", **overrides):
    receipt = {
        "schema": "leaf.ios-testflight-receipt.v1", "run_id": "run-1",
        "request_digest": "sha256:" + "b" * 64, "review_id": "review-1", "tenant_id": tenant,
        "project_id": str(project_id), "source_revision": "83bbde1",
        "source_artifact_digest": "sha256:" + SOURCE_SHA,
        "bundle_id": "com.leaf.soundbeam", "marketing_version": "1.2",
        "build_number": "19", "image_id": "ami-0123456789abcdef0",
        "image_digest": "sha256:" + "c" * 64, "host_id": "h-1",
        "instance_id": "i-1", "region": "us-east-2",
        "availability_zone": "us-east-2a", "instance_type": "mac2.metal",
        "minimum_allocation_hours": 24, "estimated_cost_usd": "28.80",
        "xcode_version": "26.3", "xcode_build": "17C529",
        "app_store_connect_app_id": "asc-app-1",
        "app_store_connect_build_id": "asc-build-19", "status": "VALID",
        "beta_group": "Internal Testers", "compliance_answered": True,
        "credentials_scrubbed": True, "mac_instance_state": "terminated",
        "dedicated_host_state": "released", "teardown_receipt_id": "teardown-1",
        "completed_at": "2026-08-13T16:20:00+00:00",
    }
    receipt.update(overrides)
    return receipt


def test_readiness_is_project_and_revision_scoped(make_org):
    org, project_a, tenant, _, _ = _seed(make_org)
    project_b = store.create_project(org.org_id, "other project")
    ready = ios_ship.get_readiness(org.org_id, tenant, project_a.project_id, "r1")
    assert ready["project_id"] == str(project_a.project_id)
    assert ready["approved_launch"]["build_number"] == "19"
    with pytest.raises(ios_ship.ReadinessUnavailable) as exc:
        ios_ship.get_readiness(org.org_id, tenant, project_b.project_id, "r1")
    assert exc.value.code == "readiness_missing"
    with pytest.raises(ios_ship.RevisionNotApproved):
        ios_ship.get_readiness(org.org_id, tenant, project_a.project_id, "r2")


def test_launch_commits_canonical_job_before_one_dispatch_and_replays(make_org):
    org, project, tenant, principal, approval_id = _seed(make_org)
    calls = []

    def dispatch(intent):
        job = store.get_job(org.org_id, uuid.UUID(intent["execution_id"]))
        assert job is not None and job.status == "running"
        calls.append(intent)
        return {"status": "dispatched", "stage": "MAC_ALLOCATED", "provider_run_id": "run-1"}

    first = _launch(org, project, tenant, principal, approval_id, dispatch)
    replay = _launch(org, project, tenant, principal, approval_id, dispatch)
    assert replay["execution_id"] == first["execution_id"]
    assert len(calls) == 1
    assert calls[0]["app_color"] == "primary"
    assert store.get_job(org.org_id, uuid.UUID(first["execution_id"])).kind == "build"
    with pytest.raises(ios_ship.LaunchConflict) as exc:
        _launch(org, project, tenant, principal, approval_id, dispatch,
                idempotency_key="launch-2")
    assert exc.value.code == "approval_consumed"


def test_same_key_retries_the_same_intent_for_controller_owned_resume(make_org):
    org, project, tenant, principal, approval_id = _seed(make_org)
    calls = []

    def dispatch(intent):
        calls.append(intent)
        if len(calls) == 1:
            return {"status": "failed", "stage": "SIGNING_READY", "provider_run_id": "run-1"}
        return {"status": "dispatched", "stage": "UPLOADED", "provider_run_id": "run-1"}

    failed = _launch(org, project, tenant, principal, approval_id, dispatch)
    assert failed["status"] == "failed"
    resumed = _launch(org, project, tenant, principal, approval_id, dispatch)
    assert resumed["execution_id"] == failed["execution_id"]
    assert calls[1] == calls[0]
    assert len(calls) == 2


@pytest.mark.parametrize("app_color", ["", "blue", "PRIMARY"])
def test_store_fails_closed_for_unapproved_server_app_color(make_org, app_color):
    org, project, tenant, principal, approval_id = _seed(make_org)
    calls = []
    with pytest.raises(ios_ship.IosShipError) as exc:
        _launch(org, project, tenant, principal, approval_id,
                lambda intent: calls.append(intent) or {"status": "dispatched"},
                app_color=app_color)
    assert exc.value.code == "invalid_launch"
    assert calls == []


def test_accepted_then_exception_stays_ambiguous_and_never_dispatches_twice(make_org):
    org, project, tenant, principal, approval_id = _seed(make_org)
    calls = []

    def accepted_then_lost(intent):
        calls.append(intent)
        raise ConnectionError("response lost after provider acceptance")

    ambiguous = _launch(org, project, tenant, principal, approval_id, accepted_then_lost)
    assert ambiguous["status"] == "dispatching"
    assert ambiguous["failed_stage"] is None
    replay = _launch(org, project, tenant, principal, approval_id, accepted_then_lost)
    assert replay["execution_id"] == ambiguous["execution_id"]
    assert replay["status"] == "dispatching"
    assert len(calls) == 1


@pytest.mark.parametrize("field,bad", [
    ("source_revision", "other"),
    ("source_sha256", "b" * 64),
    ("bundle_identifier", "com.other.app"),
    ("marketing_version", "9.9"),
    ("build_number", "20"),
])
def test_every_approved_field_is_revalidated_before_dispatch(make_org, field, bad):
    org, project, tenant, principal, approval_id = _seed(make_org)
    calls = []
    with pytest.raises(ios_ship.RevisionNotApproved) as exc:
        _launch(org, project, tenant, principal, approval_id,
                lambda intent: calls.append(intent) or {"status": "dispatched"}, **{field: bad})
    assert exc.value.code == "approval_mismatch"
    assert calls == []


def test_fixed_receipt_is_identity_bound_and_readback_is_sanitized(make_org):
    org, project, tenant, principal, approval_id = _seed(make_org)
    execution = _launch(org, project, tenant, principal, approval_id,
                        lambda _: {"status": "dispatched", "provider_run_id": "run-1"})
    receipt = {
        "kind": ios_ship.RECEIPT_KIND, "receipt_id": str(uuid.uuid4()),
        "org_id": str(org.org_id), "tenant_id": tenant,
        "project_id": str(project.project_id), "revision": "r1",
        "source_revision": "83bbde1", "source_sha256": SOURCE_SHA,
        "bundle_identifier": "com.leaf.soundbeam", "marketing_version": "1.2",
        "build_number": "19", "image_identity": "ami-034cf03b9fd70147d",
        "toolchain_identity": "Xcode 26.3 (17C529)",
        "app_store_connect_result": {
            "status": "testflight_available", "build_id": "asc-build-19",
            "beta_group": "Internal Testers",
        },
    }
    written = ios_ship.record_receipt(
        org.org_id, project.project_id, execution["execution_id"], receipt)
    reread = ios_ship.read_receipt(org.org_id, project.project_id, written["receipt_id"])
    assert reread["hash"] == written["hash"]
    assert reread["app_store_connect_result"] == receipt["app_store_connect_result"]
    assert store.get_job(org.org_id, uuid.UUID(execution["execution_id"])).status == "succeeded"
    with pytest.raises(ios_ship.IosShipError):
        ios_ship.record_receipt(org.org_id, project.project_id, execution["execution_id"],
                                {**receipt, "p8_content": "secret"})
    with pytest.raises(ios_ship.IosShipError) as exc:
        ios_ship.record_receipt(org.org_id, project.project_id, execution["execution_id"],
                                {**receipt, "build_number": "20"})
    assert exc.value.code in {"invalid_receipt", "receipt_identity_mismatch"}


def test_provider_progress_is_scope_bound_and_pending_release_is_nonterminal(make_org):
    org, project, tenant, principal, approval_id = _seed(make_org)
    execution = _launch(org, project, tenant, principal, approval_id,
                        lambda _: {"status": "dispatched", "provider_run_id": "run-1"})
    with pytest.raises(ios_ship.IosShipError) as exc:
        ios_ship.record_provider_progress(
            org.org_id, tenant, project.project_id, execution["execution_id"], "wrong-run",
            status="running", stage="BUILT")
    assert exc.value.code == "provider_scope_mismatch"
    pending = ios_ship.record_provider_progress(
        org.org_id, tenant, project.project_id, execution["execution_id"], "run-1",
        status="PENDING_RELEASE", stage="MAC_RELEASED")
    assert pending["status"] == "running" and pending["receipt_id"] is None
    failed = ios_ship.record_provider_progress(
        org.org_id, tenant, project.project_id, execution["execution_id"], "run-1",
        status="failed", stage="MAC_RELEASED")
    assert failed["status"] == "failed" and failed["failed_stage"] == "MAC_RELEASED"
    with pytest.raises(ios_ship.IosShipError) as exc:
        ios_ship.record_provider_progress(
            org.org_id, tenant, project.project_id, execution["execution_id"], "run-1",
            status="running", stage="RECEIPT")
    assert exc.value.code == "execution_terminal"


def test_failed_provider_progress_requires_an_exact_controller_stage(make_org):
    org, project, tenant, principal, approval_id = _seed(make_org)
    execution = _launch(org, project, tenant, principal, approval_id,
                        lambda _: {"status": "dispatched", "provider_run_id": "run-1"})
    for stage in (None, "SIGNED"):
        with pytest.raises(ios_ship.IosShipError) as exc:
            ios_ship.record_provider_progress(
                org.org_id, tenant, project.project_id, execution["execution_id"], "run-1",
                status="failed", stage=stage)
        assert exc.value.code == "invalid_provider_progress"


@pytest.mark.parametrize("field,bad", [
    ("run_id", "run-other"),
    ("tenant_id", "tenant-other"),
    ("project_id", str(uuid.uuid4())),
    ("source_revision", "other"),
    ("source_artifact_digest", "sha256:" + "d" * 64),
    ("bundle_id", "com.other.app"),
    ("marketing_version", "9.9"),
    ("build_number", "20"),
    ("status", "PROCESSING"),
    ("compliance_answered", False),
    ("credentials_scrubbed", False),
    ("mac_instance_state", "running"),
    ("dedicated_host_state", "allocated"),
    ("teardown_receipt_id", ""),
    ("p8_content", "secret-value"),
])
def test_terminal_controller_receipt_rejects_identity_and_proof_drift(
        make_org, field, bad):
    org, project, tenant, principal, approval_id = _seed(make_org)
    execution = _launch(org, project, tenant, principal, approval_id,
                        lambda _: {"status": "dispatched", "provider_run_id": "run-1"})
    with pytest.raises(ios_ship.IosShipError):
        ios_ship.record_provider_receipt(
            org.org_id, tenant, project.project_id, execution["execution_id"], "run-1",
            _controller_receipt(project.project_id, **{field: bad}))
    assert ios_ship.get_execution(
        org.org_id, project.project_id, execution["execution_id"])["status"] == "dispatched"


def test_valid_controller_receipt_projects_stable_browser_receipt_and_replays(make_org):
    org, project, tenant, principal, approval_id = _seed(make_org)
    execution = _launch(org, project, tenant, principal, approval_id,
                        lambda _: {"status": "dispatched", "provider_run_id": "run-1"})
    raw = _controller_receipt(project.project_id)
    first = ios_ship.record_provider_receipt(
        org.org_id, tenant, project.project_id, execution["execution_id"], "run-1", raw)
    replay = ios_ship.record_provider_receipt(
        org.org_id, tenant, project.project_id, execution["execution_id"], "run-1", raw)
    assert replay == first
    assert first["image_identity"] == "ami-0123456789abcdef0@sha256:" + "c" * 64
    assert first["toolchain_identity"] == "Xcode 26.3 (17C529)"
    assert first["app_store_connect_result"] == {
        "status": "testflight_available", "build_id": "asc-build-19",
        "beta_group": "Internal Testers", "uploaded_at": "2026-08-13T16:20:00+00:00",
    }
    execution_row = ios_ship.get_execution(
        org.org_id, project.project_id, execution["execution_id"])
    reread = ios_ship.read_receipt(org.org_id, project.project_id, first["receipt_id"])
    assert execution_row["status"] == "succeeded"
    assert execution_row["receipt_id"] == first["receipt_id"]
    assert reread["receipt_id"] == first["receipt_id"]
