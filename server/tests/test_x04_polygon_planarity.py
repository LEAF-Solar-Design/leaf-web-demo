"""X04 module candidates must lie in one plane, including vertical planes."""
from copy import deepcopy

import pytest
from test_x04_minsert_arrays import selected, TOOL
import tool_loader


@pytest.mark.parametrize("points", [
    [[0, 0, 0], [2, 0, 2], [2, 1, 3], [0, 1, 1]],
    [[3, 0, 0], [3, 2, 0], [3, 2, 1], [3, 0, 1]],
    [[0, 0], [2, 0], [2, 1], [0, 1]],
])
@pytest.mark.parametrize("reverse", [False, True])
def test_planar_quads_count_once_with_stable_membership(points, reverse):
    pts = list(reversed(points)) if reverse else points
    intake = {"polylines": [{"layer": "Tilted", "handle": "A", "closed": True, "pts": pts}]}
    original = deepcopy(intake)
    first, second = selected(intake), selected(intake)
    assert first["ok"] and second["ok"]
    assert first["result"] == second["result"] and first["overlay"] == second["overlay"]
    assert first["result"]["count"] == 1
    assert first["overlay"]["highlight_handles"] == ["A"]
    assert intake == original


@pytest.mark.parametrize("twist", [1e-8, 0.1, -2])
def test_twisted_quad_is_absent_and_cannot_be_selected(twist):
    valid = {"layer": "Plane", "handle": "A", "closed": True,
             "pts": [[0, 0, 0], [2, 0, 0], [2, 1, 0], [0, 1, 0]]}
    bad = {**valid, "layer": "Twisted", "handle": "B",
           "pts": [[0, 0, 0], [2, 0, 0], [2, 1, twist], [0, 1, 0]]}
    for entities in ([valid, bad], [bad, valid]):
        listing = tool_loader.run_tool_dynamic(TOOL, {"polylines": entities}, {}, aps_live=False)
        assert [row["layer"] for row in listing["result"]["sets"]] == ["Plane"]
        counted = selected({"polylines": entities})
        assert counted["result"]["count"] == 1
        assert counted["overlay"]["highlight_handles"] == ["A"]
