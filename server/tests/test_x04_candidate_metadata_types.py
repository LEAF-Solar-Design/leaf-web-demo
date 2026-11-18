"""Saved identity fields must be strings before grouping or normalization."""
from copy import deepcopy
import json
import pytest
import tool_loader
from test_x04_minsert_arrays import TOOL


@pytest.mark.parametrize("key", ["layer", "name", "handle"])
@pytest.mark.parametrize("value", [[], ["bad"], {}, {"bad": 1}, False, True, 0, 4.2])
def test_nonstring_identity_fields_refuse_without_mutation_or_partial_counts(key, value):
    intake = json.loads(json.dumps({"inserts": [{"handle": "A", "name": "Panel"},
        {"layer": "Design", "name": "Panel", "handle": "B", key: value}]}))
    original = deepcopy(intake)
    outcomes = [tool_loader.run_tool_dynamic(TOOL, intake, {}, aps_live=False) for _ in range(2)]
    for outcome in outcomes:
        assert not outcome["ok"]
        assert key in outcome["error"]["message"] and "re-import" in outcome["error"]["message"]
        assert not outcome.get("result") and not outcome.get("overlay")
    assert outcomes[0]["error"] == outcomes[1]["error"] and intake == original
