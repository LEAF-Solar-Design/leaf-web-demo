"""Growing ignored manifest bytes with pinned/unpinned manifest controls."""
import hashlib
import json
from pathlib import Path
import tempfile
import time
import tracemalloc
from bench_x04_packet_validation import artifact
import assemble_x04_evidence_packet as assembler
import export_x04_offline_evidence as shared


def main():
    observations = []; embedded = artifact(); expected = None
    with tempfile.TemporaryDirectory(prefix='x04-manifest-volume-') as directory:
        root = Path(directory); source = root / 'manifest.json'; output = root / 'packet.json'
        rows = []
        for i in range(4):
            path = root / f'a{i}.json'; path.write_bytes(json.dumps(embedded).encode())
            rows.append({'id': f'a{i}', 'path': path.name, 'expected_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        for padding in (0, 4096, 65536):
            manifest = {'schema': assembler.MANIFEST_SCHEMA, 'artifacts': rows,
                        'comparisons': [], 'batches': [], 'ignored_padding': 'p' * padding}
            source.write_bytes(json.dumps(manifest).encode()); raw = source.read_bytes()
            pin = hashlib.sha256(raw).hexdigest()
            paths = [source] + [root / row['path'] for row in rows]
            snapshots = {p: p.read_bytes() for p in paths}
            for pinned in (False, True):
                reads = []; hashes = 0; active = False
                read, decode, sha = assembler.read_json, shared.decode_json, shared.hashlib.sha256
                def observed_read(path, *args): reads.append(path); return read(path, *args)
                def observed_decode(data, expected_pin=None):
                    nonlocal active
                    previous = active; active = data == raw and expected_pin is not None
                    try: return decode(data, expected_pin)
                    finally: active = previous
                def observed_sha(data=b'', *args, **kwargs):
                    nonlocal hashes
                    if active: hashes += 1
                    return sha(data, *args, **kwargs)
                assembler.read_json = observed_read
                shared.decode_json, shared.hashlib.sha256 = observed_decode, observed_sha
                tracemalloc.start(); started = time.perf_counter()
                try:
                    args = [str(source), '--out', str(output), '--require-source-pins']
                    if pinned: args += ['--expected-manifest-sha256', pin]
                    assert assembler.main(args) == 0
                finally:
                    elapsed = time.perf_counter() - started
                    _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
                    assembler.read_json = read; shared.decode_json, shared.hashlib.sha256 = decode, sha
                packet = json.loads(output.read_bytes())
                if expected is None: expected = packet
                assert packet == expected and reads == paths and hashes == int(pinned)
                assert all(p.read_bytes() == data for p, data in snapshots.items())
                observations.append({'padding_bytes': padding, 'manifest_bytes': len(raw), 'manifest_pinned': pinned,
                    'explicit_sources': 4, 'reads': len(reads), 'manifest_hash_calls': hashes,
                    'seconds': elapsed, 'peak_bytes': peak})
    print(json.dumps({'scope': 'fixed_sources_manifest_pin_volume', 'hash_scope': 'manifest decoding only',
                      'timing_gates': False, 'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
