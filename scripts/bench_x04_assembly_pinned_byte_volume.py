"""Fixed pinned fraction/source count as equal-claim source byte volume grows."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import tracemalloc
from bench_x04_packet_validation import artifact
import assemble_x04_evidence_packet as assembler
import export_x04_offline_evidence as shared
import validate_x04_evidence_packet as checker


def main():
    observations = []; embedded = artifact(); encoded = json.dumps(embedded).encode(); size = 6
    with tempfile.TemporaryDirectory(prefix='x04-pin-volume-') as directory:
        root = Path(directory); source = root / 'manifest.json'; expected = None
        for volume in (1024, 4096, 16384):
            assert volume >= len(encoded)
            for i in range(size): (root / f'source-{i}.json').write_bytes(encoded + b' ' * (volume - len(encoded)))
            for pinned in (False, True):
                manifest = {'schema': assembler.MANIFEST_SCHEMA,
                    'artifacts': [{'id': f'a{i}', 'path': f'source-{i}.json'} for i in range(size)],
                    'comparisons': [], 'batches': []}
                if pinned:
                    for row in manifest['artifacts']: row['expected_sha256'] = hashlib.sha256((root / row['path']).read_bytes()).hexdigest()
                source.write_bytes(json.dumps(manifest).encode())
                paths = [source] + [root / row['path'] for row in manifest['artifacts']]
                snapshots = {p: p.read_bytes() for p in paths}; before = deepcopy(manifest)
                reads = []; byte_count = hashes = 0; active = False
                read, decode, sha = assembler.read_json, shared.decode_json, shared.hashlib.sha256
                def observed_read(path, *args): reads.append(path); return read(path, *args)
                def observed_decode(data, pin=None):
                    nonlocal byte_count, active
                    byte_count += len(data); previous = active; active = pin is not None
                    try: return decode(data, pin)
                    finally: active = previous
                def observed_sha(data=b'', *args, **kwargs):
                    nonlocal hashes
                    if active: hashes += 1
                    return sha(data, *args, **kwargs)
                shared.decode_json, shared.hashlib.sha256 = observed_decode, observed_sha
                tracemalloc.start(); started = time.perf_counter()
                try:
                    parsed = observed_read(source, assembler.DEFAULT_MAX_INPUT_BYTES)
                    packet = assembler.assemble_manifest(parsed, root, read_input=observed_read)
                finally:
                    elapsed = time.perf_counter() - started
                    _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
                    shared.decode_json, shared.hashlib.sha256 = decode, sha
                if expected is None: expected = packet
                assert packet == expected and parsed == before
                assert reads == paths and byte_count == sum(map(len, snapshots.values()))
                assert hashes == (size if pinned else 0)
                assert all(p.read_bytes() == data for p, data in snapshots.items())
                checked = checker.validate_packet(packet)
                assert [row['recomputed_result']['count'] for row in checked['validated_artifacts']] == [1] * size
                observations.append({'entries': size, 'pinned_fraction': int(pinned), 'source_bytes_each': volume,
                    'bytes_read': byte_count, 'reads': len(reads), 'hash_calls': hashes,
                    'seconds': elapsed, 'peak_bytes': peak})
    print(json.dumps({'scope': 'fixed_fraction_pinned_source_volume', 'hash_scope': 'pinned source decoding',
                      'timing_gates': False, 'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
