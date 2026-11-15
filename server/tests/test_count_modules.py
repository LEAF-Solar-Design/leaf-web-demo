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
