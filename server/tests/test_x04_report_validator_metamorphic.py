"""Saved report consistency is order-independent except for batch occurrence indices."""
from copy import deepcopy
from itertools import permutations
import json
import pytest
from test_x04_comparison_report_contract import comparison
from test_x04_batch_report_contract import batch
from validate_x04_review_report import validate_report


@pytest.mark.parametrize('order', list(permutations(range(3))))
def test_comparison_membership_and_key_permutations(order):
    report = comparison()
    for field in ('selected_ids', 'highlight_handles'):
        report[field] = {'removed': ['old0', 'old1', 'old2'], 'added': ['new0', 'new1', 'new2']}
    report['literal_dependencies'] = {'removed': [], 'added': [
        {'kind': 'font', 'name': f'../../unopened-{i}.shx', 'disposition': 'font remapped'} for i in range(3)]}
    baseline = validate_report(report)
    for field in ('selected_ids', 'highlight_handles', 'literal_dependencies'):
        for side in ('removed', 'added'):
            rows = report[field][side]
            if rows:
                report[field][side] = [rows[index] for index in order]
    # JSON round-trip changes all object key orders while retaining values and literal strings.
    def reverse_keys(value):
        if isinstance(value, dict):
            return {k: reverse_keys(v) for k, v in reversed(list(value.items()))}
        if isinstance(value, list):
            return [reverse_keys(v) for v in value]
        return value
    report = json.loads(json.dumps(reverse_keys(report)))
    before = deepcopy(report)
    assert validate_report(report) == baseline and report == before


@pytest.mark.parametrize('order', list(permutations(range(3))))
def test_batch_reordered_occurrences_require_current_indices(order):
    report = batch()
    for index, row in enumerate(report['results']):
        row['path'] = ['./-', ' spaced path.json ', '../../never-open.json'][index]
    baseline = validate_report(report)
    report['results'] = [report['results'][index] for index in order]
    before = deepcopy(report)
    if order != (0, 1, 2):
        with pytest.raises(ValueError, match='indices'):
            validate_report(report)
        assert report == before
    paths = [row['path'] for row in report['results']]
    for index, row in enumerate(report['results']):
        row['index'] = index
    before = deepcopy(report)
    assert validate_report(report) == baseline and report == before
    assert [row['path'] for row in report['results']] == paths
