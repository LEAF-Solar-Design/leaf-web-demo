"""Tuple JSON IDs preserve exact boundaries instead of joining delimiters."""
from copy import deepcopy
import json
import tool_loader
from test_x04_minsert_arrays import TOOL


def test_delimiters_and_empty_names_have_distinct_stable_isolated_ids():
    tuples = [("a|b", "c"), ("a", "b|c"), ('a","b', 'c\\d'), ("a", ""),
              ("a/", "b"), ("a", "/b"), ("", ""), ('["INSERT"]', "x:y")]
    entities = [{"layer": layer, "name": block, "handle": format(i+16, "X")} for i, (layer, block) in enumerate(tuples)]
    intake = {"inserts": entities}
    original = deepcopy(intake)
    def listing(rows):
        return tool_loader.run_tool_dynamic(TOOL, {"inserts": rows}, {}, aps_live=False)["result"]["sets"]
    first = listing(entities)
    assert first == listing(list(reversed(entities)))
    assert len({row["set_id"] for row in first}) == len(tuples)
    for row in first:
        assert json.loads(row["set_id"]) == ["INSERT", row["layer"], row["block"]]
        result = tool_loader.run_tool_dynamic(TOOL, intake, {"set_id": row["set_id"]}, aps_live=False)
        expected = next(e["handle"] for e in entities if (e["layer"] or "0", e["name"]) == (row["layer"], row["block"]))
        assert result["result"]["count"] == 1 and result["overlay"]["highlight_handles"] == [expected]
    assert intake == original
