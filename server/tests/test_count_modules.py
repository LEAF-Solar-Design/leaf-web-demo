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
    intake["blocks"] = {"Panel": {"base": [0, 0, 0], "count": 1, "complete": True,
        "children": [{"kind": "LWPOLYLINE", "closed": True,
                      "pts": [[0, 0], [0.1, 0], [0.1, 0.1], [0, 0.1]]}]}}
    before = copy.deepcopy(intake)
    tool = next(t for t in json.loads((PATH.parents[2] / "engine/registry.json").read_text())["tools"]
                if t["name"] == "count-modules")
    listing, _ = module.run(intake, {})
    identity = listing["sets"][0]["set_id"]
    assert module.run(intake, {"set_id": identity})[0]["count"] == 12
    params = {"set_ids": [identity], "region": [99, -1, 104, 4]}
    result = tool_loader.run_tool_dynamic(tool, intake, params, aps_live=False)
    assert result["error"] is None
    assert result["result"]["count"] == 4
    assert result["result"]["region"] == params["region"]
    assert result["result"]["selected_set"]["count"] == 4
    assert set(result["overlay"]["highlight_handles"]) == {"B", "C", "D", "E"}
    assert intake == before


def test_null_window_preserves_omission_and_lab_projection():
    import lab_tool_manifests

    intake = {"inserts": [{"name": "Panel", "layer": "Design", "handle": "A"}]}
    assert module.run(intake, {"region": None}) == module.run(intake, {})
    identity = module.run(intake, {})[0]["sets"][0]["set_id"]
    assert module.run(intake, {"set_id": identity, "region": None}) == module.run(
        intake, {"set_id": identity})
    tool = next(t for t in json.loads((PATH.parents[2] / "engine/registry.json").read_text())["tools"]
                if t["name"] == "count-modules")
    projection = lab_tool_manifests.project_tool(tool, "builtin")
    assert projection.reason is None
    assert projection.manifest["sample_input"]["region"] is None
    pin = projection.manifest["inputs"]["region"]
    assert pin["nullable"] and pin["has_default"] and pin["default"] is None


@pytest.mark.parametrize("bounds", [[],  [0, 1, 2], [0, 0, 1, float("nan")],
    [False, 0, 1, 1], ["0", 0, 1, 1], [1, 0, 1, 2], [2, 0, 1, 2]])
def test_invalid_design_windows_refuse_atomically(bounds):
    with pytest.raises(ValueError, match="window"):
        module.run({"inserts": []}, {"region": bounds})


def test_window_encloses_outlines_and_preserves_physical_handle_union():
    polyline = {"closed": True, "pts": [[0, 0], [2, 0], [2, 1], [0, 1]],
                "layer": "Design", "handle": "A"}
    intake = {"polylines": [polyline, dict(polyline),
        {**polyline, "handle": "B", "pts": [[2, 0], [4, 0], [4, 1], [2, 1]]}]}
    result, _ = module.run(intake, {})
    selected, overlay = module.run(intake, {"set_id": result["sets"][0]["set_id"],
        "region": [0, 0, 2, 1]})
    assert selected["count"] == 1
    assert overlay["highlight_handles"] == ["A"]


def test_window_rejects_missing_positions_and_array_references_without_partial_results():
    for entity, message in [({"name": "Panel"}, "finite instance geometry"),
        ({"name": "Panel", "x": 0, "y": 0, "columns": 3}, "individual INSERT")]:
        with pytest.raises(ValueError, match=message):
            intake = {"inserts": [{"name": "Panel", "x": 0, "y": 0}, entity],
                      "blocks": {"Panel": panel_definition()}}
            identity = module.run(intake, {})[0]["sets"][0]["set_id"]
            module.run(intake, {"set_ids": [identity], "region": [-1, -1, 3, 2]})


def test_window_with_no_selected_candidates_refuses_actionably():
    intake = {"inserts": [{"name": "Panel", "handle": "A", "x": 0, "y": 0}]}
    intake["blocks"] = {"Panel": panel_definition()}
    listing, _ = module.run(intake, {})
    with pytest.raises(ValueError, match="no candidates in this design window"):
        module.run(intake, {"set_id": listing["sets"][0]["set_id"],
                           "region": [10, 10, 20, 20]})


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


def panel_definition():
    return {"base": [0, 0, 0], "count": 1, "complete": True,
            "children": [{"kind": "LWPOLYLINE", "closed": True,
                          "pts": [[0, 0], [2, 0], [2, 1], [0, 1]]}]}


def region_intake(x=2, y=2):
    return {"blocks": {"Panel": panel_definition()},
            "inserts": [{"name": "Panel", "layer": "Design", "handle": "A", "x": x, "y": y, "z": 0}]}


def _dxf_panel_intake(positions):
    from dxf_intake import parse_dxf_bytes
    from intake_dxf import intake_to_dxf

    intake = region_intake()
    intake["blocks"]["Panel"]["children"][0].update(nrm=[0, 0, 1], elev=0, layer="0")
    intake["inserts"] = [
        {"name": "Panel", "layer": layer, "handle": format(index, "X"), "x": x, "y": y, "z": 0}
        for index, (layer, x, y) in enumerate(positions, start=256)
    ]
    return parse_dxf_bytes(intake_to_dxf(intake))


def run_region(intake, bounds):
    listing, _ = module.run(intake, {})
    identity = next(row["set_id"] for row in listing["sets"] if row["block"] == "Panel")
    return module.run(intake, {"set_ids": [identity], "region": bounds})


@pytest.mark.parametrize("position", [(-1, 2), (9, 2), (2, -0.5), (2, 9.5)])
def test_region_refuses_each_cut_through_edge(position):
    with pytest.raises(ValueError, match="Ambiguous region"):
        run_region(region_intake(*position), [0, 0, 10, 10])


def test_region_contains_contact_and_excludes_outside_contact():
    import copy
    intake = region_intake(0, 0)
    intake["inserts"].append({**intake["inserts"][0], "handle": "B", "x": -2, "y": 2})
    before = copy.deepcopy(intake)
    result, overlay = run_region(intake, [0, 0, 10, 10])
    assert result["count"] == 1 and result["region"] == [0, 0, 10, 10]
    assert overlay["highlight_handles"] == ["A"]
    assert intake == before


def test_region_inside_instance_refuses_even_when_all_vertices_are_outside():
    intake = region_intake(-1, -1)
    intake["inserts"][0]["scale"] = [10, 20, 1]
    with pytest.raises(ValueError, match="Ambiguous region"):
        run_region(intake, [0, 0, 10, 10])


@pytest.mark.parametrize("definition", [None,
    {"base": [0, 0, 0], "complete": False, "count": 61, "children": []},
    {"base": [0, 0, 0], "complete": True, "count": 1,
     "children": [{"kind": "OTHER", "type": "ELLIPSE"}]}])
def test_region_refuses_unknown_selected_geometry_but_whole_set_still_counts(definition):
    intake = region_intake()
    intake["blocks"] = {} if definition is None else {"Panel": definition}
    assert module.run(intake, {"set_id": module.run(intake, {})[0]["sets"][0]["set_id"]})[0]["count"] == 1
    with pytest.raises(ValueError, match="Region containment"):
        run_region(intake, [0, 0, 10, 10])


def test_unselected_unknown_definition_does_not_block_region():
    intake = region_intake()
    intake["inserts"].append({"name": "Unknown", "layer": "Alternate", "handle": "B"})
    result, overlay = run_region(intake, [0, 0, 10, 10])
    assert result["count"] == 1 and overlay["highlight_handles"] == ["A"]


def test_region_legacy_alias_and_conflicting_aliases_are_explicit():
    intake = region_intake()
    identity = module.run(intake, {})[0]["sets"][0]["set_id"]
    bounds = [0, 0, 10, 10]
    canonical = module.run(intake, {"set_ids": [identity], "region": bounds})
    assert canonical == module.run(intake, {"set_ids": [identity], "selection_window": bounds})
    assert canonical == module.run(intake, {"set_ids": [identity], "region": bounds,
                                          "selection_window": bounds})
    with pytest.raises(ValueError, match="Conflicting region"):
        module.run(intake, {"set_ids": [identity], "region": bounds,
                           "selection_window": [0, 0, 1, 1]})


def test_region_transforms_nonzero_base_rotation_reflection_and_tilted_normal():
    import math
    intake = region_intake(10, 20)
    definition = intake["blocks"]["Panel"]
    definition["base"] = [5, 7, 0]
    definition["children"][0]["pts"] = [[5, 7], [7, 7], [7, 8], [5, 8]]
    entity = intake["inserts"][0]
    entity.update(scale=[-2, 3, 1], rot=math.pi / 2, nrm=[0, 0, 1])
    points = module._insert_points(entity, intake["blocks"], {})
    for actual, expected in zip(points, [[10, 20, 0], [10, 16, 0], [7, 16, 0], [7, 20, 0]]):
        assert actual == pytest.approx(expected)
    assert run_region(intake, [6, 15, 11, 21])[0]["count"] == 1
    with pytest.raises(ValueError, match="Ambiguous region"):
        run_region(intake, [8, 15, 11, 21])
    entity.update(scale=[1, 1, 1], rot=0, nrm=[0, 1, 0])
    for actual, expected in zip(module._insert_points(entity, intake["blocks"], {}),
                                [[10, 20, 0], [8, 20, 0], [8, 20, 1], [10, 20, 1]]):
        assert actual == pytest.approx(expected)


def test_region_polyline_crossing_and_enclosing_refuse_for_both_windings():
    for points in ([[0, 0], [12, 0], [12, 2], [0, 2]],
                   [[-1, -1], [11, -1], [11, 11], [-1, 11]]):
        for vertices in (points, points[::-1]):
            intake = {"polylines": [{"closed": True, "pts": vertices, "layer": "Design", "handle": "A"}]}
            identity = module.run(intake, {})[0]["sets"][0]["set_id"]
            with pytest.raises(ValueError, match="Ambiguous region"):
                module.run(intake, {"set_ids": [identity], "region": [0, 0, 10, 10]})


def test_region_definition_points_cache_is_call_local(monkeypatch):
    intake = region_intake()
    intake["inserts"].append({**intake["inserts"][0], "handle": "B", "x": 5})
    original = module._definition_points
    calls = []
    def observed(definition):
        calls.append(definition)
        return original(definition)
    monkeypatch.setattr(module, "_definition_points", observed)
    assert run_region(intake, [0, 0, 10, 10])[0]["count"] == 2
    assert len(calls) == 1
    intake["blocks"]["Panel"]["complete"] = False
    with pytest.raises(ValueError, match="complete block"):
        run_region(intake, [0, 0, 10, 10])
    assert len(calls) == 2


def test_point_child_retains_geometry_through_normal_reader_and_emitter():
    from dxf_intake import parse_dxf_bytes
    from intake_dxf import intake_to_dxf
    intake = region_intake()
    child = intake["blocks"]["Panel"]["children"][0]
    child.update(nrm=[0, 0, 1], elev=0, layer="0")
    intake["blocks"]["Panel"]["children"].append(
        {"kind": "POINT", "layer": "0", "pt": [1, 0.5, 0]})
    intake["blocks"]["Panel"]["count"] = 2
    first = parse_dxf_bytes(intake_to_dxf(intake))
    second = parse_dxf_bytes(intake_to_dxf(first))
    assert first["blocks"]["Panel"]["complete"] is True
    assert second["blocks"]["Panel"]["complete"] is True
    assert first["blocks"]["Panel"]["children"][-1]["pt"] == [1, 0.5, 0]
    assert second["blocks"]["Panel"]["children"] == first["blocks"]["Panel"]["children"]
    assert run_region(second, [0, 0, 10, 10])[0]["count"] == 1
    second["blocks"]["Panel"]["children"][-1]["pt"] = [20, 0, 0]
    with pytest.raises(ValueError, match="Ambiguous region"):
        run_region(second, [0, 0, 10, 10])


@pytest.mark.parametrize("position", ["10\n1\n", "10\nnan\n20\n1\n"])
def test_malformed_point_child_keeps_definition_incomplete(position):
    from dxf_intake import parse_dxf_bytes
    raw = ("0\nSECTION\n2\nBLOCKS\n0\nBLOCK\n2\nPanel\n10\n0\n20\n0\n"
           "0\nPOINT\n" + position + "0\nENDBLK\n0\nENDSEC\n0\nSECTION\n2\nENTITIES\n"
           "0\nINSERT\n2\nPanel\n10\n2\n20\n2\n0\nENDSEC\n0\nEOF\n")
    intake = parse_dxf_bytes(raw.encode())
    assert intake["blocks"]["Panel"]["complete"] is False
    with pytest.raises(ValueError, match="complete block"):
        run_region(intake, [0, 0, 10, 10])


@pytest.mark.parametrize("raw_count, expected", [("0", 1), ("3", 3), ("oops", 0), ("-1", -1)])
def test_native_insert_array_defaults_are_normalized_without_hiding_invalid_counts(raw_count, expected):
    from dxf_intake import parse_dxf_bytes
    raw = ("0\nSECTION\n2\nBLOCKS\n0\nBLOCK\n2\nPanel\n10\n0\n20\n0\n"
           "0\nPOINT\n10\n0\n20\n0\n0\nENDBLK\n0\nENDSEC\n0\nSECTION\n2\nENTITIES\n"
           "0\nINSERT\n2\nPanel\n10\n2\n20\n2\n70\n" + raw_count +
           "\n71\n0\n0\nENDSEC\n0\nEOF\n")
    intake = parse_dxf_bytes(raw.encode())
    assert intake["inserts"][0]["columns"] == expected
    assert intake["inserts"][0]["rows"] == 1
    if expected <= 0:
        with pytest.raises(ValueError, match="invalid array multiplicity"):
            module.run(intake, {})


def test_group_selected_modules_by_layer_with_arrays_and_repeated_handles():
    intake = {"inserts": [
        {"name": "Panel A", "layer": "Upper", "handle": "A", "rows": 2},
        {"name": "Panel B", "layer": "Upper", "handle": "A", "rows": 2},
        {"name": "Panel A", "layer": "Lower", "handle": "B"},
        {"name": "Roof Outline", "layer": "Upper", "handle": "C"},
    ]}
    listing, _ = module.run(intake, {"group_by": "layer"})
    assert listing["selection_required"] and "counts" not in listing
    ids = [s["set_id"] for s in listing["sets"] if s["block"].startswith("Panel")]
    result, overlay = module.run(intake, {"set_ids": ids, "group_by": "layer"})
    assert result["counts"] == {"Lower": 1, "Upper": 2}
    assert result["count"] == 3
    assert set(overlay["highlight_handles"]) == {"A", "B"}


def test_named_roles_keep_competing_blocks_unselected():
    intake = {"inserts": [
        {"name": "PV_MODULE", "layer": "Roof A", "handle": "A"},
        {"name": "PV Module", "layer": "Roof A", "handle": "B"},
    ]}
    request = {"group_by": "layer", "panel_scope": "all_layers"}
    listing, overlay = module.run(intake, request)
    assert listing["selection_required"] and overlay is None
    result, _ = module.run(intake, {**request, "set_id": listing["sets"][0]["set_id"]})
    assert result["counts"] == {"Roof A": 1}

@pytest.mark.parametrize("field", ["layer", "name", "handle"])
def test_invalid_count_metadata_classified_as_user_input(field):
    import tool_loader

    tool = next(t for t in json.loads((PATH.parents[2] / "engine/registry.json").read_text())["tools"]
                if t["name"] == "count-modules")
    entity = {"layer": "East", "name": "Panel", "handle": "A"}
    params = {"group_by": "layer", "panel_scope": "all_layers"}
    invalid = {**entity, field: ["private fixture value"]}
    refused = tool_loader.run_tool_dynamic(tool, {"inserts": [invalid]}, params, aps_live=False)
    assert refused["result"] is None and refused["overlay"] is None
    error = refused["error"]
    assert error["error_code"] == "BAD_PARAMS" and error["actor"] == "user"
    assert field in error["message"] and "re-import" in error["message"]
    assert "private fixture value" not in str(refused)
    recovered = tool_loader.run_tool_dynamic(tool, {"inserts": [entity]}, params, aps_live=False)
    assert recovered["result"]["counts"] == {"East": 1}
    assert recovered["overlay"]["highlight_handles"] == ["A"]


def test_count_safe_integer_boundary_raises_declared_validation_error():
    limit = 2**53 - 1
    assert module._module_total([limit - 1, 1]) == limit
    with pytest.raises(module.InputValidationError, match="split the drawing"):
        module._module_total([limit, 1])


def test_layer_shares_use_selected_physical_total():
    intake = {"inserts": [{"name": "Panel", "layer": layer, "handle": handle, "rows": rows}
                          for layer, handle, rows in [("Roof", "A", 4), ("Roof", "A", 4),
                                                       ("Ground", "B", 2)]]}
    params = {"group_by": "layer", "panel_scope": "all_layers", "layer_shares": True}
    result, overlay = module.run(intake, params)
    assert [(row["layer"], row["numerator"], row["denominator"])
            for row in result["shares"]] == [("Ground", 2, 6), ("Roof", 4, 6)]
    assert sorted(overlay["highlight_handles"]) == ["A", "B"]
    selected, _ = module.run(intake, dict(params, layer_names=["Ground"]))
    assert selected["shares"][0]["percentage"] == 100
    empty, overlay = module.run({}, params)
    assert "shares" not in empty and overlay is None


@pytest.mark.parametrize("limit,expected", [(1, ["C"]), (2, ["C", "A", "B"]), (8, ["C", "A", "B", "D"])])
def test_layer_ranking_includes_cutoff_ties(limit, expected):
    intake = {"inserts": [{"handle": h, "layer": h, "name": "Panel", "rows": n}
                          for h, n in [("B", 3), ("D", 1), ("C", 4), ("A", 3)]]}
    result, overlay = module.run(intake, {"top_layers": limit, "panel_scope": "all_layers"})
    assert [row["layer"] for row in result["ranking"]] == expected
    assert all(row["count"] == result["counts"][row["layer"]] for row in result["ranking"])
    assert result["count"] == 11 and set(overlay["highlight_handles"]) == set("ABCD")


@pytest.mark.parametrize("counts,threshold,expected", [
    ((4, 2, 1), 50, ["A"]),
    ((4, 2, 2), 50, []),
    ((3, 3, 2), 50, []),
    ((5, 4, 3), 25, ["A", "B"]),
])
def test_strict_layer_share_threshold_and_contributing_handles(counts, threshold, expected):
    intake = {"inserts": [
        {"handle": layer, "layer": layer, "name": "Panel", "rows": count}
        for layer, count in zip("ABC", counts)
    ]}
    result, overlay = module.run(intake, {
        "group_by": "layer", "panel_scope": "all_layers", "layer_share_above": threshold,
    })
    assert result["count"] == sum(counts)
    assert [(r["layer"], r["numerator"], r["denominator"])
            for r in result["layers_above_share"]] == [
        (layer, counts["ABC".index(layer)], sum(counts)) for layer in expected
    ]
    assert overlay == {"highlight_handles": expected}


@pytest.mark.parametrize("prompt, counts, minimum, share, expected", [
    ("Count modules and highlight only layers with a physical count of at least 5 and a share above 25%.",
     (5, 4, 3), 5, 25, ["East"]),
    ("Highlight module layers with more than 40% of all modules and at least 4 modules.",
     (7, 4, 1), 4, 40, ["East"]),
    ("Show module layers that have at least 3 modules and account for more than 50% of all modules.",
     (4, 3, 1), 3, 50, []),
])
def test_highlight_intersection_of_layer_count_and_share(prompt, counts, minimum, share, expected):
    import deps
    import nl_router

    route = nl_router.classify(prompt, deps.all_tools())
    assert route["tool"] == "count-modules"
    assert route["params"] == {
        "group_by": "layer", "panel_scope": "all_layers",
        "layer_count_at_least": minimum, "layer_share_above": share,
    }
    ledger = dict(zip(("East", "West", "North"), counts))
    handles = {"East": "A1", "West": "B1", "North": "C1"}
    drawing = {"inserts": [
        {"name": "Panel", "handle": handles[layer], "layer": layer, "rows": count}
        for layer, count in ledger.items()
    ]}
    result, overlay = module.run(drawing, route["params"])
    assert result["counts"] == ledger
    assert result["count"] == sum(counts)
    assert overlay == {"highlight_handles": [handles[layer] for layer in expected]}


@pytest.mark.parametrize("prompt,expected", [
    ("Which roof layer has the most panels?", ["Roof A", "Roof C"]),
    ("Which roof layers are tied for the largest module count?", ["Roof A", "Roof C"]),
    ("Rank the top three roof layers by panel count, highest first.", ["Roof A", "Roof C", "Roof B"]),
    ("Show top 2 layers by module count.", ["Ground", "Roof A", "Roof C"]),
])
def test_rank_request_routes_and_counts_only_requested_layers(prompt, expected):
    import deps
    import nl_router

    route = nl_router.classify(prompt, deps.all_tools())
    assert route["tool"] == "count-modules"
    drawing = {"inserts": [{"name": "Panel", "layer": layer, "rows": count, "handle": handle}
                           for layer, count, handle in [("Ground", 99, "A"), ("Roof A", 7, "B"),
                                                        ("Roof B", 2, "C"), ("Roof C", 7, "D")]]}
    result, _ = module.run(drawing, route["params"])
    assert [row["layer"] for row in result["ranking"]] == expected


@pytest.mark.parametrize("prompt, expected_layers, expected_handles, expected_total", [
    ("Which roof layers are tied for the largest module count? Highlight only the tied winners.",
     ["Roof A", "Roof C"], {"B", "D"}, 16),
    ("Rank the top two layers by panel count and only highlight the ranked layers.",
     ["Ground", "Roof A", "Roof C"], {"A", "B", "D"}, 115),
    ("Show top 3 roof layers by module count, highlight only the top layers.",
     ["Roof A", "Roof C", "Roof B"], {"B", "C", "D"}, 16),
])
def test_ranked_highlight_requests_keep_ties_and_preserve_selection_total(
    prompt, expected_layers, expected_handles, expected_total
):
    import deps
    import nl_router

    route = nl_router.classify(prompt, deps.all_tools())
    assert route["tool"] == "count-modules"
    assert route["params"]["highlight_ranked_layers"] is True
    drawing = {"inserts": [{"name": "Panel", "layer": layer, "rows": count, "handle": handle}
                           for layer, count, handle in [("Ground", 99, "A"), ("Roof A", 7, "B"),
                                                        ("Roof B", 2, "C"), ("Roof C", 7, "D")]]}
    result, overlay = module.run(drawing, route["params"])
    assert result["count"] == expected_total
    assert [row["layer"] for row in result["ranking"]] == expected_layers
    assert set(overlay["highlight_handles"]) == expected_handles


@pytest.mark.parametrize("phrase,expected", [
    ("What percentage of all panels is on each layer?", {"East", "West", "North"}),
    ("Select only the west, north, and East panel sets. What percentage of that selection is on each layer?",
     {"East", "West", "North"}),
    ("Within the window (-1,-1) to (3,2), what percentage of the selected panels is on each layer?", {"West"}),
    ("How are modules distributed across layers?", {"East", "West", "North"}),
    ("Show the per-layer panel percentage", {"East", "West", "North"}),
    ("What % of panels is on each layer inside the box from (-1,-1) to (3,2)?", {"West"}),
    ("For the West and North layers, show me the layer share breakdown between (-1,-1) and (3,2).", {"West"}),
])
def test_share_request_selection_and_window(phrase, expected):
    import deps
    import nl_router
    route = nl_router.classify(phrase, deps.all_tools())
    assert route["tool"] == "count-modules"
    d = {"inserts": [dict(name="Panel", layer=layer, handle=h, x=x, y=0)
                     for layer, h, x in [("West","A",0), ("East","B",10), ("North","C",20)]],
         "blocks": {"Panel": {"complete": True, "base": [0,0,0],
                              "children": [{"kind":"LINE","pts":[[0,0,0],[1,1,0]]}]}}}
    result, _ = module.run(d, route["params"])
    assert {q["layer"] for q in result["shares"]} == expected
    assert all(q["numerator"] == 1 and q["denominator"] == len(expected) for q in result["shares"])


@pytest.mark.parametrize("prompt, counts", [
    ("Rank layers by panel count and give the cumulative share after each layer.", (6, 3, 1)),
    ("Show the running module fraction by layer, largest first.", (4, 4, 2)),
    ("Report cumulative panel percentage per layer.", (2, 2, 2)),
])
def test_cumulative_share_ledger(prompt, counts):
    import deps
    import nl_router

    labels = ["East", "West", "North"]
    drawing = {"inserts": [dict(layer=l, name="Panel", rows=n) for l, n in zip(labels, counts)]}
    route = nl_router.classify(prompt, deps.all_tools())
    assert route["tool"] == "count-modules"
    params = route["params"]
    r, _ = module.run(drawing, params)
    ordered = sorted(zip(labels, counts), key=lambda row: (-row[1], row[0]))
    assert [(q["layer"], q["numerator"], q["denominator"]) for q in r["cumulative_shares"]] == [
        (l, sum(v for _, v in ordered[:i+1]), sum(counts)) for i, (l, _) in enumerate(ordered)]
    assert "cumulative_shares" not in module.run({}, params)[0]


@pytest.mark.parametrize("target,expected", [(80, [["A", "B"]]), (40, [["A"]]), (110, [])])
def test_minimum_layer_set(target, expected):
    assert module._coverage({"A": 6, "B": 3, "C": 1}, target)["solutions"] == expected


def test_region_rank_comparison_requires_two_windows():
    with pytest.raises(ValueError, match="two windows"):
        module.run({}, {"rank_windows": []})


@pytest.mark.parametrize("template, rows, expected_ranks, expected_risers", [
    ("Compare panel layer ranks across these windows: {}.",
     [(1, 2, 3), (2, 1, 2), (3, 3, 1)],
     {"A": [3, 2, 1], "B": [2, 3, 2], "C": [1, 1, 3]}, ["A"]),
    ("Rank layers by module count over these regions: {}. Which layers keep rising?",
     [(3, 2, 1), (2, 1, 2), (1, 3, 3)],
     {"A": [1, 2, 3], "B": [2, 3, 2], "C": [3, 1, 1]}, []),
    ("Show panel layer ranking in windows {} and flag improving ranks.",
     [(1, 2, 3, 4), (4, 1, 0, 0), (3, 4, 1, 2), (2, 3, 4, 1)],
     {"A": [4, 3, 2, 1], "B": [1, 4, None, None], "C": [2, 1, 3, 2], "D": [3, 2, 1, 3]}, ["A"]),
])
def test_window_rank_series_routes_and_reports_strict_trends(template, rows, expected_ranks, expected_risers):
    import deps
    import nl_router

    windows = [[20 * i, 0, 20 * i + 18, 18] for i in range(len(rows[0]))]
    query = template.format(", ".join(f"({x1},{y1}) to ({x2},{y2})" for x1, y1, x2, y2 in windows))
    route = nl_router.classify(query, deps.all_tools())
    assert route["tool"] == "count-modules"
    assert route["params"]["rank_windows"] == windows
    outlines = []
    for row, counts in enumerate(rows):
        layer = "ABCD"[row]
        for col, count in enumerate(counts):
            for offset in range(count):
                x, y = 20 * col + 2 + 3 * offset, 2 + 3 * row
                outlines.append({
                    "layer": layer, "handle": f"{len(outlines) + 16:X}", "closed": True,
                    "pts": [[x, y], [x + 2, y], [x + 2, y + 1], [x, y + 1]],
                })
    intake = {"polylines": outlines}
    listing, _ = module.run(intake, {})
    params = dict(route["params"], set_ids=[row["set_id"] for row in listing["sets"]])
    result, overlay = module.run(intake, params)
    assert result["schema_version"] == 1
    assert result["window_ranks"] == expected_ranks
    assert result["consistently_rising_layers"] == expected_risers
    assert result["count"] == sum(map(sum, rows))
    assert set(overlay["highlight_handles"]) == {row["handle"] for row in outlines}
    pair, _ = module.run(intake, dict(params, rank_windows=windows[:2]))
    assert "consistently_rising_layers" not in pair
    assert pair["window_ranks"] == {layer: places[:2] for layer, places in expected_ranks.items()}


@pytest.mark.parametrize("prompt,counts,excluded,expected", [
    ("For each roof window {}, exclude the two highest distinct physical module count tiers "
     "and rerank the surviving layers.",
     [(5, 4, 4, 2, 1), (3, 1, 6, 6, 3)], 2,
     {"B": [None, 1], "D": [1, None], "E": [2, None]}),
    ("Compare module count ranks by layer in windows {}. Drop the top 1 count tier in each window.",
     [(8, 6, 6, 4, 0), (2, 7, 7, 3, 3)], 1,
     {"A": [None, 3], "B": [1, None], "C": [1, None], "D": [3, 1], "E": [None, 1]}),
    ("Rank panel layers across windows {}. Omit three highest distinct panel count tiers.",
     [(4, 3, 2, 0, 0), (5, 4, 3, 2, 2), (1, 2, 3, 4, 5)], 3,
     {"A": [None, None, 2], "B": [None, None, 1], "D": [None, 1, None], "E": [None, 1, None]}),
])
def test_exclude_highest_count_tiers_per_window_from_dxf(prompt, counts, excluded, expected):
    import deps
    import nl_router

    windows = [[20 * index, 0, 20 * index + 18, 18] for index in range(len(counts))]
    intake = _dxf_panel_intake(
        (layer, left + 2 * column + 1, 2 * row + 1)
        for (left, _, _, _), window_counts in zip(windows, counts)
        for row, (layer, count) in enumerate(zip("ABCDE", window_counts))
        for column in range(count)
    )
    query = prompt.format(", ".join(f"({x1},{y1}) to ({x2},{y2})" for x1, y1, x2, y2 in windows))
    route = nl_router.classify(query, deps.all_tools())
    assert route["tool"] == "count-modules"
    assert route["params"]["rank_windows"] == windows
    assert route["params"]["exclude_highest_count_tiers"] == excluded
    result, _ = module.run(intake, route["params"])
    assert result["window_ranks"] == expected
    assert result["count"] == sum(map(sum, counts))


@pytest.mark.parametrize("params", [
    {"exclude_highest_count_tiers": 2},
    {"rank_windows": [[0, 0, 2, 2], [3, 3, 5, 5]], "exclude_highest_count_tiers": 0},
    {"rank_windows": [[0, 0, 2, 2], [3, 3, 5, 5]], "exclude_highest_count_tiers": True},
])
def test_highest_count_tier_exclusion_requires_valid_window_request(params):
    with pytest.raises(ValueError, match="positive number of count tiers"):
        module.run({}, params)


def test_empty_selection_has_no_percentage_point_delta():
    d = {"inserts": [{"layer": "W", "name": "Module", "handle": "A"}]}
    r, _ = module.run(d, {"compare_selections": {"by": "block", "before": [], "after": ["Module"]}})
    assert r["share_change"] == {"totals": [0, 1], "percentage_points": {"W": None}, "defined": False}


@pytest.mark.parametrize("left,right,expected", [
    ({"A": 1}, {"B": 2}, {"A": (-100, 1), "B": (100, 1)}),
    ({"A": 1, "B": 2}, {"A": 1, "C": 1}, {"A": (50, 3), "B": (-200, 3), "C": (50, 1)}),
    ({"W": 3, "w": 1}, {"W": 1, "w": 1}, {"W": (-25, 1), "w": (25, 1)}),
])
def test_share_change_by_literal_layer(left, right, expected):
    inserts = []
    for name, counts in (("Before", left), ("After", right)):
        for layer, count in counts.items():
            inserts.append({"handle": str(len(inserts) + 1), "name": name, "layer": layer, "rows": count})
    result, _ = module.run({"inserts": inserts}, {"compare_selections": {
        "by": "block", "before": ["Before"], "after": ["After"]}})
    changes = result["share_change"]["percentage_points"]
    assert {k: (v["numerator"], v["denominator"]) for k, v in changes.items()} == expected


@pytest.mark.parametrize("wire_version", ["absent", 1, 2, None, True, "1"])
@pytest.mark.parametrize("params", [
    {"compare_selections": {"by": "block", "before": [], "after": ["Panel"]}},
    {"rank_windows": [[0, 0, 2, 2], [3, 3, 5, 5]]},
])
def test_comparison_schema_at_broker_reader(monkeypatch, params, wire_version):
    from unittest.mock import Mock
    import broker_client

    drawing = {"inserts": [{"name": "Panel", "handle": "1", "layer": "Roof"}]}
    result, overlay = module.run(drawing, params)
    assert result["schema_version"] == 1
    if wire_version == "absent":
        del result["schema_version"]
    else:
        result["schema_version"] = wire_version
    envelope = {"ok": True, "result": result, "overlay": overlay, "version": "1.0"}
    response = Mock(status_code=200)
    response.json.return_value = json.loads(json.dumps(envelope))
    monkeypatch.setattr(broker_client.requests, "post", lambda *args, **kwargs: response)
    got = broker_client.run_via_broker("fixture", {"name": "count-modules"}, params, "roof", False)
    if wire_version == "absent" or type(wire_version) is int and wire_version == 1:
        assert got == envelope
    else:
        assert got["ok"] is False and got["result"] is None and got["overlay"] is None
        assert "Unsupported module comparison schema_version" in got["error"]["message"]
@pytest.mark.parametrize(("prompt", "value"), [("count modules 12", 12), ("show modules 120", 120)])
def test_plain_count_prompt_keeps_legacy_numeric_binding(prompt, value):
    import deps
    import nl_router

    route = nl_router.classify(prompt, deps.all_tools())
    assert route["tool"] == "count-modules"
    assert route["params"] == {"coverage_percent": value}
    module.run({"inserts": [], "polylines": []}, route["params"])


@pytest.mark.parametrize(
    ("prompt", "excluded", "solutions"),
    [
        (
            'Find the fewest layers covering at least 45% of the physical modules after excluding layer "Roof A".',
            ["Roof A"],
            [["Roof B", "Roof C"], ["Roof B", "Roof D"], ["Roof C", "Roof D"], ["Roof C", "Roof E"]],
        ),
        (
            'Find the smallest set of layers reaching 70 percent of the modules without layers "roof a" and "Roof E".',
            ["roof a", "Roof E"],
            [["Roof C", "Roof D"]],
        ),
        (
            "Find the minimum number of layers covering 100% of the panels, omitting Roof A, Roof C, and Roof E.",
            ["Roof A", "Roof C", "Roof E"],
            [["Roof B", "Roof D"]],
        ),
    ],
)
def test_minimum_coverage_excludes_named_layers(prompt, excluded, solutions):
    import deps
    import nl_router

    counts = {"Roof A": 8, "Roof B": 5, "Roof C": 10, "Roof D": 8, "Roof E": 4}
    intake = _dxf_panel_intake(
        (layer, 2 * column, 2 * row)
        for row, (layer, count) in enumerate(counts.items())
        for column in range(count)
    )

    route = nl_router.classify(prompt, deps.all_tools())
    assert route["tool"] == "count-modules"
    assert route["params"]["coverage_excluded_layers"] == excluded
    result, _ = module.run(intake, route["params"])
    assert result["counts"] == counts
    assert result["coverage"] == {"reachable": True, "solutions": solutions}
