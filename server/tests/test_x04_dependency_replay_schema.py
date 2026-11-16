"""Count replay preserves optional report metadata without inventing repairs."""
from copy import deepcopy
import builtins
import io
import json
import os
import pytest
from dxf_intake import parse_dxf_bytes
from test_x04_original_bytes_replay import RAW
from test_x04_minsert_arrays import TOOL, selected
import tool_loader


@pytest.mark.parametrize("report", [None, {}, {"dependencies": None}, {"dependencies": [None, 4]},
    {"dependencies": [{"kind": "font", "name": {"bad": True}, "disposition": "font remapped"}]},
    {"dependencies": [{"kind": "font", "name": "synthetic-target.shx", "disposition": "font remapped", "future": [1, 2]}], "future": {"version": 9}}])
def test_report_metadata_is_opaque_to_counting_and_retained_without_target_access(report, monkeypatch):
    tool_loader.run_tool_dynamic(TOOL, {}, {}, aps_live=False)
    intake = parse_dxf_bytes(RAW)
    intake["dependency_report"] = report
    saved = json.loads(json.dumps(intake, allow_nan=False))
    snapshot = deepcopy(saved)
    attempts = []
    def guard(opener):
        def open_file(file, *args, **kwargs):
            if 'synthetic-target' in str(file):
                attempts.append(str(file))
                raise AssertionError('Report metadata must not open targets')
            return opener(file, *args, **kwargs)
        return open_file
    for module in (builtins, io, os):
        monkeypatch.setattr(module, 'open', guard(module.open))
    outcome = selected(saved)
    assert outcome["ok"] and outcome["result"]["count"] == 7
    assert outcome["overlay"]["highlight_handles"] == ['A', 'B']
    assert saved == snapshot and saved["dependency_report"] == report and attempts == []
    # The count tool does not interpret reports. The report component separately
    # renders missing or incompatible report data as unavailable, never complete.
