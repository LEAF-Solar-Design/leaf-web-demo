"""Validate an explicit ordered list of synthetic X04 artifacts offline."""
import argparse
from pathlib import Path

from export_x04_offline_evidence import (
    DEFAULT_MAX_INPUT_BYTES, check_output_path, positive_byte_limit,
    read_json, report_failure, write_json,
)
from validate_x04_offline_evidence import validate_evidence


def validate_paths(paths, max_input_bytes=DEFAULT_MAX_INPUT_BYTES):
    if not paths or any(str(path) == '-' for path in paths):
        raise ValueError('Batch validation requires explicit artifact paths, without stdin.')
    results = []
    for index, path in enumerate(paths):
        try:
            checked = validate_evidence(read_json(path, max_input_bytes))
            results.append({'index': index, 'path': str(path), **checked})
        except (OSError, ValueError, TypeError, RecursionError) as error:
            # Keep later files independent and cap untrusted diagnostic text.
            message = 'JSON nesting exceeds the supported limit.' if isinstance(error, RecursionError) else str(error)
            results.append({'index': index, 'path': str(path), 'valid': False,
                            'error': message[:128] + ('...' if len(message) > 128 else '')})
    passed = sum(row['valid'] for row in results)
    return {'schema': 'leaf.x04.offline-batch-validation.v1', 'results': results,
            'total': len(results), 'passed': passed, 'failed': len(results) - passed,
            'original_bytes_integrity': 'unverified'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('inputs', nargs='+', help='Explicit ordered artifact paths; no glob expansion or dependency traversal')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--max-input-bytes', type=positive_byte_limit, default=DEFAULT_MAX_INPUT_BYTES)
    parser.add_argument('--max-output-bytes', type=positive_byte_limit)
    args = parser.parse_args(argv)
    try:
        for source in args.inputs:
            check_output_path(source, args.out)
        result = validate_paths(args.inputs, args.max_input_bytes)
        write_json(result, args.out, args.max_output_bytes)
    except (OSError, ValueError, TypeError, RecursionError) as error:
        return report_failure('batch validation', error)
    return 1 if result['failed'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
