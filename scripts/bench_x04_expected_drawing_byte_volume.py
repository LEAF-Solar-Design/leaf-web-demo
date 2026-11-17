"""Ignored artifact padding with fixed entities and optional drawing label guards."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import time
import tracemalloc
from bench_x04_packet_validation import artifact
import validate_x04_offline_evidence as checker
import export_x04_offline_evidence as shared


def main():
    base = artifact(); original = deepcopy(base); identity = base['drawing']['id']
    expected = checker.validate_evidence(base); observations = []
    with tempfile.TemporaryDirectory(prefix='x04-drawing-volume-') as directory:
        root = Path(directory); source = root / 'artifact.json'; output = root / 'checked.json'
        for padding in (0, 4096, 65536):
            value = {**deepcopy(base), 'ignored_padding': 'p' * padding}
            raw = json.dumps(value).encode(); source.write_bytes(raw)
            for mode in ('matching', 'wrong', 'omitted'):
                output.write_bytes(b'previous'); reads = bytes_read = calls = 0
                read, decode, export = checker.read_json, shared.decode_json, checker.export_evidence
                def observed_read(*args):
                    nonlocal reads
                    reads += 1; return read(*args)
                def observed_decode(data, pin=None):
                    nonlocal bytes_read
                    bytes_read += len(data); return decode(data, pin)
                def counted(payload):
                    nonlocal calls
                    calls += 1; return export(payload)
                checker.read_json, shared.decode_json, checker.export_evidence = observed_read, observed_decode, counted
                tracemalloc.start(); started = time.perf_counter()
                try:
                    args = [str(source), '--out', str(output)]
                    if mode != 'omitted': args += ['--expected-drawing-id', identity if mode == 'matching' else identity + '-wrong']
                    code = checker.main(args)
                finally:
                    elapsed = time.perf_counter() - started
                    _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop()
                    checker.read_json, shared.decode_json, checker.export_evidence = read, decode, export
                assert code == (2 if mode == 'wrong' else 0)
                assert reads == 1 and bytes_read == len(raw) and calls == (0 if mode == 'wrong' else 1)
                if mode == 'wrong': assert output.read_bytes() == b'previous'
                else:
                    result = json.loads(output.read_bytes()); assert result == expected
                    assert result['recomputed_result'] == {'count': 1, 'highlight_handles': ['A'],
                                                          'original_bytes_integrity': 'unverified'}
                assert source.read_bytes() == raw and not list(root.glob('.x04-*'))
                observations.append({'padding_bytes': padding, 'artifact_bytes': len(raw), 'mode': mode,
                    'reads': reads, 'bytes_read': bytes_read, 'recomputation_calls': calls,
                    'seconds': elapsed, 'peak_bytes': peak})
    assert base == original
    print(json.dumps({'scope': 'fixed_entities_expected_drawing_volume', 'timing_gates': False,
                      'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
