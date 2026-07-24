"""Router-contract tests for the APS observability read-API (routers/ops_metrics).

These exercise the auth gate, response shaping, tenant pass-through, and the
legacy-mode guard WITHOUT a live Postgres: the read-model (``ops_metrics_read``)
is monkeypatched, so what is under test here is the router contract, not the SQL.
The SQL is covered separately by the Postgres integration suite.
"""
from __future__ import annotations

import ops_metrics_read
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import ops_metrics


@pytest.fixture()
def stub_read_model(monkeypatch):
    monkeypatch.setattr(ops_metrics_read, "fleet_metrics",
                        lambda **kw: {"scope": kw.get("tenant_id") or "fleet",
                                      "throughput": {"runs": 3, "live_runs": 2}})
    monkeypatch.setattr(ops_metrics_read, "inflight",
                        lambda **kw: [{"job_id": "j1", "status": "running"}])
    monkeypatch.setattr(ops_metrics_read, "run_drilldown",
                        lambda **kw: [{"event_key": "j1:broker-run", "job_correlated": True}])


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(ops_metrics.router)
    return TestClient(app, raise_server_exceptions=True)


def test_gate_503_when_live_auth_and_no_secret(monkeypatch, stub_read_model):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.delenv("LEAF_OPS_SECRET", raising=False)
    resp = _client().get("/api/ops/metrics")
    assert resp.status_code == 503


def test_gate_403_on_wrong_secret(monkeypatch, stub_read_model):
    monkeypatch.setenv("LEAF_OPS_SECRET", "ops-secret")
    resp = _client().get("/api/ops/metrics", headers={"X-Ops-Secret": "wrong"})
    assert resp.status_code == 403


def test_metrics_ok_fleet(monkeypatch, stub_read_model):
    monkeypatch.setenv("LEAF_OPS_SECRET", "ops-secret")
    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
    resp = _client().get("/api/ops/metrics", headers={"X-Ops-Secret": "ops-secret"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["scope"] == "fleet"
    assert body["throughput"]["runs"] == 3


def test_metrics_tenant_scoped(monkeypatch, stub_read_model):
    monkeypatch.setenv("LEAF_OPS_SECRET", "ops-secret")
    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
    resp = _client().get("/api/ops/metrics?tenant_id=acme",
                         headers={"X-Ops-Secret": "ops-secret"})
    assert resp.status_code == 200
    assert resp.json()["scope"] == "acme"


def test_inflight_ok(monkeypatch, stub_read_model):
    monkeypatch.setenv("LEAF_OPS_SECRET", "ops-secret")
    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
    resp = _client().get("/api/ops/metrics/inflight",
                         headers={"X-Ops-Secret": "ops-secret"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["jobs"][0]["job_id"] == "j1"


def test_runs_ok_has_correlation_note(monkeypatch, stub_read_model):
    monkeypatch.setenv("LEAF_OPS_SECRET", "ops-secret")
    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
    resp = _client().get("/api/ops/metrics/runs",
                         headers={"X-Ops-Secret": "ops-secret"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert "correlation" in body["correlation_note"].lower()


def test_legacy_mode_requires_postgres(monkeypatch, stub_read_model):
    monkeypatch.setenv("LEAF_OPS_SECRET", "ops-secret")
    monkeypatch.setenv("LEAF_BROKER_STORE", "legacy")
    resp = _client().get("/api/ops/metrics", headers={"X-Ops-Secret": "ops-secret"})
    assert resp.status_code == 503
    assert "postgres" in resp.json().get("error", {}).get("message", "").lower()
