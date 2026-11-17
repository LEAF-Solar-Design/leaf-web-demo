"""Leading zeroes, case and optional hex prefixes identify one DXF entity."""
from copy import deepcopy
from test_x04_minsert_arrays import selected


def test_hex_aliases_union_once_and_distinct_handles_remain_distinct():
    aliases = ["A", "000a", " 0x000A ", "0X0a", "a"]
    intake = {"inserts": [{"layer": f"Set{i}", "name": "Panel", "handle": h}
                          for i, h in enumerate(aliases + ["B", "0000", "0x0"])]}
    original = deepcopy(intake)
    for rows in (intake["inserts"], list(reversed(intake["inserts"]))):
        outcome = selected({"inserts": rows})
        assert outcome["ok"] and outcome["result"]["count"] == 3
        assert set(outcome["overlay"]["highlight_handles"]) == {"A", "B", "0"}
        assert len(outcome["overlay"]["highlight_handles"]) == 3
    assert intake == original


def test_array_aliases_keep_weight_and_synthetic_nonhex_handles_stay_literal():
    intake = {"inserts": [
        {"layer": layer, "handle": handle, "name": "Panel", "rows": 2, "columns": 3}
        for layer, handle in [("East", "000A"), ("West", "0xa"), ("Other", "H001")]]}
    outcome = selected(intake)
    assert outcome["result"]["count"] == 12
    assert set(outcome["overlay"]["highlight_handles"]) == {"A", "H001"}
