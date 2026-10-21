"""Source configuration checks; no provider, database or credential access."""
from dataclasses import FrozenInstanceError
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import integration_config as config

ARN = "arn:aws:kms:us-east-1:807034087062:key/187d5443-18be-4e51-99c5-f9fdbda4cc87"
ORIGIN = "https://ops-dashboard-seven.vercel.app"
SOURCE = {"schema": "leaf.studio-integration-config.v1", "enabled": True,
          "apiOrigin": ORIGIN, "kmsKeyArn": ARN}
ENABLED = "LEAF_INTEGRATION_TURN_GRANTS_ENABLED"
ORIGIN_ENV = "LEAF_INTEGRATION_SERVICE_ORIGIN"
KEY_ENV = "LEAF_INTEGRATION_TURN_GRANTS_KMS_KEY_ID"


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch):
    for name in (ENABLED, ORIGIN_ENV, KEY_ENV):
        monkeypatch.delenv(name, raising=False)


def source(monkeypatch, raw):
    def open_source(path, mode):
        assert path == Path(config.__file__).with_name("integration_config.json")
        assert mode == "rb"
        return io.BytesIO(raw)
    monkeypatch.setattr(config.Path, "open", open_source)


def test_packaged_defaults_are_immutable_and_do_not_mutate_environment():
    before = dict(os.environ)
    settings = config.load_integration_config()
    assert (settings.enabled, settings.api_origin, settings.kms_key_id) == (True, ORIGIN, ARN)
    assert dict(os.environ) == before
    with pytest.raises(FrozenInstanceError):
        settings.enabled = False


@pytest.mark.parametrize("value,expected", [("1", True), ("0", False), ("", False),
    ("true", False), (" 1", False), ("1\n", False)])
def test_explicit_enable_override(monkeypatch, value, expected):
    monkeypatch.setenv(ENABLED, value)
    assert config.load_integration_config().enabled is expected


@pytest.mark.parametrize("name,field,value", [
    (ORIGIN_ENV, "api_origin", "https://override.example:8443"),
    (KEY_ENV, "kms_key_id", "alias/studio"),
    (KEY_ENV, "kms_key_id", "12345678-1234-4234-8234-123456789012"),
])
def test_independent_presence_overrides_are_not_cached(monkeypatch, name, field, value):
    initial = config.load_integration_config()
    monkeypatch.setenv(name, value)
    changed = config.load_integration_config()
    assert getattr(changed, field) == value
    for other in ("enabled", "api_origin", "kms_key_id"):
        if other != field:
            assert getattr(changed, other) == getattr(initial, other)
    monkeypatch.delenv(name)
    assert config.load_integration_config() == initial


@pytest.mark.parametrize("name,value", [
    (ORIGIN_ENV, ""), (KEY_ENV, ""), (KEY_ENV, "alias/bad\nkey"),
    (ORIGIN_ENV, "http://unsafe.example"), (ORIGIN_ENV, "https://EXAMPLE.com"),
    (ORIGIN_ENV, "https://example.com/"), (ORIGIN_ENV, "https://example.com?"),
    (ORIGIN_ENV, "https://user@example.com"), (ORIGIN_ENV, "https://example.com:443"),
])
def test_invalid_explicit_values_never_fall_back(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(config.IntegrationConfigError, match="^INTEGRATION_CONFIG_OVERRIDE$") as exc:
        config.load_integration_config()
    assert exc.value.__context__ is None


@pytest.mark.parametrize("raw", [b"", b"{", b"\xff", b"[]", b"null", b"{}",
    b" " * (config.MAX_SOURCE_BYTES + 1),
    json.dumps(SOURCE).replace('"enabled": true', '"enabled": true, "enabled": false').encode(),
    json.dumps({**SOURCE, "credential": "forbidden"}).encode(),
    json.dumps({**SOURCE, "enabled": 1}).encode(),
    json.dumps({**SOURCE, "schema": "other"}).encode(),
    json.dumps({**SOURCE, "apiOrigin": "http://unsafe.example"}).encode(),
    json.dumps({**SOURCE, "apiOrigin": None}).encode(),
    json.dumps({**SOURCE, "kmsKeyArn": "alias/studio"}).encode(),
    json.dumps({**SOURCE, "kmsKeyArn": None}).encode(),
])
def test_bad_source_fails_closed_even_with_environment_overrides(monkeypatch, raw):
    source(monkeypatch, raw)
    monkeypatch.setenv(ENABLED, "1")
    monkeypatch.setenv(ORIGIN_ENV, ORIGIN)
    monkeypatch.setenv(KEY_ENV, ARN)
    with pytest.raises(config.IntegrationConfigError, match="^INTEGRATION_CONFIG_SOURCE$") as exc:
        config.load_integration_config()
    assert exc.value.__context__ is None


def test_missing_source_has_fixed_error(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("private filesystem detail")
    monkeypatch.setattr(config.Path, "open", missing)
    with pytest.raises(config.IntegrationConfigError, match="^INTEGRATION_CONFIG_SOURCE$") as exc:
        config.load_integration_config()
    assert exc.value.__context__ is None


@pytest.mark.parametrize("field", list(SOURCE))
def test_every_source_field_is_required(monkeypatch, field):
    data = dict(SOURCE)
    del data[field]
    source(monkeypatch, json.dumps(data).encode())
    with pytest.raises(config.IntegrationConfigError, match="^INTEGRATION_CONFIG_SOURCE$"):
        config.load_integration_config()


def test_source_read_is_bounded(monkeypatch):
    class Bounded(io.BytesIO):
        def read(self, size=-1):
            assert size == config.MAX_SOURCE_BYTES + 1
            return super().read(size)
    monkeypatch.setattr(config.Path, "open", lambda *a: Bounded(b" " * 10000))
    with pytest.raises(config.IntegrationConfigError, match="INTEGRATION_CONFIG_SOURCE"):
        config.load_integration_config()


def test_source_off_and_explicit_off(monkeypatch):
    source(monkeypatch, json.dumps({**SOURCE, "enabled": False}).encode())
    assert config.load_integration_config().enabled is False
    monkeypatch.setenv(ENABLED, "1")
    assert config.load_integration_config().enabled is True
    monkeypatch.setenv(ENABLED, "0")
    assert config.load_integration_config().enabled is False


def test_import_has_no_configuration_io_or_network(monkeypatch):
    # Compile before installing the I/O guards so module source reading is excluded.
    code = compile(Path(config.__file__).read_text(encoding="utf-8"), config.__file__, "exec")
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("_config_import_probe", loader=None))
    monkeypatch.setitem(sys.modules, module.__name__, module)
    def forbidden(*args, **kwargs):
        pytest.fail("import performed I/O or credential access")
    with monkeypatch.context() as guards:
        guards.setattr(config.Path, "open", forbidden)
        guards.setattr(socket, "socket", forbidden)
        guards.setattr(os, "getenv", forbidden)
        guards.setattr(type(os.environ), "__getitem__", forbidden)
        exec(code, module.__dict__)
    assert module.IntegrationSettings is not None
