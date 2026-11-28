"""Synthetic failures through the same trusted loader used by normal runs."""
import json
from pathlib import Path

import pytest

import tool_loader


TOOL = next(tool for tool in json.loads(
    (Path(__file__).resolve().parents[2] / "engine" / "registry.json").read_text()
)["tools"] if tool["name"] == "count-modules")
INTAKE = {"inserts": [{"layer": "Design", "name": "Panel", "handle": "A"}]}


@pytest.mark.parametrize("malformed", ["not-an-array", 12, {}, None, [12], [["nested"]]])
def test_malformed_selection_has_actionable_error_without_partial_count(malformed):
    assert tool_loader.is_trusted_builtin_tool(TOOL)
    result = tool_loader.run_tool_dynamic(
        TOOL, INTAKE, {"set_ids": malformed}, aps_live=False)
    assert not result["ok"]
    assert result["error"]["error_code"] == "BAD_PARAMS"
    assert "set_ids" in result["error"]["message"]
    assert result.get("result") in (None, {})
    assert not result.get("overlay")


@pytest.mark.parametrize(
    "params, cause",
    [
        pytest.param(
            {"set_id": "stale"}, "Selected module set is absent from this drawing. List the sets again.", id="stale-set"
        ),
        pytest.param(
            {"set_ids": ["current", "stale"]},
            "Selected module set is absent from this drawing. List the sets again.",
            id="mixed-current-and-stale-sets",
        ),
        pytest.param(
            {"set_id": "current", "region": [10, 0, 0, 10]},
            "minimums below maximums",
            id="reversed-window",
        ),
        pytest.param(
            {"region": [0, 0, 10, 10]},
            "Select module sets before applying",
            id="window-without-set",
        ),
    ],
)
def test_count_modules_explains_selection_errors_and_accepts_correction(params, cause):
    listing = tool_loader.run_tool_dynamic(TOOL, INTAKE, {}, aps_live=False)
    set_id = listing["result"]["sets"][0]["set_id"]
    params = dict(params)
    if params.get("set_id") == "current":
        params["set_id"] = set_id
    if "set_ids" in params:
        params["set_ids"] = [set_id if value == "current" else value for value in params["set_ids"]]

    refused = tool_loader.run_tool_dynamic(TOOL, INTAKE, params, aps_live=False)
    assert refused["result"] is None
    assert refused["overlay"] is None
    error = refused["error"]
    assert error["error_code"] == "BAD_PARAMS"
    assert error["actor"] == "user"
    assert cause in error["message"]
    assert error["next_action"]
    assert "support" not in error["next_action"].lower()

    recovered = tool_loader.run_tool_dynamic(TOOL, INTAKE, {"set_id": set_id}, aps_live=False)
    assert recovered["error"] is None
    assert recovered["result"]["count"] == 1
    assert recovered["overlay"]["highlight_handles"] == ["A"]
