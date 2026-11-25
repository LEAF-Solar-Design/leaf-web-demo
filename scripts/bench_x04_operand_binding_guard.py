"""Comparison operand guard observations at fixed embedded artifact count."""
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


def main():
    embedded = artifact(); report = compare_evidence(embedded, embedded); observations = []
    with tempfile.TemporaryDirectory(prefix='x04-operands-') as directory:
        root = Path(directory); source = root / 'packet.json'; output = root / 'summary.json'
        for size in (2, 10, 100):
            ids = [f'c{i:03d}' for i in range(size)]
            value = {'schema': checker.SCHEMA, 'artifacts': [{'id': identity, 'artifact': deepcopy(embedded)} for identity in ('a', 'b')],
                     'comparisons': [{'id': identity, 'before': 'a', 'after': 'b', 'report': deepcopy(report)} for identity in ids], 'batches': []}
            encoded = json.dumps(value).encode(); assert len(encoded) <= 131072
            raw = encoded + b' ' * (131072 - len(encoded)); source.write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest()
            for mode in ('matching', 'wrong', 'omitted'):
                bindings = [(identity, 'a,b') for identity in ids] if mode == 'matching' else [(ids[0], 'b,a')] if mode == 'wrong' else []
                output.write_bytes(b'previous'); calls = 0; validate = checker.validate_evidence
                def counted(item):
                    nonlocal calls
                    calls += 1; return validate(item)
                checker.validate_evidence = counted; tracemalloc.start(); started = time.perf_counter()
                try:
                    args = [str(source), '--out', str(output), '--expected-packet-sha256', digest]
                    for identity, operands in bindings: args += ['--expected-comparison-operands', identity + '=' + operands]
                    code = checker.main(args)
                finally:
                    elapsed = time.perf_counter() - started
                    _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop(); checker.validate_evidence = validate
                refuses = mode == 'wrong'
                assert code == (2 if refuses else 0) and calls == (0 if refuses else 2)
                if refuses: assert output.read_bytes() == b'previous'
                else:
                    result = json.loads(output.read_bytes())
                    assert [row['id'] for row in result['comparisons']] == ids
                    assert all(row['before'] == 'a' and row['after'] == 'b' and row['report']['count'] == {'before': 1, 'after': 1, 'delta': 0} for row in result['comparisons'])
                    assert result['original_bytes_integrity'] == 'unverified'
                assert source.read_bytes() == raw and not list(root.glob('.x04-*'))
                observations.append({'artifacts': 2, 'comparisons': size, 'packet_bytes': len(raw), 'mode': mode,
                    'trusted_calls': calls, 'seconds': elapsed, 'peak_bytes': peak})
    print(json.dumps({'scope': 'expected_comparison_operand_bindings', 'timing_gates': False, 'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
