"""Fixed-byte packet refusal cost as embedded artifact count grows."""
import hashlib
import json
from pathlib import Path
import tempfile
import time
import tracemalloc
from bench_x04_packet_validation import artifact
import validate_x04_evidence_packet as checker
import export_x04_offline_evidence as shared


def main():
    observations = []; embedded = artifact(); target_bytes = 131072
    with tempfile.TemporaryDirectory(prefix='x04-refusal-cost-') as directory:
        root = Path(directory); source = root / 'packet.json'; output = root / 'summary.json'
        for size in (1, 10, 100):
            value = {'schema': checker.SCHEMA, 'artifacts': [{'id': f'a{i}', 'artifact': embedded} for i in range(size)],
                     'comparisons': [], 'batches': []}
            encoded = json.dumps(value).encode(); assert len(encoded) <= target_bytes
            raw = encoded + b' ' * (target_bytes - len(encoded)); source.write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            for mode in ('stale', 'matching', 'unpinned'):
                output.write_bytes(b'previous'); calls = hashes = 0; active = False
                decode, sha, validate = shared.decode_json, shared.hashlib.sha256, checker.validate_evidence
                def observed_decode(data, pin=None):
                    nonlocal active
                    previous = active; active = pin is not None
                    try: return decode(data, pin)
                    finally: active = previous
                def observed_sha(data=b'', *args, **kwargs):
                    nonlocal hashes
                    if active: hashes += 1
                    return sha(data, *args, **kwargs)
                def trusted(item):
                    nonlocal calls
                    calls += 1; return validate(item)
                shared.decode_json, shared.hashlib.sha256, checker.validate_evidence = observed_decode, observed_sha, trusted
                tracemalloc.start(); started = time.perf_counter()
                try:
                    args = [str(source), '--out', str(output)]
                    if mode != 'unpinned': args += ['--expected-packet-sha256', '0' * 64 if mode == 'stale' else digest]
                    code = checker.main(args)
                finally:
                    elapsed = time.perf_counter() - started
                    _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
                    shared.decode_json, shared.hashlib.sha256, checker.validate_evidence = decode, sha, validate
                assert hashes == (0 if mode == 'unpinned' else 1)
                assert code == (2 if mode == 'stale' else 0) and calls == (0 if mode == 'stale' else size)
                if mode == 'stale': assert output.read_bytes() == b'previous'
                else:
                    result = json.loads(output.read_bytes())
                    assert len(result['validated_artifacts']) == size and result['valid']
                    assert all(row['recomputed_result'] == {'count': 1, 'highlight_handles': ['A'],
                        'original_bytes_integrity': 'unverified'} for row in result['validated_artifacts'])
                assert source.read_bytes() == raw and not list(root.glob('.x04-*'))
                assert checker.validate_packet(value)['valid']
                observations.append({'artifacts': size, 'packet_bytes': len(raw), 'mode': mode,
                    'hash_calls': hashes, 'trusted_calls': calls, 'seconds': elapsed, 'peak_bytes': peak})
    print(json.dumps({'scope': 'fixed_byte_packet_pin_refusal', 'hash_scope': 'packet decoding only',
                      'timing_gates': False, 'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
