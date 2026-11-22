"""Oversized source positions with fixed input bytes and no timing gate."""
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
    raw = json.dumps(artifact()).encode(); per_file = 8192
    with tempfile.TemporaryDirectory(prefix='x04-refusal-positions-') as directory:
        root = Path(directory)
        for size in (10, 100):
            totals = []
            for position in (0, size // 2, size - 1):
                manifest = {'schema': assembler.MANIFEST_SCHEMA,
                    'artifacts': [{'id': f'a{i}', 'path': f'source-{i}.json'} for i in range(size)],
                    'comparisons': [], 'batches': []}
                source = root / 'manifest.json'; source.write_bytes(json.dumps(manifest).encode())
                for i in range(size):
                    (root / f'source-{i}.json').write_bytes(raw + (b' ' * (per_file + 1 - len(raw)) if i == position else b''))
                paths = [source] + [root / row['path'] for row in manifest['artifacts']]
                snapshots = {p: p.read_bytes() for p in paths}
                total = sum(len(data) for data in snapshots.values()); totals.append(total)
                output = root / 'packet.json'; output.write_bytes(b'previous')
                opens = []; consumed = 0; calls = 0
                original_open, original_validate = Path.open, checker.validate_evidence
                class Observed:
                    def __init__(self, stream): self.stream = stream
                    def __enter__(self): self.stream.__enter__(); return self
                    def __exit__(self, *args): return self.stream.__exit__(*args)
                    def read(self, amount=-1):
                        nonlocal consumed
                        data = self.stream.read(amount); consumed += len(data); return data
                def opened(path, mode='r', *args, **kwargs):
                    stream = original_open(path, mode, *args, **kwargs)
                    if mode == 'rb' and path in paths:
                        opens.append(path); return Observed(stream)
                    return stream
                def counted(value):
                    nonlocal calls
                    calls += 1
                    return original_validate(value)
                Path.open, checker.validate_evidence = opened, counted
                tracemalloc.start(); started = time.perf_counter()
                try:
                    try:
                        budget = assembler.InputBudget(total)
                        parsed = budget.read_json(source, per_file)
                        assembler.assemble_manifest(parsed, root, output, per_file, read_input=budget.read_json)
                    except ValueError as error:
                        assert 'byte' in str(error)
                    else: raise AssertionError('Oversized source accepted')
                finally:
                    elapsed = time.perf_counter() - started
                    _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
                    Path.open, checker.validate_evidence = original_open, original_validate
                assert opens == paths[:position + 2] and calls == 0
                assert consumed == len(snapshots[source]) + position * len(raw) + per_file + 1
                assert output.read_bytes() == b'previous' and not list(root.glob('.x04-*'))
                assert all(p.read_bytes() == data for p, data in snapshots.items())
                control = assembler.assemble_manifest(manifest, root, max_input_bytes=per_file + 1)
                assert all(row['artifact']['result']['count'] == 1 for row in control['artifacts'])
                assert checker.validate_packet(control)['valid']
                assert all(p.read_bytes() == data for p, data in snapshots.items())
                observations.append({'entries': size, 'oversized_index': position, 'aggregate_bytes': total,
                    'opens': len(opens), 'bytes_read': consumed, 'trusted_calls': calls,
                    'seconds': elapsed, 'peak_bytes': peak})
            assert len(set(totals)) == 1
    print(json.dumps({'scope': 'oversized_source_early_stop', 'timing_gates': False,
                      'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
