"""External-write HTTP contracts with synthetic auth and recording services.

Stub receipts prove forwarding and envelopes only. Transaction, replay, spend,
and worker isolation remain the responsibility of the service and PG tests.
"""
from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import operator_authority  # noqa: E402
import operator_deps  # noqa: E402
import operator_external_adapters  # noqa: E402
import operator_principals  # noqa: E402
from routers import operator_external as route  # noqa: E402

ROUTES = ("destinations", "propose", "execute")
WRITE_ROUTES = ("propose", "execute")
SUBJECT = "auth0|g3-operator"


class TrustedTenant:
    def __init__(self):
        self.subject = SUBJECT

    def __str__(self):
        return "fixture-tenant"


class RunbookError(RuntimeError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _body(route_id, payload_present=True):
    body = {
        "destination": "fixture-destination",
        "token_handle": "fixture-handle",
        "adapter": "fixture-adapter",
    }
    if payload_present:
        body["payload"] = {"message": "fixture"}
    if route_id == "execute":
        body["authority_id"] = "fixture-authority"
    return body


@pytest.fixture
def harness(monkeypatch):
    tenant = TrustedTenant()
    principal = operator_principals.OperatorPrincipal(
        subject=SUBJECT, role="operator", role_revision=7, status="active",
        profiles=("default",), environment="staging")
    context = operator_deps.OperatorContext(
        subject=SUBJECT, role="operator", role_revision=7,
        profiles=("default",), environment="staging", profile="default")
    resolver = Mock(return_value=principal)
    monkeypatch.setattr(operator_deps.tenant_deps, "auth_live", lambda: True)
    monkeypatch.setattr(operator_principals, "resolve_principal", resolver)

    destinations = {
        "fixture-destination": {
            "destination": "fixture-destination", "environment": "staging",
            "adapter": "fixture-adapter",
        },
    }
    proposal = {
        "authority_id": "fixture-authority", "action": "operator.external_write",
        "destination": "fixture-destination", "adapter": "fixture-adapter",
        "token_handle": "fixture-handle", "environment": "staging",
        "expires_at": "2030-01-01T00:00:00Z",
    }
    execution = {
        "action": "operator.external_write", "destination": "fixture-destination",
        "adapter": "fixture-adapter", "authority_id": "fixture-authority",
        "environment": "staging",
        "reversal": {"adapter_reversal": "fixture reversal instructions"},
    }
    spies = {
        "destinations": Mock(return_value=destinations),
        "propose": Mock(return_value=proposal),
        "execute": Mock(return_value=execution),
    }
    fake_runbook = SimpleNamespace(
        RunbookError=RunbookError, propose=spies["propose"],
        execute=spies["execute"])
    monkeypatch.setattr(route, "_rb", lambda: fake_runbook)
    monkeypatch.setattr(
        operator_external_adapters, "list_destinations", spies["destinations"])

    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[operator_deps.tenant_deps.require_tenant] = lambda: tenant
    try:
        with TestClient(app) as client:
            yield SimpleNamespace(
                client=client, tenant=tenant, principal=principal,
                context=context, resolver=resolver, spies=spies,
                destinations=destinations, proposal=proposal, execution=execution)
    finally:
        app.dependency_overrides.clear()


def _request(harness, route_id, *, body=None, profile="default"):
    headers = {"X-Operator-Profile": profile, "X-Operator-Subject": SUBJECT}
    url = f"/api/operator/external/{route_id}"
    if route_id == "destinations":
        return harness.client.get(url, headers=headers)
    return harness.client.post(
        url, json=_body(route_id) if body is None else body, headers=headers)


def _assert_no_dispatch(harness):
    for spy in harness.spies.values():
        spy.assert_not_called()


def _assert_one_dispatch(harness, route_id):
    assert harness.spies[route_id].call_count == 1
    for other_id in ROUTES:
        if other_id != route_id:
            harness.spies[other_id].assert_not_called()


def _assert_error(response, status, detail):
    assert response.status_code == status, response.text
    assert response.json() == {"detail": detail}


def _assert_validation(response, field):
    assert response.status_code == 422, response.text
    envelope = response.json()
    assert set(envelope) == {"detail"}
    errors = envelope["detail"]
    assert isinstance(errors, list) and errors
    assert all({"loc", "msg", "type"} <= set(error) for error in errors)
    assert any(error["loc"] == ["body", field] for error in errors)


@pytest.mark.parametrize("route_id", ROUTES)
def test_external_routes_reject_missing_operator_subject(harness, route_id):
    harness.tenant.subject = None
    response = _request(harness, route_id)
    _assert_error(response, 404, "operator_not_found")
    harness.resolver.assert_not_called()
    _assert_no_dispatch(harness)


@pytest.mark.parametrize("route_id", ROUTES)
def test_external_routes_reject_ungranted_operator(harness, route_id):
    harness.resolver.return_value = None
    response = _request(harness, route_id)
    _assert_error(response, 404, "operator_not_found")
    harness.resolver.assert_called_once_with(SUBJECT)
    _assert_no_dispatch(harness)


@pytest.mark.parametrize("route_id", ROUTES)
def test_external_routes_reject_inactive_operator(harness, route_id):
    harness.resolver.return_value = replace(harness.principal, status="revoked")
    response = _request(harness, route_id)
    _assert_error(response, 404, "operator_not_found")
    harness.resolver.assert_called_once_with(SUBJECT)
    _assert_no_dispatch(harness)


@pytest.mark.parametrize("route_id", ROUTES)
def test_external_routes_reject_ungranted_profile(harness, route_id):
    response = _request(harness, route_id, profile="forbidden")
    _assert_error(response, 404, "operator_not_found")
    harness.resolver.assert_called_once_with(SUBJECT)
    _assert_no_dispatch(harness)


@pytest.mark.parametrize("route_id", ROUTES)
def test_external_routes_fail_closed_when_principal_store_unavailable(harness, route_id):
    marker = "synthetic-principal-store-canary"
    harness.resolver.side_effect = RuntimeError(marker)
    response = _request(harness, route_id)
    _assert_error(response, 503, "operator_store_unavailable")
    assert marker not in response.text
    harness.resolver.assert_called_once_with(SUBJECT)
    _assert_no_dispatch(harness)


def test_external_destinations_returns_metadata_envelope(harness):
    response = _request(harness, "destinations")
    assert response.status_code == 200, response.text
    assert response.json() == {"destinations": harness.destinations}
    for metadata in response.json()["destinations"].values():
        assert set(metadata) == {"destination", "environment", "adapter"}
    harness.spies["destinations"].assert_called_once_with()
    harness.resolver.assert_called_once_with(SUBJECT)
    _assert_one_dispatch(harness, "destinations")


@pytest.mark.parametrize("payload_present", [True, False],
                         ids=["payload-present", "payload-omitted"])
def test_external_propose_dispatches_validated_body(harness, payload_present):
    body = _body("propose", payload_present)
    response = _request(harness, "propose", body=body)
    assert response.status_code == 200, response.text
    assert set(response.json()) == {
        "authority_id", "action", "destination", "adapter", "token_handle",
        "environment", "expires_at",
    }
    assert response.json() == harness.proposal
    harness.resolver.assert_called_once_with(SUBJECT)
    assert harness.spies["propose"].call_args_list == [call(
        harness.context, "fixture-destination", "fixture-handle",
        "fixture-adapter", body.get("payload"))]
    _assert_one_dispatch(harness, "propose")


@pytest.mark.parametrize("payload_present", [True, False],
                         ids=["payload-present", "payload-omitted"])
def test_external_execute_dispatches_authority_and_payload(harness, payload_present):
    body = _body("execute", payload_present)
    response = _request(harness, "execute", body=body)
    assert response.status_code == 200, response.text
    assert set(response.json()) == {
        "action", "destination", "adapter", "authority_id", "environment", "reversal",
    }
    assert response.json()["reversal"] == {
        "adapter_reversal": "fixture reversal instructions"}
    assert response.json() == harness.execution
    harness.resolver.assert_called_once_with(SUBJECT)
    assert harness.spies["execute"].call_args_list == [call(
        harness.context, "fixture-destination", "fixture-handle",
        "fixture-adapter", "fixture-authority", body.get("payload"))]
    _assert_one_dispatch(harness, "execute")


@pytest.mark.parametrize("route_id", WRITE_ROUTES)
def test_external_write_routes_reject_extra_fields(harness, route_id):
    body = _body(route_id)
    body["unexpected"] = "fixture-extra"
    response = _request(harness, route_id, body=body)
    _assert_validation(response, "unexpected")
    _assert_no_dispatch(harness)


def test_external_execute_requires_authority_id(harness):
    body = _body("execute")
    del body["authority_id"]
    response = _request(harness, "execute", body=body)
    _assert_validation(response, "authority_id")
    _assert_no_dispatch(harness)


@pytest.mark.parametrize("route_id", WRITE_ROUTES)
@pytest.mark.parametrize("error_type,reason", [
    (RunbookError, "no_adapter"),
    (operator_authority.AuthorityDenied, "authority_replayed"),
])
def test_external_write_routes_map_domain_conflicts(harness, route_id, error_type, reason):
    harness.spies[route_id].side_effect = error_type(reason)
    response = _request(harness, route_id)
    _assert_error(response, 409, reason)
    _assert_one_dispatch(harness, route_id)


@pytest.mark.parametrize("route_id", ROUTES)
def test_external_routes_mask_unexpected_backend_errors(harness, route_id):
    marker = "synthetic-backend-canary"
    harness.spies[route_id].side_effect = RuntimeError(marker)
    response = _request(harness, route_id)
    _assert_error(response, 503, "operator_runbook_unavailable")
    assert marker not in response.text
    _assert_one_dispatch(harness, route_id)


@pytest.mark.parametrize("route_id", ROUTES)
def test_external_routes_preserve_http_exception(harness, route_id):
    harness.spies[route_id].side_effect = HTTPException(429, "fixture_throttled")
    response = _request(harness, route_id)
    _assert_error(response, 429, "fixture_throttled")
    _assert_one_dispatch(harness, route_id)
