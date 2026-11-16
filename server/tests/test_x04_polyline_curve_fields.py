"""X04 count excludes curved/extruded silhouettes, preserving straight WCS planes."""
from copy import deepcopy
import pytest
from test_x04_minsert_arrays import selected


@pytest.mark.parametrize("fields,eligible", [
    ({}, True), ({"bulges": [0, 0, 0, 0], "thickness": 0}, True),
    ({"bulges": [0.2, 0, 0, 0]}, False), ({"bulges": [0, 0, 0, -0.1]}, False),
    ({"bulges": [float("nan")]}, False), ({"bulges": "0"}, False),
    ({"thickness": 0.1}, False), ({"thickness": -1}, False),
    ({"normal": [0, 0, -1]}, True), ({"normal": [0, 0, 0]}, False),
    ({"normal": [0, float("inf"), 1]}, False),
])
def test_curve_metadata_controls_eligibility_without_input_mutation(fields, eligible):
    intake = {"inserts": [{"handle": "A", "name": "Panel"}], "polylines": [
        {"handle": "B", "closed": True, "pts": [[0, 0], [2, 0], [2, 1], [0, 1]], **fields}]}
    original = deepcopy(intake)
    outcome = selected(intake)
    assert outcome["ok"] and outcome["result"]["count"] == 1 + int(eligible)
    assert set(outcome["overlay"]["highlight_handles"]) == ({"A", "B"} if eligible else {"A"})
    # NaN cannot compare equal, so compare unchanged object representations.
    assert repr(intake) == repr(original)


def test_tilted_wcs_quad_with_extrusion_normal_remains_eligible():
    intake = {"polylines": [{"handle": "C", "closed": True, "normal": [-1, 0, 1],
        "pts": [[0, 0, 0], [2, 0, 2], [2, 1, 2], [0, 1, 0]]}]}
    outcome = selected(intake)
    assert outcome["result"]["count"] == 1 and outcome["overlay"]["highlight_handles"] == ["C"]
