"""Tenant tool projection, content pins, admission and cache boundaries."""
import ast
from copy import deepcopy
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import deps
import lab_evaluate
import lab_registry
import lab_tool_manifests as manifests


def read_tool(**changes):
    row = {"name": "qa-read-tool", "version": "1.2.3", "kind": "script",
           "description": " Read drawing values \nInternal description",
           "capability": "drawing.read", "capabilities": ["drawing.read", "drawing.read", "bad_value"],
           "script": "private.py", "entry": "main", "code": "private code",
           "params": {"type": "object", "properties": {
               "drawing_id": {"type": "string"}, "max_rows": {"type": "integer", "minimum": 2},
               "label": {"type": ["string", "null"]}}, "required": ["drawing_id", "max_rows"]},
           "returns_schema": {"type": "number", "minimum": 1}}
    row.update(changes)
    return row


@pytest.fixture(autouse=True)
def clear_cache():
    manifests._CACHE.clear()
    yield
    manifests._CACHE.clear()


def test_typed_read_projects_and_admits_graph(monkeypatch):
    row = read_tool()
    before = deepcopy(row)
    monkeypatch.setattr(deps, "effective_tools_with_provenance", lambda tenant: [(row, "tenant_repo")])
    tool, = manifests.tenant_tool_projection("tenant-a")
    assert tool.reason is None
    assert tool.result_encoding == "value"
    assert row == before
    assert tool.code_sha256 == deps.catalog_tool_digest(row).removeprefix("sha256:")
    assert tool.params_by_port == {"max-rows": "max_rows", "label": "label"}
    manifest = tool.manifest
    assert manifest["title"] == "Read drawing values"
    assert set(manifest["inputs"]) == {"max-rows", "label"}
    assert manifest["inputs"]["max-rows"] == {
        "mode": "scalar", "schema": row["params"]["properties"]["max_rows"], "unit": "none",
        "nullable": False, "required": True, "has_default": False}
    assert manifest["inputs"]["label"]["nullable"] is True
    assert manifest["sample_input"] == {"max-rows": 2, "label": ""}
    assert manifest["sample_output"] == {"result": 1}
    assert manifest["outputs"]["result"]["schema"] == row["returns_schema"]
    assert manifest["permissions"] == ["drawing.read"]
    assert manifest["settings_schema"] == {
        "type": "object", "additionalProperties": False, "required": ["drawing"],
        "properties": {"drawing": {"type": "object", "additionalProperties": False,
            "required": ["project_id", "drawing_id", "dwg_version"], "properties": {
                "project_id": {"type": "string", "minLength": 1, "maxLength": 200},
                "drawing_id": {"type": "string", "minLength": 1, "maxLength": 200},
                "dwg_version": {"type": "integer", "minimum": 1}}}}}
    lab_evaluate._contract_validators()[manifests._MANIFEST_SCHEMA].validate(manifest)
    lab_evaluate.canonical_json(manifest)
    lab_evaluate._admit_manifest(manifest)
    # A standalone graph has no pipe to satisfy required input pins.
    row["params"]["required"] = []
    graph_tool, = manifests.tenant_tool_projection("tenant-a")
    graph = {"schema": "leaf.lab-graph.v1", "blocks": [{
        "id": "node", "registry": {"id": graph_tool.name, "version": graph_tool.version,
                                     "code_sha256": graph_tool.code_sha256},
        "settings": {"drawing": {"project_id": "project", "drawing_id": "drawing", "dwg_version": 1}}}],
        "pipes": [], "outputs": {"result": {"node": "node", "port": "result"}}}
    lab_evaluate.admit_graph(graph, manifests.registry_for("tenant-a"))
    assert tool.trusted == lab_registry.TrustedBlock(
        tool.name, tool.version, "compute", "tenant", "drawing.read", None, "worker", False, "production")
    with pytest.raises(FrozenInstanceError):
        tool.name = "changed"
    with pytest.raises(TypeError):
        manifest["inputs"].clear()


@pytest.mark.parametrize("changes,source,reason", [
    ({"engine": "server-builtin", "aps_live": True}, "catalog_seed", "runs on the project graph, not as a Lab node"),
    ({}, "write_seed", "writes are not Lab nodes"),
    ({"capabilities": ["drawing.write.child"]}, "authored", "writes are not Lab nodes"),
    ({"capability": "solve.child"}, "catalog_seed", "writes are not Lab nodes"),
    ({"capability": "cloud-proposal"}, "catalog_seed", "writes are not Lab nodes"),
    ({"aps_live": True}, "catalog_seed", "live APS tools are not Lab nodes"),
    ({"kind": "engine"}, "catalog_seed", "only script tools become Lab nodes"),
    ({"execution_class": "instant"}, "catalog_seed", "execution class is not a Lab node"),
    ({"capability": "money"}, "catalog_seed", "not a read-only drawing tool"),
    ({"capability": "money.transfer"}, "catalog_seed", "not a read-only drawing tool"),
    ({"name": "bad_name"}, "catalog_seed", "tool name or version is not a Lab id"),
    ({"version": "01.2.3"}, "catalog_seed", "tool name or version is not a Lab id"),
    ({"params": {"type": "object", "properties": {"value": {}}}}, "catalog_seed", "parameters lack Lab types"),
    ({"params": {"type": "object", "properties": {"value": {"type": "string", "pattern": "^x"}}}}, "catalog_seed", "parameters lack Lab types"),
    ({"params": {"type": "object", "properties": {"value": {"type": "string", "format": "email"}}}}, "catalog_seed", "parameters lack Lab types"),
    ({"params": {"type": "object", "properties": {"_value": {"type": "number"}}}}, "catalog_seed", "parameter name is not a Lab port id"),
    ({"params": {"type": "object", "properties": {"some-value": {"type": "number"}}}}, "catalog_seed", "parameter name is not a Lab port id"),
    ({"returns_schema": {"type": "string", "minLength": 1}}, "catalog_seed", "no valid sample"),
    ({"returns_schema": {"type": ["string", "null"]}}, "catalog_seed", "manifest invalid"),
])
def test_refusals(changes, source, reason):
    tool = manifests.project_tool(read_tool(**changes), source)
    assert tool.reason == reason
    assert tool.manifest is None and tool.trusted is None and tool.params_by_port == {}
    assert tool.result_encoding is None


def test_builtin_collision():
    name = next(iter(lab_evaluate.trusted_registry().values()))["id"]
    assert manifests.project_tool(read_tool(name=name), "catalog_seed").reason == "name collides with a built-in block"


def test_returns_fallback():
    row = read_tool(returns_schema=None, returns={"type": "boolean"})
    tool = manifests.project_tool(row, "catalog_seed")
    assert tool.manifest["outputs"]["result"]["schema"] == {"type": "boolean"}
    assert tool.manifest["sample_output"] == {"result": False}
    assert tool.result_encoding == "value"


def test_default_is_carried_into_manifest():
    row = read_tool(params={"type": "object", "properties": {
        "value": {"type": "integer", "default": 4}}, "required": ["value"]})
    tool = manifests.project_tool(row, "catalog_seed")
    assert tool.reason is None
    assert tool.manifest["inputs"]["value"]["has_default"] is True
    assert tool.manifest["inputs"]["value"]["default"] == 4
    assert tool.manifest["inputs"]["value"]["schema"] == {"type": "integer"}
    assert tool.manifest["sample_input"] == {"value": 4}
    assert row["params"]["properties"]["value"]["default"] == 4
    lab_evaluate._admit_manifest(tool.manifest)


@pytest.mark.parametrize("output", [None, {"type": "object"},
                                   {"type": "string", "pattern": "^x"}])
def test_json_text_output_fallback(output):
    tool = manifests.project_tool(read_tool(returns_schema=output), "catalog_seed")
    assert tool.reason is None
    assert tool.result_encoding == "json-text"
    assert tool.manifest["outputs"]["result"]["schema"] == {
        "type": "string", "maxLength": 65536, "description": "JSON text of the tool result"}
    assert tool.manifest["sample_output"] == {"result": ""}
    lab_evaluate._admit_manifest(tool.manifest)


def test_closed_object_output():
    schema = {"type": "object", "properties": {"value": {"type": "integer"}},
              "additionalProperties": False}
    tool = manifests.project_tool(read_tool(returns_schema=schema), "catalog_seed")
    assert tool.reason is None
    assert tool.result_encoding == "value"
    assert tool.manifest["outputs"]["result"]["schema"] == schema
    assert tool.manifest["sample_output"] == {"result": {}}
    lab_evaluate._admit_manifest(tool.manifest)


def test_defaults_are_stripped_from_nested_schemas():
    schema = {"type": "object", "additionalProperties": False,
              "properties": {"values": {"type": "array", "default": [],
                                        "items": {"type": "integer", "default": 2}},
                             "default": {"type": "boolean", "default": False}},
              "default": {"values": [4]}}
    row = read_tool(params={"type": "object", "properties": {"value": schema}},
                    returns_schema=schema)
    tool = manifests.project_tool(row, "catalog_seed")
    assert tool.reason is None
    stripped = {"type": "object", "additionalProperties": False,
                "properties": {"values": {"type": "array", "items": {"type": "integer"}},
                               "default": {"type": "boolean"}}}
    pin = tool.manifest["inputs"]["value"]
    assert pin["schema"] == stripped
    assert pin["has_default"] is True and pin["default"] == {"values": [4]}
    assert tool.manifest["sample_input"] == {"value": {"values": [4]}}
    assert tool.manifest["outputs"]["result"]["schema"] == stripped
    assert tool.manifest["outputs"]["result"]["has_default"] is False
    assert tool.manifest["sample_output"] == {"result": {"values": [4]}}
    assert tool.result_encoding == "value"
    assert row["params"]["properties"]["value"] == schema
    lab_evaluate._admit_manifest(tool.manifest)


def test_unsupported_nested_parameter_schema_is_refused():
    row = read_tool(params={"type": "object", "properties": {
        "values": {"type": "array", "items": {"type": "string", "format": "email"}}}})
    assert manifests.project_tool(row, "catalog_seed").reason == "parameters lack Lab types"


@pytest.mark.parametrize("schema,expected", [
    ({"type": "string", "enum": ["first", "second"]}, "first"),
    ({"type": "number", "minimum": 3}, 3),
    ({"type": ["null", "boolean"]}, False),
    ({"type": "array", "items": {"type": "integer"}}, []),
    ({"type": "object", "properties": {}, "additionalProperties": False}, {}),
    ({"type": "null"}, None),
])
def test_samples(schema, expected):
    assert manifests._sample(schema) == expected


def test_registry_lookup_and_content_pin(monkeypatch):
    row = read_tool()
    monkeypatch.setattr(deps, "effective_tools_with_provenance", lambda tenant: [(row, "catalog_seed")])
    original = dict(lab_evaluate.trusted_registry())
    registry = manifests.registry_for("tenant")
    tool, = manifests.tenant_tool_projection("tenant")
    assert registry[f"{tool.name}@{tool.version}"] == tool.manifest
    assert lab_evaluate.trusted_registry() == original
    assert registry is not lab_evaluate.trusted_registry()
    assert manifests.trusted_for("tenant", tool.name, tool.version) == tool.trusted
    sentinel = object()
    monkeypatch.setattr(lab_registry, "lookup", lambda name, version: sentinel)
    assert manifests.trusted_for("tenant", "missing", "1.0.0") is sentinel
    assert manifests.row_for("tenant", tool.name, tool.version, "0" * 64) is None
    assert manifests.row_for("tenant", tool.name, tool.version, tool.code_sha256) == (row, tool.params_by_port)
    returned, _ = manifests.row_for("tenant", tool.name, tool.version, tool.code_sha256)
    returned["params"].clear()
    assert manifests.row_for("tenant", tool.name, tool.version, tool.code_sha256)[0] == row


def test_cache_uses_current_rows_and_tenant(monkeypatch):
    row = read_tool()
    calls = []
    def effective(tenant):
        calls.append(tenant)
        return [(row, "catalog_seed")]
    monkeypatch.setattr(deps, "effective_tools_with_provenance", effective)
    first, = manifests.tenant_tool_projection("tenant")
    assert manifests.tenant_tool_projection("tenant")[0] is first
    row["description"] = "Changed"
    changed, = manifests.tenant_tool_projection("tenant")
    assert changed is not first and changed.code_sha256 != first.code_sha256
    assert manifests.tenant_tool_projection("other")[0] is not changed
    assert calls == ["tenant", "tenant", "tenant", "other"]


def test_cache_is_bounded_and_lru(monkeypatch):
    monkeypatch.setattr(deps, "effective_tools_with_provenance", lambda tenant: [(read_tool(), "catalog_seed")])
    first, = manifests.tenant_tool_projection("tenant-0")
    second, = manifests.tenant_tool_projection("tenant-1")
    for index in range(2, 64):
        manifests.tenant_tool_projection(f"tenant-{index}")
    assert manifests.tenant_tool_projection("tenant-0")[0] is first
    manifests.tenant_tool_projection("tenant-64")
    assert len(manifests._CACHE) == 64
    assert manifests.tenant_tool_projection("tenant-0")[0] is first
    assert manifests.tenant_tool_projection("tenant-1")[0] is not second
    assert len(manifests._CACHE) == 64


def test_deps_exception_propagates_even_after_cache_hit(monkeypatch):
    monkeypatch.setattr(deps, "effective_tools_with_provenance", lambda tenant: [(read_tool(), "catalog_seed")])
    manifests.tenant_tool_projection("tenant")
    def fail(tenant):
        raise RuntimeError("catalog unavailable")
    monkeypatch.setattr(deps, "effective_tools_with_provenance", fail)
    with pytest.raises(RuntimeError, match="catalog unavailable"):
        manifests.tenant_tool_projection("tenant")


def test_projection_order(monkeypatch):
    monkeypatch.setattr(deps, "effective_tools_with_provenance", lambda tenant: [
        (read_tool(name="z-tool"), "catalog_seed"), (read_tool(name="a-tool"), "authored")])
    assert [tool.name for tool in manifests.tenant_tool_projection("tenant")] == ["a-tool", "z-tool"]


def test_import_boundary():
    tree = ast.parse(Path(manifests.__file__).read_text(encoding="utf-8"))
    imported = {alias.name.split(".")[0] for node in ast.walk(tree) if isinstance(node, ast.Import)
                for alias in node.names}
    imported.update(node.module.split(".")[0] for node in ast.walk(tree)
                    if isinstance(node, ast.ImportFrom) and node.module)
    assert not imported & {"tool_loader", "broker"}
