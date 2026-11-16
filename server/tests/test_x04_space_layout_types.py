"""X04 saved spatial metadata refuses incompatible types before any counting."""
from copy import deepcopy
import builtins
import io
import json
import os

import pytest
import tool_loader
from test_x04_selection_argument_contract import TOOL


@pytest.mark.parametrize('kind', ['inserts', 'polylines'])
@pytest.mark.parametrize('field', ['space', 'layout'])
@pytest.mark.parametrize('value', [True, False, 0, 1.5, [], {}, None])
def test_bad_spatial_type_refuses_whole_replay(kind, field, value, monkeypatch):
    tool_loader.run_tool_dynamic(TOOL, {}, {}, aps_live=False)
    row = {'layer': 'bad', 'handle': 'B', field: value}
    if kind == 'polylines':
        row.update(closed=True, pts=[[0, 0], [2, 0], [2, 1], [0, 1]])
    intake = {'inserts': [{'layer': 'valid', 'name': 'Panel', 'handle': 'A'}]}
    intake.setdefault(kind, []).append(row)
    intake = json.loads(json.dumps(intake))
    snapshot = deepcopy(intake)
    attempts = []
    def no_open(*args, **kwargs):
        attempts.append(str(args[0]))
        raise AssertionError('Saved selection must not reopen source')
    for module in (builtins, io, os):
        monkeypatch.setattr(module, 'open', no_open)
    outcomes = [tool_loader.run_tool_dynamic(TOOL, intake, {}, aps_live=False) for _ in range(2)]
    for result in outcomes:
        assert not result['ok']
        message = result['error']['message']
        assert field in message and 'must be a string' in message
        assert 're-import' in message
        assert not result.get('result') and not result.get('overlay')
    assert outcomes[0]['error'] == outcomes[1]['error']
    assert intake == snapshot and attempts == []


def test_absent_and_valid_string_metadata_preserve_model_only_controls():
    intake = {'inserts': [
        {'layer': 'A', 'handle': 'A'},
        {'layer': 'B', 'handle': 'B', 'space': 'model', 'layout': 'Model'},
        {'layer': 'C', 'handle': 'C', 'space': '', 'layout': ''},
        {'layer': 'paper', 'handle': 'D', 'space': 'paper'},
        {'layer': 'sheet', 'handle': 'E', 'layout': 'Layout1'},
    ]}
    listed = tool_loader.run_tool_dynamic(TOOL, intake, {}, aps_live=False)
    assert listed['ok']
    assert [row['layer'] for row in listed['result']['sets']] == ['A', 'B', 'C']
