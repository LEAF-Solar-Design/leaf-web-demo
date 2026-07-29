"""Security and tenant-isolation checks for GET /api/converse/mcp."""
from __future__ import annotations

import hashlib
import inspect
import json
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import deps  # noqa: E402
from routers import mcp_status  # noqa: E402


def _path(store: Path, tenant: str) -> Path:
    digest = hashlib.sha256(tenant.encode("utf-8")).hexdigest()
    return store / f"{digest}.json"


@pytest.fixture()
def client():
    app = FastAPI()
    app.include_router(mcp_status.router)
    return TestClient(app, raise_server_exceptions=False)


def _response(client: TestClient, tenant: str):
    return client.get("/api/converse/mcp", headers={"X-Tenant-Id": tenant})


def test_tenants_only_see_their_own_redacted_servers(client, monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_MCP_BRIDGE_DIR", str(tmp_path))
    sentinel = "Bearer sentinel-never-leak"
    _path(tmp_path, "tenant-a").write_text(json.dumps([{
        "name": "alpha", "url": "https://alpha.example.test/private?token=secret",
        "authToken": sentinel,
    }]), encoding="utf-8")
    _path(tmp_path, "tenant-b").write_text(json.dumps([{
        "name": "beta", "url": "https://beta.example.test:8443/hidden",
        "authToken": sentinel,
    }]), encoding="utf-8")

    a = _response(client, "tenant-a")
    b = _response(client, "tenant-b")

    assert a.status_code == b.status_code == 200
    assert a.json() == {"servers": [{"name": "alpha", "host": "alpha.example.test"}]}
    assert b.json() == {"servers": [{"name": "beta", "host": "beta.example.test:8443"}]}
    assert sentinel not in a.text + b.text
    assert "private" not in a.text + b.text


@pytest.mark.parametrize("contents", ["{", "[" * 1100 + "]" * 1100])
def test_hostile_contents_degrade_to_an_empty_collection(client, monkeypatch, tmp_path, contents):
    monkeypatch.setenv("LEAF_MCP_BRIDGE_DIR", str(tmp_path))
    _path(tmp_path, "demo-tenant").write_text(contents, encoding="utf-8")

    response = _response(client, "demo-tenant")

    assert response.status_code == 200
    assert response.json() == {"servers": []}
    assert "sentinel" not in response.text


def test_missing_store_environment_degrades_to_empty_collection(client, monkeypatch):
    monkeypatch.delenv("LEAF_MCP_BRIDGE_DIR", raising=False)

    response = _response(client, "demo-tenant")

    assert response.status_code == 200
    assert response.json() == {"servers": []}


def test_drops_descriptors_that_embed_their_auth_token(client, monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_MCP_BRIDGE_DIR", str(tmp_path))
    sentinel = "token-abcdefghijklmnopqrstuvwxyz"
    _path(tmp_path, "demo-tenant").write_text(json.dumps([
        {"name": f"name-{sentinel}", "url": "https://name.example.test", "authToken": sentinel},
        {"name": "host-leak", "url": f"https://host-{sentinel}.example.test", "authToken": sentinel},
        {"name": "safe", "url": "https://safe.example.test", "authToken": sentinel},
    ]), encoding="utf-8")

    response = _response(client, "demo-tenant")

    assert response.status_code == 200
    assert response.json() == {"servers": [{"name": "safe", "host": "safe.example.test"}]}
    assert sentinel not in response.text


def test_status_uses_the_active_tenant_dependency():
    dependency = inspect.signature(mcp_status.mcp_status).parameters["tenant"].default

    assert dependency.dependency is deps.require_active_tenant
