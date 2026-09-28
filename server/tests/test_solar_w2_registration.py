"""Seven W2 parity builtins registered as registry local-graph-commit capabilities."""
import copy
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import catalog
import deps
import entitlements
import jobs
import product_capability_availability as availability
import solar_local_graph
import solar_tools
import store
from solar_design_graph import GraphValidationError
from solar_graph_context import resolve_graph_context
from test_w1_design_graph import graph  # noqa: F401
from test_w1_local_graph_adapter import held
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_local_graph_rail import api, body  # noqa: F401
from test_w1_solve_commit import seed

TENANT = "fixture-tenant"
REF = {"type": "string", "minLength": 1, "maxLength": 128}
REV = {"type": "integer", "minimum": 0, "maximum": 2147483647}
# (name, family, order, invalid_request_code, facets, ledger, op, extra properties, extra required)
TOOLS = (
    ("solar-unit-sync", "settings", 10, "INVALID_UNIT_SYNC_REQUEST", [], ["unit-sync"], None,
     {"distance_unit": {"type": "string", "enum": ["Meters", "Feet"]}}, ["distance_unit"]),
    ("solar-electrical-zones", "placement", 20, "INVALID_ZONE_REQUEST", [],
     ["electrical-zone-add", "electrical-zone-assign-panels"], None,
     {"operation": {"type": "string", "enum": ["add", "assign-panels"]},
      "name": {"type": "string", "minLength": 1, "maxLength": 255},
      "color_index": {"type": "integer", "minimum": 0, "maximum": 256},
      "panel_refs": {"type": "array", "minItems": 1, "maxItems": 100000, "items": REF}},
     ["name"]),
    ("solar-panel-add", "placement", 30, "INVALID_PANEL_ADD_REQUEST", ["frames"], ["panel-add"],
     "add-panels",
     {"frame_ref": REF, "panel_refs": {"type": "array", "minItems": 1, "maxItems": 4096,
                                       "uniqueItems": True, "items": REF}},
     ["frame_ref", "panel_refs"]),
    ("solar-panel-remove", "placement", 40, "INVALID_PANEL_REMOVE_REQUEST", ["frames"],
     ["panel-remove"], "remove-panels",
     {"frame_ref": REF, "panel_refs": {"type": "array", "minItems": 1, "maxItems": 4096,
                                       "uniqueItems": True, "items": REF}},
     ["frame_ref", "panel_refs"]),
    ("solar-panel-group-delete", "placement", 50, "INVALID_GROUP_DELETE_REQUEST", ["frames"],
     ["panel-group-delete-all"], "delete-all", {}, []),
    # No uniqueItems: the builtin reports a repeated panel as DUPLICATE_PANEL_MEMBERSHIP.
    ("solar-string-add", "stringing", 70, "INVALID_STRING_ADD_REQUEST", ["panels"],
     ["string-single-add"], "add-string",
     {"ordered_panel_refs": {"type": "array", "minItems": 1, "maxItems": 4096, "items": REF}},
     ["ordered_panel_refs"]),
    ("solar-string-delete", "stringing", 80, "INVALID_STRING_DELETE_REQUEST", ["strings"],
     ["string-delete"], "delete-strings",
     {"string_refs": {"type": "array", "minItems": 1, "maxItems": 4096,
                      "uniqueItems": True, "items": REF}},
     ["string_refs"]),
)
NAMES = tuple(row[0] for row in TOOLS)


def expected_declaration(name, family, order, code, facets, ledger, op, extra, required):
    stem = name.replace("-", "_")
    builtin = "builtins/" + stem + ".py"
    properties = {"drawing_id": {"type": "string", "maxLength": 128}}
    if op is not None:
        properties["operation"] = {"type": "string", "enum": [op], "default": op}
    properties["expected_rev"] = REV
    properties.update(extra)
    head = ["operation"] if "operation" in properties else []
    record = {
        "name": name, "version": "1.0.0", "kind": "script", "family_id": family,
        "engine_op": stem, "entry": builtin,
        "params": {"type": "object", "properties": properties,
                   "required": head + ["expected_rev"] + required,
                   "additionalProperties": False},
        "returns": {"type": "object"}, "capabilities": ["drawing.write"],
        "allow_local_fallback": False,
    }
    return {
        "schema": "leaf.solar-tool.v1", "name": name, "builtin": builtin, "family": family,
        "adapter": "local-graph-commit", "entitlement": "run_write",
        "requires_persisted_graph": True, "seedable": False, "invalid_request_code": code,
        "readiness": {"kind": "facets", "facets": facets}, "engine": "server-builtin",
        "interaction": {"mode": "form"}, "record_store": "registry", "record": record,
        "ledger": ledger, "trusted_inputs": [], "maturity": "preview", "wave": 2,
        "order": order, "scenario": "w2-rooftop",
    }


def dispatch(backend, fence, tool, params):
    return solar_local_graph.run_local_graph_commit(
        backend, TENANT, tool, dict(params, drawing_id="solar"), drawing_id="solar",
        source_version=1, holder="fixture-owner", fence=fence, job_id="w2-registration-job")


def head_graph(backend):
    return resolve_graph_context(backend, TENANT, "solar", "head")["graph"]


def latest(backend):
    return store.load_manifest(backend, TENANT, "solar")["latest"]


def feet(value):
    value["project"]["units"].update(drawing_units="ft", meters_per_unit=0.3048,
                                     drawing_unit_is_feet=True)
    return value


@pytest.mark.parametrize("row", TOOLS, ids=NAMES)
def test_declaration_shape(row):
    actual = solar_tools.get(row[0])
    description = actual["record"].pop("description")
    assert type(description) is str and description.strip()
    assert actual == expected_declaration(*row)
    assert "default" not in actual["record"]["params"]["properties"]["expected_rev"]


def test_registered_as_local_graph_commits_in_order():
    tools = solar_tools.local_graph_tools()
    assert set(NAMES) <= set(tools)
    positions = [tools.index(name) for name in NAMES]
    assert positions == sorted(positions)
    assert len(availability.W1_CAPABILITIES) == 9
    assert not set(NAMES) & set(availability.W1_CAPABILITIES)
    for name in NAMES:
        assert entitlements.tool_required_capability(solar_tools.trusted_record(name)) == "run_write"


def test_catalog_view(monkeypatch):
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    monkeypatch.delenv("LEAF_ENTITLEMENTS_FILE", raising=False)
    families = catalog.build_catalog(deps.all_tools(TENANT))
    found = {row["name"]: (family["family_id"], row) for family in families
             for row in family["capabilities"] if row["name"] in NAMES}
    assert set(found) == set(NAMES)
    family, row = found["solar-panel-add"]
    assert family == "placement"
    assert row["solar"] == {
        "schema": "leaf.solar-tool-view.v1", "name": "solar-panel-add", "family": "placement",
        "wave": 2, "order": 30, "maturity": "preview", "engine": "server-builtin",
        "adapter": "local-graph-commit", "entitlement": "run_write",
        "interaction": {"mode": "form"}, "ledger": ["panel-add"],
    }
    assert found["solar-unit-sync"][0] == "settings"


def test_readiness_ready(graph):
    actual = availability.w1_local_commit_inputs(graph)
    for name in NAMES:
        assert actual[name] == {"input_ready": True, "input_reason": None}, name


def test_readiness_frames_required(graph):
    graph["frames"] = []
    for panel in graph["panels"]:
        panel["frame_ref"] = None
        panel["matrix_cell"] = None
    actual = availability.w1_local_commit_inputs(graph)
    for name in ("solar-panel-add", "solar-panel-remove", "solar-panel-group-delete"):
        assert actual[name] == {"input_ready": False, "input_reason": "frames_required"}, name


def test_readiness_unresolved_units(graph):
    graph["project"]["units"]["meters_per_unit"] *= 2
    actual = availability.w1_local_commit_inputs(graph)
    for name in NAMES:
        assert actual[name] == {"input_ready": False, "input_reason": "unresolved_units"}, name


def test_unit_sync_publishes_a_version(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with held(backend) as fence:
        receipt = dispatch(backend, fence, "solar-unit-sync",
                           {"expected_rev": 0, "distance_unit": "Feet"})
    assert receipt["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    assert receipt["before_rev"] == 0 and receipt["after_rev"] == 1
    units = head_graph(backend)["project"]["units"]
    assert units["drawing_units"] == "ft"
    assert units["meters_per_unit"] == 0.3048
    assert units["drawing_unit_is_feet"] is True


def test_zone_add_publishes_a_version(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with held(backend) as fence:
        receipt = dispatch(backend, fence, "solar-electrical-zones",
                           {"operation": "add", "expected_rev": 0, "name": "Zone Q",
                            "color_index": 3})
    assert receipt["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    zones = head_graph(backend)["electrical_zones"]
    assert len(zones) == 2
    zone = next(zone for zone in zones if zone["name"] == "Zone Q")
    assert zone["color_index"] == 3
    assert zone["panel_refs"] == []


def test_zone_add_refuses_a_duplicate_name(graph, tmp_path, monkeypatch):
    name = graph["electrical_zones"][0]["name"].lower()
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with held(backend) as fence:
        with pytest.raises(GraphValidationError) as error:
            dispatch(backend, fence, "solar-electrical-zones",
                     {"operation": "add", "expected_rev": 0, "name": name, "color_index": 3})
    assert error.value.code == "DUPLICATE_ZONE_NAME"
    assert latest(backend) == 1


def test_same_unit_sync_cannot_publish(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, feet(graph))
    with held(backend) as fence:
        with pytest.raises(GraphValidationError) as error:
            dispatch(backend, fence, "solar-unit-sync", {"expected_rev": 0, "distance_unit": "Feet"})
    assert error.value.code == "STALE_GRAPH_COMPANION"
    assert latest(backend) == 1


def test_group_delete_publishes_a_version(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, graph)
    with held(backend) as fence:
        receipt = dispatch(backend, fence, "solar-panel-group-delete",
                           {"operation": "delete-all", "expected_rev": 0})
    assert receipt["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    stored = head_graph(backend)
    assert stored["frames"] == []
    assert all(panel["frame_ref"] is None for panel in stored["panels"])


def test_zone_operation_must_be_a_string(graph):
    source = copy.deepcopy(graph)
    builtin = solar_local_graph._load_builtin("solar-electrical-zones")
    with pytest.raises(GraphValidationError) as error:
        builtin.run(graph, {"operation": ["add"], "expected_rev": 0, "name": "Z", "color_index": 3})
    assert error.value.code == "INVALID_ZONE_REQUEST"
    assert graph == source


@pytest.fixture
def w2_api(monkeypatch, request, tmp_path):
    # The records come from the real catalog fold, before the rail fixture pins find_tool.
    monkeypatch.setattr(deps, "tenant_repo_dir", lambda tenant: None)
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    monkeypatch.setattr(deps, "AUTHORED_STORE", tmp_path / "absent-authored.json")
    records = {name: deps.find_tool(name, TENANT) for name in NAMES}
    for name, record in records.items():
        assert record == solar_tools.trusted_record(name), name
    client = request.getfixturevalue("api")
    client[2].update(records)
    return client


@pytest.mark.parametrize("tool,params", [
    ("solar-unit-sync", {"distance_unit": "Yards", "expected_rev": 0}),
    ("solar-panel-add", {"operation": "remove-panels", "expected_rev": 0, "frame_ref": "f",
                         "panel_refs": ["p"]}),
], ids=["unit-sync-enum", "panel-add-operation"])
def test_studio_run_refuses_schema_violations(w2_api, tool, params):
    response = w2_api[0].post("/api/run?wait=1", json=body(w2_api, tool, params))
    env = response.json()
    assert env.get("ok") is not True, response.text
    records = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    assert all(rec["status"] != "complete" for rec in records)
    reasons = [env.get("reason_code"), (env.get("error") or {}).get("reason_code")]
    reasons += [(rec.get("error") or {}).get("reason_code") for rec in records]
    assert "tool_params_invalid" in reasons, response.text
    manifest = store.load_manifest(w2_api[1], TENANT, "solar")
    assert manifest["head"] == 1 and manifest["latest"] == 1
