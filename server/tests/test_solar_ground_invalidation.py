"""Output invalidation for L1/L2 topology edits on the shared design graph."""
from __future__ import annotations

from copy import deepcopy
import importlib.util
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import write_loop  # noqa: F401; establishes the drawing-store import path
from solar_dependencies import affected_entities, dependency_index
from solar_design_graph import GraphValidationError, validate_graph
from solar_solve_results import invalidate_dependents, require_current_export
from test_w1_design_graph import app_id, graph  # noqa: F401, fixture
import test_solar_ground_topology as topology

canon_sha = topology.canon_sha
SERVER = Path(__file__).resolve().parents[1]


L1_ID, L2_ID = topology.L1_ID, topology.L2_ID
THIRD_ID = app_id("inverter", 3)
S1, S2 = app_id("string", 1), app_id("string", 2)
ROUTE_ID, SCHEDULE_ID = app_id("route", 1), app_id("schedule", 1)
W1_INVERTER = app_id("inverter", 1)


def sorted_index(g):
    return {key: sorted(value) for key, value in dependency_index(g).items()}


def moved(g, i, position):
    """Copy of g with inverter i at a new position (an equipment move no writer has marked)."""
    h = deepcopy(g)
    h["inverters"][i]["position"] = position
    return h


def second_central(g):
    """Copy of g with a second, empty central inverter: application id 3, device number 2."""
    h = deepcopy(g)
    h["inverters"].append(topology.central(3, 2, []))
    return h


def reassigned(g):
    """The L1 moved from central inverter 1 to central inverter 2 (both sides of the link agree)."""
    h = second_central(g)
    h["inverters"][0]["l2_ref"] = THIRD_ID
    h["inverters"][1]["l1_assignments"] = []
    h["inverters"][2]["l1_assignments"] = [{"inverter_ref": L1_ID, "mppt_index": 0}]
    return h


def unconnected(g):
    h = deepcopy(g)
    h["inverters"][0]["l2_ref"] = None
    h["inverters"][1]["l1_assignments"] = []
    return h


def s1_on_second_combiner(g, route_follows):
    """String 1 moved to a second combiner box (id 3, number 2) on the same central inverter.
    The homerun from string 1 either follows it or still ends at the first combiner."""
    h = deepcopy(g)
    h["inverters"].append(topology.combiner(3, 2, L2_ID, total_dc_inputs=2, input_assignments=[
        {"string_ref": S1, "mppt_letter": "A", "input_number": 0}]))
    h["inverters"][0]["input_assignments"] = [
        a for a in h["inverters"][0]["input_assignments"] if a["string_ref"] != S1]
    h["inverters"][1]["l1_assignments"].append({"inverter_ref": THIRD_ID, "mppt_index": 0})
    h["strings"][0].update(inverter_ref=THIRD_ID, to_ref=THIRD_ID)
    frame = h["frames"][0]
    for record in frame["panel_assignments"] + [cell for row in frame["matrix"] for cell in row]:
        if record["panel_ref"] in (app_id("panel", 1), app_id("panel", 2)):
            record.update(inverter_id=THIRD_ID, string_input_number=0)
    if route_follows:
        h["routes"][0]["to_ref"] = THIRD_ID
    return h


def route_to(g, target):
    h = deepcopy(g)
    h["routes"][0]["to_ref"] = target
    return h


# (id, builder over (W, T) giving (before, after), changed ids, affected ids)
AFFECTED = [
    ("move-l1", lambda w, t: (t, moved(t, 0, [9, 1])), [L1_ID], [L1_ID, L2_ID, ROUTE_ID]),
    ("move-l2", lambda w, t: (t, moved(t, 1, [30, 5])), [L2_ID], [L2_ID]),
    ("string-edit", lambda w, t: (t, t), [S1], [L1_ID, L2_ID, ROUTE_ID, SCHEDULE_ID, S1]),
    ("reassign-l1", lambda w, t: (second_central(t), reassigned(t)), [L1_ID],
     [L1_ID, L2_ID, THIRD_ID, ROUTE_ID]),
    ("unconnect-l1", lambda w, t: (t, unconnected(t)), [L1_ID], [L1_ID, L2_ID, ROUTE_ID]),
    ("w1-inverter-move", lambda w, t: (w, moved(w, 0, [9, 1])), [W1_INVERTER], [W1_INVERTER, ROUTE_ID]),
]

# (id, builder over (W, T), expected refusal code or None when the export is current, canon_sha of the input)
EXPORTS = [
    ("w1", lambda w, t: deepcopy(w), None,
     "67ab25efcda14a69da65a4184cf5d362ac19a85dcf266f722a9528f5881737c3"),
    ("topology", lambda w, t: deepcopy(t), None,
     "d5c400309cb9d13f7557465cd3cdf325f6a30108dce600b57362dd83b3b51d1e"),
    ("string-moved-route-follows", lambda w, t: s1_on_second_combiner(t, True), None,
     "413c6b73cf3a3f993bddc384ca012b2e58caef886b6886e89e71835825a7de2b"),
    ("string-moved-route-stays", lambda w, t: s1_on_second_combiner(t, False), "SOLAR_OUTPUT_NOT_CURRENT",
     "888c00c331643d0438b49efa576808b03dc2b0b462c17b08685d68da75ba806c"),
    ("homerun-to-central", lambda w, t: route_to(t, L2_ID), "SOLAR_OUTPUT_NOT_CURRENT",
     "43941995a6fd81b17d82c2a9c6090cbb1d9f71f00ec2e3e5705321c2cbc0f143"),
    ("w1-homerun-to-missing", lambda w, t: route_to(w, app_id("inverter", 9)), "SOLAR_OUTPUT_NOT_CURRENT",
     "54ec663a7bb70a61f685abc7b8d2ee0d1113341e2758fc064d8fee53984eccdd"),
    ("w1-homerun-to-none", lambda w, t: route_to(w, None), "SOLAR_OUTPUT_NOT_CURRENT",
     "4d982ebba9af05b36a9fb0ece3e87335186f383e41dd8dbce549cb550f1f459c"),
]


def load_builtin(name):
    spec = importlib.util.spec_from_file_location(name, SERVER / "builtins" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ground_invalidation_w1_index_is_unchanged(graph):
    assert canon_sha(sorted_index(graph)) == "6cdde121f6ad885364960e0494e2e3e6b12e1da1f3b56078ebebfdc0ad733587"
    assert require_current_export(graph) == graph


def test_ground_invalidation_topology_index(graph):
    t = topology.topology_of(graph)
    index = sorted_index(t)
    assert canon_sha(index) == "3879184b1007f6e16451ed951f2b9191c499e9f37e31f305619da00db384839a"
    assert index[L1_ID] == [L2_ID, ROUTE_ID]
    assert index[L2_ID] == []
    assert index[S1] == [L1_ID, ROUTE_ID, SCHEDULE_ID]


@pytest.mark.parametrize("name,pair,changed,expected", AFFECTED, ids=[row[0] for row in AFFECTED])
def test_ground_invalidation_affected(graph, name, pair, changed, expected):
    before, after = pair(graph, topology.topology_of(graph))
    snapshot = deepcopy((before, after))
    assert affected_entities(before, after, list(changed)) == sorted(expected)
    assert (before, after) == snapshot


def test_ground_invalidation_marks_exactly_the_dependents(graph):
    t = topology.topology_of(graph)
    after = moved(t, 0, [9, 1])
    assert invalidate_dependents(t, after, [L1_ID]) == sorted([L1_ID, L2_ID, ROUTE_ID])
    states = {e["id"]: e["validity"] for e in after["inverters"] + after["routes"]
              + after["schedules"] + after["strings"]}
    stale = {"state": "stale", "reasons": ["upstream_corrected"]}
    valid = {"state": "valid", "reasons": []}
    assert states == {L1_ID: stale, L2_ID: stale, ROUTE_ID: stale, SCHEDULE_ID: valid, S1: valid, S2: valid}
    assert validate_graph(after) == after
    with pytest.raises(GraphValidationError) as refused:
        require_current_export(after)
    assert refused.value.code == "SOLAR_OUTPUT_NOT_CURRENT"


@pytest.mark.parametrize("name,build,expected,input_sha", EXPORTS, ids=[row[0] for row in EXPORTS])
def test_ground_invalidation_export(graph, name, build, expected, input_sha):
    g = build(graph, topology.topology_of(graph))
    assert canon_sha(g) == input_sha
    assert validate_graph(g) == g
    before = deepcopy(g)
    if expected is None:
        assert require_current_export(g) == g
    else:
        with pytest.raises(GraphValidationError) as refused:
            require_current_export(g)
        assert refused.value.code == expected
    assert g == before


def test_ground_invalidation_homerun_with_no_from_ref_is_current(graph):
    g = topology.topology_of(graph)
    route = deepcopy(g["routes"][0])
    route.update(id=app_id("route", 2), from_ref=None)
    g["routes"].append(route)
    assert validate_graph(g) == g
    before = deepcopy(g)
    assert require_current_export(g) == g
    assert g == before


def test_ground_invalidation_end_homerun_must_follow_topology(graph):
    g = topology.topology_of(graph)
    assert g["strings"][0]["id"] == S1
    assert g["strings"][0]["inverter_ref"] == L1_ID
    g["routes"][0].update(route_kind="end homerun", from_ref=S1, to_ref=L2_ID)
    assert validate_graph(g) == g
    before = deepcopy(g)
    with pytest.raises(GraphValidationError) as refused:
        require_current_export(g)
    assert refused.value.code == "SOLAR_OUTPUT_NOT_CURRENT"
    assert g == before


def test_ground_invalidation_i5_kernel_state(graph):
    g, _, _ = topology.i5_graph(graph)
    index = dependency_index(g)
    l1s = [item for item in g["inverters"] if not item["is_l2"]]
    l2s = [item for item in g["inverters"] if item["is_l2"]]
    assert (len(l1s), len(l2s), len(g["strings"])) == (14, 8, 173)
    assert all(index[item["id"]] == {item["l2_ref"]} for item in l1s)
    assert all(index[item["id"]] == set() for item in l2s)
    assert all(index[s["id"]] == {s["inverter_ref"]} for s in g["strings"])
    assert canon_sha({k: sorted(v) for k, v in index.items()}) == (
        "a68ea23ba8d9ce05855b05288fcfa81bc7f2809e77c37f8531b375ab0f4f2746")
    for item in l1s:
        assert affected_entities(g, g, [item["id"]]) == sorted([item["id"], item["l2_ref"]])
    by_id = {item["id"]: item for item in l1s}
    for s in g["strings"]:
        l1 = by_id[s["inverter_ref"]]
        assert affected_entities(g, g, [s["id"]]) == sorted([s["id"], l1["id"], l1["l2_ref"]])
    assert require_current_export(g) == g


def test_ground_invalidation_string_delete_stales_the_l2(graph):
    t = topology.topology_of(graph)
    t["schedules"] = []
    before = deepcopy(t)
    result = load_builtin("solar_string_delete").delete_strings(t, {"expected_rev": 0, "string_refs": [S2]})
    assert t == before
    after = result["graph"]
    assert (after["rev"], result["deleted"]) == (1, [S2])
    states = {e["id"]: e["validity"]["state"] for e in after["inverters"] + after["routes"] + after["strings"]}
    assert states == {L1_ID: "stale", L2_ID: "stale", ROUTE_ID: "stale", S1: "valid"}
    assert validate_graph(after) == after
