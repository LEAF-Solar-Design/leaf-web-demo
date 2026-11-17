"""Fixed-byte optional artifact pin observations as synthetic entity count grows."""
import hashlib
import json
from pathlib import Path
import tempfile
import time
import tracemalloc
from bench_x04_packet_validation import artifact
from export_x04_offline_evidence import export_evidence
import export_x04_offline_evidence as shared
import validate_x04_offline_evidence as checker


def main():
    observations = []; target_bytes = 131072
    with tempfile.TemporaryDirectory(prefix='x04-artifact-pin-') as directory:
        root = Path(directory); source = root / 'artifact.json'; output = root / 'checked.json'
        for size in (1, 10, 100):
            payload = artifact(); handles = [f'A{i:03d}' for i in range(size)]
            payload['drawing']['intake']['inserts'] = [
                {'layer': 'Modules', 'name': 'Panel', 'handle': handle} for handle in handles]
            value = export_evidence(payload); identity = value['drawing']['id']
            encoded = json.dumps(value).encode(); assert len(encoded) <= target_bytes
            raw = encoded + b' ' * (target_bytes - len(encoded)); source.write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            for mode in ('stale', 'matching', 'omitted'):
                output.write_bytes(b'previous'); hashes = calls = 0; active = False
                decode, sha, export = shared.decode_json, shared.hashlib.sha256, checker.export_evidence
                def observed_decode(data, pin=None):
                    nonlocal active
                    previous = active; active = pin is not None
                    try: return decode(data, pin)
                    finally: active = previous
                def observed_sha(data=b'', *args, **kwargs):
                    nonlocal hashes
                    if active: hashes += 1
                    return sha(data, *args, **kwargs)
                def counted(item):
                    nonlocal calls
                    calls += 1; return export(item)
                shared.decode_json, shared.hashlib.sha256, checker.export_evidence = observed_decode, observed_sha, counted
                tracemalloc.start(); started = time.perf_counter()
                try:
                    args = [str(source), '--out', str(output), '--expected-drawing-id', identity]
                    if mode != 'omitted': args += ['--expected-artifact-sha256', '0' * 64 if mode == 'stale' else digest]
                    code = checker.main(args)
                finally:
                    elapsed = time.perf_counter() - started
                    _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
                    shared.decode_json, shared.hashlib.sha256, checker.export_evidence = decode, sha, export
                assert code == (2 if mode == 'stale' else 0)
                assert hashes == (0 if mode == 'omitted' else 1) and calls == (0 if mode == 'stale' else 1)
                if mode == 'stale': assert output.read_bytes() == b'previous'
                else:
                    result = json.loads(output.read_bytes())
                    assert result['drawing_id'] == identity
                    assert result['recomputed_result'] == {'count': size, 'highlight_handles': handles,
                                                          'original_bytes_integrity': 'unverified'}
                assert source.read_bytes() == raw and not list(root.glob('.x04-*'))
                observations.append({'entities': size, 'artifact_bytes': len(raw), 'mode': mode,
                    'hash_calls': hashes, 'recomputation_calls': calls, 'seconds': elapsed, 'peak_bytes': peak})
    print(json.dumps({'scope': 'fixed_byte_artifact_pin_refusal', 'hash_scope': 'artifact decoding only',
                      'timing_gates': False, 'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
