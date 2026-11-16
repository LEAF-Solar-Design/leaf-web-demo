"""Validate synthetic X04 result claims by recomputing entity-derived evidence."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from export_x04_offline_evidence import check_output_path, export_evidence, read_json, report_failure, write_json


def validate_evidence(payload):
    trusted = export_evidence(payload)
    claimed = payload.get('result')
    if not isinstance(claimed, dict):
        raise ValueError('result must contain count, highlight_handles and original_bytes_integrity.')
    count = claimed.get('count')
    if type(count) is not int or count != trusted['result']['count']:
        raise ValueError('Derived count claim does not match physical entities.')
    handles = claimed.get('highlight_handles')
    if not isinstance(handles, list) or any(not isinstance(handle, str) for handle in handles):
        raise ValueError('Highlight claims must be an array of canonical parent handles.')
    if len(set(handles)) != len(handles) or set(handles) != set(trusted['result']['highlight_handles']):
        raise ValueError('Derived highlight claims do not match selected physical parents.')
    if claimed.get('original_bytes_integrity') != 'unverified':
        raise ValueError('Original-byte integrity must remain unverified for this replay.')
    return {'schema': 'leaf.x04.offline-validation.v1', 'valid': True,
            'drawing_id': trusted['drawing']['id'], 'recomputed_result': trusted['result']}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args(argv)
    try:
        check_output_path(args.input, args.out)
        payload = read_json(args.input)
        result = validate_evidence(payload)
        write_json(result, args.out)
    except (OSError, ValueError, TypeError) as error:
        return report_failure('validation', error)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
