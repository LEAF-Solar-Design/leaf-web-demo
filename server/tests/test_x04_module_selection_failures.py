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


def test_mixed_known_unknown_selection_never_returns_partial_count():
    listing = tool_loader.run_tool_dynamic(TOOL, INTAKE, {}, aps_live=False)
    selected = listing["result"]["sets"][0]["set_id"]
    result = tool_loader.run_tool_dynamic(
        TOOL, INTAKE, {"set_ids": [selected, "unknown-set"]}, aps_live=False)
    assert not result["ok"]
    assert "absent" in result["error"]["message"]
    assert "list the sets again" in result["error"]["message"]
    assert result.get("result") in (None, {})
    assert not result.get("overlay")
