"""Packet labels never resolve as paths, URLs, reports or cyclic references."""
from copy import deepcopy
import pytest
from test_x04_packet_cross_binding import packet
import validate_x04_evidence_packet as validator


@pytest.mark.parametrize('change', [
    lambda p: p['artifacts'].append(deepcopy(p['artifacts'][0])),
    lambda p: p['comparisons'][0].update(id='a'),
    lambda p: p['comparisons'][0].update(before='missing'),
    lambda p: p['comparisons'][0].update(before='c', after='c'),
    lambda p: p['batches'][0].update(artifact_ids=['batch']),
    lambda p: p['artifacts'][0].update(id='https://unopened.example/a'),
    lambda p: p['comparisons'][0].update(before='../../unopened.json'),
    lambda p: p['batches'][0].update(artifact_ids=[{'ref': 'a'}]),
])
def test_reference_failures_precede_any_validation_or_external_reads(change, monkeypatch):
    value = packet(); change(value); before = deepcopy(value)
    calls = []
    def denied(*args, **kwargs):
        calls.append(args)
        raise AssertionError('Invalid references must refuse before artifact IO')
    with monkeypatch.context() as patch:
        patch.setattr(validator, 'validate_evidence', denied)
        patch.setattr('builtins.open', denied)
        patch.setattr('pathlib.Path.open', denied)
        for _ in range(2):
            with pytest.raises(ValueError) as refusal:
                validator.validate_packet(value)
            assert 'Packet' in str(refusal.value)
    assert not calls and value == before
    assert validator.validate_packet(packet())['valid']
