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
    monkeypatch.setattr(ops_metrics_read, "tool_metrics",
                        lambda **kw: {"scope": kw.get("tenant_id") or "fleet",
                                      "tools": [{"tool": "panel_layout", "runs": 2}]})
    monkeypatch.setattr(ops_metrics_read, "tenant_metrics",
                        lambda **kw: {"limit": kw.get("limit", 100),
                                      "tenants": [{"tenant_id": "acme", "runs": 2}]})
    monkeypatch.setattr(ops_metrics_read, "timeseries",
                        lambda **kw: {"scope": kw.get("tenant_id") or "fleet",
                                      "bucket_seconds": kw.get("bucket_seconds"),
                                      "buckets": [{"t": 1_754_000_000.0, "runs": 2}]})


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


@pytest.mark.parametrize("path", ["/api/ops/metrics",
                                  "/api/ops/metrics/inflight",
                                  "/api/ops/metrics/runs",
                                  "/api/ops/metrics/tools",
                                  "/api/ops/metrics/tenants",
                                  "/api/ops/metrics/timeseries"])
def test_all_routes_fail_closed_on_wrong_secret(monkeypatch, stub_read_model, path):
    """Every route (not just /metrics) must reject a wrong X-Ops-Secret."""
    monkeypatch.setenv("LEAF_OPS_SECRET", "ops-secret")
    resp = _client().get(path, headers={"X-Ops-Secret": "wrong"})
    assert resp.status_code == 403


@pytest.mark.parametrize("path,fn", [("/api/ops/metrics", "fleet_metrics"),
                                     ("/api/ops/metrics/inflight", "inflight"),
                                     ("/api/ops/metrics/runs", "run_drilldown"),
                                     ("/api/ops/metrics/tools", "tool_metrics")])
def test_tenant_id_passes_through_to_read_model(monkeypatch, path, fn):
    """The tenant_id query param must reach the read-model so scoping is honored."""
    seen = {}
    monkeypatch.setattr(ops_metrics_read, fn,
                        lambda **kw: seen.update(kw) or ({"scope": "x"} if fn in ("fleet_metrics", "tool_metrics") else []))
    monkeypatch.setenv("LEAF_OPS_SECRET", "ops-secret")
    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
    resp = _client().get(f"{path}?tenant_id=acme", headers={"X-Ops-Secret": "ops-secret"})
    assert resp.status_code == 200
    assert seen.get("tenant_id") == "acme"


def test_tools_ok_fleet(monkeypatch, stub_read_model):
    monkeypatch.setenv("LEAF_OPS_SECRET", "ops-secret")
    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
    resp = _client().get("/api/ops/metrics/tools", headers={"X-Ops-Secret": "ops-secret"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["scope"] == "fleet"
    assert body["tools"][0]["tool"] == "panel_layout"


def test_tenants_ok_passes_window_and_limit(monkeypatch):
    seen = {}
    monkeypatch.setattr(ops_metrics_read, "tenant_metrics",
                        lambda **kw: seen.update(kw) or {"tenants": []})
    monkeypatch.setenv("LEAF_OPS_SECRET", "ops-secret")
    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
    resp = _client().get("/api/ops/metrics/tenants?window=3600&limit=5",
                         headers={"X-Ops-Secret": "ops-secret"})
    assert resp.status_code == 200
    assert seen.get("window_seconds") == 3600
    assert seen.get("limit") == 5


def test_timeseries_ok_passes_every_param(monkeypatch):
    seen = {}
    monkeypatch.setattr(ops_metrics_read, "timeseries",
                        lambda **kw: seen.update(kw) or {"buckets": []})
    monkeypatch.setenv("LEAF_OPS_SECRET", "ops-secret")
    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
    resp = _client().get(
        "/api/ops/metrics/timeseries?window=604800&bucket=3600&tenant_id=acme&tool=panelize",
        headers={"X-Ops-Secret": "ops-secret"})
    assert resp.status_code == 200
    assert seen.get("window_seconds") == 604800
    assert seen.get("bucket_seconds") == 3600
    assert seen.get("tenant_id") == "acme"
    assert seen.get("tool") == "panelize"


def test_timeseries_rejects_disallowed_bucket_before_read_model(monkeypatch):
    """An off-menu bucket is the caller's error: 422 BAD_PARAMS, and the
    read-model is never invoked (no silent clamp to a different bucket)."""
    called = {}
    monkeypatch.setattr(ops_metrics_read, "timeseries",
                        lambda **kw: called.update(kw) or {"buckets": []})
    monkeypatch.setenv("LEAF_OPS_SECRET", "ops-secret")
    monkeypatch.setenv("LEAF_BROKER_STORE", "postgres")
    resp = _client().get("/api/ops/metrics/timeseries?bucket=1234",
                         headers={"X-Ops-Secret": "ops-secret"})
    assert resp.status_code == 422
    assert "bucket" in resp.json().get("error", {}).get("message", "").lower()
    assert called == {}


def test_timeseries_read_model_rejects_disallowed_bucket_itself():
    """Defense in depth: the read-model raises on an off-menu bucket even if a
    future caller bypasses the router check (raises BEFORE any DB access)."""
    with pytest.raises(ValueError):
        ops_metrics_read.timeseries(bucket_seconds=1234)
