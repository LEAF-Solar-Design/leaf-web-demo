"""Electrical schedules (InsertSchedules, i8) over the W1 design graph through Studio's registry, read adapter,
artifact store and broker: the recorded i7 design rebuilt as a graph reproduces the i8 receipt's three tables cell
for cell, the default topology reading follows the graph's homerun legs, and every refusal fails closed."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

import broker_client
import catalog
import deps
import entitlements
import jobs
import product_capability_availability as availability
import solar_artifacts
import solar_electrical_state_bridge as br
import solar_local_read as local
import solar_tools
import store
import write_loop
from solar_design_graph import GraphValidationError, validate_graph
from solar_solve_results import require_current_export
from test_solar_electrical_state_bridge import CREATED, DEFAULTS, minter
from test_solar_ground_topology import bare
from test_solar_tool_guardrails_read import located
from test_w1_design_graph import app_id, entity, graph  # noqa: F401
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
from test_w1_sizing_groups import confirm, passing, service, sizing_params  # noqa: F401
from test_w1_solve_commit import seed

ROOT = Path(__file__).resolve().parents[2]
TENANT = "fixture-tenant"
TOOL = "solar-electrical-schedules"
MPU = 0.0254
STATES = ROOT / "docs" / "parity" / "evidence" / "rooftop" / "inverters"
RECEIPT = ROOT / "docs" / "parity" / "receipts" / "insert-schedules" / "rooftop-inverters-i8.json"
TITLES = ["EQUIPMENT SCHEDULE", "COMBINER / INVERTER SCHEDULE", "STRING SCHEDULE"]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ev = _load("solar_inverter_outputs_evidence_schedules", ROOT / "scripts" / "solar_inverter_outputs_evidence.py")
st = br.st
CAPTURE_RECORD = copy.deepcopy(ev.host_for("i8")["InverterCatalogRecord"])


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False,
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def builtin():
    return local._load_builtin(TOOL)


def receipt_cells(side):
    document = json.loads(RECEIPT.read_text(encoding="utf-8"))
    return {row["cells"][0][0]: row["cells"] for row in document["comparison"][side]["after"]["rows"]}


# ------------------------------------------------------------------ the i7 design as a graph --

def i7_state():
    return st.load_state(STATES / "state-i7.json")


def build_i7(w1, recorded):
    """The committed i7 state (the i8 step's input) as a sized W1 graph: one frameless panel per module,
    one string per state string (tag, module count and route from the state), the bridge's adopted
    equipment, and one W1 homerun route per dc-homerun leg."""
    state = i7_state()
    g = located(bare(w1))
    rows = sorted(state["rows"]["string-assignment"], key=lambda row: row["string"])
    geometry = {item["string"]: item for item in state["geometry"]["strings"]}
    count = 0
    for k, row in enumerate(rows, 1):
        refs = []
        for _ in range(int(row["_detail"]["panel_count"])):
            count += 1
            panel = entity("panel", count, frame_ref=None, matrix_cell=None, centre=[float(count), 0.0], angle=0,
                           assignment={"string_ref": app_id("string", k), "seq": len(refs)})
            panel["provenance"]["source_handle"] = format(count, "X")
            g["panels"].append(panel)
            refs.append(panel["id"])
        points = geometry[row["string"]]["vertices"]
        string = entity("string", k, circuit_tag=row["_detail"]["circuit"], circuit_kind="String",
                        ordered_panel_refs=refs, module_count=len(refs), from_ref=refs[0], to_ref=refs[-1],
                        tag_text_ref=None, wire_gauge="", length_ft=0,
                        route=[[x * MPU, y * MPU] for x, y in points], inverter_ref=None)
        string["provenance"]["source_handle"] = row["string"]
        g["strings"].append(string)
    g = confirm(validate_graph(g), sizing_params(g, recorded))["graph"]
    binding = br.adopt_state(g, state)
    g, _ = br.graph_from_state(g, state, binding, defaults=DEFAULTS, new_id=minter(1000), created_at=CREATED)
    by_handle = {s["provenance"]["source_handle"]: s for s in g["strings"]}
    legs = [c for c in state["rows"]["cable"] if c["cable_kind"] == "dc-homerun"]
    for n, cable in enumerate(legs, 1):
        string = by_handle[cable["from"]]
        points = [st.point_of(v) for v in cable["vertices"]]
        g["routes"].append(entity("route", n, route_kind=cable["segment"] + " homerun",
                                  points=[[x * MPU, y * MPU] for x, y in points], from_ref=string["id"],
                                  to_ref=string["inverter_ref"], wire_gauge=cable["_detail"]["gauge"],
                                  length_ft=cable["length"]["value"], point_units="m", length_units="ft"))
    return validate_graph(g)


_I7 = {}


@pytest.fixture
def i7(graph, passing, service):
    if "graph" not in _I7:
        _I7["graph"] = build_i7(copy.deepcopy(graph), passing)
    return copy.deepcopy(_I7["graph"])


I7_SHA = "0a030c62567ab225705f6c003dc1058936500d3b45b8efca16103d67fc9acf31"


def variant(name, graph, i7=None):
    g = copy.deepcopy(i7 if name.startswith("i7") else graph)
    if name == "stale":
        g["strings"][0]["validity"] = {"state": "stale", "reasons": ["upstream_corrected"]}
    elif name == "solaredge":
        g["inverters"][0]["is_solaredge"] = True
    elif name == "units":
        g["project"]["units"]["meters_per_unit"] *= 2
    elif name == "unverified":
        g["settings"]["extra"]["string_sizing"] = {"mode": "global"}
    elif name == "zones":
        g["settings"]["extra"]["string_sizing"] = {"mode": "zones"}
    return g


def artifact(out):
    assert type(out) is solar_artifacts.ArtifactOutput
    assert (out.media_type, out.filename) == ("application/json", "ElectricalSchedules.json")
    document = json.loads(out.content.decode("utf-8"))
    assert out.content == builtin().render(document)
    return document


# -------------------------------------------------------------------------- the tests --

def test_electrical_schedules_kernel_replays_i8():
    kernel = builtin().kernel
    tables = kernel.build_schedules(i7_state(), ev.host_for("i8"))
    cells = [kernel.table_cells(table) for table in tables]
    assert [grid[0][0] for grid in cells] == TITLES
    for side in ("plugin", "studio"):
        recorded = receipt_cells(side)
        assert [recorded[grid[0][0]] for grid in cells] == cells
    assert cells[2][-1][:5] == ["TOTAL", "", "173", "", "2345"]
    assert cells[1][-1] == ["GRAND TOTAL", "", "173", "", "2345", "-", "-", "-"]


def test_electrical_schedules_i7_graph(i7):
    assert (len(i7["panels"]), len(i7["strings"]), len(i7["inverters"]), len(i7["routes"])) == (2345, 173, 22, 346)
    assert (sha(i7), i7["rev"], i7["parent_rev"]) == (I7_SHA, 1, 0)
    assert require_current_export(i7) == i7
    assert builtin().label_mismatches(i7) == 162


SUMMARY = {"circuit_source": "topology", "strings": 173, "modules": 2345, "label_mismatches": 162,
           "inverter_record": "absent", "sizing": "global", "module_catalog": "unresolved"}
W1_SUMMARY = dict(SUMMARY, strings=2, modules=3, label_mismatches=0, sizing="absent")
OUTPUTS = [
    ("i7", {"circuit_source": "labels", "inverter_record": CAPTURE_RECORD}, TITLES, 19319,
     "282be20f129fc695a52a0e865b0a4a32b9448ca6f34ccd88d80a91f1cc01fb83",
     dict(SUMMARY, circuit_source="labels", inverter_record="caller")),
    ("i7", {"inverter_record": CAPTURE_RECORD}, TITLES, 19953,
     "253e1fc5a476d5e0bfc5bc83de549301e52a9b7295992c0836ebffe6ff66211e", dict(SUMMARY, inverter_record="caller")),
    ("i7", {}, TITLES[1:], 19514, "07dce5dc7289d872acd257226d88842f696b4e5c3b86e8f8c3eca51a4af62ac0", SUMMARY),
    ("i7", {"circuit_source": "topology"}, TITLES[1:], 19514,
     "07dce5dc7289d872acd257226d88842f696b4e5c3b86e8f8c3eca51a4af62ac0", SUMMARY),
    ("i7", {"circuit_source": "labels"}, TITLES[1:], 18880,
     "fc30dd7fbefb06f67c87b2a8682664ebecec67a0dae1e5bce502dd45ba24349b", dict(SUMMARY, circuit_source="labels")),
    ("w1", {}, ["INVERTER SCHEDULE", "STRING SCHEDULE"], 902,
     "70cd4354fbe564f57866f042698e8dc1e04ed29e6dd628d4e02a842675e2e64e", W1_SUMMARY),
    ("w1", {"inverter_record": CAPTURE_RECORD}, ["EQUIPMENT SCHEDULE", "INVERTER SCHEDULE", "STRING SCHEDULE"], 1341,
     "8a09b64b4737ca63e47f3db19b94cac48c3e80740e6d2c5bf353250b5558b9ba", dict(W1_SUMMARY, inverter_record="caller")),
    ("w1", {"circuit_source": "labels", "inverter_record": CAPTURE_RECORD}, ["EQUIPMENT SCHEDULE"], 523,
     "dc20c73b71c7930d4e2d278724c38bcef2f5b8abba594214a776b14c4b4d0e9f",
     dict(W1_SUMMARY, circuit_source="labels", strings=0, modules=0, inverter_record="caller")),
]


def subject(request, name, graph):
    return request.getfixturevalue("i7") if name == "i7" else copy.deepcopy(graph)


def test_electrical_schedules_labels_reproduce_the_receipt(i7):
    out = builtin().run(i7, {"circuit_source": "labels", "inverter_record": CAPTURE_RECORD})
    document = artifact(out)
    assert set(document) == {"schema", "circuit_source", "tables"}
    assert (document["schema"], document["circuit_source"]) == ("leaf.solar-electrical-schedules.v1", "labels")
    assert [table["title"] for table in document["tables"]] == TITLES
    assert all(set(table) == {"title", "cells"} for table in document["tables"])
    for side in ("plugin", "studio"):
        recorded = receipt_cells(side)
        assert [table["cells"] for table in document["tables"]] == [recorded[title] for title in TITLES]


@pytest.mark.parametrize("name,params,titles,size,content_sha,summary", OUTPUTS,
                         ids=[f"{row[0]}-{n}" for n, row in enumerate(OUTPUTS)])
def test_electrical_schedules_outputs(request, graph, name, params, titles, size, content_sha, summary):
    g = subject(request, name, graph)
    before, asked = copy.deepcopy(g), copy.deepcopy(params)
    out = builtin().run(g, params)
    document = artifact(out)
    assert [table["title"] for table in document["tables"]] == titles
    assert (len(out.content), hashlib.sha256(out.content).hexdigest()) == (size, content_sha)
    assert out.summary == {"status": "written", "tables": titles, **summary}
    assert g == before and params == asked
    again = builtin().run(g, params)
    assert (again.content, again.summary) == (out.content, out.summary)


COMBINER_ROWS = [
    ["INV-1", "CB-1", "10", "140"], ["INV-1", "CB-2", "10", "138"], ["INV-1 Total", "", "20", "278"],
    ["INV-2", "CB-3", "17", "236"], ["INV-2", "CB-4", "19", "254"], ["INV-2 Total", "", "36", "490"],
    ["INV-3", "CB-5", "10", "132"], ["INV-3", "CB-6", "11", "144"], ["INV-3 Total", "", "21", "276"],
    ["INV-4", "CB-7", "10", "136"], ["INV-4 Total", "", "10", "136"],
    ["INV-5", "CB-8", "10", "138"], ["INV-5", "CB-9", "10", "136"], ["INV-5 Total", "", "20", "274"],
    ["INV-6", "CB-10", "19", "255"], ["INV-6", "CB-11", "15", "202"], ["INV-6 Total", "", "34", "457"],
    ["INV-7", "CB-12", "10", "137"], ["INV-7 Total", "", "10", "137"],
    ["INV-8", "CB-13", "10", "138"], ["INV-8", "CB-14", "12", "159"], ["INV-8 Total", "", "22", "297"],
    ["GRAND TOTAL", "", "173", "2345"],
]


def test_electrical_schedules_topology_follows_the_legs(i7):
    document = artifact(builtin().run(i7, {"inverter_record": CAPTURE_RECORD}))
    equipment, combiner, strings = (table["cells"] for table in document["tables"])
    assert equipment == receipt_cells("plugin")["EQUIPMENT SCHEDULE"]
    rows = combiner[2:]
    assert [[row[0], row[1], row[2], row[4]] for row in rows] == COMBINER_ROWS
    assert rows[-1] == ["GRAND TOTAL", "", "173", "", "2345", "-", "-", "-"]
    assert all(row[5:] == ["-", "0", "-"] for row in rows if row[0].endswith(" Total"))
    assert len(strings) == 176 and strings[-1][:5] == ["TOTAL", "", "173", "", "2345"]
    assert combiner != receipt_cells("plugin")["COMBINER / INVERTER SCHEDULE"]


W1_INVERTER = [
    ["INVERTER SCHEDULE", "", "", "", "", "", "", ""],
    ["Inverter", "MPPT", "Strings", "Mod/String", "Total Modules", "DC Power (kW)", "AC Power (kW)", "DC/AC Ratio"],
    ["INV-1", "A", "2", "2, 1", "3", "-", "-", "-"],
    ["INV-1 Total", "", "2", "", "3", "-", "-", "-"],
    ["GRAND TOTAL", "", "2", "", "3", "-", "-", "-"],
]
W1_STRING_ROWS = [
    ["1", "A", "1", "-", "2", "-", "-", "-", "-", "-", "-", "-", "-", "19.7", "-", "-", "-", "-"],
    ["1", "A", "2", "-", "1", "-", "-", "-", "-", "-", "-", "-", "-", "3.3", "-", "-", "-", "-"],
    ["TOTAL", "", "2", "", "3", "", "", "", "", "", "", "", "-", "", "", "", "", ""],
]


def test_electrical_schedules_w1_tables(graph):
    inverter, strings = (table["cells"] for table in artifact(builtin().run(graph, {}))["tables"])
    assert inverter == W1_INVERTER
    assert strings[1][:2] == ["Inv", "MPPT"] and strings[2:] == W1_STRING_ROWS


@pytest.mark.parametrize("unit,mpu", [("in", 0.0254), ("m", 1.0), ("ft", 0.3048), ("mm", 0.001)],
                         ids=["inches", "metres", "feet", "millimetres"])
def test_electrical_schedules_lengths_use_inches(graph, unit, mpu):
    module = builtin()
    baseline = module.schedule_state(graph, "topology")
    expected_lengths = [module.kernel.polyline_length(item["vertices"])
                        for item in baseline["geometry"]["strings"]]
    g = copy.deepcopy(graph)
    g["project"]["units"].update(drawing_units=unit, meters_per_unit=mpu,
                                 drawing_unit_is_feet=(unit == "ft"))
    before = copy.deepcopy(g)
    state = module.schedule_state(g, "topology")
    lengths = [module.kernel.polyline_length(item["vertices"]) for item in state["geometry"]["strings"]]
    assert lengths == pytest.approx(expected_lengths)
    assert lengths == pytest.approx([1 / MPU, 1 / MPU])
    strings = artifact(module.run(g, {}))["tables"][1]["cells"]
    assert strings[2][13] == "19.7"
    assert strings[2:] == W1_STRING_ROWS
    assert g == before


def test_electrical_schedules_sized_w1(graph, passing, service):
    g = located(copy.deepcopy(graph))
    g = confirm(g, sizing_params(g, passing))["graph"]
    assert sha(g) == "5127f9adc6ec07baa94ac19206973a2abc87746578b3519ecca8c56b606bb502"
    out = builtin().run(g, {})
    assert out.summary == {"status": "written", "tables": ["INVERTER SCHEDULE", "STRING SCHEDULE"],
                           **dict(W1_SUMMARY, sizing="global")}
    assert (len(out.content), hashlib.sha256(out.content).hexdigest()) == (
        941, "c4b5a3d0bc1addd8938c0de423a2dd4238328f30f99c845c777e7c1ba5f521c6")
    assert artifact(out)["tables"][1]["cells"][2:] == [
        ["1", "A", "1", "-", "2", "-", "102.5", "1500", "1397.5", "-", "-", "-", "-", "19.7", "-", "-", "-", "P99.5 Voc"],
        ["1", "A", "2", "-", "1", "-", "51.3", "1500", "1448.7", "-", "-", "-", "-", "3.3", "-", "-", "-", "P99.5 Voc"],
        W1_STRING_ROWS[2]]


def test_electrical_schedules_no_schedules(graph):
    before = copy.deepcopy(graph)
    out = builtin().run(graph, {"circuit_source": "labels"})
    assert out == {"status": "no-schedules", "reason": "no-schedule-data",
                   **dict(W1_SUMMARY, circuit_source="labels", strings=0, modules=0)}
    assert sha(out) == "49543f92faabfceac835c65073d7393977d652a4ec624609a4ec6834033b492c"
    assert graph == before


def record(**changes):
    value = copy.deepcopy(CAPTURE_RECORD)
    value.update(changes)
    return value


BAD_REQUESTS = [
    None, [], "topology", {"x": 1}, {"drawing_id": "solar"}, {"circuit_source": "graph"},
    {"circuit_source": 1}, {"circuit_source": None}, {"inverter_record": None}, {"inverter_record": {}},
    {"inverter_record": {k: v for k, v in CAPTURE_RECORD.items() if k != "maxACCurrent"}},
    {"inverter_record": record(extra="1")}, {"inverter_record": record(companyName="x" * 129)},
    {"inverter_record": record(companyName="Sun\ngrow")}, {"inverter_record": record(modelName=5)},
    {"inverter_record": record(maxDCPower="1e3")}, {"inverter_record": record(maxDCPower="12.1234567")},
    {"inverter_record": record(maxDCPower="1,500")}, {"inverter_record": record(maxDCPower=375)},
    {"inverter_record": record(maxDCPower="1234567890123")}, {"inverter_record": record(maxDCPower=" 375")},
]


@pytest.mark.parametrize("params", BAD_REQUESTS, ids=[f"bad-{n}" for n in range(len(BAD_REQUESTS))])
def test_electrical_schedules_request_shape_fails_closed(graph, params):
    before, asked = copy.deepcopy(graph), copy.deepcopy(params)
    with pytest.raises(GraphValidationError) as error:
        builtin().run(graph, params)
    assert error.value.code == "INVALID_SCHEDULES_REQUEST"
    assert graph == before and params == asked


def test_electrical_schedules_accepts_boundary_records(graph):
    for changes in ({"companyName": "x" * 128, "seriesName": ""}, {"maxDCPower": ""},
                    {"maxDCPower": "-123456789012.123456"}, {"maxACCurrent": "0"}):
        assert artifact(builtin().run(graph, {"inverter_record": record(**changes)}))["tables"]


REFUSALS = [
    ("units", "UNRESOLVED_UNITS"),
    ("stale", "SOLAR_OUTPUT_NOT_CURRENT"),
    ("solaredge", "SCHEDULES_OPTIMIZERS_UNSUPPORTED"),
    ("unverified", "SIZING_CONFIRMATION_REQUIRED"),
]


@pytest.mark.parametrize("name,code", REFUSALS, ids=[row[0] for row in REFUSALS])
def test_electrical_schedules_graph_refusals(graph, name, code):
    g = variant(name, graph)
    before = copy.deepcopy(g)
    with pytest.raises(GraphValidationError) as error:
        builtin().run(g, {})
    assert error.value.code == code and g == before


def test_electrical_schedules_zone_sizing_is_refused(graph, monkeypatch):
    module = builtin()
    monkeypatch.setattr(module.solar_sizing_client, "require_sizing", lambda value: value)
    with pytest.raises(GraphValidationError) as error:
        module.run(variant("zones", graph), {})
    assert error.value.code == "SCHEDULES_ZONE_SIZING_UNSUPPORTED"


def test_electrical_schedules_named_failures(graph, monkeypatch):
    module = builtin()

    def bridge_refuses(value):
        raise br.ElectricalBridgeError("BRIDGE_LEVELS_AMBIGUOUS")

    def kernel_refuses(*args):
        raise module.kernel.InverterOutputError("refused")

    real = module.st.validate_state

    def state_refuses(value):
        if value["rows"]["cable"]:
            raise module.st.InverterStateError("refused")
        return real(value)

    for target, name, patch, code in ((module.bridge, "state_from_graph", bridge_refuses, "BRIDGE_LEVELS_AMBIGUOUS"),
                                      (module.kernel, "build_schedules", kernel_refuses, "SCHEDULES_KERNEL_REFUSED"),
                                      (module.st, "validate_state", state_refuses, "SCHEDULES_MAPPING_FAILED")):
        with monkeypatch.context() as patched:
            patched.setattr(target, name, patch)
            with pytest.raises(GraphValidationError) as error:
                module.run(copy.deepcopy(graph), {})
            assert error.value.code == code


DECLARATION_PARAMS = json.loads((ROOT / "server" / "solar_tools" / "solar_electrical_schedules.json")
                                .read_text(encoding="utf-8"))["record"]["params"]


def test_electrical_schedules_registry_and_catalog(monkeypatch):
    declaration = solar_tools.get(TOOL)
    actual = copy.deepcopy(declaration)
    description = actual["record"].pop("description")
    assert type(description) is str and description.strip() and len(description) <= 1024
    assert actual == {
        "schema": "leaf.solar-tool.v1", "name": TOOL, "builtin": "builtins/solar_electrical_schedules.py",
        "family": "schedules", "adapter": "local-graph-read", "entitlement": "run_read",
        "requires_persisted_graph": True, "seedable": False, "invalid_request_code": "INVALID_SCHEDULES_REQUEST",
        "readiness": {"kind": "hook"}, "engine": "server-builtin", "interaction": {"mode": "form"},
        "record_store": "registry",
        "record": {"name": TOOL, "version": "1.0.0", "kind": "script", "family_id": "schedules",
                   "engine_op": "solar_electrical_schedules", "entry": "builtins/solar_electrical_schedules.py",
                   "params": DECLARATION_PARAMS, "returns": {"type": "object"}, "capabilities": ["drawing.read"],
                   "allow_local_fallback": False},
        "ledger": ["insert-schedules"], "trusted_inputs": [], "maturity": "preview", "wave": 2, "order": 110,
        "scenario": "w2-rooftop"}
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
    assert family["family_id"] == "schedules"
    assert row["solar"] == solar_tools.catalog_view(declaration["record"])
    assert row["params_schema"] == declaration["record"]["params"]


def test_electrical_schedules_params_schema():
    params = solar_tools.trusted_record(TOOL)["params"]
    assert set(params["properties"]) == {"drawing_id", "circuit_source", "inverter_record"}
    assert params["properties"]["circuit_source"] == {"type": "string", "enum": ["topology", "labels"]}
    schema = Draft7Validator(params)
    for good in ({}, {"drawing_id": "solar"}, {"circuit_source": "labels"}, {"inverter_record": CAPTURE_RECORD},
                 {"inverter_record": record(maxDCPower="", companyName="x" * 128)}):
        assert schema.is_valid(good)
    for bad in ({"x": 1}, {"drawing_id": "x" * 129}, {"circuit_source": "graph"}, {"inverter_record": {}},
                {"inverter_record": record(extra="1")}, {"inverter_record": record(maxDCPower="1e3")},
                {"inverter_record": record(maxDCPower="1,500")}, {"inverter_record": record(maxDCPower=375)},
                {"inverter_record": record(companyName="x" * 129)}, {"inverter_record": record(maxDCPower="1" * 21)}):
        assert not schema.is_valid(bad)
    for field in ("companyName", "modelName", "seriesName"):
        for length in (0, 128):
            assert schema.is_valid({"inverter_record": record(**{field: "x" * length})}), (field, length)
        assert not schema.is_valid({"inverter_record": record(**{field: "x" * 129})}), field


def test_electrical_schedules_numeric_newline_agreement(graph):
    module = builtin()
    schema = Draft7Validator(solar_tools.trusted_record(TOOL)["params"])
    before = copy.deepcopy(graph)
    for field in module.NUMBER_FIELDS:
        good = {"circuit_source": "labels", "inverter_record": record(**{field: "12"})}
        asked = copy.deepcopy(good)
        assert schema.is_valid(good)
        assert artifact(module.run(graph, good))["tables"]
        assert good == asked
        bad = {"circuit_source": "labels", "inverter_record": record(**{field: "12\n"})}
        asked = copy.deepcopy(bad)
        assert not schema.is_valid(bad)
        with pytest.raises(GraphValidationError) as error:
            module.run(graph, bad)
        assert error.value.code == "INVALID_SCHEDULES_REQUEST"
        assert bad == asked
    assert graph == before


def test_electrical_schedules_readiness(graph):
    assert availability.w1_graph_readiness(graph)[TOOL] == {"input_ready": True, "input_reason": None}
    for name, reason in (("stale", "solar_output_not_current"), ("solaredge", "schedules_optimizers_unsupported"),
                         ("unverified", "sizing_confirmation_required")):
        assert availability.w1_graph_readiness(variant(name, graph))[TOOL] == {
            "input_ready": False, "input_reason": reason}
    assert availability.w1_local_commit_inputs(variant("units", graph))[TOOL] == {
        "input_ready": False, "input_reason": "unresolved_units"}


# ------------------------------------------------------------------------------ the rail --

@pytest.fixture
def api(isolated_jobs, no_network, graph, tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import jobs as route

    backend, _ = seed(tmp_path, monkeypatch, graph)
    monkeypatch.setenv("BROKER_LEDGER", str(tmp_path / "ledger.jsonl"))
    monkeypatch.setenv("BROKER_TENANTS", str(tmp_path / "tenants.json"))
    monkeypatch.setenv("LEAF_BROKER_STORE", "legacy")
    monkeypatch.setenv("LEAF_RUNTIME_ENV", "staging")
    import broker

    monkeypatch.setattr(broker, "LEDGER_PATH", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(broker, "_tenants", {TENANT: {"tier": "demo", "disabled": False}})
    monkeypatch.setattr(broker, "_cap_preflight", lambda *a: None)
    monkeypatch.setattr(broker, "_emit_aps_metric", lambda *a: None)
    monkeypatch.setattr(broker, "_get_da", lambda: pytest.fail("APS must not execute"))
    monkeypatch.setattr(broker, "run_tool_dynamic", lambda *a, **k: pytest.fail("dynamic dispatch"))
    monkeypatch.setattr(deps, "tenant_repo_dir", lambda tenant: None)
    monkeypatch.setattr(deps, "load_tenant_repo_tools", lambda tenant: [])
    monkeypatch.setattr(deps, "_AUTHORED", [])
    monkeypatch.setattr(deps, "AUTHORED_STORE", tmp_path / "absent-authored.json")
    trusted = deps.find_tool(TOOL, TENANT)
    assert trusted == solar_tools.trusted_record(TOOL)

    class InlineExecutor:
        def submit(self, fn, *args, **kwargs):
            fn(*args, **kwargs)

    monkeypatch.setattr(jobs, "_executors", {jobs.lane_for(trusted, False): InlineExecutor()})
    monkeypatch.setattr(route.deps, "backedge_run_identity", lambda tenant, *a: tenant)
    monkeypatch.setattr(route.deps, "auth_live", lambda: False)
    monkeypatch.setattr(jobs.platform_link, "resolve_submission_context", lambda *a: None)
    monkeypatch.setattr(write_loop, "backend_for_tenant", lambda *a, **k: backend)
    monkeypatch.setattr(route.catalog, "live_aps_runtime_authorized", lambda *a, **k: True)
    monkeypatch.delenv("LEAF_ENTITLEMENTS_FILE", raising=False)
    monkeypatch.delenv("LEAF_EXACT_WRITE_PINS_REQUIRED", raising=False)
    requests = []

    def transport(url, *, json, headers, timeout):
        assert url.endswith("/broker/run")
        assert json["aps_live"] is False
        requests.append(copy.deepcopy(json))
        response = broker._broker_run(broker.BrokerRunRequest(**json))

        class Reply:
            status_code = response.status_code

            def json(self):
                return __import__("json").loads(response.body)

        return Reply()

    monkeypatch.setattr(broker_client.requests, "post", transport)
    app = FastAPI()
    app.include_router(route.router)
    tenant = route.deps.TenantContext(
        TENANT, tier="demo", subject="fixture-subject", authority_resolved=True)
    app.dependency_overrides[route.deps.require_tenant] = lambda: tenant
    with TestClient(app) as client:
        yield client, backend, trusted, requests


def body(api, params):
    return {"tool": TOOL, "dwg": "solar", "params": copy.deepcopy(params),
            "catalog_digest": deps.catalog_tool_digest(api[2])}


def keys(backend):
    return set(backend.drawing_object_keys(TENANT, "solar"))


def test_electrical_schedules_api_writes_one_artifact(api, graph):
    before = keys(api[1])
    response = api[0].post("/api/run?wait=1", json=body(api, {"inverter_record": CAPTURE_RECORD}))
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True and env["execution_provenance"]["execution_path"] == "local"
    result = env["result"]
    assert jobs.get_job(result["job_id"])["status"] == "complete"
    output = result["output"]
    assert set(output) == {"summary", "artifact"}
    assert output["summary"] == {"status": "written",
                                 "tables": ["EQUIPMENT SCHEDULE", "INVERTER SCHEDULE", "STRING SCHEDULE"],
                                 **dict(W1_SUMMARY, inverter_record="caller")}
    ref = output["artifact"]
    assert (ref["schema"], ref["media_type"], ref["filename"], ref["byte_length"], ref["content_sha256"],
            ref["source_version"]) == ("leaf.solar-artifact-ref.v1", "application/json", "ElectricalSchedules.json",
                                       1341, "8a09b64b4737ca63e47f3db19b94cac48c3e80740e6d2c5bf353250b5558b9ba", 1)
    assert ref["download"] == "/api/drawings/solar/artifacts/" + ref["artifact_id"]
    assert (result["output_sha256"], result["output_bytes"]) == (
        "6364b7eeb6df490d1857c4ece20b03e14bd2ec7302548f0bc367f53b328939d5", 682)
    meta, content = solar_artifacts.read_artifact(api[1], TENANT, "solar", ref["artifact_id"])
    assert meta["tool"] == TOOL and content == builtin().run(graph, {"inverter_record": CAPTURE_RECORD}).content
    added = keys(api[1]) - before
    assert len(added) == 2
    again = api[0].post("/api/run?wait=1", json=body(api, {"inverter_record": CAPTURE_RECORD})).json()
    assert again["result"]["output"] == output and keys(api[1]) - before == added
    assert len(api[3]) == 2
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 1


API_REFUSALS = [
    ({"circuit_source": "graph"}, "tool_params_invalid"),
    ({"x": 1}, "tool_params_invalid"),
    ({"inverter_record": record(maxDCPower="1,500")}, "tool_params_invalid"),
    ({"inverter_record": record(companyName="Sun\ngrow")}, "INVALID_SCHEDULES_REQUEST"),
]


@pytest.mark.parametrize("params,reason", API_REFUSALS, ids=[f"{row[1]}-{n}" for n, row in enumerate(API_REFUSALS)])
def test_electrical_schedules_api_refusals(api, params, reason):
    before = keys(api[1])
    response = api[0].post("/api/run?wait=1", json=body(api, params))
    assert response.status_code == 400
    assert response.json().get("ok") is not True
    records = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    assert records and all(rec["status"] == "failed" for rec in records)
    assert [(rec.get("error") or {}).get("reason_code") for rec in records] == [reason]
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 1
    assert keys(api[1]) == before
