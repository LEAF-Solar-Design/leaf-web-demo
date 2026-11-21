"""X04 listing and recorded selection envelopes stay consistent through JSON."""
from copy import deepcopy
import json

import pytest
import tool_loader
from test_x04_selection_argument_contract import TOOL


@pytest.mark.parametrize('selection', [[], [0], [1], [0, 1], [1, 0, 1, 0]])
def test_listing_single_and_union_envelopes(selection):
    intake = {'inserts': [
        {'layer': 'A', 'handle': 'A', 'rows': 2, 'columns': 3},
        {'layer': 'B', 'handle': 'A', 'rows': 2, 'columns': 3},
        {'layer': 'B', 'handle': 'B'},
    ]}
    snapshot = deepcopy(intake)
    listed = tool_loader.run_tool_dynamic(TOOL, intake, {}, aps_live=False)
    sets = listed['result']['sets']
    params = {'set_ids': [sets[index]['set_id'] for index in selection]}
    for saved in (intake, json.loads(json.dumps(intake))):
        envelope = tool_loader.run_tool_dynamic(TOOL, saved, params, aps_live=False)
        replay = json.loads(json.dumps(envelope, allow_nan=False))
        assert replay == envelope and envelope['ok'] and envelope['error'] is None
        result = envelope['result']
        unique = set(selection)
        expected_sets = [row for index, row in enumerate(sets) if index in unique]
        assert result['sets'] == sets and result['selected_sets'] == expected_sets
        assert result['selection_required'] is (not unique)
        assert result['selected_set'] == (expected_sets[0] if len(unique) == 1 else None)
        assert result['count'] == (None if not unique else 7 if 1 in unique else 6)
        if unique:
            assert set(envelope['overlay']['highlight_handles']) == ({'A', 'B'} if 1 in unique else {'A'})
            assert len(envelope['overlay']['highlight_handles']) == (2 if 1 in unique else 1)
        else:
            assert not envelope.get('overlay')
        assert saved == snapshot
