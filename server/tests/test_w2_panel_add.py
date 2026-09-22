"""The panel-add builtin: one group claims the named panels, nothing else moves.

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


add = builtin("solar_panel_add")


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


def new_frame(name, panels, zone_ref=None, gaps=0):
    """One frame over a single row, with `gaps` empty slots ahead of the panels.

    A leading empty slot is the shape a removal leaves behind, so a frame built
    with one proves the claim reuses the slot instead of growing the matrix.
    """
    cells = [dict(add.EMPTY_CELL) for _ in range(gaps)]
    cells.extend({"code": "panel", "panel_ref": panel["id"], "seq": None, "inverter_id": None,
                  "string_input_number": None, "x": panel["centre"][0], "y": panel["centre"][1],
                  "angle": panel["angle"]} for panel in panels)
    frame = {
        "id": new_id("frame"), "kind": "frame", "rev": 0, "extra": {},
        "validity": {"state": "valid", "reasons": []}, "provenance": provenance(),
        "name": name, "insertion_point": panels[0]["centre"][:], "installation_design": "Roof",
        "panel_refs": [panel["id"] for panel in panels], "module_rows": 1,
        "module_columns": len(cells), "module_slots": len(cells), "module_power_watts": 0,
        "module_width_along_row": 77.0, "module_height_across_row": 38.5,
        "electrical_zone_ref": zone_ref, "sequences": [], "matrix": [cells],
        "panel_assignments": [{"panel_ref": panel["id"], "string_ref": None, "seq": None,
                               "inverter_id": None, "string_input_number": None} for panel in panels],
    }
    for column, panel in enumerate(panels):
        panel["frame_ref"] = frame["id"]
        panel["matrix_cell"] = {"row": 0, "col": column + gaps}
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
    """Eight panels: a full group of three, a group of three with an emptied slot, two free."""
    result = new_empty_graph(tenant_id="studio-panel-add", drawing_id="w2-" + "a" * 16,
                             source_hash="a" * 64, units=copy.deepcopy(UNITS), created_at=CREATED_AT)
    result["panels"] = [new_panel(index) for index in range(8)]
    panels = result["panels"]
    zone = new_zone("Zone A", panels[:6])
    result["electrical_zones"] = [zone]
    result["frames"] = [new_frame("group-1", panels[:3], zone_ref=zone["id"]),
                        new_frame("group-2", panels[3:6], gaps=1)]
    return validate_graph(result)


@pytest.fixture
def wired(grouped):
    """One circuit over an ungrouped panel, a member of group-1, and another free panel."""
    panels = grouped["panels"]
    ordered = [panels[6], panels[0], panels[7]]
    string = new_string(ordered)
    grouped["strings"] = [string]
    for seq, panel in enumerate(ordered):
        panel["assignment"] = {"string_ref": string["id"], "seq": seq}
    frame = grouped["frames"][0]
    frame["matrix"][0][0]["seq"] = 1
    frame["panel_assignments"][0].update(string_ref=string["id"], seq=1)
    frame["sequences"] = [{"string_ref": string["id"], "ordered_panel_refs": [panels[0]["id"]]}]
    return validate_graph(grouped)


def add_to(graph, frame, refs):
    return add.add_panels(graph, {"expected_rev": graph["rev"], "frame_ref": frame["id"],
                                  "panel_refs": refs})


def test_a_claimed_panel_joins_the_group_and_the_drawing_is_otherwise_unchanged(grouped):
    frame, claimed = grouped["frames"][0], grouped["panels"][6]
    result = add_to(grouped, frame, [claimed["id"]])
    after = result["graph"]
    assert result["added"] == [claimed["id"]] and result["total"] == 4
    assert result["frame_ref"] == frame["id"]
    joined = after["frames"][0]
    # The plugin appends the claimed panel at the END of the membership it printed.
    assert joined["panel_refs"] == [*frame["panel_refs"], claimed["id"]]
    panel = next(p for p in after["panels"] if p["id"] == claimed["id"])
    assert panel["frame_ref"] == frame["id"] and panel["matrix_cell"] is not None
    assert (panel["centre"], panel["angle"]) == (claimed["centre"], claimed["angle"])
    assert panel["assignment"] == claimed["assignment"]
    assert panel["provenance"]["last_writer"] == add.TOOL
    # The zone is the zone capability's state: claiming a panel never joins it.
    assert after["electrical_zones"] == grouped["electrical_zones"]
    assert claimed["id"] not in after["electrical_zones"][0]["panel_refs"]
    assert [p["id"] for p in after["panels"]] == [p["id"] for p in grouped["panels"]]
    assert after["rev"] == grouped["rev"] + 1 and after["parent_rev"] == grouped["rev"]
    assert validate_graph(after) == after


def test_every_other_group_and_every_other_panel_is_byte_identical(grouped):
    after = add_to(grouped, grouped["frames"][0], [grouped["panels"][6]["id"]])["graph"]
    assert after["frames"][1] == grouped["frames"][1]
    for panel in (*grouped["panels"][:6], grouped["panels"][7]):
        assert next(p for p in after["panels"] if p["id"] == panel["id"]) == panel


def test_the_matrix_grows_by_a_whole_row_when_the_group_is_full(grouped):
    frame, claimed = grouped["frames"][0], grouped["panels"][6]
    after = add_to(grouped, frame, [claimed["id"]])["graph"]["frames"][0]
    assert (after["module_rows"], after["module_columns"], after["module_slots"]) == (2, 3, 6)
    assert len(after["matrix"]) == 2 and all(len(row) == 3 for row in after["matrix"])
    # The panels already placed keep their own cells: no membership silently shifts.
    assert after["matrix"][0] == grouped["frames"][0]["matrix"][0]
    assert after["matrix"][1][0] == {"code": "panel", "panel_ref": claimed["id"], "seq": None,
                                     "inverter_id": None, "string_input_number": None,
                                     "x": claimed["centre"][0], "y": claimed["centre"][1],
                                     "angle": claimed["angle"]}
    assert after["matrix"][1][1:] == [dict(add.EMPTY_CELL)] * 2
    assert after["panel_assignments"][-1] == {"panel_ref": claimed["id"], "string_ref": None,
                                              "seq": None, "inverter_id": None,
                                              "string_input_number": None}


def test_an_emptied_slot_is_reused_before_the_matrix_grows(grouped):
    frame, claimed = grouped["frames"][1], grouped["panels"][6]
    after = add_to(grouped, frame, [claimed["id"]])["graph"]["frames"][1]
    # The group carries one emptied slot, so the claim costs no new row.
    assert (after["module_rows"], after["module_columns"], after["module_slots"]) == (1, 4, 4)
    assert [cell["panel_ref"] for cell in after["matrix"][0]] == [
        claimed["id"], *[panel["id"] for panel in grouped["panels"][3:6]]]
    assert not [cell for row in after["matrix"] for cell in row if cell["panel_ref"] is None]


def test_several_named_panels_go_in_one_claim(grouped):
    frame = grouped["frames"][0]
    refs = [grouped["panels"][7]["id"], grouped["panels"][6]["id"]]
    result = add_to(grouped, frame, refs)
    after = result["graph"]["frames"][0]
    assert result["total"] == 5 and after["panel_refs"] == [*frame["panel_refs"], *refs]
    assert (after["module_rows"], after["module_columns"], after["module_slots"]) == (2, 3, 6)
    assert [cell["panel_ref"] for cell in after["matrix"][1]] == [*refs, None]
    assert validate_graph(result["graph"]) == result["graph"]


def test_the_add_survives_the_ordinary_reopen(grouped):
    after = add_to(grouped, grouped["frames"][0], [grouped["panels"][6]["id"]])["graph"]
    reopened = deserialize_graph(serialize_graph(after))
    assert reopened == after
    assert len(reopened["frames"][0]["panel_refs"]) == 4


def test_the_callers_graph_is_never_mutated(grouped):
    before = copy.deepcopy(grouped)
    add_to(grouped, grouped["frames"][0], [grouped["panels"][6]["id"]])
    assert grouped == before


def test_a_panel_the_kernel_would_never_group_here_is_still_claimed(grouped):
    """Membership is explicit: the named group receives the panel, geometry or not."""
    frame, claimed = grouped["frames"][0], grouped["panels"][7]
    assert claimed["centre"][0] - grouped["panels"][2]["centre"][0] == 500.0
    after = add_to(grouped, frame, [claimed["id"]])["graph"]["frames"][0]
    assert claimed["id"] in after["panel_refs"]
    # Nothing is re-derived: the frame's own placement and dimensions stand.
    assert after["insertion_point"] == frame["insertion_point"]
    assert (after["module_width_along_row"], after["module_height_across_row"]) == (77.0, 38.5)


def test_a_panel_already_in_another_group_is_refused(grouped):
    before = copy.deepcopy(grouped)
    with pytest.raises(GraphValidationError, match="PANEL_ALREADY_IN_GROUP"):
        add_to(grouped, grouped["frames"][0], [grouped["panels"][3]["id"]])
    # A refusal that also names a free panel still moves nothing.
    with pytest.raises(GraphValidationError, match="PANEL_ALREADY_IN_GROUP"):
        add_to(grouped, grouped["frames"][0], [grouped["panels"][6]["id"], grouped["panels"][4]["id"]])
    assert grouped == before


def test_a_panel_already_in_this_group_is_refused(grouped):
    before = copy.deepcopy(grouped)
    with pytest.raises(GraphValidationError, match="PANEL_ALREADY_IN_GROUP"):
        add_to(grouped, grouped["frames"][0], [grouped["panels"][1]["id"]])
    assert grouped == before


def test_an_unknown_panel_is_refused(grouped):
    before = copy.deepcopy(grouped)
    with pytest.raises(GraphValidationError, match="MISSING_PANEL"):
        add_to(grouped, grouped["frames"][0], [new_id("panel")])
    assert grouped == before


def test_an_unknown_group_is_refused(grouped):
    with pytest.raises(GraphValidationError, match="MISSING_FRAME"):
        add.add_panels(grouped, {"expected_rev": grouped["rev"], "frame_ref": new_id("frame"),
                                 "panel_refs": [grouped["panels"][6]["id"]]})


def test_a_stale_expected_revision_is_refused(grouped):
    with pytest.raises(GraphValidationError, match="STALE_GRAPH_REVISION"):
        add.add_panels(grouped, {"expected_rev": grouped["rev"] + 1,
                                 "frame_ref": grouped["frames"][0]["id"],
                                 "panel_refs": [grouped["panels"][6]["id"]]})


def test_a_wired_panel_brings_its_circuit_into_the_frame_sequence(wired):
    frame, claimed = wired["frames"][0], wired["panels"][6]
    after = add_to(wired, frame, [claimed["id"]])["graph"]
    # The circuit is the drawing's, not the group's: membership and order survive.
    assert after["strings"] == wired["strings"]
    panel = next(p for p in after["panels"] if p["id"] == claimed["id"])
    assert panel["assignment"] == {"string_ref": wired["strings"][0]["id"], "seq": 0}
    joined = after["frames"][0]
    assert joined["sequences"] == [{"string_ref": wired["strings"][0]["id"],
                                    "ordered_panel_refs": [claimed["id"], wired["panels"][0]["id"]]}]
    assert joined["matrix"][1][0]["seq"] == 0
    assert joined["panel_assignments"][-1] == {"panel_ref": claimed["id"],
                                               "string_ref": wired["strings"][0]["id"], "seq": 0,
                                               "inverter_id": None, "string_input_number": None}
    assert validate_graph(after) == after


def test_a_wired_panel_takes_the_circuits_order_not_the_selection_order(wired):
    frame, panels = wired["frames"][0], wired["panels"]
    # Selected last-first; the frame sequence still runs the way the circuit does.
    after = add_to(wired, frame, [panels[7]["id"], panels[6]["id"]])["graph"]
    assert after["frames"][0]["sequences"] == [{
        "string_ref": wired["strings"][0]["id"],
        "ordered_panel_refs": [panels[6]["id"], panels[0]["id"], panels[7]["id"]]}]
    assert after["frames"][0]["panel_refs"][-2:] == [panels[7]["id"], panels[6]["id"]]
    assert validate_graph(after) == after


def test_run_dispatches_add_panels_and_refuses_any_other(grouped):
    after = add.run(grouped, {"operation": "add-panels", "expected_rev": grouped["rev"],
                              "frame_ref": grouped["frames"][0]["id"],
                              "panel_refs": [grouped["panels"][6]["id"]]})
    assert len(after["frames"][0]["panel_refs"]) == 4 and after["rev"] == grouped["rev"] + 1
    for params in ({"operation": "add"}, {"operation": None}, {"expected_rev": 0}, ["add-panels"]):
        with pytest.raises(GraphValidationError, match="INVALID_PANEL_ADD_REQUEST"):
            add.run(grouped, params)


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
              "panel_refs": [grouped["panels"][6]["id"]]}
    params.update(overrides)
    if not overrides:
        params.pop("panel_refs")
    with pytest.raises(GraphValidationError, match="INVALID_PANEL_ADD_REQUEST"):
        add.add_panels(grouped, params)


def test_a_repeated_panel_in_one_request_is_refused(grouped):
    ref = grouped["panels"][6]["id"]
    with pytest.raises(GraphValidationError, match="INVALID_PANEL_ADD_REQUEST"):
        add_to(grouped, grouped["frames"][0], [ref, ref])
