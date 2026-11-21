"""Numeric candidate decisions are deterministic and produce strict JSON."""
import json
from pathlib import Path

import pytest

import tool_loader


TOOL = next(tool for tool in json.loads(
    (Path(__file__).resolve().parents[2] / "engine" / "registry.json").read_text()
)["tools"] if tool["name"] == "count-modules")


def rectangle(width, height):
    return [[0, 0], [width, 0], [width, height], [0, height]]


@pytest.mark.parametrize("points,eligible", [
    pytest.param(rectangle(1.0, 1e-200), True, id="thin-representable"),
    pytest.param(rectangle(1e150, 1e-150), True, id="extreme-aspect-representable"),
    pytest.param(rectangle(1e150, 1e150), True, id="large-finite-area"),
    pytest.param(rectangle(1e200, 1e200), False, id="overflow-area"),
    pytest.param(rectangle(1e-200, 1e-200), False, id="underflow-area"),
    pytest.param(rectangle(10**400, 1), False, id="integer-float-overflow"),
    pytest.param([[-1e308, 0], [1e308, 0], [1e308, 1], [-1e308, 1]], False,
                 id="finite-endpoints-overflow-edge"),
])
def test_numeric_limits_are_deterministic_and_json_safe(points, eligible):
    intake = {"polylines": [{"layer": "Numeric", "closed": True, "pts": points, "handle": "A"}]}
    json.dumps(intake, allow_nan=False)
    first = tool_loader.run_tool_dynamic(TOOL, intake, {}, aps_live=False)
    second = tool_loader.run_tool_dynamic(TOOL, intake, {}, aps_live=False)
    assert first["ok"] and second["ok"]
    assert first["result"] == second["result"]
    assert len(first["result"]["sets"]) == int(eligible)
    restored = json.loads(json.dumps(first, allow_nan=False))
    assert restored == first
    if eligible:
        ids = [row["set_id"] for row in restored["result"]["sets"]]
        selected = tool_loader.run_tool_dynamic(TOOL, intake, {"set_ids": ids}, aps_live=False)
        assert selected["result"]["count"] == 1
        assert selected["overlay"]["highlight_handles"] == ["A"]
        assert json.loads(json.dumps(selected, allow_nan=False)) == selected
