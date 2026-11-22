"""Packet selection deltas bind choices even when physical membership is equal."""
from copy import deepcopy
import pytest
from test_x04_comparison_selection_only_deltas import artifact, EAST, WEST
from compare_x04_offline_evidence import compare_evidence
from validate_x04_review_report import validate_report
import validate_x04_evidence_packet as checker


def test_equal_physical_counts_do_not_hide_stale_selection_deltas():
    a, b = artifact([EAST]), artifact([WEST])
    report = compare_evidence(a, b)
    assert report['count'] == {'before': 7, 'after': 7, 'delta': 0}
    assert report['highlight_handles'] == report['literal_dependencies'] == {'removed': [], 'added': []}
    assert a['result']['highlight_handles'] == b['result']['highlight_handles'] == ['A', 'B']
    value = {'schema': checker.SCHEMA, 'artifacts': [{'id': 'a', 'artifact': a}, {'id': 'b', 'artifact': b}],
             'comparisons': [{'id': 'c', 'before': 'a', 'after': 'b', 'report': report}], 'batches': []}
    before = deepcopy(value)
    assert checker.validate_packet(value)['comparisons'][0]['report']['selected_ids'] == {'removed': [EAST], 'added': [WEST]}
    assert value == before
    value['comparisons'][0]['report'] = compare_evidence(a, a)
    assert validate_report(value['comparisons'][0]['report'])['valid']
    stale = deepcopy(value)
    with pytest.raises(ValueError, match='contradict'):
        checker.validate_packet(value)
    assert value == stale
    value['comparisons'][0]['report'] = report
    assert checker.validate_packet(value)['valid']
