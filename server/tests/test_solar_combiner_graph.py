"""Combiner placement on the shared design graph (sf-w2-combiners piece one): the drawing's recorded combiner
intake bound to the live graph, LEAFCOMBINERAUTO run on the route bridge's state and persisted back. C5 on the
committed i4 drawing as a graph: 14 new combiner boxes, 346 DC homerun routes, 14 feeder routes, every one of the
173 strings served exactly once, equal to the recorded kernel run; the frozen binding rules and every refusal."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
import math
from pathlib import Path
import re
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solar_design_graph import GraphValidationError, validate_graph
import solar_combiner_graph as cg
import solar_electrical_route_bridge as rb
from test_w1_design_graph import app_id, entity, graph  # noqa: F401, fixture
import test_solar_electrical_state_bridge as bt
import test_solar_ground_route_kinds as rk
import test_solar_ground_topology as topo

ct = bt.cabling_tests
cab, st = bt.cab, bt.st
CREATED, minter, canon_sha = bt.CREATED, bt.minter, bt.canon_sha
MODULE = Path(__file__).resolve().parents[1] / "solar_combiner_graph.py"
HARDWARE = {"model": "fixture-combiner", "max_dc_voltage": 1500, "max_ac_power_kw": 1}
CENTRAL = {"model": "fixture-central", "mppt_count": 6, "total_dc_inputs": 36, "max_dc_voltage": 1500,
           "max_ac_power_kw": 250, "collector_capacity": 4}
I4_GRAPH_SHA = "fb2a70ff57244ec29292cbaa9ff1febba2233e670ae1e4e3f0e3b4426d02293b"
C5_GRAPH_SHA = "2849fcbd8f80b44bbb3a11d539328ddf19eec8780206ce39071deb2d44bc83e3"


def i4_graph(w1):
    """The committed i4 drawing as a graph (test scaffolding, not the module): the W1 fixture emptied into
    L1/L2 mode, one central inverter per i4 L2 block numbered by the intake's l2Inverters (matched by insertion
    point), one string per i4 string on the L2 its circuit names (input numbers in i4 row order, the MPPT
    letter of the circuit), each route the string's i4 polyline in metres, bound by provenance.source_handle."""
    before, intake, groups = ct.committed_fixture()
    numbered = deepcopy(before)
    cab._number_l2_from_intake(numbered, intake)
    g = topo.bare(w1)
    mpu = g["project"]["units"]["meters_per_unit"]
    l2_id = {}
    for row in numbered["rows"]["device"]:
        n = row["_number"]
        x, y = row["position"]["value"]
        l2_id[n] = app_id("inverter", 200 + n)
        g["inverters"].append(entity(
            "inverter", 200 + n, number=n, type_key="A", is_l2=True, position=[x * mpu, y * mpu],
            is_solaredge=False, input_assignments=[], equipment_type="central_inverter", l1_assignments=[],
            **CENTRAL))
    by_id = {inverter["id"]: inverter for inverter in g["inverters"]}
    geometry = {item["string"]: item for item in before["geometry"]["strings"]}
    used = {}
    for k, row in enumerate(before["rows"]["string-assignment"], 1):
        match = cab._HOMERUN_CIRCUIT.fullmatch(row["label"])
        target = l2_id[int(match.group("device"))]
        letter = (match.group("mppt") or "a").upper()
        string = entity("string", k, circuit_tag=row["label"], circuit_kind="String", ordered_panel_refs=[],
                        module_count=0, from_ref=None, to_ref=target, tag_text_ref=None, wire_gauge="",
                        length_ft=0, inverter_ref=target,
                        route=[[p[0] * mpu, p[1] * mpu] for p in geometry[row["string"]]["vertices"]])
        string["provenance"]["source_handle"] = row["string"]
        g["strings"].append(string)
        taken = used.setdefault((target, letter), set())
        number = 0
        while number in taken:
            number += 1
        taken.add(number)
        by_id[target]["input_assignments"].append({"string_ref": string["id"], "mppt_letter": letter,
                                                   "input_number": number})
    return validate_graph(g), intake, groups


@pytest.fixture(scope="module")
def c5():
    """(i4 graph, intake, outline groups, placed graph, receipt): one placement shared by the read-only rows."""
    g, intake, groups = i4_graph(graph.__wrapped__())
    result, receipt = cg.place_combiners(g, intake, groups, hardware=HARDWARE, new_id=minter(), created_at=CREATED)
    return g, intake, groups, result, receipt


def fresh():
    return i4_graph(graph.__wrapped__())


def code_of(excinfo):
    return str(excinfo.value)


def place(g, intake, groups, **kwargs):
    options = {"hardware": HARDWARE, "new_id": minter(), "created_at": CREATED}
    options.update(kwargs)
    return cg.place_combiners(g, intake, groups, **options)


# ------------------------------------------------------------------ constants --

def test_combiner_graph_constants():
    assert cg.BINDING_FORMAT == "leaf.solar-combiner-binding.v1"
    assert cg.RECEIPT_FORMAT == "leaf.solar-combiner-placement.v1"
    assert cg.MATCH_EPSILON == cab.MATCH_EPSILON == 1e-6
    assert (cg.MAX_L2, cg.MAX_STRINGS, cg.MAX_NUMBER, cg.MAX_TEXT) == (1000, 20000, 1_000_000, 4096)
    assert (cg.MAX_PANELS, cg.MAX_INTAKE_DEPTH, cg.MAX_INTAKE_NODES) == (200000, 32, 12800000)
    assert cg.MAX_PANELS == rb.cab.comb.MAX_PANELS
    assert (cg.MAX_LINES, cg.MAX_LINE_CHARS, cg.SYMBOL_SCALE) == (64, 512, 1.0)
    assert cg.PLAN == {"combiner_input_plan": "Apply"}
    assert cg.HARDWARE_KEYS == frozenset({"model", "max_dc_voltage", "max_ac_power_kw"})
    assert cg.rb is rb and cg.cab is rb.cab and cg.st is rb.st and cg.comb is rb.cab.comb
    assert cg.canonical_sha256 is rb.canonical_sha256
    assert issubclass(cg.CombinerGraphError, ValueError)
    assert cg.CODES == ("COMBINER_L2_MODE_REQUIRED", "COMBINER_EXISTING_L1", "COMBINER_INTAKE_INVALID",
                        "COMBINER_INTAKE_UNITS_MISMATCH", "COMBINER_INTAKE_CONTEXT_MISMATCH",
                        "COMBINER_INTAKE_L2_MISMATCH", "COMBINER_INTAKE_STRING_MISMATCH",
                        "COMBINER_OUTLINES_INVALID", "COMBINER_HARDWARE_REQUIRED", "COMBINER_HARDWARE_INVALID",
                        "COMBINER_NOT_PORTED", "COMBINER_KERNEL_REFUSED", "COMBINER_NOTHING_PLACED",
                        "COMBINER_POSTCONDITION_FAILED")


def test_combiner_graph_codes_are_closed():
    literals = set(re.findall(r'"(COMBINER_[A-Z0-9_]+)"', MODULE.read_text(encoding="utf-8")))
    assert literals == set(cg.CODES)


# ------------------------------------------------------------------ the i4 graph --

def test_combiner_graph_i4_fixture_graph(c5):
    g, intake, groups, _, _ = c5
    assert canon_sha(g) == I4_GRAPH_SHA
    assert (len(g["inverters"]), len(g["strings"]), len(g["routes"])) == (8, 173, 0)
    assert all(i["is_l2"] and i["equipment_type"] == "central_inverter" for i in g["inverters"])
    assert sorted(i["number"] for i in g["inverters"]) == list(range(1, 9))
    assert Counter(s["inverter_ref"] for s in g["strings"]) == Counter(
        {app_id("inverter", 201): 36, app_id("inverter", 202): 36, app_id("inverter", 205): 36,
         app_id("inverter", 206): 36, app_id("inverter", 207): 29})
    assert len(groups) == 11 and intake["format"] == "combiner-intake-v1"


# ------------------------------------------------------------------ binding --

def test_combiner_graph_binds_the_c5_intake(c5):
    g, intake, _, _, _ = c5
    snapshot = deepcopy((g, intake))
    binding = cg.bind_intake(g, intake)
    assert (g, intake) == snapshot
    assert set(binding) == {"format", "graph_sha256", "intake_sha256", "l2", "strings"}
    assert binding["format"] == "leaf.solar-combiner-binding.v1"
    assert binding["graph_sha256"] == I4_GRAPH_SHA == canon_sha(g)
    assert binding["intake_sha256"] == canon_sha(intake)
    assert binding["l2"] == {str(n): app_id("inverter", 200 + n) for n in range(1, 9)}
    assert len(binding["strings"]) == 173 and len(set(binding["strings"].values())) == 173
    assert sorted(int(k) for k in binding["strings"]) == sorted(
        s["StringNumber"] for s in intake["inputs"]["preBuiltStrings"])
    assert canon_sha(binding) == "11260aaf015c5c8a82b0f56aa4c9615cb17601ebc924b3e51b1a16f109064e89"


def test_combiner_graph_binding_agrees_with_the_kernel_match(c5):
    """The module's endpoint match equals the kernel's own (cab._string_ids) on the bridge state."""
    g, intake, _, _, _ = c5
    binding = cg.bind_intake(g, intake)
    state, state_binding = rb.state_from_graph(g)
    handles = cab._string_ids(state, intake)
    assert {str(n): state_binding["strings"][h]["id"] for n, h in handles.items()} == binding["strings"]


def test_combiner_graph_intake_l2_number_is_the_engine_partition(c5):
    """The dump's per-string L2Number is not the drawing's prior assignment (68 of 173 agree), and is not checked."""
    g, intake, _, _, _ = c5
    binding = cg.bind_intake(g, intake)
    by_id = {s["id"]: s for s in g["strings"]}
    number_of = {i["id"]: i["number"] for i in g["inverters"]}
    agree = sum(number_of[by_id[binding["strings"][str(item["StringNumber"])]]["inverter_ref"]] == item["L2Number"]
                for item in intake["inputs"]["preBuiltStrings"])
    assert agree == 68


# ------------------------------------------------------------------ C5 placement --

def test_combiner_graph_c5_places_and_persists(c5):
    g, _, _, result, receipt = c5
    assert canon_sha(result) == C5_GRAPH_SHA
    assert receipt["graph_sha256_after"] == C5_GRAPH_SHA and receipt["graph_sha256_before"] == I4_GRAPH_SHA
    l1 = [i for i in result["inverters"] if not i["is_l2"]]
    assert (len(result["inverters"]), len(l1)) == (22, 14)
    assert sorted(i["number"] for i in l1) == list(range(1, 15))
    assert {(i["equipment_type"], i["mppt_count"], i["total_dc_inputs"]) for i in l1} == {("combiner_box", 1, 20)}
    assert Counter(r["route_kind"] for r in result["routes"]) == {"start homerun": 173, "end homerun": 173,
                                                                  "feeder": 14}
    assert [i["id"] for i in result["inverters"][:8]] == [i["id"] for i in g["inverters"]]
    assert [i["id"] for i in l1] == [app_id("inverter", 101 + k) for k in range(14)]


def test_combiner_graph_c5_receipt(c5):
    _, intake, _, _, receipt = c5
    assert set(receipt) == {"format", "graph_sha256_before", "graph_sha256_after", "intake_sha256", "combiners",
                            "strings_served", "l2_fed", "homerun_routes", "feeder_routes", "combiner_ids", "lines"}
    assert receipt["format"] == "leaf.solar-combiner-placement.v1"
    assert receipt["intake_sha256"] == canon_sha(intake)
    assert (receipt["combiners"], receipt["strings_served"], receipt["l2_fed"], receipt["homerun_routes"],
            receipt["feeder_routes"]) == (14, 173, 8, 346, 14)
    assert receipt["combiner_ids"] == [app_id("inverter", 101 + k) for k in range(14)]
    assert receipt["lines"][0] == ("LEAFCOMBINERAUTO: placed 14 of 14 combiner(s) for 173 strings across 8 L2 "
                                   "inverter(s). Associated 173 string(s) to L1 combiner blocks.")
    assert "HomerunsAuto: drew 346 straight-line DC homerun(s)." in receipt["lines"]
    assert "14 nearest-lane comb feeder(s) drawn." in receipt["lines"]
    assert receipt["lines"][-1] == ("LEAFCOMBINERAUTO: automatic cabling complete (346 DC homerun cable(s), "
                                    "14 feeder cable(s)).")
    assert len(receipt["lines"]) == 9


def test_combiner_graph_c5_equals_the_recorded_kernel_run(c5):
    """Same combiners, positions, feeds, slots and string association as LEAFCOMBINERAUTO on the recorded i4."""
    _, intake, groups, result, _ = c5
    before, _, _ = ct.committed_fixture()
    recorded, _ = cab.combiner_auto_place(before, groups, ct.CAPTURE, ct.PLAN, intake)
    mpu = result["project"]["units"]["meters_per_unit"]
    kernel_l1, _ = cab.levels(recorded)
    l1 = {i["number"]: i for i in result["inverters"] if not i["is_l2"]}
    assert sorted(l1) == sorted(d["number"] for d in kernel_l1)
    for d in kernel_l1:
        assert l1[d["number"]]["position"] == pytest.approx([d["position"][0] * mpu, d["position"][1] * mpu],
                                                            abs=1e-9)
    by_id = {i["id"]: i for i in result["inverters"]}
    feeds = sorted((by_id[f["inverter_ref"]]["number"], i["number"], f["mppt_index"])
                   for i in result["inverters"] if i["is_l2"] for f in i["l1_assignments"])
    assert [(a, b) for a, b, _ in feeds] == sorted((int(k), v) for k, v in
                                                   recorded["setting"]["L1ToL2Assignments"].items())
    assert [(a, c) for a, _, c in feeds] == sorted((int(k), v) for k, v in
                                                   recorded["setting"]["L1ToL2InputAssignments"].items())
    handles = cab._string_ids(recorded, intake)
    expected = {handles[int(k)]: v for k, v in recorded["setting"]["CombinerStringL1Assignments"].items()}
    assert {s["provenance"]["source_handle"]: by_id[s["inverter_ref"]]["number"] for s in result["strings"]} == expected


def test_combiner_graph_every_served_string_maps_back_once(c5):
    _, intake, _, result, _ = c5
    binding = cg.bind_intake(c5[0], intake)
    by_id = {i["id"]: i for i in result["inverters"]}
    strings = {s["id"]: s for s in result["strings"]}
    holders = Counter(a["string_ref"] for i in result["inverters"] for a in i["input_assignments"])
    assert set(holders) == set(strings) and set(holders.values()) == {1}
    assert sorted(binding["strings"].values()) == sorted(strings)
    for ident in binding["strings"].values():
        target = by_id[strings[ident]["inverter_ref"]]
        assert not target["is_l2"] and strings[ident]["to_ref"] == target["id"]
        legs = [r for r in result["routes"] if r["from_ref"] == ident]
        assert sorted(r["route_kind"] for r in legs) == ["end homerun", "start homerun"]
        assert all(r["to_ref"] == target["id"] and r["points"][-1] == pytest.approx(target["position"], abs=1e-9)
                   for r in legs)
    assert all(not i["input_assignments"] for i in result["inverters"] if i["is_l2"])
    assert {a["mppt_letter"] for i in result["inverters"] if not i["is_l2"] for a in i["input_assignments"]} == {"A"}


def test_combiner_graph_every_combiner_is_fed_once(c5):
    _, _, _, result, _ = c5
    by_id = {i["id"]: i for i in result["inverters"]}
    l1 = [i for i in result["inverters"] if not i["is_l2"]]
    fed = Counter(f["inverter_ref"] for i in result["inverters"] if i["is_l2"] for f in i["l1_assignments"])
    assert set(fed) == {i["id"] for i in l1} and set(fed.values()) == {1}
    feeders = [r for r in result["routes"] if r["route_kind"] == "feeder"]
    assert sorted(r["from_ref"] for r in feeders) == sorted(i["id"] for i in l1)
    assert all(r["to_ref"] == by_id[r["from_ref"]]["l2_ref"] and by_id[r["to_ref"]]["is_l2"] for r in feeders)
    assert sum(1 for i in result["inverters"] if i["is_l2"] and i["l1_assignments"]) == 8


def test_combiner_graph_hardware_is_supplied(c5):
    _, _, _, result, _ = c5
    l1 = [i for i in result["inverters"] if not i["is_l2"]]
    assert {(i["model"], i["max_dc_voltage"], i["max_ac_power_kw"]) for i in l1} == {("fixture-combiner", 1500, 1)}
    assert all(i["provenance"]["created_by"] == "solar-electrical-bridge" for i in l1)


def test_combiner_graph_is_deterministic_and_never_mutates(c5):
    g, intake, groups, result, receipt = c5
    snapshot = deepcopy((g, intake, groups, HARDWARE))
    again, again_receipt = place(g, intake, groups)
    assert (g, intake, groups, HARDWARE) == snapshot
    assert again == result and again_receipt == receipt


def test_combiner_graph_a_placed_graph_refuses_a_second_run(c5):
    _, intake, groups, result, _ = c5
    with pytest.raises(cg.CombinerGraphError) as excinfo:
        place(result, intake, groups)
    assert code_of(excinfo) == "COMBINER_EXISTING_L1"


def test_combiner_graph_trench_survives(c5):
    g, intake, groups, _, _ = c5
    with_trench = deepcopy(g)
    trench = rk.trench(900)
    with_trench["routes"].append(trench)
    with_trench = validate_graph(with_trench)
    result, receipt = place(with_trench, intake, groups)
    assert result["routes"][0] == trench
    assert len(result["routes"]) == 361 and receipt["homerun_routes"] == 346


def test_combiner_graph_new_ids_come_from_the_caller(c5):
    g, intake, groups, _, _ = c5
    result, _ = place(g, intake, groups, new_id=minter(5000))
    assert [i["id"] for i in result["inverters"] if not i["is_l2"]] == [app_id("inverter", 5001 + k)
                                                                        for k in range(14)]
    route_ids = [r["id"] for r in result["routes"]]
    assert route_ids == [app_id("route", 5015 + k) for k in range(360)]


# ------------------------------------------------------------------ refusals --

def _not_l2(w1, intake):
    g = deepcopy(w1)
    g["settings"]["use_l2_collectors"] = False
    return topo_strip(g), intake


def topo_strip(g):
    g = deepcopy(g)
    for key in ("electrical_zones", "frames", "panels", "strings", "inverters", "routes", "schedules"):
        g[key] = []
    return g


def _existing_l1(w1, intake):
    return topo.topology_of(w1), intake


def _intake(change):
    def build(w1, intake):
        g, _, _ = i4_graph(w1)
        changed = deepcopy(intake)
        change(changed)
        return g, changed
    return build


def _graph(change):
    def build(w1, intake):
        g, _, _ = i4_graph(w1)
        change(g)
        return validate_graph(g), intake
    return build


def _set(path, value):
    def change(d):
        target = d
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    return change


def _move_l2(g):
    g["inverters"][0]["position"] = [g["inverters"][0]["position"][0] + 1.0, g["inverters"][0]["position"][1]]


def _renumber_l2(g):
    g["inverters"][0]["number"] = 9


def _reverse_route(g):
    g["strings"][0]["route"] = list(reversed(g["strings"][0]["route"]))


def _empty_route(g):
    g["strings"][0]["route"] = []


def _extra_string(g):
    target = app_id("inverter", 207)
    extra = deepcopy(g["strings"][0])
    extra.update(id=app_id("string", 999), route=[[0.0, 0.0], [1.0, 0.0]], inverter_ref=target, to_ref=target)
    g["strings"].append(extra)
    l2 = next(i for i in g["inverters"] if i["id"] == target)
    l2["input_assignments"].append({"string_ref": extra["id"], "mppt_letter": "Z", "input_number": 0})


def _dc_inputs(g):
    for inverter in g["inverters"]:
        inverter["total_dc_inputs"] = 72


def _capacity(g):
    g["inverters"][3]["collector_capacity"] = 3


def _duplicate_string_number(d):
    d["inputs"]["preBuiltStrings"][1]["StringNumber"] = d["inputs"]["preBuiltStrings"][0]["StringNumber"]


def _drop_string(d):
    d["inputs"]["preBuiltStrings"].pop()


def _drop_l2(d):
    d["inputs"]["l2Inverters"].pop()


def _duplicate_l2(d):
    d["inputs"]["l2Inverters"][1] = deepcopy(d["inputs"]["l2Inverters"][0])


def _nan_endpoint(d):
    d["inputs"]["preBuiltStrings"][0]["endpointA"]["x"] = float("nan")


def _string_bound(d):
    first = d["inputs"]["preBuiltStrings"][0]
    d["inputs"]["preBuiltStrings"] = [dict(deepcopy(first), StringNumber=k) for k in range(cg.MAX_STRINGS + 1)]


def _l2_bound(d):
    d["inputs"]["l2Inverters"] = d["inputs"]["l2Inverters"] * 126


BIND_REFUSALS = [
    ("l2-mode-required", _not_l2, "COMBINER_L2_MODE_REQUIRED"),
    ("graph-has-l1", _existing_l1, "COMBINER_EXISTING_L1"),
    ("intake-existing-l1", _intake(_set(("inputs", "existingL1s"), [{"number": 1}])), "COMBINER_EXISTING_L1"),
    ("intake-not-object", lambda w1, intake: (i4_graph(w1)[0], [intake]), "COMBINER_INTAKE_INVALID"),
    ("intake-format", _intake(_set(("format",), "combiner-intake-v2")), "COMBINER_INTAKE_INVALID"),
    ("intake-stage", _intake(_set(("stage",), "after-placement")), "COMBINER_INTAKE_INVALID"),
    ("intake-no-inputs", _intake(_set(("inputs",), None)), "COMBINER_INTAKE_INVALID"),
    ("intake-no-l2-list", _intake(_set(("inputs", "l2Inverters"), {})), "COMBINER_INTAKE_INVALID"),
    ("intake-l2-bound", _intake(_l2_bound), "COMBINER_INTAKE_INVALID"),
    ("intake-string-bound", _intake(_string_bound), "COMBINER_INTAKE_INVALID"),
    ("intake-units-type", _intake(_set(("drawing", "metersPerUnit"), "0.0254")), "COMBINER_INTAKE_INVALID"),
    ("intake-units", _intake(_set(("drawing", "metersPerUnit"), 0.3048)), "COMBINER_INTAKE_UNITS_MISMATCH"),
    ("context-bool-mppt", _intake(_set(("commandContext", "l2NumMppt"), True)), "COMBINER_INTAKE_INVALID"),
    ("context-zero-box", _intake(_set(("commandContext", "combinerBoxConnections"), 0)), "COMBINER_INTAKE_INVALID"),
    ("context-not-l2", _intake(_set(("commandContext", "useL2Collectors"), False)),
     "COMBINER_INTAKE_CONTEXT_MISMATCH"),
    ("context-mppt", _intake(_set(("commandContext", "l2NumMppt"), 5)), "COMBINER_INTAKE_CONTEXT_MISMATCH"),
    ("context-per-mppt", _intake(_set(("commandContext", "l2StringsPerMppt"), 5)),
     "COMBINER_INTAKE_CONTEXT_MISMATCH"),
    ("context-capacity", _intake(_set(("commandContext", "l1CollectorsPerL2"), 3)),
     "COMBINER_INTAKE_CONTEXT_MISMATCH"),
    ("graph-dc-inputs", _graph(_dc_inputs), "COMBINER_INTAKE_CONTEXT_MISMATCH"),
    ("graph-capacity", _graph(_capacity), "COMBINER_INTAKE_CONTEXT_MISMATCH"),
    ("l2-moved", _graph(_move_l2), "COMBINER_INTAKE_L2_MISMATCH"),
    ("l2-renumbered", _graph(_renumber_l2), "COMBINER_INTAKE_L2_MISMATCH"),
    ("l2-dropped", _intake(_drop_l2), "COMBINER_INTAKE_L2_MISMATCH"),
    ("l2-duplicate", _intake(_duplicate_l2), "COMBINER_INTAKE_L2_MISMATCH"),
    ("l2-number-type", _intake(lambda d: d["inputs"]["l2Inverters"][0].update(Number="8")),
     "COMBINER_INTAKE_INVALID"),
    ("string-reversed", _graph(_reverse_route), "COMBINER_INTAKE_STRING_MISMATCH"),
    ("string-no-route", _graph(_empty_route), "COMBINER_INTAKE_STRING_MISMATCH"),
    ("string-extra", _graph(_extra_string), "COMBINER_INTAKE_STRING_MISMATCH"),
    ("string-dropped", _intake(_drop_string), "COMBINER_INTAKE_STRING_MISMATCH"),
    ("string-number-repeat", _intake(_duplicate_string_number), "COMBINER_INTAKE_INVALID"),
    ("string-endpoint-nan", _intake(_nan_endpoint), "COMBINER_INTAKE_INVALID"),
]


@pytest.mark.parametrize("name,build,code", BIND_REFUSALS, ids=[row[0] for row in BIND_REFUSALS])
def test_combiner_graph_binding_refusals(name, build, code, c5):
    g, intake = build(graph.__wrapped__(), c5[1])
    snapshot = deepcopy((g, intake))
    with pytest.raises(cg.CombinerGraphError) as excinfo:
        cg.bind_intake(g, intake)
    assert code_of(excinfo) == code
    with pytest.raises(cg.CombinerGraphError) as excinfo:
        place(g, intake, c5[2])
    assert code_of(excinfo) == code
    assert (g, intake) == snapshot


def test_combiner_graph_invalid_graph_propagates(c5):
    g = deepcopy(c5[0])
    g["strings"][0]["inverter_ref"] = app_id("inverter", 777)
    with pytest.raises(GraphValidationError):
        cg.bind_intake(g, c5[1])


PLACE_REFUSALS = [
    ("hardware-missing", {"hardware": None}, "COMBINER_HARDWARE_REQUIRED"),
    ("hardware-extra-key", {"hardware": dict(HARDWARE, mppt_count=1)}, "COMBINER_HARDWARE_INVALID"),
    ("hardware-empty-model", {"hardware": dict(HARDWARE, model="")}, "COMBINER_HARDWARE_INVALID"),
    ("hardware-long-model", {"hardware": dict(HARDWARE, model="m" * 4097)}, "COMBINER_HARDWARE_INVALID"),
    ("hardware-bool-voltage", {"hardware": dict(HARDWARE, max_dc_voltage=True)}, "COMBINER_HARDWARE_INVALID"),
    ("hardware-zero-power", {"hardware": dict(HARDWARE, max_ac_power_kw=0)}, "COMBINER_HARDWARE_INVALID"),
    ("hardware-huge-voltage", {"hardware": dict(HARDWARE, max_dc_voltage=1e7)}, "COMBINER_HARDWARE_INVALID"),
    ("outlines-not-list", {"groups": {"A": []}}, "COMBINER_OUTLINES_INVALID"),
    ("outlines-bad-point", {"groups": [{"handle": "A", "outlines": [[[0, "x"]]]}]}, "COMBINER_OUTLINES_INVALID"),
]


@pytest.mark.parametrize("name,change,code", PLACE_REFUSALS, ids=[row[0] for row in PLACE_REFUSALS])
def test_combiner_graph_placement_refusals(name, change, code, c5, monkeypatch):
    g, intake, groups, _, _ = c5
    change = dict(change)
    groups = change.pop("groups", groups)

    def forbidden(*args, **kwargs):
        raise AssertionError("the kernel must not run")
    monkeypatch.setattr(cab, "combiner_auto_place", forbidden)
    with pytest.raises(cg.CombinerGraphError) as excinfo:
        place(g, intake, groups, **change)
    assert code_of(excinfo) == code


def test_combiner_graph_unported_branches(c5):
    g, intake, groups, _, _ = c5
    for change in (_set(("inputs", "trackerRows"), [{"id": 1}]), _set(("commandContext", "routeHomerunsInCloud"), True)):
        changed = deepcopy(intake)
        change(changed)
        with pytest.raises(cg.CombinerGraphError) as excinfo:
            place(g, changed, groups)
        assert code_of(excinfo) == "COMBINER_NOT_PORTED"


def test_combiner_graph_kernel_refusal(c5):
    g, intake, groups, _, _ = c5
    changed = deepcopy(intake)
    changed["inputs"]["options"]["EndPolicy"] = 1
    with pytest.raises(cg.CombinerGraphError) as excinfo:
        place(g, changed, groups)
    assert code_of(excinfo) == "COMBINER_KERNEL_REFUSED"


def test_combiner_graph_finite_centroid_kernel_refusal(c5):
    g, intake, groups = deepcopy(c5[:3])
    intake["inputs"]["preBuiltStrings"][0]["centroid"]["x"] = 1e308
    snapshot = deepcopy((g, intake))
    assert len(cg.bind_intake(g, intake)["strings"]) == 173
    with pytest.raises(cg.CombinerGraphError) as excinfo:
        place(g, intake, groups)
    assert code_of(excinfo) == "COMBINER_KERNEL_REFUSED"
    assert (g, intake) == snapshot


@pytest.mark.parametrize("placement", ["depth", "budget"])
def test_combiner_graph_wide_dict_walk_is_bounded(placement, monkeypatch):
    class CountingDict(dict):
        drawn = 0

        def __iter__(self):
            for key in super().__iter__():
                self.drawn += 1
                yield key

        def items(self):
            for item in super().items():
                self.drawn += 1
                yield item

        def values(self):
            for value in super().values():
                self.drawn += 1
                yield value

    wide = CountingDict({f"probe-{k}": None for k in range(50000)})
    value = wide
    if placement == "depth":
        for _ in range(30):
            value = [value]
    else:
        monkeypatch.setattr(cg, "MAX_INTAKE_NODES", 2)
    with pytest.raises(cg.CombinerGraphError) as excinfo:
        cg._bound_intake({"probe": value})
    assert code_of(excinfo) == "COMBINER_INTAKE_INVALID"
    assert wide.drawn <= 2


def test_combiner_graph_nothing_placed(c5):
    g, intake, groups, _, _ = c5
    empty = deepcopy(g)
    empty["strings"] = []
    for inverter in empty["inverters"]:
        inverter["input_assignments"] = []
    empty = validate_graph(empty)
    changed = deepcopy(intake)
    changed["inputs"]["preBuiltStrings"] = []
    changed["inputs"]["panelGroups"] = []
    with pytest.raises(cg.CombinerGraphError) as excinfo:
        place(empty, changed, groups)
    assert code_of(excinfo) == "COMBINER_NOTHING_PLACED"


def test_combiner_graph_bridge_refusals_propagate(c5):
    g, intake, groups, _, _ = c5
    with pytest.raises(rb.ElectricalBridgeError) as excinfo:
        place(g, intake, groups, created_at="yesterday")
    assert str(excinfo.value) == "BRIDGE_INVALID_REQUEST"


def test_combiner_graph_postcondition_fails_closed(c5, monkeypatch):
    g, intake, groups, _, _ = c5
    real = cab.combiner_auto_place

    def drop_one(*args, **kwargs):
        after, lines = real(*args, **kwargs)
        served = dict(after["setting"][st.L1_SETTING])
        served.pop(sorted(served)[0])
        after["setting"][st.L1_SETTING] = served
        return after, lines
    monkeypatch.setattr(cab, "combiner_auto_place", drop_one)
    with pytest.raises(cg.CombinerGraphError) as excinfo:
        place(g, intake, groups)
    assert code_of(excinfo) == "COMBINER_POSTCONDITION_FAILED"


@pytest.mark.parametrize("name,code", [
    ("units", "COMBINER_INTAKE_INVALID"),
    ("endpoint", "COMBINER_INTAKE_INVALID"),
    ("l2-insert", "COMBINER_INTAKE_INVALID"),
    ("centroid", "COMBINER_INTAKE_INVALID"),
    ("outline", "COMBINER_OUTLINES_INVALID"),
    ("hardware-dc", "COMBINER_HARDWARE_INVALID"),
    ("hardware-ac", "COMBINER_HARDWARE_INVALID"),
], ids=["units", "endpoint", "l2-insert", "centroid", "outline", "hardware-dc", "hardware-ac"])
def test_combiner_graph_numbers_never_overflow(name, code, c5):
    g, intake, groups = deepcopy(c5[:3])
    hardware = deepcopy(HARDWARE)
    huge = 10**400
    if name == "units":
        intake["drawing"]["metersPerUnit"] = huge
    elif name == "endpoint":
        intake["inputs"]["preBuiltStrings"][0]["endpointA"]["x"] = huge
    elif name == "l2-insert":
        intake["inputs"]["l2Inverters"][0]["InsertPt"]["X"] = huge
    elif name == "centroid":
        intake["inputs"]["preBuiltStrings"][0]["centroid"]["x"] = huge
    elif name == "outline":
        stack = [(None, None, groups)]
        found = False
        while stack and not found:
            parent, key, value = stack.pop()
            if type(value) is float:
                parent[key] = huge
                found = True
            elif isinstance(value, (dict, list)):
                keys = list(value) if isinstance(value, dict) else range(len(value))
                stack.extend((value, k, value[k]) for k in reversed(keys))
        assert found
    else:
        hardware["max_dc_voltage" if name == "hardware-dc" else "max_ac_power_kw"] = huge
    snapshot = deepcopy((g, intake, groups, hardware))
    with pytest.raises(cg.CombinerGraphError) as excinfo:
        if name in ("units", "endpoint", "l2-insert"):
            cg.bind_intake(g, intake)
        else:
            place(g, intake, groups, hardware=hardware)
    assert code_of(excinfo) == code
    assert (g, intake, groups, hardware) == snapshot


@pytest.mark.parametrize("name,binds", [
    ("panels-over", False), ("row-key-over", False), ("row-key-at", True), ("key-over", False),
    ("depth-over", False), ("depth-at", True), ("nan", False), ("inf", False),
], ids=["panels-over", "row-key-over", "row-key-at", "key-over", "depth-over", "depth-at", "nan", "inf"])
def test_combiner_graph_intake_bounds_precede_hashing(name, binds, c5, monkeypatch):
    g, intake = deepcopy(c5[:2])
    options = intake["inputs"]["options"]
    if name == "panels-over":
        intake["inputs"]["cadContext"]["geometry"]["panels"] = [None] * (cg.MAX_PANELS + 1)
    elif name.startswith("row-key"):
        intake["inputs"]["preBuiltStrings"][0]["PhysicalRowKey"] = "x" * (cg.MAX_TEXT + (not binds))
    elif name == "key-over":
        options["k" * (cg.MAX_TEXT + 1)] = 1
    elif name.startswith("depth"):
        depth = cg.MAX_INTAKE_DEPTH + (not binds)
        value = None
        for _ in range(depth - 4):
            value = [value]
        options["probe"] = value
    else:
        options["probe"] = float(name)
    real = cg.canonical_sha256
    hashed = []

    def record(value):
        if value is intake:
            hashed.append(True)
        return real(value)
    monkeypatch.setattr(cg, "canonical_sha256", record)
    if binds:
        assert cg.bind_intake(g, intake)["intake_sha256"] == real(intake)
        assert hashed == [True]
    else:
        with pytest.raises(cg.CombinerGraphError) as excinfo:
            cg.bind_intake(g, intake)
        assert code_of(excinfo) == "COMBINER_INTAKE_INVALID"
        assert hashed == []


@pytest.mark.parametrize("budget,binds", [(89253, True), (89252, False)], ids=["at", "over"])
def test_combiner_graph_intake_node_budget(budget, binds, c5, monkeypatch):
    monkeypatch.setattr(cg, "MAX_INTAKE_NODES", budget)
    g, intake = c5[:2]
    if binds:
        assert len(cg.bind_intake(g, intake)["strings"]) == 173
    else:
        with pytest.raises(cg.CombinerGraphError) as excinfo:
            cg.bind_intake(g, intake)
        assert code_of(excinfo) == "COMBINER_INTAKE_INVALID"


@pytest.mark.parametrize("name", [
    "served-keys", "served-target", "holders", "l2-direct", "fed-twice", "combiner-type",
    "combiner-hardware", "leg-missing", "leg-target", "feeder-twice", "feeder-target",
])
def test_combiner_graph_proof_refuses_each_tamper(name, monkeypatch):
    real = rb.graph_from_state

    def tamper(g, after, *args, **kwargs):
        result, binding = real(g, after, *args, **kwargs)
        l1s = [i for i in result["inverters"] if not i["is_l2"]]
        l2s = [i for i in result["inverters"] if i["is_l2"]]
        if name == "served-keys":
            after["setting"][st.L1_SETTING]["99999"] = 1
        elif name == "served-target":
            result["strings"][0]["to_ref"] = l2s[0]["id"]
        elif name == "holders":
            l1s[1]["input_assignments"].append(deepcopy(l1s[0]["input_assignments"][0]))
        elif name == "l2-direct":
            l2s[0]["input_assignments"].append(l1s[0]["input_assignments"].pop(0))
        elif name == "fed-twice":
            target = next(i for i in l2s if i["l1_assignments"])
            target["l1_assignments"].append(deepcopy(target["l1_assignments"][0]))
        elif name == "combiner-type":
            l1s[0]["equipment_type"] = "central_inverter"
        elif name == "combiner-hardware":
            l1s[0]["model"] = "not-the-supplied-model"
        elif name in ("leg-missing", "leg-target"):
            index = next(k for k, r in enumerate(result["routes"]) if r["route_kind"] in rb.SEGMENT_OF)
            if name == "leg-missing":
                del result["routes"][index]
            else:
                result["routes"][index]["to_ref"] = l2s[0]["id"]
        else:
            feeder = next(r for r in result["routes"] if r["route_kind"] == "feeder")
            if name == "feeder-twice":
                result["routes"].append(deepcopy(feeder))
            else:
                feeder["to_ref"] = result["strings"][0]["id"]
        return result, binding
    monkeypatch.setattr(rb, "graph_from_state", tamper)
    g, intake, groups = fresh()
    with pytest.raises(cg.CombinerGraphError) as excinfo:
        place(g, intake, groups)
    assert code_of(excinfo) == "COMBINER_POSTCONDITION_FAILED"
