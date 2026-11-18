"""Measure owned cleanup after disposable synthetic X07 captures."""
import ctypes
import gc
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import tracemalloc
from x07_audit_resource_soak import handles
from x07_jsx_failure_capsule import run_check, exit_code
from x07_profile_version_diff import JsonArgumentParser, publish_stdout


def worker_running(pid):
    if sys.platform == 'win32':
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 0x102
        finally:
            kernel.CloseHandle(handle)
    path = Path('/proc') / str(pid) / 'stat'
    if path.exists() and path.read_text().split(') ', 1)[1].startswith('Z'):
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def measure(directory, *, iterations=30):
    if type(iterations) is not int or not 1 <= iterations <= 100:
        raise ValueError('iteration budget')
    own_trace = not tracemalloc.is_tracing()
    if own_trace:
        tracemalloc.start()
    cases = []
    try:
        with tempfile.TemporaryDirectory(dir=directory, prefix='x07-capsule-soak-') as scratch:
            root = Path(scratch)
            marker = root / 'launches'
            expected = []
            for mode, body in [('success', "process.stdout.write('S')"), ('failure', "throw new Error('SYNTHETIC_FAILURE')"), ('timeout', 'setInterval(()=>{},1000)')]:
                check = root / ('x07_soak_' + mode + '.cjs')
                check.write_text("require('node:fs').appendFileSync(" + json.dumps(str(marker)) + ',' + json.dumps(mode + '\n') + ');' + body, encoding='utf-8')
            start_handles = handles()
            for index in range(iterations):
                for mode in ('success', 'failure', 'timeout'):
                    before_threads = set(threading.enumerate())
                    report = run_check('x07_soak_' + mode + '.cjs', root / (str(index) + '-' + mode + '.json'), checks_root=root,
                                       source_dependencies=[Path(__file__)], timeout=1 if mode == 'timeout' else 5)
                    expected.append(mode)
                    if marker.read_text(encoding='utf-8').splitlines() != expected:
                        raise ValueError('independent launch marker mismatch')
                    if exit_code(report) != int(mode != 'success') or report['timed_out'] != (mode == 'timeout'):
                        raise ValueError('capture outcome mismatch')
                    if not report['source_unchanged'] or not report['diagnostics_complete']:
                        raise ValueError('incomplete evidence')
                    resources = report['resources']
                    if (resources['readers_alive'] or not resources['parent_reaped'] or not resources['job_closed']
                            or worker_running(report['worker_pid']) or set(threading.enumerate()) - before_threads):
                        raise ValueError('owned resource survived cleanup')
                    gc.collect()
                    current, peak = tracemalloc.get_traced_memory()
                    cases.append({'iteration': index + 1, 'mode': mode, 'readers_alive': 0, 'workers_alive': 0,
                                  'handles_after_cleanup': handles(), 'memory_current_bytes': current, 'memory_peak_bytes': peak})
            return {'schema': 'leaf.x07.capsule-resource-soak.v1', 'scope': 'synthetic offline; owned readers/workers and process handle/Python memory samples',
                    'iterations': iterations, 'launches': len(expected), 'handles_before': start_handles,
                    'handles_after_cleanup': handles(), 'cases': cases, 'owned_resources_settled': True}
    finally:
        if own_trace:
            tracemalloc.stop()


def main():
    parser = JsonArgumentParser(result_schema='leaf.x07.capsule-resource-soak.v1', description=__doc__)
    parser.add_argument('--directory', type=Path, required=True)
    parser.add_argument('--smoke', action='store_true')
    args = parser.parse_args()
    try:
        report = measure(args.directory, iterations=2 if args.smoke else 30)
    except (OSError, ValueError):
        publish_stdout({'schema': 'leaf.x07.capsule-resource-soak.v1', 'error': 'MEASUREMENT_UNAVAILABLE'})
        return 2
    return publish_stdout(report)


if __name__ == '__main__':
    raise SystemExit(main())
