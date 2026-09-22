"""The panel-group delete builtin: every group goes, the panels and zones stay.

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


delete = builtin("solar_panel_group_delete")


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


@pytest.fixture
def ungrouped():
    result = new_empty_graph(tenant_id="studio-group-delete", drawing_id="w2-" + "a" * 16,
                             source_hash="a" * 64, units=copy.deepcopy(UNITS), created_at=CREATED_AT)
    result["panels"] = [new_panel(index) for index in range(4)]
    return validate_graph(result)


@pytest.fixture
def grouped(ungrouped):
    """Four panels, two groups of two, and one zone naming every panel."""
    panels = ungrouped["panels"]
    zone = new_zone("Zone A", panels)
    ungrouped["electrical_zones"] = [zone]
    ungrouped["frames"] = [new_frame("group-1", panels[:2], zone_ref=zone["id"]),
                           new_frame("group-2", panels[2:])]
    return validate_graph(ungrouped)


def delete_all(graph):
    return delete.delete_all_groups(graph, {"expected_rev": graph["rev"]})


def test_every_group_goes_and_every_panel_survives(grouped):
    result = delete_all(grouped)
    after = result["graph"]
    assert result["deleted"] == 2 and result["no_op"] is False
    assert after["frames"] == []
    assert [panel["id"] for panel in after["panels"]] == [panel["id"] for panel in grouped["panels"]]
    assert result["panels_cleared"] == [panel["id"] for panel in after["panels"]]
    for before, panel in zip(grouped["panels"], after["panels"]):
        assert panel["frame_ref"] is None and panel["matrix_cell"] is None
        assert (panel["centre"], panel["angle"]) == (before["centre"], before["angle"])
        assert panel["assignment"] == before["assignment"]
        assert panel["provenance"]["last_writer"] == delete.TOOL
        assert panel["rev"] == after["rev"]
    assert after["rev"] == grouped["rev"] + 1 and after["parent_rev"] == grouped["rev"]
    assert validate_graph(after) == after


def test_the_delete_survives_the_ordinary_reopen(grouped):
    after = delete_all(grouped)["graph"]
    reopened = deserialize_graph(serialize_graph(after))
    assert reopened == after
    assert reopened["frames"] == []
    assert all(panel["frame_ref"] is None for panel in reopened["panels"])


def test_the_callers_graph_is_never_mutated(grouped):
    before = copy.deepcopy(grouped)
    delete_all(grouped)
    assert grouped == before


def test_an_ungrouped_drawing_is_a_no_op_that_says_so(ungrouped):
    result = delete_all(ungrouped)
    assert (result["deleted"], result["panels_cleared"], result["no_op"]) == (0, [], True)
    # The plugin leaves dbmod alone on an ungrouped drawing, so the revision holds.
    assert result["graph"] == ungrouped


def test_deleting_twice_is_a_no_op(grouped):
    once = delete_all(grouped)["graph"]
    twice = delete_all(once)
    assert twice["no_op"] is True and twice["deleted"] == 0
    assert twice["graph"] == once


def test_zones_and_their_membership_survive_and_a_group_link_is_cleared(grouped):
    # The v1 zone carries no group link; one written anyway is cleared, not left dangling.
    grouped["electrical_zones"][0]["group_refs"] = [frame["id"] for frame in grouped["frames"]]
    grouped = validate_graph(grouped)
    after = delete_all(grouped)["graph"]
    zone = after["electrical_zones"][0]
    assert zone["panel_refs"] == [panel["id"] for panel in after["panels"]]
    assert (zone["name"], zone["color_index"]) == ("Zone A", 1)
    assert zone["group_refs"] == []
    assert zone["provenance"]["last_writer"] == delete.TOOL


def test_a_surviving_reference_to_a_removed_group_refuses_the_whole_delete(grouped):
    # A reference this module cannot reach: refusing beats a half-deleted graph.
    grouped["electrical_zones"][0]["extra"]["panel_group_ref"] = grouped["frames"][0]["id"]
    grouped = validate_graph(grouped)
    before = copy.deepcopy(grouped)
    with pytest.raises(GraphValidationError, match="GROUP_REFERENCE_NOT_CLEARED"):
        delete_all(grouped)
    assert grouped == before


def test_a_removed_group_id_used_as_a_mapping_key_is_refused(grouped):
    grouped["settings"]["extra"]["group_notes"] = {grouped["frames"][1]["id"]: "kept"}
    grouped = validate_graph(grouped)
    with pytest.raises(GraphValidationError, match="GROUP_REFERENCE_NOT_CLEARED"):
        delete_all(grouped)


def test_a_stale_expected_revision_is_refused(grouped):
    with pytest.raises(GraphValidationError, match="STALE_GRAPH_REVISION"):
        delete.delete_all_groups(grouped, {"expected_rev": grouped["rev"] + 1})


def test_run_dispatches_delete_all_and_refuses_any_other(grouped):
    after = delete.run(grouped, {"operation": "delete-all", "expected_rev": grouped["rev"]})
    assert after["frames"] == [] and after["rev"] == grouped["rev"] + 1
    for params in ({"operation": "delete"}, {"operation": None}, {"expected_rev": 0}, ["delete-all"]):
        with pytest.raises(GraphValidationError, match="INVALID_GROUP_DELETE_REQUEST"):
            delete.run(grouped, params)


@pytest.mark.parametrize("params", [
    {},
    {"expected_rev": "0"},
    {"expected_rev": True},
    {"expected_rev": 0, "unexpected": 1},
    {"expected_rev": 0, "cancel": False},
    ["expected_rev"],
    None,
])
def test_a_malformed_request_is_refused(grouped, params):
    with pytest.raises(GraphValidationError, match="INVALID_GROUP_DELETE_REQUEST"):
        delete.delete_all_groups(grouped, params)
