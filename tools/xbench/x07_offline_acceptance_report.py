"""Aggregate source-bound X07 synthetic receipts without network access."""
import argparse
import json
from pathlib import Path
import re


class EvidenceError(ValueError):
    pass


def read_receipt(path):
    try:
        with Path(path).open('rb') as stream:
            raw = stream.read(262145)
        if len(raw) > 262144:
            raise ValueError()
        value = json.loads(raw.decode('utf-8'))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except FileNotFoundError:
        raise EvidenceError('MISSING_RECEIPT') from None
    except (ValueError, UnicodeError, OSError, RecursionError):
        raise EvidenceError('MALFORMED_RECEIPT') from None


def check_source(receipt, expected):
    identities = [receipt[key] for key in ('source_sha', 'local_source') if key in receipt]
    if not identities:
        raise EvidenceError('MISSING_SOURCE_BINDING')
    if any(value != expected for value in identities):
        raise EvidenceError('SOURCE_MISMATCH')


def aggregate(journey_path, proof_paths, source_sha):
    if not isinstance(source_sha, str) or re.fullmatch('[0-9a-f]{40}', source_sha) is None:
        raise EvidenceError('INVALID_SOURCE')
    if not 1 <= len(proof_paths) <= 16:
        raise EvidenceError('MISSING_OR_EXCESS_PROOFS')
    journey = read_receipt(journey_path)
    check_source(journey, source_sha)
    if journey.get('schema') != 'leaf.x07.synthetic-journey.v1' or journey.get('proof_mode') != 'synthetic_offline':
        raise EvidenceError('MALFORMED_JOURNEY')
    steps = journey.get('steps')
    if not isinstance(steps, list) or len(steps) != 7:
        raise EvidenceError('MALFORMED_STEPS')
    results = {}
    for step in steps:
        if not isinstance(step, dict) or type(step.get('n')) is not int or step['n'] not in range(1, 8) or step['n'] in results:
            raise EvidenceError('MALFORMED_STEPS')
        assertions = step.get('assertions')
        if not isinstance(assertions, dict) or not assertions or any(type(value) is not bool for value in assertions.values()):
            raise EvidenceError('MALFORMED_ASSERTIONS')
        passed = all(assertions.values())
        if type(step.get('passed')) is not bool or step['passed'] != passed:
            raise EvidenceError('INCONSISTENT_STEP')
        results[step['n']] = {'n': step['n'], 'state': 'SYNTHETIC_PASS' if passed else 'SYNTHETIC_FAIL',
            'assertions': len(assertions)}
    proofs = []
    for path in proof_paths:
        proof = read_receipt(path)
        check_source(proof, source_sha)
        files = proof.get('files')
        if proof.get('schema') != 'leaf.x07.offline-proof.v1' or not isinstance(files, dict) or not files:
            raise EvidenceError('MALFORMED_PROOF')
        if any(not isinstance(name, str) or not isinstance(digest, str) or re.fullmatch('[0-9a-f]{64}', digest) is None
               for name, digest in files.items()):
            raise EvidenceError('MALFORMED_PROOF')
        proofs.append({'receipt': Path(path).name, 'source_sha': source_sha, 'file_digests': len(files)})
    return {'schema': 'leaf.x07.offline-acceptance-report.v1', 'proof_mode': 'synthetic_offline',
        'source_sha': source_sha, 'steps': [results[number] for number in range(1, 8)], 'local_proofs': proofs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source-sha', required=True)
    parser.add_argument('--journey', type=Path, required=True)
    parser.add_argument('--proof', type=Path, action='append', required=True)
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    code = 0
    try:
        result = aggregate(args.journey, args.proof, args.source_sha)
    except EvidenceError as error:
        code = 2
        result = {'schema': 'leaf.x07.offline-acceptance-report.v1', 'proof_mode': 'synthetic_offline',
            'error': str(error), 'steps': [{'n': number, 'state': 'UNREADABLE'} for number in range(1, 8)]}
    encoded = json.dumps(result, indent=2) + '\n'
    if args.out:
        args.out.write_text(encoded, encoding='utf-8')
    print(encoded, end='')
    return code


if __name__ == '__main__':
    raise SystemExit(main())
