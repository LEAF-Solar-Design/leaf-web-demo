"""Truthfulness tests for the ios_ship -> ios_surface contract bridge.

Oracle (server/ios_surface_bridge.py):
- ``readiness.launchable`` means A TERMINAL BUILD EXISTS, never ios_ship's
  launch eligibility. The load-bearing case is a lane that says "you may press
  Launch" while nothing has ever been built: the surface must NOT say Ready.
- ``readiness.healthy`` means LANE health; a spent or absent revision approval
  is not a lane fault.
- No data / unreachable store -> the surface reports unavailable, never a
  served contract.
- Nothing credential-adjacent can reach the payload: the bridge emits exactly
  the seven published contract keys, and ios_surface.validate_contract accepts
  the result unchanged.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import deps  # noqa: E402
import ios_surface_bridge as bridge  # noqa: E402
from routers import ios_surface  # noqa: E402

TENANT = "tenant-a"
ORG = "11111111-1111-4111-8111-111111111111"
PROJECT = "22222222-2222-4222-8222-222222222222"
REVISION = "r1"
RECEIPT = "33333333-3333-4333-8333-333333333333"

SCOPE = {"tenant_id": TENANT, "project_id": PROJECT, "revision": REVISION}


class FakeStore:
    """Stands in for platform/ios_ship.py's read surface only."""

    def __init__(self, *, readiness, execution=None, receipt=None,
                 org=ORG, receipt_error=None):
        self._readiness = readiness
        self._execution = execution
        self._receipt = receipt
        self._org = org
        self._receipt_error = receipt_error
        self.receipt_calls = []

    def project_org(self, project_id):
        return self._org

    def readiness_projection(self, org_id, tenant_id, project_id, revision):
        return self._readiness

    def latest_execution_for_revision(self, org_id, tenant_id, project_id, revision):
        return self._execution

    def read_receipt(self, org_id, project_id, receipt_id):
        self.receipt_calls.append((org_id, project_id, receipt_id))
        if self._receipt_error is not None:
            raise self._receipt_error
        return self._receipt


def _healthy_readiness(**overrides):
    """What ios_ship publishes when it WILL let you press Launch TestFlight."""
    payload = {
        "record_kind": "leaf.ios-ship-readiness.v1",
        "launchable": True, "healthy": True,
        "reported_at": "2026-08-19T12:00:00+00:00",
        "org_id": ORG, "project_id": PROJECT, "tenant_id": TENANT,
        "grant_status": "healthy", "dispatch_available": True,
        "setup_action": None,
        "approved_launch": {"approval_id": "a1", "revision": REVISION},
    }
    payload.update(overrides)
    return payload


def _closed_readiness(reason):
    return {
        "record_kind": "leaf.ios-ship-readiness.v1",
        "launchable": False, "healthy": False, "reported_at": None,
        "org_id": ORG, "project_id": PROJECT, "grant_status": None,
        "dispatch_available": False, "reason": reason,
        "setup_action": "mount-apple-ship-dispatch", "approved_launch": None,
    }


def _use(monkeypatch, store):
    monkeypatch.setattr(bridge, "_store", lambda: store)
    return store


# --------------------------------------------------------------------------- #
# THE LOAD-BEARING CASE: launch-eligible, but nothing has ever been built
# --------------------------------------------------------------------------- #
def test_launch_eligible_with_no_build_is_not_launchable(monkeypatch):
    _use(monkeypatch, FakeStore(readiness=_healthy_readiness(), execution=None))

    contract = bridge.contract_source(SCOPE)

    # ios_ship said launchable=True (press the button). The surface renders
    # launchable as "iOS app Ready", so it MUST be False here.
    assert contract["readiness"] == {"healthy": True, "launchable": False}
    assert contract["build_stage"] is None
    assert contract["receipt_id"] is None


def test_launch_eligible_with_no_build_does_not_render_ready_through_the_route(monkeypatch):
    _use(monkeypatch, FakeStore(readiness=_healthy_readiness(), execution=None))
    monkeypatch.setenv("LEAF_IOS_SURFACE_ENABLED", "1")
    ios_surface.set_contract_source(bridge.contract_source)
    try:
        app = FastAPI()
        app.include_router(ios_surface.router)
        app.dependency_overrides[deps.require_tenant] = lambda: TENANT
        response = TestClient(app, raise_server_exceptions=False).get(
            "/api/ios-surface/status",
            params={"project_id": PROJECT, "revision": REVISION})
    finally:
        ios_surface.set_contract_source(None)

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "available"
    assert body["contract"]["readiness"]["launchable"] is False
    assert body["contract"]["receipt_id"] is None


def test_in_flight_build_reports_its_stage_but_is_not_launchable(monkeypatch):
    _use(monkeypatch, FakeStore(
        readiness=_healthy_readiness(),
        execution={"execution_id": "e1", "status": "running", "failed_stage": None,
                   "receipt_id": None,
                   "dispatch_result": {"status": "running", "stage": "BUILT",
                                       "provider_run_id": "run-1"}}))

    contract = bridge.contract_source(SCOPE)

    assert contract["readiness"]["launchable"] is False
    assert contract["build_stage"] == "BUILT"
    assert contract["receipt_id"] is None


def test_failed_build_reports_its_failed_stage_and_is_not_launchable(monkeypatch):
    _use(monkeypatch, FakeStore(
        readiness=_healthy_readiness(),
        execution={"execution_id": "e1", "status": "failed",
                   "failed_stage": "SIGNING_READY", "receipt_id": None,
                   "dispatch_result": {"status": "failed", "provider_run_id": "run-1"}}))

    contract = bridge.contract_source(SCOPE)

    assert contract["readiness"]["launchable"] is False
    assert contract["build_stage"] == "SIGNING_READY"


def test_stage_outside_the_frozen_vocabulary_projects_as_null(monkeypatch):
    _use(monkeypatch, FakeStore(
        readiness=_healthy_readiness(),
        execution={"execution_id": "e1", "status": "running", "failed_stage": None,
                   "receipt_id": None,
                   "dispatch_result": {"status": "running", "stage": "NOT_A_STAGE",
                                       "provider_run_id": "run-1"}}))

    assert bridge.contract_source(SCOPE)["build_stage"] is None


# --------------------------------------------------------------------------- #
# launchable is True ONLY for a terminal execution whose receipt reads back
# --------------------------------------------------------------------------- #
def _succeeded_execution():
    return {"execution_id": "e1", "status": "succeeded", "failed_stage": None,
            "receipt_id": RECEIPT,
            "dispatch_result": {"status": "dispatched", "provider_run_id": "run-1"}}


def test_terminal_build_with_a_readable_receipt_is_launchable(monkeypatch):
    store = _use(monkeypatch, FakeStore(
        readiness=_healthy_readiness(launchable=False, reason="approval_consumed"),
        execution=_succeeded_execution(),
        receipt={"receipt_id": RECEIPT, "kind": "leaf.ios-testflight-receipt.v1"}))

    contract = bridge.contract_source(SCOPE)

    assert contract["readiness"] == {"healthy": True, "launchable": True}
    assert contract["build_stage"] == "RECEIPT"
    assert contract["receipt_id"] == RECEIPT
    assert store.receipt_calls == [(ORG, PROJECT, RECEIPT)]


def test_succeeded_execution_with_no_readable_receipt_is_not_launchable(monkeypatch):
    _use(monkeypatch, FakeStore(readiness=_healthy_readiness(),
                                execution=_succeeded_execution(), receipt=None))

    contract = bridge.contract_source(SCOPE)

    assert contract["readiness"]["launchable"] is False
    assert contract["receipt_id"] is None


def test_unverifiable_receipt_is_not_launchable(monkeypatch):
    _use(monkeypatch, FakeStore(readiness=_healthy_readiness(),
                                execution=_succeeded_execution(),
                                receipt_error=RuntimeError("receipt_tampered")))

    contract = bridge.contract_source(SCOPE)

    assert contract["readiness"]["launchable"] is False
    assert contract["receipt_id"] is None


def test_succeeded_execution_without_a_receipt_id_is_not_launchable(monkeypatch):
    execution = _succeeded_execution()
    execution["receipt_id"] = None
    store = _use(monkeypatch, FakeStore(readiness=_healthy_readiness(),
                                        execution=execution))

    contract = bridge.contract_source(SCOPE)

    assert contract["readiness"]["launchable"] is False
    assert store.receipt_calls == []


# --------------------------------------------------------------------------- #
# healthy is LANE health, not approval state
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("reason", ["unapproved_revision", "approval_consumed"])
def test_spent_or_absent_approval_still_reads_as_a_healthy_lane(monkeypatch, reason):
    _use(monkeypatch, FakeStore(readiness=_closed_readiness(reason)))
    assert bridge.contract_source(SCOPE)["readiness"]["healthy"] is True


@pytest.mark.parametrize("reason", [
    "readiness_missing", "unhealthy", "stale_readiness", "dispatch_unavailable",
    "provider_unavailable", "readiness_unavailable",
])
def test_lane_faults_read_as_unhealthy(monkeypatch, reason):
    _use(monkeypatch, FakeStore(readiness=_closed_readiness(reason)))
    assert bridge.contract_source(SCOPE)["readiness"]["healthy"] is False


# --------------------------------------------------------------------------- #
# no-data / unreachable paths fail closed
# --------------------------------------------------------------------------- #
def test_unknown_project_raises_so_the_surface_reports_unavailable(monkeypatch):
    _use(monkeypatch, FakeStore(readiness=_healthy_readiness(), org=None))
    with pytest.raises(bridge.SurfaceScopeInvalid):
        bridge.contract_source(SCOPE)


@pytest.mark.parametrize("scope", [
    {}, {"tenant_id": TENANT}, {"tenant_id": TENANT, "project_id": PROJECT},
    {"tenant_id": "", "project_id": PROJECT, "revision": REVISION},
    {"tenant_id": TENANT, "project_id": "  ", "revision": REVISION},
    {"tenant_id": TENANT, "project_id": PROJECT, "revision": None},
])
def test_invalid_scope_fails_closed(monkeypatch, scope):
    _use(monkeypatch, FakeStore(readiness=_healthy_readiness()))
    with pytest.raises(bridge.SurfaceScopeInvalid):
        bridge.contract_source(scope)


def test_unreachable_store_surfaces_as_unavailable(monkeypatch):
    def _boom():
        raise RuntimeError("no database")

    monkeypatch.setattr(bridge, "_store", _boom)
    monkeypatch.setenv("LEAF_IOS_SURFACE_ENABLED", "1")
    ios_surface.set_contract_source(bridge.contract_source)
    try:
        app = FastAPI()
        app.include_router(ios_surface.router)
        app.dependency_overrides[deps.require_tenant] = lambda: TENANT
        response = TestClient(app, raise_server_exceptions=False).get(
            "/api/ios-surface/status",
            params={"project_id": PROJECT, "revision": REVISION})
    finally:
        ios_surface.set_contract_source(None)

    assert response.status_code == 200
    assert response.json() == {
        "ok": True, "status": "unavailable", "reason": "upstream_unreachable",
        "project_id": PROJECT, "revision": REVISION}


# --------------------------------------------------------------------------- #
# nothing credential-adjacent can reach the payload
# --------------------------------------------------------------------------- #
def test_payload_carries_exactly_the_published_contract_keys(monkeypatch):
    _use(monkeypatch, FakeStore(readiness=_healthy_readiness(),
                                execution=_succeeded_execution(),
                                receipt={"receipt_id": RECEIPT}))

    contract = bridge.contract_source(SCOPE)

    assert set(contract) == {
        "schema", "project_id", "revision", "reported_at", "readiness",
        "build_stage", "receipt_id"}
    assert set(contract["readiness"]) == {"healthy", "launchable"}


def test_secret_shaped_store_fields_never_reach_the_contract(monkeypatch):
    """The store rows carry Apple-adjacent identity. The bridge builds its
    payload literally, so none of it is copied through -- and the surface's own
    validator accepts the result rather than failing it closed."""
    poisoned_readiness = _healthy_readiness()
    poisoned_readiness["apple_private_key"] = "-----BEGIN PRIVATE KEY-----abc"
    poisoned_readiness["provisioning_profile"] = "profile-1"
    poisoned_execution = _succeeded_execution()
    poisoned_execution["signing_certificate"] = "cert-1"
    poisoned_execution["bundle_identifier"] = "ai.leafdesign.app"
    _use(monkeypatch, FakeStore(readiness=poisoned_readiness,
                                execution=poisoned_execution,
                                receipt={"receipt_id": RECEIPT,
                                         "app_store_connect_result": {"build_id": "b1"}}))

    contract = bridge.contract_source(SCOPE)

    flattened = repr(contract)
    for leaked in ("apple_private_key", "provisioning_profile", "signing_certificate",
                   "BEGIN PRIVATE KEY", "ai.leafdesign.app", "b1"):
        assert leaked not in flattened

    # validate_contract is the real gate: it rejects any secret-shaped key or
    # value anywhere in the payload, so a clean pass proves the bridge's output
    # is admissible as published.
    validated = ios_surface.validate_contract(PROJECT, REVISION, contract)
    assert validated["readiness"] == {"healthy": True, "launchable": True}
    assert validated["receipt_id"] == RECEIPT
    assert validated["build_stage"] == "RECEIPT"


def test_bridge_source_mentions_no_apple_credential_vocabulary():
    source = (SERVER_DIR / "ios_surface_bridge.py").read_text(encoding="utf-8")
    for forbidden in ("app_store_connect_app_id", "team_id", "issuer_id",
                      "AuthKey", "api_key", "app-specific"):
        assert forbidden not in source
