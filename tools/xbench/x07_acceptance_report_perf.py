"""Bounded disposable timings for X07 inventory hashing and report aggregation."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import time
import tracemalloc
from x07_offline_acceptance_report import REQUIRED_SOURCE_FILES, REQUIRED_ASSERTIONS, aggregate, verify_files


def measure(bytes_per_file, proof_count, iterations):
    if not 1 <= bytes_per_file <= 65536 or not 1 <= proof_count <= 16 or not 1 <= iterations <= 20:
        raise ValueError('offline measurement bounds exceeded')
    with tempfile.TemporaryDirectory(prefix='x07-report-perf-') as directory:
        root = Path(directory)
        payload = b'x' * bytes_per_file
        expected = hashlib.sha256(payload).hexdigest()
        files = {}
        for name in REQUIRED_SOURCE_FILES:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(payload)
            files[name] = expected
        source = 'a' * 40
        journey = {'schema': 'leaf.x07.synthetic-journey.v1', 'proof_mode': 'synthetic_offline',
            'source_sha': source, 'source_dirty': False, 'files': files,
            'steps': [{'n': number, 'passed': True, 'assertions': {name: True for name in REQUIRED_ASSERTIONS[number]}} for number in range(1, 8)]}
        journey_path = root / 'journey.json'
        journey_path.write_text(json.dumps(journey), encoding='utf-8')
        proofs = []
        for number in range(proof_count):
            path = root / ('proof-' + str(number) + '.json')
            path.write_text(json.dumps({'schema': 'leaf.x07.offline-proof.v1', 'source_sha': source, 'files': files}), encoding='utf-8')
            proofs.append(path)
        tracemalloc.start()
        try:
            start = time.perf_counter()
            for _ in range(iterations): verify_files(journey, root)
            digest_seconds = time.perf_counter() - start
            start = time.perf_counter()
            for _ in range(iterations):
                result = aggregate(journey_path, proofs, source, root)
                assert [step['n'] for step in result['steps']] == list(range(1, 8))
                assert all(step['state'] == 'SYNTHETIC_PASS' for step in result['steps'])
                assert len(result['local_proofs']) == proof_count
                assert result['source_sha'] == source and result['source_dirty'] is False
            aggregate_seconds = time.perf_counter() - start
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert all((root / name).read_bytes() == payload for name in REQUIRED_SOURCE_FILES)
        return {'bytes_per_file': bytes_per_file, 'source_files': len(files), 'proofs': proof_count,
            'iterations': iterations, 'digest_seconds': round(digest_seconds, 6),
            'aggregate_seconds': round(aggregate_seconds, 6), 'peak_traced_bytes': peak,
            'independent_results_match': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--smoke', action='store_true')
    parser.add_argument('--out', type=Path)
    args = parser.parse_args()
    points = [(64, 1), (1024, 4)] if args.smoke else [(64, 1), (4096, 8), (65536, 16)]
    result = {'schema': 'leaf.x07.acceptance-report-perf.v1', 'proof_mode': 'synthetic_offline',
        'rows': [measure(size, proofs, 2 if args.smoke else 10) for size, proofs in points]}
    encoded = json.dumps(result, indent=2) + '\n'
    if args.out: args.out.write_text(encoded, encoding='utf-8')
    print(encoded, end='')


if __name__ == '__main__': main()
