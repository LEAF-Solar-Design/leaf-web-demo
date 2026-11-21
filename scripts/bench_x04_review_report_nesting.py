"""X04 equal-byte nesting and flat-report observations without timing gates."""
from copy import deepcopy
import json
import time
import tracemalloc
from validate_x04_review_report import COMPARISON, MAX_JSON_CONTAINER_DEPTH, validate_report


def base():
    return {'schema': COMPARISON, 'drawing_id': 'synthetic-nesting',
            'count': {'before': 0, 'after': 0, 'delta': 0},
            'selected_ids': {'removed': [], 'added': []},
            'highlight_handles': {'removed': [], 'added': []},
            'literal_dependencies': {'removed': [], 'added': []},
            'original_bytes_integrity': 'unverified'}


def encoded_size(payload):
    return len(json.dumps(payload, ensure_ascii=True).encode('utf-8'))


def observe(report, expected_valid):
    before = deepcopy(report)
    tracemalloc.start()
    started = time.perf_counter()
    try:
        checked = validate_report(report)
    except ValueError as error:
        assert not expected_valid and 'nesting' in str(error)
        verdict = 'refused'
    else:
        assert expected_valid
        assert checked == {'schema': 'leaf.x04.review-report-validation.v1', 'valid': True,
            'report_schema': COMPARISON, 'validation_scope': 'saved_report_internal_consistency',
            'underlying_artifacts_revalidated': False, 'embedded_paths_opened': False,
            'original_bytes_integrity': 'unverified'}
        verdict = 'valid'
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert report == before
    return {'verdict': verdict, 'seconds': elapsed, 'peak_bytes': peak}


def main():
    observations = []
    limit = MAX_JSON_CONTAINER_DEPTH
    for depth in (1, 64, limit - 1, limit):
        extension = 0
        for _ in range(depth):
            extension = [extension]
        nested = base(); nested['opaque'] = extension
        flat = base(); flat['opaque'] = ''
        flat['opaque'] = 'x' * (encoded_size(nested) - encoded_size(flat))
        assert encoded_size(nested) == encoded_size(flat)
        for kind, report in [('nested', nested), ('flat', flat)]:
            expected_valid = kind == 'flat' or depth < limit
            result = observe(report, expected_valid)
            observations.append({'kind': kind, 'extension_depth': depth,
                                 'encoded_bytes': encoded_size(report), **result})
    print(json.dumps({'scope': 'saved_report_internal_consistency',
                      'supported_container_depth': limit, 'timing_gates': False,
                      'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
