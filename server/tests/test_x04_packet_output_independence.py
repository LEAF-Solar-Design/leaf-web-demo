"""Packet nested outputs own their data across siblings, inputs and future calls."""
from copy import deepcopy
from test_x04_packet_cross_binding import packet
import validate_x04_evidence_packet as checker


def test_each_returned_result_is_independent():
    value = packet()
    value['comparisons'].append({**deepcopy(value['comparisons'][0]), 'id': 'c2'})
    before = deepcopy(value); baseline = checker.validate_packet(value)
    paths = [('validated_artifacts', i, 'recomputed_result') for i in range(2)]
    paths += [('comparisons', i, 'report') for i in range(2)]
    paths += [('batches', 0, 'report', 'results', i) for i in range(3)]
    def mutate(target):
        if 'recomputed_result' in target:
            target = target['recomputed_result']
        if 'highlight_handles' in target and isinstance(target['highlight_handles'], list):
            target['count'] = 999
            target['highlight_handles'].append('MUTATED')
        else:
            target['count']['before'] = 999
            target['literal_dependencies']['removed'][0]['name'] = 'mutated'
    for path in paths:
        result = checker.validate_packet(value); target = result
        for key in path: target = target[key]
        mutate(target)
        expected = deepcopy(baseline); modified = expected
        for key in path: modified = modified[key]
        mutate(modified)
        assert result == expected and value == before
        assert checker.validate_packet(value) == baseline
    result = checker.validate_packet(value)
    result['comparisons'][0]['report']['literal_dependencies']['removed'][0]['name'] = 'mutated'
    assert result['comparisons'][1] == baseline['comparisons'][1] and value == before
    assert checker.validate_packet(value) == baseline
