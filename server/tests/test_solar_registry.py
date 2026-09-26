"""Solar declarations drive shared seams without changing legacy catalog authority."""
import ast
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import catalog
import deps
import entitlements
import product_capability_availability as availability
import solar_local_graph
import solar_tools
from test_w1_design_graph import graph  # noqa: F401
from test_w1_local_graph_broker import rails, enabled  # noqa: F401
import test_w1_local_graph_broker as broker_tests


# Captured from the unmodified stores and canonical digest definition at
# 767e5a42739f92308e73a215b557bc661809d61f, before this correction.
PINNED_BASE_DIGESTS = {
    "solar-settings": "sha256:0a487cc04ce8d8f46c95645bc6ab05f75471e970364c3ce56cc5a08cb2cbe6f1",
    "solar-size-strings": "sha256:48d2c171a8c9da862ae6da44987fcb8fec528e2abe1a268bcf875dc6e236233f",
    "solar-panel-groups": "sha256:098e6563d15837eccc55d63f81b45011fa6efc679130b27c8241fef77e47425a",
    "solar-solve-proposal": "sha256:33ca2f21d777762c0e3dd1ad6925599c1d3533aa6341dffa39167fdd5728132d",
    "solar-commit-solve": "sha256:69b70748c4383c2503ed385a63942528a09719a9df69568c1c97524ef00421fa",
    "solar-correct-string": "sha256:7eafd72428523b7d36a0047cb56ab84625c7e42f285376726c4da8334db8a427",
    "solar-assign-equipment": "sha256:f6f53fc166c6247e0676f929fc7bfef6f0d46f198e8c1c8152ccb8fab26a8892",
    "solar-homeruns": "sha256:0872050b6d4aec9ac3145ba9893ded785943097bc8b60d57c8a857e6699b24d7",
    "solar-schedule": "sha256:df46c4d27c872c9f00025999ab984ca48c4bf4e0dae830ab7a5b01de8461fea9",
}


@pytest.fixture
def package(tmp_path):
    directory = tmp_path / "solar_tools"
    directory.mkdir()
    (tmp_path / "builtins").mkdir()
    for path in (SERVER / "solar_tools").glob("*.json"):
        (directory / path.name).write_bytes(path.read_bytes())
        (tmp_path / "builtins" / (path.stem + ".py")).write_text(
            "def input_readiness(graph):\n"
            "    return {'input_ready': True, 'input_reason': None}\n", encoding="utf-8")
    for name in ("capability_families.json", "write_tools.json", "catalog_tools.json"):
        (tmp_path / name).write_bytes((SERVER / name).read_bytes())
    return tmp_path, directory


def load_package(package):
    root, directory = package
    return solar_tools.load(directory=directory, server_dir=root)


def write_declaration(package, declaration):
    root, directory = package
    stem = declaration["name"].replace("-", "_")
    (directory / (stem + ".json")).write_text(json.dumps(declaration), encoding="utf-8")
    (root / declaration["builtin"]).write_text(
        "import copy\n"
        "calls = []\n"
        "results = []\n"
        "def input_readiness(graph):\n"
        "    return {'input_ready': False, 'input_reason': 'example_required'}\n"
        "def run(graph, params):\n"
        "    calls.append((copy.deepcopy(graph), copy.deepcopy(params)))\n"
        "    after = copy.deepcopy(graph)\n"
        "    after['parent_rev'] = graph['rev']\n"
        "    after['rev'] = graph['rev'] + 1\n"
        "    results.append(after)\n"
        "    return after\n", encoding="utf-8")


def extra_declaration(package):
    declaration = solar_tools.get("solar-settings")
    declaration.update(name="solar-example", builtin="builtins/solar_example.py",
                       scenario="w2-test", wave=2, order=5, seedable=False,
                       record_store="registry", readiness={"kind": "hook"})
    record = solar_tools.trusted_record("solar-settings")
    record.update(name=declaration["name"], entry=declaration["builtin"], engine_op="solar_example")
    declaration["record"] = record
    write_declaration(package, declaration)
    return declaration


def bind_records(monkeypatch, registry):
    monkeypatch.setattr(solar_tools, "registry_records", registry.registry_records)
    monkeypatch.setattr(deps, "tenant_repo_dir", lambda tenant: None)
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])


def test_manifest_equals_a_fresh_scan():
    expected = tuple(sorted(
        (path.stem.replace("_", "-"), hashlib.sha256(path.read_bytes()).hexdigest())
        for path in (SERVER / "solar_tools").glob("*.json")))
    assert solar_tools.manifest() == expected
    assert solar_tools.load().manifest() == expected


def _assert_shipped_declarations(registry):
    entries = registry.entries()
    names = {row["name"] for row in entries}
    assert names == {name for name, _ in registry.manifest()}
    assert entries == tuple(sorted(entries, key=lambda row: (row["wave"], row["order"], row["name"])))
    for record in registry.registry_records():
        assert record["name"] in names
        assert registry.get(record["name"])["record"] == record
    expected = [
        ("solar-settings", 10, "local-graph-commit", "run_write", True, True,
         "INVALID_SETTINGS_REQUEST", {"kind": "facets", "facets": []}, "server-builtin", "write_seed", []),
        ("solar-size-strings", 20, None, "run_write", True, False, None,
         {"kind": "w1-chain"}, "cloud-service", "write_seed", ["string-sizer"]),
        ("solar-panel-groups", 30, None, "run_write", True, False, None,
         {"kind": "w1-chain"}, "autocad-lane", "write_seed", ["panel-group-create"]),
        ("solar-solve-proposal", 40, "cloud-proposal", "solve", False, False, None,
         {"kind": "w1-chain"}, "cloud-service", "catalog_seed", ["solve"]),
        ("solar-commit-solve", 50, None, "run_write", True, False, None,
         {"kind": "w1-chain"}, "server-builtin", "write_seed", ["solve"]),
        ("solar-correct-string", 60, "local-graph-commit", "run_write", True, False,
         "INVALID_CORRECTION", {"kind": "facets", "facets": ["strings"]}, "server-builtin", "write_seed", []),
        ("solar-assign-equipment", 70, None, "run_write", True, False, None,
         {"kind": "w1-chain"}, "autocad-lane", "write_seed", ["inverter-add"]),
        ("solar-homeruns", 80, None, "run_write", True, False, None,
         {"kind": "w1-chain"}, "autocad-lane", "write_seed", ["homeruns"]),
        ("solar-schedule", 90, None, "run_write", True, False, None,
         {"kind": "w1-chain"}, "autocad-lane", "write_seed", []),
    ]
    keys = ("name", "order", "adapter", "entitlement", "requires_persisted_graph", "seedable",
            "invalid_request_code", "readiness", "engine", "record_store", "ledger")
    actual = {row["name"]: tuple(row[key] for key in keys) for row in entries}
    for expected_row in expected:
        assert actual[expected_row[0]] == expected_row
    for name, *_ in expected:
        row = registry.get(name)
        assert row["schema"] == solar_tools.SCHEMA == "leaf.solar-tool.v1"
        assert row["builtin"] == "builtins/" + row["name"].replace("-", "_") + ".py"
        assert row["family"] == "stringing"
        assert row["trusted_inputs"] == []
        assert row["record"] is None
        assert row["wave"] == 1
        assert row["maturity"] == "production"
        assert row["scenario"] == "w1-rooftop"
        assert row["interaction"] == {"mode": "form"}


def test_real_declarations_validate():
    registry = solar_tools.load()
    assert {row["name"] for row in registry.entries()} == {
        path.stem.replace("_", "-") for path in (SERVER / "solar_tools").glob("*.json")}
    _assert_shipped_declarations(registry)


def test_a_tenth_declaration_loads_without_editing_shared_tests(package):
    declaration = extra_declaration(package)
    registry = load_package(package)
    _assert_shipped_declarations(registry)
    assert {row["name"] for row in registry.entries()} == {
        row["name"] for row in solar_tools.entries()} | {declaration["name"]}
    assert declaration["record"] in registry.registry_records()


@pytest.mark.parametrize("name,is_directory", [
    ("readme.txt", False), ("solar_bad.py", False), ("solar_Bad.json", False),
    ("solar_bad_.json", False), ("solar_bad.json", True), ("extra", True),
])
def test_stray_entries_are_refused(package, name, is_directory):
    _, directory = package
    path = directory / name
    if is_directory:
        path.mkdir()
    else:
        path.write_text("{}", encoding="utf-8")
    with pytest.raises(solar_tools.SolarRegistryError):
        load_package(package)
    # Fresh module execution must reject the package before exporting a registry.
    init = directory / "__init__.py"
    init.write_bytes((SERVER / "solar_tools" / "__init__.py").read_bytes())
    spec = importlib.util.spec_from_file_location("_invalid_solar_registry", init)
    module = importlib.util.module_from_spec(spec)
    with pytest.raises(ValueError, match="unexpected registry entry"):
        spec.loader.exec_module(module)


@pytest.mark.parametrize("field,value", [
    ("schema", "other"), ("name", "solar-other"), ("unexpected", True),
    ("family", "missing-family"), ("family", []), ("adapter", "other"),
    ("adapter", []), ("adapter", None), ("trusted_inputs", ["untrusted"]),
    ("trusted_inputs", ""), ("requires_persisted_graph", 1), ("seedable", 1),
    ("seedable", False), ("invalid_request_code", None), ("invalid_request_code", 1),
    ("entitlement", "admin"), ("engine", "other"), ("maturity", "other"),
    ("wave", 0), ("wave", 6), ("wave", True), ("order", -1), ("order", 10000),
    ("order", 1.0), ("scenario", None), ("scenario", ""), ("ledger", [1]),
    ("record_store", "other"), ("record_store", "registry"), ("record", {}),
    ("builtin", "../solar_settings.py"), ("builtin", "builtins/solar_other.py"),
    ("readiness", {"kind": "other"}), ("readiness", {"kind": "facets"}),
    ("readiness", {"kind": "facets", "facets": [False]}),
    ("readiness", {"kind": "hook", "facets": []}),
    ("interaction", {"mode": "other"}), ("interaction", {"mode": "form", "pick": []}),
    ("interaction", {"mode": "pick", "pick": [], "preview": "none", "commit": "explicit"}),
])
def test_malformed_declarations_are_refused(package, field, value):
    path = package[1] / "solar_settings.json"
    declaration = json.loads(path.read_text(encoding="utf-8"))
    declaration[field] = value
    path.write_text(json.dumps(declaration), encoding="utf-8")
    with pytest.raises(solar_tools.SolarRegistryError):
        load_package(package)


@pytest.mark.parametrize("key", tuple(solar_tools.get("solar-settings")))
def test_missing_keys_are_refused(package, key):
    path = package[1] / "solar_settings.json"
    row = json.loads(path.read_text(encoding="utf-8"))
    del row[key]
    path.write_text(json.dumps(row), encoding="utf-8")
    with pytest.raises(solar_tools.SolarRegistryError):
        load_package(package)


@pytest.mark.parametrize("payload", [
    '{"key": 1, "key": 2}', "NaN", "Infinity", "-Infinity", "1e999",
])
def test_non_json_and_duplicate_values_are_refused(package, payload):
    declaration = extra_declaration(package)
    declaration["record"]["probe"] = "replace_me"
    write_declaration(package, declaration)
    path = package[1] / "solar_example.json"
    source = path.read_text(encoding="utf-8")
    path.write_text(source.replace('"replace_me"', payload), encoding="utf-8")
    with pytest.raises(solar_tools.SolarRegistryError):
        load_package(package)


def test_missing_builtin_is_refused(package):
    (package[0] / "builtins" / "solar_settings.py").unlink()
    with pytest.raises(solar_tools.SolarRegistryError, match="missing builtin"):
        load_package(package)


def test_declaration_size_is_bounded(package):
    path = package[1] / "solar_settings.json"
    path.write_bytes(path.read_bytes() + b" " * 65536)
    with pytest.raises(solar_tools.SolarRegistryError, match="65536"):
        load_package(package)


def test_declaration_count_is_bounded(package):
    for index in range(solar_tools.MAX_DECLARATIONS):
        (package[1] / ("solar_extra" + str(index) + ".json")).write_text("{}", encoding="utf-8")
    with pytest.raises(solar_tools.SolarRegistryError, match="too many"):
        load_package(package)


def test_working_directory_decoy_is_ignored(tmp_path, monkeypatch):
    decoy = tmp_path / "solar_tools"
    decoy.mkdir()
    (decoy / "solar_trap.json").write_text("not JSON", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert solar_tools.load().manifest() == solar_tools.manifest()


def test_ledger_ids_exist_as_t_or_f_rows():
    ledger = json.loads((SERVER.parent / "docs" / "parity" / "solar-ledger.json").read_text(encoding="utf-8"))
    ids = {row["capability"] for row in ledger["rows"] if row["class"] in ("T", "F")}
    assert {item for row in solar_tools.entries() for item in row["ledger"]} <= ids


def test_w1_view_is_the_scenario_subset_sharing_rows():
    assert list(availability.W1_CAPABILITIES) == [
        name for name, row in availability.SOLAR_CAPABILITIES.items() if row["scenario"] == "w1-rooftop"]
    for name, row in availability.W1_CAPABILITIES.items():
        assert row is availability.SOLAR_CAPABILITIES[name]


def test_local_graph_tools_derive_from_declarations():
    assert solar_local_graph.LOCAL_GRAPH_TOOLS == tuple(solar_tools.local_graph_tools())
    assert solar_local_graph.LOCAL_GRAPH_TOOLS == tuple(
        row["name"] for row in solar_tools.entries() if row["adapter"] == "local-graph-commit")


def test_one_seed_tool(package):
    assert [row["name"] for row in solar_tools.entries() if row["seedable"]] == [solar_tools.seed_tool()]
    declaration = solar_tools.get("solar-correct-string")
    declaration["seedable"] = True
    write_declaration(package, declaration)
    with pytest.raises(solar_tools.SolarRegistryError, match="exactly one"):
        load_package(package)


def test_every_adapter_is_known_and_bound():
    assert solar_tools.ADAPTER_KINDS == (solar_local_graph.ADAPTER_KIND, availability.CLOUD_PROPOSAL_ADAPTER)
    for row in solar_tools.entries():
        assert row["adapter"] is None or row["adapter"] in solar_tools.ADAPTER_KINDS
        if row["adapter"] == solar_local_graph.ADAPTER_KIND:
            assert callable(solar_local_graph._load_builtin(row["name"]).run)
            assert availability.is_local_graph_commit({"name": row["name"]})
        elif row["adapter"] == availability.CLOUD_PROPOSAL_ADAPTER:
            assert availability.is_cloud_proposal({"name": row["name"]})


def test_declared_entitlement_matches_record_class(package, monkeypatch):
    extra_declaration(package)
    registry = load_package(package)
    monkeypatch.setattr(availability, "SOLAR_CAPABILITIES", registry.capability_table())
    for row in registry.entries():
        record = registry.trusted_record(row["name"])
        caps = record.get("capabilities", [])
        expected = "run_write" if "drawing.write" in caps else "solve" if "solve" in caps else "run_read"
        assert row["entitlement"] == expected
        assert entitlements.tool_required_capability(record) == expected


def test_registry_record_folds_into_write_seed(package, monkeypatch):
    declaration = extra_declaration(package)
    registry = load_package(package)
    bind_records(monkeypatch, registry)
    legacy = json.loads(deps.WRITE_TOOLS_STORE.read_text(encoding="utf-8"))["tools"]
    assert deps.load_seed_write_tools() == legacy + [declaration["record"]]
    assert declaration["record"] in deps.all_tools("solar-registry-test")
    rows = deps.effective_tools_with_provenance("solar-registry-test")
    assert (declaration["record"], deps.TOOL_SOURCE_WRITE_SEED) in rows


@pytest.mark.parametrize("store_name", ["WRITE_TOOLS_STORE", "CATALOG_TOOLS_STORE", "ENGINE_REGISTRY"])
@pytest.mark.parametrize("loader", ["load_seed_write_tools", "all_tools", "effective_tools_with_provenance"])
def test_registry_record_collision_is_refused(package, monkeypatch, store_name, loader):
    declaration = extra_declaration(package)
    registry = load_package(package)
    bind_records(monkeypatch, registry)
    path = package[0] / (store_name + ".json")
    path.write_text(json.dumps({"tools": [declaration["record"]]}), encoding="utf-8")
    monkeypatch.setattr(deps, store_name, path)
    with pytest.raises(deps.ToolCatalogCollisionError, match="solar-example"):
        getattr(deps, loader)()


def test_trusted_record_legacy_and_registry(package):
    declaration = extra_declaration(package)
    registry = load_package(package)
    assert registry.trusted_record(declaration["name"]) == declaration["record"]
    assert registry.trusted_record("solar-settings") == solar_tools.trusted_record("solar-settings")
    assert registry.trusted_record("solar-solve-proposal") == solar_tools.trusted_record("solar-solve-proposal")
    assert registry.trusted_record("absent") is None
    record = registry.trusted_record(declaration["name"])
    record["params"]["properties"].clear()
    assert registry.trusted_record(declaration["name"]) == declaration["record"]
    (package[0] / "write_tools.json").write_text('{"tools": []}', encoding="utf-8")
    assert registry.trusted_record("solar-settings") is None


def test_catalog_view_requires_the_trusted_record(package):
    declaration = extra_declaration(package)
    registry = load_package(package)
    for name in ("solar-settings", "solar-solve-proposal", declaration["name"]):
        trusted = registry.trusted_record(name)
        before = copy.deepcopy(trusted)
        view = registry.catalog_view(trusted)
        assert view["schema"] == "leaf.solar-tool-view.v1"
        assert view["name"] == name
        projected = dict(trusted, catalog_digest="digest", tool_manifest_sha256="digest",
                         catalog_commit="commit", effective_catalog_digest="digest",
                         _aps_live_runtime_authorized=object())
        assert registry.catalog_view(projected) == view
        assert registry.catalog_view(dict(trusted, description="tenant replacement")) is None
        assert registry.catalog_view(dict(trusted, unknown=True)) is None
        assert trusted == before
    assert registry.catalog_view(None) is None
    assert registry.catalog_view({"name": "unknown"}) is None


def test_catalog_digests_unchanged_for_the_nine(monkeypatch):
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    legacy = {}
    for filename in ("write_tools.json", "catalog_tools.json"):
        legacy.update({row["name"]: row for row in json.loads((SERVER / filename).read_text(encoding="utf-8"))["tools"]})
    tools = deps.all_tools("solar-registry-test")
    before = copy.deepcopy(tools)
    families = catalog.build_catalog(tools)
    views = {row["name"] for family in families for row in family["capabilities"] if "solar" in row}
    assert views == set(solar_tools.capability_table())
    assert PINNED_BASE_DIGESTS.keys() <= views
    for tool in tools:
        if tool["name"] in PINNED_BASE_DIGESTS:
            assert tool == legacy[tool["name"]]
            assert deps.catalog_tool_digest(tool) == PINNED_BASE_DIGESTS[tool["name"]]
    assert tools == before


def test_catalog_digests_match_the_pinned_base_values(monkeypatch):
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    tools = {tool["name"]: tool for tool in deps.all_tools("solar-registry-test")}
    for name, expected in PINNED_BASE_DIGESTS.items():
        assert deps.catalog_tool_digest(tools[name]) == expected


def test_no_migrated_literal_in_shared_seams():
    for filename, banned in (
        ("solar_local_graph.py", {"solar-settings", "solar-correct-string"}),
        ("product_capability_availability.py", {"solar-settings", "solar-correct-string"}),
        ("entitlements.py", {"solar-solve-proposal"}),
    ):
        tree = ast.parse((SERVER / filename).read_text(encoding="utf-8"))
        strings = {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        assert not strings.intersection(banned), filename
    source = (SERVER / "solar_local_graph.py").read_text(encoding="utf-8")
    assert all(word not in source for word in ("tool_loader", "import_module", "exec("))


def test_extra_local_declaration_needs_no_shared_edit(package, monkeypatch):
    declaration = extra_declaration(package)
    registry = load_package(package)
    table = registry.capability_table()
    assert {name: row for name, row in table.items() if row["scenario"] == "w1-rooftop"} == availability.W1_CAPABILITIES
    assert declaration["name"] in registry.local_graph_tools()
    for name in registry.local_graph_tools():
        assert table[name]["adapter"] == "local-graph-commit"
    monkeypatch.setattr(availability, "SOLAR_CAPABILITIES", table)
    assert availability.is_local_graph_commit({"name": declaration["name"]})


def _assert_valid_registered_dispatch(monkeypatch, graph, declaration, module):
    backend = {"intake": b"registry dispatch intake"}
    published = []
    before_sha = solar_local_graph.digest(graph)
    intake_sha = hashlib.sha256(backend["intake"]).hexdigest()

    def context(backend_arg, tenant_id, drawing_id, version, **kwargs):
        assert backend_arg is backend
        assert (tenant_id, drawing_id) == ("registry-test", "drawing")
        assert version in (1, 2)
        value = graph if version == 1 else published[0]
        return {"representation": "intake", "graph": value,
                "graph_sha256": solar_local_graph.digest(value),
                "resolved_version": version, "project_id": graph["project"]["id"]}

    def publish(backend_arg, tenant_id, drawing_id, *, before, after, **kwargs):
        assert backend_arg is backend
        assert (tenant_id, drawing_id) == ("registry-test", "drawing")
        assert before == graph
        assert after is module.results[0]
        published.append(after)
        return {"version": 2, "parent_version": 1, "replayed": False,
                "graph_sha256": solar_local_graph.digest(after), "intake_sha256": intake_sha}

    monkeypatch.setattr(solar_local_graph, "resolve_graph_context", context)
    monkeypatch.setattr(solar_local_graph, "publish_version", publish)
    monkeypatch.setattr(solar_local_graph.store, "resolve_version", lambda *args: (None, "intake"))
    params = {"expected_rev": graph["rev"], "changes": {"panels_in_sequence": 3}}
    original_graph = copy.deepcopy(graph)
    original_params = copy.deepcopy(params)
    result = solar_local_graph.run_local_graph_commit(
        backend, "registry-test", declaration["name"], params, drawing_id="drawing",
        source_version=1, holder="holder", fence=1, job_id="job")
    assert module.calls == [(original_graph, original_params)]
    assert len(module.results) == len(published) == 1
    assert graph == original_graph
    assert params == original_params
    assert result == {
        "schema_version": solar_local_graph.RESULT_SCHEMA, "adapter": solar_local_graph.ADAPTER_KIND,
        "tenant_id": "registry-test", "job_id": "job", "tool": declaration["name"],
        "project_id": graph["project"]["id"], "drawing_id": "drawing",
        "request_sha256": solar_local_graph.request_digest(declaration["name"], "drawing", 1, params),
        "new_version": {"drawing_id": "drawing", "version": 2, "parent": 1},
        "before_graph_sha256": before_sha, "graph_sha256": solar_local_graph.digest(module.results[0]),
        "intake_sha256": intake_sha, "before_rev": graph["rev"],
        "after_rev": module.results[0]["rev"], "drawing_changed": True, "replayed": False,
    }


def test_registered_capability_is_dispatchable_through_local_graph(package, monkeypatch, graph):
    declaration = extra_declaration(package)
    registry = load_package(package)
    # Keep the already imported dispatcher and its real loader. Only the
    # validated registry and the packaged server location belong to the fixture.
    monkeypatch.setattr(solar_tools, "_REGISTRY", registry)
    monkeypatch.setattr(solar_local_graph, "__file__", str(package[0] / "solar_local_graph.py"))
    solar_local_graph._load_builtin.cache_clear()
    try:
        assert solar_local_graph.local_graph_tools() == registry.local_graph_tools()
        assert declaration["name"] in solar_local_graph.local_graph_tools()
        module = solar_local_graph._load_builtin(declaration["name"])
        assert Path(module.__file__).resolve() == (package[0] / declaration["builtin"]).resolve()
        assert module.input_readiness({}) == {
            "input_ready": False, "input_reason": "example_required"}
        assert solar_local_graph._load_builtin(declaration["name"]) is module
        with pytest.raises(solar_local_graph.GraphValidationError, match=declaration["invalid_request_code"]):
            solar_local_graph.run_local_graph_commit(
                None, "registry-test", declaration["name"], [], drawing_id="drawing",
                source_version=1, holder="holder", fence=1, job_id="job")
        with pytest.raises(solar_local_graph.GraphValidationError, match="UNKNOWN_LOCAL_GRAPH_TOOL"):
            solar_local_graph._load_builtin("solar-unregistered")
        _assert_valid_registered_dispatch(monkeypatch, graph, declaration, module)
    finally:
        solar_local_graph._load_builtin.cache_clear()


def test_registered_capability_dispatch_runs_the_builtin_for_a_valid_request(package, monkeypatch, graph):
    declaration = extra_declaration(package)
    registry = load_package(package)
    monkeypatch.setattr(solar_tools, "_REGISTRY", registry)
    monkeypatch.setattr(solar_local_graph, "__file__", str(package[0] / "solar_local_graph.py"))
    solar_local_graph._load_builtin.cache_clear()
    try:
        module = solar_local_graph._load_builtin(declaration["name"])
        _assert_valid_registered_dispatch(monkeypatch, graph, declaration, module)
    finally:
        solar_local_graph._load_builtin.cache_clear()


def test_registry_cloud_proposal_is_refused(package):
    declaration = extra_declaration(package)
    declaration["adapter"] = "cloud-proposal"
    write_declaration(package, declaration)
    with pytest.raises(solar_tools.SolarRegistryError, match="registry cloud proposal"):
        load_package(package)


def test_trusted_record_reads_fresh(package):
    registry = load_package(package)
    path = package[0] / "write_tools.json"
    original = registry.trusted_record("solar-settings")
    stamp = path.stat()
    before = path.read_bytes()
    data = json.loads(before)
    record = next(row for row in data["tools"] if row["name"] == "solar-settings")
    record["description"] = "x" * len(record["description"])
    after = before.replace(json.dumps(original["description"]).encode("utf-8"),
                           json.dumps(record["description"]).encode("utf-8"))
    assert len(after) == len(before)
    path.write_bytes(after)
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert registry.trusted_record("solar-settings") == record
    assert registry.trusted_record("solar-settings") != original


def test_catalog_uses_one_trusted_snapshot(monkeypatch):
    calls = []
    original = solar_tools._REGISTRY._store

    def tracked(store):
        calls.append(store)
        return original(store)

    monkeypatch.setattr(solar_tools._REGISTRY, "_store", tracked)
    records = [solar_tools.trusted_record(row["name"]) for row in solar_tools.entries()]
    calls.clear()
    catalog.build_catalog(records)
    assert calls == ["write_seed", "catalog_seed"]


@pytest.mark.parametrize("key", ["name", "entry", "family_id"])
def test_registry_record_identity_is_bound(package, key):
    declaration = extra_declaration(package)
    declaration["record"][key] = "wrong"
    write_declaration(package, declaration)
    with pytest.raises(solar_tools.SolarRegistryError, match="record disagrees"):
        load_package(package)


def test_pick_keys_belong_to_record_params(package):
    declaration = extra_declaration(package)
    declaration["interaction"] = {"mode": "pick", "preview": "highlight", "commit": "explicit", "pick": [
        {"kind": "entity", "key": "expected_rev", "role": "string", "repeat": False,
         "ordered": True, "min": 1, "max": 1},
    ]}
    write_declaration(package, declaration)
    assert load_package(package).get(declaration["name"])["interaction"] == declaration["interaction"]
    declaration["interaction"]["pick"][0]["key"] = "not_a_parameter"
    write_declaration(package, declaration)
    with pytest.raises(solar_tools.SolarRegistryError, match="pick key absent"):
        load_package(package)


def test_broker_uses_trusted_registry_record(package, enabled, monkeypatch):
    declaration = solar_tools.get("solar-settings")
    declaration.update(record_store="registry", record=copy.deepcopy(enabled[2]))
    write_declaration(package, declaration)
    registry = load_package(package)
    calls = []

    def trusted(name):
        calls.append(name)
        return registry.trusted_record(name)

    monkeypatch.setattr(solar_tools, "trusted_record", trusted)
    status, _ = broker_tests.call(enabled)
    assert status == 200
    assert calls == [declaration["name"]]
    monkeypatch.setattr(solar_tools, "trusted_record", lambda name: None)
    status, body = broker_tests.call(enabled)
    assert status == 400
    assert body["error"]["reason_code"] == "local_graph_commit_invalid"


def _record_readiness_calls(monkeypatch):
    calls = []
    builtins = {}

    def load_builtin(builtin):
        if builtin not in builtins:
            class Builtin:
                @staticmethod
                def input_readiness(value):
                    calls.append((builtin, value))
                    reason = Path(builtin).stem.removeprefix("solar_") + "_required"
                    return {"input_ready": False, "input_reason": reason}

            builtins[builtin] = Builtin
        return builtins[builtin]

    monkeypatch.setattr(availability, "_load_readiness_builtin", load_builtin)
    return calls


@pytest.mark.parametrize("adapter", ["local-graph-commit", None])
def test_hook_readiness_uses_its_own_builtin(package, monkeypatch, graph, adapter):
    declaration = extra_declaration(package)
    declaration["adapter"] = adapter
    write_declaration(package, declaration)
    registry = load_package(package)
    monkeypatch.setattr(solar_tools, "entries", registry.entries)
    calls = _record_readiness_calls(monkeypatch)
    result = availability.w1_local_commit_inputs(graph)
    assert result[declaration["name"]] == {"input_ready": False, "input_reason": "example_required"}
    assert [value for builtin, value in calls if builtin == declaration["builtin"]] == [graph]
    assert calls == [(row["builtin"], graph) for row in registry.entries()
                     if row["readiness"]["kind"] == "hook"]


def test_a_second_hook_declaration_needs_no_shared_test_edit(package, monkeypatch, graph):
    declaration = extra_declaration(package)
    second = copy.deepcopy(declaration)
    second.update(name="solar-next", builtin="builtins/solar_next.py", order=6)
    second["record"].update(name=second["name"], entry=second["builtin"], engine_op="solar_next")
    write_declaration(package, second)
    registry = load_package(package)
    monkeypatch.setattr(solar_tools, "entries", registry.entries)
    calls = _record_readiness_calls(monkeypatch)
    result = availability.w1_local_commit_inputs(graph)
    for row, reason in ((declaration, "example_required"), (second, "next_required")):
        assert result[row["name"]] == {"input_ready": False, "input_reason": reason}
        assert [value for builtin, value in calls if builtin == row["builtin"]] == [graph]
    assert calls == [(row["builtin"], graph) for row in registry.entries()
                     if row["readiness"]["kind"] == "hook"]
