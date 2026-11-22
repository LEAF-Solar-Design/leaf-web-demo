"""Equal handleless rows remain distinct; repeated selected IDs never duplicate them."""
from copy import deepcopy
import tool_loader
from test_x04_minsert_arrays import TOOL


def test_identical_handleless_records_keep_physical_row_counts():
    insert = {"layer": "Design", "name": "Panel"}
    quad = {"layer": "Shapes", "closed": True, "pts": [[0, 0], [2, 0], [2, 1], [0, 1]]}
    intake = {"inserts": [deepcopy(insert), deepcopy(insert)],
              "polylines": [deepcopy(quad), deepcopy(quad)]}
    original = deepcopy(intake)
    listing = tool_loader.run_tool_dynamic(TOOL, intake, {}, aps_live=False)
    ids = [row["set_id"] for row in listing["result"]["sets"]]
    assert [row["count"] for row in listing["result"]["sets"]] == [2, 2]
    for choices, expected in [(ids[:1], 2), (ids, 4), (ids + ids + ids[:1], 4)]:
        outcome = tool_loader.run_tool_dynamic(TOOL, intake, {"set_ids": choices}, aps_live=False)
        assert outcome["ok"] and outcome["result"]["count"] == expected
        assert outcome["overlay"]["highlight_handles"] == []
    assert intake == original
