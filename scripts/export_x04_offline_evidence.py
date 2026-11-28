"""Export portable synthetic X04 evidence with trusted entity-derived results."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import re

X04_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(X04_ROOT / 'server'))
import tool_loader
from drawing_dependency_report import from_pairs

SCHEMA = 'leaf.x04.offline-evidence.v1'
MAX_JSON_CONTAINER_DEPTH = 256
MAX_JSON_INTEGER_DIGITS = 4300
DEFAULT_MAX_INPUT_BYTES = 16 * 1024 * 1024
_ADDED_DEPENDENCY_FIELDS = frozenset({
    'explicit_line_linetypes_checked', 'explicit_panel_material_links',
    'note_style_links', 'table_style_links', 'tolerance_font_links',
    'linetype_shape_links',
})


def read_bounded(stream, limit):
    chunks = []
    size = 0
    while True:
        chunk = stream.read(min(65536, limit - size + 1))
        if not chunk:
            return b''.join(chunks)
        size += len(chunk)
        if size > limit:
            raise ValueError(f'Input exceeds the {limit}-byte ceiling.')
        chunks.append(chunk)


def validate_sha256_pin(value):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-fA-F]{64}', value, re.ASCII) is None:
        raise ValueError('expected_sha256 must contain exactly 64 hexadecimal characters.')
    return value.lower()


def decode_json(encoded, expected_sha256=None):
    if expected_sha256 is not None:
        expected = validate_sha256_pin(expected_sha256)
        if hashlib.sha256(encoded).hexdigest() != expected:
            raise ValueError('Explicit source bytes do not match expected_sha256.')
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'Duplicate JSON key: {key}')
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError(f'Nonfinite JSON number: {value}')

    def bounded_integer(value):
        if len(value.lstrip('-')) > MAX_JSON_INTEGER_DIGITS:
            raise ValueError(f'JSON integer token exceeds {MAX_JSON_INTEGER_DIGITS} digits.')
        return int(value)

    return json.loads(encoded.decode('utf-8-sig'), object_pairs_hook=unique_object,
                      parse_constant=reject_constant, parse_int=bounded_integer)


def read_json(path, max_input_bytes=DEFAULT_MAX_INPUT_BYTES, expected_sha256=None):
    if str(path) == '-':
        stream = getattr(sys.stdin, 'buffer', None)
        if stream is None or getattr(stream, 'closed', False):
            raise ValueError('Stdin requires an open binary input buffer.')
        encoded = read_bounded(stream, max_input_bytes)
    else:
        source = Path(path)
        if source.stat().st_size > max_input_bytes:
            raise ValueError(f'Input exceeds the {max_input_bytes}-byte ceiling.')
        with source.open('rb') as stream:
            encoded = read_bounded(stream, max_input_bytes)
    return decode_json(encoded, expected_sha256)


def positive_byte_limit(value):
    try:
        limit = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError('Byte ceiling must be a positive integer.') from None
    if limit <= 0:
        raise argparse.ArgumentTypeError('Byte ceiling must be a positive integer.')
    return limit


def _matches_dependency_report(report, pairs):
    current = from_pairs(pairs)
    if report == current:
        return True
    # Historical v1 artifacts predate the added dependency observations.
    legacy = {key: value for key, value in current.items() if key not in _ADDED_DEPENDENCY_FIELDS}
    legacy['dependencies'] = [row for row in current['dependencies'] if row['kind'] != 'linetype']
    return report == legacy


def export_evidence(payload):
    if not isinstance(payload, dict) or payload.get('schema') != SCHEMA or payload.get('synthetic') is not True:
        raise ValueError('Require the X04 offline-evidence v1 schema and synthetic=true.')
    pending = [(payload, 0)]
    while pending:
        value, depth = pending.pop()
        if isinstance(value, (dict, list, tuple)):
            if depth >= MAX_JSON_CONTAINER_DEPTH:
                raise ValueError(f'JSON nesting exceeds {MAX_JSON_CONTAINER_DEPTH} container levels.')
            children = value.values() if isinstance(value, dict) else value
            pending.extend((child, depth + 1) for child in children)
    # Refuse nonportable extensions before producing an artifact.
    json.dumps(payload, allow_nan=False, ensure_ascii=False).encode('utf-8')
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
    if not _matches_dependency_report(report, pairs):
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
    try:
        registry = json.loads((X04_ROOT / 'engine/registry.json').read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise ValueError('Tool registry engine/registry.json must be readable valid JSON.') from error
    if not isinstance(registry, dict) or not isinstance(registry.get('tools'), list):
        raise ValueError('Tool registry must contain a tools array.')
    tool = next((row for row in registry['tools']
                 if isinstance(row, dict) and row.get('name') == 'count-modules'), None)
    if tool is None:
        raise ValueError('Tool registry requires a count-modules entry.')
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


def write_all(stream, encoded, label):
    offset = 0
    while offset < len(encoded):
        chunk = encoded[offset:offset + 65536]
        written = stream.write(chunk)
        if type(written) is not int or written <= 0 or written > len(chunk):
            raise OSError(f'{label} write did not report valid forward progress.')
        offset += written


def write_json(value, output=None, max_output_bytes=None):
    encoder = json.JSONEncoder(ensure_ascii=True, allow_nan=False, indent=2)
    chunks = []
    size = 1  # Reserve the final newline. ASCII characters each occupy one UTF-8 byte.
    for chunk in encoder.iterencode(value):
        size += len(chunk)
        if max_output_bytes is not None and size > max_output_bytes:
            raise ValueError(f'Encoded output exceeds the {max_output_bytes}-byte ceiling.')
        chunks.append(chunk)
    encoded = ''.join(chunks) + '\n'
    if output is None:
        try:
            write_all(sys.stdout, encoded, 'Stdout')
            sys.stdout.flush()
        except (OSError, ValueError):
            # Retire a failed stream so interpreter shutdown does not flush it again.
            sys.stdout = None
            raise
        return
    target = Path(output)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n',
                                         dir=target.parent, prefix='.x04-', delete=False) as stream:
            temporary = Path(stream.name)
            write_all(stream, encoded, 'Temporary output')
            stream.flush()
        os.replace(temporary, target)
    except BaseException as error:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as cleanup_error:
                note = (f'Temporary file {temporary.name} remains after cleanup failed '
                        f'({type(cleanup_error).__name__}).')
                error.add_note(note)
                if isinstance(error, KeyboardInterrupt):
                    report_failure('cleanup', note)
        raise


def check_output_path(source, output):
    if output is None or str(source) == '-':
        return
    source = Path(source)
    target = Path(output)
    if source.resolve() == target.resolve() or (target.exists() and source.exists() and source.samefile(target)):
        raise ValueError('Input and output must refer to different files.')


def report_failure(operation, error):
    notes = getattr(error, '__notes__', ())
    if isinstance(error, RecursionError):
        error = 'JSON nesting exceeds the supported recursion depth.'
    message = str(error)
    message = message[:128] + ('...' if len(message) > 128 else '')
    escaped = json.dumps(message, ensure_ascii=True)
    if sys.stderr is None:
        return 2
    try:
        print(f'X04 {operation} refused: {escaped}', file=sys.stderr, flush=True)
    except (OSError, ValueError):
        # A closed diagnostic stream still leaves a deterministic failure exit.
        sys.stderr = None
    for note in notes:
        report_failure('cleanup', note)
    return 2


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('input', help='Synthetic saved evidence JSON path, or - for stdin')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--max-input-bytes', type=positive_byte_limit, default=DEFAULT_MAX_INPUT_BYTES,
                        help='Maximum input bytes, including BOM and whitespace (default: 16777216)')
    parser.add_argument('--max-output-bytes', type=positive_byte_limit,
                        help='Optional encoded JSON byte ceiling (default: unlimited)')
    args = parser.parse_args(argv)
    try:
        check_output_path(args.input, args.out)
        payload = read_json(args.input, args.max_input_bytes)
        artifact = export_evidence(payload)
        write_json(artifact, args.out, args.max_output_bytes)
    except (OSError, ValueError, TypeError, RecursionError) as error:
        return report_failure('export', error)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
