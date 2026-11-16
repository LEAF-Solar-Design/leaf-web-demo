"""Assemble explicitly named local X04 evidence files into a validated packet."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from export_x04_offline_evidence import (
    DEFAULT_MAX_INPUT_BYTES, check_output_path, positive_byte_limit,
    read_json, report_failure, write_json,
)
from validate_x04_evidence_packet import SCHEMA, local_id, validate_packet
from validate_x04_review_report import require

MANIFEST_SCHEMA = 'leaf.x04.evidence-packet-manifest.v1'


def assemble_manifest(manifest, directory, output=None, max_input_bytes=DEFAULT_MAX_INPUT_BYTES):
    """Read only explicit files; relative paths are relative to the manifest directory.

    The manifest and sources are successive reads, not a simultaneous snapshot.
    Embedded dependency names and report labels never select files to read.
    """
    require(isinstance(manifest, dict) and manifest.get('schema') == MANIFEST_SCHEMA,
            'Require the X04 evidence-packet-manifest v1 schema.')
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
            source = Path(directory) / path
            check_output_path(source, output)
            entry = {'id': identity}
            if section == 'comparisons':
                entry.update(before=row.get('before'), after=row.get('after'))
            elif section == 'batches':
                entry['artifact_ids'] = row.get('artifact_ids')
            packet[section].append(entry)
            sources.append((source, entry, 'artifact' if section == 'artifacts' else 'report'))
    # All source/output aliases are checked before any artifact or report is opened.
    for source, entry, field in sources:
        entry[field] = read_json(source, max_input_bytes)
    validate_packet(packet)
    return packet


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument('manifest', type=Path, help='Local manifest JSON; paths resolve from its directory')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--max-input-bytes', type=positive_byte_limit, default=DEFAULT_MAX_INPUT_BYTES,
                        help='Per-file ceiling for the manifest and each explicit input')
    parser.add_argument('--max-output-bytes', type=positive_byte_limit)
    args = parser.parse_args(argv)
    try:
        manifest = args.manifest.resolve()
        check_output_path(manifest, args.out)
        packet = assemble_manifest(read_json(manifest, args.max_input_bytes), manifest.parent,
                                   args.out, args.max_input_bytes)
        write_json(packet, args.out, args.max_output_bytes)
    except (OSError, ValueError, TypeError, RecursionError) as error:
        return report_failure('packet assembly', error)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
