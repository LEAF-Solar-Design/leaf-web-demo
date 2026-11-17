"""Retain one synthetic X07 JSX check's bounded source-bound diagnostics."""
import ctypes
import base64
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
from x07_profile_version_diff import JsonArgumentParser, publish_stdout


MAX_STREAM_BYTES = 256 * 1024
ROOT = Path(__file__).resolve().parents[2]


class WindowsJob:
    """Own descendants even after their original parent exits."""
    def __init__(self):
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                        ('flags', w.DWORD), ('minimum', ctypes.c_size_t), ('maximum', ctypes.c_size_t),
                        ('active', w.DWORD), ('affinity', ctypes.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', ctypes.c_ulonglong * 6),
                        ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                        ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        information = Extended()
        information.basic.flags = 0x2000
        if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(information), ctypes.sizeof(information)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error
    def assign_and_resume(self, child):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(child._handle)):
            raise ctypes.WinError(ctypes.get_last_error())
        resume = ctypes.WinDLL('ntdll').NtResumeProcess
        resume.argtypes = [ctypes.c_void_p]
        resume.restype = ctypes.c_long
        if resume(int(child._handle)) < 0:
            raise OSError('owned process resume failed')
    def close(self):
        if self.handle:
            self.kernel.TerminateJobObject(self.handle, 1)
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def snapshot(check, checks, custom_root, dependencies):
    sources = [check, Path(__file__)] + list(dependencies)
    if not custom_root:
        sources += [checks / 'x07_adapter_jsx_journey.cjs']
        sources += sorted((ROOT / 'web/src/solar').glob('X07*.jsx'))
        sources += sorted((ROOT / 'server').glob('x07_*.py'))
        sources += sorted((ROOT / 'tools/xbench').glob('x07_*.py'))
    return {'source_sha': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
            'dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT)),
            'source_file_sha256': {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}}


def stop_group(child):
    try:
        os.killpg(child.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def run_check(name, output, *, dependencies=None, timeout=90, checks_root=None,
              source_dependencies=(), on_chunk=None, stream_limit=MAX_STREAM_BYTES):
    if not isinstance(name, str) or not re.fullmatch(r'x07_[a-z0-9_]+\.cjs', name):
        raise ValueError('one named X07 check required')
    if type(timeout) not in (int, float) or not 0 < timeout <= 300 or not math.isfinite(timeout):
        raise ValueError('timeout budget')
    if type(stream_limit) is not int or not 1 <= stream_limit <= MAX_STREAM_BYTES:
        raise ValueError('stream byte budget')
    checks = Path(checks_root) if checks_root is not None else ROOT / 'tools/xbench/tests'
    check = checks / name
    if (checks.is_symlink() or not check.is_file() or check.is_symlink()
            or check.resolve().parent != checks.resolve() or check.stat().st_nlink != 1):
        raise ValueError('regular check required')
    node = shutil.which('node')
    if node is None:
        raise ValueError('node unavailable')
    before = snapshot(check, checks, checks_root is not None, source_dependencies)
    command = [node, str(check.resolve())]
    if dependencies is not None:
        command += ['--deps', str(Path(dependencies).resolve())]
    output = Path(output)
    artifact = output.open('x', encoding='utf-8')
    identity = os.fstat(artifact.fileno())
    job, child, threads = None, None, []
    streams = {'stdout': bytearray(), 'stderr': bytearray()}
    exceeded = threading.Event()
    timed_out = False
    try:
        job = WindowsJob() if sys.platform == 'win32' else None
        flags = (subprocess.CREATE_NO_WINDOW | 4) if job is not None else 0
        child = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, start_new_session=job is None, creationflags=flags)
        def consume(label, stream):
            while True:
                chunk = stream.read1(4096)
                if not chunk:
                    return
                available = stream_limit - len(streams[label])
                streams[label].extend(chunk[:available])
                if len(chunk) > available:
                    exceeded.set()
                if on_chunk is not None:
                    on_chunk(label, chunk)
        threads = [threading.Thread(target=consume, args=(label, getattr(child, label)), daemon=True) for label in streams]
        for thread in threads:
            thread.start()
        if job is not None:
            job.assign_and_resume(child)
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            if job is not None:
                job.close()
            else:
                stop_group(child)
            child.wait(timeout=10)
            for thread in threads:
                thread.join(10)
        if any(thread.is_alive() for thread in threads):
            raise ValueError('diagnostic reader did not settle')
        try:
            after = snapshot(check, checks, checks_root is not None, source_dependencies)
        except OSError:
            after = None
        unchanged = before == after
        report = {'schema': 'leaf.x07.jsx-failure-capsule.v1', 'command': command, 'exit_code': child.returncode,
                  'worker_pid': child.pid,
                  'timed_out': timed_out, 'diagnostics_complete': not exceeded.is_set(),
                  'stdout': bytes(streams['stdout']).decode('utf-8', errors='replace'),
                  'stderr': bytes(streams['stderr']).decode('utf-8', errors='replace'),
                  'owned_processes_settled': True, 'source_unchanged': unchanged,
                  'stream_limit': stream_limit,
                  'resources': {'readers_started': len(threads), 'readers_alive': sum(thread.is_alive() for thread in threads),
                                'parent_reaped': child.returncode is not None, 'job_closed': job is None or job.handle is None}}
        for label, raw in streams.items():
            report[label + '_bytes'] = len(raw)
            try:
                raw.decode('utf-8', errors='strict')
                report[label + '_encoding'] = 'utf-8'
            except UnicodeDecodeError:
                report[label + '_encoding'] = 'binary-base64'
                report[label + '_base64'] = base64.b64encode(raw).decode('ascii')
        if unchanged:
            report.update(before)
        else:
            report.update(error='SOURCE_CHANGED', source_before=before, source_after=after)
        serialized = json.dumps(report, sort_keys=True) + '\n'
        if artifact.write(serialized) != len(serialized):
            raise OSError('short capture write')
        artifact.flush()
        artifact.close()
        return report
    except BaseException:
        try:
            artifact.close()
        except OSError:
            pass
        current = output.lstat()
        if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
            output.unlink()
        raise
    finally:
        if job is not None:
            job.close()
        if child is not None:
            if job is None:
                stop_group(child)
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)
            for thread in threads:
                thread.join(10)
            child.stdout.close(); child.stderr.close()


def exit_code(report):
    if report.get('error') or not report['source_unchanged']:
        return 2
    return 0 if report['exit_code'] == 0 and report['diagnostics_complete'] and not report['timed_out'] else 1


def main():
    parser = JsonArgumentParser(result_schema='leaf.x07.jsx-failure-capsule.v1', description=__doc__)
    parser.add_argument('--check', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--deps', type=Path)
    parser.add_argument('--timeout', type=float, default=90)
    parser.add_argument('--stream-bytes', type=int, default=MAX_STREAM_BYTES)
    arguments = parser.parse_args()
    try:
        report = run_check(arguments.check, arguments.out, dependencies=arguments.deps, timeout=arguments.timeout, stream_limit=arguments.stream_bytes)
    except (OSError, ValueError, subprocess.SubprocessError):
        publish_stdout({'schema': 'leaf.x07.jsx-failure-capsule.v1', 'error': 'CAPSULE_UNAVAILABLE'})
        return 2
    summary = {key: report[key] for key in ('schema', 'exit_code', 'timed_out', 'diagnostics_complete', 'source_unchanged')}
    if report.get('error'):
        summary['error'] = report['error']
    if publish_stdout(summary):
        return 2
    return exit_code(report)


if __name__ == '__main__':
    raise SystemExit(main())
