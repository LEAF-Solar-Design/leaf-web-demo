"""X04 saved comparison reports prove internal consistency only."""
from copy import deepcopy
import pytest
from test_x04_comparison_delta_algebra import artifact
from compare_x04_offline_evidence import compare_evidence
from validate_x04_review_report import validate_report


def comparison():
    return compare_evidence(artifact(0), artifact(1))


def test_genuine_comparison_is_immutable_and_does_not_revalidate(monkeypatch):
    report = comparison()
    before = deepcopy(report)
    def denied(*args, **kwargs):
        raise AssertionError('Underlying validation or file access is forbidden')
    monkeypatch.setattr('builtins.open', denied)
    monkeypatch.setattr('compare_x04_offline_evidence.validate_evidence', denied)
    result = validate_report(report)
    assert result['valid'] is True
    assert result['underlying_artifacts_revalidated'] is False
    assert result['embedded_paths_opened'] is False
    assert result['original_bytes_integrity'] == 'unverified'
    assert report == before


@pytest.mark.parametrize('field,value', [
    ('count', {'before': 6, 'after': 1, 'delta': 0}),
    ('count', {'before': True, 'after': 1, 'delta': 0}),
    ('count', {'before': 6, 'after': -1, 'delta': -7}),
    ('count', {'before': 6, 'after': 1, 'delta': False}),
    ('original_bytes_integrity', 'verified'),
    ('selected_ids', {'removed': ['x', 'x'], 'added': []}),
    ('selected_ids', {'removed': ['x'], 'added': ['x']}),
    ('selected_ids', {'removed': [False], 'added': []}),
    ('highlight_handles', {'removed': [], 'added': ['']}),
    ('highlight_handles', {'removed': [], 'added': ['A'], 'extra': []}),
    ('literal_dependencies', {'removed': [], 'added': ['file.shx']}),
    ('literal_dependencies', {'removed': [], 'added': [{'kind': 'font', 'name': 'x'}]}),
    ('literal_dependencies', {'removed': [], 'added': [
        {'kind': 'font', 'name': 'x', 'disposition': False}]}),
])
def test_forged_comparison_refuses_without_mutation(field, value):
    report = comparison()
    report[field] = value
    before = deepcopy(report)
    with pytest.raises(ValueError):
        validate_report(report)
    assert report == before


@pytest.mark.parametrize('intersect', [False, True])
def test_dependency_memberships_use_whole_row_identity(intersect):
    report = comparison()
    row = {'kind': 'font', 'name': '../../unopened.shx', 'disposition': 'font remapped'}
    reverse = dict(reversed(list(row.items())))
    report['literal_dependencies'] = (
        {'removed': [row], 'added': [reverse]} if intersect else
        {'removed': [], 'added': [row, reverse]})
    with pytest.raises(ValueError):
        validate_report(report)


def test_self_comparison_and_changed_opaque_selection_are_valid():
    same = compare_evidence(artifact(0), artifact(0))
    same['selected_ids'] = {'removed': ['opaque-old'], 'added': ['opaque-new']}
    assert validate_report(same)['valid'] is True
