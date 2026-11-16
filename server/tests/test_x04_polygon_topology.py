"""X04 module silhouettes require strict convex, simple quadrilaterals."""
from copy import deepcopy
import pytest
from test_x04_minsert_arrays import selected


@pytest.mark.parametrize("points,eligible", [
    ([[0, 0], [3, 0], [2, 2], [0, 1]], True),
    ([[0, 0], [3, 0], [1, 0.5], [0, 2]], False),
    ([[0, 0], [3, 2], [0, 2], [2, 0]], False),
    ([[0, 0], [3, 0], [1, 0], [1, 2]], False),
])
@pytest.mark.parametrize("reverse", [False, True])
def test_topology_controls_eligibility_and_exact_highlights(points, eligible, reverse):
    quad = {"layer": "Candidate", "closed": True, "handle": "B",
            "pts": list(reversed(points)) if reverse else points}
    intake = {"inserts": [{"layer": "Control", "handle": "A", "name": "Panel"}],
              "polylines": [quad]}
    original = deepcopy(intake)
    counted = selected(intake)
    assert counted["ok"] and counted["result"]["count"] == 1 + int(eligible)
    assert set(counted["overlay"]["highlight_handles"]) == ({"A", "B"} if eligible else {"A"})
    assert intake == original
