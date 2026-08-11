"""Focused route and mount contracts for the identity pair operator rail."""
from __future__ import annotations

import ast
import json
import sys
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parents[1]
ROOT = SERVER_DIR.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from operator_deps import OperatorContext  # noqa: E402
from routers import operator_identity_bindings as route  # noqa: E402


class PairConflict(ValueError):
    pass


class PrincipalDrift(ValueError):
    pass


class FakeStore:
    IdentityBindingPairConflict = PairConflict
    IdentityBindingPairPrincipalDrift = PrincipalDrift

    def __init__(self):
        self.calls = []

    def bind_identity_pair(self, bindings, **kwargs):
        self.calls.append((bindings, kwargs))
        return {"bindings": [
            {"tenant_id": item["tenant_id"], "binding_id": uuid.uuid4(),
             "role": "read_only", "state": "created"}
            for item in bindings
        ]}


@pytest.fixture
def harness(monkeypatch):
    fake = FakeStore()
    app = FastAPI()
    app.include_router(route.router)
    operator = OperatorContext(
        subject="auth0|operator", role="operator", role_revision=7,
        profiles=("default",), environment="staging", profile="default")
    app.dependency_overrides[route.operator_deps.require_operator] = lambda: operator
    monkeypatch.setattr(route.platform_link, "platform_store", lambda: fake)
    return TestClient(app, raise_server_exceptions=False), fake, operator


def _body(subject_a="auth0|synthetic-a", subject_b="auth0|synthetic-b"):
    return {
        "environment": "staging",
        "bindings": [
            {"tenant_id": str(uuid.uuid4()), "subject": subject_a,
             "role": "read_only"},
            {"tenant_id": str(uuid.uuid4()), "subject": subject_b,
             "role": "read_only"},
        ],
    }


def test_valid_pair_passes_verified_operator_context_without_echo(harness, caplog):
    client, fake, operator = harness
    key = "pair-request-123"
    body = _body()
    response = client.post(
        "/api/operator/identity-bindings/pair", json=body,
        headers={"Idempotency-Key": key})

    assert response.status_code == 200, response.text
    payload = response.json()
    rendered = response.text + caplog.text
    assert body["bindings"][0]["subject"] not in rendered
    assert body["bindings"][1]["subject"] not in rendered
    assert key not in rendered
    assert payload["role"] == "read_only"
    _, kwargs = fake.calls[0]
    assert kwargs == {
        "operator_subject": operator.subject,
        "operator_role_revision": operator.role_revision,
        "environment": operator.environment,
        "idempotency_key": key,
    }


@pytest.mark.parametrize("mutation", [
    lambda body: body.update(extra="rejected"),
    lambda body: body["bindings"][0].update(role="owner"),
    lambda body: body.update(environment="production"),
    lambda body: body["bindings"].__setitem__(1, body["bindings"][0].copy()),
])
def test_strict_request_and_environment_reject_before_store(harness, mutation):
    client, fake, _operator = harness
    body = _body()
    mutation(body)
    response = client.post(
        "/api/operator/identity-bindings/pair", json=body,
        headers={"Idempotency-Key": "pair-request-123"})
    assert response.status_code in {404, 422}
    assert fake.calls == []


def test_conflict_is_generic_and_does_not_echo_target(harness, monkeypatch):
    client, fake, _operator = harness
    target = "auth0|conflicting-synthetic"
    monkeypatch.setattr(
        fake, "bind_identity_pair",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(PairConflict(target)))
    response = client.post(
        "/api/operator/identity-bindings/pair", json=_body(target),
        headers={"Idempotency-Key": "pair-conflict-123"})
    assert response.status_code == 409
    assert response.json() == {"detail": "identity_pair_conflict"}
    assert target not in response.text


@pytest.mark.parametrize("case", [
    "oversized_subject",
    "malformed_nested",
    "extra_field",
    "wrong_type",
    "malformed_json",
])
def test_rejected_raw_body_never_echoes_input_or_logs(harness, caplog, case):
    client, fake, _operator = harness
    sentinel = f"SECRET-{case}-{uuid.uuid4().hex}"
    body = _body()
    if case == "oversized_subject":
        body["bindings"][0]["subject"] = "auth0|" + sentinel + ("x" * 300)
        raw = json.dumps(body)
    elif case == "malformed_nested":
        body["bindings"][0] = {"subject": sentinel}
        raw = json.dumps(body)
    elif case == "extra_field":
        body["bindings"][0]["secret_extra"] = sentinel
        raw = json.dumps(body)
    elif case == "wrong_type":
        body["bindings"][0]["subject"] = {"secret": sentinel}
        raw = json.dumps(body)
    else:
        raw = '{"environment":"staging","bindings":["' + sentinel

    caplog.clear()
    response = client.post(
        "/api/operator/identity-bindings/pair", content=raw,
        headers={"Content-Type": "application/json",
                 "Idempotency-Key": "pair-rejected-123"})
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_identity_pair"}
    assert sentinel not in response.text
    assert sentinel not in caplog.text
    assert fake.calls == []


def test_dedicated_mount_is_independent_and_required_config_is_durable():
    source = (SERVER_DIR / "app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = {node.name: ast.get_source_segment(source, node)
                 for node in tree.body if isinstance(node, ast.FunctionDef)}
    identity_mount = functions["_mount_operator_identity_binding_router"]
    global_mount = functions["_mount_operator_router"]
    assert "LEAF_OPERATOR_IDENTITY_BINDING_ENABLED" in identity_mount
    assert "LEAF_OPERATOR_ENABLED" not in identity_mount
    assert "operator_identity_bindings" in identity_mount
    assert "LEAF_OPERATOR_IDENTITY_BINDING_ENABLED" not in global_mount
    assert "operator_identity_bindings" not in global_mount
    required = json.loads(
        (ROOT / "deploy" / "required-config.app.json").read_text(encoding="utf-8"))
    assert required["required"]["environment"].count(
        "LEAF_OPERATOR_IDENTITY_BINDING_ENABLED") == 1
