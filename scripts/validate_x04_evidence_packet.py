"""Validate embedded X04 artifact/report bindings offline using packet-local IDs."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from export_x04_offline_evidence import (
    DEFAULT_MAX_INPUT_BYTES, MAX_JSON_CONTAINER_DEPTH, check_output_path,
    positive_byte_limit, read_json, report_failure, write_json,
)
from validate_x04_offline_evidence import validate_evidence
from compare_x04_offline_evidence import compare_evidence
from validate_x04_review_report import BATCH, validate_report, require

SCHEMA = 'leaf.x04.evidence-packet.v1'
LOCAL_ID = re.compile(r'[A-Za-z][A-Za-z0-9_-]{0,63}', re.ASCII)


def local_id(value):
    require(isinstance(value, str) and LOCAL_ID.fullmatch(value) is not None,
            'Packet IDs must be ASCII local labels, not paths or URLs.')
    return value


def comparison_claims(report):
    result = {key: report[key] for key in ('drawing_id', 'count', 'original_bytes_integrity')}
    for field in ('selected_ids', 'highlight_handles', 'literal_dependencies'):
        result[field] = {side: sorted(json.dumps(row, sort_keys=True, ensure_ascii=True)
                                     for row in report[field][side])
                         for side in ('removed', 'added')}
    return result


def validate_packet(payload):
    """Cross-check embedded claims; never resolve IDs or report path labels externally."""
    pending = [(payload, 0)]
    while pending:
        value, depth = pending.pop()
        if isinstance(value, (dict, list)):
            require(depth < MAX_JSON_CONTAINER_DEPTH, 'JSON nesting exceeds the supported limit.')
            children = value.values() if isinstance(value, dict) else value
            pending.extend((child, depth + 1) for child in children)
    json.dumps(payload, allow_nan=False, ensure_ascii=False).encode('utf-8')
    require(isinstance(payload, dict) and payload.get('schema') == SCHEMA,
            'Require the X04 evidence-packet v1 schema.')
    sections = {}
    ids = set()
    for section in ('artifacts', 'comparisons', 'batches'):
        rows = payload.get(section)
        require(isinstance(rows, list), f'Packet {section} must be an array.')
        for row in rows:
            require(isinstance(row, dict), f'Packet {section} entry must be an object.')
            identity = local_id(row.get('id'))
            require(identity not in ids, 'Packet IDs must be globally unique.')
            ids.add(identity)
        sections[section] = rows
    require(bool(sections['artifacts']), 'Packet requires at least one embedded artifact.')
    artifacts = {row['id']: row.get('artifact') for row in sections['artifacts']}
    def reference(value):
        identity = local_id(value)
        require(identity in artifacts, 'Packet reference must name an embedded artifact, never another report.')
        return identity
    # Refuse all reference defects before invoking trusted validators.
    for row in sections['comparisons']:
        reference(row.get('before')); reference(row.get('after'))
    for row in sections['batches']:
        refs = row.get('artifact_ids')
        require(isinstance(refs, list) and bool(refs), 'Batch artifact_ids must be a nonempty ordered array.')
        for value in refs:
            reference(value)
    checked = {identity: validate_evidence(artifact) for identity, artifact in artifacts.items()}
    comparisons, batches = [], []
    for row in sections['comparisons']:
        claimed = row.get('report')
        validate_report(claimed)
        require(claimed.get('schema') == 'leaf.x04.offline-comparison.v1', 'Comparison requires a comparison report.')
        expected = compare_evidence(artifacts[row['before']], artifacts[row['after']])
        require(comparison_claims(claimed) == comparison_claims(expected),
                'Comparison claims contradict the ordered embedded artifacts.')
        comparisons.append({'id': row['id'], 'before': row['before'], 'after': row['after'], 'report': expected})
    for row in sections['batches']:
        claimed = row.get('report')
        validate_report(claimed)
        require(claimed.get('schema') == BATCH, 'Batch requires a batch report.')
        refs = row['artifact_ids']
        require(len(claimed['results']) == len(refs), 'Batch occurrence count contradicts artifact_ids.')
        expected_rows = []
        for index, (identity, result) in enumerate(zip(refs, claimed['results'])):
            # Path is a literal display label, not a reference or a filesystem binding.
            expected = {'index': index, 'path': result['path'], **checked[identity]}
            require(result['valid'] is True and result.get('drawing_id') == expected['drawing_id']
                    and result.get('recomputed_result', {}).get('count') == expected['recomputed_result']['count']
                    and set(result.get('recomputed_result', {}).get('highlight_handles', []))
                    == set(expected['recomputed_result']['highlight_handles']),
                    'Batch row contradicts its ordered embedded artifact.')
            expected_rows.append(deepcopy(expected))
        report = {'schema': BATCH, 'results': expected_rows, 'total': len(refs),
                  'passed': len(refs), 'failed': 0, 'original_bytes_integrity': 'unverified'}
        batches.append({'id': row['id'], 'artifact_ids': list(refs), 'report': report})
    return {'schema': 'leaf.x04.evidence-packet-validation.v1', 'valid': True,
            'validation_scope': 'embedded_artifact_report_consistency',
            'validated_artifacts': [{'id': identity, **result} for identity, result in checked.items()],
            'comparisons': comparisons, 'batches': batches, 'external_paths_opened': False,
            'original_bytes_integrity': 'unverified'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('input', help='Self-contained packet JSON, or - for stdin')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--max-input-bytes', type=positive_byte_limit, default=DEFAULT_MAX_INPUT_BYTES)
    parser.add_argument('--max-output-bytes', type=positive_byte_limit)
    args = parser.parse_args(argv)
    try:
        check_output_path(args.input, args.out)
        result = validate_packet(read_json(args.input, args.max_input_bytes))
        write_json(result, args.out, args.max_output_bytes)
    except (OSError, ValueError, TypeError, RecursionError) as error:
        return report_failure('packet validation', error)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
