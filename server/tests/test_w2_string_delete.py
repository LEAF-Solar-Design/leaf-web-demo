"""The string-delete builtin: the named circuits go, every panel and cable else stays.

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


def builtin(name):
    spec = importlib.util.spec_from_file_location(name, SERVER / "builtins" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


delete = builtin("solar_string_delete")


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


def new_zone(name, panels, color_index=1):
    return {
        "id": new_id("zone-el"), "kind": "zone-el", "rev": 0, "extra": {},
        "validity": {"state": "valid", "reasons": []}, "provenance": provenance(),
        "name": name, "color_index": color_index, "panel_refs": [panel["id"] for panel in panels],
        "module_model": "", "inverter_model_a": "", "inverter_count_a": 0,
        "panels_in_sequence": 0, "dc_ac_ratio": 0,
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
    """Six panels in two groups of three, each group wired into one circuit."""
    result = new_empty_graph(tenant_id="studio-string-delete", drawing_id="w2-" + "a" * 16,
                             source_hash="a" * 64, units=copy.deepcopy(UNITS), created_at=CREATED_AT)
    result["panels"] = [new_panel(index) for index in range(6)]
    panels = result["panels"]
    zone = new_zone("Zone A", panels)
    result["electrical_zones"] = [zone]
    result["frames"] = [new_frame("group-1", panels[:3], zone_ref=zone["id"]),
                        new_frame("group-2", panels[3:])]
    result["strings"] = [new_string("S1", panels[:3]), new_string("S2", panels[3:])]
    sync_assignments(result)
    result["extra"]["solve_coverage"] = coverage(result)
    return validate_graph(result)


def delete_from(graph, refs):
    return delete.delete_strings(graph, {"expected_rev": graph["rev"], "string_refs": refs})


def test_a_named_circuit_goes_and_its_panels_stay_unwired(solved):
    gone, kept = solved["strings"]
    result = delete_from(solved, [gone["id"]])
    after = result["graph"]
    assert result["deleted"] == [gone["id"]] and result["remaining"] == 1
    assert result["panels_freed"] == sorted(gone["ordered_panel_refs"])
    assert [string["id"] for string in after["strings"]] == [kept["id"]]
    for ref in gone["ordered_panel_refs"]:
        panel = next(p for p in after["panels"] if p["id"] == ref)
        # The panel is still in the drawing, in its group and in its zone.
        assert panel["assignment"] == {"string_ref": None, "seq": None}
        assert panel["frame_ref"] is not None and panel["matrix_cell"] is not None
        assert panel["provenance"]["last_writer"] == delete.TOOL
    assert [p["id"] for p in after["panels"]] == [p["id"] for p in solved["panels"]]
    assert after["electrical_zones"] == solved["electrical_zones"]
    assert after["rev"] == solved["rev"] + 1 and after["parent_rev"] == solved["rev"]
    assert validate_graph(after) == after


def test_every_surviving_circuit_is_byte_identical(solved):
    gone, kept = solved["strings"]
    after = delete_from(solved, [gone["id"]])["graph"]
    assert after["strings"] == [kept]
    for ref in kept["ordered_panel_refs"]:
        panel = next(p for p in after["panels"] if p["id"] == ref)
        assert panel == next(p for p in solved["panels"] if p["id"] == ref)
    # The group that kept its circuit keeps its sequence, its matrix and its revision.
    assert after["frames"][1] == solved["frames"][1]


def test_the_group_that_lost_its_circuit_keeps_every_panel(solved):
    gone = solved["strings"][0]
    after = delete_from(solved, [gone["id"]])["graph"]
    frame = after["frames"][0]
    assert frame["panel_refs"] == solved["frames"][0]["panel_refs"]
    assert [cell["panel_ref"] for cell in frame["matrix"][0]] == frame["panel_refs"]
    assert frame["sequences"] == []
    assert all(assignment["string_ref"] is None and assignment["seq"] is None
               for assignment in frame["panel_assignments"])
    assert all(cell["seq"] is None for cell in frame["matrix"][0])


def test_solve_coverage_follows_the_circuits_that_remain(solved):
    gone = solved["strings"][0]
    assert solved["extra"]["solve_coverage"] == {"unassigned_panel_refs": [],
                                                 "duplicate_panel_refs": []}
    after = delete_from(solved, [gone["id"]])["graph"]
    assert after["extra"]["solve_coverage"] == {
        "duplicate_panel_refs": [], "unassigned_panel_refs": sorted(gone["ordered_panel_refs"])}


def test_deleting_every_circuit_leaves_a_valid_graph_with_all_its_panels(solved):
    result = delete_from(solved, [string["id"] for string in solved["strings"]])
    after = result["graph"]
    assert after["strings"] == [] and result["remaining"] == 0
    assert [p["id"] for p in after["panels"]] == [p["id"] for p in solved["panels"]]
    assert all(panel["assignment"] == {"string_ref": None, "seq": None} for panel in after["panels"])
    assert after["extra"]["solve_coverage"] == {
        "duplicate_panel_refs": [], "unassigned_panel_refs": sorted(p["id"] for p in after["panels"])}
    assert [frame["id"] for frame in after["frames"]] == [f["id"] for f in solved["frames"]]
    assert validate_graph(after) == after


def test_the_delete_survives_the_ordinary_reopen(solved):
    after = delete_from(solved, [solved["strings"][0]["id"]])["graph"]
    reopened = deserialize_graph(serialize_graph(after))
    assert reopened == after
    assert len(reopened["strings"]) == 1


def test_the_callers_graph_is_never_mutated(solved):
    before = copy.deepcopy(solved)
    delete_from(solved, [solved["strings"][0]["id"]])
    assert solved == before


def test_an_unknown_circuit_is_refused_and_moves_nothing(solved):
    before = copy.deepcopy(solved)
    with pytest.raises(GraphValidationError, match="MISSING_STRING"):
        delete_from(solved, [new_id("string")])
    # A refusal that also names a real circuit still moves nothing.
    with pytest.raises(GraphValidationError, match="MISSING_STRING"):
        delete_from(solved, [solved["strings"][0]["id"], new_id("string")])
    assert solved == before


def test_a_repeated_circuit_in_one_request_is_refused(solved):
    ref = solved["strings"][0]["id"]
    with pytest.raises(GraphValidationError, match="INVALID_STRING_DELETE_REQUEST"):
        delete_from(solved, [ref, ref])


def test_a_stale_expected_revision_is_refused(solved):
    with pytest.raises(GraphValidationError, match="STALE_GRAPH_REVISION"):
        delete.delete_strings(solved, {"expected_rev": solved["rev"] + 1,
                                       "string_refs": [solved["strings"][0]["id"]]})


def test_a_surviving_reference_to_a_deleted_circuit_refuses_the_whole_delete(solved):
    # A reference this module cannot reach: refusing beats a half-deleted drawing.
    solved["frames"][0]["extra"]["pinned_string"] = solved["strings"][0]["id"]
    solved = validate_graph(solved)
    before = copy.deepcopy(solved)
    with pytest.raises(GraphValidationError, match="STRING_REFERENCE_NOT_CLEARED"):
        delete_from(solved, [solved["strings"][0]["id"]])
    assert solved == before


def test_run_dispatches_delete_strings_and_refuses_any_other(solved):
    after = delete.run(solved, {"operation": "delete-strings", "expected_rev": solved["rev"],
                                "string_refs": [solved["strings"][0]["id"]]})
    assert len(after["strings"]) == 1 and after["rev"] == solved["rev"] + 1
    for params in ({"operation": "delete"}, {"operation": None}, {"expected_rev": 0}, ["delete-strings"]):
        with pytest.raises(GraphValidationError, match="INVALID_STRING_DELETE_REQUEST"):
            delete.run(solved, params)


@pytest.mark.parametrize("overrides", [
    {},
    {"expected_rev": "0"},
    {"expected_rev": True},
    {"string_refs": []},
    {"string_refs": "one"},
    {"string_refs": [None]},
    {"string_refs": [""]},
    {"unexpected": 1},
])
def test_a_malformed_request_is_refused(solved, overrides):
    params = {"expected_rev": solved["rev"], "string_refs": [solved["strings"][0]["id"]]}
    params.update(overrides)
    if not overrides:
        params.pop("string_refs")
    with pytest.raises(GraphValidationError, match="INVALID_STRING_DELETE_REQUEST"):
        delete.delete_strings(solved, params)
