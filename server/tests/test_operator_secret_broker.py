"""Wave 3 gate: operator secret broker.

The broker serves handle METADATA only and injects a short-lived credential
into exactly one call without ever returning or logging the value. Fail-closed:
production scopes and a missing minter are refused. No DB needed (the audit is
best-effort).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import operator_secret_broker as broker  # noqa: E402


@pytest.fixture(autouse=True)
def _no_minter():
    broker.register_minter(None)
    yield
    broker.register_minter(None)


# --- metadata only ----------------------------------------------------------

def test_describe_returns_metadata_never_value():
    meta = broker.describe("github_operator_pr")
    assert meta is not None
    assert meta["scope"] == "github_pr_open_operator_branch"
    assert meta["environment"] == "staging"
    assert "value" not in meta and "token" not in meta and "secret" not in meta


def test_unknown_handle_describe_is_none():
    assert broker.describe("does_not_exist") is None


def test_list_handles_is_metadata_only():
    handles = broker.list_handles()
    assert set(handles) == {"github_operator_pr", "worker_provider"}
    blob = json.dumps(handles)
    for banned in ("value", "token", "secret", "password"):
        assert banned not in blob.lower()


def test_shipped_registry_holds_no_values():
    raw = (SERVER_DIR / "operator_secrets.json").read_text(encoding="utf-8")
    d = json.loads(raw)
    for handle, meta in d["handles"].items():
        assert set(meta) == {"scope", "environment", "kind", "ttl_s"}, handle
        assert meta["environment"] in {"staging", "development"}, handle


# --- registry validation ----------------------------------------------------

def _write_registry(tmp_path, monkeypatch, handles):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"handles": handles}), encoding="utf-8")
    monkeypatch.setenv("LEAF_OPERATOR_SECRETS_FILE", str(p))


def test_registry_rejects_a_value_field(tmp_path, monkeypatch):
    _write_registry(tmp_path, monkeypatch, {"h": {
        "scope": "s", "environment": "staging", "kind": "k",
        "ttl_s": 900, "value": "SECRET"}})
    with pytest.raises(broker.SecretBrokerError):
        broker.list_handles()


def test_registry_rejects_production_environment(tmp_path, monkeypatch):
    _write_registry(tmp_path, monkeypatch, {"h": {
        "scope": "s", "environment": "production", "kind": "k", "ttl_s": 900}})
    with pytest.raises(broker.SecretBrokerError):
        broker.list_handles()


def test_registry_rejects_missing_required_field(tmp_path, monkeypatch):
    # Only environment present: scope/kind/ttl_s omitted must be refused.
    _write_registry(tmp_path, monkeypatch, {"h": {"environment": "staging"}})
    with pytest.raises(broker.SecretBrokerError):
        broker.list_handles()


def test_missing_registry_yields_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_OPERATOR_SECRETS_FILE", str(tmp_path / "none.json"))
    assert broker.list_handles() == {}
    assert broker.describe("github_operator_pr") is None


# --- injection: fail closed + never returns the value -----------------------

def test_inject_without_minter_is_dark():
    with pytest.raises(broker.SecretBrokerError) as e:
        broker.with_injected("github_operator_pr", "staging", lambda c: c)
    assert e.value.reason == "no_minter"


def test_inject_unknown_handle_refused():
    broker.register_minter(lambda meta: "SHORT")
    with pytest.raises(broker.SecretBrokerError) as e:
        broker.with_injected("nope", "staging", lambda c: c)
    assert e.value.reason == "unknown_handle"


def test_inject_production_environment_refused():
    broker.register_minter(lambda meta: "SHORT")
    with pytest.raises(broker.SecretBrokerError) as e:
        broker.with_injected("github_operator_pr", "production", lambda c: c)
    assert e.value.reason == "production_scope_refused"


def test_inject_passes_credential_to_one_call_and_returns_fixed_receipt():
    seen = {}
    broker.register_minter(lambda meta: f"tok-{meta['handle']}-abc")

    def use(credential):
        seen["cred"] = credential
        seen["calls"] = seen.get("calls", 0) + 1

    receipt = broker.with_injected("github_operator_pr", "staging", use)
    cred = "tok-github_operator_pr-abc"
    assert seen["cred"] == cred  # the adapter received the real credential
    assert seen["calls"] == 1    # exactly one call
    # The broker returns a FIXED receipt with no adapter-derived data.
    assert receipt == {"handle": "github_operator_pr",
                       "scope": "github_pr_open_operator_branch",
                       "injected": True}


def test_adapter_return_value_is_discarded_so_it_cannot_leak():
    # The credential cannot escape through the return value no matter what the
    # adapter returns: the broker discards the adapter's return entirely.
    broker.register_minter(lambda meta: "CANARY-CREDENTIAL")
    fixed = {"handle": "github_operator_pr",
             "scope": "github_pr_open_operator_branch", "injected": True}
    hostile_returns = (
        lambda c: c,                       # echo the whole credential
        lambda c: list(c),                 # split it character-by-character
        lambda c: [c[:5], c[5:]],          # split it into two fragments
        lambda c: {c: "x", "ok": True},    # hide it in a dict key
        lambda c: {"nested": [c, {"k": c}]},  # bury it in nested structures
        lambda c: {c},                     # a set (was previously "non-serializable")
        lambda c: c.encode(),              # bytes
        lambda c: object(),                # a custom object
    )
    for hostile in hostile_returns:
        receipt = broker.with_injected("github_operator_pr", "staging", hostile)
        assert receipt == fixed
        assert "CANARY-CREDENTIAL" not in json.dumps(receipt)


def test_adapter_exception_never_leaks_the_credential():
    broker.register_minter(lambda meta: "CANARY-CREDENTIAL")

    def use(credential):
        raise RuntimeError(f"adapter blew up with {credential}")

    with pytest.raises(broker.SecretBrokerError) as e:
        broker.with_injected("github_operator_pr", "staging", use)
    assert e.value.reason == "adapter_failed"
    # The credential is NOT in the error, its cause, OR its retained context.
    assert "CANARY" not in str(e.value)
    assert e.value.__cause__ is None
    assert e.value.__context__ is None


def test_callback_raising_secretbrokererror_with_credential_is_masked():
    broker.register_minter(lambda meta: "CANARY-CREDENTIAL")

    def use(credential):
        raise broker.SecretBrokerError(credential)  # tries to smuggle it out

    with pytest.raises(broker.SecretBrokerError) as e:
        broker.with_injected("github_operator_pr", "staging", use)
    assert e.value.reason == "adapter_failed"
    assert "CANARY" not in str(e.value)
    assert e.value.__cause__ is None
    assert e.value.__context__ is None


def test_base_exception_carrying_the_credential_is_masked():
    # Even a non-Exception BaseException (e.g. a credential-bearing error
    # raised while a hostile return type is materialized) must be masked.
    broker.register_minter(lambda meta: "CANARY-CREDENTIAL")

    class Boom(BaseException):
        pass

    def use(credential):
        raise Boom(credential)

    with pytest.raises(broker.SecretBrokerError) as e:
        broker.with_injected("github_operator_pr", "staging", use)
    assert e.value.reason == "adapter_failed"
    assert "CANARY" not in str(e.value)
    assert e.value.__context__ is None


def test_minter_failure_is_a_denial():
    def bad_minter(meta):
        raise RuntimeError("minter down")
    broker.register_minter(bad_minter)
    with pytest.raises(broker.SecretBrokerError) as e:
        broker.with_injected("github_operator_pr", "staging", lambda c: c)
    assert e.value.reason == "minter_failed"


def test_registry_rejects_nested_value_in_scope(tmp_path, monkeypatch):
    bad = {"handles": {"h": {"scope": {"token": "REGISTRY-CANARY"},
                             "environment": "staging", "kind": "k"}}}
    p = tmp_path / "s.json"
    p.write_text(json.dumps(bad), encoding="utf-8")
    monkeypatch.setenv("LEAF_OPERATOR_SECRETS_FILE", str(p))
    with pytest.raises(broker.SecretBrokerError):
        broker.list_handles()


# --- route registration -----------------------------------------------------

def test_secret_router_registers_metadata_routes_only():
    from fastapi import FastAPI
    from routers import operator_secrets as router_mod

    app = FastAPI()
    app.include_router(router_mod.router)
    paths = {r.path for r in app.routes}
    assert "/api/operator/secrets" in paths
    assert "/api/operator/secrets/{handle}" in paths
    # There is deliberately NO route that injects/returns a credential value.
    assert not any("inject" in p for p in paths)
