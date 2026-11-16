"""Export portable synthetic X04 evidence with trusted entity-derived results."""
from __future__ import annotations

import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import tempfile

X04_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(X04_ROOT / 'server'))
import tool_loader
from drawing_dependency_report import from_pairs

SCHEMA = 'leaf.x04.offline-evidence.v1'


def read_json(path):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'Duplicate JSON key: {key}')
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError(f'Nonfinite JSON number: {value}')

    return json.loads(Path(path).read_text(encoding='utf-8'),
                      object_pairs_hook=unique_object, parse_constant=reject_constant)


def export_evidence(payload):
    if not isinstance(payload, dict) or payload.get('schema') != SCHEMA or payload.get('synthetic') is not True:
        raise ValueError('Require the X04 offline-evidence v1 schema and synthetic=true.')
    # Refuse nonportable extensions before producing an artifact.
    json.dumps(payload, allow_nan=False)
    drawing = payload.get('drawing')
    request = payload.get('request')
    report = payload.get('observed_dependencies')
    if not isinstance(drawing, dict) or not isinstance(drawing.get('id'), str) or not drawing['id'].strip():
        raise ValueError('drawing.id must be a nonempty string.')
    if not isinstance(request, dict) or request.get('drawing_id') != drawing['id']:
        raise ValueError('request.drawing_id must match drawing.id exactly.')
    ids = request.get('set_ids')
    if not isinstance(ids, list) or not ids or any(not isinstance(value, str) or not value.strip() for value in ids):
        raise ValueError('request.set_ids must explicitly select nonempty string IDs.')
    if not isinstance(report, dict) or report.get('scope') != 'local_dxf_records' or report.get('external_files_opened') is not False:
        raise ValueError('Observed dependencies require local_dxf_records scope and external_files_opened=false.')
    if not isinstance(report.get('coverage_note'), str) or not report['coverage_note'].strip():
        raise ValueError('Observed dependencies require a coverage note.')
    pairs = drawing.get('dependency_pairs')
    if not isinstance(pairs, list) or any(
            not isinstance(pair, list) or len(pair) != 2 or type(pair[0]) is not int
            or not isinstance(pair[1], str) for pair in pairs):
        raise ValueError('drawing.dependency_pairs must contain decoded integer-code/string-value pairs.')
    if report != from_pairs(pairs):
        raise ValueError('Observed dependency rows and coverage must match embedded decoded records.')
    rows = report.get('dependencies')
    if not isinstance(rows, list):
        raise ValueError('Observed dependencies must be an array.')
    for row in rows:
        if not isinstance(row, dict) or any(not isinstance(row.get(field), str) or not row[field]
                                            for field in ('kind', 'name', 'disposition')):
            raise ValueError('Each observed dependency requires string kind, name and disposition.')
        if 'remapped_to' in row and not isinstance(row['remapped_to'], str):
            raise ValueError('Dependency remapped_to must be a string.')
    registry = json.loads((X04_ROOT / 'engine/registry.json').read_text(encoding='utf-8'))
    tool = next(row for row in registry['tools'] if row['name'] == 'count-modules')
    outcome = tool_loader.run_tool_dynamic(tool, drawing.get('intake'), {'set_ids': ids}, aps_live=False)
    if not outcome['ok']:
        error = outcome.get('error') or {}
        raise ValueError(error.get('message', 'Saved intake or selected IDs are invalid.'))
    result = outcome['result']
    return {'schema': SCHEMA, 'synthetic': True, 'drawing': deepcopy(drawing),
            'observed_dependencies': deepcopy(report),
            'request': {'drawing_id': drawing['id'], 'set_ids': list(dict.fromkeys(ids))},
            'result': {'count': result['count'],
                       'highlight_handles': sorted(outcome['overlay']['highlight_handles']),
                       'original_bytes_integrity': 'unverified'}}


def write_json(value, output=None):
    encoded = json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2) + '\n'
    if output is None:
        sys.stdout.write(encoded)
        return
    target = Path(output)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                         dir=target.parent, prefix='.x04-', delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(encoded)
        os.replace(temporary, target)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def check_output_path(source, output):
    if output is None:
        return
    source = Path(source)
    target = Path(output)
    if source.resolve() == target.resolve() or (target.exists() and source.samefile(target)):
        raise ValueError('Input and output must refer to different files.')


def report_failure(operation, error):
    try:
        print(f'X04 {operation} refused: {error}', file=sys.stderr)
    except (OSError, ValueError):
        # A closed diagnostic stream still leaves a deterministic failure exit.
        pass
    return 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path, help='Synthetic saved evidence JSON')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args(argv)
    try:
        check_output_path(args.input, args.out)
        payload = read_json(args.input)
        artifact = export_evidence(payload)
        write_json(artifact, args.out)
    except (OSError, ValueError, TypeError) as error:
        return report_failure('export', error)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
