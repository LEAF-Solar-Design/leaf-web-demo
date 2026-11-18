"""An empty saved drawing lists no eligible modules and never invents a count."""
from copy import deepcopy
import json
from dxf_intake import parse_dxf_bytes
from test_x04_original_bytes_replay import guard_source
from test_x04_minsert_arrays import TOOL
import tool_loader


def test_empty_intake_replays_and_rejects_missing_selections_without_source(tmp_path, monkeypatch):
    tool_loader.run_tool_dynamic(TOOL, {}, {}, aps_live=False)
    attempts = guard_source(monkeypatch, tmp_path / "empty-original.dxf")
    raw = b"0\nSECTION\n2\nENTITIES\n0\nLINE\n5\nA\n10\n0\n20\n0\n11\n1\n21\n1\n0\nENDSEC\n0\nEOF\n"
    intake = json.loads(json.dumps(parse_dxf_bytes(raw)))
    snapshot = deepcopy(intake)
    listed = tool_loader.run_tool_dynamic(TOOL, intake, {}, aps_live=False)
    assert listed["ok"] and listed["result"]["sets"] == []
    assert listed["result"]["selection_required"] and listed["result"]["count"] is None
    assert not listed.get("overlay")
    first = tool_loader.run_tool_dynamic(TOOL, intake, {"set_id": "missing"}, aps_live=False)
    second = tool_loader.run_tool_dynamic(TOOL, intake, {"set_id": "missing"}, aps_live=False)
    assert first["error"] == second["error"]
    assert not first["ok"] and "list the sets again" in first["error"]["message"]
    assert not first.get("result") and not first.get("overlay")
    assert intake == snapshot and attempts == []
