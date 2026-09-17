"""Offline circuit connectivity and typed schedule persistence checks."""
import copy
import importlib.util
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))
sys.path.insert(0, str(SERVER / "tests"))

import solar_wiring_client as wiring
from leaf_cloud_grants import CloudError
from solar_design_graph import GraphValidationError, deserialize_graph, serialize_graph
from test_w1_design_graph import graph  # noqa: F401
from test_w1_graph_versions import drawing, commit, request_for, TENANT, DRAWING  # noqa: F401
import store


def builtin(name):
    spec = importlib.util.spec_from_file_location(name, SERVER / "builtins" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


homeruns = builtin("solar_homeruns")
schedule = builtin("solar_schedule")


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import requests

    def refuse(*args, **kwargs):
        pytest.fail("offline tests must not use the network")

    monkeypatch.setattr(requests.sessions.Session, "request", refuse)


def licensed(**kwargs):
    """Synthetic licensed readback, not evidence of a real DWG execution."""
    assert kwargs["timeout"] == 50
    candidate = kwargs["graph"]
    index = {e["id"]: e for key in ("routes", "schedules") for e in candidate[key]}
    return {"candidate_sha256": wiring.digest(candidate),
            "entities": [copy.deepcopy(index[ref]) for ref in kwargs["changed_ids"]],
            "application_to_handle": {ref: format(n + 256, "X")
                                      for n, ref in enumerate(kwargs["changed_ids"])}}


def routed(graph, adapter=licensed):
    return homeruns.create_homeruns(graph, {"expected_rev": graph["rev"]},
                                    licensed_write=adapter)["graph"]


def scheduled(graph, adapter=licensed):
    return schedule.create_schedule(graph, {"expected_rev": graph["rev"],
                                            "insertion_point": [10, 20]},
                                    licensed_write=adapter)["graph"]


def test_both_leads_connect_ordered_terminals_to_assigned_input(graph):
    before = copy.deepcopy(graph)
    result = routed(graph)
    assert graph == before
    assert (result["rev"], result["parent_rev"]) == (1, 0)
    assert len(result["routes"]) == 4
    for string in graph["strings"]:
        leads = [r for r in result["routes"] if r["from_ref"] == string["id"]]
        assert [r["route_kind"] for r in leads] == ["start homerun", "end homerun"]
        for route, panel_id in zip(leads, [string["ordered_panel_refs"][0],
                                         string["ordered_panel_refs"][-1]]):
            panel = next(p for p in graph["panels"] if p["id"] == panel_id)
            assert route["points"] == [panel["centre"] + [0.0], [5.0, 0.0, 0.0]]
            assert route["to_ref"] == string["inverter_ref"]
            assert route["extra"]["terminal_panel_ref"] == panel_id
            assert route["wire_gauge"] == "10 AWG"
            assert route["length_ft"] == pytest.approx((5 - panel["centre"][0]) / .3048)
            assert route["point_units"] == "m" and route["length_units"] == "ft"
    assert result["schedules"][0]["validity"]["state"] == "stale"


def test_compute_coordinates_are_not_scaled_twice(graph):
    graph["project"]["units"].update(drawing_units="ft", meters_per_unit=.3048,
                                      drawing_unit_is_feet=True)
    result = routed(graph)
    assert result["routes"][0]["length_ft"] == pytest.approx(4 / .3048)


@pytest.mark.parametrize("defect", ["gauge", "empty", "inverter", "stale", "degenerate"])
def test_failed_routing_cannot_return_success_or_mutate_source(graph, defect):
    if defect == "gauge":
        graph["strings"][0]["wire_gauge"] = ""
    elif defect == "empty":
        graph["strings"] = []
    elif defect == "inverter":
        graph["strings"][0]["inverter_ref"] = None
    elif defect == "stale":
        graph["strings"][0]["validity"]["state"] = "stale"
    else:
        graph["inverters"][0]["position"] = graph["panels"][0]["centre"][:]
    before = copy.deepcopy(graph)
    with pytest.raises(GraphValidationError):
        routed(graph)
    assert graph == before


@pytest.mark.parametrize("defect", ["empty", "visualization", "hash", "geometry", "handle"])
def test_licensed_readback_must_prove_complete_persistence(graph, defect):
    def broken(**kwargs):
        reply = licensed(**kwargs)
        if defect == "empty":
            return {}
        if defect == "visualization":
            return {"svg": "synthetic visualization"}
        if defect == "hash":
            reply["candidate_sha256"] = "0" * 64
        elif defect == "geometry":
            reply["entities"][0]["points"].reverse()
        else:
            reply["application_to_handle"] = {}
        return reply

    before = copy.deepcopy(graph)
    with pytest.raises(GraphValidationError, match="INVALID_LICENSED_READBACK"):
        routed(graph, broken)
    assert graph == before


def test_license_required_cancel_and_stale_revision(graph):
    with pytest.raises(GraphValidationError, match="LICENSED_WRITE_REQUIRED"):
        routed(graph, None)
    for tool in (homeruns.create_homeruns, schedule.create_schedule):
        assert tool(graph, {"expected_rev": 0, "cancel": True})["graph"] == graph
        with pytest.raises(GraphValidationError, match="STALE_GRAPH_REVISION"):
            tool(graph, {"expected_rev": 99, "cancel": True})


def test_dark_service_is_unavailable_not_routing_failure():
    with pytest.raises(CloudError, match="cloud_service_unavailable"):
        wiring.check_service_status(503)
    wiring.check_service_status(200)


def test_typed_schedule_and_json_round_trip(graph):
    result = scheduled(routed(graph))
    table = result["schedules"][-1]
    assert table["insertion_point"] == [10.0, 20.0, 0.0]
    assert table["layer"] == "LEAF-SCHEDULES"
    assert table["source_rev"] == 1 and table["rev"] == 2
    assert table["headers"] == ["Circuit", "Modules", "Conductor", "Start homerun",
                                "End homerun", "Total wire"]
    assert table["rows"][0][:3] == ["S1", 2, "10 AWG"]
    assert type(table["rows"][0][1]) is int
    assert table["rows"][0][-1] == pytest.approx(7 / .3048)
    assert table["column_units"] == [None, "count", None, "ft", "ft", "ft"]
    assert {r["id"] for r in result["routes"]} <= set(table["source_refs"])
    assert deserialize_graph(serialize_graph(result)) == result


@pytest.mark.parametrize("defect", ["missing", "duplicate", "reversed", "length", "input"])
def test_schedule_refuses_incomplete_or_wrong_connectivity(graph, defect):
    result = routed(graph)
    route = result["routes"][0]
    if defect == "missing":
        result["routes"].pop()
    elif defect == "duplicate":
        result["routes"][1]["route_kind"] = route["route_kind"]
    elif defect == "reversed":
        route["points"].reverse()
    elif defect == "length":
        route["length_ft"] += 1
    else:
        route["extra"]["input_number"] += 1
    with pytest.raises(GraphValidationError, match="COMPLETE_ROUTING_REQUIRED"):
        scheduled(result)


def test_schedule_reopens_through_existing_version_transaction(drawing, graph):
    backend, _ = drawing
    # Establish revision zero, then publish routing and schedule as separate
    # accepted mutations on the same existing authority.
    initial = request_for(backend, graph, apply_id="initial")
    commit(drawing, initial)
    current = store.read_graph_bundle(backend, TENANT, DRAWING)["graph"]
    for name, operation in (("routing", routed), ("schedule", scheduled)):
        candidate = operation(current)
        request = request_for(backend, candidate, apply_id=name, dwg=name.encode())
        commit(drawing, request, dwg=name.encode())
        current = store.read_graph_bundle(store.FilesystemBackend(backend.root),
                                          TENANT, DRAWING)["graph"]
        assert current == request["graph"]
    table = current["schedules"][-1]
    assert table["rows"][0][-1] == pytest.approx(7 / .3048)
    assert table["source_rev"] == 1 and table["rev"] == 2
    assert deserialize_graph(serialize_graph(current))["schedules"][-1] == table
