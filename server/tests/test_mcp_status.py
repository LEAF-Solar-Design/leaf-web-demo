"""Security checks for the public standard-services status facade."""
from __future__ import annotations

import inspect

from fastapi import FastAPI
from fastapi.testclient import TestClient

import deps
from routers import mcp_status


def client() -> TestClient:
    app = FastAPI()
    app.include_router(mcp_status.router)
    return TestClient(app, raise_server_exceptions=False)


def test_reports_only_the_fixed_broker_facade(monkeypatch):
    monkeypatch.setenv(
        "LEAF_TENANT_MCP_BROKER_URL",
        "https://tenant-broker.example:8443/mcp",
    )

    response = client().get(
        "/api/converse/mcp", headers={"X-Tenant-Id": "tenant-a"}
    )

    assert response.status_code == 200
    assert response.json() == {
        "servers": [{"name": "services", "host": "tenant-broker.example:8443"}]
    }
    assert "mcp" not in response.text


def test_never_exposes_credentials_paths_or_operator_servers(monkeypatch):
    secret = "secret-never-returned"
    monkeypatch.setenv(
        "LEAF_TENANT_MCP_BROKER_URL",
        f"https://user:{secret}@operator.example/private?token={secret}",
    )

    response = client().get(
        "/api/converse/mcp", headers={"X-Tenant-Id": "tenant-a"}
    )

    assert response.json() == {"servers": []}
    assert secret not in response.text
    assert "operator" not in response.text


def test_missing_or_invalid_broker_config_fails_closed(monkeypatch):
    monkeypatch.delenv("LEAF_TENANT_MCP_BROKER_URL", raising=False)
    assert mcp_status._broker_descriptor() == []
    assert mcp_status._broker_descriptor({
        "LEAF_TENANT_MCP_BROKER_URL": "file:///operator-gateway"
    }) == []


def test_status_uses_the_active_tenant_dependency():
    dependency = inspect.signature(mcp_status.mcp_status).parameters["tenant"].default
    assert dependency.dependency is deps.require_active_tenant
