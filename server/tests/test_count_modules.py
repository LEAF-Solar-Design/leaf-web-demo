import json
from pathlib import Path

import pytest

import importlib.util


PATH = Path(__file__).resolve().parents[1] / "builtins" / "count_modules.py"
spec = importlib.util.spec_from_file_location("module_count_builtin", PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_lists_competing_sets_and_counts_selected_inserts_only():
    intake = {"inserts": [
        {"name": "Panel A", "layer": "Design", "handle": "1"},
        {"name": "Panel A", "layer": "Design", "handle": "2"},
        {"name": "Panel B", "layer": "Alternate", "handle": "3"},
        {"name": "Panel A", "layer": "Design", "handle": "4", "space": "paper"}],
        "polylines": [{"layer": "Design", "pts": [[0, 0], [1, 1]], "handle": "5"}]}
    listing, overlay = module.run(intake, {})
    assert listing["count"] is None and listing["selection_required"]
    assert overlay is None
    assert len(listing["sets"]) == 2
    choice = next(s for s in listing["sets"] if s["layer"] == "Design")
    result, overlay = module.run(intake, {"set_id": choice["set_id"]})
    assert result["count"] == 2
    assert result["selected_set"]["block"] == "Panel A"
    assert result["selected_set"]["layer"] == "Design"
    assert overlay["highlight_handles"] == ["1", "2"]


def test_lists_closed_four_corner_polylines_but_excludes_other_entities():
    panel = {"layer": "Panels", "closed": True,
             "pts": [[0, 0], [2, 0], [2, 1], [0, 1]], "handle": "A"}
    intake = {"polylines": [panel, {**panel, "closed": False},
        {**panel, "pts": [[0, 0], [1, 0], [0, 1]]},
        {**panel, "layout": "Sheet 1", "space": "paper"}]}
    result, _ = module.run(intake, {})
    assert len(result["sets"]) == 1
    counted, _ = module.run(intake, {"set_id": result["sets"][0]["set_id"]})
    assert counted["count"] == 1
    assert counted["selected_set"]["entity_type"] == "LWPOLYLINE"


def test_rejects_stale_selection_instead_of_zero_count():
    with pytest.raises(ValueError, match="absent"):
        module.run({}, {"set_id": "stale"})


def test_one_selection_can_span_layers_without_counting_a_set_twice():
    intake = {"inserts": [
        {"layer": "Roof A", "name": "Panel", "handle": "A"},
        {"layer": "Roof B", "name": "Panel", "handle": "B"}]}
    listing, _ = module.run(intake, {})
    ids = [s["set_id"] for s in listing["sets"]]
    result, overlay = module.run(intake, {"set_ids": [*ids, ids[0]]})
    assert result["count"] == 2
    assert len(result["selected_sets"]) == 2
    assert not result["selection_required"]
    assert overlay["highlight_handles"] == ["A", "B"]
    with pytest.raises(ValueError, match="absent"):
        module.run(intake, {"set_ids": [ids[0], "stale"]})


def test_normal_registry_and_trusted_loader_resolve_builtin():
    import tool_loader
    registry = json.loads((PATH.parents[2] / "engine" / "registry.json").read_text())
    tool = next(t for t in registry["tools"] if t["name"] == "count-modules")
    assert tool_loader.resolve_local_file(tool) == PATH
    assert tool_loader.is_trusted_builtin_tool(tool)
    envelope = tool_loader.run_tool_dynamic(tool, {"inserts": []}, {}, aps_live=False)
    assert envelope["error"] is None
    assert envelope["result"]["selection_required"] is True


def test_design_window_counts_one_copy_and_records_exact_highlights_through_loader():
    import copy
    import tool_loader

    intake = {"inserts": [
        {"name": "Panel", "layer": "Design", "handle": f"{copy_index * 10 + index + 1:X}",
         "x": copy_index * 100 + index, "y": index}
        for copy_index in range(3) for index in range(4)]}
    before = copy.deepcopy(intake)
    tool = next(t for t in json.loads((PATH.parents[2] / "engine/registry.json").read_text())["tools"]
                if t["name"] == "count-modules")
    listing, _ = module.run(intake, {})
    identity = listing["sets"][0]["set_id"]
    assert module.run(intake, {"set_id": identity})[0]["count"] == 12
    params = {"set_ids": [identity], "selection_window": [99, -1, 104, 4]}
    result = tool_loader.run_tool_dynamic(tool, intake, params, aps_live=False)
    assert result["error"] is None
    assert result["result"]["count"] == 4
    assert result["result"]["selection_window"] == params["selection_window"]
    assert result["result"]["selected_set"]["count"] == 4
    assert set(result["overlay"]["highlight_handles"]) == {"B", "C", "D", "E"}
    assert intake == before


@pytest.mark.parametrize("bounds", [None, [], [0, 1, 2], [0, 0, 1, float("nan")],
    [False, 0, 1, 1], ["0", 0, 1, 1], [1, 0, 1, 2], [2, 0, 1, 2]])
def test_invalid_design_windows_refuse_atomically(bounds):
    with pytest.raises(ValueError, match="window"):
        module.run({"inserts": []}, {"selection_window": bounds})


def test_window_encloses_outlines_and_preserves_physical_handle_union():
    polyline = {"closed": True, "pts": [[0, 0], [2, 0], [2, 1], [0, 1]],
                "layer": "Design", "handle": "A"}
    intake = {"polylines": [polyline, dict(polyline),
        {**polyline, "handle": "B", "pts": [[2, 0], [4, 0], [4, 1], [2, 1]]}]}
    result, _ = module.run(intake, {})
    selected, overlay = module.run(intake, {"set_id": result["sets"][0]["set_id"],
        "selection_window": [0, 0, 2, 1]})
    assert selected["count"] == 1
    assert overlay["highlight_handles"] == ["A"]


def test_window_rejects_missing_positions_and_array_references_without_partial_results():
    for entity, message in [({"name": "Panel"}, "finite drawing position"),
        ({"name": "Panel", "x": 0, "y": 0, "columns": 3}, "individual INSERT")]:
        with pytest.raises(ValueError, match=message):
            module.run({"inserts": [{"name": "Panel", "x": 0, "y": 0}, entity]},
                       {"selection_window": [-1, -1, 1, 1]})


def test_window_with_no_selected_candidates_refuses_actionably():
    intake = {"inserts": [{"name": "Panel", "handle": "A", "x": 0, "y": 0}]}
    listing, _ = module.run(intake, {})
    with pytest.raises(ValueError, match="no candidates in this design window"):
        module.run(intake, {"set_id": listing["sets"][0]["set_id"],
                           "selection_window": [10, 10, 20, 20]})


@pytest.mark.parametrize("ordering", ["reverse", "rotate"])
def test_input_order_preserves_set_identity_counts_and_highlight_membership(ordering):
    panel = {"layer": "Design", "closed": True,
             "pts": [[0, 0], [2, 0], [2, 1], [0, 1]]}
    intake = {"inserts": [
        {"layer": "Design", "name": "Panel", "handle": "I1"},
        {"layer": "Other", "name": "Panel", "handle": "I3"},
        {"layer": "Design", "name": "Panel", "handle": "I2"}],
        "polylines": [{**panel, "handle": "P1"},
                      {**panel, "layer": "Other", "handle": "P3"},
                      {**panel, "handle": "P2"}]}
    reordered = {key: (list(reversed(rows)) if ordering == "reverse"
                       else rows[1:] + rows[:1]) for key, rows in intake.items()}
    listing, _ = module.run(intake, {})
    changed_listing, _ = module.run(reordered, {})
    assert changed_listing["sets"] == listing["sets"]
    ids = [row["set_id"] for row in listing["sets"] if row["layer"] == "Design"]
    result, overlay = module.run(intake, {"set_ids": ids})
    changed_result, changed_overlay = module.run(reordered, {"set_ids": ids})
    assert result == changed_result
    assert changed_result["count"] == 4
    assert set(overlay["highlight_handles"]) == {"I1", "I2", "P1", "P2"}
    assert set(changed_overlay["highlight_handles"]) == set(overlay["highlight_handles"])
    assert len(changed_overlay["highlight_handles"]) == 4


def test_closed_rings_and_reversed_winding_count_but_invalid_corners_do_not():
    rectangle = [[0, 0, 0], [2, 0, 0], [2, 1, 0], [0, 1, 0]]
    valid = [rectangle + [rectangle[0]], list(reversed(rectangle))]
    invalid = [
        [[0, 0], [1, 0], [2, 0], [3, 0]],
        [[0, 0], [1, 0], [2, 0], [0, 1]],
        [[0, 0], [2, 0], [2, 0], [0, 1]],
    ]
    for value in (float("nan"), float("inf"), float("-inf")):
        for axis in range(3):
            shape = [list(point) for point in rectangle]
            shape[1][axis] = value
            invalid.append(shape)
    intake = {"polylines": [
        {"layer": "Valid", "closed": True, "pts": points, "handle": f"V{i}"}
        for i, points in enumerate(valid)] + [
        {"layer": f"Invalid {i}", "closed": True, "pts": points, "handle": f"X{i}"}
        for i, points in enumerate(invalid)]}
    listing, _ = module.run(intake, {})
    assert [(row["layer"], row["count"]) for row in listing["sets"]] == [("Valid", 2)]
    result, overlay = module.run(intake, {"set_id": listing["sets"][0]["set_id"]})
    assert result["count"] == 2
    assert set(overlay["highlight_handles"]) == {"V0", "V1"}


def test_escaped_unicode_set_names_roundtrip_without_identity_collisions():
    names = [("\u5c4b\u6839", 'Panel "A"'), ('roof,"west"', r"block\part"),
             ("a,b", "c"), ("a", "b,c")]
    intake = {"inserts": [{"layer": layer, "name": block, "handle": f"H{i}-{j}"}
        for i, (layer, block) in enumerate(names) for j in range(2)]}
    listing, _ = module.run(intake, {})
    ids = [row["set_id"] for row in listing["sets"]]
    assert len(ids) == len(set(ids)) == 4
    for row in listing["sets"]:
        assert json.loads(row["set_id"]) == ["INSERT", row["layer"], row["block"]]
        counted, overlay = module.run(intake, {"set_id": row["set_id"]})
        index = names.index((row["layer"], row["block"]))
        assert counted["count"] == 2
        assert set(overlay["highlight_handles"]) == {f"H{index}-0", f"H{index}-1"}
    result, overlay = module.run(intake, {"set_ids": ids})
    assert result["count"] == 8
    assert set(overlay["highlight_handles"]) == {f"H{i}-{j}" for i in range(4) for j in range(2)}


def test_overlapping_sets_count_each_physical_handle_once_without_mutation():
    from copy import deepcopy

    panel = {"closed": True, "pts": [[0, 0], [2, 0], [2, 1], [0, 1]]}
    intake = {"inserts": [
        {"layer": "A", "name": "Panel", "handle": "A"},
        {"layer": "A", "name": "Panel", "handle": "0xa"},
        {"layer": "B", "name": "Other panel", "handle": "A"},
        {"layer": "B", "name": "Other panel", "handle": "B"},
        {"layer": "Unused", "name": "Panel", "handle": "C"}],
        "polylines": [{**panel, "layer": "P", "handle": "b"},
                      {**panel, "layer": "P", "handle": "D"}]}
    original = deepcopy(intake)
    listing, _ = module.run(intake, {})
    selected = [row["set_id"] for row in listing["sets"] if row["layer"] != "Unused"]
    result, overlay = module.run(intake, {"set_ids": [*selected, selected[0]]})
    assert result["count"] == 3
    assert set(overlay["highlight_handles"]) == {"A", "B", "D"}
    assert len(overlay["highlight_handles"]) == 3
    assert {row["layer"]: row["count"] for row in result["selected_sets"]} == {"A": 1, "B": 2, "P": 2}
    assert intake == original


def test_distinct_unidentified_entities_are_not_collapsed_by_missing_handles():
    intake = {"inserts": [{"layer": "A", "name": "Panel"},
                          {"layer": "A", "name": "Panel"}]}
    listing, _ = module.run(intake, {})
    result, overlay = module.run(intake, {"set_ids": [listing["sets"][0]["set_id"]]})
    assert result["count"] == 2
    assert overlay["highlight_handles"] == []


def test_mixed_spaces_exclude_paper_entities_from_all_module_outputs():
    panel = {"closed": True, "pts": [[0, 0], [2, 0], [2, 1], [0, 1]]}
    intake = {"inserts": [
        {"layer": "Model modules", "name": "Panel", "handle": "I0"},
        {"layer": "Model modules", "name": "Panel", "handle": "I1", "space": "model", "layout": "Model"},
        {"layer": "Paper modules", "name": "Panel", "handle": "I2", "space": "paper"},
        {"layer": "Paper modules", "name": "Panel", "handle": "I0", "layout": "Sheet 1"}],
        "polylines": [
            {**panel, "layer": "Model outlines", "handle": "P0"},
            {**panel, "layer": "Model outlines", "handle": "P1", "space": "model", "layout": "Model"},
            {**panel, "layer": "Paper outlines", "handle": "P2", "space": "paper", "layout": "Model"},
            {**panel, "layer": "Paper outlines", "handle": "P0", "layout": "Sheet 2"}]}
    listing, _ = module.run(intake, {})
    assert {(row["layer"], row["count"]) for row in listing["sets"]} == {
        ("Model modules", 2), ("Model outlines", 2)}
    result, overlay = module.run(intake, {"set_ids": [row["set_id"] for row in listing["sets"]]})
    assert result["count"] == 4
    assert set(overlay["highlight_handles"]) == {"I0", "I1", "P0", "P1"}


@pytest.mark.parametrize("scale, offset, angle", [
    (1e-6, (0.1, -0.2), 0.4),
    (1.0, (1e12, -1e12), 0.0),
    (1e6, (1e10, -1e10), 0.7853981633974483),
    (0.5, (-13.0, 8.0), 3.141592653589793),
])
def test_finite_translation_rotation_and_uniform_scale_preserve_candidates(scale, offset, angle):
    import math

    shapes = [[[0, 0], [2, 0], [2, 1], [0, 1]],
              [[0, 0], [3, 0], [2, 2], [0, 1]]]
    baseline = {"polylines": [{"layer": "Modules", "closed": True,
                               "pts": points, "handle": f"Q{i}"}
                              for i, points in enumerate(shapes)]}
    transformed = {"polylines": [{**entity, "pts": [
        [offset[0] + scale * (x * math.cos(angle) - y * math.sin(angle)),
         offset[1] + scale * (x * math.sin(angle) + y * math.cos(angle))]
        for x, y in entity["pts"]]} for entity in baseline["polylines"]]}
    expected, _ = module.run(baseline, {})
    listing, _ = module.run(transformed, {})
    assert listing["sets"] == expected["sets"]
    result, overlay = module.run(transformed, {"set_ids": [row["set_id"] for row in listing["sets"]]})
    assert result["count"] == 2
    assert set(overlay["highlight_handles"]) == {"Q0", "Q1"}
