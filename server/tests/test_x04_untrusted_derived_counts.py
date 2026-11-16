"""Saved result claims cannot replace trusted counting from physical entities."""
from copy import deepcopy
import json
import pytest
import tool_loader
from test_x04_minsert_arrays import TOOL, selected


@pytest.mark.parametrize('claims', [
    {'count': 999999, 'selected_sets': [{'set_id': 'forged', 'count': 999999}]},
    {'count': -1, 'selected_set': {'layer': 'Wrong'}, 'overlay': {'highlight_handles': ['BAD']}},
    {'count': None, 'selected_sets': None, 'highlight_handles': ['DEAD']},
    {'count': {'bad': True}, 'selected_sets': 'all', 'overlay': [False]},
])
def test_forged_saved_claims_are_opaque_and_recomputed(claims):
    intake = {'inserts': [{'name': 'Panel', 'layer': 'A', 'handle': '0x0a', 'rows': 2, 'columns': 3},
                          {'name': 'Panel', 'layer': 'B', 'handle': 'A', 'rows': 2, 'columns': 3},
                          {'name': 'Panel', 'layer': 'B', 'handle': 'B'}], **claims}
    saved = json.loads(json.dumps(intake))
    before = deepcopy(saved)
    outcome = selected(saved)
    assert outcome['ok'] and outcome['result']['count'] == 7
    assert set(outcome['overlay']['highlight_handles']) == {'A', 'B'}
    assert outcome['result']['original_bytes_integrity'] == 'unverified'
    listed = tool_loader.run_tool_dynamic(TOOL, saved, {}, aps_live=False)
    assert listed['ok'] and listed['result']['selection_required'] and listed['result']['count'] is None
    refused = tool_loader.run_tool_dynamic(TOOL, saved, {'set_ids': ['forged']}, aps_live=False)
    assert not refused['ok'] and not refused.get('result') and not refused.get('overlay')
    assert saved == before
