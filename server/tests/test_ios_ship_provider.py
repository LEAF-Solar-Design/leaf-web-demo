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


@pytest.mark.parametrize("schema", ["leaf.ios-testflight-receipt.v1",
                                    "leaf.ios-testflight-receipt.v2"])
def test_authenticated_callbacks_forward_only_fixed_scope(tmp_path, monkeypatch, schema):
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
    raw = {"schema": schema}
    receipt = client.post(
        f"/internal/v1/ios-ship/executions/{EXECUTION}/receipt",
        json={"org_id": ORG, "tenant_id": "tenant-a", "project_id": PROJECT,
              "execution_id": EXECUTION, "provider_run_id": "run-1", "receipt": raw},
        headers=headers)
    assert receipt.status_code == 200
    assert store.receipts == [(ORG, "tenant-a", PROJECT, EXECUTION, "run-1", raw)]


@pytest.mark.parametrize("receipt", [None, [], "receipt", 1, True])
def test_callback_refuses_non_dict_receipt_before_store(tmp_path, monkeypatch, receipt):
    callback.set_config(_config(tmp_path, monkeypatch))
    store = Store()
    monkeypatch.setattr(callback, "_store", lambda: store)
    response = _client().post(
        f"/internal/v1/ios-ship/executions/{EXECUTION}/receipt",
        json={"org_id": ORG, "tenant_id": "tenant-a", "project_id": PROJECT,
              "execution_id": EXECUTION, "provider_run_id": "run-1", "receipt": receipt},
        headers={"Authorization": f"Bearer {TOKEN}", "X-Leaf-Ios-Ship-Provider": PROVIDER_ID})
    assert response.status_code == 400
    assert store.receipts == []


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
            self.source_catalog = object()

    monkeypatch.setattr(composition.ios_ship_provider_client, "ProviderConfig", FakeConfig)
    monkeypatch.setattr(composition.ios_ship_provider_client, "HttpProviderDispatch", FakeAdapter)
    monkeypatch.setattr(composition.ios_ship, "set_dispatch",
                        lambda value: mounted.append(("dispatch", value)))
    monkeypatch.setattr(composition.ios_ship, "set_provider_readiness",
                        lambda value: mounted.append(("readiness", value)))
    monkeypatch.setattr(composition.ios_ship_provider_router, "set_config",
                        lambda value: mounted.append(("callback", value)))
    monkeypatch.setattr(composition.ios_ship, "set_provider_catalog",
                        lambda value: mounted.append(("catalog", value)))
    composition.initialize_ios_ship_provider()
    assert [name for name, _value in mounted] == ["dispatch", "readiness", "catalog", "callback"]
    assert all(value is not None for _name, value in mounted)
    assert mounted[3][1] is config


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
    monkeypatch.setattr(composition.ios_ship, "set_provider_catalog",
                        lambda value: mounted.append(("catalog", value)))
    composition.initialize_ios_ship_provider()
    assert mounted == [("dispatch", None), ("readiness", None), ("catalog", None), ("callback", None)]


B4A_PROJECT = "c6dbda41-f9bf-4f0f-984c-45f3199f4ca1"


def _b4a_catalog():
    return {"schema": "leaf.ios-ship-source-catalog.v1", "project_id": B4A_PROJECT,
            "catalog_key": "exzachly", "status": "ok", "sources": [{
                "source_revision": "c76380846278cdfa4ffcb71b031dce33c7f139f0",
                "source_sha256": "41f1cd4e5bee84238973ea785c23e49888edd5154f385254c7781813bc6065c6",
                "producer_receipt_digest": "ce8186d075b4ca8877560442dba99777558e1385f5b5ae52256f8102fed193b3",
                "bundle_identifier": "com.exzachly.app", "marketing_version": "0.2.0",
                "build_number": "12", "repository": "https://github.com/Evan-Haug/ExZachly.git"}],
            "unpinned": [], "refused": []}


def test_b4a_catalog_get_contract(tmp_path, monkeypatch):
    config = _config(tmp_path, monkeypatch)
    calls = []
    payload = _b4a_catalog()

    def get(*args, **kwargs):
        calls.append((args, kwargs))
        return Response(payload)

    assert provider.HttpProviderDispatch(config, get=get).source_catalog(B4A_PROJECT) == payload
    assert calls == [((config.base_url + provider.SOURCE_CATALOG_PATH,), {
        "headers": {"Authorization": f"Bearer {TOKEN}"}, "params": {"project_id": B4A_PROJECT},
        "timeout": 3.5, "verify": str(config.ca_file)})]


def test_b4a_catalog_scope_mismatch(tmp_path, monkeypatch):
    payload = _b4a_catalog()
    payload["project_id"] = "5ec5345a-0d85-4c3f-80e2-8ab99ae25c32"
    adapter = provider.HttpProviderDispatch(_config(tmp_path, monkeypatch),
                                           get=lambda *a, **k: Response(payload))
    with pytest.raises(provider.ProviderCatalogError):
        adapter.source_catalog(B4A_PROJECT)


@pytest.mark.parametrize("change", ["missing", "extra", "source_extra", "status"])
def test_b4a_catalog_exact_shape(tmp_path, monkeypatch, change):
    payload = _b4a_catalog()
    if change == "missing":
        del payload["refused"]
    elif change == "extra":
        payload["extra"] = "no"
    elif change == "source_extra":
        payload["sources"][0]["extra"] = "no"
    else:
        payload["status"] = "ready"
    adapter = provider.HttpProviderDispatch(_config(tmp_path, monkeypatch),
                                           get=lambda *a, **k: Response(payload))
    with pytest.raises(provider.ProviderCatalogError):
        adapter.source_catalog(B4A_PROJECT)


@pytest.mark.parametrize("key,value", [("repository", "https://host/-----BEGIN material"),
                                       ("Authorization", "private")])
def test_b4a_catalog_secret_refusal(tmp_path, monkeypatch, key, value):
    payload = _b4a_catalog()
    payload["sources"][0][key] = value
    adapter = provider.HttpProviderDispatch(_config(tmp_path, monkeypatch),
                                           get=lambda *a, **k: Response(payload))
    with pytest.raises(provider.ProviderCatalogError) as exc:
        adapter.source_catalog(B4A_PROJECT)
    assert str(exc.value) == ""


@pytest.mark.parametrize("failure", ["get", "503", "json"])
def test_b4a_catalog_transport_is_sanitized(tmp_path, monkeypatch, failure):
    class BrokenResponse(Response):
        def json(self):
            raise ValueError(TOKEN)

    def get(*a, **k):
        if failure == "get":
            raise RuntimeError(TOKEN)
        return Response({}, 503) if failure == "503" else BrokenResponse(None)

    adapter = provider.HttpProviderDispatch(_config(tmp_path, monkeypatch), get=get)
    with pytest.raises(provider.ProviderCatalogError) as exc:
        adapter.source_catalog(B4A_PROJECT)
    assert str(exc.value) == ""


@pytest.mark.parametrize("project", ["not-a-uuid", None, 12])
def test_b4a_catalog_invalid_project_never_calls_provider(tmp_path, monkeypatch, project):
    calls = []
    adapter = provider.HttpProviderDispatch(_config(tmp_path, monkeypatch),
                                           get=lambda *a, **k: calls.append(True))
    with pytest.raises(provider.ProviderCatalogError):
        adapter.source_catalog(project)
    assert calls == []


def test_b4a_composition_missing_catalog_fails_closed(monkeypatch):
    import app as composition

    monkeypatch.setattr(composition.ios_ship_provider_client.ProviderConfig,
                        "from_environment", lambda: None)
    mounted = []
    monkeypatch.setattr(composition.ios_ship, "set_provider_catalog", mounted.append)
    composition.initialize_ios_ship_provider()
    assert mounted == [None]
