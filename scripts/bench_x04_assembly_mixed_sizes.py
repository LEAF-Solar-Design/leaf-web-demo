"""Equal aggregate byte cohorts with different explicit source counts."""
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
    with tempfile.TemporaryDirectory(prefix='x04-mixed-inputs-') as directory:
        root = Path(directory); embedded = artifact()
        raw = json.dumps(embedded).encode('utf-8')
        for target in (32768, 65536):
            totals = []
            for size in (2, 10):
                manifest = {'schema': assembler.MANIFEST_SCHEMA,
                    'artifacts': [{'id': f'a{i}', 'path': f'source-{i}.json'} for i in range(size)],
                    'comparisons': [], 'batches': []}
                source = root / 'manifest.json'
                source.write_bytes(json.dumps(manifest).encode())
                available = target - source.stat().st_size
                for i in range(size):
                    length = available // size + int(i < available % size)
                    assert length >= len(raw)
                    (root / f'source-{i}.json').write_bytes(raw + b' ' * (length - len(raw)))
                paths = [source] + [root / row['path'] for row in manifest['artifacts']]
                snapshots = {p: p.read_bytes() for p in paths}; before = deepcopy(manifest)
                total = sum(len(data) for data in snapshots.values()); totals.append(total)
                assert total == target
                reads = calls = 0; captured = []
                read, validate = assembler.read_json, checker.validate_evidence
                def counted_read(path, limit):
                    nonlocal reads
                    reads += 1
                    return read(path, limit)
                def counted_validate(value):
                    nonlocal calls
                    calls += 1
                    result = validate(value); captured.append(result); return result
                checker.validate_evidence = counted_validate
                tracemalloc.start(); started = time.perf_counter()
                try:
                    parsed = counted_read(source, assembler.DEFAULT_MAX_INPUT_BYTES)
                    packet = assembler.assemble_manifest(parsed, root, read_input=counted_read)
                finally:
                    elapsed = time.perf_counter() - started
                    _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
                    checker.validate_evidence = validate
                assert reads == size + 1 and calls == size and parsed == before
                assert all(row['artifact'] == embedded for row in packet['artifacts'])
                assert all(row['recomputed_result'] == {'count': 1, 'highlight_handles': ['A'],
                    'original_bytes_integrity': 'unverified'} for row in captured)
                assert all(p.read_bytes() == data for p, data in snapshots.items())
                observations.append({'entries': size, 'aggregate_bytes': total, 'reads': reads,
                    'trusted_calls': calls, 'seconds': elapsed, 'peak_bytes': peak,
                    'output_bytes': len((json.dumps(packet, ensure_ascii=True, indent=2) + '\n').encode())})
            assert totals[0] == totals[1]
    print(json.dumps({'scope': 'equal_aggregate_input_bytes', 'timing_gates': False,
                      'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
