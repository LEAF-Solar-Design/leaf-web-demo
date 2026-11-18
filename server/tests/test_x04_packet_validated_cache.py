"""Packet trusted results are reused only inside the current validation call."""
from copy import deepcopy
import pytest
from test_x04_packet_cross_binding import packet
from test_x04_comparison_delta_algebra import artifact
import compare_x04_offline_evidence as comparison
import validate_x04_evidence_packet as checker


def test_packet_calls_once_per_artifact_and_keeps_exact_reports(monkeypatch):
    value = packet()
    template = value['comparisons'][0]
    value['comparisons'] = [{**deepcopy(template), 'id': f'c{i}'} for i in range(5)]
    before = deepcopy(value); calls = []
    original = checker.validate_evidence
    def counted(item):
        calls.append(item)
        return original(item)
    def forbidden(*args):
        raise AssertionError('Packet must not revalidate comparison operands')
    with monkeypatch.context() as patch:
        patch.setattr(checker, 'validate_evidence', counted)
        patch.setattr(comparison, 'validate_evidence', forbidden)
        result = checker.validate_packet(value)
    assert len(calls) == len(value['artifacts']) == 2 and value == before
    assert [row['report'] for row in result['comparisons']] == [template['report']] * 5
    assert result['batches'][0]['report'] == value['batches'][0]['report']


def test_standalone_comparison_still_validates_both_operands(monkeypatch):
    value = packet(); a, b = [row['artifact'] for row in value['artifacts']]
    original = comparison.validate_evidence; calls = []
    def counted(item):
        calls.append(item)
        return original(item)
    with monkeypatch.context() as patch:
        patch.setattr(comparison, 'validate_evidence', counted)
        assert comparison.compare_evidence(a, b) == value['comparisons'][0]['report']
        assert len(calls) == 2
        a['result']['count'] = 999
        with pytest.raises(ValueError, match='count'):
            comparison.compare_evidence(a, b)


def test_fresh_call_with_same_ids_recomputes_and_refuses_forged_operand():
    value = packet(); first = checker.validate_packet(value)
    value['artifacts'][0]['artifact'] = artifact(2)
    a, b = [row['artifact'] for row in value['artifacts']]
    value['comparisons'][0]['report'] = comparison.compare_evidence(a, b)
    for index, identity in enumerate(value['batches'][0]['artifact_ids']):
        item = a if identity == 'a' else b
        value['batches'][0]['report']['results'][index].update(checker.validate_evidence(item))
    second = checker.validate_packet(value)
    assert first['validated_artifacts'][0]['recomputed_result']['count'] == 6
    assert second['validated_artifacts'][0]['recomputed_result']['count'] == 7
    value['artifacts'][0]['artifact']['result']['count'] = 6
    with pytest.raises(ValueError, match='count'):
        checker.validate_packet(value)
