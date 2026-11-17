"""Fixed-byte expected drawing identity observations as synthetic entities grow."""
import json
from pathlib import Path
import tempfile
import time
import tracemalloc
from bench_x04_packet_validation import artifact
from export_x04_offline_evidence import export_evidence
import validate_x04_offline_evidence as checker


def main():
    observations = []; target_bytes = 131072
    with tempfile.TemporaryDirectory(prefix='x04-drawing-cost-') as directory:
        root = Path(directory); source = root / 'artifact.json'; output = root / 'checked.json'
        for size in (1, 10, 100):
            payload = artifact()
            handles = [f'A{i:03d}' for i in range(size)]
            payload['drawing']['intake']['inserts'] = [
                {'layer': 'Modules', 'name': 'Panel', 'handle': handle} for handle in handles]
            value = export_evidence(payload); identity = value['drawing']['id']
            encoded = json.dumps(value).encode(); assert len(encoded) <= target_bytes
            raw = encoded + b' ' * (target_bytes - len(encoded)); source.write_bytes(raw)
            for mode in ('matching', 'wrong', 'omitted'):
                output.write_bytes(b'previous'); calls = 0; export = checker.export_evidence
                def counted(item):
                    nonlocal calls
                    calls += 1; return export(item)
                checker.export_evidence = counted
                tracemalloc.start(); started = time.perf_counter()
                try:
                    args = [str(source), '--out', str(output)]
                    if mode != 'omitted': args += ['--expected-drawing-id', identity if mode == 'matching' else identity + '-wrong']
                    code = checker.main(args)
                finally:
                    elapsed = time.perf_counter() - started
                    _, peak = tracemalloc.get_traced_memory(); tracemalloc.stop(); checker.export_evidence = export
                assert code == (2 if mode == 'wrong' else 0) and calls == (0 if mode == 'wrong' else 1)
                if mode == 'wrong': assert output.read_bytes() == b'previous'
                else:
                    result = json.loads(output.read_bytes())
                    assert result['drawing_id'] == identity
                    assert result['recomputed_result'] == {'count': size, 'highlight_handles': handles,
                                                          'original_bytes_integrity': 'unverified'}
                assert source.read_bytes() == raw and not list(root.glob('.x04-*'))
                observations.append({'entities': size, 'artifact_bytes': len(raw), 'mode': mode,
                    'recomputation_calls': calls, 'seconds': elapsed, 'peak_bytes': peak})
    print(json.dumps({'scope': 'fixed_byte_expected_drawing_identity', 'timing_gates': False,
                      'observations': observations}, indent=2))


if __name__ == '__main__':
    main()
