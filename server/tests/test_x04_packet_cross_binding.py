"""Packet reports must agree with explicit embedded operands and occurrences."""
from copy import deepcopy
import pytest
from test_x04_comparison_delta_algebra import artifact
from compare_x04_offline_evidence import compare_evidence
from validate_x04_offline_evidence import validate_evidence
from validate_x04_review_report import BATCH, validate_report
from validate_x04_evidence_packet import SCHEMA, validate_packet


def packet():
    a, b = artifact(0), artifact(1)
    rows = [{'index': i, 'path': '../../unopened.json', **validate_evidence(value)}
            for i, value in enumerate((a, b, a))]
    batch = {'schema': BATCH, 'results': rows, 'total': 3, 'passed': 3, 'failed': 0,
             'original_bytes_integrity': 'unverified'}
    return {'schema': SCHEMA, 'artifacts': [{'id': 'a', 'artifact': a}, {'id': 'b', 'artifact': b}],
            'comparisons': [{'id': 'c', 'before': 'a', 'after': 'b', 'report': compare_evidence(a, b)}],
            'batches': [{'id': 'batch', 'artifact_ids': ['a', 'b', 'a'], 'report': batch}]}


def test_exact_control_and_membership_permutation():
    value = packet(); before = deepcopy(value)
    result = validate_packet(value)
    assert result['comparisons'][0]['report']['count'] == {'before': 6, 'after': 1, 'delta': -5}
    assert [row['recomputed_result']['count'] for row in result['batches'][0]['report']['results']] == [6, 1, 6]
    assert result['external_paths_opened'] is False and result['original_bytes_integrity'] == 'unverified'
    assert value == before
    value['comparisons'][0]['report']['highlight_handles']['removed'].reverse()
    assert validate_packet(value)['valid']


@pytest.mark.parametrize('change', [
    lambda p: p['comparisons'][0].update(before='b', after='a'),
    lambda p: p['batches'][0].update(artifact_ids=['b', 'a', 'a']),
    lambda p: p['comparisons'][0].update(report=compare_evidence(artifact(0), artifact(0))),
    lambda p: p['artifacts'][0].update(artifact=artifact(2)),
])
def test_individually_valid_but_cross_bound_contradiction(change):
    value = packet(); change(value)
    for row in value['comparisons'] + value['batches']:
        assert validate_report(row['report'])['valid']
    for row in value['artifacts']:
        assert validate_evidence(row['artifact'])['valid']
    before = deepcopy(value)
    with pytest.raises(ValueError, match='contradict'):
        validate_packet(value)
    assert value == before
