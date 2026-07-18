"""Offline tests for APS credential loading in local and ECS environments."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import client  # noqa: E402


def test_load_creds_prefers_ecs_secret(monkeypatch, tmp_path):
    expected = {"client_id": "ecs-client", "client_secret": "ecs-secret"}
    monkeypatch.setenv("APS_CREDENTIALS_JSON", json.dumps(expected))
    monkeypatch.setattr(client, "CRED_PATH", str(tmp_path / "missing.json"))

    assert client._load_creds() == expected


def test_load_creds_uses_local_file_when_ecs_secret_absent(monkeypatch, tmp_path):
    expected = {"client_id": "local-client", "client_secret": "local-secret"}
    credential_path = tmp_path / "credentials.json"
    credential_path.write_text(json.dumps(expected), encoding="utf-8")
    monkeypatch.delenv("APS_CREDENTIALS_JSON", raising=False)
    monkeypatch.setattr(client, "CRED_PATH", str(credential_path))

    assert client._load_creds() == expected


@pytest.mark.parametrize(
    "value",
    ["not-json", "{}", '{"client_id":"only-id"}', '{"client_secret":"only-secret"}'],
)
def test_load_creds_rejects_invalid_ecs_secret(monkeypatch, value):
    monkeypatch.setenv("APS_CREDENTIALS_JSON", value)

    with pytest.raises(ValueError):
        client._load_creds()
