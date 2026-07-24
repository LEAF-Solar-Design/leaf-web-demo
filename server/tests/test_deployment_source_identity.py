import json
from pathlib import Path

from fastapi.testclient import TestClient

import app as app_module
import customization_service


ROOT = Path(__file__).resolve().parents[2]


def test_health_reports_image_source_sha(monkeypatch):
    source_sha = "a" * 40
    monkeypatch.setenv("LEAF_SOURCE_SHA", source_sha)

    response = TestClient(app_module.app).get("/api/health")

    assert response.status_code == 200
    assert response.json()["source_sha"] == source_sha


def test_health_stays_live_with_disabled_shared_customization_store(
    tmp_path, monkeypatch
):
    database = tmp_path / "customization.db"
    database.touch()
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R5_MODE", "off")
    monkeypatch.setenv("LEAF_CUSTOMIZATION_R6_MODE", "off")
    monkeypatch.setattr(customization_service, "database_path", lambda: database)
    monkeypatch.setattr(customization_service, "_shared_sqlite_path", lambda path: True)

    response = TestClient(app_module.app).get("/api/health")

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_required_deployment_manifests_have_frozen_shape():
    for service in ("app", "broker", "harness", "web"):
        manifest = json.loads(
            (ROOT / "deploy" / f"required-config.{service}.json").read_text(
                encoding="utf-8"
            )
        )
        assert manifest["schemaVersion"] == 1
        assert manifest["container"] == f"leaf-platform-{service}"
        assert set(manifest["required"]) == {"environment", "mountPaths", "secrets"}
        assert all(
            isinstance(value, list)
            and all(isinstance(item, str) and item for item in value)
            for value in manifest["required"].values()
        )


def test_required_deployment_manifests_include_runtime_auth_secrets():
    expected = {
        "app": {"LEAF_BROKER_SECRET", "LEAF_OPS_SECRET"},
        "broker": {"LEAF_BROKER_SECRET"},
        "harness": {"LEAF_BROKER_SECRET"},
    }

    for service, required_secrets in expected.items():
        manifest = json.loads(
            (ROOT / "deploy" / f"required-config.{service}.json").read_text(
                encoding="utf-8"
            )
        )
        assert required_secrets <= set(manifest["required"]["secrets"])


def test_app_manifest_requires_durable_runtime_and_build_identity():
    manifest = json.loads(
        (ROOT / "deploy" / "required-config.app.json").read_text(encoding="utf-8")
    )
    required = set(manifest["required"]["environment"])
    assert {
        "LEAF_AGENT_AUDIT",
        "LEAF_AGENT_LEDGER",
        "LEAF_AGENT_STATE_DIR",
        "LEAF_AGENT_TENANTS_FILE",
        "LEAF_BUILD_REVISION_REQUIRED",
        "LEAF_GUEST_STORE_DIR",
    } <= required


def test_web_image_writes_source_identity_health_file():
    dockerfile = (ROOT / "deploy" / "Dockerfile.web").read_text(encoding="utf-8")
    assert "ARG LEAF_SOURCE_SHA=unknown" in dockerfile
    assert '"service":"leaf-platform-web"' in dockerfile
    assert '"component":"frontend"' in dockerfile
    assert 'source_sha":"%s' in dockerfile
