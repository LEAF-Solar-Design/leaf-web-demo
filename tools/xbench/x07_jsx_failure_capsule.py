"""Retain one synthetic X07 JSX check's bounded source-bound diagnostics."""
import hashlib
import json
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


def stop_tree(child):
    if sys.platform == 'win32':
        subprocess.run(['taskkill.exe', '/PID', str(child.pid), '/T', '/F'], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                       creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run_check(name, output, *, dependencies=None, timeout=90, checks_root=None):
    if not isinstance(name, str) or not re.fullmatch(r'x07_[a-z0-9_]+\.cjs', name):
        raise ValueError('one named X07 check required')
    if type(timeout) not in (int, float) or not 0 < timeout <= 300:
        raise ValueError('timeout budget')
    checks = Path(checks_root) if checks_root is not None else ROOT / 'tools/xbench/tests'
    check = checks / name
    if not check.is_file() or check.is_symlink():
        raise ValueError('regular check required')
    node = shutil.which('node')
    if node is None:
        raise ValueError('node unavailable')
    output = Path(output)
    # Exclusive creation reserves a new artifact before any child starts.
    with output.open('x', encoding='utf-8') as artifact:
        sources = [check, Path(__file__)]
        if checks_root is None:
            sources += [checks / 'x07_adapter_jsx_journey.cjs']
            sources += sorted((ROOT / 'web/src/solar').glob('X07*.jsx'))
            sources += sorted((ROOT / 'server').glob('x07_*.py'))
            sources += sorted((ROOT / 'tools/xbench').glob('x07_*.py'))
        digests = {str(path.resolve()): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
        head = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        dirty = bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT))
        command = [node, str(check.resolve())]
        if dependencies is not None:
            command += ['--deps', str(Path(dependencies).resolve())]
        child = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, start_new_session=sys.platform != 'win32',
                                 creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
        streams = {'stdout': bytearray(), 'stderr': bytearray()}
        exceeded = threading.Event()

        def consume(label, stream):
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    return
                available = MAX_STREAM_BYTES - len(streams[label])
                streams[label].extend(chunk[:available])
                if len(chunk) > available:
                    exceeded.set()

        threads = [threading.Thread(target=consume, args=(label, getattr(child, label)), daemon=True) for label in streams]
        for thread in threads:
            thread.start()
        timed_out = False
        try:
            child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            # Retire descendants even when an assertion exited the parent first.
            if child.poll() is None or sys.platform != 'win32':
                stop_tree(child)
            child.wait(timeout=10)
            for thread in threads:
                thread.join(10)
            child.stdout.close(); child.stderr.close()
        if any(thread.is_alive() for thread in threads):
            raise ValueError('diagnostic reader did not settle')
        report = {'schema': 'leaf.x07.jsx-failure-capsule.v1', 'source_sha': head, 'dirty': dirty,
                  'source_file_sha256': digests, 'command': command, 'exit_code': child.returncode,
                  'timed_out': timed_out, 'diagnostics_complete': not exceeded.is_set(),
                  'stdout': bytes(streams['stdout']).decode('utf-8', errors='replace'),
                  'stderr': bytes(streams['stderr']).decode('utf-8', errors='replace'),
                  'owned_processes_settled': True}
        artifact.write(json.dumps(report, sort_keys=True) + '\n')
    return report


def main():
    parser = JsonArgumentParser(result_schema='leaf.x07.jsx-failure-capsule.v1', description=__doc__)
    parser.add_argument('--check', required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--deps', type=Path)
    parser.add_argument('--timeout', type=float, default=90)
    arguments = parser.parse_args()
    try:
        report = run_check(arguments.check, arguments.out, dependencies=arguments.deps, timeout=arguments.timeout)
    except (OSError, ValueError, subprocess.SubprocessError):
        publish_stdout({'schema': 'leaf.x07.jsx-failure-capsule.v1', 'error': 'CAPSULE_UNAVAILABLE'})
        return 2
    publish_stdout({'schema': report['schema'], 'exit_code': report['exit_code'], 'timed_out': report['timed_out'],
                    'diagnostics_complete': report['diagnostics_complete']})
    return 0 if report['exit_code'] == 0 and report['diagnostics_complete'] and not report['timed_out'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
