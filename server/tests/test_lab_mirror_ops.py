"""Fake-backed authorization, replay status, and privacy checks for SEL-316b."""

import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import lab_artifacts
import lab_artifacts_git
from routers import lab_mirror_ops

ENABLE = "/internal/ops/lab/mirror/enable"
TENANT = "tenant-acc-7f3a"
SECRET = {"X-Ops-Secret": "lab-mirror-test-secret"}


@pytest.fixture
def fakes(monkeypatch):
    receipt = {"status": "complete", "tenant": TENANT, "watermark": {"doc-91c2": "rev-1"}}
    complete = {"mode": "mirror", "receipt": receipt}
    status = Mock(return_value={"mode": "ledger", "receipt": {}})
    enable = Mock(return_value=complete)
    monkeypatch.setattr(lab_artifacts_git, "mirror_status", status)
    monkeypatch.setattr(lab_artifacts_git, "enable_mirror", enable)
    return SimpleNamespace(status=status, enable=enable, complete=complete)


@pytest.fixture
def client(monkeypatch, fakes):
    monkeypatch.setenv("LEAF_OPS_SECRET", SECRET["X-Ops-Secret"])
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    monkeypatch.setenv("LEAF_LAB_GIT_TENANTS", TENANT)
    app = FastAPI()
    app.include_router(lab_mirror_ops.router)
    with TestClient(app, raise_server_exceptions=False) as result:
        yield result


def test_wrong_and_missing_secret(client, fakes):
    for headers in ({}, {"X-Ops-Secret": "wrong"}):
        response = client.post(ENABLE, json={"tenant": TENANT}, headers=headers)
        assert response.status_code == 403
    fakes.status.assert_not_called()
    fakes.enable.assert_not_called()


def test_unset_secret_fails_closed(client, fakes, monkeypatch):
    monkeypatch.delenv("LEAF_OPS_SECRET")
    monkeypatch.delenv("LEAF_AUTH_LIVE")
    response = client.post(ENABLE, json={"tenant": TENANT}, headers=SECRET)
    assert response.status_code == 503
    assert response.json() == {"detail": "lab_mirror_ops_secret_not_configured"}
    fakes.status.assert_not_called()
    fakes.enable.assert_not_called()


def test_tenant_not_allowlisted(client, fakes, monkeypatch):
    monkeypatch.setenv("LEAF_LAB_GIT_TENANTS", "tenant-other")
    response = client.post(ENABLE, json={"tenant": TENANT}, headers=SECRET)
    assert response.status_code == 403
    assert response.json() == {"detail": "lab_mirror_ineligible"}
    fakes.status.assert_not_called()
    fakes.enable.assert_not_called()


@pytest.mark.parametrize("body,status,detail", [
    ("not JSON", 422, "lab_mirror_tenant_required"),
    ("[]", 422, "lab_mirror_tenant_required"),
    ("{}", 422, "lab_mirror_tenant_required"),
    ('{"tenant": 7}', 422, "lab_mirror_tenant_required"),
    ('{"tenant": "t", "extra": 1}', 422, "lab_mirror_tenant_required"),
    ('{"tenant": "bad id"}', 422, "lab_mirror_tenant_required"),
    ("x" * 5000, 413, "lab_mirror_body_too_large"),
])
def test_malformed_bodies(client, fakes, body, status, detail):
    response = client.post(ENABLE, content=body, headers=SECRET)
    assert response.status_code == status
    assert response.json() == {"detail": detail}
    fakes.status.assert_not_called()
    fakes.enable.assert_not_called()


def test_fresh_enable(client, fakes):
    response = client.post(ENABLE, json={"tenant": TENANT}, headers=SECRET)
    assert response.status_code == 200
    assert response.json() == {"status": "complete"}
    fakes.status.assert_called_once_with(TENANT)
    fakes.enable.assert_called_once_with(TENANT, "ops:lab-mirror-enable")
    assert TENANT not in response.text
    assert "doc-91c2" not in response.text


def test_already_mirrored(client, fakes):
    fakes.status.return_value = fakes.complete
    response = client.post(ENABLE, json={"tenant": TENANT}, headers=SECRET)
    assert response.status_code == 200
    assert response.json() == {"status": "complete"}
    fakes.enable.assert_not_called()


def test_first_status_store_unavailable(client, fakes):
    fakes.status.side_effect = lab_artifacts.StoreUnavailable("lab_db_unset")
    response = client.post(ENABLE, json={"tenant": TENANT}, headers=SECRET)
    assert response.status_code == 503
    assert response.json() == {"detail": "lab_store_unavailable"}
    fakes.enable.assert_not_called()
    assert TENANT not in response.text
    assert "doc-91c2" not in response.text


def test_first_status_store_migrating(client, fakes):
    fakes.status.side_effect = lab_artifacts.StoreMigrating("lab_store_migrating")
    response = client.post(ENABLE, json={"tenant": TENANT}, headers=SECRET)
    assert response.status_code == 409
    assert response.json() == {"detail": "lab_store_migrating"}
    fakes.enable.assert_not_called()
    assert TENANT not in response.text
    assert "doc-91c2" not in response.text


def test_refused_status_reread_store_unavailable(client, fakes):
    fakes.status.side_effect = [
        {"mode": "ledger", "receipt": {}},
        lab_artifacts.StoreUnavailable("lab_db_unset"),
    ]
    fakes.enable.side_effect = lab_artifacts_git.MirrorError("lab_mirror_mode_refused")
    response = client.post(ENABLE, json={"tenant": TENANT}, headers=SECRET)
    assert response.status_code == 503
    assert response.json() == {"detail": "lab_store_unavailable"}
    assert fakes.status.call_count == 2
    fakes.enable.assert_called_once_with(TENANT, "ops:lab-mirror-enable")
    assert TENANT not in response.text
    assert "doc-91c2" not in response.text


def test_race_to_mirrored(client, fakes):
    fakes.status.side_effect = [{"mode": "ledger", "receipt": {}}, fakes.complete]
    fakes.enable.side_effect = lab_artifacts_git.MirrorError("lab_mirror_mode_refused")
    response = client.post(ENABLE, json={"tenant": TENANT}, headers=SECRET)
    assert response.status_code == 200
    assert response.json() == {"status": "complete"}
    assert fakes.status.call_count == 2


def test_refused_and_not_mirrored(client, fakes):
    fakes.status.side_effect = [
        {"mode": "ledger", "receipt": {}},
        {"mode": "replaying", "receipt": {"status": "divergent"}},
    ]
    fakes.enable.side_effect = lab_artifacts_git.MirrorError("lab_mirror_mode_refused")
    response = client.post(ENABLE, json={"tenant": TENANT}, headers=SECRET)
    assert response.status_code == 409
    assert response.json() == {"detail": "lab_mirror_mode_refused"}
    assert fakes.status.call_count == 2


@pytest.mark.parametrize("code,status", [
    ("lab_replay_too_large", 413),
    ("lab_replay_divergence", 409),
    ("lab_ref_conflict", 409),
    ("unknown", 503),
    ("lab_harness_capability_missing", 503),
])
def test_error_mapping(client, fakes, code, status):
    fakes.enable.side_effect = lab_artifacts_git.MirrorError(code, observed="doc-91c2", capability="cap-x")
    response = client.post(ENABLE, json={"tenant": TENANT}, headers=SECRET)
    assert response.status_code == status
    assert response.json() == {"detail": code}
    for value in (TENANT, "doc-91c2", "cap-x"):
        assert value not in response.text


def test_malformed_code(client, fakes):
    fakes.enable.side_effect = lab_artifacts_git.MirrorError("Bad Code!")
    response = client.post(ENABLE, json={"tenant": TENANT}, headers=SECRET)
    assert response.status_code == 503
    assert response.json() == {"detail": "unknown"}


def test_unexpected_exception(client, fakes, caplog):
    fakes.enable.side_effect = RuntimeError("tenant-acc-7f3a doc-91c2")
    with caplog.at_level(logging.WARNING):
        response = client.post(ENABLE, json={"tenant": TENANT}, headers=SECRET)
    assert response.status_code == 503
    assert response.json() == {"detail": "lab_mirror_unavailable"}
    messages = [record.getMessage() for record in caplog.records]
    assert all(TENANT not in message and "doc-91c2" not in message for message in messages)
    assert "lab_mirror_enable_failed class=RuntimeError" in messages


def test_busy(client, fakes):
    assert lab_mirror_ops._RUNNING.acquire(blocking=False)
    try:
        response = client.post(ENABLE, json={"tenant": TENANT}, headers=SECRET)
        assert response.status_code == 409
        assert response.json() == {"detail": "lab_mirror_enable_busy"}
    finally:
        lab_mirror_ops._RUNNING.release()
    fakes.status.assert_not_called()
    fakes.enable.assert_not_called()


def test_logs_on_success(client, fakes, caplog):
    with caplog.at_level(logging.DEBUG, logger="routers.lab_mirror_ops"):
        response = client.post(ENABLE, json={"tenant": TENANT}, headers=SECRET)
    assert response.status_code == 200
    assert response.json() == {"status": "complete"}
    fakes.enable.assert_called_once_with(TENANT, "ops:lab-mirror-enable")
    for record in caplog.records:
        assert TENANT not in record.getMessage()
        assert "doc-91c2" not in record.getMessage()


def test_mount():
    text = (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")
    include = '__import__("routers.lab_mirror_ops", fromlist=["router"]).router'
    assert text.count(include) == 1
    lines = text.splitlines()
    engine = next(index for index, line in enumerate(lines)
                  if '__import__("routers.engine_changes", fromlist=["router"]).router' in line)
    assert include in lines[engine + 1]
