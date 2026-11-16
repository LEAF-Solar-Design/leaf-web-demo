"""Compare two independently validated synthetic X04 evidence artifacts offline."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from export_x04_offline_evidence import (
    DEFAULT_MAX_INPUT_BYTES, check_output_path, positive_byte_limit,
    read_json, report_failure, write_json,
)
from validate_x04_offline_evidence import validate_evidence


def membership_delta(before, after):
    """Compare literal JSON values, ignoring membership order and duplicates."""
    def indexed(values):
        return {json.dumps(value, sort_keys=True, ensure_ascii=True): value for value in values}
    left, right = indexed(before), indexed(after)
    return {'removed': [left[key] for key in sorted(left.keys() - right.keys())],
            'added': [right[key] for key in sorted(right.keys() - left.keys())]}


def compare_evidence(before, after):
    left, right = validate_evidence(before), validate_evidence(after)
    if left['drawing_id'] != right['drawing_id']:
        raise ValueError('Drawing identities must match exactly before comparison.')
    first, second = left['recomputed_result'], right['recomputed_result']
    return {'schema': 'leaf.x04.offline-comparison.v1', 'drawing_id': left['drawing_id'],
            'selected_ids': membership_delta(before['request']['set_ids'], after['request']['set_ids']),
            'count': {'before': first['count'], 'after': second['count'],
                      'delta': second['count'] - first['count']},
            'highlight_handles': membership_delta(first['highlight_handles'], second['highlight_handles']),
            'literal_dependencies': membership_delta(before['observed_dependencies']['dependencies'],
                                                      after['observed_dependencies']['dependencies']),
            'original_bytes_integrity': 'unverified',
            'scope_note': 'Validated synthetic replay deltas only. Matching IDs do not establish unchanged drawing content or authenticate source bytes.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('before')
    parser.add_argument('after')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--max-input-bytes', type=positive_byte_limit, default=DEFAULT_MAX_INPUT_BYTES)
    parser.add_argument('--max-output-bytes', type=positive_byte_limit)
    args = parser.parse_args(argv)
    try:
        if args.before == args.after == '-':
            raise ValueError('At most one comparison input may use stdin.')
        for source in (args.before, args.after):
            check_output_path(source, args.out)
        result = compare_evidence(read_json(args.before, args.max_input_bytes),
                                  read_json(args.after, args.max_input_bytes))
        write_json(result, args.out, args.max_output_bytes)
    except (OSError, ValueError, TypeError, RecursionError) as error:
        return report_failure('comparison', error)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
