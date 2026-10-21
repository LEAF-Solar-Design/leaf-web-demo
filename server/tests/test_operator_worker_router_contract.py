"""HTTP admission and forwarding only; fixture receipts prove no worker isolation."""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import deps  # noqa: E402
import operator_deps  # noqa: E402
import operator_principals  # noqa: E402
import operator_worker_dispatch as dispatch  # noqa: E402
from routers import operator_worker as route  # noqa: E402


class SyntheticTenant:
    subject = "auth0|g3-operator"

    def __str__(self):
        return "tenant-g3-fixture"


def _body(route_id):
    if route_id == "dispatch":
        return {"commands": ["echo inert"], "repo": "fixture-repo", "timeout_ms": 1000}
    return {"worker_id": "worker-fixture", "run_id": "run-fixture"}


def _post(harness, route_id, body=None, profile="default"):
    return harness.client.post(
        f"/api/operator/worker/{route_id}",
        json=_body(route_id) if body is None else body,
        headers={"X-Operator-Profile": profile},
    )


def _assert_no_forwarding(harness):
    harness.dispatch.assert_not_called()
    harness.cancel.assert_not_called()


@pytest.fixture
def harness(monkeypatch):
    tenant = SyntheticTenant()
    principal = operator_principals.OperatorPrincipal(
        subject="auth0|g3-operator", role="operator", role_revision=7,
        status="active", profiles=("default",), environment="staging",
    )
    resolver = Mock(return_value=principal)
    kill_switch = Mock(return_value=False)
    dispatch_spy = Mock(return_value={"jobId": "opjob-fixture", "status": "succeeded"})
    cancel_spy = Mock(return_value={"status": "cancelled"})
    monkeypatch.setattr(deps, "auth_live", lambda: True)
    monkeypatch.setattr(operator_principals, "resolve_principal", resolver)
    monkeypatch.setattr(route.operator_authority, "kill_switch_active", kill_switch)
    monkeypatch.setattr(dispatch, "dispatch_to_isolated_worker", dispatch_spy)
    monkeypatch.setattr(dispatch, "cancel_exact_active_worker", cancel_spy)
    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[deps.require_tenant] = lambda: tenant
    try:
        with TestClient(app) as client:
            yield SimpleNamespace(
                client=client, tenant=tenant, principal=principal,
                resolver=resolver, kill_switch=kill_switch,
                dispatch=dispatch_spy, cancel=cancel_spy,
            )
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize("route_id", ["dispatch", "cancel"])
@pytest.mark.parametrize("case", [
    "missing-subject", "absent-principal", "inactive-principal",
])
def test_worker_routes_reject_missing_or_revoked_principal(harness, route_id, case):
    if case == "missing-subject":
        harness.tenant.subject = None
    elif case == "absent-principal":
        harness.resolver.return_value = None
    else:
        harness.resolver.return_value = replace(harness.principal, status="revoked")

    response = _post(harness, route_id)

    assert response.status_code == 404
    assert response.json() == {"detail": "operator_not_found"}
    _assert_no_forwarding(harness)
    if case == "missing-subject":
        harness.resolver.assert_not_called()
    else:
        harness.resolver.assert_called_once_with("auth0|g3-operator")


@pytest.mark.parametrize("route_id", ["dispatch", "cancel"])
def test_worker_routes_fail_closed_when_principal_store_unavailable(harness, route_id):
    marker = "synthetic-principal-store-canary"
    harness.resolver.side_effect = RuntimeError(marker)

    response = _post(harness, route_id)

    assert response.status_code == 503
    assert response.json() == {"detail": "operator_store_unavailable"}
    assert marker not in response.text
    harness.resolver.assert_called_once_with("auth0|g3-operator")
    _assert_no_forwarding(harness)


@pytest.mark.parametrize("route_id", ["dispatch", "cancel"])
def test_worker_routes_reject_ungranted_profile(harness, route_id):
    response = _post(harness, route_id, profile="ungranted")

    assert response.status_code == 404
    assert response.json() == {"detail": "operator_not_found"}
    harness.resolver.assert_called_once_with("auth0|g3-operator")
    _assert_no_forwarding(harness)


@pytest.mark.parametrize("route_id", ["dispatch", "cancel"])
def test_worker_routes_kill_switch_prevents_forwarding(harness, route_id):
    harness.kill_switch.return_value = True

    response = _post(harness, route_id)

    assert response.status_code == 409
    assert response.json() == {"detail": "kill_switch_active"}
    harness.resolver.assert_called_once_with("auth0|g3-operator")
    harness.kill_switch.assert_called_once_with()
    _assert_no_forwarding(harness)


def test_worker_dispatch_forwards_server_identity_and_receipt(harness):
    response = _post(harness, "dispatch")

    assert response.status_code == 200
    assert response.json() == {"jobId": "opjob-fixture", "status": "succeeded"}
    assert set(response.json()) == {"jobId", "status"}
    expected_context = operator_deps.OperatorContext(
        subject="auth0|g3-operator", role="operator", role_revision=7,
        profiles=("default",), environment="staging", profile="default",
    )
    harness.resolver.assert_called_once_with("auth0|g3-operator")
    harness.dispatch.assert_called_once_with(
        expected_context, ["echo inert"], repo="fixture-repo",
        timeout_ms=1000, tenant_id="tenant-g3-fixture",
    )
    assert isinstance(harness.dispatch.call_args.args[0], operator_deps.OperatorContext)
    harness.cancel.assert_not_called()


@pytest.mark.parametrize("updates, field", [
    ({"commands": []}, "commands"),
    ({"commands": ["echo inert"] * 51}, "commands"),
    ({"timeout_ms": 0}, "timeout_ms"),
    ({"timeout_ms": 1800001}, "timeout_ms"),
    ({"tenant_id": "forged-tenant-fixture"}, "tenant_id"),
], ids=[
    "empty-commands", "too-many-commands", "timeout-zero",
    "timeout-too-large", "forged-tenant",
])
def test_worker_dispatch_rejects_invalid_body_before_forwarding(harness, updates, field):
    body = _body("dispatch")
    body.update(updates)

    response = _post(harness, "dispatch", body=body)

    assert response.status_code == 422
    payload = response.json()
    assert set(payload) == {"detail"}
    assert isinstance(payload["detail"], list)
    assert payload["detail"]
    assert any(error["loc"] == ["body", field] for error in payload["detail"])
    for error in payload["detail"]:
        assert {"loc", "msg", "type"} <= error.keys()
    _assert_no_forwarding(harness)


@pytest.mark.parametrize("reason, status", [
    ("substrate_not_isolating", 503), ("worker_receipt_invalid", 502),
])
def test_worker_dispatch_preserves_typed_refusal_status(harness, reason, status):
    harness.dispatch.side_effect = dispatch.OperatorWorkerError(reason, http_status=status)

    response = _post(harness, "dispatch")

    assert response.status_code == status
    assert response.json() == {"detail": reason}
    assert harness.dispatch.call_count == 1
    harness.cancel.assert_not_called()


def test_worker_dispatch_redacts_unexpected_backend_failure(harness):
    marker = "synthetic-backend-canary"
    harness.dispatch.side_effect = RuntimeError(marker)

    response = _post(harness, "dispatch")

    assert response.status_code == 503
    assert response.json() == {"detail": "operator_worker_unavailable"}
    assert marker not in response.text
    assert harness.dispatch.call_count == 1
    harness.cancel.assert_not_called()
