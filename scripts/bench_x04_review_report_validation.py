"""Studio X-bench X04 saved-report consistency observations, without timing gates."""
from copy import deepcopy
import json
import time
import tracemalloc

from validate_x04_review_report import BATCH, COMPARISON, validate_report


def measure(payload):
    before = deepcopy(payload)
    tracemalloc.start()
    start = time.perf_counter()
    checked = validate_report(payload)
    elapsed = time.perf_counter() - start
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert payload == before
    assert checked['valid'] and not checked['underlying_artifacts_revalidated']
    assert checked['original_bytes_integrity'] == 'unverified'
    return {'seconds': elapsed, 'peak_bytes': peak}


def main():
    observations = []
    for size in (10, 100, 1000):
        comparison = {'schema': COMPARISON, 'drawing_id': 'synthetic-benchmark',
                      'original_bytes_integrity': 'unverified',
                      'count': {'before': size, 'after': 2 * size, 'delta': size}}
        for field in ('selected_ids', 'highlight_handles'):
            comparison[field] = {'removed': [f'old-{i}' for i in range(size)],
                                 'added': [f'new-{i}' for i in range(size)]}
        comparison['literal_dependencies'] = {'removed': [], 'added': [
            {'kind': 'font', 'name': f'unopened-{i}.shx', 'disposition': 'font remapped'}
            for i in range(size)]}
        batch = {'schema': BATCH, 'original_bytes_integrity': 'unverified',
                 'total': size, 'passed': (size + 1) // 2, 'failed': size // 2, 'results': []}
        for index in range(size):
            row = {'index': index, 'path': '../../unopened-repeated.json', 'valid': index % 2 == 0}
            if row['valid']:
                row.update(schema='leaf.x04.offline-validation.v1', drawing_id='synthetic-benchmark',
                           recomputed_result={'count': index + 1, 'highlight_handles': [f'H{index}'],
                                              'original_bytes_integrity': 'unverified'})
            else:
                row['error'] = 'Synthetic refusal'
            batch['results'].append(row)
        for kind, payload in [('comparison', comparison), ('batch', batch)]:
            result = measure(payload)
            forged = deepcopy(payload)
            if kind == 'comparison':
                forged['count']['delta'] += 1
            else:
                forged['passed'], forged['failed'] = forged['failed'], forged['passed']
                # Even cohort sizes have equal passed/failed counts.
                forged['passed'] += 1
                forged['failed'] -= 1
            try:
                validate_report(forged)
            except ValueError:
                pass
            else:
                raise AssertionError('Forged arithmetic accepted')
            observations.append({'kind': kind, 'size': size, **result})
    print(json.dumps({'scope': 'saved_report_internal_consistency',
                      'timing_gates': False, 'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
