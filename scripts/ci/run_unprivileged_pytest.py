"""Fixed corpus pytest identity boundary; never a general command launcher.

The private snapshot is an exact copy of tracked working-tree bytes, not a
checkout of HEAD. Reading it under the outer process supervisor deliberately
retains conservative source dependencies. No historical CI cause is inferred.
"""
from __future__ import annotations

import ctypes
from contextlib import contextmanager
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from urllib.parse import quote

TARGETS = {"tests/test_lab_runs.py", "tests/test_lab_corpus_executor.py"}
UID = GID = 10003
MAX_FILES = 10000
MAX_BYTES = 300 * 1024 * 1024
SELF = Path(__file__).resolve()
REPO = SELF.parents[2]


class Stopped(Exception):
    def __init__(self, signum):
        self.signum = signum


@contextmanager
def termination_guard():
    def stop(signum, frame):
        raise Stopped(signum)

    previous = {}
    try:
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            previous[signum] = signal.signal(signum, stop)
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def validate_args(args):
    if len(args) < 2 or args[0] != "pytest":
        raise ValueError("routing")
    target = args[1].split("::", 1)[0]
    if target not in TARGETS:
        raise ValueError("routing")
    # Options used by these gates, plus explicit nodeids for focused reruns.
    index = 2
    while index < len(args):
        word = args[index]
        if word in ("-q", "-v", "-vv", "-s", "--color=no"):
            index += 1
        elif word in ("-r", "-p") and index + 1 < len(args):
            allowed = {"s", "a", "A"} if word == "-r" else {"no:cacheprovider", "pytest_selection"}
            if args[index + 1] not in allowed:
                raise ValueError("option")
            index += 2
        elif any(word.startswith("--leaf-" + name + "=") for name in
                 ("output", "repo", "selection", "catalog")):
            index += 1
        elif word.split("::", 1)[0] == target:
            index += 1
        else:
            raise ValueError("option")
    return list(args)


def identity():
    fields = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines()
                  if ":" in line)
    ids = [int(value) for value in fields["Uid"].split()]
    caps = [int(fields[name].strip(), 16) for name in ("CapEff", "CapPrm", "CapInh", "CapAmb")]
    if not all(ids) or any(caps) or fields["NoNewPrivs"].strip() != "1":
        raise ValueError("identity")
    return ids[1]


def child(args):
    validate_args(args)
    # PR_SET_NO_NEW_PRIVS is irreversible and is inherited across exec.
    if ctypes.CDLL(None, use_errno=True).prctl(38, 1, 0, 0, 0) != 0:
        raise OSError("nnp")
    uid = identity()
    print(f"corpus identity uid={uid} CapEff=0 CapPrm=0 CapInh=0 CapAmb=0 NNP=1", flush=True)
    os.execv(sys.executable, [sys.executable, "-m", *args])


def safe_file(root, relative):
    rel = PurePosixPath(relative)
    if rel.is_absolute() or not rel.parts or any(p in ("..", ".git") for p in rel.parts) or "\\" in relative:
        raise ValueError("source_path")
    current = root
    for part in rel.parts:
        current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError("source_symlink")
    if not stat.S_ISREG(info.st_mode) or not current.resolve().is_relative_to(root.resolve()):
        raise ValueError("source_type")
    return current


class Budget:
    def __init__(self):
        self.files = self.bytes = 0

    def copy(self, source, destination):
        info = source.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("copy_type")
        self.files += 1
        self.bytes += info.st_size
        if self.files > MAX_FILES or self.bytes > MAX_BYTES:
            raise ValueError("copy_budget")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as src, destination.open("xb") as dst:
            remaining = info.st_size
            while remaining:
                block = src.read(min(1024 * 1024, remaining))
                if not block:
                    raise ValueError("copy_changed")
                dst.write(block)
                remaining -= len(block)
            if src.read(1):
                raise ValueError("copy_changed")
        if destination.stat().st_size != info.st_size:
            raise ValueError("copy_changed")
        destination.chmod(0o700 if info.st_mode & 0o111 else 0o600)


def snapshot(repo, destination, budget):
    result = subprocess.run(["git", "-C", str(repo), "ls-files", "-z", "--cached"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True, timeout=30)
    names = set(os.fsdecode(name) for name in result.stdout.split(b"\0") if name)
    names.add("scripts/ci/run_unprivileged_pytest.py")
    if len(names) > MAX_FILES:
        raise ValueError("source_budget")
    for name in sorted(names):
        budget.copy(safe_file(repo, name), destination / name)


def own_tree(directory):
    # Only the unique tempfile tree is ever chmod/chowned.
    for root, dirs, files in os.walk(directory):
        os.chown(root, UID, GID)
        os.chmod(root, 0o700)
        for name in files:
            os.chown(Path(root) / name, UID, GID)


def public_executable(path):
    original = Path(path).absolute()
    path = original.resolve(strict=True)
    if not path.is_file() or not path.stat().st_mode & stat.S_IXOTH:
        raise ValueError("executable_access")
    if any(not parent.stat().st_mode & stat.S_IXOTH for parent in (*original.parents, *path.parents)):
        raise ValueError("executable_access")


def remap(value, mappings):
    if isinstance(value, str):
        for private, original in sorted(mappings, key=lambda pair: len(pair[0]), reverse=True):
            if value == private or value.startswith(private + "/"):
                return original + value[len(private):]
    elif isinstance(value, list):
        return [remap(item, mappings) for item in value]
    elif isinstance(value, dict):
        return {key: remap(item, mappings) for key, item in value.items()}
    return value


def handback(private, original, mappings, budget):
    if not private.exists():
        return
    for directory, dirs, files in os.walk(private, followlinks=False):
        if any((Path(directory) / name).is_symlink() for name in dirs):
            raise ValueError("output_symlink")
        for name in files:
            rel = (Path(directory) / name).relative_to(private).as_posix()
            src = safe_file(private, rel)
            dest = original / rel
            # No child-controlled symlink may redirect a privileged write.
            for parent in (dest, *dest.parents):
                if parent.is_symlink():
                    raise ValueError("destination_symlink")
            if dest.exists():
                raise ValueError("output_collision")
            budget.copy(src, dest)
            if dest.suffix in (".json", ".jsonl"):
                content = dest.read_text(encoding="utf-8")
                if dest.suffix == ".json":
                    content = json.dumps(remap(json.loads(content), mappings))
                else:
                    content = "".join(json.dumps(remap(json.loads(line), mappings)) + "\n"
                                      for line in content.splitlines())
                dest.write_text(content, encoding="utf-8")


def prepare(directory, args, environment):
    budget = Budget()
    source = directory / "source"
    snapshot(REPO, source, budget)
    env = dict(environment)
    mappings = [(str(source), str(REPO))]
    outputs = []
    for key in ("HOME", "TMPDIR", "TMP", "TEMP"):
        path = directory / ("home" if key == "HOME" else "tmp")
        path.mkdir(exist_ok=True)
        env[key] = str(path)
    runtime_paths = {}
    trusted = Path(env.get("LEAF_TRUSTED_CI_DIR") or REPO / "scripts/ci").absolute()
    for raw in [env.get("LEAF_TRUSTED_CI_DIR", ""), *env.get("PYTHONPATH", "").split(os.pathsep)]:
        if not raw or raw in runtime_paths:
            continue
        original = Path(raw).absolute()
        if original == trusted:
            # These four modules form the reporter's complete non-stdlib
            # dependency closure. Never sweep an untracked runtime directory.
            copied = directory / ("runtime-" + str(len(runtime_paths)))
            for name in ("sitecustomize.py", "pytest_selection.py", "select_tests.py", "trace_reads.py"):
                budget.copy(safe_file(original, name), copied / name)
        elif original.is_relative_to(REPO):
            copied = source / original.relative_to(REPO)
            if not copied.is_dir():
                raise ValueError("runtime_source")
        else:
            raise ValueError("runtime_unrecognized")
        runtime_paths[raw] = str(copied)
        mappings.append((str(copied), str(original)))
    if env.get("LEAF_TRUSTED_CI_DIR"):
        env["LEAF_TRUSTED_CI_DIR"] = runtime_paths[env["LEAF_TRUSTED_CI_DIR"]]
    if env.get("PYTHONPATH"):
        if any(not part for part in env["PYTHONPATH"].split(os.pathsep)):
            raise ValueError("runtime_empty_path")
        env["PYTHONPATH"] = os.pathsep.join(runtime_paths[p] for p in env["PYTHONPATH"].split(os.pathsep))
    for key in ("LEAF_TEST_REPORT_DIR", "LEAF_READSET_DIR", "LEAF_PROCESS_CAPTURE_DIR"):
        if not env.get(key):
            continue
        original = Path(env[key]).absolute()
        private = directory / key.lower()
        private.mkdir()
        env[key] = str(private)
        mappings.append((str(private), str(original)))
        if key == "LEAF_READSET_DIR":
            suite = quote(env["LEAF_READSET_SUITE"], safe="").replace(".", "%2E")
            attempt = str(int(env["LEAF_READSET_ATTEMPT"]))
            if int(attempt) < 1:
                raise ValueError("readset_attempt")
            outputs.append((private / suite / attempt, original / suite / attempt))
        else:
            outputs.append((private, original))
    if env.get("LEAF_READSET_ROOT"):
        if Path(env["LEAF_READSET_ROOT"]).resolve() != REPO:
            raise ValueError("readset_root")
        env["LEAF_READSET_ROOT"] = str(source)
    translated = []
    for word in args:
        if word.startswith(("--leaf-selection=", "--leaf-catalog=")):
            option, raw = word.split("=", 1)
            original = Path(raw).absolute()
            copied = directory / (option[2:] + ".json")
            budget.copy(safe_file(original.parent, original.name), copied)
            translated.append(option + "=" + str(copied))
        elif word.startswith("--leaf-output="):
            raw = word.split("=", 1)[1]
            if Path(raw).absolute() != Path(environment["LEAF_TEST_REPORT_DIR"]).absolute():
                raise ValueError("report_output")
            translated.append("--leaf-output=" + env["LEAF_TEST_REPORT_DIR"])
        elif word.startswith("--leaf-repo="):
            if Path(word.split("=", 1)[1]).resolve() != REPO:
                raise ValueError("report_root")
            translated.append("--leaf-repo=" + str(source))
        else:
            translated.append(word)
    own_tree(directory)
    return source, translated, env, outputs, mappings


def supervise(command, cwd, env):
    process = None
    stopped = []
    previous = {}
    libc = ctypes.CDLL(None, use_errno=True)
    prior_subreaper = ctypes.c_int()
    if (libc.prctl(37, ctypes.byref(prior_subreaper), 0, 0, 0) != 0 or
            libc.prctl(36, 1, 0, 0, 0) != 0):
        raise OSError("subreaper")

    def stop(signum, frame):
        stopped.append(signum)
        if process is not None:
            try:
                os.killpg(process.pid, signum)
            except ProcessLookupError:
                pass

    def reap_exited_orphans(signum=None, frame=None):
        # Native PostgreSQL detaches from pg_ctl. As its subreaper we must
        # reap its exit while pytest is still checking cleanup, not only
        # after pytest exits. Preserve Popen's ownership of the main child.
        if process is None:
            return
        children_file = Path(f"/proc/self/task/{os.getpid()}/children")
        for pid in (int(value) for value in children_file.read_text().split()):
            if process is not None and pid == process.pid:
                continue
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass

    try:
        for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            previous[signum] = signal.signal(signum, stop)
        previous[signal.SIGCHLD] = signal.signal(signal.SIGCHLD, reap_exited_orphans)
        process = subprocess.Popen(command, cwd=cwd, env=env, start_new_session=True)
        if stopped:
            stop(stopped[-1], None)
        while True:
            reap_exited_orphans()
            try:
                code = process.wait(timeout=0.2)
                if stopped:
                    return 128 + stopped[-1]
                return 128 + (-code) if code < 0 else code
            except subprocess.TimeoutExpired:
                if stopped:
                    try:
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                    code = process.wait()
                    return 128 + stopped[-1]
    finally:
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            # Reap even descendants which created a new session (e.g. private
            # PostgreSQL). Subreaper adoption closes the process-group escape.
            deadline = time.monotonic() + 3
            children_file = Path(f"/proc/self/task/{os.getpid()}/children")
            while True:
                children = [int(pid) for pid in children_file.read_text().split()]
                if not children:
                    break
                for pid in children:
                    try:
                        os.kill(pid, signal.SIGKILL)
                        os.waitpid(pid, os.WNOHANG)
                    except (ProcessLookupError, ChildProcessError):
                        pass
                if time.monotonic() >= deadline:
                    raise OSError("descendant_reap")
                time.sleep(0.01)
        libc.prctl(36, prior_subreaper.value, 0, 0, 0)
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def main(args=None):
    args = list(sys.argv[1:] if args is None else args)
    stage = "arguments"
    try:
        if args[:1] == ["--child"]:
            stage = "identity"
            child(args[1:])
        args = validate_args(args)
        if not sys.platform.startswith("linux"):
            return subprocess.call([sys.executable, "-m", *args], cwd=REPO / "server")
        if os.geteuid() != 0:
            return supervise([sys.executable, "-I", str(SELF), "--child", *args],
                             REPO / "server", os.environ.copy())
        stage = "prerequisites"
        setpriv = shutil.which("setpriv")
        if not setpriv:
            raise ValueError("setpriv")
        public_executable(setpriv)
        public_executable(sys.executable)
        stage = "prepare"
        with termination_guard(), tempfile.TemporaryDirectory(prefix="leaf-corpus-", dir="/tmp") as temporary:
            directory = Path(temporary)
            source, argv, env, outputs, mappings = prepare(directory, args, os.environ)
            stage = "execute"
            command = [setpriv, "--reuid=10003", "--regid=10003", "--clear-groups",
                       "--bounding-set=-all", "--inh-caps=-all", "--ambient-caps=-all",
                       "--no-new-privs", sys.executable, "-I",
                       str(source / "scripts/ci/run_unprivileged_pytest.py"), "--child", *argv]
            code = supervise(command, source / "server", env)
            stage = "handback"
            budget = Budget()
            for private, original in outputs:
                handback(private, original, mappings, budget)
            return code
    except Stopped as exc:
        return 128 + exc.signum
    except Exception as exc:
        print(f"corpus launcher refused stage={stage} class={type(exc).__name__}", file=sys.stderr)
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
