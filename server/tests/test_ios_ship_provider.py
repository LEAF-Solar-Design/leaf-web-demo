"""Provider HTTP boundary and callback authentication tests."""
from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import ios_ship_provider as provider
from routers import ios_ship_provider as callback


ORG = str(uuid.uuid4())
PROJECT = str(uuid.uuid4())
EXECUTION = str(uuid.uuid4())
TOKEN = "provider-boundary-token"
PROVIDER_ID = "ios-ship-provider-staging"


class Response:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("provider response must not escape")

    def json(self):
        return self.payload


def _config(tmp_path, monkeypatch, *, secure=True):
    path = tmp_path / "provider.token"
    path.write_text(TOKEN, encoding="utf-8")
    ca_path = tmp_path / "provider-ca.pem"
    ca_path.write_text("public test CA", encoding="utf-8")
    monkeypatch.setattr(provider, "_is_private_token_file", lambda value: secure and value == path)
    return provider.ProviderConfig("https://ios-provider.internal", path, ca_path, PROVIDER_ID,
                                   timeout_seconds=3.5)


def test_http_dispatch_posts_exact_sanitized_intent_and_contract(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    calls = []

    def post(*args, **kwargs):
        calls.append((args, kwargs))
        return Response({"status": "dispatched", "provider_run_id": "run-1"})

    intent = {"kind": "leaf.ios-ship-execution.v1", "execution_id": EXECUTION,
              "org_id": ORG, "tenant_id": "tenant-a", "project_id": PROJECT,
              "source_sha256": "a" * 64}
    result = provider.HttpProviderDispatch(config, post=post).dispatch(intent)
    assert result == {"status": "dispatched", "provider_run_id": "run-1"}
    args, kwargs = calls[0]
    assert args == ("https://ios-provider.internal/v1/ios-ship/executions",)
    assert kwargs == {
        "headers": {"Authorization": f"Bearer {TOKEN}",
                    "Content-Type": "application/json", "Idempotency-Key": EXECUTION},
        "json": intent, "timeout": 3.5,
        "verify": str(config.ca_file),
    }


def _readiness(scope, **overrides):
    payload = {
        "schema": "leaf.ios-ship-provider-readiness.v1", "scope": scope,
        "healthy": True, "reported_at": "2026-08-13T12:00:00+00:00",
        "grant_id": str(uuid.uuid4()), "grant_revision": "7",
        "grant_expires_at": "2026-08-14T12:00:00+00:00", "setup_action": None,
        "dispatch_available": True,
    }
    payload.update(overrides)
    return payload


def test_http_readiness_gets_only_the_exact_revision_tuple(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    calls = []
    scope = {"tenant_id": "tenant-a", "project_id": PROJECT, "source_revision": "83bbde1",
             "source_sha256": "a" * 64, "bundle_id": "com.leaf.soundbeam",
             "marketing_version": "1.2", "build_number": "19"}

    def get(*args, **kwargs):
        calls.append((args, kwargs))
        return Response(_readiness(scope))

    result = provider.HttpProviderDispatch(config, get=get).readiness(scope)
    assert result["healthy"] is True
    args, kwargs = calls[0]
    assert args == ("https://ios-provider.internal/v1/readiness",)
    assert kwargs == {"headers": {"Authorization": f"Bearer {TOKEN}"}, "params": scope,
                      "timeout": 3.5, "verify": str(config.ca_file)}


@pytest.mark.parametrize("payload", [
    {"password": "no"},
    {"schema": "leaf.ios-ship-provider-readiness.v1"},
])
def test_http_readiness_rejects_secret_or_incomplete_response(tmp_path, monkeypatch, payload):
    config = _config(tmp_path, monkeypatch)
    scope = {"tenant_id": "tenant-a", "project_id": PROJECT, "source_revision": "83bbde1",
             "source_sha256": "a" * 64, "bundle_id": "com.leaf.soundbeam",
             "marketing_version": "1.2", "build_number": "19"}
    adapter = provider.HttpProviderDispatch(config, get=lambda *_a, **_k: Response(payload))
    with pytest.raises(provider.ProviderReadinessError):
        adapter.readiness(scope)


def test_http_readiness_rejects_tuple_drift(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    scope = {"tenant_id": "tenant-a", "project_id": PROJECT, "source_revision": "83bbde1",
             "source_sha256": "a" * 64, "bundle_id": "com.leaf.soundbeam",
             "marketing_version": "1.2", "build_number": "19"}
    drifted = dict(scope, build_number="20")
    adapter = provider.HttpProviderDispatch(config, get=lambda *_a, **_k: Response(_readiness(drifted)))
    with pytest.raises(provider.ProviderReadinessError):
        adapter.readiness(scope)


@pytest.mark.parametrize("payload", [
    {"status": "dispatched", "provider_run_id": "run-1", "message": "no"},
    {"status": "succeeded", "provider_run_id": "run-1"},
    {"status": "failed", "provider_run_id": "run-1"},
    {"status": "running"},
])
def test_http_dispatch_rejects_nonfixed_responses(tmp_path, monkeypatch, payload):
    config = _config(tmp_path, monkeypatch)
    adapter = provider.HttpProviderDispatch(config, post=lambda *_a, **_k: Response(payload))
    with pytest.raises(provider.ProviderDispatchError):
        adapter.dispatch({"execution_id": EXECUTION})


def test_http_dispatch_never_sends_secret_shaped_input(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    calls = []
    adapter = provider.HttpProviderDispatch(
        config, post=lambda *_a, **_k: calls.append(True) or Response({}))
    with pytest.raises(provider.ProviderDispatchError):
        adapter.dispatch({"execution_id": EXECUTION, "apple_token": "not-allowed"})
    assert calls == []


def test_http_error_is_replaced_with_secret_free_ambiguous_outcome(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)

    def post(*_args, **_kwargs):
        raise RuntimeError(f"provider leaked {TOKEN}")

    with pytest.raises(provider.ProviderDispatchError) as exc:
        provider.HttpProviderDispatch(config, post=post).dispatch({"execution_id": EXECUTION})
    assert str(exc.value) == "provider admission outcome is unavailable"
    assert TOKEN not in str(exc.value)


def test_environment_config_fails_closed_when_missing_or_insecure(tmp_path, monkeypatch):
    monkeypatch.delenv("LEAF_IOS_SHIP_PROVIDER_URL", raising=False)
    monkeypatch.delenv("LEAF_IOS_SHIP_PROVIDER_TOKEN_FILE", raising=False)
    assert provider.ProviderConfig.from_environment() is None
    path = tmp_path / "provider.token"
    path.write_text(TOKEN, encoding="utf-8")
    monkeypatch.setenv("LEAF_IOS_SHIP_PROVIDER_URL", "https://provider.internal")
    monkeypatch.setenv("LEAF_IOS_SHIP_PROVIDER_TOKEN_FILE", str(path))
    monkeypatch.setenv("LEAF_IOS_SHIP_PROVIDER_CA_FILE", str(tmp_path / "missing-ca.pem"))
    monkeypatch.setenv("LEAF_IOS_SHIP_PROVIDER_ID", PROVIDER_ID)
    monkeypatch.setattr(provider, "_is_private_token_file", lambda _path: False)
    assert provider.ProviderConfig.from_environment() is None


class Store:
    def __init__(self):
        self.progress = []
        self.receipts = []

    def record_provider_progress(self, *args, **kwargs):
        self.progress.append((args, kwargs))
        return {"execution_id": EXECUTION, "status": "running"}

    def record_provider_receipt(self, *args):
        self.receipts.append(args)
        return {"receipt_id": str(uuid.uuid4()), "kind": "leaf.ios-testflight-receipt.v1"}


def _client():
    app = FastAPI()
    app.include_router(callback.router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _reset_callback():
    callback.set_config(None)
    yield
    callback.set_config(None)


def test_callback_missing_and_wrong_token_have_same_denial(tmp_path, monkeypatch):
    callback.set_config(_config(tmp_path, monkeypatch))
    body = {"org_id": ORG, "tenant_id": "tenant-a", "project_id": PROJECT,
            "execution_id": EXECUTION, "provider_run_id": "run-1", "status": "running"}
    missing = _client().post(
        f"/internal/v1/ios-ship/executions/{EXECUTION}/progress", json=body)
    wrong = _client().post(
        f"/internal/v1/ios-ship/executions/{EXECUTION}/progress", json=body,
        headers={"Authorization": "Bearer wrong", "X-Leaf-Ios-Ship-Provider": PROVIDER_ID})
    assert missing.status_code == wrong.status_code == 401
    assert missing.json() == wrong.json()
    assert TOKEN not in missing.text + wrong.text


def test_callback_requires_the_configured_private_provider_identity(tmp_path, monkeypatch):
    callback.set_config(_config(tmp_path, monkeypatch))
    body = {"org_id": ORG, "tenant_id": "tenant-a", "project_id": PROJECT,
            "execution_id": EXECUTION, "provider_run_id": "run-1", "status": "running"}
    response = _client().post(
        f"/internal/v1/ios-ship/executions/{EXECUTION}/progress", json=body,
        headers={"Authorization": f"Bearer {TOKEN}",
                 "X-Leaf-Ios-Ship-Provider": "wrong-provider"})
    assert response.status_code == 401
    assert response.json()["error"]["error_code"] == "provider_unauthorized"


def test_callback_rejects_ungrammatical_provider_controlled_scope(tmp_path, monkeypatch):
    callback.set_config(_config(tmp_path, monkeypatch))
    store = Store()
    monkeypatch.setattr(callback, "_store", lambda: store)
    body = {"org_id": ORG, "tenant_id": "tenant a", "project_id": PROJECT,
            "execution_id": EXECUTION, "provider_run_id": "run-1\nleak", "status": "running"}
    response = _client().post(
        f"/internal/v1/ios-ship/executions/{EXECUTION}/progress", json=body,
        headers={"Authorization": f"Bearer {TOKEN}",
                 "X-Leaf-Ios-Ship-Provider": PROVIDER_ID})
    assert response.status_code == 400
    assert store.progress == []


def test_authenticated_callbacks_forward_only_fixed_scope(tmp_path, monkeypatch):
    callback.set_config(_config(tmp_path, monkeypatch))
    store = Store()
    monkeypatch.setattr(callback, "_store", lambda: store)
    client = _client()
    headers = {"Authorization": f"Bearer {TOKEN}", "X-Leaf-Ios-Ship-Provider": PROVIDER_ID}
    progress = {"org_id": ORG, "tenant_id": "tenant-a", "project_id": PROJECT,
                "execution_id": EXECUTION, "provider_run_id": "run-1", "status": "PENDING_RELEASE",
                "stage": "MAC_RELEASED"}
    response = client.post(
        f"/internal/v1/ios-ship/executions/{EXECUTION}/progress",
        json=progress, headers=headers)
    assert response.status_code == 200
    assert store.progress == [((ORG, "tenant-a", PROJECT, EXECUTION, "run-1"),
                               {"status": "PENDING_RELEASE", "stage": "MAC_RELEASED"})]
    raw = {"schema": "leaf.ios-testflight-receipt.v1"}
    receipt = client.post(
        f"/internal/v1/ios-ship/executions/{EXECUTION}/receipt",
        json={"org_id": ORG, "tenant_id": "tenant-a", "project_id": PROJECT,
              "execution_id": EXECUTION, "provider_run_id": "run-1", "receipt": raw},
        headers=headers)
    assert receipt.status_code == 200
    assert store.receipts == [(ORG, "tenant-a", PROJECT, EXECUTION, "run-1", raw)]


def test_production_composition_mounts_dispatch_and_callback_together(monkeypatch):
    import app as composition

    config = object()
    mounted = []

    class FakeConfig:
        @staticmethod
        def from_environment():
            return config

    class FakeAdapter:
        def __init__(self, value):
            assert value is config
            self.dispatch = object()
            self.readiness = object()

    monkeypatch.setattr(composition.ios_ship_provider_client, "ProviderConfig", FakeConfig)
    monkeypatch.setattr(composition.ios_ship_provider_client, "HttpProviderDispatch", FakeAdapter)
    monkeypatch.setattr(composition.ios_ship, "set_dispatch",
                        lambda value: mounted.append(("dispatch", value)))
    monkeypatch.setattr(composition.ios_ship, "set_provider_readiness",
                        lambda value: mounted.append(("readiness", value)))
    monkeypatch.setattr(composition.ios_ship_provider_router, "set_config",
                        lambda value: mounted.append(("callback", value)))
    composition.initialize_ios_ship_provider()
    assert [name for name, _value in mounted] == ["dispatch", "readiness", "callback"]
    assert mounted[2][1] is config


def test_production_composition_leaves_both_boundaries_fail_closed(monkeypatch):
    import app as composition

    mounted = []

    class MissingConfig:
        @staticmethod
        def from_environment():
            return None

    monkeypatch.setattr(composition.ios_ship_provider_client, "ProviderConfig", MissingConfig)
    monkeypatch.setattr(composition.ios_ship, "set_dispatch",
                        lambda value: mounted.append(("dispatch", value)))
    monkeypatch.setattr(composition.ios_ship, "set_provider_readiness",
                        lambda value: mounted.append(("readiness", value)))
    monkeypatch.setattr(composition.ios_ship_provider_router, "set_config",
                        lambda value: mounted.append(("callback", value)))
    composition.initialize_ios_ship_provider()
    assert mounted == [("dispatch", None), ("readiness", None), ("callback", None)]
