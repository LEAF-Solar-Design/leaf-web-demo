"""Safe catalog reads for tenants that have not published a catalog yet."""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

import deps
import customization_service
from customization_service import CustomizationServiceError
from routers import prompt as prompt_router
from routers import capabilities as capabilities_router
from routers import tools as tools_router
import tenant_paths


BASE_TOOL = {
    "name": "safe-base-reader",
    "version": "1.0.0",
    "description": "Read from the shipped base catalog.",
    "capabilities": ["drawing.read"],
    "entry": "tools/safe-base-reader/tool.py",
    "params": {"type": "object", "properties": {}},
}
TENANT_TOOL = {
    **BASE_TOOL,
    "name": "unpublished-tenant-tool",
    "entry": "tools/unpublished-tenant-tool/tool.py",
}
TRUSTED_LIVE_TOOL = {**BASE_TOOL, "aps_live": True}
TRUSTED_LIVE_DIGEST = deps.catalog_tool_digest(TRUSTED_LIVE_TOOL)


def _raise(error: CustomizationServiceError):
    def raiser(_tenant: str):
        raise error

    return raiser


def _fresh_enabled_tenant(monkeypatch, tmp_path):
    tenant = "fresh-tenant"
    tenant_repo = tmp_path / tenant
    tenant_repo.mkdir()
    (tenant_repo / "registry.json").write_text(
        json.dumps({"tools": [TENANT_TOOL]}), encoding="utf-8"
    )
    monkeypatch.setenv("LEAF_TENANTS_DIR", str(tmp_path))
    monkeypatch.delenv("LEAF_TENANT_REPO", raising=False)
    monkeypatch.setattr(customization_service, "effective_catalog_pin", lambda _tenant: None)
    monkeypatch.setattr(tenant_paths, "enabled", lambda _wave, _tenant: True)
    monkeypatch.setattr(
        deps,
        "_global_tool_tiers",
        lambda: [
            ("engine_registry", [BASE_TOOL]),
            ("catalog_seed", []),
            ("write_seed", []),
        ],
    )
    monkeypatch.setattr(deps, "_AUTHORED", [])
    return tenant


def test_fresh_enabled_tenant_catalog_and_consumers_use_only_safe_base(
    monkeypatch, tmp_path
):
    tenant = _fresh_enabled_tenant(monkeypatch, tmp_path)

    catalog = deps.all_tools(tenant)
    assert [tool["name"] for tool in catalog] == [BASE_TOOL["name"]]
    assert deps.find_tool(BASE_TOOL["name"], tenant) == BASE_TOOL
    prompt = prompt_router.nl_prompt(
        prompt_router.PromptRequest(text="safe base reader"), tenant=tenant
    )
    assert prompt["lane"] == "run"
    assert prompt["tool"] == BASE_TOOL["name"]


def test_capabilities_uses_only_safe_base_for_tenant_without_pin(
    monkeypatch, tmp_path
):
    tenant = _fresh_enabled_tenant(monkeypatch, tmp_path)
    monkeypatch.setattr(
        capabilities_router.deps, "shared_tools", lambda: [BASE_TOOL]
    )

    body = capabilities_router.capabilities(
        x_internal_role=None, x_ops_secret=None, tenant=tenant
    )

    names = {
        tool["name"]
        for family in body["families"]
        for tool in family["capabilities"]
    }
    assert names == {BASE_TOOL["name"]}
    assert TENANT_TOOL["name"] not in names
    assert body["error"] is None


@pytest.mark.parametrize(
    ("effective_tool", "runtime_enabled", "expected"),
    [
        (TRUSTED_LIVE_TOOL, True, True),
        (TRUSTED_LIVE_TOOL, False, False),
        (
            {**BASE_TOOL, "description": "tenant override", "aps_live": True},
            True,
            False,
        ),
        ({**BASE_TOOL, "description": "same-name inherited override"}, True, False),
        ({**BASE_TOOL, "name": "arbitrary-live-tool", "aps_live": True}, True, False),
        (
            {
                **BASE_TOOL,
                "description": "forged digest override",
                "aps_live": True,
                "catalog_digest": TRUSTED_LIVE_DIGEST,
            },
            True,
            False,
        ),
    ],
)
def test_capabilities_requires_exact_engine_and_runtime_live_aps_authority(
    monkeypatch, effective_tool, runtime_enabled, expected
):
    monkeypatch.setattr(
        capabilities_router.deps,
        "load_engine_registry_tools",
        lambda: [TRUSTED_LIVE_TOOL],
    )
    monkeypatch.setattr(
        capabilities_router.deps, "all_tools", lambda _tenant: [effective_tool]
    )
    monkeypatch.setattr(
        customization_service, "effective_catalog_pin", lambda _tenant: None
    )
    monkeypatch.setattr(capabilities_router.deps, "APS_LIVE", runtime_enabled)

    body = capabilities_router.capabilities(
        x_internal_role=None, x_ops_secret=None, tenant="tenant-a"
    )

    projected = next(
        capability
        for family in body["families"]
        for capability in family["capabilities"]
        if capability["name"] == effective_tool["name"]
    )
    assert projected["aps_live"] is expected


def test_flat_tools_uses_only_safe_base_for_tenant_without_pin(
    monkeypatch, tmp_path
):
    tenant = _fresh_enabled_tenant(monkeypatch, tmp_path)

    body = tools_router.tools(tenant=tenant)

    assert [tool["name"] for tool in body["tools"]] == [BASE_TOOL["name"]]
    assert body["error"] is None


def test_pinned_catalog_materialization_failure_does_not_fall_back(monkeypatch):
    monkeypatch.setattr(
        customization_service,
        "effective_catalog_pin",
        lambda _tenant: {
            "catalog_commit": "a" * 40,
            "effective_catalog_digest": "b" * 64,
        },
    )
    failure = CustomizationServiceError(
        "effective_catalog_digest_mismatch",
        503,
        detail="operator-only tenant path",
    )
    monkeypatch.setattr(
        customization_service, "effective_catalog_dir", _raise(failure)
    )

    with pytest.raises(CustomizationServiceError) as caught:
        deps.all_tools("pinned-tenant")

    assert caught.value is failure


def test_capabilities_maps_pin_lookup_error_to_intended_status(monkeypatch):
    failure = CustomizationServiceError("customization_store_unavailable", 503)
    monkeypatch.setattr(
        capabilities_router.deps, "all_tools", lambda _tenant: [BASE_TOOL]
    )
    monkeypatch.setattr(
        customization_service, "effective_catalog_pin", _raise(failure)
    )

    response = capabilities_router.capabilities(
        x_internal_role=None, x_ops_secret=None, tenant="pinned-tenant"
    )

    assert response.status_code == 503
    body = json.loads(response.body)
    assert body["reason_code"] == "customization_store_unavailable"


def test_pin_lookup_normalizes_store_failure_without_exposing_detail(
    monkeypatch, tmp_path
):
    database = tmp_path / "customization.db"
    database.touch()
    monkeypatch.setenv("LEAF_CUSTOMIZATION_STORE", "sqlite")
    monkeypatch.setattr(customization_service, "database_path", lambda: database)

    def fail_lookup(*, tenant_id):
        raise sqlite3.DatabaseError(f"corrupt authority for {tenant_id}")

    monkeypatch.setattr(
        customization_service.CustomizationService,
        "configured",
        lambda: SimpleNamespace(
            store=SimpleNamespace(get_effective_catalog=fail_lookup)
        ),
    )

    with pytest.raises(
        CustomizationServiceError,
        match="effective_catalog_unavailable",
    ) as caught:
        customization_service.effective_catalog_pin("tenant-a")

    assert "corrupt authority" in caught.value.detail


@pytest.mark.parametrize(
    ("route", "kwargs"),
    [
        (
            capabilities_router.capabilities,
            {"x_internal_role": None, "x_ops_secret": None},
        ),
        (tools_router.tools, {}),
    ],
)
def test_catalog_routes_preserve_customization_error_status_without_detail(
    monkeypatch, route, kwargs
):
    error = CustomizationServiceError(
        "effective_catalog_digest_mismatch",
        503,
        detail="operator-only tenant path",
    )
    monkeypatch.setattr(
        customization_service,
        "effective_catalog_pin",
        lambda _tenant: {
            "catalog_commit": "a" * 40,
            "effective_catalog_digest": "b" * 64,
        },
    )
    monkeypatch.setattr(capabilities_router.deps, "all_tools", _raise(error))

    response = route(tenant="pinned-tenant", **kwargs)

    assert response.status_code == 503
    body = json.loads(response.body)
    assert body["reason_code"] == "effective_catalog_digest_mismatch"
    assert body["error"]["error_code"] == "INTERNAL"
    assert body["error"]["retryable"] is True
    assert "operator-only tenant path" not in response.body.decode("utf-8")
