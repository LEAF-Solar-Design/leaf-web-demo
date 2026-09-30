"""solar-color-strings: LEAFCOLORSTRINGS on the design graph through the electrical state bridge, as a
local-graph-read. The recorded i3 receipt replayed on the PUBLISHED i2 graph (assign-strings committed
through the rail, then read at the new head), the kernel's small case (inverter groups coloured 1 and 2,
colour 7 unassigned), the untyped family pinned and the typed family measured, every named refusal, the
registry and the read rail. Nothing here writes a drawing version."""
import copy
import hashlib
import json
from collections import Counter

import pytest
from jsonschema import Draft7Validator

import catalog
import deps
import entitlements
import jobs
import product_capability_availability as availability
import solar_electrical_state_bridge as bridge
import solar_local_graph
import solar_local_read as local
import solar_tools
import store
from leaf_cloud_client import canonical_bytes
from solar_design_graph import GraphValidationError, validate_graph
from test_w1_design_graph import app_id, graph  # noqa: F401
from test_w1_local_graph_adapter import held
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
import test_w1_local_graph_rail as rail
from test_w1_solve_commit import seed
from test_solar_w2_registration import dispatch, head_graph, latest
import test_solar_ground_topology as topo
import test_solar_tool_solar_assign_strings as assign

TOOL = "solar-color-strings"
TENANT = "fixture-tenant"
ASSIGN = {"operation": "assign-strings", "accept_excess_capacity": True}
LINE = "LEAFCOLORSTRINGS: recoloured {} of {} strings across {} inverter(s); drew 0 in-block module overlay(s)."


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def builtin():
    return local._load_builtin(TOOL)


def colours(output):
    return [row["colour"] for row in output["strings"]]


# --- graph variants -----------------------------------------------------------------------------------------------


def assigned(graph, name):
    """assign-strings' result on the named assign-test variant (the commit tool's own answer)."""
    return assign.call(assign.variant(graph, name))["graph"]


def l2_assigned(graph):
    return assigned(graph, "L2")


def renumbered(graph):
    g = l2_assigned(graph)
    g["inverters"][0]["number"], g["inverters"][1]["number"] = 7, 12
    return g


def tagged(tag):
    def build(graph):
        g = l2_assigned(graph)
        g["strings"][0]["circuit_tag"] = tag
        return g
    return build


def routeless(graph):
    g = l2_assigned(graph)
    g["strings"][0]["route"] = []
    return g


def type_key_b(graph):
    g = l2_assigned(graph)
    for inverter in g["inverters"]:
        inverter["type_key"] = "B"
    return g


def units_doubled(graph):
    g = l2_assigned(graph)
    g["project"]["units"]["meters_per_unit"] *= 2
    return g


def duplicate_number(graph):
    g = assign.variant(graph, "A")
    assign._duplicate_number(g)
    return g


def number_zero(graph):
    g = l2_assigned(graph)
    g["inverters"][0]["number"] = 0
    return g


def levels_ambiguous(graph):
    """An L1/L2 drawing with an L1 device and one string wired straight to a second L2."""
    g = topo.topology_of(graph)
    g["inverters"].append(topo.central(3, 2, [], position=[40, 0], mppt_count=2, total_dc_inputs=4))
    string = copy.deepcopy(g["strings"][0])
    string.update(id=app_id("string", 3), circuit_tag="S3", inverter_ref=app_id("inverter", 3),
                  to_ref=app_id("inverter", 3), ordered_panel_refs=[], module_count=0, from_ref=None)
    g["strings"].append(string)
    g["inverters"][-1]["input_assignments"] = [{"string_ref": string["id"], "mppt_letter": "A", "input_number": 0}]
    return g


def schema_invalid(graph):
    g = l2_assigned(graph)
    del g["strings"][0]["route"]
    return g


# --- the receipt on the published graph ---------------------------------------------------------------------------


def read(backend, version, job_id):
    return local.run_local_graph_read(backend, TENANT, TOOL, {"drawing_id": "solar"}, drawing_id="solar",
                                      source_version=version, job_id=job_id)


def test_color_strings_i3_receipt_on_the_published_i2_graph(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, assign.i2_graph(graph))
    with held(backend) as fence:
        receipt = dispatch(backend, fence, "solar-assign-strings", dict(ASSIGN, expected_rev=0))
    assert receipt["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    published = head_graph(backend)
    assert sha(published) == "ae0f972ee14324b7953a2ae83c47995c64b431a9d5214804ae4e77c529da837f"
    result = read(backend, 2, "color-strings-i3")
    output = result["output"]
    assert (result["source_version"], result["drawing_changed"]) == (2, False)
    assert result["graph_sha256"] == local.digest(published)
    assert (result["output_bytes"], result["output_sha256"]) == (
        13559, "97ae21878a9b6d71e0b4e8de68e8a6f66725cfc16af4a9630b821f7d1504879d")
    assert output["lines"] == [LINE.format(173, 173, 5)]
    assert output["counts"] == {"strings": 173, "recoloured": 173}
    assert output["colour_family"] == "untyped" and output["schema"] == "leaf.solar-string-colours.v1"
    assert [row["string_ref"] for row in output["strings"]] == [s["id"] for s in published["strings"]]
    source = {s["id"]: s["provenance"]["source_handle"] for s in published["strings"]}
    expected = assign.receipt_rows("color-strings", "rooftop-inverters-i3")
    assert {source[row["string_ref"]]: row["colour"] for row in output["strings"]} == \
        {handle: row[3] for handle, row in expected.items()}
    numbers = {i["id"]: i["number"] for i in published["inverters"]}
    wired = {s["id"]: numbers[s["inverter_ref"]] for s in published["strings"]}
    assert sorted(Counter((wired[row["string_ref"]], row["colour"]) for row in output["strings"]).items()) == [
        ((1, 1), 36), ((2, 2), 36), ((5, 6), 36), ((6, 4), 36), ((7, 1), 29)]
    proof = local.graph_read_provenance(result, {"drawing_id": "solar"}, TENANT, "color-strings-i3", TOOL, 2,
                                        backend=backend)
    assert proof["output_sha256"] == result["output_sha256"]
    assert head_graph(backend) == published and latest(backend) == 2
    manifest = store.load_manifest(backend, TENANT, "solar")
    assert manifest["head"] == manifest["latest"] == 2
    before = read(backend, 1, "color-strings-i2")
    assert before["output"]["lines"] == [LINE.format(173, 173, 0)]
    assert set(colours(before["output"])) == {7}
    assert before["output_sha256"] == "e24ae1fa159867da37dbc9c764d256aa065f74b2671a11f86fbce0d9cccaa410"
    assert latest(backend) == 2


# --- colours on authored graphs -----------------------------------------------------------------------------------

CASES = [
    # (id, build, colours, line counts (recoloured, total, inverters), output sha, output bytes)
    ("w1-fixture", lambda g: validate_graph(copy.deepcopy(g)), [1, 1], (2, 2, 1),
     "a76da4e958ed95fef9b93d3c7b64b6ad5185251f6e2e9dd3104f612351a1ecf6", 384),
    ("unassigned", lambda g: assign.variant(g, "U"), [7, 7], (2, 2, 0),
     "263a2cd803ad0ae5266021bebe7cd186d46fbe62693657638c53de6f605df87c", 384),
    ("partial", lambda g: assign.variant(g, "P"), [1, 7], (2, 2, 1),
     "7e0f4e3b6fd3847e7864c2205c5abfe0e4e92ff6316f799d4f696417b3fb3d09", 384),
    ("l2-before", lambda g: assign.variant(g, "L2"), [7] * 6, (6, 6, 0),
     "dcd7e7e91758c5064d79330b99a1d7d91d6236216f4fa10a8c046403c28f2c28", 692),
    ("l2-assigned", l2_assigned, [1, 1, 1, 1, 2, 2], (6, 6, 2),
     "71a898ac23d7d8f9eedef0ab16220dcd00aced1de13665370ed66b5480d5e732", 692),
    ("combiners", lambda g: assigned(g, "CB"), [1, 1, 1, 2, 2, 2], (6, 6, 2),
     "5ef7ff8d9da57fb69606168847b8e94c792cf95e8b55cb56387589a6d5806e61", 692),
    ("l1-l2", topo.topology_of, [1, 1], (2, 2, 1),
     "a76da4e958ed95fef9b93d3c7b64b6ad5185251f6e2e9dd3104f612351a1ecf6", 384),
    ("renumbered", renumbered, [1, 1, 1, 1, 4, 4], (6, 6, 2),
     "b0559d5c074f003d4a7a7611bd39f6aef732facc69f581f61053d11002f5bc01", 692),
    ("type-after-slash", tagged("+1/B1a"), [7, 1, 1, 1, 2, 2], (6, 6, 2),
     "e63223454967c141e477aa5c3d46546aa20cac4e38caa4d20b9b8832514042d7", 692),
    ("type-before-slash", tagged("B1/1a"), [1, 1, 1, 1, 2, 2], (6, 6, 2),
     "71a898ac23d7d8f9eedef0ab16220dcd00aced1de13665370ed66b5480d5e732", 692),
    ("routeless", routeless, [None, 1, 1, 1, 2, 2], (5, 6, 2),
     "13d351e89e10b50377266f80876cb7849a3ac11bf8f3a24a7cb6ca2fa9df9893", 695),
    ("type-key-b", type_key_b, [1, 1, 1, 1, 2, 2], (6, 6, 2),
     "71a898ac23d7d8f9eedef0ab16220dcd00aced1de13665370ed66b5480d5e732", 692),
    ("units-doubled", units_doubled, [1, 1, 1, 1, 2, 2], (6, 6, 2),
     "71a898ac23d7d8f9eedef0ab16220dcd00aced1de13665370ed66b5480d5e732", 692),
    ("no-strings", topo.bare, [], None,
     "51ed022fa9842ef2ed916983912ab1d00caa397ae19855f177515f7e8cfb4105", 182),
]


@pytest.mark.parametrize("name,build,expected,counts,output_sha,size", CASES, ids=[row[0] for row in CASES])
def test_color_strings_colours(graph, name, build, expected, counts, output_sha, size):
    g = build(graph)
    before = copy.deepcopy(g)
    output = builtin().run(g, {})
    assert g == before
    assert colours(output) == expected
    assert [row["string_ref"] for row in output["strings"]] == [s["id"] for s in g["strings"]]
    if counts is None:
        assert output["lines"] == ["LEAFCOLORSTRINGS: no strings found on layer String."]
        assert output["counts"] == {"strings": 0, "recoloured": 0}
    else:
        assert output["lines"] == [LINE.format(*counts)]
        assert output["counts"] == {"strings": counts[1], "recoloured": counts[0]}
    assert set(output) == {"schema", "colour_family", "lines", "strings", "counts"}
    assert (sha(output), len(canonical_bytes(output))) == (output_sha, size)
    assert solar_local_graph.stable_numbers(output)
    assert builtin().run(g, {}) == output and g == before


def test_color_strings_typed_family_is_measured_not_projected(graph):
    g = l2_assigned(graph)
    state, _ = bridge.state_from_graph(g)
    assert "InverterTypeAssignments" not in state["setting"] and "InverterTypes" not in state["setting"]
    kernel = builtin().kernel
    measured = []
    for assignments in ({"1": "B", "2": "C"}, {"2": "B"}):
        typed = copy.deepcopy(state)
        typed["setting"]["InverterTypeAssignments"] = assignments
        typed["setting"]["InverterTypes"] = {key: {"TypeKey": key} for key in sorted(set(assignments.values()))}
        after, lines = kernel.color_strings(typed, {})
        rows = {row["string"]: row["colour"] for row in after["rows"]["string-assignment"]}
        measured.append([rows[handle] for handle in sorted(rows, key=lambda h: int(h, 16))])
        assert lines == [LINE.format(6, 6, 2)]
    assert measured == [[5, 5, 5, 5, 30, 30], [1, 1, 1, 1, 4, 4]]
    output = builtin().run(type_key_b(graph), {})
    assert colours(output) == [1, 1, 1, 1, 2, 2] and output["colour_family"] == "untyped"


# --- refusals -----------------------------------------------------------------------------------------------------

REFUSALS = [
    ("duplicate-number", duplicate_number, "BRIDGE_DUPLICATE_DEVICE"),
    ("number-zero", number_zero, "BRIDGE_DEVICE_UNNUMBERED"),
    ("levels-ambiguous", levels_ambiguous, "BRIDGE_LEVELS_AMBIGUOUS"),
    ("schema-invalid", schema_invalid, "INVALID_GRAPH_SCHEMA"),
]


@pytest.mark.parametrize("name,build,code", REFUSALS, ids=[row[0] for row in REFUSALS])
def test_color_strings_graph_refusals(graph, name, build, code):
    g = build(graph)
    before = copy.deepcopy(g)
    with pytest.raises(GraphValidationError) as error:
        builtin().run(g, {})
    assert error.value.code == code
    assert g == before


def test_color_strings_kernel_refusals_are_named(graph, monkeypatch):
    module = builtin()
    g = l2_assigned(graph)
    real = bridge.state_from_graph

    def overlay(value):
        state, binding = real(value)
        state["geometry"]["strings"][0]["panels"] = ["LEAFPANEL:1A2B:0"]
        return state, binding

    with monkeypatch.context() as patch:
        patch.setattr(module.bridge, "state_from_graph", overlay)
        with pytest.raises(GraphValidationError) as error:
            module.run(g, {})
        assert error.value.code == "COLOR_STRINGS_NOT_PORTED"
    kernel = module.kernel
    real_kernel = kernel.color_strings

    def refuse(state, host):
        raise kernel.InverterStringError("refused")

    def drop_row(state, host):
        after, lines = real_kernel(state, host)
        after["rows"]["string-assignment"].pop()
        return after, lines

    def colour(value):
        def patched(state, host):
            after, lines = real_kernel(state, host)
            after["rows"]["string-assignment"][0]["colour"] = value
            return after, lines
        return patched

    for patched in (refuse, drop_row, colour(300), colour(True), colour("7")):
        with monkeypatch.context() as patch:
            patch.setattr(kernel, "color_strings", patched)
            with pytest.raises(GraphValidationError) as error:
                module.run(g, {})
            assert error.value.code == "COLOR_STRINGS_MAPPING_FAILED"


def test_color_strings_duplicate_kernel_row_is_refused(graph, monkeypatch):
    module = builtin()
    g = l2_assigned(graph)
    before = copy.deepcopy(g)
    real_kernel = module.kernel.color_strings

    def duplicate_row(state, host):
        after, lines = real_kernel(state, host)
        rows = after["rows"]["string-assignment"]
        rows.append(copy.deepcopy(rows[0]))
        return after, lines

    monkeypatch.setattr(module.kernel, "color_strings", duplicate_row)
    with pytest.raises(GraphValidationError) as error:
        module.run(g, {})
    assert error.value.code == "COLOR_STRINGS_MAPPING_FAILED"
    assert g == before


@pytest.mark.parametrize("params", [None, [], 7, "", {"x": 1}, {"drawing_id": "solar"},
                                    {"operation": "color-strings"}, {"expected_rev": 0}])
def test_color_strings_request_shape_fails_closed(graph, params):
    g = l2_assigned(graph)
    before = copy.deepcopy(g)
    with pytest.raises(GraphValidationError) as error:
        builtin().run(g, copy.deepcopy(params))
    assert error.value.code == "INVALID_COLOR_STRINGS_REQUEST"
    assert g == before


# --- registry, catalog, readiness ---------------------------------------------------------------------------------

DECLARATION = {
    "schema": "leaf.solar-tool.v1", "name": TOOL, "builtin": "builtins/solar_color_strings.py",
    "family": "stringing", "adapter": "local-graph-read", "entitlement": "run_read",
    "requires_persisted_graph": True, "seedable": False,
    "invalid_request_code": "INVALID_COLOR_STRINGS_REQUEST", "readiness": {"kind": "facets", "facets": ["strings"]},
    "engine": "server-builtin", "interaction": {"mode": "form"}, "record_store": "registry",
    "record": {
        "name": TOOL, "version": "1.0.0", "kind": "script", "family_id": "stringing",
        "engine_op": "solar_color_strings", "entry": "builtins/solar_color_strings.py",
        "params": {"type": "object", "properties": {"drawing_id": {"type": "string", "maxLength": 128}},
                   "required": [], "additionalProperties": False},
        "returns": {"type": "object"}, "capabilities": ["drawing.read"], "allow_local_fallback": False,
    },
    "ledger": ["color-strings"], "trusted_inputs": [], "maturity": "preview", "wave": 2, "order": 83,
    "scenario": "w2-rooftop",
}


def test_color_strings_registry_and_catalog(monkeypatch):
    declaration = solar_tools.get(TOOL)
    actual = copy.deepcopy(declaration)
    description = actual["record"].pop("description")
    assert type(description) is str and description.strip() and len(description) <= 1024
    assert actual == DECLARATION
    assert TOOL in solar_tools.local_graph_read_tools() and TOOL not in solar_tools.local_graph_tools()
    assert availability.SOLAR_CAPABILITIES[TOOL]["adapter"] == "local-graph-read"
    assert TOOL not in availability.W1_CAPABILITIES and len(availability.W1_CAPABILITIES) == 9
    assert entitlements.tool_required_capability(solar_tools.trusted_record(TOOL)) == "run_read"
    monkeypatch.setattr(deps, "tenant_repo_dir", lambda tenant: None)
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    families = catalog.build_catalog(deps.all_tools(TENANT))
    found = [(family, row) for family in families for row in family["capabilities"] if row["name"] == TOOL]
    assert len(found) == 1
    family, row = found[0]
    assert family["family_id"] == "stringing"
    assert row["solar"] == solar_tools.catalog_view(declaration["record"])
    assert row["params_schema"] == declaration["record"]["params"]


def test_color_strings_params_schema():
    validator = Draft7Validator(solar_tools.trusted_record(TOOL)["params"])
    for params in ({}, {"drawing_id": "solar"}, {"drawing_id": "d" * 128}):
        assert validator.is_valid(params), params
    for params in ({"drawing_id": "d" * 129}, {"drawing_id": 7}, {"x": 1}, {"operation": "color-strings"},
                   {"expected_rev": 0}):
        assert not validator.is_valid(params), params


def test_color_strings_readiness(graph):
    assert availability.w1_graph_readiness(l2_assigned(graph))[TOOL] == {"input_ready": True, "input_reason": None}
    inputs = availability.w1_local_commit_inputs
    assert inputs(assign.variant(graph, "U"))[TOOL] == {"input_ready": True, "input_reason": None}
    assert inputs(topo.bare(graph))[TOOL] == {"input_ready": False, "input_reason": "strings_required"}
    assert inputs(units_doubled(graph))[TOOL] == {"input_ready": False, "input_reason": "unresolved_units"}


# --- the read rail ------------------------------------------------------------------------------------------------


class InlineExecutor:
    def submit(self, fn, *args, **kwargs):
        fn(*args, **kwargs)


def api_for(tmp_path, monkeypatch, value):
    """The shared W1 rail (rail._api) plus this read tool's record and its job lane, run inline."""
    backend, _ = seed(tmp_path, monkeypatch, value)
    record = solar_tools.trusted_record(TOOL)
    for api in rail._api(backend, tmp_path, monkeypatch):
        api[2][TOOL] = record
        monkeypatch.setitem(jobs._executors, jobs.lane_for(record, False), InlineExecutor())
        yield api


@pytest.fixture
def l2_api(isolated_jobs, no_network, graph, tmp_path, monkeypatch):  # noqa: F811
    yield from api_for(tmp_path, monkeypatch, l2_assigned(graph))


@pytest.fixture
def bare_api(isolated_jobs, no_network, graph, tmp_path, monkeypatch):  # noqa: F811
    yield from api_for(tmp_path, monkeypatch, topo.bare(graph))


@pytest.fixture
def duplicate_api(isolated_jobs, no_network, graph, tmp_path, monkeypatch):  # noqa: F811
    yield from api_for(tmp_path, monkeypatch, duplicate_number(graph))


def post(api, params):
    return api[0].post("/api/run?wait=1", json=rail.body(api, TOOL, params))


def job_records():
    return [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]


def test_color_strings_run_rail_reads_without_a_version(l2_api, graph):
    seeded = head_graph(l2_api[1])
    response = post(l2_api, {})
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True and env["execution_provenance"]["execution_mode"] == "local_graph_read"
    result = env["result"]
    assert result["output"] == builtin().run(l2_assigned(graph), {})
    assert colours(result["output"]) == [1, 1, 1, 1, 2, 2]
    assert result["drawing_changed"] is False and result["source_version"] == 1
    assert result["output_sha256"] == "71a898ac23d7d8f9eedef0ab16220dcd00aced1de13665370ed66b5480d5e732"
    records = job_records()
    assert len(records) == 1 and records[0]["status"] == "complete"
    assert local.graph_read_provenance(result, records[0]["params"], TENANT, records[0]["job_id"], TOOL, 1,
                                       backend=l2_api[1])["output_sha256"] == result["output_sha256"]
    manifest = store.load_manifest(l2_api[1], TENANT, "solar")
    assert manifest["head"] == manifest["latest"] == 1
    assert head_graph(l2_api[1]) == seeded


@pytest.mark.parametrize("params", [{"x": 1}, {"operation": "color-strings"}, {"expected_rev": 0}])
def test_color_strings_broker_refuses_schema_violations(l2_api, params):
    response = post(l2_api, params)
    env = response.json()
    assert env.get("ok") is not True, response.text
    records = job_records()
    assert all(record["status"] != "complete" for record in records)
    reasons = [env.get("reason_code"), (env.get("error") or {}).get("reason_code")]
    reasons += [(record.get("error") or {}).get("reason_code") for record in records]
    assert "tool_params_invalid" in reasons, response.text
    manifest = store.load_manifest(l2_api[1], TENANT, "solar")
    assert manifest["head"] == manifest["latest"] == 1


def test_color_strings_readiness_refusal_reaches_the_rail(bare_api):
    response = post(bare_api, {})
    assert response.status_code == 409, response.text
    env = response.json()
    assert env.get("ok") is not True
    assert env["reason_code"] == env["error"]["message"] == "strings_required"
    assert env["availability"]["input_ready"] is False and env["availability"]["runnable"] is False
    assert not job_records()


def test_color_strings_builtin_refusal_reaches_the_rail(duplicate_api):
    response = post(duplicate_api, {})
    assert response.status_code == 400, response.text
    env = response.json()
    assert env.get("ok") is not True
    assert env["error"]["message"] == env["reason_code"] == "BRIDGE_DUPLICATE_DEVICE"
    records = job_records()
    assert len(records) == 1 and records[0]["status"] == "failed"
    manifest = store.load_manifest(duplicate_api[1], TENANT, "solar")
    assert manifest["head"] == manifest["latest"] == 1
