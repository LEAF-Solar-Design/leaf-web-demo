"""The electrical-zone builtin: create, assign, and the partition the plugin keeps.

Offline and dependency-free: the graph is seeded here, no network, no fixture.
"""
from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path

import pytest

SERVER = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVER))

from solar_design_graph import GraphValidationError, deserialize_graph, new_id, serialize_graph, validate_graph
from solar_graph_seed import new_empty_graph

CREATED_AT = "2026-09-22T00:00:00+00:00"
UNITS = {"drawing_units": "in", "wcs_to_ucs": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
         "elevation_datum": "unrecorded", "crs": ""}


def builtin(name):
    spec = importlib.util.spec_from_file_location(name, SERVER / "builtins" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


zones = builtin("solar_electrical_zones")


@pytest.fixture
def graph():
    result = new_empty_graph(tenant_id="studio-zones", drawing_id="w1-" + "a" * 16,
                             source_hash="a" * 64, units=copy.deepcopy(UNITS), created_at=CREATED_AT)
    for index in range(4):
        result["panels"].append({
            "id": new_id("panel"), "kind": "panel", "rev": 0, "extra": {},
            "validity": {"state": "valid", "reasons": []},
            "provenance": {"created_by": "test", "created_at": CREATED_AT, "last_writer": "test",
                           "source_rev": 0, "source_hash": "a" * 64,
                           "source_handle": f"{index + 1:X}"},
            "frame_ref": None, "matrix_cell": None, "centre": [index * 100.0, 0.0], "angle": 0.0,
            "assignment": {"string_ref": None, "seq": None},
        })
    return validate_graph(result)


def refs(graph, *indexes):
    return [graph["panels"][index]["id"] for index in indexes]


def with_zone(graph, name, color_index=1):
    return zones.add_zone(graph, {"expected_rev": graph["rev"], "name": name,
                                  "color_index": color_index})


def assign(graph, name, *indexes):
    return zones.assign_panels(graph, {"expected_rev": graph["rev"], "name": name,
                                       "panel_refs": refs(graph, *indexes)})


def test_add_zone_writes_a_name_a_colour_and_nothing_else(graph):
    before = copy.deepcopy(graph)
    result = with_zone(graph, "Zone A", color_index=3)
    assert graph == before, "the builtin never mutates its caller's graph"
    after = result["graph"]
    assert after["rev"] == 1 and after["parent_rev"] == 0
    zone = after["electrical_zones"][0]
    assert zone["id"] == result["zone_id"] and zone["kind"] == "zone-el"
    assert (zone["name"], zone["color_index"], zone["panel_refs"]) == ("Zone A", 3, [])
    assert (zone["module_model"], zone["inverter_model_a"], zone["inverter_count_a"]) == ("", "", 0)
    assert (zone["panels_in_sequence"], zone["dc_ac_ratio"]) == (0, 0)
    assert zone["voc_cold"]["passes"] is None and zone["boundary_ref"] is None
    assert zone["provenance"]["created_by"] == zones.TOOL
    assert zone["rev"] == 1 and zone["provenance"]["source_rev"] == 0
    # It survives the ordinary reopen path unchanged.
    assert deserialize_graph(serialize_graph(after))["electrical_zones"] == after["electrical_zones"]


def test_a_second_zone_keeps_the_first_and_refuses_a_duplicate_name(graph):
    first = with_zone(graph, "Zone A")["graph"]
    second = with_zone(first, "Zone B", color_index=5)["graph"]
    assert [zone["name"] for zone in second["electrical_zones"]] == ["Zone A", "Zone B"]
    assert [zone["color_index"] for zone in second["electrical_zones"]] == [1, 5]
    for duplicate in ("Zone A", "zone a", "ZONE A"):
        with pytest.raises(GraphValidationError, match="DUPLICATE_ZONE_NAME"):
            with_zone(second, duplicate)


def test_assignment_moves_a_panel_out_of_every_other_zone(graph):
    populated = assign(with_zone(with_zone(graph, "Zone A")["graph"], "Zone B")["graph"],
                       "Zone A", 0, 1, 2)["graph"]
    assert [zone["panel_refs"] for zone in populated["electrical_zones"]] == [refs(populated, 0, 1, 2), []]
    result = assign(populated, "Zone B", 2, 3)
    moved = result["graph"]
    assert [zone["panel_refs"] for zone in moved["electrical_zones"]] == [
        refs(moved, 0, 1), refs(moved, 2, 3)]
    assert result["moved"] == refs(moved, 2)
    assert result["panel_count"] == 2
    # Zones partition: no panel is named twice anywhere in the graph.
    members = [ref for zone in moved["electrical_zones"] for ref in zone["panel_refs"]]
    assert len(members) == len(set(members))
    assert moved["rev"] == 4 and validate_graph(moved) == moved


def test_reassigning_the_same_panels_is_stable_and_keeps_membership_order(graph):
    populated = assign(with_zone(graph, "Zone A")["graph"], "Zone A", 2, 0)["graph"]
    assert populated["electrical_zones"][0]["panel_refs"] == refs(populated, 2, 0)
    again = assign(populated, "Zone A", 0, 2)["graph"]
    assert again["electrical_zones"][0]["panel_refs"] == refs(again, 2, 0)
    assert again["rev"] == populated["rev"] + 1


def test_assignment_names_the_zone_without_case_and_never_touches_geometry(graph):
    populated = assign(with_zone(graph, "Zone A")["graph"], "zone a", 0, 1)["graph"]
    assert populated["electrical_zones"][0]["panel_refs"] == refs(populated, 0, 1)
    assert [(p["centre"], p["angle"], p["frame_ref"], p["assignment"]) for p in populated["panels"]] == [
        (p["centre"], p["angle"], p["frame_ref"], p["assignment"]) for p in graph["panels"]]
    assert populated["frames"] == [] and populated["strings"] == []


def test_an_unknown_zone_or_panel_is_refused(graph):
    with_a = with_zone(graph, "Zone A")["graph"]
    with pytest.raises(GraphValidationError, match="MISSING_ELECTRICAL_ZONE"):
        assign(with_a, "Zone C", 0)
    with pytest.raises(GraphValidationError, match="MISSING_PANEL"):
        zones.assign_panels(with_a, {"expected_rev": with_a["rev"], "name": "Zone A",
                                     "panel_refs": [new_id("panel")]})
    with pytest.raises(GraphValidationError, match="MISSING_PANEL"):
        zones.assign_panels(with_a, {"expected_rev": with_a["rev"], "name": "Zone A",
                                     "panel_refs": refs(with_a, 0) + [new_id("panel")]})
    with pytest.raises(GraphValidationError, match="DUPLICATE_PANEL_MEMBERSHIP"):
        zones.assign_panels(with_a, {"expected_rev": with_a["rev"], "name": "Zone A",
                                     "panel_refs": refs(with_a, 0, 0)})


def test_run_dispatches_both_operations_and_refuses_any_other(graph):
    added = zones.run(graph, {"operation": "add", "expected_rev": graph["rev"],
                              "name": "Zone A", "color_index": 2})
    assigned = zones.run(added, {"operation": "assign-panels", "expected_rev": added["rev"],
                                 "name": "Zone A", "panel_refs": refs(added, 0)})
    assert assigned["electrical_zones"][0]["panel_refs"] == refs(assigned, 0)
    for params in ({"operation": "delete"}, {"operation": None}, {"name": "Zone A"}, ["add"]):
        with pytest.raises(GraphValidationError, match="INVALID_ZONE_REQUEST"):
            zones.run(added, params)


@pytest.mark.parametrize("operation", ["add", "assign-panels"])
def test_a_stale_expected_revision_is_refused(graph, operation):
    populated = with_zone(graph, "Zone A")["graph"]
    request = {"expected_rev": populated["rev"] - 1, "name": "Zone A"}
    if operation == "add":
        request.update(name="Zone B", color_index=1)
    else:
        request["panel_refs"] = refs(populated, 0)
    with pytest.raises(GraphValidationError, match="STALE_GRAPH_REVISION"):
        zones.OPERATIONS[operation](populated, request)


@pytest.mark.parametrize("request_fields", [
    {"name": ""},
    {"name": "   "},
    {"name": "x" * 256},
    {"name": 7},
    {"color_index": -1},
    {"color_index": 257},
    {"color_index": True},
    {"color_index": "3"},
    {"unexpected": 1},
])
def test_add_refuses_a_malformed_request(graph, request_fields):
    request = {"expected_rev": graph["rev"], "name": "Zone A", "color_index": 1}
    request.update(request_fields)
    with pytest.raises(GraphValidationError, match="INVALID_ZONE_REQUEST"):
        zones.add_zone(graph, request)


@pytest.mark.parametrize("request_fields", [
    {"panel_refs": []},
    {"panel_refs": "all"},
    {"panel_refs": [7]},
    {"name": ""},
    {"unexpected": 1},
])
def test_assignment_refuses_a_malformed_request(graph, request_fields):
    populated = with_zone(graph, "Zone A")["graph"]
    request = {"expected_rev": populated["rev"], "name": "Zone A",
               "panel_refs": refs(populated, 0)}
    request.update(request_fields)
    with pytest.raises(GraphValidationError, match="INVALID_ZONE_REQUEST"):
        zones.assign_panels(populated, request)
