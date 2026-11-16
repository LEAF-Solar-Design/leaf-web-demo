"""Unknown JSON fields remain opaque while known saved-intake fields keep meaning."""
from copy import deepcopy
import json
import pytest
from dxf_intake import parse_dxf_bytes
from test_x04_original_bytes_replay import RAW, guard_source
from test_x04_minsert_arrays import TOOL, selected
import tool_loader


@pytest.mark.parametrize("extension", [None, False, 17, ["opaque", {"v": "屋根"}], {"future": {"count": 999}}])
def test_unknown_extensions_are_retained_but_do_not_change_counting(tmp_path, monkeypatch, extension):
    tool_loader.run_tool_dynamic(TOOL, {}, {}, aps_live=False)
    attempts = guard_source(monkeypatch, tmp_path / "original.dxf")
    intake = parse_dxf_bytes(RAW, source_name=str(tmp_path / "original.dxf"))
    intake["polylines"].append({"handle": "C", "layer": "Shapes", "closed": True,
                               "pts": [[0, 0], [2, 0], [2, 1], [0, 1]]})
    baseline = selected(intake)
    intake["future_extension"] = extension
    for row in intake["inserts"] + intake["polylines"]:
        row["future_extension"] = deepcopy(extension)
    saved = json.loads(json.dumps(intake, allow_nan=False))
    snapshot = deepcopy(saved)
    outcome = selected(saved)
    assert outcome["ok"] and outcome["result"] == baseline["result"]
    assert outcome["result"]["count"] == 8
    assert set(outcome["overlay"]["highlight_handles"]) == {"A", "B", "C"}
    assert outcome["overlay"] == baseline["overlay"]
    assert saved == snapshot and saved["future_extension"] == extension and attempts == []
