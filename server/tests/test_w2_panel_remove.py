"""The panel-remove builtin: one group loses the named panels, nothing else moves.

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


remove = builtin("solar_panel_remove")


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


def new_string(panels):
    return {
        "id": new_id("string"), "kind": "string", "rev": 0, "extra": {},
        "validity": {"state": "valid", "reasons": []}, "provenance": provenance(),
        "circuit_tag": "S1", "circuit_kind": "String",
        "ordered_panel_refs": [panel["id"] for panel in panels], "module_count": len(panels),
        "from_ref": None, "to_ref": None, "tag_text_ref": None, "wire_gauge": "",
        "length_ft": 0, "route": [], "inverter_ref": None,
    }


@pytest.fixture
def grouped():
    """Six panels, two groups of three, and one zone naming every panel."""
    result = new_empty_graph(tenant_id="studio-panel-remove", drawing_id="w2-" + "a" * 16,
                             source_hash="a" * 64, units=copy.deepcopy(UNITS), created_at=CREATED_AT)
    result["panels"] = [new_panel(index) for index in range(6)]
    panels = result["panels"]
    zone = new_zone("Zone A", panels)
    result["electrical_zones"] = [zone]
    result["frames"] = [new_frame("group-1", panels[:3], zone_ref=zone["id"]),
                        new_frame("group-2", panels[3:])]
    return validate_graph(result)


@pytest.fixture
def stringed(grouped):
    """The first group's three panels wired into one string, with the frame's sequence."""
    panels, frame = grouped["panels"][:3], grouped["frames"][0]
    string = new_string(panels)
    grouped["strings"] = [string]
    for seq, panel in enumerate(panels):
        panel["assignment"] = {"string_ref": string["id"], "seq": seq}
    for cell, assignment, panel in zip(frame["matrix"][0], frame["panel_assignments"], panels):
        cell["seq"] = assignment["seq"] = panel["assignment"]["seq"]
        assignment["string_ref"] = string["id"]
    frame["sequences"] = [{"string_ref": string["id"],
                           "ordered_panel_refs": [panel["id"] for panel in panels]}]
    return validate_graph(grouped)


def remove_from(graph, frame, refs):
    return remove.remove_panels(graph, {"expected_rev": graph["rev"], "frame_ref": frame["id"],
                                        "panel_refs": refs})


def test_a_named_panel_leaves_the_group_and_the_panel_survives(grouped):
    frame, gone = grouped["frames"][0], grouped["panels"][1]
    result = remove_from(grouped, frame, [gone["id"]])
    after = result["graph"]
    assert result["removed"] == [gone["id"]] and result["remaining"] == 2
    assert result["frame_ref"] == frame["id"]
    kept = after["frames"][0]
    assert kept["panel_refs"] == [grouped["panels"][0]["id"], grouped["panels"][2]["id"]]
    # The panel is still in the drawing, with its geometry and its zone untouched.
    panel = next(p for p in after["panels"] if p["id"] == gone["id"])
    assert panel["frame_ref"] is None and panel["matrix_cell"] is None
    assert (panel["centre"], panel["angle"]) == (gone["centre"], gone["angle"])
    assert panel["assignment"] == gone["assignment"]
    assert panel["provenance"]["last_writer"] == remove.TOOL
    assert after["electrical_zones"] == grouped["electrical_zones"]
    assert [p["id"] for p in after["panels"]] == [p["id"] for p in grouped["panels"]]
    assert after["rev"] == grouped["rev"] + 1 and after["parent_rev"] == grouped["rev"]
    assert validate_graph(after) == after


def test_every_other_group_is_byte_identical(grouped):
    frame = grouped["frames"][0]
    after = remove_from(grouped, frame, [grouped["panels"][1]["id"]])["graph"]
    assert after["frames"][1] == grouped["frames"][1]
    for panel in (grouped["panels"][0], *grouped["panels"][2:]):
        assert next(p for p in after["panels"] if p["id"] == panel["id"]) == panel


def test_the_matrix_stays_rectangular_with_an_emptied_cell(grouped):
    frame, gone = grouped["frames"][0], grouped["panels"][1]
    after = remove_from(grouped, frame, [gone["id"]])["graph"]["frames"][0]
    assert (after["module_rows"], after["module_columns"], after["module_slots"]) == (1, 3, 3)
    assert len(after["matrix"]) == 1 and len(after["matrix"][0]) == 3
    assert after["matrix"][0][1] == dict(remove.EMPTY_CELL)
    # The panels that stayed keep their own cells, so no membership silently shifts.
    assert [cell["panel_ref"] for cell in after["matrix"][0]] == [
        grouped["panels"][0]["id"], None, grouped["panels"][2]["id"]]
    assert [a["panel_ref"] for a in after["panel_assignments"]] == [
        grouped["panels"][0]["id"], grouped["panels"][2]["id"]]


def test_the_removal_survives_the_ordinary_reopen(grouped):
    after = remove_from(grouped, grouped["frames"][0], [grouped["panels"][1]["id"]])["graph"]
    reopened = deserialize_graph(serialize_graph(after))
    assert reopened == after
    assert len(reopened["frames"][0]["panel_refs"]) == 2


def test_the_callers_graph_is_never_mutated(grouped):
    before = copy.deepcopy(grouped)
    remove_from(grouped, grouped["frames"][0], [grouped["panels"][1]["id"]])
    assert grouped == before


def test_several_named_panels_go_in_one_removal(grouped):
    frame = grouped["frames"][0]
    refs = [grouped["panels"][0]["id"], grouped["panels"][2]["id"]]
    result = remove_from(grouped, frame, refs)
    after = result["graph"]["frames"][0]
    assert result["remaining"] == 1 and after["panel_refs"] == [grouped["panels"][1]["id"]]
    assert [cell["panel_ref"] for cell in after["matrix"][0]] == [None, grouped["panels"][1]["id"], None]
    assert validate_graph(result["graph"]) == result["graph"]


def test_a_panel_the_group_does_not_hold_is_refused(grouped):
    frame = grouped["frames"][0]
    before = copy.deepcopy(grouped)
    for ref in (grouped["panels"][3]["id"], new_id("panel")):
        with pytest.raises(GraphValidationError, match="PANEL_NOT_IN_GROUP"):
            remove_from(grouped, frame, [ref])
    # A refusal that also names a real member still moves nothing.
    with pytest.raises(GraphValidationError, match="PANEL_NOT_IN_GROUP"):
        remove_from(grouped, frame, [grouped["panels"][0]["id"], grouped["panels"][4]["id"]])
    assert grouped == before


def test_emptying_a_group_is_refused(grouped):
    frame = grouped["frames"][0]
    with pytest.raises(GraphValidationError, match="GROUP_WOULD_BE_EMPTY"):
        remove_from(grouped, frame, [panel["id"] for panel in grouped["panels"][:3]])


def test_an_unknown_group_is_refused(grouped):
    with pytest.raises(GraphValidationError, match="MISSING_FRAME"):
        remove.remove_panels(grouped, {"expected_rev": grouped["rev"], "frame_ref": new_id("frame"),
                                       "panel_refs": [grouped["panels"][0]["id"]]})


def test_a_stale_expected_revision_is_refused(grouped):
    with pytest.raises(GraphValidationError, match="STALE_GRAPH_REVISION"):
        remove.remove_panels(grouped, {"expected_rev": grouped["rev"] + 1,
                                       "frame_ref": grouped["frames"][0]["id"],
                                       "panel_refs": [grouped["panels"][0]["id"]]})


def test_a_stringed_panel_keeps_its_string_and_the_frame_sequence_drops_it(stringed):
    frame, gone = stringed["frames"][0], stringed["panels"][1]
    after = remove_from(stringed, frame, [gone["id"]])["graph"]
    # The string is the drawing's, not the group's: membership and order survive.
    assert after["strings"] == stringed["strings"]
    panel = next(p for p in after["panels"] if p["id"] == gone["id"])
    assert panel["assignment"] == {"string_ref": stringed["strings"][0]["id"], "seq": 1}
    sequence = after["frames"][0]["sequences"][0]
    assert sequence["ordered_panel_refs"] == [stringed["panels"][0]["id"], stringed["panels"][2]["id"]]
    assert validate_graph(after) == after


def test_a_surviving_reference_to_a_removed_panel_refuses_the_whole_removal(grouped):
    # A reference this module cannot reach: refusing beats a half-updated frame.
    grouped["frames"][0]["extra"]["pinned_panel"] = grouped["panels"][1]["id"]
    grouped = validate_graph(grouped)
    before = copy.deepcopy(grouped)
    with pytest.raises(GraphValidationError, match="PANEL_REFERENCE_NOT_CLEARED"):
        remove_from(grouped, grouped["frames"][0], [grouped["panels"][1]["id"]])
    assert grouped == before


def test_run_dispatches_remove_panels_and_refuses_any_other(grouped):
    after = remove.run(grouped, {"operation": "remove-panels", "expected_rev": grouped["rev"],
                                 "frame_ref": grouped["frames"][0]["id"],
                                 "panel_refs": [grouped["panels"][1]["id"]]})
    assert len(after["frames"][0]["panel_refs"]) == 2 and after["rev"] == grouped["rev"] + 1
    for params in ({"operation": "remove"}, {"operation": None}, {"expected_rev": 0}, ["remove-panels"]):
        with pytest.raises(GraphValidationError, match="INVALID_PANEL_REMOVE_REQUEST"):
            remove.run(grouped, params)


@pytest.mark.parametrize("overrides", [
    {},
    {"expected_rev": "0"},
    {"expected_rev": True},
    {"frame_ref": ""},
    {"frame_ref": None},
    {"panel_refs": []},
    {"panel_refs": "one"},
    {"panel_refs": [None]},
    {"unexpected": 1},
])
def test_a_malformed_request_is_refused(grouped, overrides):
    params = {"expected_rev": grouped["rev"], "frame_ref": grouped["frames"][0]["id"],
              "panel_refs": [grouped["panels"][1]["id"]]}
    params.update(overrides)
    if not overrides:
        params.pop("panel_refs")
    with pytest.raises(GraphValidationError, match="INVALID_PANEL_REMOVE_REQUEST"):
        remove.remove_panels(grouped, params)


def test_a_repeated_panel_in_one_request_is_refused(grouped):
    ref = grouped["panels"][1]["id"]
    with pytest.raises(GraphValidationError, match="INVALID_PANEL_REMOVE_REQUEST"):
        remove_from(grouped, grouped["frames"][0], [ref, ref])
