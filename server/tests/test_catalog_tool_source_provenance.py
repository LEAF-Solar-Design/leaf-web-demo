"""Authority-safe source provenance for the effective tool catalog."""
from __future__ import annotations

import copy

import pytest

import deps


ENGINE_TOOL = {
    "name": "count-by-layer",
    "engine_op": "count_by_layer",
    "capabilities": ["drawing.read"],
    "params": {"type": "object", "properties": {}},
}
UNRELATED_ENGINE_TOOL = {
    "name": "list-layouts",
    "engine_op": "list_layouts",
    "capabilities": ["drawing.read"],
    "params": {"type": "object", "properties": {}},
}


def _catalog(monkeypatch, *, catalog_seed=(), write_seed=(), tenant=(), authored=()):
    monkeypatch.setattr(
        deps,
        "_global_tool_tiers",
        lambda: [
            ("engine_registry", [ENGINE_TOOL, UNRELATED_ENGINE_TOOL]),
            ("catalog_seed", list(catalog_seed)),
            ("write_seed", list(write_seed)),
        ],
    )
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda _tenant: list(tenant))
    monkeypatch.setattr(deps, "_AUTHORED", list(authored))
    return deps.effective_tools_with_provenance("tenant-a")


def _by_name(rows):
    return {tool["name"]: (tool, source) for tool, source in rows}


def test_plain_engine_row_is_operator_owned_and_preserves_all_tools(monkeypatch):
    rows = _catalog(monkeypatch)

    assert _by_name(rows)[ENGINE_TOOL["name"]] == (
        ENGINE_TOOL,
        deps.TOOL_SOURCE_OPERATOR_OWNED_ENGINE,
    )
    assert [tool for tool, _source in rows] == deps.all_tools("tenant-a")


def test_exact_copy_tenant_override_is_not_operator_owned_engine(monkeypatch):
    tenant_copy = copy.deepcopy(ENGINE_TOOL)

    rows = _catalog(monkeypatch, tenant=[tenant_copy])

    tool, source = _by_name(rows)[ENGINE_TOOL["name"]]
    assert tool == ENGINE_TOOL
    assert source == deps.TOOL_SOURCE_TENANT_REPO


def test_exact_copy_authored_override_is_not_operator_owned_engine(monkeypatch):
    authored_copy = {**copy.deepcopy(ENGINE_TOOL), "tenant_id": "tenant-a"}

    rows = _catalog(monkeypatch, authored=[authored_copy])

    tool, source = _by_name(rows)[ENGINE_TOOL["name"]]
    assert {key: value for key, value in tool.items() if key != "tenant_id"} == ENGINE_TOOL
    assert source == deps.TOOL_SOURCE_AUTHORED


def test_modified_override_reports_the_actual_winning_tier(monkeypatch):
    tenant_override = {**ENGINE_TOOL, "description": "tenant version"}
    authored_override = {
        **ENGINE_TOOL,
        "description": "authored version",
        "tenant_id": "tenant-a",
    }

    rows = _catalog(
        monkeypatch,
        tenant=[tenant_override],
        authored=[authored_override],
    )

    tool, source = _by_name(rows)[ENGINE_TOOL["name"]]
    assert tool["description"] == "authored version"
    assert source == deps.TOOL_SOURCE_AUTHORED


def test_unrelated_engine_tool_keeps_its_operator_owned_source(monkeypatch):
    rows = _catalog(monkeypatch, tenant=[copy.deepcopy(ENGINE_TOOL)])

    assert _by_name(rows)[UNRELATED_ENGINE_TOOL["name"]] == (
        UNRELATED_ENGINE_TOOL,
        deps.TOOL_SOURCE_OPERATOR_OWNED_ENGINE,
    )


def test_catalog_and_write_seed_tools_are_not_operator_owned(monkeypatch):
    catalog_tool = {**ENGINE_TOOL, "name": "catalog-only"}
    write_tool = {**ENGINE_TOOL, "name": "write-only"}

    rows = _by_name(
        _catalog(monkeypatch, catalog_seed=[catalog_tool], write_seed=[write_tool])
    )

    assert rows[catalog_tool["name"]][1] == deps.TOOL_SOURCE_CATALOG_SEED
    assert rows[write_tool["name"]][1] == deps.TOOL_SOURCE_WRITE_SEED


@pytest.mark.parametrize(
    "tenant_rows",
    [
        [None],
        [{}],
        [{"name": ""}],
        [{"name": "duplicate"}, {"name": "duplicate"}],
    ],
)
def test_provenance_fails_closed_on_malformed_or_ambiguous_rows(
    monkeypatch, tenant_rows
):
    with pytest.raises(deps.ToolCatalogProvenanceError):
        _catalog(monkeypatch, tenant=tenant_rows)
