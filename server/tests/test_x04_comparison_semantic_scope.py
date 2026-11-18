"""Empty supported deltas can accompany different embedded drawing content."""
from copy import deepcopy
import pytest
from test_x04_untrusted_derived_counts import worked_example
from export_x04_offline_evidence import export_evidence
from compare_x04_offline_evidence import compare_evidence


@pytest.mark.parametrize('change', ['coordinates', 'extension', 'both'])
def test_valid_content_edits_can_have_empty_deltas(change):
    before = export_evidence(worked_example()); changed = deepcopy(before)
    if change in ('coordinates', 'both'):
        quad = changed['drawing']['intake']['polylines'][0]
        quad['pts'] = [[point[0] + 100, point[1] - 50, *point[2:]] for point in quad['pts']]
    if change in ('extension', 'both'):
        changed['drawing']['opaque_extension'] = {'review': 'changed synthetic annotation'}
    after = export_evidence(changed)
    originals = deepcopy((before, after))
    assert before['drawing'] != after['drawing']
    result = compare_evidence(before, after)
    assert result['count'] == {'before': 7, 'after': 7, 'delta': 0}
    for name in ('selected_ids', 'highlight_handles', 'literal_dependencies'):
        assert result[name] == {'removed': [], 'added': []}
    assert result['original_bytes_integrity'] == 'unverified'
    assert 'authenticate' in result['scope_note'] and (before, after) == originals


def test_supported_selection_change_still_reports_exact_delta():
    before = export_evidence(worked_example()); changed = deepcopy(before)
    changed['request']['set_ids'] = ['["INSERT","East","Panel"]']
    after = export_evidence(changed)
    result = compare_evidence(before, after)
    assert result['count'] == {'before': 7, 'after': 6, 'delta': -1}
    assert result['highlight_handles'] == {'removed': ['B'], 'added': []}
