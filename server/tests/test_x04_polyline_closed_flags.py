"""X04 explicit closure: True or integer 1, never generic truthiness."""
from copy import deepcopy
import json
import pytest
from test_x04_minsert_arrays import selected


@pytest.mark.parametrize('closed,eligible', [(True, True), (1, True),
    (False, False), (0, False), (2, False), (-1, False), (1.0, False),
    ('true', False), ('false', False), ('1', False), ('0', False),
    ('', False), (None, False), ([], False), ([1], False), ({}, False)])
def test_explicit_closure_contract_through_saved_selection(closed, eligible):
    intake = {'inserts': [{'handle': 'A', 'name': 'Panel'}], 'polylines': [
        {'handle': 'B', 'layer': 'Quad', 'closed': closed, 'pts': [[0, 0], [2, 0], [2, 1], [0, 1]]}]}
    saved = json.loads(json.dumps(intake))
    before = deepcopy(saved)
    outcome = selected(saved)
    assert outcome['ok']
    assert outcome['result']['count'] == (2 if eligible else 1)
    assert set(outcome['overlay']['highlight_handles']) == ({'A', 'B'} if eligible else {'A'})
    assert saved == before


def test_missing_closure_never_enters_selectable_sets():
    outcome = selected({'inserts': [{'handle': 'A'}], 'polylines': [
        {'handle': 'B', 'pts': [[0, 0], [2, 0], [2, 1], [0, 1]]}]})
    assert outcome['ok'] and outcome['result']['count'] == 1
    assert outcome['overlay']['highlight_handles'] == ['A']
