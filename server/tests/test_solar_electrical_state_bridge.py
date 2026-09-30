"""The electrical state bridge between the shared design graph's topology and the inverter kernels' G35
state: identity round trips, the recorded i5 combiner state (C5), the combiner move (C4), the positive
feeders (C10), the frozen input and slot decisions, and every refusal."""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import math
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solar_design_graph import GraphValidationError, validate_graph
import solar_electrical_state_bridge as br
from test_w1_design_graph import app_id, entity, graph  # noqa: F401, fixture
import test_solar_ground_topology as topo
import test_solar_inverter_cabling as cabling_tests

cab = cabling_tests.cab
st = cabling_tests.st

DEFAULTS = {
    "central_inverter": {"model": "fixture-central", "max_dc_voltage": 1500, "max_ac_power_kw": 250,
                         "mppt_count": 6, "total_dc_inputs": 36, "collector_capacity": 4},
    "combiner_box": {"model": "fixture-combiner", "max_dc_voltage": 1500, "max_ac_power_kw": 1},
    "string_inverter": {"model": "fixture-string", "max_dc_voltage": 600, "max_ac_power_kw": 10,
                        "mppt_count": 1, "total_dc_inputs": 2},
}
CREATED = "2026-09-29T00:00:00Z"


def canon_sha(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode("utf-8")).hexdigest()


def minter(start=100):
    """Deterministic new ids: leaf:inverter:...-000000000101, -102, ..."""
    counter = [start]

    def new_id(kind):
        counter[0] += 1
        return app_id(kind, counter[0])
    return new_id


def strings_graph(w1, handles, mode=True):
    """The W1 fixture emptied of electrical content, in L1/L2 mode or not, holding one unassigned string
    per state handle, each bound to it by provenance.source_handle."""
    g = topo.bare(w1)
    g["settings"]["use_l2_collectors"] = mode
    for n, handle in enumerate(handles, 1):
        string = entity("string", n, circuit_tag=handle, circuit_kind="String", ordered_panel_refs=[],
                        module_count=0, from_ref=None, to_ref=None, tag_text_ref=None, wire_gauge="",
                        length_ft=0, route=[], inverter_ref=None)
        string["provenance"]["source_handle"] = handle
        g["strings"].append(string)
    return g


def adopt_write(w1, state, mode=True):
    """A recorded kernel state adopted onto a strings graph and written back (every device new)."""
    g = strings_graph(w1, sorted(row["string"] for row in state["rows"]["string-assignment"]), mode)
    snapshot = deepcopy((w1, g, state, DEFAULTS))
    binding = br.adopt_state(g, state)
    bound_snapshot = deepcopy(binding)
    result = br.graph_from_state(g, state, binding, defaults=DEFAULTS, new_id=minter(), created_at=CREATED)
    assert (w1, g, state, DEFAULTS) == snapshot
    assert binding == bound_snapshot
    return result


def round_trip(g):
    state, binding = br.state_from_graph(g)
    again, rebound = br.graph_from_state(g, state, binding)
    return state, binding, again, rebound


def c5():
    before, intake, groups = cabling_tests.committed_fixture()
    after, _ = cab.combiner_auto_place(before, groups, cabling_tests.CAPTURE, cabling_tests.PLAN, intake)
    return after, intake


def levels_facts(state):
    l1, l2 = cab.levels(state)
    return sorted((False, d["number"], d["position"]) for d in l1) + sorted((True, d["number"], d["position"]) for d in l2)


def handle_of(g, ref):
    return next(s["provenance"]["source_handle"] for s in g["strings"] if s["id"] == ref)


def device(g, ref):
    return next(i for i in g["inverters"] if i["id"] == ref)


GRAPHS = [("w1", lambda w: deepcopy(w)), ("topology", topo.topology_of), ("bare", topo.bare)] + \
    [(name, (lambda b, c: lambda w: topo.build(w, b, c))(base, change)) for name, base, change, _ in topo.ADMITTED]


# ------------------------------------------------------------------ round trips --

@pytest.mark.parametrize("name,make", GRAPHS, ids=[row[0] for row in GRAPHS])
def test_electrical_bridge_round_trip_is_identity(graph, name, make):
    g = make(graph)
    before = deepcopy(g)
    state, binding, again, rebound = round_trip(g)
    assert again == g and canon_sha(again) == canon_sha(g)
    assert rebound == binding
    assert g == before


def test_electrical_bridge_w1_state_shape(graph):
    state, binding = br.state_from_graph(graph)
    assert st.validate_state(state) == state
    assert state["source"] == {"dump_sha256": canon_sha(graph), "reopened": False}
    assert [(r["string"], r["device"], r["input"], r["label"], r["_detail"]["circuit"])
            for r in state["rows"]["string-assignment"]] == [("1", 1, 1, "+1/1a", "+1/1a"), ("2", 1, 1, "+2/1a", "+2/1a")]
    (row,) = state["rows"]["device"]
    assert (row["role"], row["number"], row["_number"], row["_pair"], row["position"]["value"], row["_detail"]) == \
        ("combiner", 1, 1, "device:bridge-1", [5 / 0.0254, 0.0],
         {"type_key": "A", "is_l2": False, "box_input_count": 0, "colour": None})
    assert state["rows"]["cable"] == [] and state["rows"]["schedule"] == [] and state["rows"]["lbd"] == []
    assert state["setting"] == {"UseL2Collectors": False, "InstallationDesign": "Roof", "NumMppt": 1,
                                "StringPerMppt": 2, "L1ToL2Assignments": {}, "L1ToL2InputAssignments": {}}
    assert [g["vertices"] for g in state["geometry"]["strings"]] == [[[0.0, 0.0], [1 / 0.0254, 0.0]]] * 2
    assert binding["devices"] == {"device:bridge-1": {"id": app_id("inverter", 1), "position": [5 / 0.0254, 0.0]}}
    assert binding["strings"] == {"1": {"id": app_id("string", 1), "circuit": "+1/1a"},
                                  "2": {"id": app_id("string", 2), "circuit": "+2/1a"}}
    assert canon_sha(st.publish(state)) == "301b692f01e1abf39bdb76d0aae8471224b8fbbcc1f7a6556b93e2395127c9f0"
    assert canon_sha(binding) == "697604653c42ecec1a8d25d5ed265d72d1d97190b2757850dc0e6059d9626b18"


def test_electrical_bridge_topology_state_shape(graph):
    state, _ = br.state_from_graph(topo.topology_of(graph))
    assert [(r["role"], r["number"], r["_detail"]["is_l2"], r["_detail"]["box_input_count"])
            for r in state["rows"]["device"]] == [("combiner", 1, False, 2), ("inverter", 1, True, 0)]
    assert state["setting"]["UseL2Collectors"] is True
    assert state["setting"]["L1ToL2Assignments"] == {"1": 1}
    assert state["setting"]["L1ToL2InputAssignments"] == {"1": 0}
    assert [r["_detail"]["circuit"] for r in state["rows"]["string-assignment"]] == ["+1/1a", "+2/1a"]
    l1, l2 = cab.levels(state)
    assert [d["number"] for d in l1] == [1] and [d["number"] for d in l2] == [1]
    assert canon_sha(st.publish(state)) == "9ebbbb32df8abd9506888192c8d8c293edd20f3bdaef870000914294fade4a16"


def test_electrical_bridge_verbatim_circuit_when_it_names_the_collector(graph):
    g = deepcopy(graph)
    g["strings"][0]["circuit_tag"] = "+7/1c"
    g["strings"][1]["circuit_tag"] = "+8/2a"
    state, binding = br.state_from_graph(g)
    assert [r["_detail"]["circuit"] for r in state["rows"]["string-assignment"]] == ["+7/1c", "+2/1a"]
    again, _ = br.graph_from_state(g, state, binding)
    assert again == g


# ------------------------------------------------------------------ C5: the recorded i5 state --

def test_electrical_bridge_c5_i5_projects_onto_the_graph(graph):
    after, intake = c5()
    snapshot = deepcopy(after)
    g, binding = adopt_write(graph, after)
    assert after == snapshot
    assert canon_sha(g) == "c7844974a24942104bfdeff65b6606372cdf371b1ff85e93b2a6954f5a06814a"
    assert canon_sha(binding) == "79c81b839e06acd3ece2238ddee3a3a55bf98578520b4225644249acee6f2b98"
    inverters = g["inverters"]
    l1 = [i for i in inverters if not i["is_l2"]]
    l2 = [i for i in inverters if i["is_l2"]]
    assert (len(inverters), len(l1), len(l2)) == (22, 14, 8)
    assert {(i["equipment_type"], i["mppt_count"], i["total_dc_inputs"]) for i in l1} == {("combiner_box", 1, 20)}
    assert {(i["equipment_type"], i["mppt_count"], i["total_dc_inputs"], i["collector_capacity"]) for i in l2} == \
        {("central_inverter", 6, 36, 4)}
    assert [i["number"] for i in l1] == list(range(1, 15)) and [i["number"] for i in l2] == list(range(1, 9))
    assert [i["id"] for i in inverters] == [app_id("inverter", 101 + k) for k in range(22)]
    assert sum(len(i["input_assignments"]) for i in l1) == 173 and max(len(i["input_assignments"]) for i in l1) == 19
    assert all(not i["input_assignments"] for i in l2)
    assert {a["mppt_letter"] for i in l1 for a in i["input_assignments"]} == {"A"}
    assert all(sorted(a["input_number"] for a in i["input_assignments"]) == list(range(len(i["input_assignments"])))
               for i in l1)
    by_id = {i["id"]: i for i in inverters}
    mpu = g["project"]["units"]["meters_per_unit"]
    kernel_l1, _ = cab.levels(after)
    assert sorted((d["number"], [d["position"][0] * mpu, d["position"][1] * mpu]) for d in kernel_l1) == \
        sorted((i["number"], i["position"]) for i in l1)
    # The association equals the kernel's CombinerStringL1Assignments read through the intake's string numbers.
    handles = cab._string_ids(after, intake)
    expected = {handles[int(k)]: v for k, v in after["setting"]["CombinerStringL1Assignments"].items()}
    assert {s["provenance"]["source_handle"]: by_id[s["inverter_ref"]]["number"] for s in g["strings"]} == expected
    assert all(s["to_ref"] == s["inverter_ref"] and not by_id[s["inverter_ref"]]["is_l2"] for s in g["strings"])
    # Feeds equal L1ToL2Assignments, slots L1ToL2InputAssignments, and the 14 kernel feeder rows.
    feeds = sorted((by_id[f["inverter_ref"]]["number"], i["number"], f["mppt_index"])
                   for i in l2 for f in i["l1_assignments"])
    assert [(a, b) for a, b, _ in feeds] == sorted((int(k), v) for k, v in after["setting"]["L1ToL2Assignments"].items())
    assert [(a, c) for a, _, c in feeds] == \
        sorted((int(k), v) for k, v in after["setting"]["L1ToL2InputAssignments"].items())
    assert [(a, b) for a, b, _ in feeds] == \
        sorted((r["from"], r["to"]) for r in after["rows"]["cable"] if r["cable_kind"] == "feeder")
    l2_of = {a: b for a, b, _ in feeds}
    assert all(by_id[i["l2_ref"]]["number"] == l2_of[i["number"]] for i in l1)
    # Every one of the 346 homerun legs ends at the recorded position of its string's L1 (drawing units).
    string_l1 = {s["provenance"]["source_handle"]: s["inverter_ref"] for s in g["strings"]}
    position_of = {item["id"]: tuple(item["position"]) for item in binding["devices"].values()}
    legs = [r for r in after["rows"]["cable"] if r["cable_kind"] == "dc-homerun"]
    assert len(legs) == 346 and len(binding["devices"]) == 22
    assert all(tuple(st.point_of(r["vertices"][-1])) == position_of[string_l1[r["from"]]] for r in legs)
    # The first combiner's inputs follow the kernels' string order (ascending handle).
    first = sorted(l1[0]["input_assignments"], key=lambda a: a["input_number"])
    assert [handle_of(g, a["string_ref"]) for a in first][:5] == ["A662", "A666", "A67A", "A67E", "A682"]


def test_electrical_bridge_c5_round_trips(graph):
    after, _ = c5()
    g, _ = adopt_write(graph, after)
    state, binding, again, rebound = round_trip(g)
    assert again == g and rebound == binding
    assert sorted(item["id"] for item in binding["devices"].values()) == sorted(i["id"] for i in g["inverters"])
    assert sorted(item["id"] for item in binding["strings"].values()) == sorted(s["id"] for s in g["strings"])
    kernel, projected = levels_facts(after), levels_facts(state)
    assert [(a, b) for a, b, _ in kernel] == [(a, b) for a, b, _ in projected]
    drift = max(max(abs(p[0] - q[0]), abs(p[1] - q[1])) for (_, _, p), (_, _, q) in zip(kernel, projected))
    assert 0 < drift <= 1e-9
    assert state["setting"]["L1ToL2Assignments"] == after["setting"]["L1ToL2Assignments"]
    assert state["setting"]["L1ToL2InputAssignments"] == after["setting"]["L1ToL2InputAssignments"]
    assert canon_sha(st.publish(state)) == "b97c52e9b6fb1d36390f8ed8a8db5162e2d2e3621a86ec0d30de4192f0977448"


# ------------------------------------------------------------------ C4: the combiner move --

def test_electrical_bridge_c4_move_keeps_every_identity(graph):
    before = cabling_tests.make_state()
    before_snapshot = deepcopy(before)
    args = ["A9D5", "300,200,0"]
    host_snapshot, args_snapshot = deepcopy((cabling_tests.HOST, args))
    g, binding = adopt_write(graph, before)
    assert canon_sha(g) == "fd53f6b57be26992b85363767d9ef6e7746110ec5fcc1d13131855edaad33d79"
    after, lines = cab.inverter_move(before, cabling_tests.HOST, args)
    assert cabling_tests.HOST == host_snapshot and args == args_snapshot
    assert lines == ["Inverter 1 moved; 2 homerun(s) rerouted."]
    assert [d["number"] for d in cab.levels(after)[0]] == [2, None]     # the moved device left its feeder vertex
    snapshot = deepcopy((graph, g, after, binding, DEFAULTS))
    moved, rebound = br.graph_from_state(g, after, binding, defaults=DEFAULTS)
    assert (graph, g, after, binding, DEFAULTS) == snapshot
    assert before == before_snapshot
    assert canon_sha(moved) == "ea2f0a11b5a17e21d068f80a1f04ed724be55504c939dd3ec55bc494da8f3adc"
    assert [i["id"] for i in moved["inverters"]] == [i["id"] for i in g["inverters"]]
    changed = [(old["number"], old["is_l2"], new["position"])
               for old, new in zip(g["inverters"], moved["inverters"]) if old != new]
    assert changed == [(1, False, [7.62, 5.08])]
    assert {k for k in g if g[k] != moved[k]} == {"inverters"}
    numbers = {i["id"]: (i["number"], i["is_l2"]) for i in moved["inverters"]}
    assert {handle_of(moved, s["id"]): numbers[s["inverter_ref"]] for s in moved["strings"]} == \
        {"A01": (1, False), "A02": (2, False)}
    assert rebound["devices"]["device:2"] == {"id": app_id("inverter", 101), "position": [300.0, 200.0]}
    # The kernel facts of C4 (test_solar_inverter_cabling.py): both A01 legs end at the moved device, A02 untouched.
    legs = [r for r in after["rows"]["cable"] if r["cable_kind"] == "dc-homerun"]
    assert sorted(cabling_tests.points(r)[1] for r in legs if r["from"] == "A01") == [(300.0, 200.0)] * 2
    assert all(cabling_tests.points(r)[1] == cabling_tests.CB_2 for r in legs if r["from"] == "A02")
    assert len(after["setting"]["HomerunRouting"]["CableCatalog"]) == 4


def test_electrical_bridge_c4_bridge_state_carries_no_homeruns(graph):
    g, _ = adopt_write(graph, cabling_tests.make_state())
    state, binding = br.state_from_graph(g)
    after, lines = cab.inverter_move(state, cabling_tests.HOST, ["A9D5", "300,200,0"])
    assert lines == ["The selected inverter has no homeruns to move."]
    again, _ = br.graph_from_state(g, after, binding)
    assert again == g


# ------------------------------------------------------------------ C10: positive feeders --

def test_electrical_bridge_c10_feeders_become_l1_assignments(graph):
    before = cabling_tests.make_state(feeders=False, numbered=True, settings={"L1ToL2Assignments": {}})
    g, binding = adopt_write(graph, before)
    assert canon_sha(g) == "23bfefa21a048704057ab8952845105d4b8b16a9243c423b14c69b7104fb1d13"
    assert [(i["number"], i["is_l2"], i.get("l2_ref"), i.get("l1_assignments")) for i in g["inverters"]] == \
        [(1, False, None, None), (2, False, None, None), (1, True, None, []), (2, True, None, [])]
    after, lines = cab.route_l2_feeders(before, cabling_tests.GROUPS, cabling_tests.HOST)
    assert "2 nearest-lane comb feeder(s) drawn." in lines
    fed, _ = br.graph_from_state(g, after, binding)
    assert canon_sha(fed) == "fd53f6b57be26992b85363767d9ef6e7746110ec5fcc1d13131855edaad33d79"
    l1 = {i["number"]: i for i in fed["inverters"] if not i["is_l2"]}
    l2 = {i["number"]: i for i in fed["inverters"] if i["is_l2"]}
    assert [(n, l1[n]["l2_ref"]) for n in (1, 2)] == [(1, l2[1]["id"]), (2, l2[2]["id"])]
    assert [(n, l2[n]["l1_assignments"]) for n in (1, 2)] == \
        [(1, [{"inverter_ref": l1[1]["id"], "mppt_index": 0}]), (2, [{"inverter_ref": l1[2]["id"], "mppt_index": 0}])]
    assert "L1ToL2InputAssignments" not in after["setting"]          # the slots are the seeded default
    feeders = sorted((r for r in after["rows"]["cable"] if r["cable_kind"] == "feeder"), key=lambda r: r["from"])
    assert [(r["from"], r["to"]) for r in feeders] == [(1, 1), (2, 2)]
    assert cabling_tests.points(feeders[0]) == [cabling_tests.CB_1, (-100.0, 200.0), (-100.0, 0.0), cabling_tests.L2_1]
    assert feeders[0]["length"]["value"] == pytest.approx(500.0 / 12.0)


def test_electrical_bridge_c10_through_the_bridge_state(graph):
    before = cabling_tests.make_state(feeders=False, numbered=True, settings={"L1ToL2Assignments": {}})
    g, binding = adopt_write(graph, before)
    kernel_after, _ = cab.route_l2_feeders(before, cabling_tests.GROUPS, cabling_tests.HOST)
    direct, _ = br.graph_from_state(g, kernel_after, binding)
    state, bridged = br.state_from_graph(g)
    after, lines = cab.route_l2_feeders(state, cabling_tests.GROUPS, cabling_tests.HOST)
    assert "2 nearest-lane comb feeder(s) drawn." in lines
    assert after["setting"]["L1ToL2Assignments"] == {"1": 1, "2": 2}
    feeders = sorted((r for r in after["rows"]["cable"] if r["cable_kind"] == "feeder"), key=lambda r: r["from"])
    assert cabling_tests.points(feeders[0]) == [cabling_tests.CB_1, (-100.0, 200.0), (-100.0, 0.0), cabling_tests.L2_1]
    again, _ = br.graph_from_state(g, after, bridged)
    assert again == direct


# ------------------------------------------------------------------ frozen decisions --

def test_electrical_bridge_unchanged_strings_keep_their_inputs(graph):
    state, binding = br.state_from_graph(graph)
    for row in state["rows"]["string-assignment"]:
        if row["string"] == "1":
            row["_detail"]["circuit"] = row["label"] = "-"
    g, _ = br.graph_from_state(graph, state, binding)
    s1, s2 = g["strings"]
    assert (s1["inverter_ref"], s1["to_ref"], s1["circuit_tag"]) == (None, None, "-")
    assert (s2["inverter_ref"], s2["circuit_tag"]) == (app_id("inverter", 1), "S2")
    assert g["inverters"][0]["input_assignments"] == \
        [{"string_ref": app_id("string", 2), "mppt_letter": "A", "input_number": 1}]
    frame = g["frames"][0]
    assert [(r["panel_ref"], r["inverter_id"], r["string_input_number"]) for r in frame["panel_assignments"]] == \
        [(app_id("panel", 1), None, None), (app_id("panel", 2), None, None), (app_id("panel", 3), app_id("inverter", 1), 1)]
    assert canon_sha(g) == "bcb7d22e09bc804dfda92e826c2f60c0b28249cbcf935c01ba834488adad121b"


def test_electrical_bridge_changed_circuit_takes_the_lowest_free_input(graph):
    state, binding = br.state_from_graph(graph)
    for row in state["rows"]["string-assignment"]:
        if row["string"] == "1":
            row["_detail"]["circuit"] = row["label"] = "+9/1a"
    g, _ = br.graph_from_state(graph, state, binding)
    assert g["strings"][0]["circuit_tag"] == "+9/1a"
    assert g["inverters"][0]["input_assignments"] == [
        {"string_ref": app_id("string", 2), "mppt_letter": "A", "input_number": 1},
        {"string_ref": app_id("string", 1), "mppt_letter": "A", "input_number": 0}]

    # Both lower numbers are occupied by kept assignments: the fresh input must skip them.
    occupied = strings_graph(graph, ["1", "2", "3"], mode=False)
    inverter = deepcopy(graph["inverters"][0])
    inverter["total_dc_inputs"] = 3
    inverter["input_assignments"] = [
        {"string_ref": string["id"], "mppt_letter": "A", "input_number": n}
        for n, string in enumerate(occupied["strings"])]
    occupied["inverters"] = [inverter]
    for string in occupied["strings"]:
        string["inverter_ref"] = string["to_ref"] = inverter["id"]
    assert validate_graph(occupied) == occupied
    state, binding = br.state_from_graph(occupied)
    state["rows"]["string-assignment"][2]["_detail"]["circuit"] = "+9/1a"
    state["rows"]["string-assignment"][2]["label"] = "+9/1a"
    snapshot = deepcopy((occupied, state, binding))
    allocated, _ = br.graph_from_state(occupied, state, binding)
    assert allocated["inverters"][0]["input_assignments"] == inverter["input_assignments"]
    assert allocated["strings"][2]["circuit_tag"] == "+9/1a"
    assert (occupied, state, binding) == snapshot


def test_electrical_bridge_unrecorded_slot_takes_the_seeded_default(graph):
    g = topo.build(graph, "topology", topo._shared)
    state, binding = br.state_from_graph(g)
    assert state["setting"]["L1ToL2InputAssignments"] == {"1": 0, "2": 0}
    del state["setting"]["L1ToL2InputAssignments"]
    seeded, _ = br.graph_from_state(g, state, binding)
    assert [(f["inverter_ref"], f["mppt_index"]) for f in seeded["inverters"][1]["l1_assignments"]] == \
        [(app_id("inverter", 1), 0), (app_id("inverter", 3), 1)]


def test_electrical_bridge_engine_created_combiner_is_minted(graph):
    g = topo.topology_of(graph)
    state, binding = br.state_from_graph(g)
    state["rows"]["device"].append(_device(2, x=400.0, box=20))
    state["setting"]["L1ToL2Assignments"] = {"1": 1, "2": 1}
    snapshot = deepcopy((graph, g, state, binding, DEFAULTS))
    minted, rebound = br.graph_from_state(g, state, binding, defaults=DEFAULTS, new_id=minter(), created_at=CREATED)
    assert (graph, g, state, binding, DEFAULTS) == snapshot
    new = minted["inverters"][2]
    assert (new["id"], new["number"], new["equipment_type"], new["total_dc_inputs"], new["mppt_count"],
            new["l2_ref"], new["position"], new["model"]) == \
        (app_id("inverter", 101), 2, "combiner_box", 20, 1, app_id("inverter", 2), [400.0 * 0.0254, 0.0],
         "fixture-combiner")
    assert new["provenance"] == {"created_by": "solar-electrical-bridge", "created_at": CREATED,
                                 "last_writer": "solar-electrical-bridge", "source_rev": 0}
    assert minted["inverters"][1]["l1_assignments"] == [{"inverter_ref": app_id("inverter", 1), "mppt_index": 0},
                                                        {"inverter_ref": app_id("inverter", 101), "mppt_index": 1}]
    assert rebound["devices"]["device:new-1"] == {"id": app_id("inverter", 101), "position": [400.0, 0.0]}
    assert canon_sha(minted) == "edaa86da2df7ec1862db738bb17d437e7844de2d175449951cde24eac77000fd"


# ------------------------------------------------------------------ refusals --

def _device(number, is_l2=False, x=900.0, box=0, pair="device:new-1"):
    """A device row an engine created: fresh pair, `_number` as PlaceNewInverterBlock records it."""
    return {"number": None, "role": "inverter" if is_l2 else "combiner", "position": st.coordinate(x, 0.0),
            "scale": 1.0, "rotation": st.angle(0.0), "placement": None, "hardware": None, "_pair": pair,
            "_number": number, "_detail": {"type_key": "A", "is_l2": is_l2, "box_input_count": box, "colour": None}}


def _leg(handle, segment, end):
    return {"cable_kind": "dc-homerun", "segment": segment, "from": handle, "to": None,
            "vertices": [st.coordinate(0.0, 0.0), st.coordinate(*end)],
            "length": {"kind": "length", "value": 0.0, "unit": "ft"},
            "_detail": {"circuit": "", "gauge": "", "closed": False}}


def _w1(graph):
    state, binding = br.state_from_graph(graph)
    return deepcopy(graph), state, binding


def _t(graph):
    g = topo.topology_of(graph)
    state, binding = br.state_from_graph(g)
    return g, state, binding


def _c4(graph):
    g, _ = adopt_write(graph, cabling_tests.make_state())
    state, binding = br.state_from_graph(g)
    return g, state, binding


def _edit(base, change, **kwargs):
    def build(graph):
        g, state, binding = base(graph)
        change(g, state, binding)
        return g, state, binding, kwargs
    return build


def _set(mapping, key, value):
    mapping[key] = value


def _stale(g, state, binding):
    g["strings"][0]["wire_gauge"] = "12 AWG"


def _new_l1(g, state, binding):
    state["rows"]["device"].append(_device(2))


def _nothing(g, state, binding):
    pass


def _lower_key(g, state, binding):
    binding["strings"] = {("a" if k == "1" else k): v for k, v in binding["strings"].items()}


GRAPH_FROM_STATE_REFUSALS = [
    ("binding-stale", _edit(_w1, _stale), "BRIDGE_BINDING_STALE"),
    ("binding-format", _edit(_w1, lambda g, s, b: _set(b, "format", "leaf.solar-electrical-binding.v0")),
     "BRIDGE_BINDING_INVALID"),
    ("binding-extra-key", _edit(_w1, lambda g, s, b: _set(b, "extra", {})), "BRIDGE_BINDING_INVALID"),
    ("binding-lowercase-handle", _edit(_w1, _lower_key), "BRIDGE_BINDING_INVALID"),
    ("binding-bad-position", _edit(_w1, lambda g, s, b: _set(b["devices"]["device:bridge-1"], "position",
                                                             [1.0, float("nan")])), "BRIDGE_BINDING_INVALID"),
    ("binding-unbound-inverter", _edit(_w1, lambda g, s, b: _set(b, "devices", {})), "BRIDGE_BINDING_INVALID"),
    ("state-malformed", _edit(_w1, lambda g, s, b: _set(s, "format", "inverter-state-v0")), "BRIDGE_STATE_INVALID"),
    ("unbound-string", _edit(_w1, lambda g, s, b: s["rows"]["string-assignment"].append(
        {"string": "FF", "device": 0, "input": 0, "label": "-", "colour": None, "_detail": {"circuit": "-"}})),
     "BRIDGE_UNBOUND_STRING"),
    ("string-missing", _edit(_w1, lambda g, s, b: s["rows"]["string-assignment"].pop()), "BRIDGE_STRING_MISSING"),
    ("device-removed", _edit(_t, lambda g, s, b: s["rows"]["device"].pop()), "BRIDGE_DEVICE_REMOVED"),
    ("device-unnumbered", _edit(_w1, lambda g, s, b: s["rows"]["device"].append(_device(None))),
     "BRIDGE_DEVICE_UNNUMBERED"),
    ("duplicate-device", _edit(_w1, lambda g, s, b: s["rows"]["device"].append(_device(1))), "BRIDGE_DUPLICATE_DEVICE"),
    ("l2-in-legacy-mode", _edit(_w1, lambda g, s, b: s["rows"]["device"].append(_device(1, is_l2=True))),
     "BRIDGE_L2_MODE_REQUIRED"),
    ("feeds-in-legacy-mode", _edit(_w1, lambda g, s, b: _set(s["setting"], "L1ToL2Assignments", {"1": 1})),
     "BRIDGE_L2_MODE_REQUIRED"),
    ("feed-unresolved", _edit(_t, lambda g, s, b: _set(s["setting"], "L1ToL2Assignments", {"1": 9})),
     "BRIDGE_FEED_UNRESOLVED"),
    ("feed-map-malformed", _edit(_t, lambda g, s, b: _set(s["setting"], "L1ToL2Assignments", {"x": 1})),
     "BRIDGE_STATE_INVALID"),
    ("leg-to-nowhere", _edit(_w1, lambda g, s, b: s["rows"]["cable"].append(_leg("1", "start", (999.0, 999.0)))),
     "BRIDGE_ASSOCIATION_CONFLICT"),
    ("legs-disagree", _edit(_c4, lambda g, s, b: s["rows"]["cable"].extend(
        [_leg("1", "start", (100.0, 200.0)), _leg("1", "end", (900.0, 200.0))])), "BRIDGE_ASSOCIATION_CONFLICT"),
    ("defaults-required", _edit(_w1, _new_l1, created_at=CREATED), "BRIDGE_DEFAULTS_REQUIRED"),
    ("created-at-required", _edit(_w1, _new_l1, defaults=DEFAULTS), "BRIDGE_INVALID_REQUEST"),
    ("created-at-malformed", _edit(_w1, _new_l1, defaults=DEFAULTS, created_at="yesterday"), "BRIDGE_INVALID_REQUEST"),
    ("new-id-not-a-string", _edit(_w1, _new_l1, defaults=DEFAULTS, created_at=CREATED, new_id=lambda kind: 5),
     "BRIDGE_INVALID_REQUEST"),
    ("id-collision", _edit(_w1, _new_l1, defaults=DEFAULTS, created_at=CREATED,
                           new_id=lambda kind: app_id("inverter", 1)), "BRIDGE_ID_COLLISION"),
    ("defaults-unknown-type", _edit(_w1, _nothing, defaults={"skid": {}}), "BRIDGE_INVALID_REQUEST"),
    ("defaults-missing-key", _edit(_w1, _nothing, defaults={"central_inverter": {
        k: v for k, v in DEFAULTS["central_inverter"].items() if k != "collector_capacity"}}), "BRIDGE_INVALID_REQUEST"),
    ("defaults-bool-voltage", _edit(_w1, _nothing, defaults={"combiner_box": dict(
        DEFAULTS["combiner_box"], max_dc_voltage=True)}), "BRIDGE_INVALID_REQUEST"),
    ("defaults-negative-power", _edit(_w1, _nothing, defaults={"combiner_box": dict(
        DEFAULTS["combiner_box"], max_ac_power_kw=-1)}), "BRIDGE_INVALID_REQUEST"),
    ("defaults-model-too-long", _edit(_w1, _nothing, defaults={"combiner_box": dict(
        DEFAULTS["combiner_box"], model="m" * 4097)}), "BRIDGE_INVALID_REQUEST"),
    ("defaults-capacity-over-bound", _edit(_w1, _nothing, defaults={"central_inverter": dict(
        DEFAULTS["central_inverter"], collector_capacity=10001)}), "BRIDGE_INVALID_REQUEST"),
]


@pytest.mark.parametrize("name,build,code", GRAPH_FROM_STATE_REFUSALS, ids=[row[0] for row in GRAPH_FROM_STATE_REFUSALS])
def test_electrical_bridge_graph_from_state_refusals(graph, name, build, code):
    g, state, binding, kwargs = build(graph)
    snapshot = deepcopy((g, state, binding))
    with pytest.raises(br.ElectricalBridgeError) as error:
        br.graph_from_state(g, state, binding, **kwargs)
    assert error.value.code == code and str(error.value) == code
    assert (g, state, binding) == snapshot


def _mixed(graph):
    g, state, binding = _c4(graph)
    state["rows"]["cable"].extend([_leg("2", "start", (0.0, 0.0)), _leg("2", "end", (0.0, 0.0))])
    return g, state, binding


def _over(graph):
    g, state, binding = _t(graph)
    state["rows"]["device"][0]["_detail"]["box_input_count"] = 1
    return g, state, binding


VALIDATOR_REFUSALS = [("l2-mixed-inputs", _mixed, "L2_MIXED_INPUTS"),
                      ("combiner-over-inputs", _over, "INVERTER_CAPACITY_EXCEEDED")]


@pytest.mark.parametrize("name,build,code", VALIDATOR_REFUSALS, ids=[row[0] for row in VALIDATOR_REFUSALS])
def test_electrical_bridge_graph_validator_keeps_failing_closed(graph, name, build, code):
    g, state, binding = build(graph)
    snapshot = deepcopy((g, state, binding))
    with pytest.raises(GraphValidationError) as error:
        br.graph_from_state(g, state, binding)
    assert error.value.code == code
    assert (g, state, binding) == snapshot


def _letter(graph):
    g = deepcopy(graph)
    for a in g["inverters"][0]["input_assignments"]:
        a["mppt_letter"] = "AB"
    return g


def _twin(graph):
    g = deepcopy(graph)
    g["inverters"].append(entity("inverter", 2, number=1, type_key="A", is_l2=False, position=[9, 0], model="twin",
                                 mppt_count=1, total_dc_inputs=2, max_dc_voltage=600, max_ac_power_kw=1,
                                 is_solaredge=False, input_assignments=[]))
    return g


def _zero(graph):
    g = deepcopy(graph)
    g["inverters"][0]["number"] = 0
    return g


STATE_FROM_GRAPH_REFUSALS = [("letter-unsupported", _letter, "BRIDGE_CIRCUIT_UNSUPPORTED"),
                             ("legacy-duplicate-number", _twin, "BRIDGE_DUPLICATE_DEVICE"),
                             ("number-zero", _zero, "BRIDGE_DEVICE_UNNUMBERED")]


@pytest.mark.parametrize("name,build,code", STATE_FROM_GRAPH_REFUSALS, ids=[row[0] for row in STATE_FROM_GRAPH_REFUSALS])
def test_electrical_bridge_state_from_graph_refusals(graph, name, build, code):
    g = build(graph)
    assert validate_graph(g) == g
    snapshot = deepcopy(g)
    with pytest.raises(br.ElectricalBridgeError) as error:
        br.state_from_graph(g)
    assert error.value.code == code
    assert g == snapshot


def _no_handle(graph, state):
    g = strings_graph(graph, ["A01", "A02"])
    del g["strings"][0]["provenance"]["source_handle"]
    return g, state


def _missing_string(graph, state):
    return strings_graph(graph, ["A01"]), state


def _lower(graph, state):
    state["rows"]["string-assignment"][0]["string"] = "a01"
    return strings_graph(graph, ["a01", "A02"]), state


def _extra_inverter(graph, state):
    g, _ = adopt_write(graph, state)
    state["rows"]["device"].pop()
    return g, state


ADOPT_REFUSALS = [("source-handle-missing", _no_handle, "BRIDGE_STRING_BINDING_MISMATCH"),
                  ("state-string-unmatched", _missing_string, "BRIDGE_STRING_BINDING_MISMATCH"),
                  ("handle-not-upper-hex", _lower, "BRIDGE_STRING_BINDING_MISMATCH"),
                  ("graph-inverter-absent", _extra_inverter, "BRIDGE_DEVICE_REMOVED")]


@pytest.mark.parametrize("name,build,code", ADOPT_REFUSALS, ids=[row[0] for row in ADOPT_REFUSALS])
def test_electrical_bridge_adopt_refusals(graph, name, build, code):
    g, state = build(graph, cabling_tests.make_state())
    snapshot = deepcopy((g, state))
    with pytest.raises(br.ElectricalBridgeError) as error:
        br.adopt_state(g, state)
    assert error.value.code == code
    assert (g, state) == snapshot


def test_electrical_bridge_bounds(graph, monkeypatch):
    monkeypatch.setattr(br, "MAX_DEVICES", 1)
    with pytest.raises(br.ElectricalBridgeError) as error:
        br.state_from_graph(topo.topology_of(graph))
    assert error.value.code == "BRIDGE_BOUNDS_EXCEEDED"
    monkeypatch.setattr(br, "MAX_DEVICES", 10_000)
    monkeypatch.setattr(br, "MAX_STRINGS", 1)
    with pytest.raises(br.ElectricalBridgeError) as error:
        br.state_from_graph(graph)
    assert error.value.code == "BRIDGE_BOUNDS_EXCEEDED"


def test_electrical_bridge_refuses_direct_l2_strings_beside_l1(graph):
    g = deepcopy(graph)
    topo._direct(g)
    snapshot = deepcopy(g)
    _, _, again, _ = round_trip(g)
    assert again == g and g == snapshot
    g["inverters"].append(topo.combiner(2, 1, None))
    assert validate_graph(g) == g
    snapshot = deepcopy(g)
    with pytest.raises(br.ElectricalBridgeError) as error:
        br.state_from_graph(g)
    assert error.value.code == "BRIDGE_LEVELS_AMBIGUOUS"
    assert str(error.value) == "BRIDGE_LEVELS_AMBIGUOUS"
    assert g == snapshot


@pytest.mark.parametrize("number", [1, 2], ids=["same-number", "different-number"])
def test_electrical_bridge_new_l1_never_moves_unchanged_strings(graph, number):
    g = deepcopy(graph)
    topo._direct(g)
    state, binding = br.state_from_graph(g)
    state["rows"]["device"].append(_device(number, box=20))
    defaults = deepcopy(DEFAULTS)
    snapshot = deepcopy((graph, g, state, binding, defaults))
    with pytest.raises(br.ElectricalBridgeError) as error:
        br.graph_from_state(g, state, binding, defaults=defaults, new_id=minter(), created_at=CREATED)
    assert error.value.code == str(error.value) == "BRIDGE_LEVELS_AMBIGUOUS"
    assert (graph, g, state, binding, defaults) == snapshot


@pytest.mark.parametrize("pair", [None, 1, "", "bad/pair", "x" * 65, [], "device:bridge-1"],
                         ids=["none", "integer", "empty", "grammar", "long", "list", "duplicate"])
def test_electrical_bridge_adopt_state_checks_pairs(graph, pair):
    g = topo.topology_of(graph)
    state, _ = br.state_from_graph(g)
    for string, row in zip(g["strings"], state["rows"]["string-assignment"]):
        string["provenance"]["source_handle"] = row["string"]
    state["rows"]["device"][1]["_pair"] = pair
    snapshot = deepcopy((graph, g, state))
    with pytest.raises(br.ElectricalBridgeError) as error:
        br.adopt_state(g, state)
    assert error.value.code == str(error.value) == "BRIDGE_STATE_INVALID"
    assert (graph, g, state) == snapshot


@pytest.mark.parametrize("pair", ["device:new-1", None, 1, "", "bad/pair", "x" * 65, []],
                         ids=["duplicate", "none", "integer", "empty", "grammar", "long", "list"])
def test_electrical_bridge_new_device_pairs_are_checked(graph, pair):
    state, binding = br.state_from_graph(graph)
    state["rows"]["device"].append(_device(2, pair=pair))
    if pair == "device:new-1":
        state["rows"]["device"].append(_device(3, x=1000.0, pair=pair))
    defaults = deepcopy(DEFAULTS)
    snapshot = deepcopy((graph, state, binding, defaults))
    with pytest.raises(br.ElectricalBridgeError) as error:
        br.graph_from_state(graph, state, binding, defaults=defaults, new_id=minter(), created_at=CREATED)
    assert error.value.code == str(error.value) == "BRIDGE_STATE_INVALID"
    assert (graph, state, binding, defaults) == snapshot


def test_electrical_bridge_integral_float_numbers_round_trip(graph):
    g = topo.topology_of(graph)
    for inverter in g["inverters"]:
        inverter["number"] = 1.0
    g["inverters"][1]["l1_assignments"][0]["mppt_index"] = 0.0
    assert validate_graph(g) == g
    snapshot = deepcopy(g)
    state, binding = br.state_from_graph(g)
    assert all(type(row["_number"]) is int and type(row["number"]) is int for row in state["rows"]["device"])
    assert state["setting"]["L1ToL2InputAssignments"] == {"1": 0}
    assert type(state["setting"]["L1ToL2InputAssignments"]["1"]) is int
    state_snapshot = deepcopy((state, binding))
    again, rebound = br.graph_from_state(g, state, binding)
    assert again == g and canon_sha(again) == canon_sha(g) and rebound == binding
    assert all(type(inverter["number"]) is float for inverter in again["inverters"])
    assert type(again["inverters"][1]["l1_assignments"][0]["mppt_index"]) is float
    assert g == snapshot and (state, binding) == state_snapshot


@pytest.mark.parametrize("where,code", [("binding", "BRIDGE_BINDING_INVALID"),
                                      ("defaults", "BRIDGE_INVALID_REQUEST"),
                                      ("state", "BRIDGE_STATE_INVALID")])
def test_electrical_bridge_huge_integers_refuse_by_name(graph, where, code):
    for field in (["max_dc_voltage", "max_ac_power_kw"] if where == "defaults" else [None]):
        state, binding = br.state_from_graph(graph)
        defaults = deepcopy(DEFAULTS)
        if where == "binding":
            binding["devices"]["device:bridge-1"]["position"][0] = 10 ** 400
        elif where == "defaults":
            defaults["combiner_box"][field] = 10 ** 400
        else:
            state["rows"]["device"][0]["position"]["value"][0] = 10 ** 400
        snapshot = deepcopy((graph, state, binding, defaults))
        with pytest.raises(br.ElectricalBridgeError) as error:
            br.graph_from_state(graph, state, binding, defaults=defaults)
        assert error.value.code == str(error.value) == code
        assert (graph, state, binding, defaults) == snapshot
