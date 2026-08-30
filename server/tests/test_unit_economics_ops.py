"""Contract tests for the fleet-only unit-economics ops surface."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import ops


class _Conflict(RuntimeError):
    pass


class _Store:
    LedgerConflict = _Conflict

    def __init__(self):
        self.report_args = None
        self.observation_args = None

    def fleet_report(self, start, end):
        self.report_args = (start, end)
        return {
            "schema": "leaf.unit-economics-report.v1",
            "scope": "fleet",
            "coverage_gaps": [],
        }

    def append_observation(self, **kwargs):
        self.observation_args = kwargs
        return {"observation_key": "a" * 64, "recorded": True}


@pytest.fixture()
def stub_store(monkeypatch):
    import platform_link

    store = _Store()
    monkeypatch.setattr(platform_link, "unit_economics_store", lambda: store)
    return store


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(ops.router)
    return TestClient(app, raise_server_exceptions=True)


def test_report_fails_closed_before_store_access(monkeypatch):
    monkeypatch.setenv("LEAF_OPS_SECRET", "ops-secret")
    response = _client().get(
        "/api/ops/unit-economics", headers={"X-Ops-Secret": "wrong"},
    )
    assert response.status_code == 403


def test_report_is_fleet_only_and_passes_exact_period(monkeypatch, stub_store):
    monkeypatch.setenv("LEAF_OPS_SECRET", "ops-secret")
    response = _client().get(
        "/api/ops/unit-economics",
        params={
            "period_start": "2026-08-01T00:00:00Z",
            "period_end": "2026-09-01T00:00:00Z",
        },
        headers={"X-Ops-Secret": "ops-secret"},
    )
    assert response.status_code == 200
    assert response.json()["scope"] == "fleet"
    assert stub_store.report_args == (
        datetime(2026, 8, 1, tzinfo=timezone.utc),
        datetime(2026, 9, 1, tzinfo=timezone.utc),
    )


def test_observation_is_forwarded_and_returns_envelope(monkeypatch, stub_store):
    monkeypatch.setenv("LEAF_OPS_SECRET", "ops-secret")
    response = _client().post(
        "/api/ops/unit-economics/observations",
        json={
            "idempotency_key": "aws-2026-08-shared",
            "period_start": "2026-08-01T00:00:00Z",
            "period_end": "2026-09-01T00:00:00Z",
            "kind": "shared_fixed",
            "category": "hosting",
            "amount_usd": "125.50",
            "source": "aws-cost-explorer",
            "metadata": {"environment": "production"},
        },
        headers={"X-Ops-Secret": "ops-secret"},
    )
    assert response.status_code == 200
    assert response.json()["recorded"] is True
    assert stub_store.observation_args["category"] == "hosting"
    assert str(stub_store.observation_args["amount_usd"]) == "125.50"


def test_observation_idempotency_drift_is_409(monkeypatch, stub_store):
    monkeypatch.setenv("LEAF_OPS_SECRET", "ops-secret")

    def conflict(**_kwargs):
        raise _Conflict("observation idempotency key was reused")

    stub_store.append_observation = conflict
    response = _client().post(
        "/api/ops/unit-economics/observations",
        json={
            "idempotency_key": "same-key",
            "period_start": "2026-08-01T00:00:00Z",
            "period_end": "2026-09-01T00:00:00Z",
            "kind": "revenue",
            "category": "subscriptions",
            "amount_usd": 10,
            "source": "stripe",
        },
        headers={"X-Ops-Secret": "ops-secret"},
    )
    assert response.status_code == 409
