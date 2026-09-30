"""The route-aware electrical bridge: the legacy equipment bridge plus the graph's homerun and feeder
routes projected both ways. Identity round trips over every legacy and route-kinds graph, the recorded
i5 combiner state (C5: 346 legs, 22 devices, 14 feeders), the combiner move (C4) that the legacy bridge
cannot reroute, the positive feeders (C10: 500/12 ft), trench and pathway preservation, the frozen gauge,
unit and drift decisions, and every refusal."""
from __future__ import annotations

from copy import deepcopy
import math
from pathlib import Path
import re
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from solar_design_graph import GraphValidationError, validate_graph
import solar_electrical_route_bridge as rb
import solar_electrical_state_bridge as br
from test_w1_design_graph import app_id, entity, graph  # noqa: F401, fixture
import test_solar_electrical_state_bridge as bt
import test_solar_ground_route_kinds as rk
import test_solar_ground_topology as topo

ct = bt.cabling_tests
cab, st = bt.cab, bt.st
DEFAULTS, CREATED, minter, canon_sha = bt.DEFAULTS, bt.CREATED, bt.minter, bt.canon_sha
MODULE = Path(__file__).resolve().parents[1] / "solar_electrical_route_bridge.py"


def adopt_write(w1, state, mode=True):
    """A recorded kernel state adopted onto a strings graph and written back: every device and route new."""
    g = bt.strings_graph(w1, sorted(row["string"] for row in state["rows"]["string-assignment"]), mode)
    snapshot = deepcopy((w1, g, state, DEFAULTS))
    binding = rb.adopt_state(g, state)
    bound = deepcopy(binding)
    result = rb.graph_from_state(g, state, binding, defaults=DEFAULTS, new_id=minter(), created_at=CREATED)
    assert (w1, g, state, DEFAULTS) == snapshot and binding == bound
    return result


def round_trip(g):
    state, binding = rb.state_from_graph(g)
    again, rebound = rb.graph_from_state(g, state, binding)
    return state, binding, again, rebound


def route_of(g, ref, kind):
    return next(r for r in g["routes"] if r["from_ref"] == ref and r["route_kind"] == kind)


def by_handle(g):
    return {s["provenance"]["source_handle"]: s for s in g["strings"]}


# Every legacy bridge graph plus the route-kinds graphs the bridge can carry (feeder-from-unconnected-l1
# is a stale feeder: see the refusals).
ROUTE_GRAPHS = [(name, (lambda b, c: lambda w: rk.build(w, b, c))(base, change))
                for name, base, change, _ in rk.ADMITTED if name != "feeder-from-unconnected-l1"]
GRAPHS = bt.GRAPHS + ROUTE_GRAPHS


@pytest.fixture(scope="module")
def i5():
    after, intake = bt.c5()
    return after, intake


# ------------------------------------------------------------------ constants --

def test_route_bridge_constants():
    assert rb.WRITER == "solar-electrical-route-bridge"
    assert (rb.MAX_ROUTES, rb.MAX_CABLE_ROWS, rb.MAX_ROUTE_VERTICES, rb.MAX_VERTICES) == \
        (100_000, 200_000, 100_000, 1_000_000)
    assert (rb.MAX_COORDINATE, rb.MAX_LENGTH_FT, rb.MAX_NUMBER) == (1e12, 1e9, 1_000_000)
    assert (rb.MAX_GAUGE, rb.MAX_CIRCUIT_CHARS, rb.MAX_CIRCUIT_DIGITS) == (4096, 64, 9)
    assert rb.SEGMENT_OF == {"start homerun": "start", "end homerun": "end"}
    assert rb.KIND_OF == {"start": "start homerun", "end": "end homerun"}
    assert rb.CODES == ("BRIDGE_ROUTE_UNSUPPORTED", "BRIDGE_ROUTE_DUPLICATE", "BRIDGE_ROUTE_TOPOLOGY_MISMATCH",
                        "BRIDGE_ROUTE_UNBOUND", "BRIDGE_ROUTE_INVALID", "BRIDGE_BOUNDS_EXCEEDED",
                        "BRIDGE_INVALID_REQUEST", "BRIDGE_ID_COLLISION", "BRIDGE_DEVICE_UNNUMBERED")
    assert rb.ElectricalBridgeError is br.ElectricalBridgeError
    assert rb.adopt_state is br.adopt_state and rb.st is br.st and rb.cab is br.cab
    assert rb.canonical_sha256 is br.canonical_sha256


def test_route_bridge_codes_are_closed():
    literals = set(re.findall(r'"(BRIDGE_[A-Z_]+)"', MODULE.read_text(encoding="utf-8")))
    assert literals == set(rb.CODES)


# ------------------------------------------------------------------ round trips --

@pytest.mark.parametrize("name,make", GRAPHS, ids=[row[0] for row in GRAPHS])
def test_route_bridge_round_trip_is_identity(graph, name, make):
    g = make(graph)
    before = deepcopy(g)
    state, binding, again, rebound = round_trip(g)
    assert again == g and canon_sha(again) == canon_sha(g)
    assert rebound == binding
    assert g == before
    assert len(state["rows"]["cable"]) == sum(1 for r in g["routes"] if r["route_kind"] != "trench")
    legacy_state, legacy_binding = br.state_from_graph(g)
    assert binding == legacy_binding
    assert {k: v for k, v in state["rows"].items() if k != "cable"} == \
        {k: v for k, v in legacy_state["rows"].items() if k != "cable"}
    assert {k: v for k, v in state.items() if k != "rows"} == {k: v for k, v in legacy_state.items() if k != "rows"}


def test_route_bridge_w1_state_shape(graph):
    state, binding = rb.state_from_graph(graph)
    assert state["rows"]["cable"] == [{
        "cable_kind": "dc-homerun", "segment": "start", "from": "1", "to": 1,
        "vertices": [st.coordinate(0.0, 0.0), st.coordinate(5 / 0.0254, 0.0)],
        "length": {"kind": "length", "value": 16.4042, "unit": "ft"}, "_pair": "cable:bridge-1",
        "_detail": {"circuit": "+1/1a", "gauge": "10 AWG", "closed": False}}]
    assert binding == br.state_from_graph(graph)[1]
    assert canon_sha(st.publish(state)) == "2def751772407d5705d3ef485133906cba8785c4dd739e35afeea52f7ff262a3"


def test_route_bridge_composite_state_shape(graph):
    g = rk.composite(graph)
    state, _ = rb.state_from_graph(g)
    cables = state["rows"]["cable"]
    assert [(c["cable_kind"], c.get("segment"), c["from"], c["to"], c["_pair"], c["_detail"]) for c in cables] == [
        ("dc-homerun", "start", "1", 1, "cable:bridge-1", {"circuit": "+1/1a", "gauge": "10 AWG", "closed": False}),
        ("feeder", None, 1, 1, "cable:bridge-2", {"circuit": "F1/1", "gauge": "", "closed": False})]
    feeder = rk.route(g, rk.FEEDER_ID)
    assert [st.point_of(v) for v in cables[1]["vertices"]] == [[p[0] / 0.0254, p[1] / 0.0254] for p in feeder["points"]]
    assert cables[1]["length"] == {"kind": "length", "value": feeder["length_ft"], "unit": "ft"}
    assert canon_sha(st.publish(state)) == "150dc5b4e6436e2f64a84c52bf3334754f4b82085480bc956fa6aa69c3092795"


# ------------------------------------------------------------------ C5: the recorded i5 state --

def test_route_bridge_c5_i5_projects_routes(graph, i5):
    after, intake = i5
    snapshot = deepcopy(after)
    g, binding = adopt_write(graph, after)
    assert after == snapshot
    assert canon_sha(g) == "f3d0c2e8485d6d1c5261ac7f6d7d792336bdf642d485f171cbe9e137d5679faa"
    assert canon_sha(binding) == "ae504cf292a87d05563ba95e59045715f764087b2370dc64bcf3f94b25f27ef3"
    # The equipment is exactly the legacy bridge's C5 projection (test_solar_electrical_state_bridge.py).
    legacy_g, legacy_binding = bt.adopt_write(graph, after)
    assert dict(g, routes=[]) == legacy_g and canon_sha(legacy_g) == \
        "c7844974a24942104bfdeff65b6606372cdf371b1ff85e93b2a6954f5a06814a"
    assert {k: v for k, v in binding.items() if k != "graph_sha256"} == \
        {k: v for k, v in legacy_binding.items() if k != "graph_sha256"}
    assert binding["graph_sha256"] == canon_sha(g)
    kinds = [r["route_kind"] for r in g["routes"]]
    assert (kinds.count("start homerun"), kinds.count("end homerun"), kinds.count("feeder"), len(g["inverters"])) == \
        (173, 173, 14, 22)
    assert len(binding["devices"]) == 22
    # New routes follow the kernels' cable order and take ids 123..482 after the 22 minted inverters.
    assert [r["id"] for r in g["routes"]] == [app_id("route", 123 + k) for k in range(360)]
    assert kinds == ["end homerun", "start homerun"] * 173 + ["feeder"] * 14
    mpu = g["project"]["units"]["meters_per_unit"]
    strings = {s["id"]: s for s in g["strings"]}
    inverters = {i["id"]: i for i in g["inverters"]}
    legs = {(r["from"], r["segment"]): r for r in after["rows"]["cable"] if r["cable_kind"] == "dc-homerun"}
    assert len(legs) == 346
    for route in g["routes"][:346]:
        string = strings[route["from_ref"]]
        leg = legs[(string["provenance"]["source_handle"], rb.SEGMENT_OF[route["route_kind"]])]
        assert route["to_ref"] == string["inverter_ref"] and not inverters[route["to_ref"]]["is_l2"]
        assert route["points"][-1] == inverters[route["to_ref"]]["position"]
        assert route["points"] == [[x * mpu, y * mpu] for x, y in (st.point_of(v) for v in leg["vertices"])]
        assert route["length_ft"] == leg["length"]["value"] and route["wire_gauge"] == ""
        assert route["provenance"] == {"created_by": rb.WRITER, "created_at": CREATED, "last_writer": rb.WRITER,
                                       "source_rev": 0}
    feeders = {r["from"]: r for r in after["rows"]["cable"] if r["cable_kind"] == "feeder"}
    for route in g["routes"][346:]:
        source, target = inverters[route["from_ref"]], inverters[route["to_ref"]]
        row = feeders[source["number"]]
        assert (row["to"], source["l2_ref"], target["equipment_type"]) == (target["number"], target["id"], "central_inverter")
        assert route["length_ft"] == row["length"]["value"]
        assert route["points"] == [[x * mpu, y * mpu] for x, y in (st.point_of(v) for v in row["vertices"])]
    assert sorted(inverters[r["from_ref"]]["number"] for r in g["routes"][346:]) == list(range(1, 15))


def test_route_bridge_c5_round_trips(graph, i5):
    after, _ = i5
    g, _ = adopt_write(graph, after)
    state, binding, again, rebound = round_trip(g)
    assert again == g and rebound == binding
    strings = {s["id"]: s for s in g["strings"]}
    source = {handle: strings[item["id"]]["provenance"]["source_handle"] for handle, item in binding["strings"].items()}
    kernel = {(r["cable_kind"], r["from"], r.get("segment")): r for r in after["rows"]["cable"]}
    projected = {(r["cable_kind"], source.get(r["from"], r["from"]), r.get("segment")): r for r in state["rows"]["cable"]}
    assert set(kernel) == set(projected) and len(projected) == 360
    drift = 0.0
    for key, row in kernel.items():
        mine = projected[key]
        assert mine["length"] == row["length"]
        if key[0] == "feeder":
            assert (mine["from"], mine["to"], mine["_detail"]["circuit"]) == (row["from"], row["to"], row["_detail"]["circuit"])
        for p, q in zip(row["vertices"], mine["vertices"]):
            drift = max(drift, abs(p["value"][0] - q["value"][0]), abs(p["value"][1] - q["value"][1]))
    assert 0 < drift <= 1e-9
    assert canon_sha(st.publish(state)) == "ac4aad8930a6288c12506657c59fa05a3dfc325adadd53bd82eef332cad40a77"


# ------------------------------------------------------------------ C4: the combiner move --

def test_route_bridge_c4_move_reroutes_homeruns(graph):
    g, _ = adopt_write(graph, ct.make_state())
    assert canon_sha(g) == "ec314e216b589f2b3b8e18abf563c31136f626a43e50634f58b39fc0c025dd98"
    assert [(r["route_kind"], r["wire_gauge"], r["length_ft"]) for r in g["routes"]] == \
        [("end homerun", "14 AWG", 17.17960677340692), ("start homerun", "14 AWG", 17.17960677340692)] * 2 + \
        [("feeder", "NA", 30.0)] * 2
    state, binding = rb.state_from_graph(g)
    snapshot = deepcopy((graph, g, state, binding))
    after, lines = cab.inverter_move(state, ct.HOST, ["A9D5", "300,200,0"])
    assert lines == ["Inverter 1 moved; 2 homerun(s) rerouted."]
    moved, rebound = rb.graph_from_state(g, after, binding)
    assert (graph, g, state, binding) == snapshot
    assert canon_sha(moved) == "47b615ca05f223ddb7b3156cdc8250e841f8f88bafedce458407a6088598e1a7"
    assert canon_sha(rebound) == "f7d9865606a702ad0c67092c8ad4dd049ff19a21fb2b32caca1527df3b93d5c7"
    assert {k for k in g if g[k] != moved[k]} == {"inverters", "routes"}
    # The equipment write is the legacy bridge's, exactly.
    assert moved["inverters"] == br.graph_from_state(g, after, binding)[0]["inverters"]
    assert [i["position"] for i in moved["inverters"] if not i["is_l2"]] == [[7.62, 5.08], [22.86, 5.08]]
    assert [r["id"] for r in moved["routes"]] == [r["id"] for r in g["routes"]]
    a01 = by_handle(g)["A01"]["id"]
    changed = [(old["route_kind"], old["from_ref"], new["points"], new["length_ft"], new["wire_gauge"])
               for old, new in zip(g["routes"], moved["routes"]) if old != new]
    assert changed == [("end homerun", a01, [[3.81, 10.16], [7.62, 5.08]], 250.0 / 12.0, "14 AWG"),
                       ("start homerun", a01, [[1.27, 10.16], [7.62, 5.08]], math.dist((50, 400), (300, 200)) / 12.0,
                        "14 AWG")]
    # The feeders stay where the kernel left them (MOVEINV does not redraw feeders).
    assert moved["routes"][4:] == g["routes"][4:]
    assert rebound["devices"]["device:bridge-1"]["position"] == [300.0, 200.0]


def test_route_bridge_retargeted_homeruns_follow_their_string(graph):
    g, _ = adopt_write(graph, ct.make_state())
    state, binding = rb.state_from_graph(g)
    a01 = by_handle(g)["A01"]["id"]
    handle = next(h for h, item in binding["strings"].items() if item["id"] == a01)
    for row in state["rows"]["cable"]:
        if row["cable_kind"] == "dc-homerun" and row["from"] == handle:
            row["vertices"][-1] = st.coordinate(900.0, 200.0)
    moved, _ = rb.graph_from_state(g, state, binding)
    assert canon_sha(moved) == "47ff24d34d91c239c6ef4012c0d5152d8943824a7ad993b68a149fb07f718b17"
    cb2 = next(i["id"] for i in moved["inverters"] if not i["is_l2"] and i["number"] == 2)
    assert by_handle(moved)["A01"]["inverter_ref"] == cb2
    # to_ref follows the legacy association, the moved end is converted, the kept start and the stored
    # length are untouched (the bridge never recomputes a length from points).
    assert [(r["route_kind"], r["to_ref"], r["points"], r["length_ft"]) for r in moved["routes"] if r["from_ref"] == a01] == \
        [("end homerun", cb2, [[3.81, 10.16], [22.86, 5.08]], 17.17960677340692),
         ("start homerun", cb2, [[1.27, 10.16], [22.86, 5.08]], 17.17960677340692)]
    assert [r["id"] for r in moved["routes"]] == [r["id"] for r in g["routes"]]
    assert rb.state_from_graph(moved)[0]["rows"]["cable"]


def test_route_bridge_c4_legacy_state_still_carries_no_homeruns(graph):
    g, _ = adopt_write(graph, ct.make_state())
    legacy_state, _ = br.state_from_graph(g)
    assert legacy_state["rows"]["cable"] == []
    _, lines = cab.inverter_move(legacy_state, ct.HOST, ["A9D5", "300,200,0"])
    assert lines == ["The selected inverter has no homeruns to move."]
    state, _ = rb.state_from_graph(g)
    assert [(c["cable_kind"], c["_detail"]["gauge"]) for c in state["rows"]["cable"]] == \
        [("dc-homerun", "14 AWG")] * 4 + [("feeder", "NA")] * 2


# ------------------------------------------------------------------ C10: positive feeders --

def _c10(graph):
    before = ct.make_state(feeders=False, numbered=True, settings={"L1ToL2Assignments": {}})
    g, binding = adopt_write(graph, before)
    return before, g, binding


def test_route_bridge_c10_feeders_become_routes(graph):
    before, g, binding = _c10(graph)
    assert canon_sha(g) == "4ce106a4fa6ca94481fa7d1ae99c4e77f7408b4f872f77ded21e662e74890570"
    assert [r["route_kind"] for r in g["routes"]] == ["end homerun", "start homerun"] * 2
    after, lines = cab.route_l2_feeders(before, ct.GROUPS, ct.HOST)
    assert "2 nearest-lane comb feeder(s) drawn." in lines
    fed, _ = rb.graph_from_state(g, after, binding, new_id=minter(200), created_at=CREATED)
    assert canon_sha(fed) == "e8cd97b7fbbbdddc85bc421fd643d01b5c2abcbe4dc9dc1bfe7e351b0cc45a32"
    assert fed["routes"][:4] == g["routes"]
    l1 = {i["number"]: i for i in fed["inverters"] if not i["is_l2"]}
    l2 = {i["number"]: i for i in fed["inverters"] if i["is_l2"]}
    assert [(r["id"], r["from_ref"], r["to_ref"], r["points"], r["length_ft"], r["wire_gauge"]) for r in fed["routes"][4:]] == [
        (app_id("route", 201), l1[1]["id"], l2[1]["id"], [[2.54, 5.08], [-2.54, 5.08], [-2.54, 0.0], [0.0, 0.0]],
         500.0 / 12.0, ""),
        (app_id("route", 202), l1[2]["id"], l2[2]["id"],
         [[22.86, 5.08], [27.939999999999998, 5.08], [27.939999999999998, 0.0], [25.4, 0.0]], 500.0 / 12.0, "")]
    assert fed["routes"][4]["length_ft"] == pytest.approx(500.0 / 12.0) and 500.0 / 12.0 == 41.666666666666664
    assert [l1[n]["l2_ref"] for n in (1, 2)] == [l2[1]["id"], l2[2]["id"]]


def test_route_bridge_c10_through_the_bridge_state(graph):
    before, g, binding = _c10(graph)
    kernel_after, _ = cab.route_l2_feeders(before, ct.GROUPS, ct.HOST)
    direct, _ = rb.graph_from_state(g, kernel_after, binding, new_id=minter(200), created_at=CREATED)
    state, bridged = rb.state_from_graph(g)
    after, lines = cab.route_l2_feeders(state, ct.GROUPS, ct.HOST)
    assert "2 nearest-lane comb feeder(s) drawn." in lines
    again, _ = rb.graph_from_state(g, after, bridged, new_id=minter(200), created_at=CREATED)
    assert again == direct
    # The bridge state of the fed graph carries both feeders; the kernel adopts them and draws none.
    fed_state, fed_binding = rb.state_from_graph(direct)
    adopted, lines = cab.route_l2_feeders(fed_state, ct.GROUPS, ct.HOST)
    assert "0 nearest-lane comb feeder(s) drawn." in lines
    same, rebound = rb.graph_from_state(direct, adopted, fed_binding)
    assert same == direct and rebound == fed_binding


# ------------------------------------------------------------------ trenches, pathways, gauges, units --

def _move_l1(g):
    state, binding = rb.state_from_graph(g)
    host = dict(ct.HOST, MovedDevice=["L1", 1])
    after, lines = cab.inverter_move(state, host, ["X", "300,200,0"])
    return state, binding, after, lines


def test_route_bridge_trench_and_pathways_survive_a_move(graph):
    g = rk.composite(graph)
    state, binding, after, lines = _move_l1(g)
    assert lines == ["Inverter 1 moved; 1 homerun(s) rerouted."]
    moved, _ = rb.graph_from_state(g, after, binding)
    homerun, feeder, trench = (rk.route(moved, ident) for ident in (rk.HOMERUN_ID, rk.FEEDER_ID, rk.TRENCH_ID))
    assert trench == rk.route(g, rk.TRENCH_ID)
    assert feeder == rk.route(g, rk.FEEDER_ID) and feeder["pathway_ref"] == rk.TRENCH_ID
    assert rk.route(g, rk.HOMERUN_ID)["pathway_ref"] == rk.TRENCH_ID
    assert (homerun["points"], homerun["pathway_ref"], homerun["length_ft"]) == \
        ([[0, 0], [7.62, 5.08]], None, math.dist((0.0, 0.0), (300.0, 200.0)) / 12.0)
    assert [r["id"] for r in moved["routes"]] == [r["id"] for r in g["routes"]]
    # A string riding a trench keeps its pathway through the legacy write-back.
    s = rk.string_on_trench(graph)
    _, _, again, _ = round_trip(s)
    assert again == s and again["strings"][0]["pathway_ref"] == rk.TRENCH_ID
    _, s_binding, s_after, _ = _move_l1(s)
    s_moved, _ = rb.graph_from_state(s, s_after, s_binding)
    assert s_moved["strings"][0]["pathway_ref"] == rk.TRENCH_ID and rk.route(s_moved, rk.TRENCH_ID) == rk.route(s, rk.TRENCH_ID)


def test_route_bridge_gauge_rules(graph):
    state, binding = rb.state_from_graph(graph)
    # A non-empty row gauge replaces the route's; nothing else changes.
    edited = deepcopy(state)
    edited["rows"]["cable"][0]["_detail"]["gauge"] = "12 AWG"
    g, _ = rb.graph_from_state(graph, edited, binding)
    assert g["routes"][0]["wire_gauge"] == "12 AWG" and dict(g["routes"][0], wire_gauge="10 AWG") == graph["routes"][0]
    # An empty row gauge keeps it.
    edited["rows"]["cable"][0]["_detail"]["gauge"] = ""
    assert rb.graph_from_state(graph, edited, binding)[0] == graph
    # A new homerun without a gauge takes its string's conductor, a fresh id and the writer's provenance.
    fresh = deepcopy(state)
    leg = deepcopy(fresh["rows"]["cable"][0])
    leg.update(segment="end", vertices=[st.coordinate(1 / 0.0254, 0.0), st.coordinate(5 / 0.0254, 0.0)],
               length={"kind": "length", "value": 13.123, "unit": "ft"})
    leg["_detail"]["gauge"] = ""
    fresh["rows"]["cable"].append(leg)
    g, rebound = rb.graph_from_state(graph, fresh, binding, new_id=minter(), created_at=CREATED)
    assert g["routes"][0] == graph["routes"][0]
    assert g["routes"][1] == {
        "id": app_id("route", 101), "kind": "route", "rev": 0,
        "provenance": {"created_by": rb.WRITER, "created_at": CREATED, "last_writer": rb.WRITER, "source_rev": 0},
        "extra": {}, "validity": {"state": "valid", "reasons": []}, "route_kind": "end homerun",
        "points": [[1.0, 0.0], [5.0, 0.0]], "from_ref": app_id("string", 1),
        "to_ref": app_id("inverter", 1), "wire_gauge": "10 AWG", "length_ft": 13.123, "point_units": "m",
        "length_units": "ft"}
    assert rebound["graph_sha256"] == canon_sha(g)


def test_route_bridge_removed_rows_remove_routes(graph):
    state, binding = rb.state_from_graph(graph)
    state["rows"]["cable"] = []
    g, rebound = rb.graph_from_state(graph, state, binding)
    assert g["routes"] == [] and dict(g, routes=graph["routes"]) == graph and rebound["graph_sha256"] == canon_sha(g)
    c = rk.composite(graph)
    state, binding = rb.state_from_graph(c)
    state["rows"]["cable"] = [row for row in state["rows"]["cable"] if row["cable_kind"] == "feeder"]
    g, _ = rb.graph_from_state(c, state, binding)
    assert [r["id"] for r in g["routes"]] == [rk.FEEDER_ID, rk.TRENCH_ID]
    assert g["routes"] == [rk.route(c, rk.FEEDER_ID), rk.route(c, rk.TRENCH_ID)]


def test_route_bridge_units_are_separate_from_length(graph):
    g = deepcopy(graph)
    g["project"]["units"].update(drawing_units="ft", meters_per_unit=0.3048, drawing_unit_is_feet=True)
    g["routes"][0]["points"] = [[0, 0, 0.0], [5, 0, 0.0]]
    assert validate_graph(g) == g
    state, binding, again, _ = round_trip(g)
    assert again == g
    row = state["rows"]["cable"][0]
    assert [st.point_of(v) for v in row["vertices"]] == [[0.0, 0.0], [5 / 0.3048, 0.0]]
    assert row["length"] == {"kind": "length", "value": 16.4042, "unit": "ft"}
    # A new length is stored as the row says; the unchanged points are kept exactly, z included.
    row["length"]["value"] = 123.5
    written, _ = rb.graph_from_state(g, state, binding)
    assert written["routes"][0]["length_ft"] == 123.5 and written["routes"][0]["points"] == [[0, 0, 0.0], [5, 0, 0.0]]
    # A moved first vertex becomes [x_du * mpu, y_du * mpu]; the untouched end keeps its z and its type.
    row["vertices"][0] = st.coordinate(1.0, 2.0)
    written, _ = rb.graph_from_state(g, state, binding)
    assert written["routes"][0]["points"] == [[0.3048, 0.6096], [5, 0, 0.0]]
    assert written["routes"][0]["length_ft"] == 123.5


# ------------------------------------------------------------------ refusals --

def _w1(graph):
    return deepcopy(graph)


def _second_inverter(g):
    g["inverters"].append(entity("inverter", 2, number=2, type_key="A", is_l2=False, position=[9, 0], model="two",
                                 mppt_count=1, total_dc_inputs=2, max_dc_voltage=600, max_ac_power_kw=1,
                                 is_solaredge=False, input_assignments=[]))


def _g(change, base=_w1):
    def build(graph):
        g = base(graph)
        change(g)
        return g
    return build


def _route0(key, value):
    return _g(lambda g: g["routes"][0].__setitem__(key, deepcopy(value)))


def _duplicate(g):
    twin = deepcopy(g["routes"][0])
    twin["id"] = app_id("route", 9)
    g["routes"].append(twin)


STATE_FROM_GRAPH_ROUTE_REFUSALS = [
    ("homerun-from-panel", _route0("from_ref", app_id("panel", 1)), "BRIDGE_ROUTE_UNSUPPORTED"),
    ("homerun-from-null", _route0("from_ref", None), "BRIDGE_ROUTE_UNSUPPORTED"),
    ("homerun-to-string", _route0("to_ref", app_id("string", 2)), "BRIDGE_ROUTE_UNSUPPORTED"),
    ("homerun-one-point", _route0("points", [[0, 0]]), "BRIDGE_ROUTE_UNSUPPORTED"),
    ("homerun-no-points", _route0("points", []), "BRIDGE_ROUTE_UNSUPPORTED"),
    ("homerun-length-over-bound", _route0("length_ft", 1e10), "BRIDGE_ROUTE_UNSUPPORTED"),
    ("homerun-duplicate", _g(_duplicate), "BRIDGE_ROUTE_DUPLICATE"),
    ("homerun-to-other-inverter", _g(lambda g: (_second_inverter(g), g["routes"][0].__setitem__(
        "to_ref", app_id("inverter", 2)))), "BRIDGE_ROUTE_TOPOLOGY_MISMATCH"),
    ("homerun-detached", _route0("points", [[0, 0], [4, 0]]), "BRIDGE_ROUTE_TOPOLOGY_MISMATCH"),
    ("feeder-left-behind", lambda graph: rk.build(graph, "topology", lambda g: rk._reassigned(g, False)),
     "BRIDGE_ROUTE_TOPOLOGY_MISMATCH"),
    ("feeder-from-unconnected-l1", lambda graph: rk.build(graph, "topology", rk._unconnected_with_feeder),
     "BRIDGE_ROUTE_TOPOLOGY_MISMATCH"),
]


@pytest.mark.parametrize("name,build,code", STATE_FROM_GRAPH_ROUTE_REFUSALS,
                         ids=[row[0] for row in STATE_FROM_GRAPH_ROUTE_REFUSALS])
def test_route_bridge_state_from_graph_refusals(graph, name, build, code):
    g = build(graph)
    assert validate_graph(g) == g
    snapshot = deepcopy(g)
    for call in (lambda: rb.state_from_graph(g),
                 lambda: rb.graph_from_state(g, *br.state_from_graph(g), new_id=minter(), created_at=CREATED)):
        with pytest.raises(rb.ElectricalBridgeError) as error:
            call()
        assert error.value.code == str(error.value) == code
    assert g == snapshot


def _row(change, base="w1"):
    """(graph, state, binding, kwargs) from a route-bridge state of `base`, with `change(state)` applied."""
    def build(graph):
        g = {"w1": deepcopy, "feeder": lambda w: rk.build(w, "topology", rk._feeder),
             "second-central": lambda w: rk.build(w, "topology", rk._then(rk._second_central, rk._feeder))}[base](graph)
        state, binding = rb.state_from_graph(g)
        kwargs = change(state) or {}
        return g, state, binding, kwargs
    return build


def _cable(key, value, index=0):
    def change(state):
        state["rows"]["cable"][index][key] = deepcopy(value)
    return change


def _vertex(index, value):
    def change(state):
        state["rows"]["cable"][0]["vertices"][index] = deepcopy(value)
    return change


def _detail(value):
    def change(state):
        state["rows"]["cable"][0]["_detail"] = deepcopy(value)
    return change


def _gauge(value):
    def change(state):
        state["rows"]["cable"][0]["_detail"]["gauge"] = value
    return change


def _twice(index=0):
    def change(state):
        state["rows"]["cable"].append(deepcopy(state["rows"]["cable"][index]))
    return change


def _new_leg(**kwargs):
    def change(state):
        leg = deepcopy(state["rows"]["cable"][0])
        leg["segment"] = "end"
        state["rows"]["cable"].append(leg)
        return kwargs
    return change


def _feeder_row(**fields):
    def change(state):
        state["rows"]["cable"][1].update(deepcopy(fields))
    return change


GRAPH_FROM_STATE_ROUTE_REFUSALS = [
    ("homerun-unbound-handle", _row(_cable("from", "FF")), "BRIDGE_ROUTE_UNBOUND"),
    ("homerun-from-null", _row(_cable("from", None)), "BRIDGE_ROUTE_INVALID"),
    ("homerun-from-list", _row(_cable("from", [])), "BRIDGE_ROUTE_INVALID"),
    ("homerun-from-dict", _row(_cable("from", {})), "BRIDGE_ROUTE_INVALID"),
    ("homerun-segment-list", _row(_cable("segment", [])), "BRIDGE_ROUTE_INVALID"),
    ("homerun-segment-dict", _row(_cable("segment", {})), "BRIDGE_ROUTE_INVALID"),
    ("homerun-bad-segment", _row(_cable("segment", "middle")), "BRIDGE_ROUTE_INVALID"),
    ("homerun-duplicate", _row(_twice()), "BRIDGE_ROUTE_DUPLICATE"),
    ("vertices-one", _row(lambda s: s["rows"]["cable"][0].__setitem__("vertices", s["rows"]["cable"][0]["vertices"][1:])),
     "BRIDGE_ROUTE_INVALID"),
    ("vertex-not-coordinate", _row(_vertex(0, {"kind": "length", "value": 1.0, "unit": "ft"})), "BRIDGE_ROUTE_INVALID"),
    ("vertex-over-bound", _row(_vertex(0, st.coordinate(1e13, 0.0))), "BRIDGE_ROUTE_INVALID"),
    ("length-unit", _row(_cable("length", {"kind": "length", "value": 1.0, "unit": "m"})), "BRIDGE_ROUTE_INVALID"),
    ("length-extra-key", _row(_cable("length", {"kind": "length", "value": 1.0, "unit": "ft", "extra": 0})),
     "BRIDGE_ROUTE_INVALID"),
    ("length-negative", _row(_cable("length", {"kind": "length", "value": -1.0, "unit": "ft"})), "BRIDGE_ROUTE_INVALID"),
    ("length-boolean", _row(_cable("length", {"kind": "length", "value": True, "unit": "ft"})), "BRIDGE_ROUTE_INVALID"),
    ("length-over-bound", _row(_cable("length", {"kind": "length", "value": 1e10, "unit": "ft"})), "BRIDGE_ROUTE_INVALID"),
    ("gauge-not-string", _row(_gauge(5)), "BRIDGE_ROUTE_INVALID"),
    ("gauge-too-long", _row(_gauge("g" * 4097)), "BRIDGE_ROUTE_INVALID"),
    ("detail-not-object", _row(_detail("x")), "BRIDGE_ROUTE_INVALID"),
    ("unsupported-cable-kind", _row(_cable("cable_kind", "ac-feeder")), "BRIDGE_ROUTE_UNSUPPORTED"),
    ("feeder-ends-unreadable", _row(_feeder_row(**{"from": "x", "to": None, "_detail": {"circuit": ""}}), "feeder"),
     "BRIDGE_ROUTE_INVALID"),
    ("feeder-number-zero", _row(_feeder_row(**{"from": 0}), "feeder"), "BRIDGE_ROUTE_INVALID"),
    ("feeder-from-list", _row(_feeder_row(**{"from": []}), "feeder"), "BRIDGE_ROUTE_INVALID"),
    ("feeder-from-dict", _row(_feeder_row(**{"from": {}}), "feeder"), "BRIDGE_ROUTE_INVALID"),
    ("feeder-detail-list", _row(_feeder_row(_detail=[]), "feeder"), "BRIDGE_ROUTE_INVALID"),
    ("feeder-detail-string", _row(_feeder_row(_detail="x"), "feeder"), "BRIDGE_ROUTE_INVALID"),
    ("feeder-circuit-too-long", _row(_feeder_row(**{"from": None, "to": None,
        "_detail": {"circuit": "F" + "x" * 64}}), "feeder"), "BRIDGE_ROUTE_INVALID"),
    ("feeder-circuit-too-many-digits", _row(_feeder_row(**{"from": None, "to": None,
        "_detail": {"circuit": "F" + "1" * 10 + "/1"}}), "feeder"), "BRIDGE_ROUTE_INVALID"),
    ("feeder-circuit-interpreter-limit", _row(_feeder_row(**{"from": None, "to": None,
        "_detail": {"circuit": "F" + "1" * 5000 + "/1"}}), "feeder"), "BRIDGE_ROUTE_INVALID"),
    ("feeder-to-missing-l2", _row(_feeder_row(to=9), "feeder"), "BRIDGE_ROUTE_UNBOUND"),
    ("feeder-duplicate", _row(_twice(1), "feeder"), "BRIDGE_ROUTE_DUPLICATE"),
    ("feeder-against-assignment", _row(_feeder_row(to=2), "second-central"), "BRIDGE_ROUTE_TOPOLOGY_MISMATCH"),
    ("new-route-created-at-required", _row(_new_leg()), "BRIDGE_INVALID_REQUEST"),
    ("new-route-created-at-malformed", _row(_new_leg(created_at="yesterday")), "BRIDGE_INVALID_REQUEST"),
    ("new-route-id-not-a-string", _row(_new_leg(created_at=CREATED, new_id=lambda kind: 5)), "BRIDGE_INVALID_REQUEST"),
    ("new-route-id-collision", _row(_new_leg(created_at=CREATED, new_id=lambda kind: app_id("route", 1))),
     "BRIDGE_ID_COLLISION"),
]


@pytest.mark.parametrize("name,build,code", GRAPH_FROM_STATE_ROUTE_REFUSALS,
                         ids=[row[0] for row in GRAPH_FROM_STATE_ROUTE_REFUSALS])
def test_route_bridge_graph_from_state_refusals(graph, name, build, code):
    g, state, binding, kwargs = build(graph)
    snapshot = deepcopy((g, state, binding))
    with pytest.raises(rb.ElectricalBridgeError) as error:
        rb.graph_from_state(g, state, binding, **kwargs)
    assert error.value.code == str(error.value) == code
    assert (g, state, binding) == snapshot


def test_route_bridge_feeder_circuit_digit_boundary(graph):
    g = rk.build(graph, "topology", rk._feeder)
    state, binding = rb.state_from_graph(g)
    row = state["rows"]["cable"][1]
    row.update({"from": None, "to": None})
    row["_detail"]["circuit"] = "F000000001/000000001"
    snapshot = deepcopy((g, state, binding))
    result, rebound = rb.graph_from_state(g, state, binding)
    assert result == g and rebound == binding
    assert (g, state, binding) == snapshot


@pytest.mark.parametrize("index", [0, 1], ids=["homerun", "feeder"])
@pytest.mark.parametrize("vertices", [True, 1.5, 10**400, {"x": 0, "y": 1}],
                         ids=["boolean", "float", "huge-int", "object"])
def test_route_bridge_vertices_container_refusals(graph, index, vertices):
    g, state, binding, kwargs = _row(_cable("vertices", vertices, index), "feeder")(graph)
    snapshot = deepcopy((g, state, binding))
    with pytest.raises(rb.ElectricalBridgeError) as error:
        rb.graph_from_state(g, state, binding, **kwargs)
    assert error.value.code == str(error.value) == "BRIDGE_ROUTE_INVALID"
    assert (g, state, binding) == snapshot


@pytest.mark.parametrize("index", [0, 1], ids=["homerun", "feeder"])
@pytest.mark.parametrize("vertex", [
    {}, {"kind": "length", "value": [0, 1]},
    {"kind": "coordinate", "value": [0]},
    {"kind": "coordinate", "value": [0, 1, 2]},
    {"kind": "coordinate", "value": [True, 1]},
    {"kind": "coordinate", "value": [float("nan"), 1]},
    {"kind": "coordinate", "value": [float("inf"), 1]},
    {"kind": "coordinate", "value": [10**400, 1]},
], ids=["missing", "kind", "short", "long", "boolean", "nan", "infinity", "overflow"])
def test_route_bridge_vertex_shape_refusals(graph, index, vertex):
    g = rk.build(graph, "topology", rk._feeder)
    state, binding = rb.state_from_graph(g)
    state["rows"]["cable"][index]["vertices"][0] = vertex
    # NaN cannot be compared to itself; preserve inputs by checking the original object and row.
    original = state["rows"]["cable"][index]["vertices"]
    with pytest.raises(rb.ElectricalBridgeError) as error:
        rb.graph_from_state(g, state, binding)
    assert error.value.code == str(error.value) == "BRIDGE_ROUTE_INVALID"
    assert state["rows"]["cable"][index]["vertices"] is original
    assert original[0] is vertex


def test_route_bridge_vertex_shape_matches_state_reader(graph):
    g = rk.build(graph, "topology", rk._feeder)
    state, binding = rb.state_from_graph(g)
    for row in state["rows"]["cable"]:
        for vertex in row["vertices"]:
            # point_of accepts extra keys and does not require a unit.
            vertex.pop("unit")
            vertex["extra"] = "accepted by point_of"
            assert st.point_of(vertex) == vertex["value"]
    snapshot = deepcopy((g, state, binding))
    assert rb.graph_from_state(g, state, binding) == (g, binding)
    assert (g, state, binding) == snapshot


@pytest.mark.parametrize("probe,code", [
    ("vertices", "BRIDGE_BOUNDS_EXCEEDED"),
    ("handle", "BRIDGE_ROUTE_INVALID"),
], ids=["oversized-vertices", "oversized-hex-handle"])
def test_route_bridge_raw_bounds_precede_legacy_state(graph, monkeypatch, probe, code):
    g = rk.build(graph, "topology", rk._feeder)
    state, binding = rb.state_from_graph(g)
    row = state["rows"]["cable"][1]
    if probe == "vertices":
        row["vertices"] = [st.coordinate(0, 0)] * 100_001
    else:
        row["from"] = "A" * 1_000_000
        row["_detail"]["circuit"] = "F1/1"
    snapshot = deepcopy((g, state, binding))

    def forbidden_state(value):
        pytest.fail("raw bounds must refuse before legacy._state")

    monkeypatch.setattr(br, "_state", forbidden_state)
    with pytest.raises(rb.ElectricalBridgeError) as error:
        rb.graph_from_state(g, state, binding)
    assert error.value.code == str(error.value) == code
    assert (g, state, binding) == snapshot


def test_route_bridge_raw_total_vertices_precede_legacy_state(graph, monkeypatch):
    g = rk.build(graph, "topology", rk._feeder)
    state, binding = rb.state_from_graph(g)
    total = sum(len(row["vertices"]) for row in state["rows"]["cable"])
    snapshot = deepcopy((g, state, binding))

    def forbidden_state(value):
        pytest.fail("raw vertex total must refuse before legacy._state")

    with monkeypatch.context() as patch:
        patch.setattr(sys.modules["solar_design_graph"], "MAX_NODES", total - 1)
        patch.setattr(br, "_state", forbidden_state)
        with pytest.raises(rb.ElectricalBridgeError) as error:
            rb.graph_from_state(g, state, binding)
        assert error.value.code == str(error.value) == "BRIDGE_BOUNDS_EXCEEDED"
    assert rb.graph_from_state(g, state, binding) == (g, binding)
    assert (g, state, binding) == snapshot


@pytest.mark.parametrize("circuit", ["F0000000001/0000000001", "F" + "0" * 50 + "1/1"],
                         ids=["ten-digit-runs", "fifty-leading-zeros"])
def test_route_bridge_feeder_circuit_leading_zeros(graph, monkeypatch, circuit):
    g = rk.build(graph, "topology", rk._feeder)
    state, binding = rb.state_from_graph(g)
    row = state["rows"]["cable"][1]
    row.update({"from": None, "to": None})
    row["_detail"]["circuit"] = circuit
    snapshot = deepcopy((g, state, binding))
    actual = rb.graph_from_state(g, state, binding)
    with monkeypatch.context() as patch:
        patch.setattr(rb, "_check_row_types", lambda value: None)
        expected = rb.graph_from_state(g, state, binding)
    assert actual == expected == (g, binding)
    assert (g, state, binding) == snapshot


# The route-kinds validator codes fail closed through both functions, before anything is projected.
VALIDATOR_REFUSALS = [(name, base, change, code) for name, base, change, code in rk.REFUSED
                      if name in ("feeder-from-central", "feeder-to-combiner", "duplicate-feeder",
                                  "pathway-to-homerun", "string-pathway-to-homerun")]


@pytest.mark.parametrize("name,base,change,code", VALIDATOR_REFUSALS, ids=[row[0] for row in VALIDATOR_REFUSALS])
def test_route_bridge_validator_codes_fail_closed(graph, name, base, change, code):
    valid = topo.topology_of(graph) if base == "topology" else deepcopy(graph)
    state, binding = rb.state_from_graph(valid)
    g = rk.build(graph, base, change)
    snapshot = deepcopy((g, state, binding))
    for call in (lambda: rb.state_from_graph(g), lambda: rb.graph_from_state(g, state, binding)):
        with pytest.raises(GraphValidationError) as error:
            call()
        assert error.value.code == code
    assert (g, state, binding) == snapshot


@pytest.mark.parametrize("name,value,where,code", [
    ("routes", 0, "state_from_graph", "BRIDGE_BOUNDS_EXCEEDED"),
    ("vertices-graph", 1, "state_from_graph", "BRIDGE_BOUNDS_EXCEEDED"),
    ("vertices-state", 1, "rows", "BRIDGE_BOUNDS_EXCEEDED"),
    ("cable-rows", 0, "rows", "BRIDGE_BOUNDS_EXCEEDED"),
    ("route-vertices", 1, "rows", "BRIDGE_ROUTE_INVALID"),
    ("coordinate", 1.0, "state_from_graph", "BRIDGE_ROUTE_UNSUPPORTED"),
    ("length", 1.0, "state_from_graph", "BRIDGE_ROUTE_UNSUPPORTED"),
], ids=lambda value: str(value))
def test_route_bridge_bounds(graph, monkeypatch, name, value, where, code):
    state, binding = rb.state_from_graph(graph)
    bare = dict(deepcopy(graph), routes=[])
    bare_state, bare_binding = rb.state_from_graph(bare)
    bare_state["rows"]["cable"] = deepcopy(state["rows"]["cable"])
    attribute = {"routes": "MAX_ROUTES", "vertices-graph": "MAX_VERTICES", "vertices-state": "MAX_VERTICES",
                 "cable-rows": "MAX_CABLE_ROWS", "route-vertices": "MAX_ROUTE_VERTICES",
                 "coordinate": "MAX_COORDINATE", "length": "MAX_LENGTH_FT"}[name]
    monkeypatch.setattr(rb, attribute, value)
    with pytest.raises(rb.ElectricalBridgeError) as error:
        if where == "state_from_graph":
            rb.state_from_graph(graph)
        else:
            rb.graph_from_state(bare, bare_state, bare_binding, new_id=minter(), created_at=CREATED)
    assert error.value.code == str(error.value) == code


def test_route_bridge_output_route_bound(graph, monkeypatch):
    routed, _ = adopt_write(graph, ct.make_state())
    projected, _ = rb.state_from_graph(routed)
    bare = dict(deepcopy(routed), routes=[])
    state, binding = rb.state_from_graph(bare)
    state["rows"]["cable"] = deepcopy(projected["rows"]["cable"])
    assert [row["cable_kind"] for row in state["rows"]["cable"]] == ["dc-homerun"] * 4 + ["feeder"] * 2
    snapshot = deepcopy((bare, state, binding))
    with monkeypatch.context() as patch:
        patch.setattr(rb, "MAX_ROUTES", 3)
        with pytest.raises(rb.ElectricalBridgeError) as error:
            rb.graph_from_state(bare, state, binding, new_id=minter(200), created_at=CREATED)
        assert error.value.code == str(error.value) == "BRIDGE_BOUNDS_EXCEEDED"
    assert (bare, state, binding) == snapshot
    result, _ = rb.graph_from_state(bare, state, binding, new_id=minter(200), created_at=CREATED)
    assert len(result["routes"]) == 6
    assert (bare, state, binding) == snapshot
