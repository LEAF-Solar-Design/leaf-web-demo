"""Concurrent saved-intake reads share no per-call selection or mutable result."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from dxf_intake import parse_dxf_bytes
from test_x04_original_bytes_replay import RAW
from test_x04_minsert_arrays import TOOL
import tool_loader


def test_concurrent_listings_and_selections_match_serial_controls():
    first = json.loads(json.dumps(parse_dxf_bytes(RAW)))
    second = deepcopy(first)
    for row in second["inserts"]:
        row["handle"] = "F" + row["handle"]
        row["rows"] = 2
        row["columns"] = 2
    snapshots = deepcopy((first, second))
    requests = []
    def invoke(request):
        source, params = request
        result = tool_loader.run_tool_dynamic(TOOL, source, params, aps_live=False)
        assert result["ok"]
        return deepcopy((result["result"], result["overlay"]))
    for source in (first, second):
        listing = invoke((source, {}))[0]
        ids = [row["set_id"] for row in listing["sets"]]
        requests.extend((source, params) for params in ({}, {"set_ids": ids[:1]},
                        {"set_ids": ids[1:]}, {"set_ids": ids}))
    controls = [invoke(request) for request in requests]
    # Warmed trusted modules above; bounded threads test shared read behavior.
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(invoke, requests * 3))
    assert results == controls * 3
    assert (first, second) == snapshots
    results[1][0]["sets"].clear()
    assert invoke(requests[1]) == controls[1]
