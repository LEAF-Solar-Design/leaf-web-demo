"""Malformed saved intakes refuse with deterministic guidance, without source I/O."""
import builtins
from copy import deepcopy
import io
import json
from pathlib import Path
import pytest
import tool_loader
from test_x04_minsert_arrays import TOOL


@pytest.mark.parametrize("intake", ['{"inserts":', [], None, 4, True,
    {"inserts": "bad"}, {"inserts": {}}, {"inserts": [None]},
    {"polylines": [{"closed": True}]}, {"polylines": [{"pts": "bad"}]}])
def test_incompatible_saved_intake_refuses_without_partial_output_or_source_access(intake, monkeypatch):
    # Warm the trusted module, then guard the removed original source only.
    tool_loader.run_tool_dynamic(TOOL, {}, {}, aps_live=False)
    source = Path("synthetic-removed-source.dxf").resolve()
    attempts = []
    def guarded(opener):
        def open_file(file, *args, **kwargs):
            if not isinstance(file, int) and Path(file).resolve() == source:
                attempts.append(str(file))
                raise AssertionError("No source file may be reopened")
            return opener(file, *args, **kwargs)
        return open_file
    for module in (builtins, io):
        monkeypatch.setattr(module, "open", guarded(module.open))
    original = deepcopy(intake)
    results = [tool_loader.run_tool_dynamic(TOOL, intake, {}, aps_live=False) for _ in range(2)]
    for result in results:
        assert not result["ok"]
        assert "re-import" in result["error"]["message"]
        assert not result.get("result") and not result.get("overlay")
    assert results[0]["error"] == results[1]["error"]
    assert intake == original and attempts == []


def test_truncated_json_is_rejected_before_replay():
    with pytest.raises(json.JSONDecodeError):
        json.loads('{"inserts":[')
