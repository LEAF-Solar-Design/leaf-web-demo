"""Overlay membership is only the chosen physical parents, including array weights."""
from copy import deepcopy
import json
import pytest
import tool_loader
from test_x04_minsert_arrays import TOOL


@pytest.mark.parametrize('layers,expected,count', [([], set(), None),
    (['A'], {'A'}, 6), (['B'], {'A', 'B'}, 7), (['Q'], {'C'}, 1),
    (['A', 'B', 'Q'], {'A', 'B', 'C'}, 8)])
def test_saved_overlay_is_replaced_by_exact_selected_parent_membership(layers, expected, count):
    quad = {'layer': 'Q', 'handle': '0x00c', 'closed': True, 'pts': [[0, 0], [2, 0], [2, 1], [0, 1]]}
    intake = {'inserts': [{'layer': 'A', 'handle': '0x0a', 'rows': 2, 'columns': 3},
        {'layer': 'B', 'handle': 'A', 'rows': 2, 'columns': 3}, {'layer': 'B', 'handle': 'B'},
        {'layer': 'Paper', 'handle': 'D', 'space': 'paper'}],
        'polylines': [quad, {**quad, 'handle': 'E', 'space': 'paper'},
            {**quad, 'handle': 'F', 'bulges': [1, 0, 0, 0]}],
        'overlay': {'highlight_handles': ['D', 'E', 'F', 'BAD']}}
    saved = json.loads(json.dumps(intake)); before = deepcopy(saved)
    listed = tool_loader.run_tool_dynamic(TOOL, saved, {}, aps_live=False)
    assert listed['ok'] and not listed.get('overlay')
    ids = [row['set_id'] for row in listed['result']['sets'] if row['layer'] in layers]
    envelope = tool_loader.run_tool_dynamic(TOOL, saved, {'set_ids': ids}, aps_live=False)
    replay = json.loads(json.dumps(envelope, allow_nan=False))
    assert replay == envelope and envelope['ok'] and envelope['result']['count'] == count
    actual = envelope.get('overlay') or {}
    assert set(actual.get('highlight_handles', [])) == expected
    assert len(actual.get('highlight_handles', [])) == len(expected)
    assert saved == before
