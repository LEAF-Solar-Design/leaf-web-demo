"""Independent set reconstruction pins three-way deltas and remove/readd."""
from copy import deepcopy
from itertools import permutations
import json
import pytest
from test_x04_untrusted_derived_counts import worked_example
from export_x04_offline_evidence import export_evidence
from drawing_dependency_report import from_pairs
from compare_x04_offline_evidence import compare_evidence


def artifact(indices):
    value = worked_example()
    value['drawing']['intake'] = {'inserts': [{'layer': 'A', 'name': 'Module', 'handle': format(i, 'X')} for i in indices]}
    value['request']['set_ids'] = ['["INSERT","A","Module"]']
    pairs = [[code, text] for i in indices for code, text in [(0, 'STYLE'), (3, f'../font-{i}.shx')]]
    value['drawing']['dependency_pairs'] = pairs; value['observed_dependencies'] = from_pairs(pairs)
    return export_evidence(value)


@pytest.mark.parametrize('order', list(permutations(range(3))))
def test_three_way_reconstruction_and_count_additivity(order):
    ids = [{1, 2, 3}, {2, 4}, {1, 4, 5}]
    a, b, c = [artifact(sorted(ids[i])) for i in order]
    before = deepcopy((a, b, c))
    ab, bc, ac = compare_evidence(a, b), compare_evidence(b, c), compare_evidence(a, c)
    assert ab['count']['delta'] + bc['count']['delta'] == ac['count']['delta']
    canonical = lambda rows: {json.dumps(row, sort_keys=True) for row in rows}
    for field, initial, final in [
        ('highlight_handles', a['result']['highlight_handles'], c['result']['highlight_handles']),
        ('literal_dependencies', a['observed_dependencies']['dependencies'], c['observed_dependencies']['dependencies'])]:
        initial_set, final_set = canonical(initial), canonical(final)
        restored = (initial_set - canonical(ab[field]['removed'])) | canonical(ab[field]['added'])
        restored = (restored - canonical(bc[field]['removed'])) | canonical(bc[field]['added'])
        assert restored == final_set
        assert canonical(ac[field]['removed']) == initial_set - final_set
        assert canonical(ac[field]['added']) == final_set - initial_set
    assert (a, b, c) == before and ac['original_bytes_integrity'] == 'unverified'
