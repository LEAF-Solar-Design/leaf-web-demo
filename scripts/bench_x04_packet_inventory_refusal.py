"""Expected packet inventory observations with growing synthetic artifact counts."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import tracemalloc
from bench_x04_packet_validation import artifact
import validate_x04_evidence_packet as checker


def main():
    embedded = artifact(); observations = []; target_bytes = 131072
    with tempfile.TemporaryDirectory(prefix='x04-inventory-') as directory:
        root = Path(directory); source = root / 'packet.json'; output = root / 'summary.json'
        for size in (2, 10, 100):
            ids = [f'a{i:03d}' for i in range(size)]
            value = {'schema': checker.SCHEMA, 'artifacts': [{'id': identity, 'artifact': deepcopy(embedded)} for identity in ids],
                     'comparisons': [], 'batches': []}
            encoded = json.dumps(value).encode(); assert len(encoded) <= target_bytes
            raw = encoded + b' ' * (target_bytes - len(encoded)); source.write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            for mode in ('matching', 'missing', 'extra', 'omitted'):
                expected = ids if mode == 'matching' else ids[:-1] if mode == 'missing' else ids + ['extra'] if mode == 'extra' else None
                output.write_bytes(b'previous'); calls = 0; validate = checker.validate_evidence
                def counted(item):
                    nonlocal calls
                    calls += 1; return validate(item)
                checker.validate_evidence = counted; tracemalloc.start(); started = time.perf_counter()
                try:
                    args = [str(source), '--out', str(output), '--expected-packet-sha256', digest]
                    for identity in expected or []: args += ['--expected-artifact-id', identity]
                    code = checker.main(args)
                finally:
                    elapsed = time.perf_counter() - started
                    _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop(); checker.validate_evidence = validate
                refuses = mode in ('missing', 'extra')
                assert code == (2 if refuses else 0) and calls == (0 if refuses else size)
                if refuses: assert output.read_bytes() == b'previous'
                else:
                    result = json.loads(output.read_bytes())
                    assert [row['id'] for row in result['validated_artifacts']] == ids
                    assert all(row['recomputed_result'] == {'count': 1, 'highlight_handles': ['A'],
                        'original_bytes_integrity': 'unverified'} for row in result['validated_artifacts'])
                    assert result['original_bytes_integrity'] == 'unverified'
                assert source.read_bytes() == raw and not list(root.glob('.x04-*'))
                observations.append({'artifacts': size, 'packet_bytes': len(raw), 'mode': mode,
                    'trusted_calls': calls, 'seconds': elapsed, 'peak_bytes': peak})
    print(json.dumps({'scope': 'expected_packet_artifact_inventory', 'matching_packet_pins': True,
                      'timing_gates': False, 'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
