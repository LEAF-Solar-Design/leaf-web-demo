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
    # capabilities() prefers the provenance seam (deps.effective_tools_with_
    # provenance) over deps.all_tools/shared_tools when both are present on
    # deps (tiered-fold catalog, mainline #545). Mock at that seam so the
    # router-level assertion below (only the safe base surfaces, the
    # unpublished tenant tool never leaks in) exercises the code path
    # capabilities() actually takes.
    monkeypatch.setattr(
        capabilities_router.deps,
        "effective_tools_with_provenance",
        lambda _tenant: [(BASE_TOOL, deps.TOOL_SOURCE_CATALOG_SEED)],
        raising=False,
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
    ("effective_tool", "source", "runtime_enabled", "expected"),
    [
        (TRUSTED_LIVE_TOOL, "operator_owned_engine", True, True),
        (TRUSTED_LIVE_TOOL, "operator_owned_engine", False, False),
        (TRUSTED_LIVE_TOOL, "tenant_repo", True, False),
        (TRUSTED_LIVE_TOOL, "authored", True, False),
        (
            {**BASE_TOOL, "description": "tenant override", "aps_live": True},
            "tenant_repo",
            True,
            False,
        ),
        (
            {**BASE_TOOL, "description": "same-name inherited override"},
            "tenant_repo",
            True,
            False,
        ),
        (
            {**BASE_TOOL, "name": "arbitrary-live-tool", "aps_live": True},
            "tenant_repo",
            True,
            False,
        ),
        (
            {
                **BASE_TOOL,
                "description": "forged digest override",
                "aps_live": True,
                "catalog_digest": TRUSTED_LIVE_DIGEST,
            },
            "tenant_repo",
            True,
            False,
        ),
    ],
)
def test_capabilities_requires_exact_engine_and_runtime_live_aps_authority(
    monkeypatch, effective_tool, source, runtime_enabled, expected
):
    monkeypatch.setattr(
        capabilities_router.deps,
        "TOOL_SOURCE_OPERATOR_OWNED_ENGINE",
        "operator_owned_engine",
        raising=False,
    )
    monkeypatch.setattr(
        capabilities_router.deps,
        "effective_tools_with_provenance",
        lambda _tenant: [(effective_tool, source)],
        raising=False,
    )
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


def test_capabilities_uses_one_provenance_snapshot_without_all_tools_join(monkeypatch):
    def split_read_forbidden(_tenant):
        raise AssertionError("all_tools split read must not run with provenance")

    monkeypatch.setattr(
        capabilities_router.deps,
        "TOOL_SOURCE_OPERATOR_OWNED_ENGINE",
        "operator_owned_engine",
        raising=False,
    )
    monkeypatch.setattr(
        capabilities_router.deps,
        "effective_tools_with_provenance",
        lambda _tenant: [(TRUSTED_LIVE_TOOL, "operator_owned_engine")],
        raising=False,
    )
    monkeypatch.setattr(capabilities_router.deps, "all_tools", split_read_forbidden)
    monkeypatch.setattr(
        capabilities_router.deps,
        "load_engine_registry_tools",
        lambda: [TRUSTED_LIVE_TOOL],
    )
    monkeypatch.setattr(
        customization_service, "effective_catalog_pin", lambda _tenant: None
    )
    monkeypatch.setattr(capabilities_router.deps, "APS_LIVE", True)

    body = capabilities_router.capabilities(
        x_internal_role=None, x_ops_secret=None, tenant="tenant-a"
    )

    projected = next(
        capability
        for family in body["families"]
        for capability in family["capabilities"]
        if capability["name"] == TRUSTED_LIVE_TOOL["name"]
    )
    assert body["error"] is None
    assert projected["catalog_digest"] == TRUSTED_LIVE_DIGEST
    assert projected["aps_live"] is True


@pytest.mark.parametrize("missing_name", [
    "effective_tools_with_provenance",
    "TOOL_SOURCE_OPERATOR_OWNED_ENGINE",
])
def test_capabilities_without_provenance_seam_keeps_catalog_but_disables_live_aps(
    monkeypatch, missing_name
):
    monkeypatch.setattr(
        capabilities_router.deps,
        "TOOL_SOURCE_OPERATOR_OWNED_ENGINE",
        "operator_owned_engine",
        raising=False,
    )
    monkeypatch.setattr(
        capabilities_router.deps,
        "effective_tools_with_provenance",
        lambda _tenant: [(TRUSTED_LIVE_TOOL, "operator_owned_engine")],
        raising=False,
    )
    monkeypatch.delattr(capabilities_router.deps, missing_name)
    monkeypatch.setattr(
        capabilities_router.deps, "all_tools", lambda _tenant: [TRUSTED_LIVE_TOOL]
    )
    monkeypatch.setattr(
        capabilities_router.deps,
        "load_engine_registry_tools",
        lambda: [TRUSTED_LIVE_TOOL],
    )
    monkeypatch.setattr(
        customization_service, "effective_catalog_pin", lambda _tenant: None
    )
    monkeypatch.setattr(capabilities_router.deps, "APS_LIVE", True)

    body = capabilities_router.capabilities(
        x_internal_role=None, x_ops_secret=None, tenant="tenant-a"
    )

    projected = next(
        capability
        for family in body["families"]
        for capability in family["capabilities"]
        if capability["name"] == TRUSTED_LIVE_TOOL["name"]
    )
    assert body["error"] is None
    assert projected["aps_live"] is False


@pytest.mark.parametrize("provenance_error", [
    deps.ToolCatalogProvenanceError("tool source 'authored' contains a non-object row"),
    deps.ToolCatalogCollisionError("tool name collision across GLOBAL catalog tiers"),
])
def test_capabilities_degrades_to_batch_only_when_provenance_fails(
    monkeypatch, capsys, provenance_error
):
    """A provenance failure must not take the catalog down: the route serves the
    forgiving read (today's production catalog) with live APS failed closed."""
    def raise_provenance(_tenant):
        raise provenance_error

    monkeypatch.setattr(
        capabilities_router.deps,
        "TOOL_SOURCE_OPERATOR_OWNED_ENGINE",
        "operator_owned_engine",
        raising=False,
    )
    monkeypatch.setattr(
        capabilities_router.deps,
        "effective_tools_with_provenance",
        raise_provenance,
        raising=False,
    )
    monkeypatch.setattr(
        capabilities_router.deps, "all_tools", lambda _tenant: [TRUSTED_LIVE_TOOL]
    )
    monkeypatch.setattr(
        capabilities_router.deps,
        "load_engine_registry_tools",
        lambda: [TRUSTED_LIVE_TOOL],
    )
    monkeypatch.setattr(
        customization_service, "effective_catalog_pin", lambda _tenant: None
    )
    monkeypatch.setattr(capabilities_router.deps, "APS_LIVE", True)

    body = capabilities_router.capabilities(
        x_internal_role=None, x_ops_secret=None, tenant="tenant-a"
    )

    projected = next(
        capability
        for family in body["families"]
        for capability in family["capabilities"]
        if capability["name"] == TRUSTED_LIVE_TOOL["name"]
    )
    assert body["error"] is None
    assert projected["aps_live"] is False
    # The degrade identifies the branch and type without exposing the message.
    stderr = capsys.readouterr().err
    assert "diagnostic_code=catalog_provenance_unavailable" in stderr
    assert f"exception_type={type(provenance_error).__name__}" in stderr
    assert str(provenance_error) not in stderr


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
    monkeypatch, tmp_path, caplog, capsys
):
    database = tmp_path / "customization.db"
    database.touch()
    monkeypatch.setenv("LEAF_CUSTOMIZATION_STORE", "sqlite")
    monkeypatch.setattr(customization_service, "database_path", lambda: database)

    marker = "private-database-marker-9f3a"
    failure = sqlite3.DatabaseError(marker)

    def fail_lookup(*, tenant_id):
        raise failure

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

    error = caught.value
    assert error.code == "effective_catalog_unavailable"
    assert error.status_code == 503
    assert error.diagnostic_code == "catalog_pin_lookup_failed"
    assert error.exception_type == "DatabaseError"
    assert error.detail == error.diagnostic_code
    assert error.__cause__ is failure
    from routers import author as author_router

    author_response = author_router._customization_error(error)
    catalog_response = capabilities_router._catalog_error(error)
    assert author_response.status_code == catalog_response.status_code == 503
    assert json.loads(author_response.body) == {
        "error": {
            "error_code": "BAD_PARAMS",
            "message": "Protected tool authoring could not complete. The approved request was not executed.",
            "retryable": False, "retry_class": "after_action", "actor": "user",
            "next_action": "Review the inputs, correct them, and submit again.",
        },
        "reason_code": "effective_catalog_unavailable", "degraded_mode": False,
    }
    assert json.loads(catalog_response.body) == {
        "error": {
            "error_code": "INTERNAL",
            "message": "The catalog authority is temporarily unavailable.",
            "retryable": True, "retry_class": "backoff", "actor": "service",
            "next_action": "Wait a short time, then retry the request.",
        },
        "reason_code": "effective_catalog_unavailable", "degraded_mode": False,
    }
    assert marker not in repr((error.detail, error.diagnostic_code, error.exception_type))
    assert marker not in caplog.text + capsys.readouterr().err


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
    # capabilities() reads deps.effective_tools_with_provenance first when it
    # is present (tiered-fold catalog, mainline #545); tools_router.tools()
    # has no such branch and only ever calls deps.all_tools. Mock both seams
    # so the SAME customization-service failure surfaces as the SAME
    # normalized error envelope regardless of which route is under test.
    monkeypatch.setattr(
        capabilities_router.deps,
        "effective_tools_with_provenance",
        _raise(error),
        raising=False,
    )

    response = route(tenant="pinned-tenant", **kwargs)

    assert response.status_code == 503
    body = json.loads(response.body)
    assert body["reason_code"] == "effective_catalog_digest_mismatch"
    assert body["error"]["error_code"] == "INTERNAL"
    assert body["error"]["retryable"] is True
    assert "operator-only tenant path" not in response.body.decode("utf-8")
