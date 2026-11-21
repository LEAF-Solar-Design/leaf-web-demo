"""Measure strict X07 capsule consumption with bounded disposable fixtures."""
import copy
import json
from pathlib import Path
import tempfile
import time
import tracemalloc
import sys
if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
from x07_jsx_failure_capsule import MAX_STREAM_BYTES, read_capture, run_check
from x07_profile_version_diff import JsonArgumentParser, publish_stdout


def benchmark(directory, *, smoke=False):
    sizes = [0, 1024, 65536] if smoke else [0, 1024, 65536, MAX_STREAM_BYTES]
    own_trace = not tracemalloc.is_tracing()
    if own_trace:
        tracemalloc.start()
    accepted = refused = 0
    cases = []
    try:
        with tempfile.TemporaryDirectory(dir=directory, prefix='x07-consumer-perf-') as scratch:
            root = Path(scratch)
            for size in sizes:
                check = root / 'x07_perf.cjs'
                check.write_bytes(('process.stdout.write("S".repeat(' + str(size) + '))').encode('ascii'))
                path = root / (str(size) + '.json')
                value = run_check(check.name, path, checks_root=root, timeout=10)
                original = path.read_bytes()
                if own_trace:
                    tracemalloc.reset_peak()
                started = time.perf_counter()
                consumed = read_capture(path)
                elapsed = time.perf_counter() - started
                current, peak = tracemalloc.get_traced_memory()
                if consumed['stdout'] != 'S' * size or consumed['stdout_bytes'] != size:
                    raise ValueError('independent accepted-byte oracle failed')
                accepted += 1
                changed = copy.deepcopy(value)
                changed['stdout_sha256'] = '0' * 64
                bad = root / (str(size) + '-invalid.json')
                invalid = (json.dumps(changed) + '\n').encode('utf-8')
                bad.write_bytes(invalid)
                try:
                    read_capture(bad)
                except ValueError:
                    refused += 1
                else:
                    raise ValueError('independent refusal oracle failed')
                if path.read_bytes() != original or bad.read_bytes() != invalid:
                    raise ValueError('capture bytes changed')
                cases.append({'stream_bytes': size, 'capture_bytes': len(original),
                              'elapsed_seconds': round(elapsed, 6), 'memory_current_bytes': current,
                              'memory_peak_bytes': peak})
        if accepted != len(sizes) or refused != len(sizes):
            raise ValueError('independent case-count oracle failed')
        return {'schema': 'leaf.x07.capsule-consumer-perf.v1', 'scope': 'synthetic offline Python traced allocations',
                'accepted': accepted, 'refused': refused, 'cases': cases}
    finally:
        if own_trace:
            tracemalloc.stop()


def main():
    parser = JsonArgumentParser(result_schema='leaf.x07.capsule-consumer-perf.v1', description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--smoke', action='store_true')
    args = parser.parse_args()
    try:
        result = benchmark(args.directory, smoke=args.smoke)
    except (OSError, ValueError):
        publish_stdout({'schema': 'leaf.x07.capsule-consumer-perf.v1', 'error': 'MEASUREMENT_UNAVAILABLE'})
        return 2
    return publish_stdout(result)


if __name__ == '__main__':
    raise SystemExit(main())
