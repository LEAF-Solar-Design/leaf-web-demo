"""Retain one synthetic X07 JSX check's bounded source-bound diagnostics."""
import ctypes
import base64
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from x07_profile_version_diff import JsonArgumentParser, publish_stdout


MAX_STREAM_BYTES = 256 * 1024
ROOT = Path(__file__).resolve().parents[2]
MAX_CAPTURE_BYTES = 8 * 1024 * 1024
MAX_CAPTURE_DEPTH = 32
MAX_CAPTURE_NODES = 20000


class CaptureCancelled(ValueError):
    pass


def _bounded_json(text):
    # Count containers, keys and scalar values before allocating the object tree.
    depth = nodes = index = 0
    while index < len(text):
        char = text[index]
        if char in ' \r\n\t,:':
            index += 1
            continue
        if char in '}]':
            depth -= 1
            index += 1
            continue
        nodes += 1
        if nodes > MAX_CAPTURE_NODES:
            raise ValueError('capture node budget')
        if char in '{[':
            depth += 1
            if depth > MAX_CAPTURE_DEPTH:
                raise ValueError('capture depth budget')
            index += 1
        elif char == '"':
            index += 1
            while index < len(text):
                char = text[index]
                index += 1
                if char == '\\':
                    index += 1
                elif char == '"':
                    break
        else:
            while index < len(text) and text[index] not in ' \r\n\t,:{}[]':
                index += 1
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError('duplicate capture key')
            result[key] = value
        return result
    def constant(value):
        raise ValueError('nonfinite capture number')
    value = json.loads(text, object_pairs_hook=pairs, parse_constant=constant)
    # Reject overflowed exponents and escaped lone surrogates, including keys.
    json.dumps(value, allow_nan=False, ensure_ascii=False).encode('utf-8')
    return value


def _integer(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def _source_binding(value):
    return (isinstance(value, dict) and isinstance(value.get('source_sha'), str)
            and re.fullmatch(r'[a-f0-9]{40}', value['source_sha']) is not None
            and type(value.get('dirty')) is bool
            and isinstance(value.get('source_file_sha256'), dict) and bool(value['source_file_sha256'])
            and all(isinstance(key, str) and key and isinstance(digest, str)
                    and re.fullmatch(r'[a-f0-9]{64}', digest) for key, digest in value['source_file_sha256'].items()))


def _validate_capture(value):
    if (not isinstance(value, dict) or value.get('schema') != 'leaf.x07.jsx-failure-capsule.v1'
            or value.get('owned_processes_settled') is not True
            or not _integer(value.get('exit_code'), -(2**31), 2**32 - 1)
            or not _integer(value.get('worker_pid'), 1, 2**32 - 1)
            or not _integer(value.get('stream_limit'), 1, MAX_STREAM_BYTES)
            or any(type(value.get(key)) is not bool for key in ('timed_out', 'diagnostics_complete', 'source_unchanged'))
            or not isinstance(value.get('command'), list) or len(value['command']) < 2
            or any(not isinstance(arg, str) or not arg or '\x00' in arg for arg in value['command'])):
        raise ValueError('invalid capture')
    resources = value.get('resources')
    if (not isinstance(resources, dict) or type(resources.get('readers_started')) is not int
            or resources['readers_started'] != 2 or type(resources.get('readers_alive')) is not int
            or resources['readers_alive'] != 0 or resources.get('parent_reaped') is not True
            or resources.get('job_closed') is not True):
        raise ValueError('unsettled capture')
    errors = value.get('reader_errors', [])
    if not isinstance(errors, list) or any(label not in ('stdout', 'stderr') for label in errors) or len(set(errors)) != len(errors):
        raise ValueError('invalid reader errors')
    complete = not errors
    for label in ('stdout', 'stderr'):
        content = value.get(label)
        count, received = value.get(label + '_bytes'), value.get(label + '_received_bytes')
        eof = value.get(label + '_complete')
        if (not isinstance(content, str) or not _integer(count, 0, value['stream_limit'])
                or not _integer(received, 0, 2**53 - 1) or count != min(received, value['stream_limit'])
                or type(eof) is not bool or eof != (label not in errors)):
            raise ValueError('invalid diagnostic counts')
        encoding = value.get(label + '_encoding')
        if encoding == 'utf-8':
            raw = content.encode('utf-8')
            if label + '_base64' in value:
                raise ValueError('contradictory encoding')
        elif encoding == 'binary-base64':
            encoded = value.get(label + '_base64')
            if not isinstance(encoded, str):
                raise ValueError('missing binary evidence')
            raw = base64.b64decode(encoded, validate=True)
            if base64.b64encode(raw).decode('ascii') != encoded or raw.decode('utf-8', errors='replace') != content:
                raise ValueError('contradictory binary evidence')
            try:
                raw.decode('utf-8')
            except UnicodeDecodeError:
                pass
            else:
                raise ValueError('unnecessary binary encoding')
        else:
            raise ValueError('unknown encoding')
        if len(raw) != count or hashlib.sha256(raw).hexdigest() != value.get(label + '_sha256'):
            raise ValueError('contradictory diagnostic evidence')
        complete = complete and eof and received <= value['stream_limit']
    if value['diagnostics_complete'] != complete:
        raise ValueError('contradictory completeness')
    if value['source_unchanged']:
        if not _source_binding(value) or value.get('error') != ('READER_FAILED' if errors else None):
            raise ValueError('missing source binding')
    elif (value.get('error') != 'SOURCE_CHANGED' or not _source_binding(value.get('source_before'))
          or not (value.get('source_after') is None or _source_binding(value['source_after']))
          or 'source_file_sha256' in value):
        raise ValueError('unlabelled source refusal')


def _file_state(metadata):
    return metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns


def _read_regular(path, limit):
    """Read one bounded file whose path and opened identity remain stable."""
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError('regular capture required')
    if metadata.st_size > limit:
        raise ValueError('file byte budget')
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_NOFOLLOW', 0)
    descriptor = os.open(path, flags)
    try:
        stream = os.fdopen(descriptor, 'rb')
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode) or _file_state(opened) != _file_state(metadata):
            raise ValueError('file changed before read')
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        current = path.lstat()
        if (len(raw) != opened.st_size or len(raw) > limit or not stat.S_ISREG(current.st_mode)
                or _file_state(after) != _file_state(opened) or _file_state(current) != _file_state(opened)):
            raise ValueError('file changed during read')
    return raw


def _verify_local_source(value, source_root, recorded_source_root):
    try:
        root = Path(source_root)
        if root.is_symlink():
            raise ValueError('linked source root')
        root = root.resolve(strict=True)
        if not root.is_dir() or not value['source_unchanged']:
            raise ValueError('source unavailable')
        for name, digest in value['source_file_sha256'].items():
            # Interpret recorded Windows paths on POSIX too, without consulting that host.
            windows = PureWindowsPath(name)
            recorded = windows if windows.drive or '\\' in name else PurePosixPath(name)
            if recorded.is_absolute():
                if recorded_source_root is None:
                    raise ValueError('explicit recorded source root required')
                origin = type(recorded)(str(recorded_source_root))
                if not origin.is_absolute():
                    raise ValueError('absolute recorded source root required')
                relative = recorded.relative_to(origin)
            else:
                relative = recorded
            if relative.drive or relative.root or '..' in relative.parts or not relative.parts:
                raise ValueError('escaping dependency')
            local = root
            for part in relative.parts:
                local = local / part
                if local.is_symlink():
                    raise ValueError('linked dependency')
            local.resolve(strict=True).relative_to(root)
            if hashlib.sha256(_read_regular(local, MAX_CAPTURE_BYTES)).hexdigest() != digest:
                raise ValueError('dependency changed')
        return {'status': 'VERIFIED', 'dependencies': len(value['source_file_sha256'])}
    except (OSError, ValueError) as error:
        raise ValueError('SOURCE_UNVERIFIABLE') from error


def read_capture(path, *, source_root=None, recorded_source_root=None):
    """Strict current-schema consumer. Partial artifacts never count as success."""
    path = Path(path)
    try:
        raw = _read_regular(path, MAX_CAPTURE_BYTES)
        if len(raw) > MAX_CAPTURE_BYTES or not raw.endswith(b'\n'):
            raise ValueError('incomplete capture')
        value = _bounded_json(raw.decode('utf-8'))
        _validate_capture(value)
        if recorded_source_root is not None and source_root is None:
            raise ValueError('SOURCE_UNVERIFIABLE')
        if source_root is not None:
            value['local_source_verification'] = _verify_local_source(value, source_root, recorded_source_root)
        return value
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError('capture unavailable') from error


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
              source_dependencies=(), on_chunk=None, stream_limit=MAX_STREAM_BYTES, cancel_event=None):
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
    received = dict.fromkeys(streams, 0)
    eof = dict.fromkeys(streams, False)
    exceeded = threading.Event()
    reader_failed = threading.Event()
    reader_errors = []
    timed_out = False
    try:
        job = WindowsJob() if sys.platform == 'win32' else None
        flags = (subprocess.CREATE_NO_WINDOW | 4) if job is not None else 0
        child = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, start_new_session=job is None, creationflags=flags)
        def consume(label, stream):
            try:
                while True:
                    chunk = stream.read1(4096)
                    if not chunk:
                        eof[label] = True
                        return
                    received[label] += len(chunk)
                    available = stream_limit - len(streams[label])
                    streams[label].extend(chunk[:available])
                    if len(chunk) > available:
                        exceeded.set()
                    if on_chunk is not None:
                        on_chunk(label, chunk)
            except BaseException:
                reader_errors.append(label)
                reader_failed.set()
        threads = [threading.Thread(target=consume, args=(label, getattr(child, label)), daemon=True) for label in streams]
        for thread in threads:
            thread.start()
        if job is not None:
            job.assign_and_resume(child)
        try:
            deadline = time.monotonic() + timeout
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise CaptureCancelled('capture cancelled')
                if reader_failed.is_set():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                try:
                    child.wait(timeout=min(.05, remaining))
                    break
                except subprocess.TimeoutExpired:
                    pass
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
                  'timed_out': timed_out, 'diagnostics_complete': not exceeded.is_set() and not reader_errors,
                  'stdout': bytes(streams['stdout']).decode('utf-8', errors='replace'),
                  'stderr': bytes(streams['stderr']).decode('utf-8', errors='replace'),
                  'owned_processes_settled': True, 'source_unchanged': unchanged,
                  'stream_limit': stream_limit,
                  'resources': {'readers_started': len(threads), 'readers_alive': sum(thread.is_alive() for thread in threads),
                                'parent_reaped': child.returncode is not None, 'job_closed': job is None or job.handle is None}}
        for label, raw in streams.items():
            report[label + '_bytes'] = len(raw)
            report[label + '_received_bytes'] = received[label]
            report[label + '_complete'] = eof[label]
            report[label + '_sha256'] = hashlib.sha256(raw).hexdigest()
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
        if reader_errors:
            report['reader_errors'] = sorted(reader_errors)
            if 'error' not in report:
                report['error'] = 'READER_FAILED'
        serialized = json.dumps(report, sort_keys=True) + '\n'
        if artifact.write(serialized) != len(serialized):
            raise OSError('short capture write')
        artifact.flush()
        artifact.close()
        return report
    except BaseException as error:
        try:
            artifact.close()
        except OSError:
            pass
        current = output.lstat()
        if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
            output.unlink()
        if isinstance(error, KeyboardInterrupt):
            raise CaptureCancelled('capture cancelled') from None
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
    except (CaptureCancelled, KeyboardInterrupt):
        publish_stdout({'schema': 'leaf.x07.jsx-failure-capsule.v1', 'error': 'CAPTURE_CANCELLED'})
        return 2
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
