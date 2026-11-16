"""X04 fixed-row report payload-width observations without timing thresholds."""
from copy import deepcopy
import json
import time
import tracemalloc
from validate_x04_review_report import BATCH, COMPARISON, validate_report


def main():
    observations = []
    rows = 20
    previous_bytes = {}
    for width in (8, 128, 2048):
        comparison = {'schema': COMPARISON, 'drawing_id': 'synthetic-width',
            'original_bytes_integrity': 'unverified',
            'count': {'before': rows, 'after': rows, 'delta': 0},
            'selected_ids': {'removed': [], 'added': []},
            'highlight_handles': {'removed': [], 'added': []},
            'literal_dependencies': {'removed': [], 'added': [
                {'kind': 'font', 'name': f'{i}-' + 'x' * width + '.shx',
                 'disposition': 'font remapped'} for i in range(rows)]}}
        batch = {'schema': BATCH, 'total': rows, 'passed': rows, 'failed': 0,
            'original_bytes_integrity': 'unverified', 'results': [
                {'index': i, 'path': '../../' + 'x' * width + f'/{i}.json',
                 'schema': 'leaf.x04.offline-validation.v1', 'valid': True,
                 'drawing_id': 'synthetic-width', 'recomputed_result': {
                     'count': i + 1, 'highlight_handles': [f'H{i}'],
                     'original_bytes_integrity': 'unverified'}} for i in range(rows)]}
        for kind, report in [('comparison', comparison), ('batch', batch)]:
            before = deepcopy(report)
            encoded = len(json.dumps(report, ensure_ascii=True).encode('utf-8'))
            tracemalloc.start()
            start = time.perf_counter()
            checked = validate_report(report)
            elapsed = time.perf_counter() - start
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            assert report == before
            assert checked == {'schema': 'leaf.x04.review-report-validation.v1', 'valid': True,
                'report_schema': report['schema'], 'validation_scope': 'saved_report_internal_consistency',
                'underlying_artifacts_revalidated': False, 'embedded_paths_opened': False,
                'original_bytes_integrity': 'unverified'}
            assert encoded > previous_bytes.get(kind, 0)
            previous_bytes[kind] = encoded
            assert len(report['results'] if kind == 'batch' else report['literal_dependencies']['added']) == rows
            observations.append({'kind': kind, 'rows': rows, 'literal_width': width,
                                 'encoded_bytes': encoded, 'seconds': elapsed, 'peak_bytes': peak})
    print(json.dumps({'scope': 'saved_report_internal_consistency', 'timing_gates': False,
                      'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
