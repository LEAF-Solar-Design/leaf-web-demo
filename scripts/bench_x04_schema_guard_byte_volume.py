"""Ignored report padding with fixed claims and optional expected-kind guards."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import time
import tracemalloc
from bench_x04_report_pin_refusal_cost import batch
import validate_x04_review_report as checker
import export_x04_offline_evidence as shared


def main():
    comparison = {'schema': checker.COMPARISON, 'drawing_id': 'synthetic-report',
        'count': {'before': 6, 'after': 1, 'delta': -5}, 'original_bytes_integrity': 'unverified',
        'selected_ids': {'removed': ['old'], 'added': ['new']},
        'highlight_handles': {'removed': ['B'], 'added': []},
        'literal_dependencies': {'removed': [], 'added': []}}
    bases = {'comparison': comparison, 'batch': batch(2)}; original = deepcopy(bases); observations = []
    with tempfile.TemporaryDirectory(prefix='x04-report-volume-') as directory:
        root = Path(directory); source = root / 'report.json'; output = root / 'summary.json'
        for kind, base in bases.items():
            expected = checker.validate_report(base)
            for padding in (0, 4096, 65536):
                value = {**deepcopy(base), 'ignored_padding': 'p' * padding}
                raw = json.dumps(value).encode(); source.write_bytes(raw); digest = hashlib.sha256(raw).hexdigest()
                for mode in ('matching', 'wrong-kind', 'omitted'):
                    reads = bytes_read = hashes = checks = 0; active = False; output.write_bytes(b'previous')
                    read, decode, sha, validate = checker.read_json, shared.decode_json, shared.hashlib.sha256, checker.validate_report
                    def observed_read(*args):
                        nonlocal reads
                        reads += 1; return read(*args)
                    def observed_decode(data, pin=None):
                        nonlocal bytes_read, active
                        bytes_read += len(data); previous = active; active = pin is not None
                        try: return decode(data, pin)
                        finally: active = previous
                    def observed_sha(data=b'', *args, **kwargs):
                        nonlocal hashes
                        if active: hashes += 1
                        return sha(data, *args, **kwargs)
                    def checked(item):
                        nonlocal checks
                        checks += 1; return validate(item)
                    checker.read_json, checker.validate_report = observed_read, checked
                    shared.decode_json, shared.hashlib.sha256 = observed_decode, observed_sha
                    tracemalloc.start(); started = time.perf_counter()
                    try:
                        args = [str(source), '--out', str(output)]
                        args += ['--expected-report-sha256', digest]
                        if mode != 'omitted':
                            selected = kind if mode == 'matching' else ('batch' if kind == 'comparison' else 'comparison')
                            args += ['--expected-report-schema', selected]
                        code = checker.main(args)
                    finally:
                        elapsed = time.perf_counter() - started
                        _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
                        checker.read_json, checker.validate_report = read, validate
                        shared.decode_json, shared.hashlib.sha256 = decode, sha
                    assert code == (2 if mode == 'wrong-kind' else 0)
                    assert reads == 1 and bytes_read == len(raw) and hashes == 1
                    assert checks == (0 if mode == 'wrong-kind' else 1)
                    if mode == 'wrong-kind': assert output.read_bytes() == b'previous'
                    else: assert json.loads(output.read_bytes()) == expected
                    assert source.read_bytes() == raw and not list(root.glob('.x04-*'))
                    observations.append({'kind': kind, 'padding_bytes': padding, 'input_bytes': len(raw),
                        'mode': mode, 'reads': reads, 'bytes_read': bytes_read, 'hash_calls': hashes,
                        'report_checks': checks, 'seconds': elapsed, 'peak_bytes': peak})
    assert bases == original and comparison['count'] == {'before': 6, 'after': 1, 'delta': -5}
    assert all(row['recomputed_result']['count'] == 1 and row['recomputed_result']['highlight_handles'] == ['A'] for row in bases['batch']['results'])
    print(json.dumps({'scope': 'fixed_report_claims_schema_volume', 'hash_scope': 'report decoding only',
                      'timing_gates': False, 'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
