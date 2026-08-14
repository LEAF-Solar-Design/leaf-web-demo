from pathlib import Path

import pytest

import ios_ship_callback_listener as listener


def _private(path: Path) -> Path:
    path.write_text("test", encoding="utf-8")
    path.chmod(0o600)
    return path


def test_callback_tls_is_optional(monkeypatch):
    for name in (
        "LEAF_IOS_SHIP_CALLBACK_TLS_CERT_FILE",
        "LEAF_IOS_SHIP_CALLBACK_TLS_KEY_FILE",
        "LEAF_IOS_SHIP_CALLBACK_TLS_PORT",
    ):
        monkeypatch.delenv(name, raising=False)
    assert listener.CallbackTlsConfig.from_environment() is None


def test_callback_tls_requires_complete_private_files(monkeypatch, tmp_path):
    cert, key = _private(tmp_path / "cert.pem"), _private(tmp_path / "key.pem")
    monkeypatch.setenv("LEAF_IOS_SHIP_CALLBACK_TLS_CERT_FILE", str(cert))
    monkeypatch.setenv("LEAF_IOS_SHIP_CALLBACK_TLS_KEY_FILE", str(key))
    monkeypatch.setenv("LEAF_IOS_SHIP_CALLBACK_TLS_PORT", "8444")
    config = listener.CallbackTlsConfig.from_environment()
    assert config is not None and config.port == 8444

    if listener.os.name != "nt":
        key.chmod(0o644)
        with pytest.raises(RuntimeError, match="not private"):
            listener.CallbackTlsConfig.from_environment()


def test_callback_tls_rejects_plain_app_port(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_IOS_SHIP_CALLBACK_TLS_CERT_FILE", str(_private(tmp_path / "cert.pem")))
    monkeypatch.setenv("LEAF_IOS_SHIP_CALLBACK_TLS_KEY_FILE", str(_private(tmp_path / "key.pem")))
    monkeypatch.setenv("LEAF_IOS_SHIP_CALLBACK_TLS_PORT", "8130")
    with pytest.raises(RuntimeError, match="port is invalid"):
        listener.CallbackTlsConfig.from_environment()
