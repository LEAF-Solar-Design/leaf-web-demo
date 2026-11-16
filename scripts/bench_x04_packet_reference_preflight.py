"""X04 missing-reference preflight observations without timing thresholds."""
from copy import deepcopy
import json
import time
import tracemalloc
from bench_x04_packet_unused_artifacts import example
import validate_x04_evidence_packet as checker


def main():
    observations = []
    for size in (10, 100, 1000):
        valid = example(1)
        template = valid['comparisons'][0]
        valid['comparisons'] = [{**deepcopy(template), 'id': f'comparison{i:04d}'} for i in range(size)]
        original = deepcopy(valid)
        for position, index in [('first', 0), ('middle', size // 2), ('last', size - 1)]:
            bad = deepcopy(valid)
            bad['comparisons'][index]['before'] = 'missing'
            before = deepcopy(bad); calls = []
            real_validate = checker.validate_evidence
            def denied(value):
                calls.append(value)
                raise AssertionError('Missing reference reached trusted validation')
            checker.validate_evidence = denied
            tracemalloc.start(); started = time.perf_counter()
            try:
                try: checker.validate_packet(bad)
                except ValueError as error: assert 'reference' in str(error)
                else: raise AssertionError('Missing reference accepted')
            finally:
                elapsed = time.perf_counter() - started
                _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
                checker.validate_evidence = real_validate
            assert calls == [] and bad == before and valid == original
            recovered = checker.validate_packet(valid)
            assert recovered['valid'] and len(recovered['comparisons']) == size
            assert recovered['original_bytes_integrity'] == 'unverified'
            observations.append({'comparisons': size, 'position': position,
                'trusted_validations_before_refusal': len(calls), 'seconds': elapsed,
                'peak_bytes': peak, 'valid_recovery': True})
    print(json.dumps({'scope': 'embedded_artifact_report_consistency', 'timing_gates': False,
                      'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
