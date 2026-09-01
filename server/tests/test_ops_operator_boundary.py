"""Operator-principal boundary for the browser tenant controls."""
from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

SERVER_DIR = Path(__file__).resolve().parents[1]
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import operator_deps  # noqa: E402
import operator_egress_guard  # noqa: E402
import operator_principals  # noqa: E402
from operator_principals import OperatorPrincipal  # noqa: E402
import deps  # noqa: E402
from routers import ops  # noqa: E402


def _client(*, raise_server_exceptions: bool = False) -> TestClient:
    app = FastAPI()
    app.include_router(ops.router)
    app.dependency_overrides[deps.require_tenant] = lambda: "demo-tenant"
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def _active_operator() -> OperatorPrincipal:
    return OperatorPrincipal(
        subject="auth0|operator",
        role="operator",
        role_revision=1,
        status="active",
        profiles=("default",),
        environment="staging",
    )


def test_ungranted_browser_cannot_list_or_mutate_tenants(monkeypatch) -> None:
    monkeypatch.setattr(operator_deps.tenant_deps, "auth_live", lambda: False)
    monkeypatch.setattr(operator_principals, "resolve_principal", lambda _subject: None)
    client = _client(raise_server_exceptions=True)
    forged = {
        "X-Ops-Secret": "forged",
        "X-Internal-Role": "qa",
        "X-Operator-Subject": "auth0|forged",
    }

    assert client.get("/api/operator/tenants", headers=forged).status_code == 404
    assert client.post("/api/operator/tenants/tenant-a/disable", headers=forged).status_code == 404
    assert client.post("/api/operator/tenants/tenant-a/enable", headers=forged).status_code == 404


def test_active_operator_can_list_disable_and_enable(monkeypatch) -> None:
    monkeypatch.setattr(operator_deps.tenant_deps, "auth_live", lambda: False)
    monkeypatch.setattr(
        operator_principals,
        "resolve_principal",
        lambda subject: _active_operator() if subject == "auth0|operator" else None,
    )
    monkeypatch.setattr(ops, "_disabled_set", lambda: {"tenant-b"})
    monkeypatch.setattr(ops, "_broker_store_mode", lambda: "legacy")
    monkeypatch.setattr(ops, "_distinct_tenants", lambda _path: {"tenant-a", "tenant-b"})
    monkeypatch.setattr(ops, "_usage_mod", lambda: None)
    def _proxy(tenant_id: str, action: str) -> JSONResponse:
        assert operator_egress_guard.is_armed()
        return JSONResponse({"tenant_id": tenant_id, "action": action})

    monkeypatch.setattr(ops, "_proxy", _proxy)
    client = _client(raise_server_exceptions=True)
    headers = {"X-Operator-Subject": "auth0|operator"}

    listed = client.get("/api/operator/tenants", headers=headers)
    assert listed.status_code == 200
    assert listed.json()["tenants"] == [
        {"tenant_id": "tenant-a", "runs": 0, "usd_est": 0.0, "disabled": False},
        {"tenant_id": "tenant-b", "runs": 0, "usd_est": 0.0, "disabled": True},
    ]
    assert client.post("/api/operator/tenants/tenant-a/disable", headers=headers).json() == {
        "tenant_id": "tenant-a", "action": "disable"}
    assert client.post("/api/operator/tenants/tenant-a/enable", headers=headers).json() == {
        "tenant_id": "tenant-a", "action": "enable"}


def test_browser_source_has_no_ops_credential() -> None:
    api_source = (SERVER_DIR.parent / "web" / "src" / "api.js").read_text(encoding="utf-8")
    drawer_source = (
        SERVER_DIR.parent / "web" / "src" / "components" / "OpsDrawer.jsx"
    ).read_text(encoding="utf-8")
    browser_source = api_source + drawer_source

    assert "leaf.ops_secret" not in browser_source
    assert "X-Ops-Secret" not in browser_source
    assert "X-Internal-Role" not in browser_source
    assert "/api/operator/tenants" in api_source
    assert "headers: authHeaders()" in api_source
