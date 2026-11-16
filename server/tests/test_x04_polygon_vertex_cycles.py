"""X04 eligibility does not depend on a starting corner or coordinate-axis name."""
from copy import deepcopy
from itertools import permutations
import pytest
from test_x04_minsert_arrays import selected


@pytest.mark.parametrize("points,eligible", [
    ([[0, 0, 0], [3, 0, 0], [2, 2, 0], [0, 1, 0]], True),
    ([[0, 0, 0], [3, 0, 0], [1, 0.5, 0], [0, 2, 0]], False),
    ([[0, 0, 0], [3, 0, 0], [2, 2, 0.2], [0, 1, 0]], False),
])
def test_vertex_cycles_and_axis_permutations_preserve_eligibility(points, eligible):
    for axes in permutations(range(3)):
        transformed = [[p[axis] for axis in axes] for p in points]
        for start in range(4):
            cycle = transformed[start:] + transformed[:start]
            for corners in (cycle, list(reversed(cycle))):
                intake = {"inserts": [{"name": "Panel", "handle": "A"}],
                    "polylines": [{"layer": "Cycle", "handle": "B", "closed": True, "pts": corners}]}
                original = deepcopy(intake)
                result = selected(intake)
                assert result["ok"] and result["result"]["count"] == 1 + int(eligible)
                assert set(result["overlay"]["highlight_handles"]) == ({"A", "B"} if eligible else {"A"})
                assert intake == original
