"""HTTP contract tests for the browser-safe Wave D router."""
from __future__ import annotations

import os
import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import deps
from routers import ios_ship as router

SHA = "a" * 64
PROJECT = str(uuid.uuid4())
ORG = str(uuid.uuid4())
APPROVAL = str(uuid.uuid4())


class _Error(ValueError):
    def __init__(self, code="error", message="error", setup_action=None):
        super().__init__(message)
        self.code = code
        self.setup_action = setup_action


class FakeStore:
    GrantInvalid = RevisionNotApproved = LaunchConflict = ProjectUnavailable = IosShipError = _Error
    ReceiptTampered = _Error

    def __init__(self):
        self.launches = []
        self.project_available = True
        self.principal = "binding-1"
        self.principal_requests = []
        self.project_access = True
        self.grants = []
        self.readiness_records = []
        self.prior_readiness_missing = False

    def project_org(self, _project_id):
        return ORG if self.project_available else None

    def project_exists(self, org_id, _project_id):
        return self.project_available and str(org_id) == ORG

    def project_accessible(self, org_id, project_id, subject):
        return (self.project_available and self.project_access
                and str(org_id) == ORG and project_id == PROJECT and bool(subject))

    def resolve_ship_principal(self, _org_id, _project_id, _subject):
        self.principal_requests.append((str(_org_id), _project_id, _subject))
        return self.principal

    def readiness_projection(self, org_id, tenant_id, project_id, revision):
        if self.prior_readiness_missing and not self.readiness_records:
            raise self.IosShipError("readiness_missing", "no readiness record")
        if self.readiness_records and not self.readiness_records[-1]["healthy"]:
            return {"record_kind": "leaf.ios-ship-readiness.v1", "launchable": False,
                    "healthy": False, "reported_at": None, "org_id": str(org_id),
                    "project_id": project_id, "tenant_id": tenant_id,
                    "grant_status": "unavailable", "dispatch_available": False,
                    "reason": "unhealthy",
                    "setup_action": self.readiness_records[-1]["dispatch"]["action"],
                    "approved_launch": None}
        return {"record_kind": "leaf.ios-ship-readiness.v1", "launchable": True,
                "healthy": True, "reported_at": "2026-08-13T12:00:00+00:00",
                "org_id": str(org_id), "project_id": project_id, "tenant_id": tenant_id,
                "grant_status": "healthy", "dispatch_available": True,
                "setup_action": None, "approved_launch": {
                    "approval_id": APPROVAL, "revision": revision,
                    "source_revision": "83bbde1", "source_sha256": SHA,
                    "bundle_identifier": "com.leaf.soundbeam",
                    "marketing_version": "1.2", "build_number": "19"}}

    def approved_launch(self, _org_id, _project_id, revision):
        return {"approval_id": APPROVAL, "revision": revision,
                "source_revision": "83bbde1", "source_sha256": SHA,
                "bundle_identifier": "com.leaf.soundbeam",
                "marketing_version": "1.2", "build_number": "19"}

    def launch_execution(self, *args, **kwargs):
        self.launches.append((args, kwargs))
        return {"execution_id": "e-1", "project_id": PROJECT, "revision": "r1",
                "source_revision": "83bbde1", "source_sha256": SHA,
                "bundle_identifier": "com.leaf.soundbeam", "marketing_version": "1.2",
                "build_number": "19", "status": "dispatched", "receipt_id": None}

    def record_grant(self, *args):
        self.grants.append(args)

    def upsert_readiness(self, record):
        self.readiness_records.append(record)
        return record

    def get_execution(self, *_args):
        return {"execution_id": "e-1", "project_id": PROJECT, "status": "running"}

    def read_receipt(self, *_args):
        return {"kind": "leaf.ios-testflight-receipt.v1", "receipt_id": "r-1",
                "project_id": PROJECT, "bundle_identifier": "com.leaf.soundbeam",
                "marketing_version": "1.2", "build_number": "19",
                "app_store_connect_result": {"status": "testflight_available",
                                               "build_id": "asc-19"}}


class VerifiedTenant(str):
    def __new__(cls, tenant_id, org_id, subject):
        value = super().__new__(cls, tenant_id)
        value.tenant_id = tenant_id
        value.org_id = org_id
        value.subject = subject
        return value


def _client(tenant=None):
    app = FastAPI()
    app.include_router(router.router)
    if tenant is not None:
        app.dependency_overrides[deps.require_tenant] = lambda: tenant
    return TestClient(app, raise_server_exceptions=False)


def _body(**overrides):
    body = {"project_id": PROJECT, "approval_id": APPROVAL, "revision": "r1",
            "source_revision": "83bbde1", "source_sha256": SHA,
            "bundle_identifier": "com.leaf.soundbeam", "marketing_version": "1.2",
            "build_number": "19"}
    body.update(overrides)
    return body


@pytest.fixture(autouse=True)
def _reset_dispatch():
    router.set_dispatch(None)
    router.set_provider_readiness(None)
    previous = os.environ.get("LEAF_IOS_SHIP_APP_COLOR")
    os.environ["LEAF_IOS_SHIP_APP_COLOR"] = "primary"
    yield
    router.set_dispatch(None)
    router.set_provider_readiness(None)
    if previous is None:
        os.environ.pop("LEAF_IOS_SHIP_APP_COLOR", None)
    else:
        os.environ["LEAF_IOS_SHIP_APP_COLOR"] = previous


def test_readiness_is_revision_scoped_and_foreign_project_fails_closed(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(router, "_store", lambda: store)
    router.set_dispatch(lambda _: {"status": "dispatched"})
    router.set_provider_readiness(lambda scope: {
        "schema": "leaf.ios-ship-provider-readiness.v1", "healthy": True,
        "reported_at": "2026-08-13T12:00:00+00:00", "grant_id": str(uuid.uuid4()),
        "grant_revision": "1", "grant_expires_at": "2026-08-14T12:00:00+00:00",
        "setup_action": None, "dispatch_available": True})
    client = _client(VerifiedTenant(ORG, ORG, "auth0|user"))
    ready = client.get("/api/ios-ship/readiness",
                       params={"project_id": PROJECT, "revision": "r1"})
    assert ready.status_code == 200
    assert ready.json()["readiness"]["approved_launch"]["build_number"] == "19"
    store.project_available = False
    foreign = client.get("/api/ios-ship/readiness",
                         params={"project_id": PROJECT, "revision": "r1"})
    assert foreign.json()["readiness"]["launchable"] is False
    assert foreign.json()["readiness"]["reason"] == "project_unavailable"


def test_readiness_hides_launch_when_provider_is_not_mounted(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(router, "_store", lambda: store)
    response = _client(VerifiedTenant(ORG, ORG, "auth0|user")).get(
        "/api/ios-ship/readiness", params={"project_id": PROJECT, "revision": "r1"})
    readiness = response.json()["readiness"]
    assert readiness["launchable"] is False
    assert readiness["dispatch_available"] is False
    assert readiness["reason"] == "dispatch_unavailable"
    assert readiness["setup_action"] == "mount-apple-ship-dispatch"


def test_readiness_refreshes_the_exact_approved_tuple_before_projecting(monkeypatch):
    store = FakeStore()
    seen = []
    monkeypatch.setattr(router, "_store", lambda: store)
    router.set_dispatch(lambda _: {"status": "dispatched"})

    def refresh(scope):
        seen.append(scope)
        return {"healthy": True, "reported_at": "2026-08-13T12:00:00+00:00",
                "grant_id": str(uuid.uuid4()), "grant_revision": "7",
                "grant_expires_at": "2026-08-14T12:00:00+00:00", "setup_action": None,
                "dispatch_available": True}

    router.set_provider_readiness(refresh)
    response = _client(VerifiedTenant(ORG, ORG, "auth0|user")).get(
        "/api/ios-ship/readiness", params={"project_id": PROJECT, "revision": "r1"})
    assert response.json()["readiness"]["launchable"] is True
    assert seen == [{"tenant_id": ORG, "project_id": PROJECT, "source_revision": "83bbde1",
                     "source_sha256": SHA, "bundle_id": "com.leaf.soundbeam",
                     "marketing_version": "1.2", "build_number": "19"}]
    assert store.grants[0][2]["status"] == "healthy"
    assert store.readiness_records[0]["dispatch"]["available"] is True


def test_readiness_refresh_does_not_require_a_prior_readiness_record(monkeypatch):
    store = FakeStore()
    store.prior_readiness_missing = True
    monkeypatch.setattr(router, "_store", lambda: store)
    router.set_dispatch(lambda _: {"status": "dispatched"})
    router.set_provider_readiness(lambda _scope: {
        "healthy": True, "reported_at": "2026-08-13T12:00:00+00:00",
        "grant_id": str(uuid.uuid4()), "grant_revision": "7",
        "grant_expires_at": "2026-08-14T12:00:00+00:00", "setup_action": None,
        "dispatch_available": True})

    response = _client(VerifiedTenant(ORG, ORG, "auth0|user")).get(
        "/api/ios-ship/readiness", params={"project_id": PROJECT, "revision": "r1"})

    assert response.json()["readiness"]["launchable"] is True
    assert len(store.readiness_records) == 1


def test_provider_unavailable_and_unhealthy_refresh_hide_the_launch(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(router, "_store", lambda: store)
    router.set_dispatch(lambda _: {"status": "dispatched"})
    router.set_provider_readiness(lambda _: (_ for _ in ()).throw(RuntimeError()))
    client = _client(VerifiedTenant(ORG, ORG, "auth0|user"))
    unavailable = client.get("/api/ios-ship/readiness", params={"project_id": PROJECT, "revision": "r1"})
    assert unavailable.json()["readiness"]["launchable"] is False
    assert unavailable.json()["readiness"]["reason"] == "provider_unavailable"

    router.set_provider_readiness(lambda _: {
        "healthy": False, "reported_at": "2026-08-13T12:00:00+00:00",
        "grant_id": str(uuid.uuid4()), "grant_revision": "7",
        "grant_expires_at": "2026-08-14T12:00:00+00:00",
        "setup_action": "re-mount-apple-developer-grant", "dispatch_available": False})
    unhealthy = client.get("/api/ios-ship/readiness", params={"project_id": PROJECT, "revision": "r1"})
    assert unhealthy.json()["readiness"]["launchable"] is False
    assert unhealthy.json()["readiness"]["setup_action"] == "re-mount-apple-developer-grant"
    assert store.grants[-1][2]["status"] == "unavailable"


@pytest.mark.parametrize("color", ["", "blue", "PRIMARY"])
def test_readiness_and_launch_fail_closed_without_a_valid_server_app_color(monkeypatch, color):
    store = FakeStore()
    monkeypatch.setattr(router, "_store", lambda: store)
    monkeypatch.setenv("LEAF_IOS_SHIP_APP_COLOR", color)
    router.set_dispatch(lambda _: {"status": "dispatched"})
    client = _client(VerifiedTenant(ORG, ORG, "auth0|user"))
    ready = client.get("/api/ios-ship/readiness", params={"project_id": PROJECT, "revision": "r1"})
    assert ready.json()["readiness"]["reason"] == "app_color_unavailable"
    launch = client.post("/api/ios-ship/launch", json=_body(), headers={"Idempotency-Key": "k-1"})
    assert launch.status_code == 503
    assert store.launches == []


def test_launch_requires_key_and_rejects_secret_fields_before_store(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(router, "_store", lambda: store)
    client = _client()
    assert client.post("/api/ios-ship/launch", json=_body()).status_code == 400
    router.set_dispatch(lambda _: {"status": "dispatched"})
    response = client.post("/api/ios-ship/launch", json=_body(apple_password="bad"),
                           headers={"Idempotency-Key": "k-1"})
    assert response.status_code == 400
    assert response.json()["error"]["error_code"] == "secret_shaped_field"
    assert store.launches == []


def test_launch_forwards_all_six_approved_fields_and_ignores_principal_header(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(router, "_store", lambda: store)
    router.set_dispatch(lambda _: {"status": "dispatched"})
    client = _client(VerifiedTenant(ORG, ORG, "auth0|user"))
    response = client.post("/api/ios-ship/launch", json=_body(), headers={
        "Idempotency-Key": "k-1", "X-Principal-Id": "attacker"})
    assert response.status_code == 202
    args, kwargs = store.launches[0]
    assert args[2] == "binding-1"
    assert {key: kwargs[key] for key in (
        "revision", "source_revision", "source_sha256", "bundle_identifier",
        "marketing_version", "build_number")} == {
            "revision": "r1", "source_revision": "83bbde1", "source_sha256": SHA,
            "bundle_identifier": "com.leaf.soundbeam", "marketing_version": "1.2",
            "build_number": "19"}
    assert kwargs["app_color"] == "primary"


@pytest.mark.parametrize("color", ["primary", "alternate"])
def test_each_server_color_forwards_only_its_server_owned_route_selector(monkeypatch, color):
    store = FakeStore()
    monkeypatch.setattr(router, "_store", lambda: store)
    monkeypatch.setenv("LEAF_IOS_SHIP_APP_COLOR", color)
    router.set_dispatch(lambda _: {"status": "dispatched"})
    response = _client(VerifiedTenant(ORG, ORG, "auth0|user")).post(
        "/api/ios-ship/launch", json=_body(), headers={"Idempotency-Key": "k-1"})
    assert response.status_code == 202
    assert store.launches[0][1]["app_color"] == color


def test_live_role_without_ship_principal_is_forbidden(monkeypatch):
    store = FakeStore()
    store.principal = None
    monkeypatch.setattr(router, "_store", lambda: store)
    router.set_dispatch(lambda _: {"status": "dispatched"})
    client = _client(VerifiedTenant(ORG, ORG, "auth0|read-only"))
    response = client.post("/api/ios-ship/launch", json=_body(),
                           headers={"Idempotency-Key": "k-1"})
    assert response.status_code == 403
    assert store.launches == []


def test_live_project_membership_is_part_of_principal_resolution(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(router, "_store", lambda: store)
    router.set_dispatch(lambda _: {"status": "dispatched"})
    client = _client(VerifiedTenant(ORG, ORG, "auth0|user"))
    response = client.post("/api/ios-ship/launch", json=_body(),
                           headers={"Idempotency-Key": "k-1"})
    assert response.status_code == 202
    assert store.principal_requests == [(ORG, PROJECT, "auth0|user")]


def test_execution_and_fixed_receipt_are_project_scoped(monkeypatch):
    store = FakeStore()
    monkeypatch.setattr(router, "_store", lambda: store)
    client = _client(VerifiedTenant(ORG, ORG, "auth0|user"))
    execution = client.get("/api/ios-ship/executions/e-1", params={"project_id": PROJECT})
    receipt = client.get("/api/ios-ship/receipts/r-1", params={"project_id": PROJECT})
    assert execution.status_code == 200
    assert receipt.status_code == 200
    assert receipt.json()["receipt"]["app_store_connect_result"]["build_id"] == "asc-19"


def test_revoked_project_member_cannot_read_execution_or_receipt(monkeypatch):
    store = FakeStore()
    store.project_access = False
    monkeypatch.setattr(router, "_store", lambda: store)
    client = _client(VerifiedTenant(ORG, ORG, "auth0|revoked"))
    execution = client.get("/api/ios-ship/executions/e-1", params={"project_id": PROJECT})
    receipt = client.get("/api/ios-ship/receipts/r-1", params={"project_id": PROJECT})
    assert execution.status_code == 404
    assert receipt.status_code == 404
