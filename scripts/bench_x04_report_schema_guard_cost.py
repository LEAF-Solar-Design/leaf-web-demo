"""Fixed-byte report-kind guard observations as synthetic batch row count grows."""
import hashlib
import json
from pathlib import Path
import tempfile
import time
import tracemalloc
from bench_x04_report_pin_refusal_cost import batch
import validate_x04_review_report as checker


def main():
    observations = []; target_bytes = 131072
    with tempfile.TemporaryDirectory(prefix='x04-schema-cost-') as directory:
        root = Path(directory); source = root / 'report.json'; output = root / 'summary.json'
        for size in (1, 10, 100):
            value = batch(size); encoded = json.dumps(value).encode(); assert len(encoded) <= target_bytes
            raw = encoded + b' ' * (target_bytes - len(encoded)); source.write_bytes(raw)
            digest = hashlib.sha256(raw).hexdigest(); expected = checker.validate_report(value)
            for mode in ('matching', 'wrong-kind', 'omitted'):
                output.write_bytes(b'previous'); calls = rows = 0
                validate, membership = checker.validate_report, checker.membership
                def checked(item):
                    nonlocal calls
                    calls += 1; return validate(item)
                def checked_membership(items, label, dependencies=False):
                    nonlocal rows
                    if label == 'recomputed_result.highlight_handles': rows += 1
                    return membership(items, label, dependencies)
                checker.validate_report, checker.membership = checked, checked_membership
                tracemalloc.start(); started = time.perf_counter()
                try:
                    args = [str(source), '--out', str(output), '--expected-report-sha256', digest]
                    if mode != 'omitted': args += ['--expected-report-schema', 'batch' if mode == 'matching' else 'comparison']
                    code = checker.main(args)
                finally:
                    elapsed = time.perf_counter() - started
                    _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
                    checker.validate_report, checker.membership = validate, membership
                assert code == (2 if mode == 'wrong-kind' else 0)
                assert calls == (0 if mode == 'wrong-kind' else 1) and rows == (0 if mode == 'wrong-kind' else size)
                if mode == 'wrong-kind': assert output.read_bytes() == b'previous'
                else: assert json.loads(output.read_bytes()) == expected
                assert all(row['recomputed_result']['count'] == 1 and row['recomputed_result']['highlight_handles'] == ['A'] for row in value['results'])
                assert source.read_bytes() == raw and not list(root.glob('.x04-*'))
                observations.append({'batch_rows': size, 'report_bytes': len(raw), 'mode': mode,
                    'semantic_calls': calls, 'row_checks': rows, 'seconds': elapsed, 'peak_bytes': peak})
    print(json.dumps({'scope': 'fixed_byte_expected_report_kind', 'all_inputs_have_matching_byte_pins': True,
                      'timing_gates': False, 'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
