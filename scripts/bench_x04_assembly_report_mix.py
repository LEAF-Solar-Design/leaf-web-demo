"""Hold explicit entry count and actual aggregate input bytes fixed across report mixes."""
import json
from pathlib import Path
import tempfile
import time
import tracemalloc
from bench_x04_packet_validation import artifact
from compare_x04_offline_evidence import compare_evidence
import assemble_x04_evidence_packet as assembler
import validate_x04_evidence_packet as checker
from validate_x04_review_report import BATCH


def main():
    observations = []; embedded = artifact(); target_bytes = 65536
    checked = checker.validate_evidence(embedded)
    comparison = compare_evidence(embedded, embedded)
    batch = {'schema': BATCH, 'total': 3, 'passed': 3, 'failed': 0,
             'original_bytes_integrity': 'unverified', 'results': [
                 {'index': i, 'path': '../../unopened.json', **checked} for i in range(3)]}
    with tempfile.TemporaryDirectory(prefix='x04-report-mix-') as directory:
        root = Path(directory)
        for artifacts, comparisons, batches in ((8, 2, 2), (2, 8, 2), (2, 2, 8)):
            manifest = {'schema': assembler.MANIFEST_SCHEMA, 'artifacts': [], 'comparisons': [], 'batches': []}
            files = []
            for size, section, prefix, value in ((artifacts, 'artifacts', 'a', embedded),
                    (comparisons, 'comparisons', 'c', comparison), (batches, 'batches', 'b', batch)):
                for i in range(size):
                    entry = {'id': f'{prefix}{i}', 'path': f'{prefix}{i}.json'}
                    if section == 'comparisons': entry.update(before='a0', after='a1')
                    if section == 'batches': entry['artifact_ids'] = ['a0', 'a1', 'a0']
                    manifest[section].append(entry)
                    path = root / entry['path']; path.write_bytes(json.dumps(value).encode()); files.append(path)
            source = root / 'manifest.json'; source.write_bytes(json.dumps(manifest).encode())
            remaining = target_bytes - len(source.read_bytes()) - sum(p.stat().st_size for p in files)
            assert remaining >= 0
            for i, path in enumerate(files):
                path.write_bytes(path.read_bytes() + b' ' * (remaining // len(files) + int(i < remaining % len(files))))
            paths = [source] + files; snapshots = {p: p.read_bytes() for p in paths}
            assert len(files) == 12 and sum(map(len, snapshots.values())) == target_bytes
            reads = []; calls = 0; read, validate = assembler.read_json, checker.validate_evidence
            def counted_read(path, *args): reads.append(path); return read(path, *args)
            def counted_validate(value):
                nonlocal calls
                calls += 1; return validate(value)
            checker.validate_evidence = counted_validate
            tracemalloc.start(); started = time.perf_counter()
            try:
                parsed = counted_read(source, assembler.DEFAULT_MAX_INPUT_BYTES)
                packet = assembler.assemble_manifest(parsed, root, read_input=counted_read)
            finally:
                elapsed = time.perf_counter() - started
                _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
                checker.validate_evidence = validate
            assert reads == paths and calls == artifacts and parsed == manifest
            result = checker.validate_packet(packet)
            assert all(r['recomputed_result'] == {'count': 1, 'highlight_handles': ['A'],
                'original_bytes_integrity': 'unverified'} for r in result['validated_artifacts'])
            assert all(r['report']['count'] == {'before': 1, 'after': 1, 'delta': 0} for r in result['comparisons'])
            assert all([row['recomputed_result']['count'] for row in r['report']['results']] == [1, 1, 1] for r in result['batches'])
            assert all(p.read_bytes() == data for p, data in snapshots.items())
            observations.append({'artifacts': artifacts, 'comparisons': comparisons, 'batches': batches,
                'explicit_entries': len(files), 'aggregate_bytes': target_bytes, 'reads': len(reads),
                'trusted_calls': calls, 'seconds': elapsed, 'peak_bytes': peak,
                'output_bytes': len((json.dumps(packet, ensure_ascii=True, indent=2) + '\n').encode())})
    print(json.dumps({'scope': 'fixed_entry_equal_input_report_mix', 'timing_gates': False,
                      'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
