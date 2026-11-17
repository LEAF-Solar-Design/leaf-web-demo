"""Studio X-bench X04 comparison obeys membership and count delta algebra."""
from copy import deepcopy
import pytest
from test_x04_untrusted_derived_counts import worked_example
from export_x04_offline_evidence import export_evidence
from drawing_dependency_report import from_pairs
from compare_x04_offline_evidence import compare_evidence


def artifact(index):
    payload = worked_example()
    selections = [['["INSERT","East","Panel"]'], ['["LWPOLYLINE","East",""]'],
                  payload['request']['set_ids'], payload['request']['set_ids']]
    payload['request']['set_ids'] = selections[index]
    if index == 3:
        payload['drawing']['intake']['inserts'][0]['rows'] = 3
    payload['drawing']['dependency_pairs'] = [[0, 'STYLE'], [3, f'../literal-{index}.shx']]
    payload['observed_dependencies'] = from_pairs(payload['drawing']['dependency_pairs'])
    result = export_evidence(payload)
    assert result['result']['count'] == [6, 1, 7, 10][index]
    return result


@pytest.mark.parametrize('left', range(4))
@pytest.mark.parametrize('right', range(4))
def test_delta_algebra_and_input_immutability(left, right):
    a, b = artifact(left), artifact(right)
    original = deepcopy((a, b))
    for item in (a, b):
        same = compare_evidence(item, item)
        assert same['count']['delta'] == 0
        for key in ('selected_ids', 'highlight_handles', 'literal_dependencies'):
            assert same[key] == {'removed': [], 'added': []}
    forward, reverse = compare_evidence(a, b), compare_evidence(b, a)
    assert forward['count']['delta'] == -reverse['count']['delta']
    assert forward['count']['before'] == reverse['count']['after']
    assert forward['count']['after'] == reverse['count']['before']
    for key in ('selected_ids', 'highlight_handles', 'literal_dependencies'):
        assert forward[key]['removed'] == reverse[key]['added']
        assert forward[key]['added'] == reverse[key]['removed']
    assert (a, b) == original
    assert forward['original_bytes_integrity'] == reverse['original_bytes_integrity'] == 'unverified'
