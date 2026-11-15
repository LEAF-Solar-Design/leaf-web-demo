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
