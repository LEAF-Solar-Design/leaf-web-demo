"""Billing secret-id fallback: fake AWS clients, bounded cache, safe logs."""
import builtins
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from types import SimpleNamespace

import pytest

from leaf_platform import billing


SECRET_ID = "leaf/staging/billing-sync"
SECRET = "private-billing-hop-value"


class FakeClient:
    def __init__(self, response=None, error=None):
        self.response = {"SecretString": SECRET} if response is None else response
        self.error = error
        self.calls = []

    def get_secret_value(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.response


@pytest.fixture(autouse=True)
def isolated_secret(monkeypatch):
    monkeypatch.delenv(billing.TIER_SYNC_SECRET_ENV, raising=False)
    monkeypatch.delenv(billing.TIER_SYNC_SECRET_ID_ENV, raising=False)
    monkeypatch.setattr(billing, "_secret_cache", None)
    clock = [1000.0]
    monkeypatch.setattr(billing.time, "monotonic", lambda: clock[0])
    return clock


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeClient()
    monkeypatch.setenv(billing.TIER_SYNC_SECRET_ID_ENV, SECRET_ID)
    monkeypatch.setattr(billing, "_secrets_client_factory", lambda: client)
    return client


def test_direct_env_wins_without_aws(fake_client, monkeypatch):
    monkeypatch.setenv(billing.TIER_SYNC_SECRET_ENV, " direct secret ")
    assert billing.sync_secret() == " direct secret "
    assert fake_client.calls == []


def test_id_fetch_and_success_ttl(fake_client, isolated_secret, monkeypatch):
    monkeypatch.setenv(billing.TIER_SYNC_SECRET_ENV, "")
    assert billing.sync_secret() == SECRET
    isolated_secret[0] += 299
    assert billing.sync_secret() == SECRET
    assert fake_client.calls == [{"SecretId": SECRET_ID}]
    isolated_secret[0] += 1
    fake_client.response = {"SecretString": "rotated-secret"}
    assert billing.sync_secret() == "rotated-secret"
    assert len(fake_client.calls) == 2


def test_cache_has_only_one_id_entry(fake_client, monkeypatch):
    assert billing.sync_secret() == SECRET
    monkeypatch.setenv(billing.TIER_SYNC_SECRET_ID_ENV, "other-id")
    assert billing.sync_secret() == SECRET
    monkeypatch.setenv(billing.TIER_SYNC_SECRET_ID_ENV, SECRET_ID)
    assert billing.sync_secret() == SECRET
    assert fake_client.calls == [
        {"SecretId": SECRET_ID}, {"SecretId": "other-id"}, {"SecretId": SECRET_ID},
    ]


@pytest.mark.parametrize("error_type", [RuntimeError, TimeoutError])
def test_failure_negative_cache_and_safe_log(
    fake_client, isolated_secret, caplog, error_type,
):
    fake_client.error = error_type(SECRET)
    assert billing.sync_secret() == ""
    isolated_secret[0] += 29
    assert billing.sync_secret() == ""
    assert len(fake_client.calls) == 1
    assert SECRET not in caplog.text
    assert caplog.messages == [f"{error_type.__name__} {SECRET_ID}"]
    isolated_secret[0] += 1
    fake_client.error = None
    assert billing.sync_secret() == SECRET
    assert len(fake_client.calls) == 2
    assert SECRET not in caplog.text


@pytest.mark.parametrize("secret_id", ["", " ", "bad id", "id\n", "id\x01", "id\x7f", "x" * 513])
def test_invalid_id_never_fetches(fake_client, monkeypatch, secret_id):
    monkeypatch.setenv(billing.TIER_SYNC_SECRET_ID_ENV, secret_id)
    assert billing.sync_secret() == ""
    assert fake_client.calls == []


@pytest.mark.parametrize("response", [{}, {"SecretString": ""}, {"SecretString": None}, {"SecretString": 7}, {"SecretBinary": b"hidden"}])
def test_invalid_secret_string_fails_closed(fake_client, caplog, response):
    fake_client.response = response
    assert billing.sync_secret() == ""
    assert billing.sync_secret() == ""
    assert len(fake_client.calls) == 1
    assert caplog.messages == [f"ValueError {SECRET_ID}"]


def test_boto3_missing_fails_closed(monkeypatch, caplog):
    monkeypatch.setenv(billing.TIER_SYNC_SECRET_ID_ENV, SECRET_ID)
    original_import = builtins.__import__

    def missing_boto3(name, *args, **kwargs):
        if name == "boto3":
            raise ImportError(SECRET)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_boto3)
    assert billing.sync_secret() == ""
    assert caplog.messages == [f"ImportError {SECRET_ID}"]
    assert SECRET not in caplog.text


def test_default_factory_uses_bounded_client_config(monkeypatch):
    observed = {}
    client = FakeClient()

    def config(**kwargs):
        observed["config"] = kwargs
        return kwargs

    def make_client(service, **kwargs):
        observed["service"] = service
        observed["client_kwargs"] = kwargs
        return client

    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=make_client))
    monkeypatch.setitem(sys.modules, "botocore", SimpleNamespace(__path__=[]))
    monkeypatch.setitem(sys.modules, "botocore.config", SimpleNamespace(Config=config))
    monkeypatch.setenv(billing.TIER_SYNC_SECRET_ID_ENV, SECRET_ID)
    assert billing.sync_secret() == SECRET
    assert observed["service"] == "secretsmanager"
    assert observed["config"] == {
        "connect_timeout": 2, "read_timeout": 3,
        "retries": {"max_attempts": 2, "mode": "standard"},
    }
    assert observed["client_kwargs"] == {"config": observed["config"]}


def test_concurrent_callers_share_fetch(fake_client, monkeypatch):
    entered = Event()
    release = Event()
    second_started = Event()
    fetch = fake_client.get_secret_value

    def blocked_fetch(**kwargs):
        entered.set()
        assert release.wait(5)
        return fetch(**kwargs)

    def second_call():
        second_started.set()
        return billing.sync_secret()

    monkeypatch.setattr(fake_client, "get_secret_value", blocked_fetch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(billing.sync_secret)
        try:
            assert entered.wait(5)
            second = pool.submit(second_call)
            assert second_started.wait(5)
        finally:
            release.set()
        assert first.result(timeout=5) == SECRET
        assert second.result(timeout=5) == SECRET
    assert fake_client.calls == [{"SecretId": SECRET_ID}]
