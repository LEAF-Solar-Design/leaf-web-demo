"""Wave 2 application trust-boundary regression gates."""
from __future__ import annotations

import json

import pytest
from fastapi.responses import JSONResponse

import broker
import broker_client
from routers import capabilities as capabilities_router
from routers import tenant as tenant_router


def _safe_production(monkeypatch):
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "production")
    monkeypatch.setenv("LEAF_BROKER_SECRET", "broker-secret")
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_QA_HOOKS", "0")
    monkeypatch.setenv("LEAF_AUTHORED_EXECUTION", "0")


def test_broker_accepts_explicit_safe_production_contract(monkeypatch):
    _safe_production(monkeypatch)
    assert broker.validate_runtime_safety() is None


def test_broker_rejects_blank_production_caller_secret(monkeypatch):
    _safe_production(monkeypatch)
    monkeypatch.setenv("LEAF_BROKER_SECRET", " \n")
    with pytest.raises(RuntimeError, match="LEAF_BROKER_SECRET"):
        broker.validate_runtime_safety()


def test_broker_rejects_production_without_live_auth(monkeypatch):
    _safe_production(monkeypatch)
    monkeypatch.setenv("LEAF_AUTH_LIVE", "0")
    with pytest.raises(RuntimeError, match="LEAF_AUTH_LIVE=1"):
        broker.validate_runtime_safety()


@pytest.mark.parametrize("qa_value", [None, "1"])
def test_broker_rejects_nonexplicit_qa_containment(monkeypatch, qa_value):
    _safe_production(monkeypatch)
    if qa_value is None:
        monkeypatch.delenv("LEAF_QA_HOOKS", raising=False)
    else:
        monkeypatch.setenv("LEAF_QA_HOOKS", qa_value)
    with pytest.raises(RuntimeError, match="LEAF_QA_HOOKS=0"):
        broker.validate_runtime_safety()


def test_qa_capability_projection_rejects_plain_role_header(monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_OPS_SECRET", "ops-secret")
    denied = capabilities_router.capabilities(
        x_internal_role="qa", x_ops_secret=None, tenant="tenant-a"
    )
    assert isinstance(denied, JSONResponse)
    assert denied.status_code == 403


def test_qa_capability_projection_accepts_verified_operator(monkeypatch):
    monkeypatch.setenv("LEAF_AUTH_LIVE", "1")
    monkeypatch.setenv("LEAF_OPS_SECRET", "ops-secret")
    monkeypatch.setattr(capabilities_router.deps, "all_tools", lambda _tenant: [])
    monkeypatch.setattr(
        capabilities_router.catalog,
        "build_catalog",
        lambda _tools, include_internal: [{"internal": include_internal}],
    )
    body = capabilities_router.capabilities(
        x_internal_role="qa", x_ops_secret="ops-secret", tenant="tenant-a"
    )
    assert body["families"] == [{"internal": True}]


def test_app_diagnostic_proxy_strips_unknown_and_secret_fields(monkeypatch):
    monkeypatch.setenv("LEAF_AUTHOR_HARNESS_URL", "http://harness.internal")
    monkeypatch.setattr(
        broker_client, "harness_headers",
        lambda: {"X-Harness-Secret": "shared-secret"},
    )
    seen = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {
                "schema": "leaf.grant-diagnostic.v1",
                "linked": True,
                "kind": "oauth",
                "linked_at": "2026-07-23T00:00:00Z",
                "backend": "file",
                "path_class": "efs_access_point",
                "record_format": "v1",
                "legacy_fallback_present": False,
                "owner": {"uid": 1000, "gid": 1000, "mode": "0600"},
                "persistence": {
                    "atomic_publish": True,
                    "file_fsync": True,
                    "directory_fsync": True,
                },
                "degraded": False,
                "token": "must-not-escape",
                "path": "/must/not/escape",
            }

    def fake_get(url, *, headers, timeout):
        seen.update(url=url, headers=headers, timeout=timeout)
        return Response()

    import requests
    monkeypatch.setattr(requests, "get", fake_get)
    body = tenant_router.grant_diagnostic(tenant="tenant a")
    encoded = json.dumps(body)
    assert seen["url"] == "http://harness.internal/grants/tenant%20a/diagnostic"
    assert seen["headers"] == {"X-Harness-Secret": "shared-secret"}
    assert "must-not-escape" not in encoded
    assert "/must/not/escape" not in encoded
    assert body["schema"] == "leaf.grant-diagnostic.v1"
    assert body["record_format"] == "v1"
