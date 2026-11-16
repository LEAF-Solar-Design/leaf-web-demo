"""Mixed candidate kinds share a physical identity, never partial results."""
from copy import deepcopy
import pytest
from test_x04_minsert_arrays import selected


@pytest.mark.parametrize("alias", ["A", " a ", "0xa"])
def test_array_polygon_identity_conflict_refuses_entire_result(alias):
    intake = {"inserts": [{"handle": "A", "name": "Panel", "rows": 2, "columns": 3},
                           {"handle": "C", "name": "Panel"}],
              "polylines": [{"handle": alias, "closed": True,
                             "pts": [[0, 0], [2, 0], [2, 1], [0, 1]]}]}
    original = deepcopy(intake)
    outcome = selected(intake)
    assert not outcome["ok"]
    assert "conflicting array multiplicity" in outcome["error"]["message"]
    assert not outcome.get("result") and not outcome.get("overlay")
    assert intake == original


@pytest.mark.parametrize("rows,expected", [(2, 7), (1, 4)])
def test_distinct_handles_preserve_weighted_counts(rows, expected):
    intake = {"inserts": [{"handle": "A", "name": "Panel", "rows": rows, "columns": 3}],
              "polylines": [{"handle": "B", "closed": True,
                             "pts": [[0, 0], [2, 0], [2, 1], [0, 1]]}]}
    original = deepcopy(intake)
    outcome = selected(intake)
    assert outcome["ok"] and outcome["result"]["count"] == expected
    assert set(outcome["overlay"]["highlight_handles"]) == {"A", "B"}
    assert len(outcome["overlay"]["highlight_handles"]) == 2
    assert intake == original
