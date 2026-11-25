"""Vary pinned occurrences at fixed entries/input bytes without timing gates."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import tracemalloc
from bench_x04_packet_validation import artifact
from compare_x04_offline_evidence import compare_evidence
from validate_x04_review_report import BATCH
import export_x04_offline_evidence as shared
import assemble_x04_evidence_packet as assembler
import validate_x04_evidence_packet as checker


def main():
    observations = []; embedded = artifact(); checked = checker.validate_evidence(embedded)
    report = compare_evidence(embedded, embedded)
    batch = {'schema': BATCH, 'total': 2, 'passed': 2, 'failed': 0, 'original_bytes_integrity': 'unverified',
             'results': [{'index': i, 'path': '../../unopened.json', **checked} for i in range(2)]}
    with tempfile.TemporaryDirectory(prefix='x04-pin-density-') as directory:
        root = Path(directory); base = {'schema': assembler.MANIFEST_SCHEMA, 'artifacts': [], 'comparisons': [], 'batches': []}
        for size, key, prefix, value in ((8, 'artifacts', 'a', embedded), (2, 'comparisons', 'c', report), (2, 'batches', 'b', batch)):
            for i in range(size):
                row = {'id': f'{prefix}{i}', 'path': f'{prefix}{i}.json'}
                if key == 'comparisons': row.update(before='a0', after='a1')
                if key == 'batches': row['artifact_ids'] = ['a0', 'a1']
                base[key].append(row); (root / row['path']).write_bytes(json.dumps(value).encode())
        entries = [row for key in ('artifacts', 'comparisons', 'batches') for row in base[key]]
        source = root / 'manifest.json'; expected = assembler.assemble_manifest(base, root)
        for pinned in (0, 6, 12):
            manifest = deepcopy(base)
            rows = [row for key in ('artifacts', 'comparisons', 'batches') for row in manifest[key]]
            for row in rows[:pinned]: row['expected_sha256'] = hashlib.sha256((root / row['path']).read_bytes()).hexdigest()
            raw = json.dumps(manifest).encode()
            remainder = 65536 - len(raw) - sum((root / row['path']).stat().st_size for row in rows)
            assert remainder >= 0; source.write_bytes(raw + b' ' * remainder)
            paths = [source] + [root / row['path'] for row in rows]
            snapshots = {p: p.read_bytes() for p in paths}
            assert len(rows) == 12 and sum(map(len, snapshots.values())) == 65536
            reads = []; hashes = 0; active = False
            read, decode, sha = assembler.read_json, shared.decode_json, shared.hashlib.sha256
            def counted_read(path, *args): reads.append(path); return read(path, *args)
            def counted_decode(encoded, pin=None):
                nonlocal active
                previous = active; active = pin is not None
                try: return decode(encoded, pin)
                finally: active = previous
            def counted_sha(data=b'', *args, **kwargs):
                nonlocal hashes
                if active: hashes += 1
                return sha(data, *args, **kwargs)
            shared.decode_json, shared.hashlib.sha256 = counted_decode, counted_sha
            tracemalloc.start(); started = time.perf_counter()
            try:
                parsed = counted_read(source, assembler.DEFAULT_MAX_INPUT_BYTES)
                packet = assembler.assemble_manifest(parsed, root, read_input=counted_read)
            finally:
                elapsed = time.perf_counter() - started
                _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
                shared.decode_json, shared.hashlib.sha256 = decode, sha
            assert reads == paths and hashes == pinned and packet == expected and parsed == manifest
            assert all(p.read_bytes() == data for p, data in snapshots.items())
            assert checker.validate_packet(packet)['valid']
            observations.append({'explicit_entries': 12, 'pinned_sources': pinned, 'aggregate_bytes': 65536,
                'reads': len(reads), 'hash_calls': hashes, 'seconds': elapsed, 'peak_bytes': peak})
    print(json.dumps({'scope': 'equal_input_pin_density', 'hash_scope': 'sha256 calls during pinned source decoding',
                      'timing_gates': False, 'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
