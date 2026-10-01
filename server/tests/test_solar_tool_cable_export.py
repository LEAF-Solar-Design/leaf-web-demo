"""solar-cable-export: the plugin's CableExport Export All workbook (server/solar_inverter_outputs.py
cable_export) built from the CURRENT design graph through the route-aware electrical bridge and published as a
revision-bound XLSX artifact. The recorded i8 design rebuilt as a graph (2345 panels, 173 strings, 22 devices,
346 homerun legs and 14 feeders, every route written by the route bridge) reproduces the four sheets of the
plugin's saved i9 workbook row for row, the 14 feeder rows in the Homeruns sheet included, and adds the Feeder
Schedule its drawing setting asks for; lengths hold in every drawing unit; the XLSX reopens and is the same
bytes every run; every named refusal; the registry, readiness and the read rail with the stored artifact read
back. Nothing here writes a drawing version."""
import copy
import hashlib
import importlib.util
import json
import math
from pathlib import Path
import re

import pytest
from jsonschema import Draft7Validator

import broker_client
import catalog
import deps
import entitlements
import jobs
import product_capability_availability as availability
import solar_artifacts
import solar_electrical_route_bridge as rb
import solar_local_graph
import solar_local_read as local
import solar_tools
import solar_xlsx
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
TOOL = "solar-cable-export"
MPU = 0.0254
STATES = ROOT / "docs" / "parity" / "evidence" / "rooftop" / "inverters"
PLUGIN_WORKBOOK_TEXT = STATES / "i9-plugin-workbook.txt"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PLUGIN_SHEETS = ["Homeruns", "Equipment Schedule", "Inverter Schedule", "String Schedule"]


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ev = _load("solar_inverter_outputs_evidence_cable_export", ROOT / "scripts" / "solar_inverter_outputs_evidence.py")
st = rb.st
CAPTURE_RECORD = copy.deepcopy(ev.host_for("i9")["InverterCatalogRecord"])
I9_PARAMS = {"circuit_source": "labels", "inverter_record": CAPTURE_RECORD, "suggested_inverter_count": 5,
             "design_min_temp_c": -40}


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False,
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def builtin():
    return local._load_builtin(TOOL)


# ------------------------------------------------------------------ the i8 design as a graph --

def build_i8(w1, recorded):
    """The committed i8 state (the i9 step's input) as a sized W1 graph: one frameless panel per module, one
    string per state string (tag, module count and route from the state), then the ROUTE bridge's write-back of
    the state's equipment, its 346 dc-homerun legs and its 14 feeders."""
    state = st.load_state(STATES / "state-i8.json")
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
    binding = rb.adopt_state(g, state)
    g, _ = rb.graph_from_state(g, state, binding, defaults=DEFAULTS, new_id=minter(1000), created_at=CREATED)
    return validate_graph(g)


_I8 = {}


@pytest.fixture
def i8(graph, passing, service):
    if "graph" not in _I8:
        _I8["graph"] = build_i8(copy.deepcopy(graph), passing)
    return copy.deepcopy(_I8["graph"])


I8_SHA = "0732c4c09af1b126ef18e2328fe4cdfd8fb80293ba891317fc03b7d206b80d83"


def unsized(g):
    g = copy.deepcopy(g)
    g["settings"]["extra"].pop("string_sizing")
    g["settings"]["global_string_sizing_confirmed"] = False
    return g


def variant(name, g):
    g = copy.deepcopy(g)
    if name == "stale-string":
        g["strings"][0]["validity"] = {"state": "stale", "reasons": ["upstream_corrected"]}
    elif name == "stale-feeder":
        feeder = next(r for r in g["routes"] if r["route_kind"] == "feeder")
        feeder["validity"] = {"state": "stale", "reasons": ["settings_changed"]}
    elif name == "solaredge":
        g["inverters"][0]["is_solaredge"] = True
    elif name == "units":
        g["project"]["units"]["meters_per_unit"] *= 2
    elif name == "unverified":
        g["settings"]["extra"]["string_sizing"] = {"mode": "global"}
    elif name == "zones":
        g["settings"]["extra"]["string_sizing"] = {"mode": "zones"}
    return g


def sections(text):
    """[(sheet name, its lines)] of a workbook_text rendering."""
    out = []
    for line in text.splitlines():
        if line.startswith("# "):
            out.append((line[2:], []))
        else:
            out[-1][1].append(line)
    return out


def as_reopened(lines):
    """A workbook_text sheet as solar_xlsx.read_workbook returns it: (row index, cells) for every row with a
    value, cells dense from column A to the last non-empty one, None for an empty cell."""
    rows = []
    for index, line in enumerate(lines, 1):
        cells = line.split("\t")
        while cells and cells[-1] == "":
            cells.pop()
        if cells:
            rows.append((index, [cell if cell != "" else None for cell in cells]))
    return rows


def reopened(out):
    """The artifact checked and reopened: [(sheet name, [(row index, cells)])]."""
    assert type(out) is solar_artifacts.ArtifactOutput
    assert (out.media_type, out.filename) == (XLSX, "CableExport.xlsx")
    assert out.content[:4] == b"PK\x03\x04"
    return [(sheet.name, [(row.index, row.cells) for row in sheet.rows])
            for sheet in solar_xlsx.read_workbook(out.content)]


# -------------------------------------------------------------------------- the tests --

def test_cable_export_i8_graph(i8):
    kinds = [r["route_kind"] for r in i8["routes"]]
    assert (len(i8["panels"]), len(i8["strings"]), len(i8["inverters"]), len(i8["routes"])) == (2345, 173, 22, 360)
    assert (kinds.count("start homerun"), kinds.count("end homerun"), kinds.count("feeder")) == (173, 173, 14)
    assert (sha(i8), i8["rev"], i8["parent_rev"], i8["settings"]["use_l2_collectors"]) == (I8_SHA, 1, 0, True)
    assert require_current_export(i8) == i8


def test_cable_export_reproduces_the_plugin_workbook(i8):
    out = builtin().run(i8, I9_PARAMS)
    sheets = reopened(out)
    plugin = sections(PLUGIN_WORKBOOK_TEXT.read_text(encoding="utf-8-sig"))
    assert [name for name, _ in plugin] == PLUGIN_SHEETS
    assert [name for name, _ in sheets] == PLUGIN_SHEETS + ["Feeder Schedule"]
    for (name, rows), (plugin_name, lines) in zip(sheets, plugin):
        assert name == plugin_name and rows == as_reopened(lines), name
    homeruns = dict(sheets)["Homeruns"]
    assert len(homeruns) == 534
    feeders = [cells for _, cells in homeruns if cells[:3] == ["-", "-", "Feeder"]]
    assert [float(cells[4]) for cells in feeders] == \
        [165.42, 211.06, 410.57, 243.43, 203.66, 272.37, 201.43, 163.30, 36.42, 164.29, 248.35, 66.27,
         172.94, 88.37]
    assert [cells for _, cells in homeruns[-14:]] == feeders


FEEDER_SCHEDULE = [
    (1, ["Combiner Box #", "Inverter #", "Feeder Length (ft)"]),
    (2, ["1", "1", "164.3"]), (3, ["2", "1", "165.4"]), (4, ["3", "2", "410.6"]), (5, ["4", "2", "163.3"]),
    (6, ["5", "3", "36.4"]), (7, ["6", "3", "88.4"]), (8, ["7", "4", "248.3"]), (9, ["8", "5", "201.4"]),
    (10, ["9", "5", "203.7"]), (11, ["10", "6", "211.1"]), (12, ["11", "6", "172.9"]), (13, ["12", "7", "66.3"]),
    (14, ["13", "8", "243.4"]), (15, ["14", "8", "272.4"]),
]


def test_cable_export_feeder_schedule_lists_the_graph_feeders(i8, graph):
    assert dict(reopened(builtin().run(i8, I9_PARAMS)))["Feeder Schedule"] == FEEDER_SCHEDULE
    assert graph["settings"]["use_l2_collectors"] is False
    assert "Feeder Schedule" not in dict(reopened(builtin().run(copy.deepcopy(graph), {})))


SUMMARY = {"circuit_source": "topology", "strings": 173, "modules": 2345, "feeders": 14, "inverter_record": "absent",
           "sizing": "global", "module_catalog": "unresolved"}
W1_SUMMARY = dict(SUMMARY, strings=2, modules=3, feeders=0, sizing="absent")
ALL_SHEETS = ["Homeruns", "Equipment Schedule", "Inverter Schedule", "String Schedule", "Feeder Schedule"]
NO_EQUIPMENT = ["Homeruns", "Inverter Schedule", "String Schedule", "Feeder Schedule"]
OUTPUTS = [
    ("i8", {}, NO_EQUIPMENT, [534, 37, 179, 15], 35322,
     "37c572b36392c664c3369d2bb74af1bcb0d881726bb984a05f288a71329b3b0e", SUMMARY),
    ("i8", {"inverter_record": CAPTURE_RECORD}, ALL_SHEETS, [534, 16, 37, 179, 15], 36430,
     "b75d8f17c8668d2590c7222131a4eb1a67092007c9e1bf2d237333c366c1f1a2", dict(SUMMARY, inverter_record="caller")),
    ("i8", {"circuit_source": "labels"}, NO_EQUIPMENT, [534, 39, 179, 15], 35683,
     "42f7b59cc0c01fbf3acfec2e5e5accb39eed4423e56304635a8389bb7919503c", dict(SUMMARY, circuit_source="labels")),
    ("i8", I9_PARAMS, ALL_SHEETS, [534, 16, 39, 179, 15], 36796,
     "0bb20c4234c1d5c50bec8a039d5e86c227b5f232440af62143f3e1380ef8a4eb",
     dict(SUMMARY, circuit_source="labels", inverter_record="caller")),
    ("i8", dict(I9_PARAMS, project_location="Akron, OH", design_min_temp_c=-23.5), ALL_SHEETS,
     [534, 17, 39, 179, 15], 36825, "935ffb14da3274a2f1f97ebd5abb09b659380af54a078677a9823381590c99a6",
     dict(SUMMARY, circuit_source="labels", inverter_record="caller")),
    ("w1", {}, ["Homeruns", "Inverter Schedule", "String Schedule"], [4, 7, 8], 3376,
     "f10611f481701d4a27c8e59f62b7d10a3c24034ce760c1a5a4b5499bc2b88ad4", W1_SUMMARY),
    ("w1", {"circuit_source": "labels"}, ["Homeruns"], [4], 1790,
     "95824cf2b1dd68339e2b4afc1fc874802e84ceb25147e1e4eb77634ab913f03c",
     dict(W1_SUMMARY, circuit_source="labels", strings=0, modules=0)),
    ("w1", {"inverter_record": CAPTURE_RECORD}, PLUGIN_SHEETS, [4, 16, 7, 8], 4387,
     "8cd0c1000faa2237da224e5c876dceec87e160b7be6361d3ca490d393f913913", dict(W1_SUMMARY, inverter_record="caller")),
    ("w1", {"inverter_record": CAPTURE_RECORD, "suggested_inverter_count": 3, "design_min_temp_c": -12,
            "project_location": "Akron, OH"}, PLUGIN_SHEETS, [4, 17, 7, 8], 4421,
     "475c7c55ac1ee7eff28cc8d7a0bbf9d63b9d988d94624231f6077e39012be6e2", dict(W1_SUMMARY, inverter_record="caller")),
]


def subject(request, name, graph):
    return request.getfixturevalue("i8") if name == "i8" else copy.deepcopy(graph)


@pytest.mark.parametrize("name,params,sheets,rows,size,content_sha,summary", OUTPUTS,
                         ids=[f"{row[0]}-{n}" for n, row in enumerate(OUTPUTS)])
def test_cable_export_outputs(request, graph, name, params, sheets, rows, size, content_sha, summary):
    g = subject(request, name, graph)
    before, asked = copy.deepcopy(g), copy.deepcopy(params)
    out = builtin().run(g, params)
    assert [sheet for sheet, _ in reopened(out)] == sheets
    assert (len(out.content), hashlib.sha256(out.content).hexdigest()) == (size, content_sha)
    assert out.summary == {"status": "written", "sheets": sheets, "rows": rows, **summary}
    assert g == before and params == asked
    again = builtin().run(copy.deepcopy(g), copy.deepcopy(params))
    assert (again.content, again.summary) == (out.content, out.summary)


W1_HOMERUNS = [
    (1, ["Inverter/MPPT", "Circuit", "Which", "Mod / String", "Length (ft)", "Circuit Length (ft)",
         "One-Way Length (ft)"]),
    (2, ["1 - a", "1", "Start Homerun", "NA", "16.40", "19.69", "9.84"]),
    (3, ["1 - a", "1", "String", "2", "3.28", "19.69", "9.84"]),
    (4, ["1 - a", "2", "String", "1", "3.28", "3.28", "1.64"]),
]
W1_EQUIPMENT_HEAD = [
    (1, ["EQUIPMENT SCHEDULE"]),
    (3, ["Tag", "Description", "Manufacturer", "Model", "Qty", "Rating", "Listing", "NEC Ref"]),
    (4, ["INV-1..3", "String Inverter", "Sungrow", "SG250HX", "3", "250.0kW AC, 800V, 180.5A", "UL 1741", "690.4"]),
    (5, ["DC-DISC", "DC Disconnect", "(by installer)", "-", "3", "1500V, 30A", "UL 98", "690.13"]),
    (6, ["AC-DISC", "AC Disconnect", "(by installer)", "-", "3", "800V, 226A", "UL 98", "690.54"]),
    (8, ["DESIGN PARAMETERS"]), (9, ["Design Min Temp", "-12°C per NEC 690.7(A)(3)"]),
    (10, ["Project Location", "Akron, OH"]), (12, ["NEC REFERENCES"]),
]


def test_cable_export_host_inputs_reach_the_sheets(graph):
    params = {"inverter_record": CAPTURE_RECORD, "suggested_inverter_count": 3, "design_min_temp_c": -12,
              "project_location": "Akron, OH"}
    sheets = dict(reopened(builtin().run(copy.deepcopy(graph), params)))
    assert sheets["Homeruns"] == W1_HOMERUNS
    assert sheets["Equipment Schedule"][:9] == W1_EQUIPMENT_HEAD
    assert sheets["String Schedule"][1] == (2, ["S1-A1", "1", "A", "-", "2", "-", "-", "1500", "-", "-", "-", "-",
                                                "-", "19.7", "-", "-", "-", "-"])
    assert sheets["Inverter Schedule"][-1][1][0].endswith("Tmin = -12°C")
    plain = dict(reopened(builtin().run(copy.deepcopy(graph), {"inverter_record": CAPTURE_RECORD})))
    assert plain["Equipment Schedule"][2][1][:5] == ["INV-1", "String Inverter", "Sungrow", "SG250HX", "1"]
    assert "Project Location" not in [cells[0] for _, cells in plain["Equipment Schedule"]]


UNITS = [("in", 0.0254), ("m", 1.0), ("ft", 0.3048), ("mm", 0.001)]


@pytest.mark.parametrize("unit,mpu", UNITS, ids=[row[0] for row in UNITS])
def test_cable_export_w1_lengths_use_inches(graph, unit, mpu):
    expected = builtin().run(copy.deepcopy(graph), {}).content
    g = copy.deepcopy(graph)
    g["project"]["units"].update(drawing_units=unit, meters_per_unit=mpu, drawing_unit_is_feet=(unit == "ft"))
    before = copy.deepcopy(g)
    out = builtin().run(g, {})
    assert out.content == expected
    assert dict(reopened(out))["Homeruns"] == W1_HOMERUNS
    assert g == before


I8_UNITS = [("m", 1.0), ("ft", 0.3048), ("mm", 0.001), ("cm", 0.01)]
I8_UNSIZED_SHA = "c0d7d50a81a9d7284cb3f8ec777259fe231e1c42e2cd827d0089b0b1fd1067c6"


@pytest.mark.parametrize("unit,mpu", I8_UNITS, ids=[row[0] for row in I8_UNITS])
def test_cable_export_i8_lengths_use_inches(i8, unit, mpu):
    base = unsized(i8)
    expected = builtin().run(copy.deepcopy(base), I9_PARAMS).content
    assert (len(expected), hashlib.sha256(expected).hexdigest()) == (36315, I8_UNSIZED_SHA)
    g = copy.deepcopy(base)
    g["project"]["units"].update(drawing_units=unit, meters_per_unit=mpu, drawing_unit_is_feet=(unit == "ft"))
    assert builtin().run(g, I9_PARAMS).content == expected
    homeruns = dict(reopened(builtin().run(g, I9_PARAMS)))["Homeruns"]
    assert homeruns[1] == (2, ["1 - a", "1", "End Homerun", "NA", "29.69", "163.26", "81.63"])


def test_cable_export_no_strings_writes_nothing(graph):
    g = bare(copy.deepcopy(graph))
    before = copy.deepcopy(g)
    out = builtin().run(g, {})
    assert out == {"status": "no-export", "reason": "no-homeruns", **dict(W1_SUMMARY, strings=0, modules=0)}
    assert sha(out) == "b892b622ecad1e761b93e0e10c70e857bba9d41eb65b88b06b68cdf58e23cfb8"
    assert g == before


def record(**changes):
    value = copy.deepcopy(CAPTURE_RECORD)
    value.update(changes)
    return value


BAD_REQUESTS = [
    None, [], "topology", {"x": 1}, {"drawing_id": "solar"}, {"circuit_source": "graph"}, {"circuit_source": None},
    {"inverter_record": None}, {"inverter_record": {}}, {"inverter_record": record(extra="1")},
    {"inverter_record": record(companyName="x" * 129)}, {"inverter_record": record(companyName="Sun\ngrow")},
    {"inverter_record": record(maxDCPower="1e3")}, {"inverter_record": record(maxDCPower=375)},
    {"suggested_inverter_count": 0}, {"suggested_inverter_count": 10001}, {"suggested_inverter_count": True},
    {"suggested_inverter_count": "5"}, {"suggested_inverter_count": 2.5}, {"suggested_inverter_count": None},
    {"design_min_temp_c": -90.5}, {"design_min_temp_c": 60.01}, {"design_min_temp_c": -1000},
    {"design_min_temp_c": True}, {"design_min_temp_c": "-40"}, {"design_min_temp_c": None},
    {"project_location": ""}, {"project_location": "x" * 129}, {"project_location": "Akron\nOH"},
    {"project_location": "Akron\x7f"}, {"project_location": 7}, {"project_location": None},
]


@pytest.mark.parametrize("params", BAD_REQUESTS, ids=[f"bad-{n}" for n in range(len(BAD_REQUESTS))])
def test_cable_export_request_shape_fails_closed(graph, params):
    before, asked = copy.deepcopy(graph), copy.deepcopy(params)
    with pytest.raises(GraphValidationError) as error:
        builtin().run(graph, params)
    assert error.value.code == "INVALID_CABLE_EXPORT_REQUEST"
    assert graph == before and asked == params


GOOD_REQUESTS = [
    {"suggested_inverter_count": 1}, {"suggested_inverter_count": 10000}, {"suggested_inverter_count": 5.0},
    {"design_min_temp_c": -90}, {"design_min_temp_c": 60}, {"design_min_temp_c": -23.5},
    {"project_location": "A"}, {"project_location": "x" * 128},
    {"inverter_record": record(companyName="x" * 128, seriesName="", maxDCPower="")},
]


@pytest.mark.parametrize("params", GOOD_REQUESTS, ids=[f"good-{n}" for n in range(len(GOOD_REQUESTS))])
def test_cable_export_boundary_requests_agree_with_the_schema(graph, params):
    assert Draft7Validator(solar_tools.trusted_record(TOOL)["params"]).is_valid(params)
    assert builtin().run(copy.deepcopy(graph), params).summary["status"] == "written"


REFUSALS = [
    ("units", "UNRESOLVED_UNITS"),
    ("stale-string", "SOLAR_OUTPUT_NOT_CURRENT"),
    ("stale-feeder", "SOLAR_OUTPUT_NOT_CURRENT"),
    ("solaredge", "SCHEDULES_OPTIMIZERS_UNSUPPORTED"),
    ("unverified", "SIZING_CONFIRMATION_REQUIRED"),
]


@pytest.mark.parametrize("name,code", REFUSALS, ids=[row[0] for row in REFUSALS])
def test_cable_export_graph_refusals(i8, name, code):
    g = variant(name, i8)
    before = copy.deepcopy(g)
    with pytest.raises(GraphValidationError) as error:
        builtin().run(g, {})
    assert error.value.code == code and g == before


def test_cable_export_zone_sizing_is_refused(graph, monkeypatch):
    module = builtin()
    monkeypatch.setattr(module.solar_sizing_client, "require_sizing", lambda value: value)
    with pytest.raises(GraphValidationError) as error:
        module.run(variant("zones", graph), {})
    assert error.value.code == "SCHEDULES_ZONE_SIZING_UNSUPPORTED"


def test_cable_export_named_failures(graph, monkeypatch):
    module = builtin()

    def bridge_refuses(value):
        raise rb.ElectricalBridgeError("BRIDGE_ROUTE_TOPOLOGY_MISMATCH")

    def kernel_refuses(*args):
        raise module.kernel.InverterOutputError("refused")

    real = module.st.validate_state

    def state_refuses(value):
        if any("panel_count" in row["_detail"] for row in value["rows"]["string-assignment"]):
            raise module.st.InverterStateError("refused")
        return real(value)

    for target, name, patch, code in ((module.bridge, "state_from_graph", bridge_refuses,
                                       "BRIDGE_ROUTE_TOPOLOGY_MISMATCH"),
                                      (module.kernel, "cable_export", kernel_refuses, "SCHEDULES_KERNEL_REFUSED"),
                                      (module.st, "validate_state", state_refuses, "SCHEDULES_MAPPING_FAILED")):
        with monkeypatch.context() as patched:
            patched.setattr(target, name, patch)
            with pytest.raises(GraphValidationError) as error:
                module.run(copy.deepcopy(graph), {})
            assert error.value.code == code


def test_cable_export_refuses_after_a_settings_edit(graph):
    settings = solar_local_graph._load_builtin("solar-settings")
    after = settings.run(copy.deepcopy(graph), {"expected_rev": graph["rev"], "changes": {"home_run_layer": "H2"}})
    assert builtin().input_readiness(copy.deepcopy(after)) == {"input_ready": False,
                                                                "input_reason": "solar_output_not_current"}
    with pytest.raises(GraphValidationError) as error:
        builtin().run(copy.deepcopy(after), {})
    assert error.value.code == "SOLAR_OUTPUT_NOT_CURRENT"


DECLARATION_PARAMS = json.loads((ROOT / "server" / "solar_tools" / "solar_cable_export.json")
                                .read_text(encoding="utf-8"))["record"]["params"]


@pytest.mark.parametrize("params,accepted", [
    ({"project_location": "Akron\ufffe", "inverter_record": CAPTURE_RECORD}, False),
    ({"project_location": "Akron\uffff", "inverter_record": CAPTURE_RECORD}, False),
    ({"inverter_record": record(companyName="Sun\ufffe")}, False),
    ({"inverter_record": record(modelName="SG\uffff")}, False),
    ({"inverter_record": record(seriesName="SG\ufffe")}, False),
    ({"project_location": "Akron\ufffd", "inverter_record": CAPTURE_RECORD}, True),
], ids=["location-fffe", "location-ffff", "company-fffe", "model-ffff", "series-fffe", "location-fffd"])
def test_cable_export_text_refuses_noncharacters(graph, params, accepted):
    assert Draft7Validator(DECLARATION_PARAMS).is_valid(params) is accepted
    if not accepted:
        with pytest.raises(GraphValidationError) as error:
            builtin().run(copy.deepcopy(graph), params)
        assert error.value.code == "INVALID_CABLE_EXPORT_REQUEST"
    else:
        sheets = dict(reopened(builtin().run(copy.deepcopy(graph), params)))
        assert ["Project Location", "Akron\ufffd"] in [cells for _, cells in sheets["Equipment Schedule"]]


@pytest.mark.parametrize("ch", ["\x00", "\x1f", "\ufffe", "\uffff"])
def test_xml_chars_circuit_tag_refused(graph, ch):
    g = copy.deepcopy(graph)
    g["strings"][0]["circuit_tag"] = "+1/1a" + ch
    with pytest.raises(GraphValidationError) as error:
        builtin().run(g, {"circuit_source": "labels"})
    assert error.value.code == "SCHEDULES_KERNEL_REFUSED"


def test_xml_chars_start_and_midpoint_scale(graph, monkeypatch):
    module = builtin()
    real = module.bridge.state_from_graph

    def with_points(*args, **kwargs):
        state, binding = real(*args, **kwargs)
        for geometry in state["geometry"]["strings"]:
            geometry["start"] = [3.0, 4.0]
            geometry["midpoint"] = [5.0, 6.0]
        return state, binding

    monkeypatch.setattr(module.bridge, "state_from_graph", with_points)
    g = copy.deepcopy(graph)
    g["project"]["units"].update(drawing_units="m", meters_per_unit=1.0, drawing_unit_is_feet=False)
    state = module.export_state(validate_graph(g), "topology")
    assert state["geometry"]["strings"]
    for geometry in state["geometry"]["strings"]:
        for key, expected in (("start", [3 / 0.0254, 4 / 0.0254]),
                              ("midpoint", [5 / 0.0254, 6 / 0.0254])):
            assert len(geometry[key]) == 2
            assert all(math.isclose(value, target, rel_tol=1e-12)
                       for value, target in zip(geometry[key], expected))


@pytest.mark.parametrize("unit,mpu", [("m", 1.0), ("ft", 0.3048), ("mm", 0.001)],
                         ids=["m", "ft", "mm"])
def test_cable_export_state_is_in_inches(graph, unit, mpu):
    inch = copy.deepcopy(graph)
    inch["project"]["units"].update(drawing_units="in", meters_per_unit=0.0254, drawing_unit_is_feet=False)
    expected = builtin().export_state(validate_graph(inch), "topology")
    g = copy.deepcopy(graph)
    g["project"]["units"].update(drawing_units=unit, meters_per_unit=mpu, drawing_unit_is_feet=(unit == "ft"))
    actual = builtin().export_state(validate_graph(g), "topology")

    def same_point(point, inch_point):
        if inch_point is None:
            assert point is None
        else:
            assert point is not None and len(point) == len(inch_point)
            assert all(math.isclose(value, inch_value, rel_tol=1e-12, abs_tol=1e-9)
                       for value, inch_value in zip(point, inch_point))

    assert len(actual["geometry"]["strings"]) == len(expected["geometry"]["strings"])
    for geometry, inch_geometry in zip(actual["geometry"]["strings"], expected["geometry"]["strings"]):
        assert len(geometry["vertices"]) == len(inch_geometry["vertices"])
        for vertex, inch_vertex in zip(geometry["vertices"], inch_geometry["vertices"]):
            same_point(vertex, inch_vertex)
        for key in ("start", "end", "midpoint"):
            same_point(geometry[key], inch_geometry[key])
    assert len(expected["rows"]["device"]) == 1
    assert sum(geometry[key] is not None for geometry in expected["geometry"]["strings"]
               for key in ("start", "end", "midpoint")) == 4
    assert len(actual["rows"]["device"]) == len(expected["rows"]["device"])
    for row, inch_row in zip(actual["rows"]["device"], expected["rows"]["device"]):
        same_point(st.point_of(row["position"]), st.point_of(inch_row["position"]))
    assert len(actual["rows"]["cable"]) == len(expected["rows"]["cable"])
    for row, inch_row in zip(actual["rows"]["cable"], expected["rows"]["cable"]):
        assert len(row["vertices"]) == len(inch_row["vertices"])
        for vertex, inch_vertex in zip(row["vertices"], inch_row["vertices"]):
            same_point(st.point_of(vertex), st.point_of(inch_vertex))


@pytest.mark.parametrize("field", ["companyName", "modelName", "seriesName", "project_location", "drawing_id"])
def test_cable_export_declaration_bounds(field):
    schema = Draft7Validator(DECLARATION_PARAMS)
    for length, accepted in ((128, True), (129, False)):
        params = {"drawing_id": "d1"}
        if field in ("companyName", "modelName", "seriesName"):
            params["inverter_record"] = record(**{field: "x" * length})
        else:
            params[field] = "x" * length
        assert schema.is_valid(params) is accepted


def test_cable_export_registry_and_catalog(monkeypatch):
    declaration = solar_tools.get(TOOL)
    actual = copy.deepcopy(declaration)
    description = actual["record"].pop("description")
    assert type(description) is str and description.strip() and len(description) <= 1024
    assert actual == {
        "schema": "leaf.solar-tool.v1", "name": TOOL, "builtin": "builtins/solar_cable_export.py",
        "family": "schedules", "adapter": "local-graph-read", "entitlement": "run_read",
        "requires_persisted_graph": True, "seedable": False, "invalid_request_code": "INVALID_CABLE_EXPORT_REQUEST",
        "readiness": {"kind": "hook"}, "engine": "server-builtin", "interaction": {"mode": "form"},
        "record_store": "registry",
        "record": {"name": TOOL, "version": "1.0.0", "kind": "script", "family_id": "schedules",
                   "engine_op": "solar_cable_export", "entry": "builtins/solar_cable_export.py",
                   "params": DECLARATION_PARAMS, "returns": {"type": "object"}, "capabilities": ["drawing.read"],
                   "allow_local_fallback": False},
        "ledger": ["cable-export"], "trusted_inputs": [], "maturity": "preview", "wave": 2, "order": 120,
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


def test_cable_export_params_schema():
    params = solar_tools.trusted_record(TOOL)["params"]
    assert set(params["properties"]) == {"drawing_id", "circuit_source", "inverter_record",
                                         "suggested_inverter_count", "design_min_temp_c", "project_location"}
    assert params["properties"]["suggested_inverter_count"] == {"type": "integer", "minimum": 1, "maximum": 10000}
    assert params["properties"]["design_min_temp_c"] == {"type": "number", "minimum": -90, "maximum": 60}
    schema = Draft7Validator(params)
    for bad in ({"x": 1}, {"drawing_id": "x" * 129}, {"circuit_source": "graph"}, {"inverter_record": {}},
                {"suggested_inverter_count": 0}, {"suggested_inverter_count": 10001},
                {"suggested_inverter_count": 2.5}, {"suggested_inverter_count": True},
                {"design_min_temp_c": -90.5}, {"design_min_temp_c": 61}, {"design_min_temp_c": "-40"},
                {"project_location": ""}, {"project_location": "x" * 129}, {"project_location": "Akron\nOH"},
                {"project_location": "Akron, OH\n"}, {"project_location": "Akron\x7f"},
                {"inverter_record": record(maxDCPower="12\n")}):
        assert not schema.is_valid(bad), bad


def test_cable_export_trailing_newline_is_refused_by_both(graph):
    schema = Draft7Validator(solar_tools.trusted_record(TOOL)["params"])
    for bad in ({"project_location": "Akron, OH\n"}, {"inverter_record": record(maxACPower="250\n")}):
        assert not schema.is_valid(bad)
        with pytest.raises(GraphValidationError) as error:
            builtin().run(copy.deepcopy(graph), bad)
        assert error.value.code == "INVALID_CABLE_EXPORT_REQUEST"


def test_cable_export_readiness(graph, i8):
    assert availability.w1_graph_readiness(graph)[TOOL] == {"input_ready": True, "input_reason": None}
    assert builtin().input_readiness(i8) == {"input_ready": True, "input_reason": None}
    for name, reason in (("stale-string", "solar_output_not_current"),
                         ("solaredge", "schedules_optimizers_unsupported"),
                         ("unverified", "sizing_confirmation_required")):
        assert availability.w1_graph_readiness(variant(name, graph))[TOOL] == {
            "input_ready": False, "input_reason": reason}
    assert availability.w1_local_commit_inputs(variant("units", graph))[TOOL] == {
        "input_ready": False, "input_reason": "unresolved_units"}


READINESS_REASONS = {"unresolved_units", "solar_output_not_current", "sizing_confirmation_required",
                     "schedules_optimizers_unsupported", "schedules_zone_sizing_unsupported",
                     "schedules_mapping_failed", "schedules_kernel_refused", "schedules_input_unsupported"}


def test_cable_export_readiness_reasons_are_literal_and_mapped():
    # The Solar rail's copy test (web/src/lib/ribbonClusters.test.js RC6) reads every literal
    # "input_reason": "<code>" in a hook builtin and refuses a source that derives codes with .lower().
    source = (ROOT / "server" / "builtins" / "solar_cable_export.py").read_text(encoding="utf-8")
    assert ".lower()" not in source
    assert set(re.findall(r'"input_reason":\s*"([a-z][a-z0-9_]{0,63})"', source)) == READINESS_REASONS
    module = builtin()
    assert {value["input_reason"] for value in module.NOT_READY.values()} | {
        module.UNSUPPORTED["input_reason"]} == READINESS_REASONS


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


API_PARAMS = {"inverter_record": CAPTURE_RECORD, "suggested_inverter_count": 3, "design_min_temp_c": -12,
              "project_location": "Akron, OH"}


def test_cable_export_api_writes_one_artifact(api, graph):
    before = keys(api[1])
    response = api[0].post("/api/run?wait=1", json=body(api, API_PARAMS))
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True and env["execution_provenance"]["execution_path"] == "local"
    result = env["result"]
    assert jobs.get_job(result["job_id"])["status"] == "complete"
    output = result["output"]
    assert set(output) == {"summary", "artifact"}
    assert output["summary"] == {"status": "written", "sheets": PLUGIN_SHEETS, "rows": [4, 17, 7, 8],
                                 **dict(W1_SUMMARY, inverter_record="caller")}
    ref = output["artifact"]
    assert (ref["schema"], ref["media_type"], ref["filename"], ref["byte_length"], ref["content_sha256"],
            ref["source_version"]) == ("leaf.solar-artifact-ref.v1", XLSX, "CableExport.xlsx", 4421,
                                       "475c7c55ac1ee7eff28cc8d7a0bbf9d63b9d988d94624231f6077e39012be6e2", 1)
    assert ref["download"] == "/api/drawings/solar/artifacts/" + ref["artifact_id"]
    assert (result["output_sha256"], result["output_bytes"]) == (OUTPUT_SHA256, OUTPUT_BYTES)
    meta, content = solar_artifacts.read_artifact(api[1], TENANT, "solar", ref["artifact_id"], require_head=True)
    assert (meta["tool"], meta["media_type"], meta["source_version"]) == (TOOL, XLSX, 1)
    assert content == builtin().run(copy.deepcopy(graph), API_PARAMS).content
    assert [sheet.name for sheet in solar_xlsx.read_workbook(content)] == PLUGIN_SHEETS
    added = keys(api[1]) - before
    assert len(added) == 2
    again = api[0].post("/api/run?wait=1", json=body(api, API_PARAMS)).json()
    assert again["result"]["output"] == output and keys(api[1]) - before == added
    assert len(api[3]) == 2
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 1


OUTPUT_SHA256 = "8e74a3c4458786952c3d1ef54af45f7716abc16a31708fa0f5476ed518a0a9b2"
OUTPUT_BYTES = 743

API_REFUSALS = [
    ({"circuit_source": "graph"}, "tool_params_invalid"),
    ({"suggested_inverter_count": 0}, "tool_params_invalid"),
    ({"project_location": "Akron\nOH"}, "tool_params_invalid"),
    ({"inverter_record": record(companyName="Sun\ngrow")}, "tool_params_invalid"),
]


@pytest.mark.parametrize("params,reason", API_REFUSALS, ids=[f"{row[1]}-{n}" for n, row in enumerate(API_REFUSALS)])
def test_cable_export_api_refusals(api, params, reason):
    before = keys(api[1])
    response = api[0].post("/api/run?wait=1", json=body(api, params))
    assert response.status_code == 400
    assert response.json().get("ok") is not True
    records = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    assert records and all(rec["status"] == "failed" for rec in records)
    assert [(rec.get("error") or {}).get("reason_code") for rec in records] == [reason]
    assert store.load_manifest(api[1], TENANT, "solar")["head"] == 1
    assert keys(api[1]) == before
