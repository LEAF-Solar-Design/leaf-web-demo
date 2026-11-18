"""Equal payloads retain distinct local identities and owned trusted results."""
from copy import deepcopy
import pytest
from test_x04_packet_cross_binding import packet
from compare_x04_offline_evidence import compare_evidence
import validate_x04_evidence_packet as checker


def test_equal_payload_ids_each_validate_and_remain_independent(monkeypatch):
    value = packet(); a = value['artifacts'][0]['artifact']
    value['artifacts'][1]['artifact'] = deepcopy(a)
    value['comparisons'][0]['report'] = compare_evidence(a, a)
    value['batches'] = []
    snapshot = deepcopy(value); calls = []; original = checker.validate_evidence
    def counted(item): calls.append(item); return original(item)
    monkeypatch.setattr(checker, 'validate_evidence', counted)
    result = checker.validate_packet(value)
    assert len(calls) == 2 and calls[0] is not calls[1] and calls[0] == calls[1]
    assert [r['id'] for r in result['validated_artifacts']] == ['a', 'b']
    assert (result['comparisons'][0]['before'], result['comparisons'][0]['after']) == ('a', 'b')
    assert result['comparisons'][0]['report']['count'] == {'before': 6, 'after': 6, 'delta': 0}
    result['validated_artifacts'][0]['recomputed_result']['highlight_handles'].append('MUTATED')
    assert result['validated_artifacts'][1]['recomputed_result']['highlight_handles'] == ['A']
    assert value == snapshot
    value['artifacts'][1]['artifact']['result']['count'] = 999
    bad = deepcopy(value)
    with pytest.raises(ValueError, match='count'): checker.validate_packet(value)
    assert value == bad
    value['artifacts'][1]['artifact'] = deepcopy(a)
    recovered = checker.validate_packet(value)
    assert recovered['valid'] and recovered['validated_artifacts'][0] == {**recovered['validated_artifacts'][1], 'id': 'a'}
