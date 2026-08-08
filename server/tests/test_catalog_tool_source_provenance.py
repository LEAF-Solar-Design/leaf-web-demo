"""Authority-safe source provenance for the effective tool catalog."""
from __future__ import annotations

import copy
import json

import pytest

import deps


def _tool(name: str, marker: str = "engine"):
    return {
        "name": name,
        "engine_op": name.replace("-", "_"),
        "capabilities": ["drawing.read"],
        "params": {"type": "object", "properties": {}},
        "marker": marker,
    }


@pytest.fixture
def stores(monkeypatch, tmp_path):
    engine_store = tmp_path / "engine.json"
    catalog_store = tmp_path / "catalog.json"
    write_store = tmp_path / "write.json"
    tenant_root = tmp_path / "tenant-a"
    tenant_root.mkdir()

    monkeypatch.setattr(deps, "ENGINE_REGISTRY", engine_store)
    monkeypatch.setattr(deps, "CATALOG_TOOLS_STORE", catalog_store)
    monkeypatch.setattr(deps, "WRITE_TOOLS_STORE", write_store)
    monkeypatch.setattr(deps, "tenant_repo_dir", lambda _tenant: tenant_root)
    monkeypatch.setattr(deps, "_AUTHORED", [])

    def configure(*, engine=(), catalog=(), tenant=(), write=(), authored=()):
        for path, tools in (
            (engine_store, engine),
            (catalog_store, catalog),
            (tenant_root / "registry.json", tenant),
            (write_store, write),
        ):
            path.write_text(json.dumps({"tools": list(tools)}), encoding="utf-8")
        monkeypatch.setattr(deps, "_AUTHORED", list(authored))

    return configure


def _by_name(rows):
    return {tool["name"]: (tool, source) for tool, source in rows}


def test_plain_engine_row_is_operator_owned_and_preserves_all_tools(stores):
    engine_tool = _tool("count-by-layer")
    stores(engine=[engine_tool])

    rows = deps.effective_tools_with_provenance("tenant-a")

    assert _by_name(rows)[engine_tool["name"]] == (
        engine_tool,
        deps.TOOL_SOURCE_OPERATOR_OWNED_ENGINE,
    )
    assert [tool for tool, _source in rows] == deps.all_tools("tenant-a")


def test_empty_engine_registry_uses_operator_owned_default_fallback(stores):
    stores(engine=[])

    rows = deps.effective_tools_with_provenance("tenant-a")

    assert [tool for tool, _source in rows] == deps.all_tools("tenant-a")
    assert [tool for tool, _source in rows] == list(deps.fb.DEFAULT_TOOLS)
    assert all(
        source == deps.TOOL_SOURCE_OPERATOR_OWNED_ENGINE
        for _tool_row, source in rows
    )


def test_exact_copy_tenant_override_is_not_operator_owned_engine(stores):
    engine_tool = _tool("count-by-layer")
    tenant_copy = copy.deepcopy(engine_tool)
    stores(engine=[engine_tool], tenant=[tenant_copy])

    tool, source = _by_name(
        deps.effective_tools_with_provenance("tenant-a")
    )[engine_tool["name"]]

    assert tool == engine_tool
    assert source == deps.TOOL_SOURCE_TENANT_REPO


def test_exact_copy_authored_override_is_not_operator_owned_engine(stores):
    engine_tool = _tool("count-by-layer")
    authored_copy = {**copy.deepcopy(engine_tool), "tenant_id": "tenant-a"}
    stores(engine=[engine_tool], authored=[authored_copy])

    tool, source = _by_name(
        deps.effective_tools_with_provenance("tenant-a")
    )[engine_tool["name"]]

    assert {key: value for key, value in tool.items() if key != "tenant_id"} == engine_tool
    assert source == deps.TOOL_SOURCE_AUTHORED


def test_modified_override_reports_the_actual_winning_tier(stores):
    engine_tool = _tool("count-by-layer")
    tenant_override = _tool("count-by-layer", "tenant")
    authored_override = {
        **_tool("count-by-layer", "authored"),
        "tenant_id": "tenant-a",
    }
    stores(
        engine=[engine_tool],
        tenant=[tenant_override],
        authored=[authored_override],
    )

    tool, source = _by_name(
        deps.effective_tools_with_provenance("tenant-a")
    )[engine_tool["name"]]

    assert tool["marker"] == "authored"
    assert source == deps.TOOL_SOURCE_AUTHORED


def test_full_precedence_matches_all_tools_and_reports_each_winner(stores):
    engine = [_tool("engine-only"), _tool("engine-shadow")]
    catalog = [_tool("catalog-only", "catalog"), _tool("catalog-tenant", "catalog")]
    tenant = [
        _tool("engine-shadow", "tenant"),
        _tool("catalog-tenant", "tenant"),
        _tool("tenant-write", "tenant"),
        _tool("tenant-only", "tenant"),
    ]
    write = [
        _tool("tenant-write", "write"),
        _tool("write-authored", "write"),
        _tool("write-only", "write"),
    ]
    authored = [
        {**_tool("engine-shadow", "authored"), "tenant_id": "tenant-a"},
        {**_tool("write-authored", "authored"), "tenant_id": "tenant-a"},
    ]
    stores(
        engine=engine,
        catalog=catalog,
        tenant=tenant,
        write=write,
        authored=authored,
    )

    rows = deps.effective_tools_with_provenance("tenant-a")
    projected = [tool for tool, _source in rows]
    sources = {tool["name"]: source for tool, source in rows}

    assert projected == deps.all_tools("tenant-a")
    assert sources == {
        "engine-only": deps.TOOL_SOURCE_OPERATOR_OWNED_ENGINE,
        "engine-shadow": deps.TOOL_SOURCE_AUTHORED,
        "catalog-only": deps.TOOL_SOURCE_CATALOG_SEED,
        "catalog-tenant": deps.TOOL_SOURCE_TENANT_REPO,
        "tenant-write": deps.TOOL_SOURCE_WRITE_SEED,
        "tenant-only": deps.TOOL_SOURCE_TENANT_REPO,
        "write-authored": deps.TOOL_SOURCE_AUTHORED,
        "write-only": deps.TOOL_SOURCE_WRITE_SEED,
    }


def test_both_apis_obey_the_same_mutated_fold_order(stores, monkeypatch):
    shared_name = "tenant-write-order-probe"
    tenant_tool = _tool(shared_name, "tenant")
    write_tool = _tool(shared_name, "write")
    stores(
        engine=[_tool("engine-only")],
        tenant=[tenant_tool],
        write=[write_tool],
    )
    reordered = list(deps.EFFECTIVE_TOOL_SOURCE_PRECEDENCE)
    tenant_index = reordered.index(deps.TOOL_SOURCE_TENANT_REPO)
    write_index = reordered.index(deps.TOOL_SOURCE_WRITE_SEED)
    reordered[tenant_index], reordered[write_index] = (
        reordered[write_index], reordered[tenant_index]
    )
    monkeypatch.setattr(deps, "EFFECTIVE_TOOL_SOURCE_PRECEDENCE", tuple(reordered))

    projected = deps.all_tools("tenant-a")
    provenance = deps.effective_tools_with_provenance("tenant-a")

    assert [tool for tool, _source in provenance] == projected
    tool, source = _by_name(provenance)[shared_name]
    assert tool["marker"] == "tenant"
    assert source == deps.TOOL_SOURCE_TENANT_REPO


@pytest.mark.parametrize("malformed_source", ["tenant", "catalog", "write"])
def test_real_filtered_store_cannot_hide_a_malformed_row(stores, malformed_source):
    engine_tool = _tool("count-by-layer")
    values = {
        "engine": [engine_tool],
        "catalog": [_tool("catalog-only", "catalog")],
        "tenant": [copy.deepcopy(engine_tool)],
        "write": [_tool("write-only", "write")],
    }
    values[malformed_source].append(None)
    stores(**values)

    # The compatibility projection drops the malformed row. Authority
    # provenance reads the same real file before that filter and must stop.
    assert deps.all_tools("tenant-a")
    with pytest.raises(deps.ToolCatalogProvenanceError):
        deps.effective_tools_with_provenance("tenant-a")


def test_provenance_fails_closed_on_same_tier_duplicate(stores):
    duplicate = _tool("count-by-layer")
    stores(engine=[duplicate], tenant=[duplicate, copy.deepcopy(duplicate)])

    with pytest.raises(deps.ToolCatalogProvenanceError, match="duplicate name"):
        deps.effective_tools_with_provenance("tenant-a")


@pytest.mark.parametrize("duplicate_source", ["tenant", "catalog", "write"])
def test_real_store_same_name_collision_fails_closed(stores, duplicate_source):
    duplicate = _tool(f"{duplicate_source}-duplicate", duplicate_source)
    values = {
        "engine": [_tool("engine-only")],
        "catalog": [],
        "tenant": [],
        "write": [],
    }
    values[duplicate_source] = [duplicate, copy.deepcopy(duplicate)]
    stores(**values)

    with pytest.raises(deps.ToolCatalogProvenanceError, match="duplicate name"):
        deps.effective_tools_with_provenance("tenant-a")
