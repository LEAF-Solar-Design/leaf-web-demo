"""Assemble explicitly named local X04 evidence files into a validated packet."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from export_x04_offline_evidence import (
    DEFAULT_MAX_INPUT_BYTES, MAX_JSON_CONTAINER_DEPTH, check_output_path, positive_byte_limit,
    decode_json, read_json, report_failure, validate_sha256_pin, write_json,
)
from validate_x04_evidence_packet import SCHEMA, local_id, validate_packet
from validate_x04_review_report import require

MANIFEST_SCHEMA = 'leaf.x04.evidence-packet-manifest.v1'


class InputBudget:
    """One assembly call's actual byte budget, including every repeated read."""
    def __init__(self, ceiling):
        self.ceiling = ceiling
        self.used = 0

    def read_json(self, path, per_file, expected_sha256=None):
        if self.used >= self.ceiling:
            raise ValueError('Aggregate input byte ceiling exhausted before the next source.')
        chunks = []
        size = 0
        with Path(path).open('rb') as stream:
            while True:
                # One excess byte distinguishes exact EOF from an oversized input.
                amount = min(65536, per_file - size + 1, self.ceiling - self.used + 1)
                chunk = stream.read(amount)
                if not chunk:
                    break
                size += len(chunk)
                self.used += len(chunk)
                if size > per_file:
                    raise ValueError(f'Input exceeds the {per_file}-byte ceiling.')
                if self.used > self.ceiling:
                    raise ValueError(f'Aggregate input exceeds the {self.ceiling}-byte ceiling.')
                chunks.append(chunk)
        return decode_json(b''.join(chunks), expected_sha256)


def assemble_manifest(manifest, directory, output=None, max_input_bytes=DEFAULT_MAX_INPUT_BYTES,
                      read_input=None, require_source_pins=False):
    """Read only explicit files; relative paths are relative to the manifest directory.

    The manifest and sources are successive reads, not a simultaneous snapshot.
    Embedded dependency names and report labels never select files to read.
    """
    require(isinstance(manifest, dict) and manifest.get('schema') == MANIFEST_SCHEMA,
            'Require the X04 evidence-packet-manifest v1 schema.')
    pending = [(manifest, 0)]
    while pending:
        value, depth = pending.pop()
        if isinstance(value, (dict, list)):
            require(depth < MAX_JSON_CONTAINER_DEPTH, 'JSON nesting exceeds the supported limit.')
            children = value.values() if isinstance(value, dict) else value
            pending.extend((child, depth + 1) for child in children)
    packet = {'schema': SCHEMA}
    sources = []
    identities = set()
    for section in ('artifacts', 'comparisons', 'batches'):
        rows = manifest.get(section)
        require(isinstance(rows, list), f'Manifest {section} must be an array.')
        packet[section] = []
        for row in rows:
            require(isinstance(row, dict), 'Manifest entries must be objects.')
            identity = local_id(row.get('id'))
            require(identity not in identities, 'Manifest IDs must be globally unique.')
            identities.add(identity)
            path = row.get('path')
            require(isinstance(path, str) and bool(path.strip()), 'Manifest path must name a local file.')
            require(not require_source_pins or 'expected_sha256' in row,
                    'Every explicit source row requires expected_sha256 in strict mode.')
            pin = validate_sha256_pin(row['expected_sha256']) if 'expected_sha256' in row else None
            source = Path(directory) / path
            check_output_path(source, output)
            entry = {'id': identity}
            if section == 'comparisons':
                entry.update(before=row.get('before'), after=row.get('after'))
            elif section == 'batches':
                entry['artifact_ids'] = row.get('artifact_ids')
            packet[section].append(entry)
            sources.append((source, entry, 'artifact' if section == 'artifacts' else 'report', pin))
    # All source/output aliases are checked before any artifact or report is opened.
    reader = read_json if read_input is None else read_input
    for source, entry, field, pin in sources:
        entry[field] = reader(source, max_input_bytes) if pin is None else reader(source, max_input_bytes, pin)
    validate_packet(packet)
    return packet


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('manifest', type=Path, help='Local manifest JSON; paths resolve from its directory')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--max-input-bytes', type=positive_byte_limit, default=DEFAULT_MAX_INPUT_BYTES,
                        help='Per-file ceiling for the manifest and each explicit input')
    parser.add_argument('--max-output-bytes', type=positive_byte_limit)
    parser.add_argument('--max-total-input-bytes', type=positive_byte_limit,
                        help='Optional actual byte ceiling across manifest and every explicit read')
    parser.add_argument('--expected-manifest-sha256', type=validate_sha256_pin,
                        help='Optional SHA-256 of the exact manifest bytes read and decoded')
    parser.add_argument('--require-source-pins', action='store_true',
                        help='Require expected_sha256 on every explicit source row before payload reads')
    args = parser.parse_args(argv)
    try:
        manifest = args.manifest.resolve()
        check_output_path(manifest, args.out)
        reader = read_json if args.max_total_input_bytes is None else InputBudget(args.max_total_input_bytes).read_json
        payload = reader(manifest, args.max_input_bytes) if args.expected_manifest_sha256 is None else reader(
            manifest, args.max_input_bytes, args.expected_manifest_sha256)
        packet = assemble_manifest(payload, manifest.parent,
                                   args.out, args.max_input_bytes, read_input=reader,
                                   require_source_pins=args.require_source_pins)
        write_json(packet, args.out, args.max_output_bytes)
    except (OSError, ValueError, TypeError, RecursionError) as error:
        return report_failure('packet assembly', error)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
