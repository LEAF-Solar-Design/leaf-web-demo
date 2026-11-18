"""Measure explicit repeated-source reads and trusted calls without timing gates."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import tracemalloc
from bench_x04_packet_validation import artifact
import assemble_x04_evidence_packet as assembler
import validate_x04_evidence_packet as checker


def main():
    observations = []
    embedded = artifact()
    with tempfile.TemporaryDirectory(prefix='x04-source-fanout-') as directory:
        root = Path(directory)
        path = root / 'synthetic-artifact.json'
        path.write_text(json.dumps(embedded), encoding='utf-8')
        original_bytes = path.read_bytes()
        for size in (1, 10, 100):
            manifest = {'schema': assembler.MANIFEST_SCHEMA,
                        'artifacts': [{'id': f'a{i}', 'path': path.name} for i in range(size)],
                        'comparisons': [], 'batches': []}
            snapshot = deepcopy(manifest)
            reads = calls = 0
            read, validate = assembler.read_json, checker.validate_evidence
            captured = []
            def counted_read(*args):
                nonlocal reads
                reads += 1
                return read(*args)
            def counted_validate(item):
                nonlocal calls
                calls += 1
                result = validate(item); captured.append(result)
                return result
            assembler.read_json, checker.validate_evidence = counted_read, counted_validate
            tracemalloc.start(); started = time.perf_counter()
            try:
                packet = assembler.assemble_manifest(manifest, root)
            finally:
                elapsed = time.perf_counter() - started
                _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
                assembler.read_json, checker.validate_evidence = read, validate
            assert reads == calls == size
            assert manifest == snapshot and path.read_bytes() == original_bytes
            assert len(packet['artifacts']) == len(captured) == size
            assert [row['id'] for row in packet['artifacts']] == [f'a{i}' for i in range(size)]
            assert all(row['artifact'] == embedded for row in packet['artifacts'])
            assert all(row['recomputed_result'] == {'count': 1, 'highlight_handles': ['A'],
                       'original_bytes_integrity': 'unverified'} for row in captured)
            output_bytes = len((json.dumps(packet, ensure_ascii=True, indent=2) + '\n').encode('utf-8'))
            packet['artifacts'][0]['artifact']['drawing']['intake']['inserts'][0]['handle'] = 'MUTATED'
            captured[0]['recomputed_result']['highlight_handles'].append('MUTATED')
            assert all(row['artifact'] == embedded for row in packet['artifacts'][1:])
            assert all(row['recomputed_result']['highlight_handles'] == ['A'] for row in captured[1:])
            assert path.read_bytes() == original_bytes and manifest == snapshot
            observations.append({'explicit_entries': size, 'source_reads': reads, 'trusted_calls': calls,
                                 'seconds': elapsed, 'peak_bytes': peak, 'output_bytes': output_bytes})
    print(json.dumps({'scope': 'explicit_manifest_assembly', 'timing_gates': False,
                      'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
