"""W1 homeruns, schedule and readiness keep feeders, trenches and pathways, and judge only homeruns."""
from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import sys
import uuid

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import write_loop  # noqa: F401; establishes the drawing-store import path
import product_capability_availability as availability
import solar_local_graph
import solar_wiring_client as wiring
from solar_design_graph import GraphValidationError, validate_graph
from solar_graph_context import resolve_graph_context
from solar_solve_results import HOMERUN_KINDS
from test_w1_design_graph import app_id, graph  # noqa: F401, fixture
from test_w1_equipment import case, equipment  # noqa: F401, fixture
from test_w1_equipment import licensed as licensed_equipment
from test_w1_local_graph_adapter import held, seed
import test_solar_ground_route_kinds as kinds
import test_solar_ground_topology as topology

canon_sha = topology.canon_sha
TENANT = "fixture-tenant"
L1_ID = app_id("inverter", 1)
FRAME_ID = app_id("frame", 1)
TRENCH_ID, FEEDER_ID = kinds.TRENCH_ID, kinds.FEEDER_ID
EXTRA_ID = app_id("route", 9)
ROUTE_IDS = [
    "leaf:route:00000000-0000-4000-8000-000000000001",
    "leaf:route:912d9168-74b2-4997-a6e4-1f11adb24709",
    "leaf:route:c80159af-4565-403d-a8af-7388f19009cc",
    "leaf:route:3539b776-d49d-47aa-b6be-61109c1c9d7f",
]
# The start homerun of string 1 as run() draws it: panel 1 centre to the inverter at [5, 0].
DIRECT = [[1.0, 0.0, 0.0], [5.0, 0.0, 0.0]]
# The same lead snapped onto the trench at y = 1 (LEAFCABLETOTRAY moves interior vertices only).
SNAPPED = [[1.0, 0.0, 0.0], [1.0, 1.0, 0.0], [5.0, 1.0, 0.0], [5.0, 0.0, 0.0]]
SCHEDULE_ROWS = "571d70ac4f73cc643526b70d626701368ce4c2989da25f87eca1f72956b5b007"
# The same rows after the inverter moves from [5, 0] to [6, 0] and the leads are regenerated.
MOVED_ROWS = "d9ea17c8b83838dcabb3bb7dbb1f22df8f6ad6caed9f4d93abf21cb35248fe42"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import requests

    def refuse(*args, **kwargs):
        pytest.fail("route consumer tests must not use the network")

    monkeypatch.setattr(requests.sessions.Session, "request", refuse)


def homeruns():
    return solar_local_graph._load_builtin("solar-homeruns")


def schedule():
    return solar_local_graph._load_builtin("solar-schedule")


def licensed(**kwargs):
    """Synthetic licensed readback (test_w1_routes_schedule.licensed), not a real DWG execution."""
    assert kwargs["timeout"] == 50
    candidate = kwargs["graph"]
    index = {e["id"]: e for key in ("routes", "schedules") for e in candidate[key]}
    return {"candidate_sha256": wiring.digest(candidate),
            "entities": [copy.deepcopy(index[ref]) for ref in kwargs["changed_ids"]],
            "application_to_handle": {ref: format(n + 256, "X")
                                      for n, ref in enumerate(kwargs["changed_ids"])}}


@pytest.fixture
def assigned(case):
    source, params, intake = case
    output = equipment.assign_equipment(
        source, params, drawing_intake=intake, licensed_equipment=licensed_equipment)["graph"]
    # assign_equipment stamps the placed inverter with the wall clock; pin it so digests hold.
    output["inverters"][0]["provenance"]["created_at"] = "2026-09-17T00:00:00Z"
    return validate_graph(output)


@pytest.fixture
def routed(assigned):
    """R: the assigned W1 graph after run(); rev 2, four current homeruns, one stale schedule."""
    return homeruns().run(copy.deepcopy(assigned), {"expected_rev": 1})


def route(g, route_id):
    return next(item for item in g["routes"] if item["id"] == route_id)


def with_trench(g):
    g["routes"].append(kinds.trench())
    return g


def with_riders(g):
    """A trench from frame 1 to an open hub; the start homerun of string 1 and string 1 ride it."""
    g["routes"].append(kinds.trench(from_ref=FRAME_ID))
    g["routes"][0]["pathway_ref"] = TRENCH_ID
    g["strings"][0]["pathway_ref"] = TRENCH_ID
    return g


def with_snapped(g):
    with_riders(g)
    g["routes"][0].update(points=copy.deepcopy(SNAPPED), length_ft=6 / .3048)
    return g


def with_moved_inverter(g):
    with_riders(g)
    g["inverters"][0]["position"] = [6.0, 0.0, 0.0]
    return g


def with_duplicate(g):
    """A trench and a second copy of the start homerun of string 1 under a new id."""
    with_trench(g)
    extra = copy.deepcopy(g["routes"][0])
    extra["id"] = EXTRA_ID
    g["routes"].append(extra)
    return g


def with_missing(g):
    with_trench(g)
    g["routes"] = [item for item in g["routes"] if item["id"] != ROUTE_IDS[3]]
    return g


def with_reversed(g):
    with_trench(g)
    g["routes"][0]["points"].reverse()
    return g


def with_feeder(g):
    """L1/L2 mode (topology_of): the W1 inverter is combiner box 1 feeding central inverter 2."""
    g = topology.topology_of(g)
    g["routes"].append(kinds.feeder())
    return g


def rerouted(change):
    def build(g):
        g = change(g)
        return homeruns().run(g, {"expected_rev": g["rev"]})
    return build


# (id, change over R, canon sha of the schedule rows, or None when schedule readiness and the
# schedule builtin both refuse COMPLETE_ROUTING_REQUIRED)
VARIANTS = [
    ("routed", lambda g: g, SCHEDULE_ROWS),
    ("trench", with_trench, SCHEDULE_ROWS),
    ("riders", with_riders, SCHEDULE_ROWS),
    ("snapped", with_snapped, None),
    ("moved-inverter", with_moved_inverter, None),
    ("duplicate-homerun", with_duplicate, None),
    ("missing-homerun", with_missing, None),
    ("reversed-homerun", with_reversed, None),
    ("snapped-rerouted", rerouted(with_snapped), SCHEDULE_ROWS),
    ("moved-inverter-rerouted", rerouted(with_moved_inverter), MOVED_ROWS),
]


def variant(routed, change):
    g = change(copy.deepcopy(routed))
    assert validate_graph(g) == g
    return g


def non_homeruns(g):
    return [item for item in g["routes"] if item["route_kind"] not in HOMERUN_KINDS]


@pytest.mark.parametrize("collisions", [0, 1, 2])
def test_route_consumers_new_ids_avoid_retained_routes(assigned, collisions):
    source = copy.deepcopy(assigned)
    source["routes"] = []
    # Frozen pre-correction derivation, including the independently reproduced first candidate.
    def original_id(string, kind, counter=0):
        seed = "solar-homeruns|" + string["id"] + "|" + kind
        if counter:
            seed += "|" + str(counter)
        identity = hashlib.sha256(seed.encode("utf-8")).digest()
        return "leaf:route:" + str(uuid.UUID(bytes=identity[:16], version=4))

    first = source["strings"][0]
    assert original_id(first, HOMERUN_KINDS[0]) == (
        "leaf:route:1a9024d1-3e5a-4c6f-bb2f-7ef7bd93640c")
    for counter in range(collisions):
        trench = kinds.trench(n=20 + counter)
        trench["id"] = original_id(first, HOMERUN_KINDS[0], counter)
        source["routes"].append(trench)
    assert validate_graph(source) == source
    before = copy.deepcopy(source)
    assert availability.w1_graph_readiness(source)["solar-homeruns"] == {
        "input_ready": True, "input_reason": None}
    output = homeruns().run(source, {"expected_rev": 1})
    repeated = homeruns().run(source, {"expected_rev": 1})
    ids = [item["id"] for item in output["routes"][:4]]
    expected = [original_id(string, kind) for string in source["strings"]
                for kind in HOMERUN_KINDS]
    expected[0] = original_id(first, HOMERUN_KINDS[0], collisions)
    assert ids == expected
    assert ids == [item["id"] for item in repeated["routes"][:4]]
    assert non_homeruns(output) == before["routes"]
    assert source == before
    assert validate_graph(output) == output
    assert availability.w1_graph_readiness(output)["solar-schedule"]["input_ready"]


@pytest.mark.parametrize("extension", [
    [{"id": []}],
    [{"id": "extension-only", "kind": "route"}],
], ids=["unhashable-id", "route-shaped"])
def test_route_consumers_extension_lists_are_not_graph_ids(assigned, extension):
    # A top-level list the validator ignores never takes part in identity reservation.
    plain = homeruns().run(copy.deepcopy(assigned), {"expected_rev": 1})
    source = copy.deepcopy(assigned)
    source["future_root"] = copy.deepcopy(extension)
    assert validate_graph(source) == source
    assert availability.w1_graph_readiness(copy.deepcopy(source))["solar-homeruns"] == {
        "input_ready": True, "input_reason": None}
    before = copy.deepcopy(source)
    output = homeruns().run(source, {"expected_rev": 1})
    assert output["routes"] == plain["routes"]
    assert output["future_root"] == extension
    assert source == before


def test_route_consumers_changed_null_pathway_reports_zero_drops(routed):
    source = copy.deepcopy(routed)
    source["routes"][0].update(points=copy.deepcopy(SNAPPED), length_ft=6 / .3048,
                                pathway_ref=None)
    assert validate_graph(source) == source
    result = homeruns().create_homeruns(source, {"expected_rev": 2}, licensed_write=licensed)
    assert result["graph"]["routes"][0]["points"] == DIRECT
    assert result["graph"]["routes"][0]["pathway_ref"] is None
    assert result["receipt"]["pathway_refs_dropped"] == 0


@pytest.mark.parametrize("licensed_path", [False, True], ids=["local", "licensed"])
def test_route_consumers_retained_routes_keep_relative_order(routed, licensed_path):
    source = copy.deepcopy(routed)
    kept = [kinds.trench(n=n) for n in (8, 3, 7)]
    source["routes"] = [kept[0], source["routes"][0], kept[1],
                        *source["routes"][1:], kept[2]]
    assert validate_graph(source) == source
    before = copy.deepcopy(source)
    if licensed_path:
        output = homeruns().create_homeruns(
            source, {"expected_rev": 2}, licensed_write=licensed)["graph"]
    else:
        output = homeruns().run(source, {"expected_rev": 2})
    assert non_homeruns(output) == kept
    assert all(item["route_kind"] in HOMERUN_KINDS for item in output["routes"][:4])
    assert source == before
    assert all(actual is not original for actual, original in zip(non_homeruns(output), kept))


def test_route_consumers_replacement_keeps_retained_objects(routed):
    source = copy.deepcopy(routed)
    kept = [kinds.trench(n=n) for n in (8, 3, 7)]
    source["routes"] = [kept[0], source["routes"][0], kept[1],
                        *source["routes"][1:], kept[2]]
    routes = wiring.local_routes(source)
    assert homeruns()._replace_homeruns(source, routes) == 0
    assert source["routes"][:4] == routes
    assert all(actual is original for actual, original in zip(source["routes"][4:], kept))
    assert len(source["routes"]) == 7


def test_route_consumers_feeder_keeps_its_pathway(routed):
    source = with_feeder(copy.deepcopy(routed))
    source["routes"].append(kinds.trench())
    route(source, FEEDER_ID)["pathway_ref"] = TRENCH_ID
    assert validate_graph(source) == source
    kept = copy.deepcopy(non_homeruns(source))
    result = homeruns().create_homeruns(source, {"expected_rev": 2}, licensed_write=licensed)
    assert non_homeruns(result["graph"]) == kept
    assert route(result["graph"], FEEDER_ID)["pathway_ref"] == TRENCH_ID
    tabled = schedule().create_schedule(
        result["graph"], {"expected_rev": 3, "insertion_point": [10, 20]},
        licensed_write=licensed)["graph"]
    assert non_homeruns(tabled) == kept
    assert set(result["receipt"]["application_to_handle"]).isdisjoint(
        item["id"] for item in kept)


def test_route_consumers_feeder_only_requires_homeruns(routed):
    source = with_feeder(copy.deepcopy(routed))
    source["routes"] = non_homeruns(source)
    assert validate_graph(source) == source
    before = copy.deepcopy(source)
    with pytest.raises(GraphValidationError) as refused:
        schedule().create_schedule(
            source, {"expected_rev": 2, "insertion_point": [10, 20]}, licensed_write=licensed)
    assert refused.value.code == "COMPLETE_ROUTING_REQUIRED"
    result = homeruns().create_homeruns(source, {"expected_rev": 2}, licensed_write=licensed)
    assert non_homeruns(result["graph"]) == before["routes"]
    assert [item["route_kind"] for item in result["graph"]["routes"][:4]] == [
        "start homerun", "end homerun", "start homerun", "end homerun"]
    assert result["receipt"]["pathway_refs_dropped"] == 0
    tabled = schedule().create_schedule(
        result["graph"], {"expected_rev": 3, "insertion_point": [10, 20]},
        licensed_write=licensed)["graph"]
    assert non_homeruns(tabled) == before["routes"]
    assert source == before


def test_route_consumers_w1_outputs_are_unchanged(assigned, routed):
    assert canon_sha(routed) == "935dd79c6e509bc2f24b207ca3d2f890a0c9c88eb4e5ed0bb39278e17d0c2b22"
    assert [item["id"] for item in routed["routes"]] == ROUTE_IDS
    assert all("pathway_ref" not in item for item in routed["routes"])
    tabled = schedule().run(copy.deepcopy(routed), {"expected_rev": 2, "insertion_point": [10, 20]})
    assert canon_sha(tabled) == "4ece445f56a4140aa719623bdc1d1072ec3ebe9dcb0e2a88efdaf26c9d9264a3"
    assert canon_sha(tabled["schedules"][-1]["rows"]) == SCHEDULE_ROWS
    readiness = availability.w1_graph_readiness(copy.deepcopy(routed))
    assert readiness["solar-homeruns"] == readiness["solar-schedule"] == {
        "input_ready": True, "input_reason": None}


def test_route_consumers_run_keeps_a_trench(routed):
    source = variant(routed, with_trench)
    trench = copy.deepcopy(route(source, TRENCH_ID))
    before = copy.deepcopy(source)
    output = homeruns().run(source, {"expected_rev": 2})
    assert source == before
    assert [item["id"] for item in output["routes"]] == ROUTE_IDS + [TRENCH_ID]
    assert route(output, TRENCH_ID) == trench
    assert all("pathway_ref" not in item for item in output["routes"][:4])
    assert output["schedules"][0]["validity"] == {"state": "stale", "reasons": ["ROUTES_CHANGED"]}
    assert canon_sha(output) == "357655a1492be98165a8f7dc9ec70531e961a23e30fa892ee9d2810a8ac14acd"


def test_route_consumers_run_keeps_riders_whose_points_hold(routed):
    source = variant(routed, with_riders)
    output = homeruns().run(copy.deepcopy(source), {"expected_rev": 2})
    assert [item["id"] for item in output["routes"]] == ROUTE_IDS + [TRENCH_ID]
    assert route(output, TRENCH_ID) == route(source, TRENCH_ID)
    assert output["routes"][0]["points"] == DIRECT
    assert output["routes"][0]["pathway_ref"] == TRENCH_ID
    assert all("pathway_ref" not in item for item in output["routes"][1:4])
    assert output["strings"][0]["pathway_ref"] == TRENCH_ID
    assert canon_sha(output) == "6fcb9b2ffee67384a8088bf50cb85414f3fa0c9b72012282fa1fa66747aa29a1"


@pytest.mark.parametrize("name,change,points", [
    ("snapped", with_snapped, DIRECT),
    ("moved-inverter", with_moved_inverter, [[1.0, 0.0, 0.0], [6.0, 0.0, 0.0]]),
], ids=["snapped", "moved-inverter"])
def test_route_consumers_run_drops_a_pathway_whose_points_change(routed, name, change, points):
    source = variant(routed, change)
    output = homeruns().run(copy.deepcopy(source), {"expected_rev": 2})
    lead = output["routes"][0]
    assert (lead["id"], lead["points"], lead["pathway_ref"]) == (ROUTE_IDS[0], points, None)
    assert all("pathway_ref" not in item for item in output["routes"][1:4])
    assert route(output, TRENCH_ID) == route(source, TRENCH_ID)
    assert output["strings"][0]["pathway_ref"] == TRENCH_ID
    assert validate_graph(output) == output


def test_route_consumers_create_homeruns_keeps_routes_and_reports_drops(routed):
    for change, dropped, kept in ((with_riders, 0, TRENCH_ID), (with_snapped, 1, None)):
        source = variant(routed, change)
        before = copy.deepcopy(source)
        result = homeruns().create_homeruns(source, {"expected_rev": 2}, licensed_write=licensed)
        assert source == before
        output = result["graph"]
        assert [item["route_kind"] for item in output["routes"]] == [
            "start homerun", "end homerun", "start homerun", "end homerun", "trench"]
        assert route(output, TRENCH_ID) == route(source, TRENCH_ID)
        assert output["routes"][0]["pathway_ref"] == kept
        assert set(result["receipt"]) == {"candidate_sha256", "application_to_handle",
                                          "pathway_refs_dropped"}
        assert result["receipt"]["pathway_refs_dropped"] == dropped
        assert set(result["receipt"]["application_to_handle"]) == {
            item["id"] for item in output["routes"][:4] + output["schedules"]}


def test_route_consumers_create_homeruns_keeps_a_feeder(routed):
    source = with_feeder(copy.deepcopy(routed))
    assert validate_graph(source) == source
    feeder = copy.deepcopy(route(source, FEEDER_ID))
    result = homeruns().create_homeruns(source, {"expected_rev": 2}, licensed_write=licensed)
    output = result["graph"]
    assert non_homeruns(output) == [feeder]
    assert [item["to_ref"] for item in output["routes"][:4]] == [L1_ID] * 4
    assert result["receipt"]["pathway_refs_dropped"] == 0
    readiness = availability.w1_graph_readiness(copy.deepcopy(output))
    # W1 equipment does not model L1/L2 devices, so both W1 tools stay refused upstream.
    assert readiness["solar-homeruns"] == {"input_ready": False,
                                           "input_reason": "equipment_assignment_required"}
    assert readiness["solar-schedule"] == {"input_ready": False,
                                           "input_reason": "complete_routing_required"}


def test_route_consumers_create_schedule_ignores_a_feeder(routed):
    source = with_feeder(copy.deepcopy(routed))
    result = schedule().create_schedule(
        source, {"expected_rev": 2, "insertion_point": [10, 20]}, licensed_write=licensed)
    output = result["graph"]
    assert canon_sha(output["schedules"][-1]["rows"]) == SCHEDULE_ROWS
    assert non_homeruns(output) == non_homeruns(source)
    assert FEEDER_ID not in output["schedules"][-1]["source_refs"]


@pytest.mark.parametrize("name,change,rows", VARIANTS, ids=[row[0] for row in VARIANTS])
def test_route_consumers_schedule_and_readiness_agree(routed, name, change, rows):
    ready = rows is not None
    source = variant(routed, change)
    readiness = availability.w1_graph_readiness(copy.deepcopy(source))
    assert readiness["solar-homeruns"] == {"input_ready": True, "input_reason": None}
    assert readiness["solar-schedule"] == {
        "input_ready": ready, "input_reason": None if ready else "complete_routing_required"}
    before = copy.deepcopy(source)
    params = {"expected_rev": source["rev"], "insertion_point": [10, 20]}
    if ready:
        output = schedule().run(source, params)
        assert canon_sha(output["schedules"][-1]["rows"]) == rows
        assert non_homeruns(output) == non_homeruns(before)
        assert set(output["schedules"][-1]["source_refs"]).isdisjoint(
            item["id"] for item in non_homeruns(before))
    else:
        with pytest.raises(GraphValidationError) as refused:
            schedule().run(source, params)
        assert refused.value.code == "COMPLETE_ROUTING_REQUIRED"
    assert source == before


@pytest.mark.parametrize("name,change,rows", VARIANTS, ids=[row[0] for row in VARIANTS])
def test_route_consumers_homeruns_run_where_readiness_says(routed, name, change, rows):
    source = variant(routed, change)
    assert availability.w1_graph_readiness(copy.deepcopy(source))["solar-homeruns"]["input_ready"]
    output = homeruns().run(copy.deepcopy(source), {"expected_rev": source["rev"]})
    assert non_homeruns(output) == non_homeruns(source)
    assert sum(item["route_kind"] in HOMERUN_KINDS for item in output["routes"]) == 4
    assert availability.w1_graph_readiness(copy.deepcopy(output))["solar-schedule"] == {
        "input_ready": True, "input_reason": None}


def test_route_consumers_shared_check_is_pure_and_counts_duplicates(routed):
    for change, current in ((with_riders, True), (with_duplicate, False), (with_missing, False)):
        source = variant(routed, change)
        before = copy.deepcopy(source)
        assert availability.w1_homeruns_current(source, wiring.local_routes(source)) is current
        assert source == before


def test_route_consumers_local_commit_publishes_and_reopens(routed, tmp_path, monkeypatch):
    source = variant(routed, with_riders)
    backend, _ = seed(tmp_path, monkeypatch, source)
    with held(backend) as fence:
        receipt = solar_local_graph.run_local_graph_commit(
            backend, TENANT, "solar-homeruns", {"drawing_id": "solar", "expected_rev": 2},
            drawing_id="solar", source_version=1, holder="fixture-owner", fence=fence,
            job_id="route-consumers-job")
        assert receipt["new_version"] == {"drawing_id": "solar", "version": 2, "parent": 1}
        stored = resolve_graph_context(backend, TENANT, "solar")["graph"]
        assert receipt["graph_sha256"] == wiring.digest(stored)
        assert [item["id"] for item in stored["routes"]] == ROUTE_IDS + [TRENCH_ID]
        assert stored["routes"][0]["pathway_ref"] == TRENCH_ID
        assert route(stored, TRENCH_ID) == route(source, TRENCH_ID)
        tabled = solar_local_graph.run_local_graph_commit(
            backend, TENANT, "solar-schedule",
            {"drawing_id": "solar", "expected_rev": 3, "insertion_point": [10, 20]},
            drawing_id="solar", source_version=2, holder="fixture-owner", fence=fence,
            job_id="route-consumers-schedule-job")
        assert tabled["new_version"] == {"drawing_id": "solar", "version": 3, "parent": 2}
        final = resolve_graph_context(backend, TENANT, "solar")["graph"]
        assert canon_sha(final["schedules"][-1]["rows"]) == SCHEDULE_ROWS
        assert route(final, TRENCH_ID) == route(source, TRENCH_ID)
