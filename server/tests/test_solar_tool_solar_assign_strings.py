"""solar-assign-strings: AssignStrings (Auto, the pattern matcher) on the design graph through the electrical
state bridge. The recorded i2 receipt replayed exactly (assignments, tags, inputs and colours), the published
graph shown to be the complete i3 intake, the host inputs derived from the graph, the C11 boundary (a label
naming inverter 1 never becomes an inverter), every named refusal, the registry and the commit rail."""
import copy
import hashlib
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

SERVER = Path(__file__).resolve().parents[1]
ROOT = SERVER.parent
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

import catalog
import deps
import entitlements
import jobs
import product_capability_availability as availability
import solar_electrical_state_bridge as bridge
import solar_local_graph
import solar_tools
import store
from solar_design_graph import GraphValidationError, entities, validate_graph
from solar_sizing_client import sizing_basis
from solar_solve_results import sync_assignments, upstream_basis
from test_w1_design_graph import app_id, entity, graph  # noqa: F401
from test_w1_local_graph_adapter import held
from test_w1_local_graph_jobs import isolated_jobs, no_network  # noqa: F401
import test_w1_local_graph_rail as rail
from test_w1_solve_commit import seed
from test_solar_w2_registration import dispatch, head_graph, latest
import test_solar_ground_topology as topo
import test_solar_pvcase_solve as pv_tests

TOOL = "solar-assign-strings"
TENANT = "fixture-tenant"
MPU = 0.0254
BASE = {"G": "67ab25efcda14a69da65a4184cf5d362ac19a85dcf266f722a9528f5881737c3"}
S1, S2 = app_id("string", 1), app_id("string", 2)
INV1 = app_id("inverter", 1)
ASSIGN = {"operation": "assign-strings", "accept_excess_capacity": True}
# The capture host's DeviceNumbers (scripts/solar_inverter_strings_evidence.py CAPTURE_HOST, i1's log).
I1_NUMBERS = (([14298.6, 1644.1], 1), ([16758.4, 1576.1], 2), ([18680.2, 2030.8], 3), ([20260.9, 2025.7], 4),
              ([14522.6, 3955.0], 5), ([16613.1, 4121.3], 6), ([18437.0, 3800.2], 7), ([20260.9, 3955.0], 8))


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode("utf-8")).hexdigest()


def builtin():
    return solar_local_graph._load_builtin(TOOL)


def call(g, **params):
    """The operation's full answer: graph, assigned, collector_colours, lines."""
    request = dict({"accept_excess_capacity": True}, **params)
    request.setdefault("expected_rev", g["rev"])
    return builtin().OPERATIONS["assign-strings"](g, request)


# --- graph variants -----------------------------------------------------------------------------------------------


def string_entity(n, route, circuit_tag, handle=None):
    value = entity("string", n, circuit_tag=circuit_tag, circuit_kind="String", ordered_panel_refs=[],
                   module_count=0, from_ref=None, to_ref=None, tag_text_ref=None, wire_gauge="", length_ft=0,
                   route=route, inverter_ref=None)
    if handle is not None:
        value["provenance"]["source_handle"] = handle
    return value


def du(x, y):
    """A drawing-unit point in graph metres."""
    return [x * MPU, y * MPU]


def variant(graph, name):
    g = copy.deepcopy(graph)
    assert sha(validate_graph(g)) == BASE["G"]
    if name == "W":
        return validate_graph(g)
    if name in ("A", "U", "P"):
        g["electrical_zones"] = []
        g["frames"][0]["electrical_zone_ref"] = None
        unassign = {"A": (), "U": (S1, S2), "P": (S2,)}[name]
        for string in g["strings"]:
            if string["id"] in unassign:
                string.update(inverter_ref=None, to_ref=None)
        g["inverters"][0]["input_assignments"] = [
            a for a in g["inverters"][0]["input_assignments"] if a["string_ref"] not in unassign]
        sync_assignments(g)
        return validate_graph(g)
    g = topo.bare(g)
    xs = (0, 10, 20, 30, 40, 50)
    g["strings"] = [string_entity(n, [du(x, 20), du(x, 25)], "-") for n, x in enumerate(xs, 1)]
    if name == "L2":
        g["inverters"] = [topo.central(1, 1, [], position=du(0, 0), mppt_count=2, total_dc_inputs=4),
                          topo.central(2, 2, [], position=du(100, 0), mppt_count=2, total_dc_inputs=4)]
    elif name == "CB":
        l2 = app_id("inverter", 3)
        g["inverters"] = [topo.combiner(1, 1, l2, position=du(0, 0), total_dc_inputs=3),
                          topo.combiner(2, 2, l2, position=du(100, 0), total_dc_inputs=4),
                          topo.central(3, 1, [{"inverter_ref": app_id("inverter", 1), "mppt_index": 0},
                                              {"inverter_ref": app_id("inverter", 2), "mppt_index": 1}],
                                       position=du(50, -100))]
    elif name == "MIX":
        g["inverters"] = [topo.central(1, 1, [], position=du(0, 0), mppt_count=2, total_dc_inputs=4),
                          topo.central(2, 2, [], position=du(100, 0), mppt_count=2, total_dc_inputs=6)]
    else:
        raise AssertionError(name)
    return validate_graph(g)


def c11_graph(graph, numbers):
    """The C11 PVcase case: 88 strings (the committed eleven-group intake's count), each labelled the way
    the solve's fallback labels it (L2 1), none assigned; central inverters numbered `numbers`."""
    outcome = pv_tests.pv.pvcase_solve(json.loads(pv_tests.COMMITTED_INTAKE.read_text(encoding="utf-8")))
    assert (outcome["strings_created"], outcome["l2_count"]) == (88, 0)
    g = topo.bare(graph)
    g["strings"] = [string_entity(n, [[float(n), 0.0], [float(n), 2.0]], f"+{n}/1a") for n in range(1, 89)]
    g["inverters"] = [topo.central(k, number, [], position=[44.0 * (k - 1), 1.0])
                      for k, number in enumerate(numbers, 1)]
    return validate_graph(g)


def i2_graph(graph):
    """The committed state-i1 (the i2 intake) as a graph: its 8 central inverters (numbered by the capture's
    DeviceNumbers, the kernel's own numbering), its 173 unassigned strings (routes = the string polylines,
    source_handle = the handle); drawing NumMppt 3 and StringPerMppt 3 as the state holds them; each inverter
    6 MPPTs of 6 inputs (the tag records i2 wrote)."""
    state = json.loads((ROOT / "docs/parity/evidence/rooftop/inverters/state-i1.json").read_text(encoding="utf-8"))
    g = topo.bare(graph)
    g["settings"].update(num_mppt=3, strings_per_mppt=3)
    for k, device in enumerate(state["rows"]["device"], 1):
        x, y = device["position"]["value"]
        (number,) = [n for (px, py), n in I1_NUMBERS if abs(px - x) <= 0.05 and abs(py - y) <= 0.05]
        g["inverters"].append(topo.central(k, number, [], position=du(x, y)))
    geometry = {item["string"]: item for item in state["geometry"]["strings"]}
    for n, row in enumerate(state["rows"]["string-assignment"], 1):
        route = [du(*vertex) for vertex in geometry[row["string"]]["vertices"]]
        g["strings"].append(string_entity(n, route, "-", handle=row["string"]))
    return validate_graph(g)


def receipt_rows(capability, fixture):
    path = ROOT / "docs" / "parity" / "receipts" / capability / f"{fixture}.json"
    value = json.loads(path.read_text(encoding="utf-8"))
    assert value["capability"] == capability and value["comparator"]["verdict"] == "pass"
    sides = [[row for row in value["comparison"][side]["after"]["rows"] if row["type"] == "string-assignment"]
             for side in ("plugin", "studio")]
    assert sides[0] == sides[1]
    return {row["string"]: (row["device"], row["input"], row["label"], row["colour"]) for row in sides[0]}


def by_handle(result, answer):
    """{source_handle: (inverter number, MPPT index, circuit tag, colour)} of every assigned string."""
    numbers = {inverter["id"]: inverter["number"] for inverter in result["inverters"]}
    inputs = {a["string_ref"]: a for inverter in result["inverters"] for a in inverter["input_assignments"]}
    strings = {string["id"]: string for string in result["strings"]}
    return {strings[row["string_ref"]]["provenance"]["source_handle"]:
            (numbers[row["inverter_ref"]], ord(inputs[row["string_ref"]]["mppt_letter"]) - ord("A") + 1,
             row["circuit_tag"], row["colour"]) for row in answer["assigned"]}


def only_assignment_changed(before, after):
    """No design input moved: project, settings, panels, zones and both digests are byte-identical; every
    changed entity is a string, inverter, frame, route or schedule; rev advanced by one."""
    old = {e["id"]: e for e in entities(before)}
    changed = {e["kind"] for e in entities(after) if old.get(e["id"]) != e}
    return (changed <= {"string", "inverter", "frame", "route", "schedule"}
            and [e["id"] for e in entities(before)] == [e["id"] for e in entities(after)]
            and before["project"] == after["project"] and before["settings"] == after["settings"]
            and before["panels"] == after["panels"] and before["electrical_zones"] == after["electrical_zones"]
            and upstream_basis(before) == upstream_basis(after) and sizing_basis(before) == sizing_basis(after)
            and (after["rev"], after["parent_rev"]) == (before["rev"] + 1, before["rev"]))


# --- the receipts -------------------------------------------------------------------------------------------------


def test_assign_strings_i2_receipt_replay(graph):
    g = i2_graph(graph)
    before = copy.deepcopy(g)
    assert (len(g["strings"]), len(g["inverters"])) == (173, 8)
    assert sha(g) == '687c9781af8c913da8185f3148ff86e5d7c2d66e7e43815745cdd5bece16a402'
    answer = call(g)
    assert g == before
    result = answer["graph"]
    assert sha(result) == 'ae0f972ee14324b7953a2ae83c47995c64b431a9d5214804ae4e77c529da837f'
    assert by_handle(result, answer) == receipt_rows("assign-strings", "rooftop-inverters-i2")
    numbers = {inverter["id"]: inverter["number"] for inverter in result["inverters"]}
    assert {numbers[ref]: colour for ref, colour in answer["collector_colours"].items()} == \
        {1: 2, 2: 6, 5: 5, 6: 3, 7: 4}
    assert answer["lines"] == ["Pattern assignment complete. 173 strings assigned to 8 collectors in 1 seconds."]
    assert only_assignment_changed(g, result)


def test_assign_strings_result_is_the_i3_intake(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, i2_graph(graph))
    with held(backend) as fence:
        receipt = dispatch(backend, fence, TOOL, dict(ASSIGN, expected_rev=0))
    assert receipt["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    assert latest(backend) == 2
    result = head_graph(backend)
    state, binding = bridge.state_from_graph(result)
    after, lines = builtin().kernel.color_strings(state, {})
    assert lines == ["LEAFCOLORSTRINGS: recoloured 173 of 173 strings across 5 inverter(s); "
                     "drew 0 in-block module overlay(s)."]
    source = {string["id"]: string["provenance"]["source_handle"] for string in result["strings"]}
    assert {source[binding["strings"][row["string"]]["id"]]: (row["device"], row["input"], row["label"], row["colour"])
            for row in after["rows"]["string-assignment"]} == receipt_rows("color-strings", "rooftop-inverters-i3")


# --- positive cases -----------------------------------------------------------------------------------------------


def short(g, answer):
    names = {string["id"]: f"S{n}" for n, string in enumerate(g["strings"], 1)}
    numbers = {inverter["id"]: inverter["number"] for inverter in answer["graph"]["inverters"]}
    inputs = {a["string_ref"]: (a["mppt_letter"], a["input_number"])
              for inverter in answer["graph"]["inverters"] for a in inverter["input_assignments"]}
    return [(names[row["string_ref"]], numbers[row["inverter_ref"]], row["circuit_tag"]) + inputs[row["string_ref"]]
            + (row["colour"],) for row in answer["assigned"]]


@pytest.mark.parametrize("name,rows,collectors,lines,graph_sha", [
    ('U',
     [('S1', 1, '+1/1a', 'A', 0, 2),
      ('S2', 1, '+2/1a', 'A', 1, 2)],
     [(1, 2)],
     ['2 unassigned strings, 2 available slots across 1 inverter(s).',
      'Pattern assignment complete. 2 strings assigned to 1 collectors in 1 seconds.'],
     '6be98e62746f8a1336a63bb601365426278d3e9d78ad304b2069c83643482ac6'),
    ('P',
     [('S2', 1, '+2/1a', 'A', 1, 256)],
     [(1, 256)],
     ['1 unassigned strings, 1 available slots across 1 inverter(s).',
      'Pattern assignment complete. 1 strings assigned to 1 collectors in 1 seconds.'],
     'c80f85befad769d75653ccc43b821230a19a3a8f08ae90f6e1ac8c098a9ef201'),
    ('L2',
     [('S1', 1, '+1/1a', 'A', 0, 2),
      ('S2', 1, '+2/1a', 'A', 1, 2),
      ('S3', 1, '+3/1b', 'B', 0, 2),
      ('S4', 1, '+4/1b', 'B', 1, 2),
      ('S5', 2, '+6/2a', 'A', 0, 5),
      ('S6', 2, '+5/2a', 'A', 1, 5)],
     [(1, 2), (2, 5)],
     ['Pattern assignment complete. 6 strings assigned to 2 collectors in 1 seconds.'],
     '4695e76c87054980b8001f3c071bc590a887c7a7a02d5ef9d26dcbc78a497ad3'),
    ('CB',
     [('S1', 1, '+1/1a', 'A', 0, 2),
      ('S2', 1, '+2/1a', 'A', 1, 2),
      ('S3', 1, '+3/1a', 'A', 2, 2),
      ('S4', 2, '+6/2a', 'A', 0, 5),
      ('S5', 2, '+5/2a', 'A', 1, 5),
      ('S6', 2, '+4/2a', 'A', 2, 5)],
     [(1, 2), (2, 5)],
     ['Pattern assignment complete. 6 strings assigned to 2 collectors in 1 seconds.'],
     '487470cd92996f7ac3f3ee2be3dc36516647aa1317bbe32c1694da947df52cdd'),
])
def test_assign_strings_assigns(graph, name, rows, collectors, lines, graph_sha):
    g = variant(graph, name)
    before = copy.deepcopy(g)
    answer = call(g)
    assert g == before
    assert short(g, answer) == rows
    numbers = {inverter["id"]: inverter["number"] for inverter in answer["graph"]["inverters"]}
    assert sorted((numbers[ref], colour) for ref, colour in answer["collector_colours"].items()) == collectors
    assert answer["lines"] == lines
    assert sha(answer["graph"]) == graph_sha
    assert only_assignment_changed(g, answer["graph"])
    assert builtin().run(g, dict(ASSIGN, expected_rev=0)) == answer["graph"]


def test_assign_strings_stales_dependent_outputs(graph):
    g = variant(graph, "U")
    unrelated = copy.deepcopy(g["schedules"][0])
    unrelated.update(id=app_id("schedule", 2), source_refs=[g["panels"][0]["id"]])
    g["schedules"].append(unrelated)
    g = validate_graph(g)
    result = call(g)["graph"]
    assert [route["validity"] for route in result["routes"]] == [{"state": "stale", "reasons": ["strings_assigned"]}]
    assert result["schedules"][0]["validity"] == {"state": "stale", "reasons": ["strings_assigned"]}
    assert result["schedules"][1] == unrelated
    assert all(s["validity"] == {"state": "valid", "reasons": []} for s in result["strings"])
    assert result["inverters"][0]["validity"] == {"state": "valid", "reasons": []}
    old = {e["id"]: e for e in entities(g)}
    assert sorted(e["kind"] for e in entities(result) if old[e["id"]] != e) == [
        "frame", "inverter", "route", "schedule", "string", "string"]
    assert result["extra"]["solve_coverage"] == {'duplicate_panel_refs': [], 'unassigned_panel_refs': []}


def test_assign_strings_c11_never_fabricates_an_inverter(graph):
    empty = c11_graph(graph, [])
    before = copy.deepcopy(empty)
    with pytest.raises(GraphValidationError) as error:
        builtin().run(empty, dict(ASSIGN, expected_rev=0))
    assert error.value.code == "NO_STRING_COLLECTORS" and empty == before
    assert builtin().input_readiness(empty) == {"input_ready": False, "input_reason": "string_collectors_required"}
    g = c11_graph(graph, [2, 3, 4])
    assert sha(g) == '4d09bfedec1b3a623445e51278e60399765ebdfe1dd85c8a705a7c010f62fab4'
    answer = call(g)
    result = answer["graph"]
    assert sorted(i["number"] for i in result["inverters"]) == [2, 3, 4]
    assert all(string["inverter_ref"] is not None for string in result["strings"])
    devices = [bridge.CIRCUIT.fullmatch(string["circuit_tag"]).group("device") for string in result["strings"]]
    assert "1" not in devices
    numbers = {inverter["id"]: inverter["number"] for inverter in result["inverters"]}
    counts = {}
    for row in answer["assigned"]:
        counts[numbers[row["inverter_ref"]]] = counts.get(numbers[row["inverter_ref"]], 0) + 1
    assert counts == {2: 36, 3: 36, 4: 16}
    assert answer["lines"] == ['Pattern assignment complete. 88 strings assigned to 3 collectors in 1 seconds.']
    assert sha(result) == '4af4b688b45490b144f9b058cfbbbcd50e5f655da01aa0d36239aa71f36c7054'


# --- refusals -----------------------------------------------------------------------------------------------------


def test_assign_strings_partial_assignment_refuses(graph):
    g = variant(graph, "U")
    g["strings"][1]["route"] = [[1e200, 0], [1e200, 1]]
    g = validate_graph(g)
    before = copy.deepcopy(g)
    request = dict(ASSIGN, expected_rev=0)
    sent = copy.deepcopy(request)
    with pytest.raises(GraphValidationError, match="STRING_ASSIGNMENT_INCOMPLETE"):
        builtin().run(g, request)
    assert g == before and request == sent


def test_assign_strings_malformed_generated_letter_refuses(graph):
    g = topo.bare(graph)
    g["inverters"] = [topo.central(1, 1, [], mppt_count=27, total_dc_inputs=27,
                                   input_assignments=[{"string_ref": S1, "mppt_letter": "A", "input_number": 0}])]
    g["strings"] = [string_entity(1, [du(0, 20), du(0, 25)], "+26/1a"),
                    string_entity(2, [du(10, 20), du(10, 25)], "-")]
    g["strings"][0].update(inverter_ref=INV1, to_ref=INV1)
    g = validate_graph(g)
    before = copy.deepcopy(g)
    request = dict(ASSIGN, expected_rev=0)
    sent = copy.deepcopy(request)
    with pytest.raises(GraphValidationError, match="STRING_ASSIGNMENT_MAPPING_FAILED"):
        builtin().run(g, request)
    assert g == before and request == sent


def test_assign_strings_letter_outside_collector_refuses(graph):
    g = variant(graph, "P")
    g["settings"].update(num_mppt=2, strings_per_mppt=1)
    g["inverters"][0].update(number=2, position=[100, 0], mppt_count=1, total_dc_inputs=1)
    second = copy.deepcopy(g["inverters"][0])
    second.update(id=app_id("inverter", 2), number=1, position=[0, 0], input_assignments=[])
    g["inverters"].append(second)
    g["strings"][0]["circuit_tag"] = "+1/2a"
    g = validate_graph(g)
    before = copy.deepcopy(g)
    request = dict(ASSIGN, expected_rev=0)
    sent = copy.deepcopy(request)
    with pytest.raises(GraphValidationError, match="INVERTER_CAPACITY_EXCEEDED"):
        builtin().run(g, request)
    assert g == before and request == sent


def test_assign_strings_integral_float_hardware_counts(graph):
    integer = variant(graph, "U")
    g = copy.deepcopy(integer)
    g["inverters"][0].update(mppt_count=1.0, total_dc_inputs=2.0)
    g = validate_graph(g)
    before = copy.deepcopy(g)
    actual, expected = call(g), call(integer)
    assert short(g, actual) == short(integer, expected)
    assert actual["assigned"] == expected["assigned"]
    assert actual["collector_colours"] == expected["collector_colours"]
    assert actual["graph"]["inverters"][0]["input_assignments"] == \
        expected["graph"]["inverters"][0]["input_assignments"]
    assert g == before


def test_assign_strings_integral_float_combiner_counts(graph):
    integer = variant(graph, "CB")
    g = copy.deepcopy(integer)
    for inverter in g["inverters"]:
        if inverter.get("equipment_type") == "combiner_box":
            inverter["total_dc_inputs"] = float(inverter["total_dc_inputs"])
    g = validate_graph(g)
    before = copy.deepcopy(g)
    actual, expected = call(g), call(integer)
    assert actual["assigned"] == expected["assigned"]
    assert actual["collector_colours"] == expected["collector_colours"]
    assert actual["lines"] == expected["lines"]
    assert [i["input_assignments"] for i in actual["graph"]["inverters"]] == \
        [i["input_assignments"] for i in expected["graph"]["inverters"]]
    counts = [i["total_dc_inputs"] for i in actual["graph"]["inverters"]
              if i.get("equipment_type") == "combiner_box"]
    assert counts == [3.0, 4.0] and all(type(count) is float for count in counts)
    assert g == before
    assert only_assignment_changed(g, actual["graph"])


def four_input_graph(graph):
    g = topo.bare(graph)
    g["inverters"] = [topo.central(1, 1, [], mppt_count=2, total_dc_inputs=4)]
    g["strings"] = [string_entity(n, [du(n * 10, 20), du(n * 10, 25)], "-") for n in range(1, 5)]
    return validate_graph(g)


def test_assign_strings_inputs_fit_each_mppt(graph):
    g = four_input_graph(graph)
    before = copy.deepcopy(g)
    answer = call(g)
    assert len(answer["assigned"]) == 4
    assert [s["circuit_tag"] for s in answer["graph"]["strings"]] == \
        ["+4/1b", "+3/1b", "+2/1a", "+1/1a"]
    assert {(a["mppt_letter"], a["input_number"])
            for a in answer["graph"]["inverters"][0]["input_assignments"]} == \
        {("A", 0), ("A", 1), ("B", 0), ("B", 1)}
    assert g == before


def test_assign_strings_reassignment_to_full_mppt_refuses(graph):
    assigned = call(four_input_graph(graph))["graph"]
    state, binding = bridge.state_from_graph(assigned)
    row, = [row for row in state["rows"]["string-assignment"]
            if binding["strings"][row["string"]]["id"] == S1]
    row["_detail"]["circuit"] = bridge.UNASSIGNED
    g, _ = bridge.graph_from_state(assigned, state, binding)
    assigned = call(g)["graph"]
    assert {(a["mppt_letter"], a["input_number"])
            for a in assigned["inverters"][0]["input_assignments"]} == \
        {("A", 0), ("A", 1), ("B", 0), ("B", 1)}
    assert assigned["rev"] == g["rev"] + 1 == 2
    state, binding = bridge.state_from_graph(assigned)
    row, = [row for row in state["rows"]["string-assignment"]
            if binding["strings"][row["string"]]["id"] == S2]
    row["_detail"]["circuit"] = bridge.UNASSIGNED
    g, _ = bridge.graph_from_state(assigned, state, binding)
    before = copy.deepcopy(g)
    request = dict(ASSIGN, expected_rev=g["rev"])
    sent = copy.deepcopy(request)
    with pytest.raises(GraphValidationError, match="INVERTER_CAPACITY_EXCEEDED"):
        builtin().run(g, request)
    assert g == before and request == sent
    assert g["rev"] == before["rev"]


def test_assign_strings_writeback_circuit_mismatch_refuses(graph, monkeypatch):
    g = variant(graph, "U")
    before = copy.deepcopy(g)
    request = dict(ASSIGN, expected_rev=0)
    sent = copy.deepcopy(request)
    real = builtin().bridge.graph_from_state

    def mismatched_tag(*args, **kwargs):
        result, binding = real(*args, **kwargs)
        string = result["strings"][0]
        collector, = [i for i in result["inverters"] if i["id"] == string["inverter_ref"]]
        assert sum(a["string_ref"] == string["id"] for a in collector["input_assignments"]) == 1
        string["circuit_tag"] = "+999/1a"
        return result, binding

    monkeypatch.setattr(builtin().bridge, "graph_from_state", mismatched_tag)
    with pytest.raises(GraphValidationError, match="STRING_ASSIGNMENT_MAPPING_FAILED"):
        builtin().run(g, request)
    assert g == before and request == sent


def test_assign_strings_number_overflow_refuses_before_kernel(graph, monkeypatch):
    g = variant(graph, "P")
    g["strings"][0]["circuit_tag"] = "+1000000/1a"
    g = validate_graph(g)
    before = copy.deepcopy(g)
    request = dict(ASSIGN, expected_rev=0)
    sent = copy.deepcopy(request)

    def unexpected(*args):
        pytest.fail("overflow must refuse before calling the kernel")

    monkeypatch.setattr(builtin().kernel, "assign_strings", unexpected)
    with pytest.raises(GraphValidationError, match="STRING_NUMBER_OUT_OF_RANGE"):
        builtin().run(g, request)
    assert g == before and request == sent


def test_assign_strings_number_boundary_succeeds(graph):
    g = variant(graph, "P")
    g["strings"][0]["circuit_tag"] = "+999999/1a"
    g = validate_graph(g)
    before = copy.deepcopy(g)
    answer = call(g)
    assert short(g, answer) == [("S2", 1, "+1000000/1a", "A", 1, 256)]
    assert g == before
    assert only_assignment_changed(g, answer["graph"])


def _set(path, value):
    def patch(g):
        target = g
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    return patch


def _units_doubled(g):
    g["project"]["units"]["meters_per_unit"] *= 2


def _duplicate_number(g):
    second = copy.deepcopy(g["inverters"][0])
    second.update(id=app_id("inverter", 2), input_assignments=[])
    g["inverters"].append(second)


def _uneven_inputs(g):
    g["inverters"][0].update(mppt_count=2, total_dc_inputs=3)


def _two_letters(g):
    g["settings"].update(num_mppt=2, strings_per_mppt=1)


def _declined_total(g):
    g["inverters"][0].update(total_dc_inputs=4, mppt_count=1)


REFUSALS = [
    ("W", None, {}, "ELECTRICAL_ZONES_UNSUPPORTED"),
    ("A", None, {}, "NO_UNASSIGNED_STRINGS"),
    ("U", _units_doubled, {}, "UNRESOLVED_UNITS"),
    ("U", None, {"expected_rev": 5}, "STALE_GRAPH_REVISION"),
    ("U", _set(("strings", 0, "route"), []), {}, "STRING_ROUTE_REQUIRED"),
    ("U", _duplicate_number, {}, "BRIDGE_DUPLICATE_DEVICE"),
    ("U", _uneven_inputs, {}, "STRING_CAPACITY_UNSUPPORTED"),
    ("U", _set(("inverters", 0, "mppt_count"), 0), {}, "STRING_CAPACITY_UNSUPPORTED"),
    ("MIX", None, {}, "STRING_CAPACITY_UNSUPPORTED"),
    ("U", _set(("inverters", 0, "total_dc_inputs"), 1), {}, "STRING_CAPACITY_INSUFFICIENT"),
    ("U", _declined_total, {"accept_excess_capacity": False}, "EXCESS_CAPACITY_DECLINED"),
    ("P", _set(("strings", 0, "circuit_tag"), "+1000001/1a"), {}, "STRING_NUMBER_OUT_OF_RANGE"),
    ("U", _two_letters, {}, "INVERTER_CAPACITY_EXCEEDED"),
]


@pytest.mark.parametrize("name,patch,params,code", REFUSALS,
                         ids=[f"{row[0]}-{row[3]}-{i}" for i, row in enumerate(REFUSALS)])
def test_assign_strings_refusals_are_atomic(graph, name, patch, params, code):
    g = variant(graph, name)
    if patch is not None:
        patch(g)
    request = dict(ASSIGN, expected_rev=0)
    request.update(params)
    before, sent = copy.deepcopy(g), copy.deepcopy(request)
    with pytest.raises(GraphValidationError) as error:
        builtin().run(g, request)
    assert error.value.code == code
    assert g == before and request == sent


def test_assign_strings_repeat_has_nothing_to_assign(graph):
    once = call(variant(graph, "U"))["graph"]
    with pytest.raises(GraphValidationError, match="NO_UNASSIGNED_STRINGS"):
        call(once)


def test_assign_strings_kernel_refusals_are_named(graph, monkeypatch):
    module = builtin()
    for error, code in ((module.kernel.InverterStringNotPortedError("manual"), "STRING_ASSIGNMENT_NOT_PORTED"),
                        (module.kernel.InverterStringError("refused"), "STRING_ASSIGNMENT_MAPPING_FAILED")):
        def refuse(*args, error=error, **kwargs):
            raise error
        with monkeypatch.context() as patch:
            patch.setattr(module.kernel, "assign_strings", refuse)
            with pytest.raises(GraphValidationError) as raised:
                call(variant(graph, "U"))
            assert raised.value.code == code
    with monkeypatch.context() as patch:
        patch.setattr(module.kernel, "assign_strings", lambda state, host, forms: (state, ["surprise"]))
        with pytest.raises(GraphValidationError, match="STRING_ASSIGNMENT_MAPPING_FAILED"):
            call(variant(graph, "U"))


def test_assign_strings_host_inputs(graph, monkeypatch):
    module = builtin()
    seen = []
    real = module.kernel.assign_strings

    def spy(state, host, forms):
        seen.append((host, forms))
        return real(state, host, forms)

    with monkeypatch.context() as patch:
        patch.setattr(module.kernel, "assign_strings", spy)
        for name in ("U", "L2", "CB"):
            call(variant(graph, name))
    common = {"UseCombinerBox": False, "UsePatternStringAssignment": True, "CombinerBoxConnections": 0,
              "SessionColorCounter": 1, "DeviceNumbers": []}
    assert [host for host, _ in seen] == [
        dict(common, UseL2Collectors=False, L2NumMppt=0, L2StringsPerMppt=0, NumMppt=1, StringsPerMppt=2),
        dict(common, UseL2Collectors=True, L2NumMppt=2, L2StringsPerMppt=2, NumMppt=0, StringsPerMppt=0),
        dict(common, UseL2Collectors=True, L2NumMppt=0, L2StringsPerMppt=0, NumMppt=0, StringsPerMppt=0),
    ]
    expected_forms = {
        'excess_capacity': 'Yes',
        'assign_strings_to_inverters': 'OK',
        'assign_strings_to_inverters_mode': 'Auto',
        'assign_strings_to_combiner_boxs': 'OK',
        'assign_strings_to_combiner_boxs_mode': 'Auto',
        'assign_strings_to_central_inverters': 'OK',
        'assign_strings_to_central_inverters_mode': 'Auto',
        'assign_strings_to_l1_l2_collectors': 'OK',
        'assign_strings_to_l1_l2_collectors_mode': 'Auto',
    }
    assert [forms for _, forms in seen] == [expected_forms] * 3


@pytest.mark.parametrize("params", [
    None, [], {}, {"expected_rev": 0}, {"operation": 7, "expected_rev": 0, "accept_excess_capacity": True},
    {"operation": ["assign-strings"], "expected_rev": 0, "accept_excess_capacity": True},
    {"operation": "color-strings", "expected_rev": 0, "accept_excess_capacity": True},
    {"operation": "assign-strings", "expected_rev": 0},
    dict(ASSIGN, expected_rev=0, accept_excess_capacity="Yes"),
    dict(ASSIGN, expected_rev=0, accept_excess_capacity=1),
    dict(ASSIGN, expected_rev=0, accept_excess_capacity=None),
    dict(ASSIGN, expected_rev="0"), dict(ASSIGN, expected_rev=0.0), dict(ASSIGN, expected_rev=True),
    dict(ASSIGN, expected_rev=0, mode="Manual"), dict(ASSIGN, expected_rev=0, drawing_id="solar"),
])
def test_assign_strings_request_shape_fails_closed(graph, params):
    g = variant(graph, "U")
    before = copy.deepcopy(g)
    with pytest.raises(GraphValidationError, match="INVALID_STRING_ASSIGNMENT_REQUEST"):
        builtin().run(g, copy.deepcopy(params))
    assert g == before


# --- registry, catalog, readiness ---------------------------------------------------------------------------------

DECLARATION = {
    "schema": "leaf.solar-tool.v1", "name": TOOL, "builtin": "builtins/solar_assign_strings.py",
    "family": "stringing", "adapter": "local-graph-commit", "entitlement": "run_write",
    "requires_persisted_graph": True, "seedable": False,
    "invalid_request_code": "INVALID_STRING_ASSIGNMENT_REQUEST", "readiness": {"kind": "hook"},
    "engine": "server-builtin", "interaction": {"mode": "form"}, "record_store": "registry",
    "record": {
        "name": TOOL, "version": "1.0.0", "kind": "script", "family_id": "stringing",
        "engine_op": "solar_assign_strings", "entry": "builtins/solar_assign_strings.py",
        "params": {
            "type": "object",
            "properties": {
                "drawing_id": {"type": "string", "maxLength": 128},
                "operation": {"type": "string", "enum": ["assign-strings"]},
                "expected_rev": {"type": "integer", "minimum": 0, "maximum": 2147483647},
                "accept_excess_capacity": {"type": "boolean"},
            },
            "required": ["operation", "expected_rev", "accept_excess_capacity"],
            "additionalProperties": False,
        },
        "returns": {"type": "object"}, "capabilities": ["drawing.write"], "allow_local_fallback": False,
    },
    "ledger": ["assign-strings"], "trusted_inputs": [], "maturity": "preview", "wave": 2, "order": 82,
    "scenario": "w2-rooftop",
}


def test_assign_strings_registry_and_catalog(monkeypatch):
    declaration = solar_tools.get(TOOL)
    actual = copy.deepcopy(declaration)
    description = actual["record"].pop("description")
    assert type(description) is str and description.strip() and len(description) <= 1024
    assert actual == DECLARATION
    assert TOOL in solar_tools.local_graph_tools() and TOOL not in solar_tools.local_graph_read_tools()
    assert availability.SOLAR_CAPABILITIES[TOOL]["adapter"] == "local-graph-commit"
    assert TOOL not in availability.W1_CAPABILITIES and len(availability.W1_CAPABILITIES) == 9
    assert entitlements.tool_required_capability(solar_tools.trusted_record(TOOL)) == "run_write"
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


def test_assign_strings_params_schema():
    validator = Draft7Validator(solar_tools.trusted_record(TOOL)["params"])
    for params in (dict(ASSIGN, expected_rev=0), dict(ASSIGN, expected_rev=7, accept_excess_capacity=False),
                   dict(ASSIGN, expected_rev=2147483647, drawing_id="solar")):
        assert validator.is_valid(params), params
    for params in (ASSIGN, {"operation": "assign-strings", "expected_rev": 0},
                   dict(ASSIGN, expected_rev=-1), dict(ASSIGN, expected_rev=2147483648),
                   dict(ASSIGN, expected_rev=0, operation="color-strings"),
                   dict(ASSIGN, expected_rev=0, accept_excess_capacity="Yes"),
                   dict(ASSIGN, expected_rev=0, drawing_id="d" * 129), dict(ASSIGN, expected_rev=0, mode="Auto")):
        assert not validator.is_valid(params), params


def test_assign_strings_readiness(graph):
    assert availability.w1_graph_readiness(variant(graph, "U"))[TOOL] == {"input_ready": True, "input_reason": None}
    inputs = availability.w1_local_commit_inputs
    assert inputs(variant(graph, "W"))[TOOL] == {"input_ready": False, "input_reason": "electrical_zones_unsupported"}
    assert inputs(variant(graph, "A"))[TOOL] == {"input_ready": False, "input_reason": "unassigned_strings_required"}
    assert inputs(c11_graph(graph, []))[TOOL] == {"input_ready": False, "input_reason": "string_collectors_required"}
    assert inputs(variant(graph, "L2"))[TOOL] == {"input_ready": True, "input_reason": None}
    g = variant(graph, "U")
    _units_doubled(g)
    assert inputs(g)[TOOL] == {"input_ready": False, "input_reason": "unresolved_units"}


# --- the rail -----------------------------------------------------------------------------------------------------


@pytest.fixture
def u_api(isolated_jobs, no_network, graph, tmp_path, monkeypatch):  # noqa: F811
    backend, _ = seed(tmp_path, monkeypatch, variant(graph, "U"))
    yield from rail._api(backend, tmp_path, monkeypatch)


@pytest.fixture
def l2_api(isolated_jobs, no_network, graph, tmp_path, monkeypatch):  # noqa: F811
    backend, _ = seed(tmp_path, monkeypatch, variant(graph, "L2"))
    yield from rail._api(backend, tmp_path, monkeypatch)


def post(api, params):
    api[2][TOOL] = solar_tools.trusted_record(TOOL)
    return api[0].post("/api/run?wait=1", json=rail.body(api, TOOL, params))


def test_assign_strings_run_rail_commits_a_version(u_api, graph):
    response = post(u_api, dict(ASSIGN, expected_rev=0))
    assert response.status_code == 200, response.text
    env = response.json()
    assert env["ok"] is True and env["execution_provenance"]["execution_mode"] == "local_graph_commit"
    assert env["result"]["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
    head = head_graph(u_api[1])
    assert head == call(variant(graph, "U"))["graph"]
    assert [s["circuit_tag"] for s in head["strings"]] == ["+1/1a", "+2/1a"]
    assert store.load_manifest(u_api[1], TENANT, "solar")["head"] == 2
    records = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    assert len(records) == 1 and records[0]["status"] == "complete"


@pytest.mark.parametrize("patch", [{"accept_excess_capacity": "Yes"}, {"unknown": 1},
                                   {"operation": "color-strings"}])
def test_assign_strings_broker_refuses_schema_violations(u_api, patch):
    response = post(u_api, dict(ASSIGN, expected_rev=0, **patch))
    env = response.json()
    assert env.get("ok") is not True, response.text
    records = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    assert all(record["status"] != "complete" for record in records)
    reasons = [env.get("reason_code"), (env.get("error") or {}).get("reason_code")]
    reasons += [(record.get("error") or {}).get("reason_code") for record in records]
    assert "tool_params_invalid" in reasons, response.text
    manifest = store.load_manifest(u_api[1], TENANT, "solar")
    assert manifest["head"] == manifest["latest"] == 1


def test_assign_strings_builtin_refusal_reaches_the_rail(l2_api):
    response = post(l2_api, dict(ASSIGN, expected_rev=0, accept_excess_capacity=False))
    assert response.status_code == 400, response.text
    env = response.json()
    assert env.get("ok") is not True
    assert env["error"]["message"] == env["reason_code"] == "EXCESS_CAPACITY_DECLINED"
    assert store.load_manifest(l2_api[1], TENANT, "solar")["head"] == 1
    records = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    assert len(records) == 1 and records[0]["status"] == "failed"


def test_assign_strings_readiness_refusal_reaches_the_rail(u_api):
    first = post(u_api, dict(ASSIGN, expected_rev=0, accept_excess_capacity=False))
    assert first.status_code == 200, first.text   # exact capacity: no Excess Capacity dialog to decline
    second = post(u_api, dict(ASSIGN, expected_rev=1))
    assert second.status_code == 409, second.text
    env = second.json()
    assert env.get("ok") is not True
    assert env["reason_code"] == env["error"]["message"] == "unassigned_strings_required"
    assert env["availability"]["input_ready"] is False and env["availability"]["runnable"] is False
    assert store.load_manifest(u_api[1], TENANT, "solar")["head"] == 2
    records = [jobs.get_job(row["job_id"]) for row in jobs._query("SELECT job_id FROM jobs")]
    assert len(records) == 1 and records[0]["status"] == "complete"


def test_assign_strings_dispatch_replays_and_refuses_stale(graph, tmp_path, monkeypatch):
    backend, _ = seed(tmp_path, monkeypatch, variant(graph, "L2"))
    with held(backend) as fence:
        receipt = dispatch(backend, fence, TOOL, dict(ASSIGN, expected_rev=0))
        assert receipt["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
        assert (receipt["before_rev"], receipt["after_rev"], receipt["replayed"]) == (0, 1, False)
        again = dispatch(backend, fence, TOOL, dict(ASSIGN, expected_rev=0))
        assert again["new_version"] == receipt["new_version"] and again["replayed"] is True
        with pytest.raises(GraphValidationError, match="STALE_GRAPH_REVISION"):
            solar_local_graph.run_local_graph_commit(
                backend, TENANT, TOOL, dict(ASSIGN, expected_rev=0, drawing_id="solar"), drawing_id="solar",
                source_version=2, holder="fixture-owner", fence=fence, job_id="assign-strings-stale")
    assert latest(backend) == 2
    assert head_graph(backend) == call(variant(graph, "L2"))["graph"]
