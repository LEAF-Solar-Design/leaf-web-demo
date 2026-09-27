"""Database-free contract checks for the native binding-grant issuer."""
import ast
from contextlib import nullcontext
import json
from pathlib import Path
import uuid

import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from leaf_platform import binding_grant_issuance as issuance
from leaf_platform.binding_grant import LocalTestSigner, verify_grant
from leaf_platform.counters import CounterResult


def body():
    return {"pluginSessionId": str(uuid.uuid4()), "documentFingerprint": "sha256:" + "a" * 64}


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(issuance.router, prefix="/api")
    app.dependency_overrides[issuance.live_actor] = lambda: issuance.Actor(uuid.uuid4(), uuid.uuid4())
    app.dependency_overrides[issuance.configured_signer] = lambda: None
    return TestClient(app)


def url():
    return f"/api/projects/{uuid.uuid4()}/drawing-versions/{uuid.uuid4()}/binding-grants"


@pytest.mark.parametrize("changes", [
    {"extra": "not allowed"}, {"pluginSessionId": "bad"},
    {"pluginSessionId": "00000000-0000-0000-0000-000000000000"},
    {"pluginSessionId": "AAAAAAAA-AAAA-AAAA-AAAA-AAAAAAAAAAAA"},
    {"pluginSessionId": uuid.uuid4().hex}, {"pluginSessionId": 123},
    {"documentFingerprint": "sha256:" + "A" * 64},
    {"documentFingerprint": "sha256:" + "a" * 63},
    {"documentFingerprint": "a" * 64},
])
def test_invalid_body_is_422(client, changes):
    assert client.post(url(), json={**body(), **changes}).status_code == 422


def test_uuid_has_no_version_restriction():
    payload = body()
    payload["pluginSessionId"] = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert issuance.BindingGrantBody(**payload).pluginSessionId == payload["pluginSessionId"]


def test_auth_off_refuses_even_with_org_hints(client, monkeypatch):
    client.app.dependency_overrides.pop(issuance.live_actor)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    response = client.post(url(), json=body(), headers={
        "X-Org-Id": str(uuid.uuid4()), "X-Actor-Binding-Id": str(uuid.uuid4()),
    })
    assert response.status_code == 503
    assert response.json()["detail"] == "binding_grant_issuance_disabled"
    assert response.headers["cache-control"] == "no-store"


def test_disabled_signer(client, monkeypatch):
    client.app.dependency_overrides.pop(issuance.configured_signer)
    monkeypatch.delenv("LEAF_BINDING_GRANT_SIGNER", raising=False)
    response = client.post(url(), json=body())
    assert response.status_code == 503
    assert response.json()["detail"] == "binding_grant_issuance_disabled"


def test_invalid_auth_is_401(client, monkeypatch):
    client.app.dependency_overrides.pop(issuance.live_actor)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    def invalid(_):
        raise HTTPException(401, "invalid")
    monkeypatch.setattr(issuance.deps, "_verified_identity", invalid)
    assert client.post(url(), json=body()).status_code == 401


class MemoryCounters:
    def __init__(self):
        self.values = {}

    def consume_in_transaction(self, conn, *, namespace, key, limit):
        identity = namespace, key
        value = self.values.get(identity, 0)
        accepted = value < limit
        value += int(accepted)
        self.values[identity] = value
        return CounterResult(accepted, value, limit)


@pytest.mark.parametrize("dimension,limit", [("actor", 10), ("session", 3), ("workspace", 100)])
def test_rate_arithmetic(dimension, limit):
    counters = MemoryCounters()
    actor = issuance.Actor(uuid.uuid4(), uuid.uuid4())
    session = str(uuid.uuid4())
    def consume():
        subject = issuance.Actor(actor.org_id, uuid.uuid4()) if dimension == "workspace" else actor
        plugin_session = session if dimension == "session" else str(uuid.uuid4())
        issuance.consume_limits(None, subject, plugin_session, 125, counters)
    for _ in range(limit):
        consume()
    with pytest.raises(HTTPException) as caught:
        consume()
    assert caught.value.status_code == 429
    assert caught.value.headers["Retry-After"] == "55"
    issuance.consume_limits(None, actor, session, 180, counters)


@pytest.fixture
def signing_config():
    return issuance.SigningConfig(LocalTestSigner(
        rsa.generate_private_key(public_exponent=65537, key_size=3072)), "https://binding.test/")


class FakeConnection:
    def __init__(self):
        self.audit = None
        self.committed = False
        self.row = {"workspace_name": "Workspace", "project_name": "Project",
                    "drawing_id": uuid.uuid4(), "drawing_name": "Drawing", "seq": 7,
                    "role": "owner"}

    def execute(self, query, values):
        if "INSERT INTO binding_grant_audit" in query:
            self.audit = dict(values)
        self.result = self.row if query == issuance._TARGET_SQL else {"binding_id": uuid.uuid4()}
        return self

    def fetchone(self):
        return self.result

    def transaction(self):
        return nullcontext()


def fake_decision(monkeypatch):
    conn = FakeConnection()
    def run(operation, **kwargs):
        result = operation(conn)
        conn.committed = True
        return result
    monkeypatch.setattr(issuance.db, "run_transaction", run)
    monkeypatch.setattr(issuance, "consume_limits", lambda *args: None)
    return conn


def test_response_audit_before_signing_and_no_grant_logging(monkeypatch, signing_config, caplog):
    conn = fake_decision(monkeypatch)
    original = signing_config.signer.sign
    def sign(message):
        assert conn.committed and conn.audit["decision"] == "authorized"
        return original(message)
    monkeypatch.setattr(signing_config.signer, "sign", sign)
    response = issuance.issue_grant(
        issuance.Actor(uuid.uuid4(), uuid.uuid4()), uuid.uuid4(), uuid.uuid4(),
        issuance.BindingGrantBody(**body()), signing_config, clock=lambda: 1000,
    )
    assert response.headers["cache-control"] == "no-store"
    payload = json.loads(response.body)
    assert set(payload) == {"grant", "expiresAt"}
    claims = verify_grant(payload["grant"], signing_config.signer.private_key.public_key(),
                          now=1000, expected_iss=signing_config.issuer)
    assert claims.versionName == "Version 7"
    assert claims.exp == 1120
    assert payload["expiresAt"] == "1970-01-01T00:18:40Z"
    assert all(payload["grant"] not in record.getMessage() for record in caplog.records)
    # No logging sink in the issuer, including an inactive logging branch.
    tree = ast.parse(Path(issuance.__file__).read_text(encoding="utf-8"))
    assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                   and n.func.attr in {"debug", "info", "warning", "error", "exception", "log"}
                   for n in ast.walk(tree))


def test_audit_failure_never_signs(monkeypatch, signing_config):
    fake_decision(monkeypatch)
    def fail(*args):
        raise RuntimeError("audit down")
    monkeypatch.setattr(issuance, "_audit", fail)
    monkeypatch.setattr(signing_config.signer, "sign", lambda _: pytest.fail("signed without audit"))
    with pytest.raises(HTTPException) as caught:
        issuance.issue_grant(issuance.Actor(uuid.uuid4(), uuid.uuid4()), uuid.uuid4(), uuid.uuid4(),
                             issuance.BindingGrantBody(**body()), signing_config)
    assert caught.value.status_code == 503


def test_signing_failure_returns_no_grant(monkeypatch, signing_config):
    conn = fake_decision(monkeypatch)
    def fail(_):
        raise RuntimeError("signer unavailable")
    monkeypatch.setattr(signing_config.signer, "sign", fail)
    with pytest.raises(HTTPException) as caught:
        issuance.issue_grant(issuance.Actor(uuid.uuid4(), uuid.uuid4()), uuid.uuid4(), uuid.uuid4(),
                             issuance.BindingGrantBody(**body()), signing_config)
    assert caught.value.status_code == 503
    assert caught.value.detail == "binding_grant_signing_unavailable"
    assert conn.committed


def test_legacy_bad_name_requires_rename(monkeypatch, signing_config):
    conn = fake_decision(monkeypatch)
    conn.row["drawing_name"] = "misleading\u202e label"
    with pytest.raises(HTTPException) as caught:
        issuance.issue_grant(issuance.Actor(uuid.uuid4(), uuid.uuid4()), uuid.uuid4(), uuid.uuid4(),
                             issuance.BindingGrantBody(**body()), signing_config)
    assert caught.value.status_code == 422
    assert caught.value.detail == "binding_grant_rename_required"
    assert conn.committed and conn.audit["decision"] == "refused"
