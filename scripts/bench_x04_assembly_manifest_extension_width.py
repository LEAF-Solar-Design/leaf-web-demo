"""Ignored manifest extension width with equal-byte flat/nested controls."""
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
    baseline = {'schema': assembler.MANIFEST_SCHEMA, 'artifacts': [{'id': 'a', 'path': 'artifact.json'}],
                'comparisons': [], 'batches': []}
    expected = {'schema': checker.SCHEMA, 'artifacts': [{'id': 'a', 'artifact': embedded}], 'comparisons': [], 'batches': []}
    with tempfile.TemporaryDirectory(prefix='x04-extension-width-') as directory:
        root = Path(directory); source = root / 'manifest.json'; target = root / 'artifact.json'
        target.write_bytes(json.dumps(embedded).encode()); original_bytes = target.read_bytes()
        for size in (1, 100, 1000):
            variants = {kind: {**deepcopy(baseline), 'ignored': {f'k{i:04d}': (i if kind == 'flat' else {'v': i}) for i in range(size)}}
                        for kind in ('flat', 'nested')}
            encoded = {kind: json.dumps(value).encode() for kind, value in variants.items()}
            length = max(map(len, encoded.values())); cohort_bytes = []
            for kind, value in variants.items():
                source.write_bytes(encoded[kind] + b' ' * (length - len(encoded[kind])))
                before = source.read_bytes(); cohort_bytes.append(len(before)); snapshot = deepcopy(value)
                reads = calls = 0; read, validate = assembler.read_json, checker.validate_evidence
                def counted_read(path, limit):
                    nonlocal reads
                    reads += 1; return read(path, limit)
                def counted_validate(item):
                    nonlocal calls
                    calls += 1; return validate(item)
                checker.validate_evidence = counted_validate
                tracemalloc.start(); started = time.perf_counter()
                try:
                    parsed = counted_read(source, assembler.DEFAULT_MAX_INPUT_BYTES)
                    packet = assembler.assemble_manifest(parsed, root, read_input=counted_read)
                finally:
                    elapsed = time.perf_counter() - started
                    _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
                    checker.validate_evidence = validate
                assert reads == 2 and calls == 1 and packet == expected and parsed == snapshot
                assert source.read_bytes() == before and target.read_bytes() == original_bytes
                bad = deepcopy(baseline); nested = 0
                for _ in range(256): nested = [nested]
                bad['ignored'] = nested
                def forbidden(*args): raise AssertionError('Deep ignored manifest must refuse before payload read')
                try: assembler.assemble_manifest(bad, root, read_input=forbidden)
                except ValueError as error: assert 'nesting' in str(error)
                else: raise AssertionError('Deep ignored manifest accepted')
                assert assembler.assemble_manifest(snapshot, root) == expected
                observations.append({'extension_fields': size, 'kind': kind, 'manifest_bytes': len(before),
                    'reads': reads, 'trusted_calls': calls, 'seconds': elapsed, 'peak_bytes': peak})
            assert cohort_bytes[0] == cohort_bytes[1]
    print(json.dumps({'scope': 'ignored_manifest_extension_width', 'timing_gates': False,
                      'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
