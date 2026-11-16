"""Observe comparison fanout with fixed artifacts; elapsed time is not a gate."""
from copy import deepcopy
import json
import time
import tracemalloc
from bench_x04_packet_validation import artifact
from compare_x04_offline_evidence import compare_evidence
from drawing_dependency_report import from_pairs
import validate_x04_evidence_packet as checker


def main():
    before = artifact(); after = deepcopy(before)
    after['drawing']['dependency_pairs'] = [[0, 'STYLE'], [3, '../synthetic-sentinel.shx']]
    after['observed_dependencies'] = from_pairs(after['drawing']['dependency_pairs'])
    expected = compare_evidence(before, after)
    observations = []
    for size in (1, 10, 100, 500):
        value = {'schema': checker.SCHEMA,
                 'artifacts': [{'id': 'a', 'artifact': deepcopy(before)}, {'id': 'b', 'artifact': deepcopy(after)}],
                 'comparisons': [{'id': f'c{i}', 'before': 'a', 'after': 'b', 'report': deepcopy(expected)} for i in range(size)],
                 'batches': []}
        snapshot = deepcopy(value); calls = 0; original = checker.validate_evidence
        def counted(item):
            nonlocal calls
            calls += 1
            return original(item)
        checker.validate_evidence = counted
        tracemalloc.start(); started = time.perf_counter()
        try:
            result = checker.validate_packet(value)
        finally:
            elapsed = time.perf_counter() - started
            _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
            checker.validate_evidence = original
        assert calls == 2 and value == snapshot
        assert all(row['report'] == expected for row in result['comparisons'])
        encoded = len((json.dumps(result, ensure_ascii=True, indent=2) + '\n').encode('utf-8'))
        result['comparisons'][0]['report']['literal_dependencies']['added'][0]['name'] = 'MUTATED'
        assert all(row['report'] == expected for row in result['comparisons'][1:])
        assert value == snapshot and checker.validate_packet(value)['comparisons'][0]['report'] == expected
        observations.append({'artifacts': 2, 'comparisons': size, 'validation_calls': calls,
                             'seconds': elapsed, 'peak_bytes': peak, 'output_bytes': encoded})
    print(json.dumps({'scope': 'embedded_artifact_report_consistency', 'timing_gates': False,
                      'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
