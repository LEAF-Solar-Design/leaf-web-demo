"""Every malformed selector type rejects through the trusted loader."""
from copy import deepcopy
import pytest
import tool_loader
from test_x04_minsert_arrays import TOOL


@pytest.mark.parametrize("key", ["set_id", "set_ids"])
@pytest.mark.parametrize("value", [0, 1.5, True, False, {}, {"id": "A"}, [["A"]]])
def test_typed_selector_refuses_deterministically_without_mutation(key, value):
    intake = {"inserts": [{"handle": "A", "name": "Panel"}]}
    params = {key: value}
    original = deepcopy((intake, params))
    first = tool_loader.run_tool_dynamic(TOOL, intake, params, aps_live=False)
    second = tool_loader.run_tool_dynamic(TOOL, intake, params, aps_live=False)
    for result in (first, second):
        assert not result["ok"]
        assert result["error"]["error_code"] == "BAD_PARAMS"
        assert key in result["error"]["message"]
        assert not result.get("result") and not result.get("overlay")
    assert first["error"] == second["error"]
    assert (intake, params) == original
