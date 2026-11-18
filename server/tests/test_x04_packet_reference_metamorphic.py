"""Consistent packet ID renaming and entry order preserve semantic bindings."""
from copy import deepcopy
import pytest
from test_x04_packet_cross_binding import packet
import validate_x04_evidence_packet as checker


@pytest.mark.parametrize('reverse', [False, True])
def test_consistent_rename_and_entry_order(reverse, monkeypatch):
    original = packet(); baseline = checker.validate_packet(original)
    value = deepcopy(original)
    names = {'a': 'Left_001', 'b': 'Right_002', 'c': 'Compare_003', 'batch': 'Batch_004'}
    for kind in ('artifacts', 'comparisons', 'batches'):
        for row in value[kind]:
            row['id'] = names[row['id']]
        if reverse:
            value[kind].reverse()
    for row in value['comparisons']:
        row['before'], row['after'] = names[row['before']], names[row['after']]
    for row in value['batches']:
        row['artifact_ids'] = [names[identity] for identity in row['artifact_ids']]
    before = deepcopy(value)
    result = checker.validate_packet(value)
    assert value == before and original == packet()
    assert result['comparisons'][0]['report'] == baseline['comparisons'][0]['report']
    assert result['batches'][0]['report'] == baseline['batches'][0]['report']
    by_id = {row['id']: row['recomputed_result'] for row in result['validated_artifacts']}
    assert by_id == {names[row['id']]: row['recomputed_result'] for row in baseline['validated_artifacts']}
    value['comparisons'][0]['before'] = 'a'
    broken = deepcopy(value)
    def denied(*args, **kwargs):
        raise AssertionError('Incomplete rename must refuse before external read or validation')
    with monkeypatch.context() as patch:
        patch.setattr(checker, 'validate_evidence', denied)
        patch.setattr('builtins.open', denied)
        patch.setattr('pathlib.Path.open', denied)
        with pytest.raises(ValueError, match='reference'):
            checker.validate_packet(value)
    assert value == broken
    assert checker.validate_packet(original) == baseline
