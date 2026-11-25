"""Explicit choices can change while fully overlapping physical membership stays fixed."""
from copy import deepcopy
import pytest
from test_x04_untrusted_derived_counts import worked_example
from export_x04_offline_evidence import export_evidence
from compare_x04_offline_evidence import compare_evidence

EAST = '["INSERT","East","Module"]'
WEST = '["INSERT","West","Module"]'


def artifact(ids):
    value = worked_example()
    value['drawing']['intake'] = {'inserts': [
        {'layer': layer, 'name': 'Module', 'handle': handle, 'rows': rows, 'columns': columns}
        for layer in ('East', 'West') for handle, rows, columns in [('A', 2, 3), ('B', 1, 1)]]}
    value['request']['set_ids'] = ids
    return export_evidence(value)


@pytest.mark.parametrize('left,right', [([EAST], [WEST]), ([EAST], [EAST, WEST]), ([EAST, WEST], [WEST])])
def test_selection_only_delta(left, right):
    a, b = artifact(left), artifact(right)
    before = deepcopy((a, b))
    delta = compare_evidence(a, b)
    assert delta['selected_ids'] == {'removed': sorted(set(left) - set(right)), 'added': sorted(set(right) - set(left))}
    assert delta['count'] == {'before': 7, 'after': 7, 'delta': 0}
    for field in ('highlight_handles', 'literal_dependencies'):
        assert delta[field] == {'removed': [], 'added': []}
    assert a['result']['highlight_handles'] == b['result']['highlight_handles'] == ['A', 'B']
    assert delta['original_bytes_integrity'] == 'unverified' and (a, b) == before
