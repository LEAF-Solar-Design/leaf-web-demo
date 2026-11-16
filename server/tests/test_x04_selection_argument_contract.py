"""Nonempty set_ids wins; empty/omitted arrays fall back to set_id.

Without either effective selector, the tool lists candidates without counting.
"""
import json
from pathlib import Path

import pytest

import tool_loader


TOOL = next(tool for tool in json.loads(
    (Path(__file__).resolve().parents[2] / "engine" / "registry.json").read_text()
)["tools"] if tool["name"] == "count-modules")
INTAKE = {"inserts": [{"layer": layer, "name": "Panel", "handle": layer}
                      for layer in ("A", "B")]}


@pytest.mark.parametrize("selectors,count,layers", [
    ({}, None, []),
    ({"set_ids": []}, None, []),
    ({"set_id": ""}, None, []),
    ({"set_id": "A"}, 1, ["A"]),
    ({"set_id": "A", "set_ids": []}, 1, ["A"]),
    ({"set_id": "A", "set_ids": ["B"]}, 1, ["B"]),
    ({"set_id": "stale", "set_ids": ["B"]}, 1, ["B"]),
    ({"set_ids": ["A", "A", "B", "B"]}, 2, ["A", "B"]),
])
def test_selector_precedence_empty_listing_and_repetition(selectors, count, layers):
    listing = tool_loader.run_tool_dynamic(TOOL, INTAKE, {}, aps_live=False)
    ids = {row["layer"]: row["set_id"] for row in listing["result"]["sets"]}
    params = {key: ([ids.get(value, value) for value in values] if isinstance(values, list)
                    else ids.get(values, values)) for key, values in selectors.items()}
    envelope = tool_loader.run_tool_dynamic(TOOL, INTAKE, params, aps_live=False)
    assert envelope["ok"] and envelope["error"] is None
    assert envelope["result"]["count"] == count
    assert envelope["result"]["selection_required"] == (count is None)
    assert [row["layer"] for row in envelope["result"]["selected_sets"]] == layers
    if count is None:
        assert not envelope.get("overlay")
    else:
        assert set(envelope["overlay"]["highlight_handles"]) == set(layers)
