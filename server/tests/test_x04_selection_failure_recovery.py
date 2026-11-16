"""Failed requests cannot contaminate a stored intake or later successful counts."""
from copy import deepcopy
import json
import tool_loader
from dxf_intake import parse_dxf_bytes
from test_x04_original_bytes_replay import RAW
from test_x04_minsert_arrays import TOOL, selected


def test_invalid_selectors_and_conflicts_do_not_poison_valid_saved_replay():
    intake = json.loads(json.dumps(parse_dxf_bytes(RAW), allow_nan=False))
    snapshot = deepcopy(intake)
    fresh = selected(json.loads(json.dumps(intake)))
    listing = tool_loader.run_tool_dynamic(TOOL, intake, {}, aps_live=False)
    known = listing["result"]["sets"][0]["set_id"]
    conflict = deepcopy(intake)
    conflict["inserts"].append({**conflict["inserts"][0], "handle": "000a", "rows": 3})
    bad_snapshot = deepcopy(conflict)
    requests = [(intake, {"set_ids": [known, "missing"]}), (intake, {"set_ids": True}),
                (conflict, {}), (intake, {"set_id": "foreign"}), (conflict, {"set_ids": [known]})]
    for source, params in requests:
        failed = tool_loader.run_tool_dynamic(TOOL, source, params, aps_live=False)
        assert not failed["ok"] and not failed.get("result") and not failed.get("overlay")
        assert intake == snapshot and conflict == bad_snapshot
        recovered = selected(intake)
        assert recovered["ok"] and recovered["result"] == fresh["result"]
        assert recovered["overlay"] == fresh["overlay"]
