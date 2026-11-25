"""Concurrent packets may reuse IDs without leaking drawings or mutable results."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading
import pytest
from test_x04_packet_cross_binding import packet
from test_x04_comparison_delta_algebra import artifact
from compare_x04_offline_evidence import compare_evidence
import validate_x04_evidence_packet as checker


@pytest.mark.parametrize('failed', [None, 0, 1])
def test_parallel_packet_isolation(failed, monkeypatch):
    values = [packet(), packet()]
    values[1]['artifacts'][0]['artifact'] = artifact(3)
    for row in values[1]['artifacts']:
        row['artifact']['drawing']['id'] = 'independent-drawing'
        row['artifact']['request']['drawing_id'] = 'independent-drawing'
    a, b = [row['artifact'] for row in values[1]['artifacts']]
    values[1]['comparisons'][0]['report'] = compare_evidence(a, b)
    for index, item in enumerate((a, b, a)):
        values[1]['batches'][0]['report']['results'][index].update(checker.validate_evidence(item))
    expected = [checker.validate_packet(value) for value in values]
    if failed is not None:
        values[failed]['comparisons'][0]['report']['count']['delta'] += 1
    before = deepcopy(values)
    barrier = threading.Barrier(2); local = threading.local()
    original = checker.validate_evidence
    def synchronized(value):
        if not getattr(local, 'entered', False):
            local.entered = True; barrier.wait(timeout=20)
        return original(value)
    def run(value):
        try: return checker.validate_packet(value)
        except ValueError as error: return error
    with monkeypatch.context() as patch:
        patch.setattr(checker, 'validate_evidence', synchronized)
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(run, value) for value in values]
            actual = [future.result(timeout=30) for future in futures]
    assert values == before
    for index, result in enumerate(actual):
        if index == failed: assert isinstance(result, ValueError)
        else: assert result == expected[index]
    fresh = [checker.validate_packet(packet()), checker.validate_packet(before[1] if failed != 1 else values[0])]
    # Mutating a returned nested result must never alter another result or any input.
    retained = deepcopy(fresh[1])
    fresh[0]['validated_artifacts'][0]['recomputed_result']['count'] = 999
    assert fresh[1] == retained and values == before
    if failed is not None:
        values[failed]['comparisons'][0]['report']['count']['delta'] -= 1
        assert checker.validate_packet(values[failed]) == expected[failed]
