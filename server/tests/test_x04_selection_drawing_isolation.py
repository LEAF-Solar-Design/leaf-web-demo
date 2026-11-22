"""A listed set from one synthetic drawing cannot select a different set."""
import json
from pathlib import Path

import pytest

import tool_loader


TOOL = next(tool for tool in json.loads(
    (Path(__file__).resolve().parents[2] / "engine" / "registry.json").read_text()
)["tools"] if tool["name"] == "count-modules")


@pytest.mark.parametrize("source,target", [("East", "West"), ("West", "East")])
def test_foreign_drawing_set_ids_are_rejected_without_partial_results(source, target):
    drawings = {name: {"inserts": [{"layer": name, "name": f"{name} panel", "handle": "A"}]}
                for name in ("East", "West")}
    listing = tool_loader.run_tool_dynamic(TOOL, drawings[source], {}, aps_live=False)
    foreign_id = listing["result"]["sets"][0]["set_id"]
    own_listing = tool_loader.run_tool_dynamic(TOOL, drawings[target], {}, aps_live=False)
    own_id = own_listing["result"]["sets"][0]["set_id"]
    assert foreign_id != own_id
    for ids in ([foreign_id], [own_id, foreign_id]):
        result = tool_loader.run_tool_dynamic(TOOL, drawings[target], {"set_ids": ids}, aps_live=False)
        assert not result["ok"]
        assert "absent" in result["error"]["message"]
        assert result.get("result") in (None, {})
        assert not result.get("overlay")
