"""Observe report inventory refusal cost with a fixed synthetic artifact count."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import tracemalloc

from bench_x04_packet_validation import artifact
from compare_x04_offline_evidence import compare_evidence
import validate_x04_evidence_packet as checker
from validate_x04_review_report import BATCH


def main():
    embedded = artifact()
    trusted = checker.validate_evidence(embedded)
    comparison = compare_evidence(embedded, embedded)
    batch = {
        'schema': BATCH,
        'results': [{'index': 0, 'path': '../../unopened.json', **trusted}],
        'total': 1, 'passed': 1, 'failed': 0,
        'original_bytes_integrity': 'unverified',
    }
    observations = []
    with tempfile.TemporaryDirectory(prefix='x04-report-inventory-') as directory:
        root = Path(directory)
        source = root / 'packet.json'
        output = root / 'summary.json'
        for size in (1, 10, 100):
            comparison_ids = [f'c{i:03d}' for i in range(size)]
            batch_ids = [f'b{i:03d}' for i in range(size)]
            value = {
                'schema': checker.SCHEMA,
                'artifacts': [{'id': 'a', 'artifact': deepcopy(embedded)}],
                'comparisons': [{'id': identity, 'before': 'a', 'after': 'a',
                                 'report': deepcopy(comparison)} for identity in comparison_ids],
                'batches': [{'id': identity, 'artifact_ids': ['a'],
                             'report': deepcopy(batch)} for identity in batch_ids],
            }
            raw = json.dumps(value).encode()
            source.write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            for mode in ('matching', 'wrong-comparison', 'wrong-batch', 'omitted'):
                options = [str(source), '--out', str(output), '--expected-packet-sha256', digest]
                if mode != 'omitted':
                    expected_comparisons = ['wrong'] if mode == 'wrong-comparison' else comparison_ids
                    expected_batches = ['wrong'] if mode == 'wrong-batch' else batch_ids
                    for identity in expected_comparisons:
                        options += ['--expected-comparison-id', identity]
                    for identity in expected_batches:
                        options += ['--expected-batch-id', identity]
                output.write_bytes(b'prior report')
                calls = 0
                validate = checker.validate_evidence

                def counted(item):
                    nonlocal calls
                    calls += 1
                    return validate(item)

                checker.validate_evidence = counted
                tracemalloc.start()
                started = time.perf_counter()
                try:
                    code = checker.main(options)
                finally:
                    elapsed = time.perf_counter() - started
                    _, peak = tracemalloc.get_traced_memory()
                    tracemalloc.stop()
                    checker.validate_evidence = validate
                refuses = mode.startswith('wrong')
                assert code == (2 if refuses else 0)
                assert calls == (0 if refuses else 1)
                if refuses:
                    assert output.read_bytes() == b'prior report'
                else:
                    result = json.loads(output.read_bytes())
                    assert [row['id'] for row in result['comparisons']] == comparison_ids
                    assert [row['id'] for row in result['batches']] == batch_ids
                    assert result['validated_artifacts'][0]['recomputed_result']['count'] == 1
                    assert result['validated_artifacts'][0]['recomputed_result']['highlight_handles'] == ['A']
                    assert all(row['report']['count']['delta'] == 0 for row in result['comparisons'])
                    assert all(row['report']['passed'] == 1 for row in result['batches'])
                assert source.read_bytes() == raw
                assert not list(root.glob('.x04-*'))
                observations.append({'artifacts': 1, 'comparisons': size, 'batches': size,
                                     'packet_bytes': len(raw), 'mode': mode, 'trusted_calls': calls,
                                     'seconds': elapsed, 'peak_bytes': peak})
    print(json.dumps({'scope': 'expected_report_id_inventories', 'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
