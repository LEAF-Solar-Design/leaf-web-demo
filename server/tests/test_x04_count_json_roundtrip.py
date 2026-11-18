"""Strict JSON roundtrips preserve normal loader identities and physical unions."""
import json
from pathlib import Path

import tool_loader


TOOL = next(tool for tool in json.loads(
    (Path(__file__).resolve().parents[2] / "engine" / "registry.json").read_text()
)["tools"] if tool["name"] == "count-modules")


def roundtrip(value):
    return json.loads(json.dumps(value, allow_nan=False))


def test_intake_listing_and_count_envelopes_survive_json_with_exact_union():
    panel = {"layer": "Outlines", "closed": True, "pts": [[0, 0], [2, 0], [2, 1], [0, 1]]}
    intake = {"inserts": [
        {"layer": 'Roof "east"', "name": "\u5c4b\u6839\\panel", "handle": "a"},
        {"layer": 'Roof "east"', "name": "\u5c4b\u6839\\panel", "handle": "0xA"},
        {"layer": "Other", "name": "Panel", "handle": "B"}],
        "polylines": [{**panel, "handle": "b"}, {**panel, "handle": "P1"}]}
    restored_intake = roundtrip(intake)
    assert restored_intake == intake
    listed = tool_loader.run_tool_dynamic(TOOL, restored_intake, {}, aps_live=False)
    restored_listing = roundtrip(listed)
    assert restored_listing == listed
    ids = [row["set_id"] for row in restored_listing["result"]["sets"]]
    for row in restored_listing["result"]["sets"]:
        assert json.loads(row["set_id"]) == [row["entity_type"], row["layer"], row["block"]]
    counted = tool_loader.run_tool_dynamic(TOOL, restored_intake, roundtrip({"set_ids": ids}), aps_live=False)
    restored_count = roundtrip(counted)
    assert restored_count == counted
    assert restored_count["result"]["count"] == 3
    assert [row["set_id"] for row in restored_count["result"]["selected_sets"]] == ids
    assert set(restored_count["overlay"]["highlight_handles"]) == {"A", "B", "P1"}
    assert len(restored_count["overlay"]["highlight_handles"]) == 3
