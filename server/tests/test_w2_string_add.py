"""The string-add builtin: one circuit over the named panels, in the named order.

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
from solar_solve_results import coverage, sync_assignments

CREATED_AT = "2026-09-22T00:00:00+00:00"
UNITS = {"drawing_units": "in", "wcs_to_ucs": [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1],
         "elevation_datum": "unrecorded", "crs": ""}
# The sized length the seeded drawing committed: every solved circuit below is this
# long, so a shorter one is a state a re-solve could not produce.
SIZED_LENGTH = 4


def builtin(name):
    spec = importlib.util.spec_from_file_location(name, SERVER / "builtins" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


add = builtin("solar_string_add")


def provenance():
    return {"created_by": "test", "created_at": CREATED_AT, "last_writer": "test",
            "source_rev": 0, "source_hash": "a" * 64}


def new_panel(index):
    panel = {
        "id": new_id("panel"), "kind": "panel", "rev": 0, "extra": {},
        "validity": {"state": "valid", "reasons": []}, "provenance": provenance(),
        "frame_ref": None, "matrix_cell": None, "centre": [index * 100.0, 0.0], "angle": 0.0,
        "assignment": {"string_ref": None, "seq": None},
    }
    panel["provenance"]["source_handle"] = f"{index + 1:X}"
    return panel


def new_frame(name, panels, zone_ref=None):
    """One frame over a single row of panels, the shape the groups builtin commits."""
    frame = {
        "id": new_id("frame"), "kind": "frame", "rev": 0, "extra": {},
        "validity": {"state": "valid", "reasons": []}, "provenance": provenance(),
        "name": name, "insertion_point": panels[0]["centre"][:], "installation_design": "Roof",
        "panel_refs": [panel["id"] for panel in panels], "module_rows": 1,
        "module_columns": len(panels), "module_slots": len(panels), "module_power_watts": 0,
        "module_width_along_row": 77.0, "module_height_across_row": 38.5,
        "electrical_zone_ref": zone_ref, "sequences": [],
        "matrix": [[{"code": "panel", "panel_ref": panel["id"], "seq": None, "inverter_id": None,
                     "string_input_number": None, "x": panel["centre"][0], "y": panel["centre"][1],
                     "angle": panel["angle"]} for panel in panels]],
        "panel_assignments": [{"panel_ref": panel["id"], "string_ref": None, "seq": None,
                               "inverter_id": None, "string_input_number": None} for panel in panels],
    }
    for column, panel in enumerate(panels):
        panel["frame_ref"] = frame["id"]
        panel["matrix_cell"] = {"row": 0, "col": column}
    return frame


def new_zone(name, panels, color_index=1, panels_in_sequence=0):
    return {
        "id": new_id("zone-el"), "kind": "zone-el", "rev": 0, "extra": {},
        "validity": {"state": "valid", "reasons": []}, "provenance": provenance(),
        "name": name, "color_index": color_index, "panel_refs": [panel["id"] for panel in panels],
        "module_model": "", "inverter_model_a": "", "inverter_count_a": 0,
        "panels_in_sequence": panels_in_sequence, "dc_ac_ratio": 0,
        "voc_cold": {"passes": None, "override_accepted": False, "suggested_string_length": None,
                     "per_module": None, "string_voltage": None, "max_dc_voltage": None},
        "boundary_ref": None,
    }


def new_string(tag, panels):
    refs = [panel["id"] for panel in panels]
    return {
        "id": new_id("string"), "kind": "string", "rev": 0, "extra": {},
        "validity": {"state": "valid", "reasons": []}, "provenance": provenance(),
        "circuit_tag": tag, "circuit_kind": "String",
        "ordered_panel_refs": refs, "module_count": len(refs),
        "from_ref": refs[0], "to_ref": refs[-1], "tag_text_ref": None, "wire_gauge": "",
        "length_ft": 0, "route": [], "inverter_ref": None,
    }


@pytest.fixture
def solved():
    """Twelve panels in three groups of four; two groups wired, the third left free.

    The free group is the state a string delete leaves behind, which is exactly the
    drawing SINGLESTRING was captured on.
    """
    result = new_empty_graph(tenant_id="studio-string-add", drawing_id="w2-" + "a" * 16,
                             source_hash="a" * 64, units=copy.deepcopy(UNITS), created_at=CREATED_AT)
    result["panels"] = [new_panel(index) for index in range(12)]
    panels = result["panels"]
    zone = new_zone("Zone A", panels)
    result["electrical_zones"] = [zone]
    result["frames"] = [new_frame("group-1", panels[:4], zone_ref=zone["id"]),
                        new_frame("group-2", panels[4:8], zone_ref=zone["id"]),
                        new_frame("group-3", panels[8:])]
    result["strings"] = [new_string("S1", panels[:4]), new_string("S2", panels[4:8])]
    # The drawing's committed sizing: the cap the new circuit is measured against.
    result["settings"]["panels_in_sequence"] = SIZED_LENGTH
    sync_assignments(result)
    result["extra"]["solve_coverage"] = coverage(result)
    return validate_graph(result)


def free_panels(graph):
    """The panels no circuit wires, in graph order."""
    wired = {ref for string in graph["strings"] for ref in string["ordered_panel_refs"]}
    return [panel for panel in graph["panels"] if panel["id"] not in wired]


def add_to(graph, refs):
    return add.add_string(graph, {"expected_rev": graph["rev"], "ordered_panel_refs": refs})


@pytest.fixture
def chosen(solved):
    """Two free panels named in descending order: the order is the capability's output."""
    free = free_panels(solved)
    return [free[1]["id"], free[0]["id"]]


def test_one_circuit_is_created_over_the_named_panels_in_order(solved, chosen):
    result = add_to(solved, chosen)
    after = result["graph"]
    assert result["total"] == 3 and len(after["strings"]) == 3
    added = after["strings"][-1]
    assert added["id"] == result["string_ref"]
    # The order is the output, never a set: the selection is the reverse of the
    # drawing's own panel order, and it stays reversed.
    assert added["ordered_panel_refs"] == chosen
    assert chosen == [panel["id"] for panel in free_panels(solved)][1::-1] != chosen[::-1]
    assert added["module_count"] == 2
    assert added["from_ref"] == chosen[0] and added["to_ref"] == chosen[-1]
    # Local command: no cloud call, no inverter.
    assert added["inverter_ref"] is None and added["validity"]["state"] == "valid"
    assert added["provenance"]["last_writer"] == add.TOOL
    for seq, ref in enumerate(chosen):
        panel = next(p for p in after["panels"] if p["id"] == ref)
        assert panel["assignment"] == {"string_ref": added["id"], "seq": seq}
    assert after["rev"] == solved["rev"] + 1 and after["parent_rev"] == solved["rev"]
    assert validate_graph(after) == after


def test_a_circuit_shorter_than_every_other_is_accepted(solved, chosen):
    after = add_to(solved, chosen)["graph"]
    # Two panels where the solver's own circuits are four: a committed state a
    # re-solve of this drawing cannot produce.
    assert sorted(string["module_count"] for string in after["strings"]) == [2, SIZED_LENGTH,
                                                                            SIZED_LENGTH]


def test_every_existing_circuit_is_byte_identical(solved, chosen):
    after = add_to(solved, chosen)["graph"]
    assert after["strings"][:2] == solved["strings"]
    for string in solved["strings"]:
        for ref in string["ordered_panel_refs"]:
            panel = next(p for p in after["panels"] if p["id"] == ref)
            assert panel == next(p for p in solved["panels"] if p["id"] == ref)
    # A group the selection never touched keeps its sequence, its matrix and its revision.
    assert after["frames"][:2] == solved["frames"][:2]
    assert after["electrical_zones"] == solved["electrical_zones"]


def test_the_group_that_gained_the_circuit_keeps_every_panel(solved, chosen):
    after = add_to(solved, chosen)["graph"]
    added = after["strings"][-1]
    frame = after["frames"][2]
    assert frame["panel_refs"] == solved["frames"][2]["panel_refs"]
    assert [cell["panel_ref"] for cell in frame["matrix"][0]] == frame["panel_refs"]
    # The group gains exactly one sequence, the new circuit's, in the order given.
    assert [sequence["string_ref"] for sequence in frame["sequences"]] == [added["id"]]
    assert frame["sequences"][0]["ordered_panel_refs"] == chosen
    wired = {assignment["panel_ref"]: assignment["string_ref"]
             for assignment in frame["panel_assignments"]}
    assert {ref: wired[ref] for ref in chosen} == {ref: added["id"] for ref in chosen}
    assert sum(1 for value in wired.values() if value is None) == 2


def test_solve_coverage_drops_only_the_panels_the_circuit_wired(solved, chosen):
    free = [panel["id"] for panel in free_panels(solved)]
    assert solved["extra"]["solve_coverage"] == {"duplicate_panel_refs": [],
                                                 "unassigned_panel_refs": sorted(free)}
    after = add_to(solved, chosen)["graph"]
    assert after["extra"]["solve_coverage"] == {
        "duplicate_panel_refs": [],
        "unassigned_panel_refs": sorted(set(free) - set(chosen))}


def test_the_new_circuit_records_polarity_and_a_route_through_the_panel_centres(solved, chosen):
    after = add_to(solved, chosen)["graph"]
    added = after["strings"][-1]
    centres = [next(p for p in solved["panels"] if p["id"] == ref)["centre"] for ref in chosen]
    assert added["route"] == centres
    # One 100 m step between adjacent panel centres, reported in feet.
    assert added["length_ft"] == pytest.approx(100.0 / 0.3048)
    polarity = added["extra"]["polarity"]
    assert polarity["negative_panel_ref"] == chosen[0]
    assert polarity["positive_panel_ref"] == chosen[-1]
    assert polarity["source"] == "derived" and polarity["source_rev"] == solved["rev"]
    assert added["extra"]["length_provenance"]["length_units"] == "ft"


def test_the_circuit_tag_is_the_next_free_string_number(solved, chosen):
    assert solved["settings"]["string_number"] == 1
    result = add_to(solved, chosen)
    # S1 and S2 are taken, so the counter steps over them and then advances.
    assert result["circuit_tag"] == "S3"
    assert result["graph"]["strings"][-1]["circuit_tag"] == "S3"
    assert result["graph"]["settings"]["string_number"] == 4


def test_the_add_survives_the_ordinary_reopen(solved, chosen):
    after = add_to(solved, chosen)["graph"]
    reopened = deserialize_graph(serialize_graph(after))
    assert reopened == after
    assert reopened["strings"][-1]["ordered_panel_refs"] == chosen


def test_the_callers_graph_is_never_mutated(solved, chosen):
    before = copy.deepcopy(solved)
    add_to(solved, chosen)
    assert solved == before


def test_an_unknown_panel_is_refused_and_moves_nothing(solved, chosen):
    before = copy.deepcopy(solved)
    with pytest.raises(GraphValidationError, match="MISSING_PANEL"):
        add_to(solved, [new_id("panel")])
    # A refusal that also names real panels still moves nothing.
    with pytest.raises(GraphValidationError, match="MISSING_PANEL"):
        add_to(solved, chosen + [new_id("panel")])
    assert solved == before


def test_a_panel_already_in_a_circuit_is_refused_and_moves_nothing(solved, chosen):
    before = copy.deepcopy(solved)
    wired = solved["strings"][0]["ordered_panel_refs"][0]
    with pytest.raises(GraphValidationError, match="PANEL_ALREADY_ASSIGNED"):
        add_to(solved, [wired])
    with pytest.raises(GraphValidationError, match="PANEL_ALREADY_ASSIGNED"):
        add_to(solved, chosen + [wired])
    assert solved == before


def test_a_repeated_panel_is_refused_and_moves_nothing(solved, chosen):
    before = copy.deepcopy(solved)
    with pytest.raises(GraphValidationError, match="DUPLICATE_PANEL_MEMBERSHIP"):
        add_to(solved, [chosen[0], chosen[1], chosen[0]])
    assert solved == before


def test_a_circuit_longer_than_the_sized_length_is_refused(solved):
    free = [panel["id"] for panel in free_panels(solved)]
    assert len(free) == SIZED_LENGTH
    # Four free panels fit the sized length exactly; a fifth module would not, and the
    # cold-Voc guard never cleared one that long.
    assert len(add_to(solved, free)["graph"]["strings"]) == 3
    solved["settings"]["panels_in_sequence"] = SIZED_LENGTH - 1
    solved = validate_graph(solved)
    before = copy.deepcopy(solved)
    with pytest.raises(GraphValidationError, match="STRING_TOO_LONG"):
        add_to(solved, free)
    assert solved == before


def test_a_graph_that_never_committed_a_length_refuses_the_add(solved, chosen):
    solved["settings"]["panels_in_sequence"] = 0
    solved = validate_graph(solved)
    before = copy.deepcopy(solved)
    with pytest.raises(GraphValidationError, match="STRING_LENGTH_NOT_SIZED"):
        add_to(solved, chosen)
    assert solved == before


def test_a_zone_sized_graph_is_bounded_by_its_longest_zone(solved, chosen):
    # Zone sizing leaves the settings at zero and writes the length on each zone.
    solved["settings"]["panels_in_sequence"] = 0
    solved["electrical_zones"][0]["panels_in_sequence"] = len(chosen)
    solved = validate_graph(solved)
    assert add.max_string_length(solved) == len(chosen)
    assert len(add_to(solved, chosen)["graph"]["strings"]) == 3
    with pytest.raises(GraphValidationError, match="STRING_TOO_LONG"):
        add_to(solved, [panel["id"] for panel in free_panels(solved)])


def test_a_stale_expected_revision_is_refused(solved, chosen):
    with pytest.raises(GraphValidationError, match="STALE_GRAPH_REVISION"):
        add.add_string(solved, {"expected_rev": solved["rev"] + 1,
                                "ordered_panel_refs": chosen})


def test_run_dispatches_add_string_and_refuses_any_other(solved, chosen):
    after = add.run(solved, {"operation": "add-string", "expected_rev": solved["rev"],
                             "ordered_panel_refs": chosen})
    assert len(after["strings"]) == 3 and after["rev"] == solved["rev"] + 1
    for params in ({"operation": "add"}, {"operation": None}, {"expected_rev": 0}, ["add-string"]):
        with pytest.raises(GraphValidationError, match="INVALID_STRING_ADD_REQUEST"):
            add.run(solved, params)


@pytest.mark.parametrize("overrides", [
    {},
    {"expected_rev": "0"},
    {"expected_rev": True},
    {"ordered_panel_refs": []},
    {"ordered_panel_refs": "one"},
    {"ordered_panel_refs": [None]},
    {"ordered_panel_refs": [""]},
    {"unexpected": 1},
])
def test_a_malformed_request_is_refused(solved, chosen, overrides):
    params = {"expected_rev": solved["rev"], "ordered_panel_refs": chosen}
    params.update(overrides)
    if not overrides:
        params.pop("ordered_panel_refs")
    with pytest.raises(GraphValidationError, match="INVALID_STRING_ADD_REQUEST"):
        add.add_string(solved, params)
