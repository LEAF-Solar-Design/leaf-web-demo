"""X04 contradictory-row position observations without timing thresholds."""
from copy import deepcopy
import json
import time
import tracemalloc
from validate_x04_review_report import COMPARISON, BATCH, validate_report


def reports(size):
    comparison = {'schema': COMPARISON, 'drawing_id': 'synthetic-rejection',
        'original_bytes_integrity': 'unverified',
        'count': {'before': 0, 'after': size, 'delta': size},
        'selected_ids': {'removed': [], 'added': []},
        'highlight_handles': {'removed': [], 'added': []},
        'literal_dependencies': {'removed': [], 'added': [
            {'kind': 'font', 'name': f'unopened-{i}.shx', 'disposition': 'font remapped'}
            for i in range(size)]}}
    batch = {'schema': BATCH, 'original_bytes_integrity': 'unverified',
        'total': size, 'passed': size, 'failed': 0, 'results': [
            {'index': i, 'path': '../../unopened.json', 'valid': True,
             'schema': 'leaf.x04.offline-validation.v1', 'drawing_id': 'synthetic-rejection',
             'recomputed_result': {'count': 1, 'highlight_handles': [f'H{i}'],
                                   'original_bytes_integrity': 'unverified'}} for i in range(size)]}
    return [('comparison', comparison), ('batch', batch)]


def main():
    observations = []
    for size in (10, 100, 1000):
        for kind, valid in reports(size):
            expected = validate_report(valid)
            original = deepcopy(valid)
            for position, index in [('first', 0), ('middle', size // 2), ('last', size - 1)]:
                bad = deepcopy(valid)
                if kind == 'comparison':
                    bad['literal_dependencies']['added'][index]['disposition'] = False
                else:
                    bad['results'][index]['recomputed_result']['count'] = True
                before = deepcopy(bad)
                tracemalloc.start()
                started = time.perf_counter()
                try:
                    validate_report(bad)
                except ValueError:
                    pass
                else:
                    raise AssertionError('Contradictory row accepted')
                elapsed = time.perf_counter() - started
                _, peak = tracemalloc.get_traced_memory()
                tracemalloc.stop()
                assert bad == before and valid == original
                assert validate_report(valid) == expected
                assert expected['underlying_artifacts_revalidated'] is False
                observations.append({'kind': kind, 'rows': size, 'position': position,
                    'seconds': elapsed, 'peak_bytes': peak, 'refused': True})
    print(json.dumps({'scope': 'saved_report_internal_consistency', 'timing_gates': False,
                      'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
