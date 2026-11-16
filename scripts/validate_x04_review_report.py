"""Check X04 saved review reports for internal consistency only, without path traversal."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from export_x04_offline_evidence import (
    DEFAULT_MAX_INPUT_BYTES, MAX_JSON_CONTAINER_DEPTH, check_output_path,
    positive_byte_limit, read_json, report_failure, validate_sha256_pin, write_json,
)

COMPARISON = 'leaf.x04.offline-comparison.v1'
BATCH = 'leaf.x04.offline-batch-validation.v1'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def integer(value, label, nonnegative=True):
    require(type(value) is int and (not nonnegative or value >= 0),
            f'{label} must be an integer' + (' >= 0.' if nonnegative else '.'))
    return value


def text(value, label):
    require(isinstance(value, str) and bool(value.strip()), f'{label} must be a nonempty string.')


def integrity(value):
    require(value == 'unverified', 'Original-byte integrity must remain unverified.')


def membership(rows, label, dependencies=False):
    require(isinstance(rows, list), f'{label} must be an array.')
    identities = set()
    for row in rows:
        if dependencies:
            require(isinstance(row, dict), f'{label} dependency must be an object.')
            for field in ('kind', 'name', 'disposition'):
                text(row.get(field), f'{label}.{field}')
            for field in ('remapped_to', 'reason'):
                if field in row:
                    text(row[field], f'{label}.{field}')
        else:
            text(row, label)
        identity = json.dumps(row, sort_keys=True, ensure_ascii=True, allow_nan=False)
        require(identity not in identities, f'{label} contains duplicate memberships.')
        identities.add(identity)
    return identities


def delta(value, label, dependencies=False):
    require(isinstance(value, dict) and set(value) == {'removed', 'added'},
            f'{label} must contain removed and added arrays only.')
    removed = membership(value['removed'], f'{label}.removed', dependencies)
    added = membership(value['added'], f'{label}.added', dependencies)
    require(not removed.intersection(added), f'{label} additions and removals must be disjoint.')


def validate_report(payload):
    """Validate claims inside a saved report, never its underlying artifacts."""
    pending = [(payload, 0)]
    while pending:
        value, depth = pending.pop()
        if isinstance(value, (dict, list)):
            require(depth < MAX_JSON_CONTAINER_DEPTH, 'JSON nesting exceeds the supported limit.')
            children = value.values() if isinstance(value, dict) else value
            pending.extend((child, depth + 1) for child in children)
    # Keep the pure API subject to the same finite, portable JSON boundary as the CLI.
    json.dumps(payload, allow_nan=False, ensure_ascii=False).encode('utf-8')
    require(isinstance(payload, dict), 'Saved review report must be an object.')
    schema = payload.get('schema')
    require(schema in (COMPARISON, BATCH), 'Unsupported saved review report schema.')
    integrity(payload.get('original_bytes_integrity'))
    if schema == COMPARISON:
        text(payload.get('drawing_id'), 'drawing_id')
        count = payload.get('count')
        require(isinstance(count, dict), 'count must be an object.')
        before = integer(count.get('before'), 'count.before')
        after = integer(count.get('after'), 'count.after')
        difference = integer(count.get('delta'), 'count.delta', False)
        require(difference == after - before, 'count.delta does not equal after minus before.')
        for label in ('selected_ids', 'highlight_handles', 'literal_dependencies'):
            delta(payload.get(label), label, label == 'literal_dependencies')
    else:
        rows = payload.get('results')
        require(isinstance(rows, list), 'results must be an array.')
        total = integer(payload.get('total'), 'total')
        passed = integer(payload.get('passed'), 'passed')
        failed = integer(payload.get('failed'), 'failed')
        require(total == len(rows) and passed + failed == total, 'Batch totals disagree with results.')
        successes = 0
        for index, row in enumerate(rows):
            require(isinstance(row, dict), 'Batch result must be an object.')
            require(type(row.get('index')) is int and row['index'] == index,
                    'Batch indices must be contiguous and ordered from zero.')
            require(isinstance(row.get('path'), str), 'Batch path must be a literal string.')
            require(type(row.get('valid')) is bool, 'Batch valid must be boolean.')
            if row['valid']:
                require('error' not in row, 'Successful row cannot contain an error.')
                require(row.get('schema') == 'leaf.x04.offline-validation.v1',
                        'Successful row must have the validation result schema.')
                text(row.get('drawing_id'), 'drawing_id')
                result = row.get('recomputed_result')
                require(isinstance(result, dict), 'Successful row requires recomputed_result.')
                integer(result.get('count'), 'recomputed_result.count')
                membership(result.get('highlight_handles'), 'recomputed_result.highlight_handles')
                integrity(result.get('original_bytes_integrity'))
                successes += 1
            else:
                require(isinstance(row.get('error'), str) and len(row['error']) <= 131,
                        'Failed row requires an error string of at most 131 characters.')
                require(not {'drawing_id', 'recomputed_result', 'schema'}.intersection(row),
                        'Failed row cannot contain successful validation claims.')
        require(successes == passed, 'Batch passed/failed claims disagree with valid rows.')
    return {'schema': 'leaf.x04.review-report-validation.v1', 'valid': True,
            'report_schema': schema, 'validation_scope': 'saved_report_internal_consistency',
            'underlying_artifacts_revalidated': False, 'embedded_paths_opened': False,
            'original_bytes_integrity': 'unverified'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('input', help='Saved review report JSON path, or - for stdin')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--max-input-bytes', type=positive_byte_limit, default=DEFAULT_MAX_INPUT_BYTES)
    parser.add_argument('--max-output-bytes', type=positive_byte_limit)
    parser.add_argument('--expected-report-sha256', type=validate_sha256_pin,
                        help='Optional SHA-256 of the exact file or stdin bytes read and decoded')
    args = parser.parse_args(argv)
    try:
        check_output_path(args.input, args.out)
        payload = read_json(args.input, args.max_input_bytes) if args.expected_report_sha256 is None else read_json(
            args.input, args.max_input_bytes, args.expected_report_sha256)
        result = validate_report(payload)
        write_json(result, args.out, args.max_output_bytes)
    except (OSError, ValueError, TypeError, RecursionError) as error:
        return report_failure('saved report validation', error)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
