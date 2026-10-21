"""HTTP forwarding and auth contracts; fake receipts prove no store semantics."""
from __future__ import annotations

import json
import sys
import uuid
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import operator_principals  # noqa: E402
from routers import operator_identity_bindings as route  # noqa: E402

URL = "/api/operator/identity-bindings/pair"
SUBJECT = "auth0|g3-operator"
KEY = "g3-fixture-key-0001"
MARKER = "g3-private-exception-marker"
TENANTS = (
    uuid.UUID("11111111-1111-4111-8111-111111111111"),
    uuid.UUID("22222222-2222-4222-8222-222222222222"),
)
BINDING_IDS = (
    uuid.UUID("33333333-3333-4333-8333-333333333333"),
    uuid.UUID("44444444-4444-4444-8444-444444444444"),
)


@dataclass(frozen=True)
class TrustedTenant:
    subject: str | None = SUBJECT

    def __str__(self):
        return "g3-trusted-tenant"


class PrincipalDrift(RuntimeError):
    pass


class PairConflict(RuntimeError):
    pass


class FakeStore:
    IdentityBindingPairPrincipalDrift = PrincipalDrift
    IdentityBindingPairConflict = PairConflict

    def __init__(self):
        self.calls = []
        self.error = None

    def bind_identity_pair(self, bindings, **kwargs):
        self.calls.append((bindings, kwargs))
        if self.error is not None:
            raise self.error
        return {"bindings": [
            {"tenant_id": tenant, "binding_id": binding_id,
             "role": "read_only", "state": state}
            for tenant, binding_id, state in zip(
                TENANTS, BINDING_IDS, ("created", "preserved"))
        ]}


def _body():
    return {
        "environment": "staging",
        "bindings": [
            {"tenant_id": str(TENANTS[0]), "subject": "auth0|g3-a",
             "role": "read_only"},
            {"tenant_id": str(TENANTS[1]), "subject": "auth0|g3-b",
             "role": "read_only"},
        ],
    }


@pytest.fixture
def harness(monkeypatch):
    state = SimpleNamespace(
        tenant=TrustedTenant(),
        principal=operator_principals.OperatorPrincipal(
            subject=SUBJECT, role="operator", role_revision=7,
            status="active", profiles=("default",), environment="staging"),
        resolver_error=None, factory_error=None,
        resolutions=[], acquisitions=[], store=FakeStore(),
    )

    def resolve(subject):
        state.resolutions.append(subject)
        if state.resolver_error is not None:
            raise state.resolver_error
        return state.principal

    def acquire(*args, **kwargs):
        state.acquisitions.append((args, kwargs))
        if state.factory_error is not None:
            raise state.factory_error
        return state.store

    app = FastAPI()
    app.include_router(route.router)
    app.dependency_overrides[route.operator_deps.tenant_deps.require_tenant] = (
        lambda: state.tenant)
    monkeypatch.setattr(route.operator_deps.tenant_deps, "auth_live", lambda: True)
    monkeypatch.setattr(operator_principals, "resolve_principal", resolve)
    monkeypatch.setattr(route.platform_link, "platform_store", acquire)
    try:
        with TestClient(app) as client:
            state.client = client
            yield state
    finally:
        app.dependency_overrides.clear()


def _post(harness, *, key=KEY):
    return harness.client.post(URL, json=_body(), headers={"Idempotency-Key": key})


def _assert_error(response, status, detail):
    assert response.status_code == status, response.text
    assert response.json() == {"detail": detail}
    for private in (MARKER, SUBJECT, KEY, "auth0|g3-a", "auth0|g3-b"):
        assert private not in response.text


def _assert_no_store(harness):
    assert harness.acquisitions == []
    assert harness.store.calls == []


def _assert_bind_attempt(harness):
    assert harness.resolutions == [SUBJECT]
    assert harness.acquisitions == [((), {})]
    assert len(harness.store.calls) == 1


@pytest.mark.parametrize("variant", [
    "missing-subject", "absent-principal", "inactive-principal", "ungranted-profile",
], ids=[
    "missing-subject", "absent-principal", "inactive-principal", "ungranted-profile",
])
def test_identity_pair_rejects_missing_inactive_or_ungranted_operator_without_store_access(
    harness, variant,
):
    if variant == "missing-subject":
        harness.tenant = TrustedTenant(subject=None)
    elif variant == "absent-principal":
        harness.principal = None
    elif variant == "inactive-principal":
        harness.principal = replace(harness.principal, status="suspended")
    else:
        harness.principal = replace(harness.principal, profiles=("other",))
    _assert_error(_post(harness), 404, "operator_not_found")
    assert harness.resolutions == ([] if variant == "missing-subject" else [SUBJECT])
    _assert_no_store(harness)


def test_identity_pair_principal_store_outage_is_generic_503(harness):
    harness.resolver_error = RuntimeError(MARKER)
    _assert_error(_post(harness), 503, "operator_store_unavailable")
    assert harness.resolutions == [SUBJECT]
    _assert_no_store(harness)


@pytest.mark.parametrize("variant", ["wrong-role", "environment-mismatch"],
                         ids=["wrong-role", "environment-mismatch"])
def test_identity_pair_requires_operator_role_and_matching_environment(harness, variant):
    if variant == "wrong-role":
        harness.principal = replace(harness.principal, role="read_only")
    else:
        harness.principal = replace(harness.principal, environment="other")
    _assert_error(_post(harness), 404, "operator_not_found")
    assert harness.resolutions == [SUBJECT]
    _assert_no_store(harness)


def test_identity_pair_forwards_verified_identity_and_returns_only_binding_receipt(harness):
    response = _post(harness)
    assert response.status_code == 200, response.text
    assert response.json() == {
        "environment": "staging", "role": "read_only",
        "bindings": [
            {"tenant_id": str(tenant), "binding_id": str(binding_id),
             "role": "read_only", "state": state}
            for tenant, binding_id, state in zip(
                TENANTS, BINDING_IDS, ("created", "preserved"))
        ],
    }
    _assert_bind_attempt(harness)
    bindings, kwargs = harness.store.calls[0]
    assert bindings == [
        {**binding, "tenant_id": tenant}
        for binding, tenant in zip(_body()["bindings"], TENANTS)
    ]
    assert all(isinstance(binding["tenant_id"], uuid.UUID) for binding in bindings)
    assert kwargs == {
        "operator_subject": SUBJECT, "operator_role_revision": 7,
        "environment": "staging", "idempotency_key": KEY,
    }
    for private in (SUBJECT, "auth0|g3-a", "auth0|g3-b", KEY):
        assert private not in response.text
    assert "subject" not in response.text
    assert "idempotency" not in response.text


def test_identity_pair_invalid_idempotency_key_is_exact_422_without_store_access(harness):
    response = _post(harness, key="bad!")
    _assert_error(response, 422, "invalid_idempotency_key")
    assert "bad!" not in response.text
    assert harness.resolutions == [SUBJECT]
    _assert_no_store(harness)


def test_identity_pair_principal_drift_is_404(harness):
    harness.store.error = harness.store.IdentityBindingPairPrincipalDrift(MARKER)
    _assert_error(_post(harness), 404, "operator_not_found")
    _assert_bind_attempt(harness)


def test_identity_pair_conflict_is_409(harness):
    harness.store.error = harness.store.IdentityBindingPairConflict(MARKER)
    _assert_error(_post(harness), 409, "identity_pair_conflict")
    _assert_bind_attempt(harness)


@pytest.mark.parametrize("error_type", [ValueError, KeyError], ids=["ValueError", "KeyError"])
def test_identity_pair_store_validation_failure_is_422(harness, error_type):
    harness.store.error = error_type(MARKER)
    _assert_error(_post(harness), 422, "invalid_identity_pair")
    _assert_bind_attempt(harness)


@pytest.mark.parametrize("stage", ["factory", "bind"], ids=["factory", "bind"])
def test_identity_pair_store_acquisition_or_execution_failure_is_503(harness, stage):
    if stage == "factory":
        harness.factory_error = RuntimeError(MARKER)
    else:
        harness.store.error = RuntimeError(MARKER)
    _assert_error(_post(harness), 503, "operator_store_unavailable")
    assert harness.resolutions == [SUBJECT]
    assert harness.acquisitions == [((), {})]
    assert len(harness.store.calls) == (0 if stage == "factory" else 1)


def test_identity_pair_oversized_body_is_422_without_store_access(harness):
    raw = json.dumps(_body()).encode("ascii")
    raw += b" " * (16_385 - len(raw))
    assert len(raw) == 16_385
    response = harness.client.post(
        URL, content=raw,
        headers={"Idempotency-Key": KEY, "Content-Type": "application/json"})
    _assert_error(response, 422, "invalid_identity_pair")
    assert raw.decode("ascii") not in response.text
    _assert_no_store(harness)
