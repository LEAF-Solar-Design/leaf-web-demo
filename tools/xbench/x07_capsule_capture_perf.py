"""Bounded disposable digest-verification and diagnostic capture timings."""
import json
from pathlib import Path
import tempfile
import time
import tracemalloc
from x07_jsx_failure_capsule import MAX_STREAM_BYTES, run_check, snapshot, exit_code
from x07_profile_version_diff import JsonArgumentParser, publish_stdout


def benchmark(directory, *, smoke=False):
    sizes = [0, 1024, MAX_STREAM_BYTES] if smoke else [0, 1024, 65536, MAX_STREAM_BYTES]
    own_trace = not tracemalloc.is_tracing()
    if own_trace:
        tracemalloc.start()
    cases = []
    try:
        with tempfile.TemporaryDirectory(dir=directory, prefix='x07-capsule-perf-') as scratch:
            root = Path(scratch)
            marker = root / 'launches'
            for index, size in enumerate(sizes):
                check = root / ('x07_perf_' + str(size) + '.cjs')
                check.write_text("require('node:fs').appendFileSync(" + json.dumps(str(marker)) + ",'ONE\\n');" +
                                 'process.stdout.write(Buffer.alloc(' + str(size) + ",83));process.stderr.write(Buffer.alloc(" + str(size) + ',69));', encoding='utf-8')
                if own_trace:
                    tracemalloc.reset_peak()
                started = time.perf_counter()
                source = snapshot(check, root, True, [Path(__file__)])
                digest_seconds = time.perf_counter() - started
                started = time.perf_counter()
                report = run_check(check.name, root / (str(index) + '.json'), checks_root=root, source_dependencies=[Path(__file__)], timeout=10)
                capture_seconds = time.perf_counter() - started
                current, peak = tracemalloc.get_traced_memory()
                if (exit_code(report) or report['stdout'] != 'S' * size or report['stderr'] != 'E' * size
                        or report['stdout_bytes'] != size or report['stderr_bytes'] != size
                        or report['source_file_sha256'] != source['source_file_sha256']
                        or marker.read_text().splitlines() != ['ONE'] * (index + 1)):
                    raise ValueError('independent byte/source/launch oracle mismatch')
                cases.append({'bytes_per_stream': size, 'digest_verification_seconds': round(digest_seconds, 6),
                              'capture_seconds_including_revalidation': round(capture_seconds, 6), 'memory_current_bytes': current,
                              'memory_peak_bytes': peak, 'complete': True, 'owned_processes_settled': report['owned_processes_settled']})
            return {'schema': 'leaf.x07.capsule-capture-perf.v1', 'scope': 'synthetic offline; process Python traced allocations', 'cases': cases}
    finally:
        if own_trace:
            tracemalloc.stop()


def main():
    parser = JsonArgumentParser(result_schema='leaf.x07.capsule-capture-perf.v1', description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--smoke', action='store_true')
    args = parser.parse_args()
    try:
        report = benchmark(args.directory, smoke=args.smoke)
    except (OSError, ValueError):
        publish_stdout({'schema': 'leaf.x07.capsule-capture-perf.v1', 'error': 'MEASUREMENT_UNAVAILABLE'})
        return 2
    return publish_stdout(report)


if __name__ == '__main__':
    raise SystemExit(main())
