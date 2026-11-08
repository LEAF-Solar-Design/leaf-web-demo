"""Read auditing stays transparent and follows ordinary Python children."""

import builtins
import copy
import importlib.util
import inspect
import json
import multiprocessing
import os
from pathlib import Path
import py_compile
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
import types

import pytest


MODULE = Path(__file__).resolve().parents[1] / "read_audit.py"
SPEC = importlib.util.spec_from_file_location("test_leaf_read_audit", MODULE)
audit = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = audit
SPEC.loader.exec_module(audit)


@pytest.fixture
def environment(tmp_path, monkeypatch):
    root = tmp_path / "root"
    logs = tmp_path / "audit"
    root.mkdir()
    logs.mkdir()
    originals = {name: getattr(os, name) for name in
                 ("stat", "lstat", "scandir", "open", "access", "readlink")}
    path_originals = {name: getattr(os.path, name) for name in
                      ("exists", "lexists", "isfile", "isdir", "islink")}
    pathlib_originals = {name: getattr(Path, name) for name in
                         ("exists", "is_file", "is_dir", "is_symlink")}
    supports = [(support, set(support)) for support in
                (os.supports_fd, os.supports_dir_fd, os.supports_follow_symlinks,
                 os.supports_effective_ids)]
    execute_child = subprocess.Popen._execute_child
    previous_owner = getattr(sys, "_leaf_read_audit_owner", None)
    monkeypatch.setenv(audit.ENV_ROOT, str(root))
    monkeypatch.setenv(audit.ENV_DIR, str(logs))
    monkeypatch.setenv("PYTHONDONTWRITEBYTECODE", "1")
    monkeypatch.setenv("PYTHONPATH", "")
    monkeypatch.chdir(root)
    audit._state = None
    yield root, logs
    state = audit._state
    if state is not None:
        audit._close_trace(state)
        audit._rebind([(wrapped, original) for original, wrapped in state["replacements"]])
        os.close(state["fd"])
    audit._state = None
    if previous_owner is None:
        if hasattr(sys, "_leaf_read_audit_owner"):
            delattr(sys, "_leaf_read_audit_owner")
    else:
        sys._leaf_read_audit_owner = previous_owner
    subprocess.Popen._execute_child = execute_child
    for name, original in originals.items():
        setattr(os, name, original)
    for name, original in path_originals.items():
        setattr(os.path, name, original)
    for name, original in pathlib_originals.items():
        setattr(Path, name, original)
    for support, original in supports:
        support.clear()
        support.update(original)
    audit._local.busy = False


def entries(logs):
    # Reading logs must not itself extend the read set under inspection.
    busy = getattr(audit._local, "busy", False)
    audit._local.busy = True
    try:
        return [json.loads(line) for path in logs.glob("*.jsonl")
                for line in path.read_text(encoding="utf-8").splitlines()]
    finally:
        audit._local.busy = busy


def reads(logs, kind):
    return [entry["p"] for entry in entries(logs) if entry.get("k") == kind]


def test_inert_without_environment(environment, monkeypatch):
    original = os.stat
    monkeypatch.delenv(audit.ENV_DIR)
    assert audit.install() is False
    assert os.stat is original
    monkeypatch.setenv(audit.ENV_DIR, "")
    assert audit.install() is False
    monkeypatch.setenv(audit.ENV_DIR, str(environment[1]))
    monkeypatch.delenv(audit.ENV_ROOT)
    assert audit.install() is False
    assert os.stat is original
    assert not list(environment[1].iterdir())


def test_open_paths_normalized_deduplicated_and_filtered(environment):
    root, logs = environment
    target = root / "input.txt"
    target.write_text("payload", encoding="utf-8")
    outside = root.parent / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    for directory in ("__pycache__", ".git", ".pytest_cache"):
        (root / directory).mkdir()
        (root / directory / "skip").write_text("skip", encoding="utf-8")
    assert audit.install() is True
    for path in ("input.txt", str(target), target, os.fsencode(target), "input.txt"):
        with open(path) as stream:
            assert stream.read() == "payload"
    with outside.open():
        pass
    for directory in ("__pycache__", ".git", ".pytest_cache"):
        with (root / directory / "skip").open():
            pass
    fd = os.open(target, os.O_RDONLY)
    with open(fd, closefd=True):
        pass
    assert reads(logs, "f") == ["input.txt"]
    header = entries(logs)[0]
    assert header == {"v": 1, "pid": os.getpid(), "argv": sys.argv[:3]}


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX FIFOs")
@pytest.mark.parametrize("check_lock", [False, True], ids=["reader-writer", "lock-available"])
def test_fifo_open_does_not_hold_audit_lock(environment, check_lock):
    root, logs = environment
    # Isolate a regression's blocked daemon threads from pytest and fixture teardown.
    code = inspect.cleandoc(f"""
        import importlib.util
        import os
        import sys
        import threading
        import time

        spec = importlib.util.spec_from_file_location('audit', {str(MODULE)!r})
        audit = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = audit
        spec.loader.exec_module(audit)
        assert audit.install()
        fifo = os.path.join(os.environ[audit.ENV_ROOT], 'input.fifo')
        os.mkfifo(fifo)
        entered = threading.Event()
        finished = threading.Event()
        received = []
        errors = []

        def reader_opener(path, flags):
            # Called inside the real builtins.open, before its blocking syscall.
            entered.set()
            return os.open(path, flags)

        def read():
            try:
                with open(fifo, 'rb', opener=reader_opener) as stream:
                    received.append(stream.read())
            except BaseException as exc:
                errors.append(repr(exc))
            finally:
                finished.set()

        def write():
            try:
                with open(fifo, 'wb') as stream:
                    stream.write(b'x')
            except BaseException as exc:
                errors.append(repr(exc))

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        assert entered.wait(timeout=5), 'reader never entered the real open'
        assert not finished.is_set(), errors
        if {check_lock!r}:
            acquired = []

            def probe_lock():
                lock = audit._state['lock']
                held = lock.acquire(timeout=2)
                try:
                    acquired.append(held)
                finally:
                    if held:
                        lock.release()

            observer = threading.Thread(target=probe_lock, daemon=True)
            observer.start()
            observer.join(timeout=5)
            assert not observer.is_alive(), 'lock probe did not finish'
            assert acquired == [True], 'audit lock held across FIFO open'
            assert not finished.is_set(), errors
        writer = threading.Thread(target=write, daemon=True)
        writer.start()
        deadline = time.monotonic() + 10
        for thread in (reader, writer):
            thread.join(timeout=max(0, deadline - time.monotonic()))
        assert not reader.is_alive(), 'FIFO reader did not finish within 10 s'
        assert not writer.is_alive(), 'FIFO writer did not finish within 10 s'
        assert not errors, errors
        assert received == [b'x'], received
    """)
    result = subprocess.run([sys.executable, "-c", code], cwd=root,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "input.fifo" in reads(logs, "f")
    assert not list(logs.glob("*.error"))


def test_probes_listings_and_entry_stat(environment):
    root, logs = environment
    (root / "listed").mkdir()
    (root / "listed" / "entry").write_text("value", encoding="utf-8")
    assert audit.install()
    with pytest.raises(FileNotFoundError):
        os.stat("missing-stat")
    with pytest.raises(FileNotFoundError):
        os.lstat("missing-lstat")
    assert os.path.exists("missing-exists") is False
    assert Path("missing-pathlib").exists() is False
    assert os.listdir("listed") == ["entry"]
    with os.scandir("listed") as iterator:
        assert iter(iterator) is iterator
        entry = next(iterator)
        assert entry.name == "entry"
        assert os.fspath(entry) == os.path.join("listed", "entry")
        assert entry.is_file()
        assert entry.stat().st_size == 5
        with pytest.raises(StopIteration):
            next(iterator)
    iterator = os.scandir("listed")
    assert next(iterator).name == "entry"
    iterator.close()
    assert set(reads(logs, "f")) == {
        "missing-stat", "missing-lstat", "listed/entry",
    }
    assert set(reads(logs, "e")) == {"missing-exists", "missing-pathlib"}
    assert reads(logs, "d") == ["listed"]


def test_default_listings_and_original_argument_errors(environment):
    root, logs = environment
    assert audit.install()
    assert os.listdir() == []
    with os.scandir() as iterator:
        assert list(iterator) == []
    with pytest.raises(TypeError):
        os.stat()
    with pytest.raises(TypeError):
        os.lstat()
    assert reads(logs, "d") == ["."]
    assert reads(logs, "f") == []


@pytest.mark.parametrize("operation", ["listdir", "scandir"])
@pytest.mark.parametrize("args", [(), (None,)])
def test_default_listing_records_current_directory(environment, monkeypatch, operation, args):
    root, logs = environment
    directory = root / "current"
    directory.mkdir()
    assert audit.install()
    monkeypatch.chdir(directory)
    if operation == "listdir":
        assert os.listdir(*args) == []
    else:
        with os.scandir(*args) as iterator:
            assert list(iterator) == []
    assert reads(logs, "d") == ["current"]


def test_capability_sets_preserved(environment):
    support_sets = (os.supports_fd, os.supports_dir_fd, os.supports_follow_symlinks)
    before = {name: [getattr(os, name) in support for support in support_sets]
              for name in ("stat", "lstat", "scandir", "open")}
    assert audit.install()
    for name, memberships in before.items():
        assert [getattr(os, name) in support for support in support_sets] == memberships


def test_python_child_installs_and_chains_sitecustomize(environment, monkeypatch):
    root, logs = environment
    target = root / "child-input"
    target.write_text("child data", encoding="utf-8")
    next_site = root.parent / "next-site"
    next_site.mkdir()
    (next_site / "sitecustomize.py").write_text(
        "import os\nos.environ['LEAF_TEST_NEXT_SITE'] = __name__\n", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", str(next_site))
    assert audit.install()
    result = subprocess.run(
        [sys.executable, "-c", f"import os; open({str(target)!r}).close(); "
         "assert os.environ['LEAF_TEST_NEXT_SITE'] == '_leaf_next_sitecustomize'"],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    all_entries = entries(logs)
    assert not [entry for entry in all_entries if entry.get("k") == "x"]
    child_logs = []
    for log in logs.glob("*.jsonl"):
        records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        if records[0]["pid"] != os.getpid():
            child_logs.append(records)
    assert len(child_logs) == 1
    assert {"k": "f", "p": "child-input"} in child_logs[0]
    assert not list(logs.glob("*.error"))


def test_isolated_child_is_opaque_only_when_relevant(environment):
    root, logs = environment
    assert audit.install()
    subprocess.run([sys.executable, "-I", "-c", "pass"], cwd=root, check=True, timeout=20)
    subprocess.run([sys.executable, "-I", "-c", "pass"], cwd=root.parent, check=True, timeout=20)
    children = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert len(children) == 2
    assert children[0]["exe"] == "python"
    assert children[0]["cwd"] == "."
    assert children[1]["exe"] == "python"
    assert children[1]["cwd"] is None


@pytest.mark.parametrize("form", [str, os.fsencode, Path])
def test_popen_single_program_records_child(environment, form):
    root, logs = environment
    assert audit.install()
    subprocess.run(form("/bin/true"), cwd=root, check=True, timeout=20)
    assert [entry for entry in entries(logs) if entry.get("k") == "x"] == [
        {"k": "x", "exe": "true", "cwd": ".", "paths": [], "argv": ["true"]},
    ]
    assert not list(logs.glob("*.error"))


def test_popen_path_script_records_child(environment):
    root, logs = environment
    script = root / "root script"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    assert audit.install()
    with subprocess.Popen(args=script, shell=False, cwd=root) as child:
        assert child.wait(timeout=20) == 0
    assert [entry for entry in entries(logs) if entry.get("k") == "x"] == [
        {"k": "x", "exe": "root script", "cwd": ".", "paths": ["root", "root script"],
         "argv": ["root script"]},
    ]
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("executable", [None, "/bin/sh"])
def test_popen_shell_string_records_shell(environment, executable):
    root, logs = environment
    assert audit.install()
    subprocess.run("true", shell=True, executable=executable, cwd=root, check=True, timeout=20)
    assert [entry for entry in entries(logs) if entry.get("k") == "x"] == [
        {"k": "x", "exe": "sh", "cwd": ".", "paths": [], "argv": ["sh", "-c", "true"]},
    ]
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("case", [
    "temp-C", "sub-C", "repeat-C", "global-options", "git-dir-equals", "git-dir-pair",
    "work-tree-equals", "work-tree-pair", "env-git-dir", "env-work-tree", "bytes-env",
    "outside-with-root-git-dir", "outside-with-root-work-tree", "all-outside",
])
def test_git_effective_location(environment, monkeypatch, case):
    root, logs = environment
    sub = root / "sub"
    sub.mkdir()
    (sub / "input").write_text("payload", encoding="utf-8")
    (root / ".git").mkdir()
    temporary = root.parent / "fixture-repo"
    temporary.mkdir()
    (temporary / "CLAUDE.md").write_text("fixture", encoding="utf-8")

    def execute(self, args, executable=None, cwd=None, env=None, shell=False):
        return 42

    monkeypatch.setattr(subprocess.Popen, "_execute_child", execute)
    arguments, cwd, env = ["git"], root, {}
    expected_cwd, expected_paths, subcommand = ".", [], "log"
    if case == "temp-C":
        arguments += ["-C", str(temporary), "add", "CLAUDE.md"]
    elif case in {"sub-C", "repeat-C", "global-options"}:
        if case == "repeat-C":
            arguments += ["-C", str(temporary), "-C", "../root/sub"]
        else:
            arguments += ["-C", "sub"]
        if case == "global-options":
            arguments += ["-c", "core.pager=false", "--no-pager"]
        arguments += ["status", "input"]
        expected_cwd, expected_paths, subcommand = "sub", ["sub", "sub/input"], "status"
    elif case in {"git-dir-equals", "work-tree-equals"}:
        option = "--git-dir" if case == "git-dir-equals" else "--work-tree"
        arguments += [option + "=" + str(temporary / ".git"), "log"]
    elif case in {"git-dir-pair", "work-tree-pair"}:
        option = "--git-dir" if case == "git-dir-pair" else "--work-tree"
        arguments += [option, str(temporary), "log"]
    elif case in {"env-git-dir", "env-work-tree", "bytes-env"}:
        key = "GIT_WORK_TREE" if case == "env-work-tree" else "GIT_DIR"
        env = {key: str(temporary / ".git")}
        if case == "bytes-env":
            env = {os.fsencode(key): os.fsencode(value) for key, value in env.items()}
        arguments += ["log"]
    elif case == "outside-with-root-git-dir":
        cwd, env = temporary, {"GIT_DIR": str(root / ".git")}
        arguments += ["log"]
        expected_cwd = None
    elif case == "outside-with-root-work-tree":
        cwd = temporary
        arguments += ["--work-tree", "../root/sub", "status", "input"]
        expected_cwd, expected_paths, subcommand = None, ["sub"], "status"
    else:
        cwd, env = temporary, {"GIT_DIR": str(temporary / ".git")}
        arguments += ["--work-tree", str(temporary), "status"]
    assert audit.install()
    assert subprocess.Popen._execute_child(None, arguments, "git", cwd, env) == 42
    children = [entry for entry in entries(logs) if entry.get("k") == "x"]
    expected = [] if case in {"temp-C", "all-outside"} else [
        {"k": "x", "exe": "git", "cwd": expected_cwd, "sub": subcommand, "opts": [], "paths": expected_paths,
         "argv": audit._child_argv(arguments, str(cwd))},
    ]
    assert children == ([{**expected[0], "paths": ["."]}]
                        if case == "outside-with-root-git-dir" else expected)
    assert not list(logs.glob("*.error"))


def test_git_subcommands_do_not_deduplicate_each_other(environment, monkeypatch):
    root, logs = environment

    def execute(self, args, executable=None, cwd=None, env=None, shell=False):
        return 42

    monkeypatch.setattr(subprocess.Popen, "_execute_child", execute)
    assert audit.install()
    for sub in ("status", "log", "status"):
        assert subprocess.Popen._execute_child(None, ["git", sub], "git", root, {}) == 42
    assert [entry for entry in entries(logs) if entry.get("k") == "x"] == [
        {"k": "x", "exe": "git", "cwd": ".", "sub": "status", "opts": [], "paths": [],
         "argv": ["git", "status"]},
        {"k": "x", "exe": "git", "cwd": ".", "sub": "log", "opts": [], "paths": [],
         "argv": ["git", "log"]},
    ]
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("name", [
    "posix_spawn", "posix_spawnp", "execv", "execve", "execvp", "execvpe",
    "execl", "execle", "execlp", "execlpe", "spawnv", "spawnve", "spawnvp",
    "spawnvpe", "system", "_execute_child",
])
def test_opaque_entry_point_variants(environment, monkeypatch, name):
    root, logs = environment
    (root / "input").write_text("payload", encoding="utf-8")
    calls = []

    def original(*args, **kwargs):
        calls.append((args, kwargs))
        return 42

    def execute(self, args, executable=None, cwd=None, env=None, shell=False):
        return original(self, args, executable, cwd, env, shell)

    owner = subprocess.Popen if name == "_execute_child" else os
    monkeypatch.setattr(owner, name, execute if name == "_execute_child" else original)
    assert audit.install()
    if name == "_execute_child":
        result = subprocess.Popen._execute_child(None, ["git", "input"], "git", None, {})
    elif name == "system":
        result = os.system(b"git input")
    else:
        arguments = (("/bin/git", "git", "input") if name.startswith("execl") else
                     ("/bin/git", ["git", "input"]))
        if name.startswith("spawn"):
            arguments = (0, *arguments)
        if name.endswith("e") or name.startswith("posix_spawn"):
            arguments = (*arguments, {})
        result = getattr(os, name)(*arguments)
    assert result == 42
    assert len(calls) == 1
    children = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert len(children) == 1
    assert children[0] == ({"k": "x", "exe": "sh", "cwd": ".", "paths": [".", "input"],
                            "argv": ["sh", "-c", "git input"]}
                           if name == "system" else
                           {"k": "x", "exe": "git", "cwd": ".", "sub": "input", "opts": [], "paths": ["input"],
                            "argv": ["git", "root:/input"]})


@pytest.mark.parametrize("flag", ["-I", "-E", "-S", None])
def test_python_child_env_and_flags_determine_propagation(environment, flag):
    root, logs = environment
    assert audit.install()
    assert os.system(shlex.join([sys.executable, "-c", "pass"])) == 0
    subprocess.run([sys.executable, "-c", "pass"], check=True, timeout=20)
    assert [entry for entry in entries(logs) if entry.get("k") == "x"] == [
        {"k": "x", "exe": "sh", "cwd": ".", "paths": [],
         "argv": ["sh", "-c", "<ext>/" + os.path.basename(sys.executable) + " -c pass"]},
    ]
    if flag is not None:
        subprocess.run([sys.executable, flag, "-c", "pass"], check=True, timeout=20)
    else:
        subprocess.run([sys.executable, "-c", "pass"], env={}, check=True, timeout=20)
    children = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert children == [{"k": "x", "exe": "sh", "cwd": ".", "paths": [],
                         "argv": ["sh", "-c", "<ext>/" + os.path.basename(sys.executable) + " -c pass"]},
                        {"k": "x", "exe": "python", "cwd": ".", "paths": [],
                         "argv": ["python", *([flag] if flag else []), "-c", "pass"]}]


def test_sqlite_and_copy_sources(environment):
    root, logs = environment
    source = root / "source"
    source.write_text("source", encoding="utf-8")
    for name in ("mode-source", "stat-source"):
        (root / name).write_text("source", encoding="utf-8")
    (root / "tree-source").mkdir()
    (root / "tree-source" / "input").write_text("source", encoding="utf-8")
    assert audit.install()
    with sqlite3.connect(":memory:"):
        pass
    with sqlite3.connect("file:temporary?mode=memory&cache=shared", uri=True):
        pass
    with sqlite3.connect(str(root / "data.db")):
        pass
    shutil.copyfile(source, root / "destination")
    shutil.copymode("mode-source", "destination")
    shutil.copystat("stat-source", "destination")
    shutil.copytree("tree-source", "tree-dest")
    assert {"source", "data.db", "mode-source", "stat-source", "tree-source"} <= set(reads(logs, "f"))
    assert ":memory:" not in reads(logs, "f")
    assert not any("mode=memory" in path for path in reads(logs, "f"))


def test_overflow_once_then_stops(environment, monkeypatch):
    root, logs = environment
    monkeypatch.setattr(audit, "MAX_ENTRIES", 3)
    assert audit.install()
    for index in range(8):
        assert not os.path.exists(f"probe-{index}")
    records = entries(logs)
    assert [entry for entry in records if entry.get("k") == "e"] == [
        {"k": "e", "p": "probe-0"}, {"k": "e", "p": "probe-1"}, {"k": "e", "p": "probe-2"},
    ]
    assert records[-1] == {"k": "o"}
    assert len(records) == 5


def test_install_is_idempotent(environment):
    root, logs = environment
    assert audit.install() is True
    wrapped = os.stat, os.lstat, os.scandir, os.open
    pythonpath = os.environ["PYTHONPATH"]
    assert audit.install() is True
    assert (os.stat, os.lstat, os.scandir, os.open) == wrapped
    assert os.environ["PYTHONPATH"] == pythonpath
    assert len(list(logs.glob("*.jsonl"))) == 1


def test_two_module_names_share_one_install_and_trace_owner(environment):
    root, logs = environment
    (root / "input").write_text("payload", encoding="utf-8")
    code = (
        "import importlib.util, os, sys\n"
        "original = [os.stat]\n"
        "modules = []\n"
        "for name in ('_leaf_read_audit', 'read_audit'):\n"
        f"    spec = importlib.util.spec_from_file_location(name, {str(MODULE)!r})\n"
        "    module = importlib.util.module_from_spec(spec)\n"
        "    sys.modules[name] = module\n"
        "    spec.loader.exec_module(module)\n"
        "    assert module.install()\n"
        "    modules.append(module)\n"
        "assert modules[0] is not modules[1]\n"
        "assert sys._leaf_read_audit_owner is modules[0]\n"
        "assert modules[1]._state is None\n"
        "assert os.stat.__wrapped__ is original[0]\n"
        "modules[1].pytest_configure(None)\n"
        "os.environ['PYTEST_CURRENT_TEST'] = 'test_owner.py::test_read (call)'\n"
        "with open('input') as stream:\n"
        "    assert stream.read() == 'payload'\n"
        "del os.environ['PYTEST_CURRENT_TEST']\n"
    )
    env = {**os.environ, "LEAF_READ_AUDIT_TRACE": "1"}
    result = subprocess.run([sys.executable, "-c", code], cwd=root, env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ""
    assert len(list(logs.glob("*.jsonl"))) == 1
    assert reads(logs, "f") == ["input"]
    sidecar, = logs.glob("*.trace")
    assert [json.loads(line) for line in sidecar.read_text().splitlines()] == [
        {"k": "f", "p": "input", "t": "test_owner.py::test_read"},
    ]
    assert not list(logs.glob("*.error"))


def test_captured_open_is_rebound(environment, monkeypatch):
    root, logs = environment
    (root / "input").write_text("payload", encoding="utf-8")
    captured = types.ModuleType("leaf_test_captured_open")
    captured._open = builtins.open
    monkeypatch.setitem(sys.modules, captured.__name__, captured)
    original = captured._open
    assert audit.install()
    assert captured._open is builtins.open
    assert builtins.open is audit.io.open is audit._io.open
    assert audit.io.open_code is audit._io.open_code
    assert captured._open.__wrapped__ is original
    with captured._open(root / "input") as stream:
        assert stream.read() == "payload"
    assert reads(logs, "f") == ["input"]
    assert not list(logs.glob("*.error"))


def test_rebinding_failure_writes_marker_and_continues(environment, monkeypatch):
    root, logs = environment

    class RefusesRebinding(types.ModuleType):
        def __setattr__(self, name, value):
            if name == "_open" and hasattr(self, name):
                raise RuntimeError("captured reference is read-only")
            super().__setattr__(name, value)

    broken = RefusesRebinding("leaf_test_broken_rebinding")
    broken._open = builtins.open
    captured = types.ModuleType("leaf_test_later_rebinding")
    captured._open = builtins.open
    monkeypatch.setitem(sys.modules, broken.__name__, broken)
    monkeypatch.setitem(sys.modules, captured.__name__, captured)
    assert audit.install()
    assert captured._open is builtins.open
    marker, = logs.glob("*.error")
    assert marker.read_text().splitlines()[0] == "RuntimeError: captured reference is read-only"


def test_xdist_sitecustomize_threads_do_not_deadlock(environment):
    pytest.importorskip("xdist", reason="pytest-xdist is not installed in leaf-web-demo CI")
    root, logs = environment
    (root / "input").write_text("payload", encoding="utf-8")
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    test_file = root / "test_threads.py"
    test_file.write_text(
        "from concurrent.futures import ThreadPoolExecutor\n"
        "from pathlib import Path\n"
        "from threading import Barrier\n"
        "import os, sys\n"
        "import pytest\n"
        "@pytest.mark.parametrize('case', range(2))\n"
        "def test_threads(case):\n"
        "    assert sys.modules['_leaf_read_audit'] is sys.modules['read_audit']\n"
        "    assert sys._leaf_read_audit_owner is sys.modules['read_audit']\n"
        "    assert not hasattr(os.stat.__wrapped__, '__wrapped__')\n"
        "    root = Path.cwd()\n"
        "    barrier = Barrier(8)\n"
        "    def work(index):\n"
        "        barrier.wait(timeout=20)\n"
        "        for repeat in range(50):\n"
        "            directory = root / ('thread-%s-%s' % (case, index))\n"
        "            directory.mkdir(exist_ok=True)\n"
        "            assert directory.is_dir()\n"
        "            with open(root / 'input') as stream:\n"
        "                assert stream.read() == 'payload'\n"
        "    with ThreadPoolExecutor(max_workers=8) as pool:\n"
        "        list(pool.map(work, range(8)))\n", encoding="utf-8")
    audit._site(str(logs))
    env = {**os.environ, "LEAF_READ_AUDIT_TRACE": "1",
           "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": ""}
    # Child startup reads must not inherit the outer pytest test's attribution.
    env.pop("PYTEST_CURRENT_TEST", None)
    started = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "read_audit", "-p", "xdist.plugin",
         "-n", "2", "-q", "-p", "no:cacheprovider", str(test_file)],
        cwd=root, env=env, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert time.monotonic() - started < 60, result.stdout + result.stderr
    assert "2 passed" in result.stdout
    assert not list(logs.glob(f"{os.getpid()}-*.jsonl")), "the parent audit leaked into this test"
    assert not list(logs.glob(f"{os.getpid()}-*.trace")), "the parent audit leaked into this test"
    per_pid = {}
    for path in logs.glob("*.jsonl"):
        records = [json.loads(line) for line in path.read_text().splitlines()]
        pid = records[0]["pid"]
        assert pid not in per_pid, "a process installed the audit twice"
        per_pid[pid] = records
    assert len(per_pid) >= 3
    assert sum({"k": "f", "p": "input"} in records for records in per_pid.values()) == 2
    traces = [json.loads(line) for pid in per_pid for path in logs.glob(f"{pid}-*.trace")
              for line in path.read_text().splitlines()]
    assert {entry["t"] for entry in traces
            if entry.get("k") == "f" and entry.get("p") == "input"} == {
        "test_threads.py::test_threads[0]", "test_threads.py::test_threads[1]",
    }
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("launcher", ["subprocess", "system", "posix_spawn"])
def test_real_isolated_launchers_are_opaque(environment, launcher):
    root, logs = environment
    assert audit.install()
    command = [sys.executable, "-I", "-c", "pass"]
    if launcher == "subprocess":
        subprocess.run(command, check=True, timeout=20)
    elif launcher == "system":
        assert os.system(shlex.join(command)) == 0
    else:
        pid = os.posix_spawn(sys.executable, command, os.environ)
        assert os.waitpid(pid, 0) == (pid, 0)
    children = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert children == [{"k": "x", "exe": "sh" if launcher == "system" else "python",
                         "cwd": ".", "paths": [],
                         "argv": (["sh", "-c", "<ext>/" + os.path.basename(sys.executable) + " -I -c pass"]
                                  if launcher == "system" else ["python", *command[1:]])}]
    assert len(list(logs.glob("*.jsonl"))) == 1
    assert not list(logs.glob("*.error"))


def test_failures_never_change_probe_result_and_error_is_written_once(environment, monkeypatch):
    root, logs = environment
    assert audit.install()

    def broken(*args):
        raise RuntimeError("audit failure")

    monkeypatch.setattr(audit, "_relative", broken)
    assert os.path.exists("missing") is False
    assert os.path.exists("also-missing") is False
    marker = logs / f"{os.getpid()}.error"
    assert marker.read_text(encoding="utf-8").splitlines()[0] == "RuntimeError: audit failure"
    assert len(list(logs.glob("*.error"))) == 1


def test_relative_cache_stops_growing_and_respects_cwd(environment, monkeypatch):
    root, logs = environment
    (root / "nested").mkdir()
    monkeypatch.setattr(audit, "CACHE_MAX", 2)
    assert audit.install()
    assert not os.path.exists("same")
    monkeypatch.chdir(root / "nested")
    assert not os.path.exists("same")
    assert not os.path.exists("third")
    assert len(audit._state["cache"]) == 2
    assert {"same", "nested/same", "nested/third"} <= set(reads(logs, "e"))


def _fork_read(path):
    with open(path):
        pass


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
@pytest.mark.parametrize("method", ["os.fork", "multiprocessing"])
def test_fork_child_has_its_own_log(environment, method):
    root, logs = environment
    target = root / "fork-input"
    target.write_text("payload", encoding="utf-8")
    assert audit.install()
    # The child must reset the parent's seen set as well as its pid and fd.
    _fork_read(target)
    if method == "os.fork":
        pid = os.fork()
        if pid == 0:
            try:
                _fork_read(target)
            except BaseException:
                os._exit(1)
            os._exit(0)
        assert os.waitpid(pid, 0) == (pid, 0)
    else:
        child = multiprocessing.get_context("fork").Process(target=_fork_read, args=(target,))
        child.start()
        pid = child.pid
        child.join(timeout=20)
        if child.is_alive():
            child.terminate()
            child.join(timeout=20)
            pytest.fail("fork child did not finish")
        assert child.exitcode == 0
        child.close()
    child_logs = list(logs.glob(f"{pid}-*.jsonl"))
    assert len(child_logs) == 1
    records = [json.loads(line) for line in child_logs[0].read_text(encoding="utf-8").splitlines()]
    assert records[0] == {"v": 1, "pid": pid, "argv": sys.argv[:3]}
    assert {"k": "f", "p": "fork-input"} in records
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("path,expected", [
    ("__pycache__/module.cpython-312.pyc", "module.py"),
    ("package/__pycache__/module.cpython-312.opt-1.pyc", "package/module.py"),
    ("package/__pycache__/module.cpython-312.opt-2.pyc", "package/module.py"),
    ("__pycache__/module.pyc", None),
    ("__pycache__/module.cpython-312.pyo", None),
    ("__pycache__/nested/module.cpython-312.pyc", None),
    (".git/__pycache__/module.cpython-312.pyc", None),
    ("package/module.pyc", "package/module.pyc"),
])
def test_cached_bytecode_maps_to_source(environment, path, expected):
    assert audit.install()
    assert audit._relative(path) == expected
    assert audit._relative(path) == expected


def test_cached_import_records_source_in_child_log(environment):
    root, logs = environment
    source = root / "cached_input.py"
    source.write_text("VALUE = 42\n", encoding="utf-8")
    cached = py_compile.compile(str(source), doraise=True)
    assert audit.install()
    result = subprocess.run(
        [sys.executable, "-c", "import os, cached_input; "
         "assert cached_input.VALUE == 42; "
         f"assert cached_input.__cached__ == {cached!r}; print(os.getpid())"],
        cwd=root, capture_output=True, text=True, check=True, timeout=20,
    )
    pid = int(result.stdout.strip())
    child_logs = list(logs.glob(f"{pid}-*.jsonl"))
    assert len(child_logs) == 1
    records = [json.loads(line) for line in child_logs[0].read_text(encoding="utf-8").splitlines()]
    assert records[0]["pid"] == pid
    assert {"k": "f", "p": "cached_input.py"} in records
    assert not list(logs.glob("*.error"))


def test_source_then_bytecode_import_both_record_source(environment):
    root, logs = environment
    source = root / "mod.py"
    source.write_text("VALUE = 42\n", encoding="utf-8")
    assert audit.install()
    env = dict(os.environ)
    env.pop("PYTHONDONTWRITEBYTECODE", None)
    cached = Path(importlib.util.cache_from_source(str(source)))
    assert not cached.exists()
    for iteration in range(2):
        result = subprocess.run(
            [sys.executable, "-c", "import os, mod; assert mod.VALUE == 42; print(os.getpid())"],
            cwd=root, env=env, capture_output=True, text=True, timeout=20,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stderr == ""
        pid = int(result.stdout.strip())
        path, = logs.glob(f"{pid}-*.jsonl")
        records = [json.loads(line) for line in path.read_text().splitlines()]
        assert records.count({"k": "f", "p": "mod.py"}) == 1
        assert cached.is_file()
        if iteration == 0:
            cache_contents = cached.read_bytes()
            cache_mtime = cached.stat().st_mtime_ns
        else:
            assert cached.read_bytes() == cache_contents
            assert cached.stat().st_mtime_ns == cache_mtime
    assert not list(logs.glob("*.error"))


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires /proc/self/fd")
@pytest.mark.parametrize("operation", ["stat", "lstat", "open"])
@pytest.mark.parametrize("inside", [False, True])
def test_dir_fd_resolves_against_directory(environment, operation, inside):
    root, logs = environment
    directory = (root if inside else root.parent) / "nested"
    directory.mkdir()
    (directory / "x").write_text("payload", encoding="utf-8")
    fd = os.open(directory, os.O_RDONLY)
    try:
        assert audit.install()
        if operation == "open":
            opened = os.open("x", os.O_RDONLY, dir_fd=fd)
            os.close(opened)
        else:
            getattr(os, operation)("x", dir_fd=fd)
    finally:
        os.close(fd)
    assert reads(logs, "f") == (["nested/x"] if inside else [])


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires /proc/self/fd")
@pytest.mark.parametrize("operation", ["stat", "lstat", "open"])
def test_unresolvable_dir_fd_records_nothing(environment, monkeypatch, operation):
    root, logs = environment
    (root / "x").write_text("payload", encoding="utf-8")
    fd = os.open(root, os.O_RDONLY)

    def unavailable(*args):
        raise OSError("fd lookup unavailable")

    try:
        assert audit.install()
        monkeypatch.setattr(os, "readlink", unavailable)
        if operation == "open":
            os.close(os.open("x", os.O_RDONLY, dir_fd=fd))
        else:
            getattr(os, operation)("x", dir_fd=fd)
    finally:
        os.close(fd)
    assert reads(logs, "f") == []
    assert not list(logs.glob("*.error"))


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="requires /proc/self/fd")
def test_rmtree_outside_root_records_nothing(environment):
    root, logs = environment
    tree = root.parent / "temporary-tree"
    (tree / "nested").mkdir(parents=True)
    (tree / "nested" / "x").write_text("payload", encoding="utf-8")
    assert audit.install()
    shutil.rmtree(tree)
    assert reads(logs, "f") == []
    assert reads(logs, "d") == []
    assert not list(logs.glob("*.error"))


def test_opaque_child_arguments_only_include_existing_root_paths(environment):
    root, logs = environment
    target = root / "child-input"
    target.write_text("payload", encoding="utf-8")
    assert audit.install()
    subprocess.run([sys.executable, "-I", "-c", "pass", str(target)],
                   cwd=root, check=True, timeout=20)
    children = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert children == [{"k": "x", "exe": "python",
                         "cwd": ".", "paths": ["child-input"],
                         "argv": ["python", "-I", "-c", "pass", "root:/child-input"]}]
    assert reads(logs, "f") == []


@pytest.mark.parametrize("shell,expected", [(False, "git"), (True, "sh")])
def test_opaque_child_without_program_uses_first_argument(environment, monkeypatch, shell, expected):
    root, logs = environment
    (root / "input").write_text("payload", encoding="utf-8")

    def execute(self, args, executable=None, cwd=None, env=None, shell=False):
        return 42

    monkeypatch.setattr(subprocess.Popen, "_execute_child", execute)
    assert audit.install()
    assert subprocess.Popen._execute_child(
        None, ["git", "input", "missing", "--flag"], None, None, {}, shell) == 42
    children = [entry for entry in entries(logs) if entry.get("k") == "x"]
    child = {"k": "x", "exe": expected, "cwd": ".", "paths": [".", "input"] if shell else ["input"],
             "argv": [*(["sh", "-c"] if shell else []), "git", "root:/input", "missing", "--flag"]}
    if not shell:
        child["sub"] = "input"
        child["opts"] = ["--flag"]
    assert children == [child]
    assert reads(logs, "f") == []
    assert not list(logs.glob("*.error"))


def test_repeated_entries_are_not_serialized(environment, monkeypatch):
    root, logs = environment

    def execute(self, args, executable=None, cwd=None, env=None, shell=False):
        return 42

    monkeypatch.setattr(subprocess.Popen, "_execute_child", execute)
    assert audit.install()
    original = audit.json.dumps
    serialized = []

    def counting(entry, *args, **kwargs):
        serialized.append(entry)
        return original(entry, *args, **kwargs)

    monkeypatch.setattr(audit.json, "dumps", counting)
    for _ in range(3):
        assert not os.path.exists("missing")
        os.listdir(root)
        assert subprocess.Popen._execute_child(None, ["git", "--version"], "git", None, {}) == 42
    assert serialized == [{"k": "e", "p": "missing"}, {"k": "d", "p": "."},
                          {"k": "x", "exe": "git", "cwd": ".", "sub": None, "opts": [], "paths": [],
                           "argv": ["git", "--version"]}]


@pytest.mark.parametrize("explicit_env", [False, True])
def test_python_child_replacing_pythonpath_is_opaque(environment, monkeypatch, explicit_env):
    root, logs = environment
    target = root / "unaudited-child-input"
    target.write_text("payload", encoding="utf-8")
    assert audit.install()
    if explicit_env:
        child_env = {**os.environ, "PYTHONPATH": str(root)}
    else:
        monkeypatch.setenv("PYTHONPATH", str(root.parent))
        child_env = None
    subprocess.run([sys.executable, "-c", f"open({str(target)!r}).close()"],
                   cwd=root, env=child_env, check=True, timeout=20)
    children = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert children == [{"k": "x", "exe": "python",
                         "cwd": ".", "paths": ["unaudited-child-input"],
                         "argv": ["python", "-c", "open('root:/unaudited-child-input').close()"]}]
    for log in logs.glob("*.jsonl"):
        records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        if records[0]["pid"] != os.getpid():
            assert {"k": "f", "p": target.name} not in records
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("bytes_env", [False, True])
@pytest.mark.parametrize("missing", [None, "LEAF_READ_AUDIT_DIR", "LEAF_READ_AUDIT_ROOT", "PYTHONPATH"])
def test_python_child_requires_complete_hook_environment(environment, monkeypatch, bytes_env, missing):
    root, logs = environment

    def execute(self, args, executable=None, cwd=None, env=None, shell=False):
        return 42

    monkeypatch.setattr(subprocess.Popen, "_execute_child", execute)
    assert audit.install()
    child_env = dict(os.environ)
    # Normalized entries still identify the installed hook directory.
    child_env["PYTHONPATH"] = os.path.join(audit._state["site"], ".")
    if missing is not None:
        del child_env[missing]
    if bytes_env:
        child_env = {os.fsencode(key): os.fsencode(value) for key, value in child_env.items()}
    assert subprocess.Popen._execute_child(None, ["python", "-c", "pass"], "python", None, child_env) == 42
    children = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert children == ([] if missing is None else
                        [{"k": "x", "exe": "python", "cwd": ".", "paths": [],
                          "argv": ["python", "-c", "pass"]}])


@pytest.mark.parametrize("options,isolated", [
    (["-Ic", "pass"], True),
    (["-Ec", "pass"], True),
    (["-Sc", "pass"], True),
    (["-uI", "-c", "pass"], True),
    (["-u", "-c", "pass"], False),
    (["-W", "ignore", "-c", "pass"], False),
])
def test_python_child_combined_options(environment, options, isolated):
    root, logs = environment
    assert audit.install()
    subprocess.run([sys.executable, *options], cwd=root, check=True, timeout=20)
    children = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert children == ([{"k": "x", "exe": "python",
                          "cwd": ".", "paths": [], "argv": ["python", *options]}] if isolated else [])
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("options,isolated", [
    (["--", "-I"], False),
    (["-", "-I"], False),
    (["script.py", "-I"], False),
    (["--long-I", "-c", "pass"], False),
    (["--long-option", "-I"], True),
    (["-cI", "pass"], False),
    (["-m", "module", "-I"], False),
    (["-uIc", "pass"], True),
    (["-Wignore", "-I"], True),
    (["-WI", "-c", "pass"], False),
    (["-W", "-I", "-c", "pass"], False),
    (["-X", "value", "-E"], True),
    (["-XI", "-c", "pass"], False),
    (["-uX", "-I", "-S"], True),
])
def test_python_option_scan_stops_and_consumes_values(environment, monkeypatch, options, isolated):
    root, logs = environment

    def execute(self, args, executable=None, cwd=None, env=None, shell=False):
        return 42

    monkeypatch.setattr(subprocess.Popen, "_execute_child", execute)
    assert audit.install()
    assert subprocess.Popen._execute_child(None, ["python", *options], "python", None, None) == 42
    children = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert children == ([{"k": "x", "exe": "python", "cwd": ".", "paths": [], "argv": ["python", *options]}]
                        if isolated else [])


def test_access_and_readlink_record_paths_and_preserve_capabilities(environment):
    root, logs = environment
    target = root / "probe-input"
    target.write_text("payload", encoding="utf-8")
    link = root / "probe-link"
    link.symlink_to(target)
    support_sets = (os.supports_fd, os.supports_dir_fd, os.supports_follow_symlinks,
                    os.supports_effective_ids)
    before = {name: [getattr(os, name) in support for support in support_sets]
              for name in ("access", "readlink")}
    assert audit.install()
    assert os.access(target, os.R_OK)
    assert os.readlink(link) == str(target)
    assert set(reads(logs, "f")) == {"probe-input", "probe-link"}
    for name, memberships in before.items():
        assert [getattr(os, name) in support for support in support_sets] == memberships
        with pytest.raises(TypeError):
            if name == "access":
                os.access(12345, os.R_OK)
            else:
                os.readlink(12345)
    assert set(reads(logs, "f")) == {"probe-input", "probe-link"}
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("operation", ["access", "readlink"])
@pytest.mark.parametrize("inside", [False, True])
def test_access_and_readlink_resolve_dir_fd(environment, operation, inside):
    root, logs = environment
    directory = (root if inside else root.parent) / "probe-directory"
    directory.mkdir()
    (directory / "target").write_text("payload", encoding="utf-8")
    (directory / "link").symlink_to("target")
    fd = os.open(directory, os.O_RDONLY)
    try:
        assert audit.install()
        if operation == "access":
            assert os.access("link", os.R_OK, dir_fd=fd)
        else:
            assert os.readlink("link", dir_fd=fd) == "target"
    finally:
        os.close(fd)
    assert set(reads(logs, "f")) == ({"probe-directory/link", "probe-directory/target"}
                                    if inside else set())
    assert not list(logs.glob("*.error"))


def test_open_symlink_records_link_and_target_without_internal_probes(environment):
    root, logs = environment
    target = root / "link-target"
    target.write_text("payload", encoding="utf-8")
    link = root / "input-link"
    link.symlink_to(target)
    assert audit.install()
    for _ in range(2):
        with link.open() as stream:
            assert stream.read() == "payload"
    assert reads(logs, "f") == ["input-link", "link-target"]
    assert audit._state["cache"][str(link)] == (("input-link", "link-target"), True)
    assert not list(logs.glob("*.error"))


def test_symlink_root_records_reads_by_real_path(environment, monkeypatch):
    root, logs = environment
    target = root / "real-input"
    target.write_text("payload", encoding="utf-8")
    alias = root.parent / "root-alias"
    alias.symlink_to(root, target_is_directory=True)
    real_root = str(root.resolve())
    real_target = target.resolve()
    monkeypatch.setenv(audit.ENV_ROOT, str(alias))
    assert audit.install()
    with real_target.open() as stream:
        assert stream.read() == "payload"
    assert audit._state["real_root"] == real_root
    assert reads(logs, "f") == ["real-input"]
    assert not list(logs.glob("*.error"))


@pytest.fixture(autouse=True)
def isolated_temp_roots(tmp_path, monkeypatch):
    # These repositories are inputs; only the separate scratch directory is temporary.
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(audit.tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("TMPDIR", str(scratch))
    monkeypatch.delenv("TEMP", raising=False)
    monkeypatch.delenv("TMP", raising=False)
    monkeypatch.delenv("LEAF_READ_AUDIT_TRACE", raising=False)


def test_id_and_deepcopy_never_enter_guard(environment, monkeypatch):
    nested = [[index, {"value": [index]}] for index in range(10_000)]

    def forbidden(*args):
        raise AssertionError("install must not add an audit hook")

    monkeypatch.setattr(sys, "addaudithook", forbidden)
    assert audit.install()
    calls = []
    monkeypatch.setattr(audit, "_guard", lambda *args: calls.append(args))
    for value in nested:
        id(value)
    assert copy.deepcopy(nested) == nested
    assert calls == []


@pytest.mark.parametrize("encode", [str, os.fsencode])
def test_absolute_stat_fast_path_and_relative_paths_after_chdir(environment, monkeypatch, encode):
    root, logs = environment
    (root / "nested").mkdir()
    (root / "input").write_text("root", encoding="utf-8")
    (root / "nested" / "input").write_text("nested", encoding="utf-8")
    assert audit.install()
    original = audit._guard
    calls = []

    def counting(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(audit, "_guard", counting)
    target = encode(root / "input")
    os.stat(target)
    first = len(calls)
    assert first > 0
    os.stat(target)
    assert len(calls) == first
    assert ("f", target) in audit._fast
    os.stat("input")
    os.chdir(root / "nested")
    os.stat("input")
    assert set(reads(logs, "f")) == {"input", "nested/input"}
    assert ("f", "input") not in audit._fast


def test_path_exists_repeats_enter_guard_once(environment, monkeypatch):
    root, logs = environment
    target = root / "input"
    target.write_text("payload", encoding="utf-8")
    assert audit.install()
    calls = []
    original = audit._guard

    def counting(*args):
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(audit, "_guard", counting)
    for _ in range(1000):
        assert Path(target).exists()
    assert len(calls) == 1
    assert reads(logs, "e") == ["input"]
    assert reads(logs, "f") == []
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("encode", [str, os.fsencode, Path])
@pytest.mark.parametrize("change", ["chdir", "fchdir"])
def test_relative_stat_fast_keys_follow_cwd_epoch(environment, monkeypatch, encode, change):
    root, logs = environment
    directories = [root / name for name in ("a", "b")]
    for directory in directories:
        directory.mkdir()
        (directory / "x").write_text(directory.name, encoding="utf-8")
    descriptors = [os.open(directory, os.O_RDONLY) for directory in directories]
    try:
        assert audit.install()
        original = audit._guard
        calls = []

        def counting(*args):
            calls.append(args)
            return original(*args)

        monkeypatch.setattr(audit, "_guard", counting)
        for index, directory in enumerate(directories):
            epoch = audit._cwd_epoch
            getattr(os, change)(directory if change == "chdir" else descriptors[index])
            assert audit._cwd_epoch == epoch + 2
            assert audit._fast == set()
            for _ in range(1000):
                assert os.stat(encode("x")).st_size == 1
            assert len(calls) == index + 1
            assert ("f", os.fspath(encode("x")), audit._cwd_epoch) in audit._fast
        assert set(reads(logs, "f")) == {"a/x", "b/x"}
        audit._after_fork()
        assert audit._cwd_epoch == 0
        assert audit._fast == set()
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
    assert not list(logs.glob("*.error"))


def test_fast_cache_is_bounded_and_reset_after_fork(environment, monkeypatch):
    root, logs = environment
    monkeypatch.setattr(audit, "CACHE_MAX", 2)
    assert audit.install()
    for index in range(4):
        assert not os.path.exists(str(root / f"missing-{index}"))
    assert len(audit._fast) == 2
    # Exercise the child reset directly without needing a second process.
    audit._after_fork()
    assert audit._fast == set()
    assert not os.path.exists(str(root / "missing-0"))
    assert ("e", str(root / "missing-0")) in audit._fast


@pytest.mark.parametrize("location", ["parent", "root"])
def test_temp_root_containing_checkout_does_not_hide_inputs(environment, monkeypatch, location):
    root, logs = environment
    source = root / "input"
    source.write_text("source", encoding="utf-8")
    monkeypatch.setenv("TMPDIR", str(root.parent if location == "parent" else root))
    assert audit.install()
    with source.open() as stream:
        assert stream.read() == "source"
    assert reads(logs, "f") == ["input"]


@pytest.mark.parametrize("variable", [None, "TMPDIR", "TEMP", "TMP"])
def test_temp_directory_inside_root_is_not_an_input(environment, monkeypatch, variable):
    root, logs = environment
    scratch = root / "scratch"
    scratch.mkdir()
    temporary = scratch / "input"
    temporary.write_text("scratch", encoding="utf-8")
    source = root / "input"
    source.write_text("source", encoding="utf-8")
    if variable is None:
        monkeypatch.setattr(audit.tempfile, "tempdir", str(scratch))
    else:
        monkeypatch.setenv(variable, str(scratch))
    assert audit.install()
    with temporary.open() as stream:
        assert stream.read() == "scratch"
    with source.open() as stream:
        assert stream.read() == "source"
    assert reads(logs, "f") == ["input"]


def test_temp_roots_include_real_paths_and_audit_directory(environment, monkeypatch):
    root, logs = environment
    scratch = root / "scratch"
    scratch.mkdir()
    temporary = scratch / "input"
    temporary.write_text("scratch", encoding="utf-8")
    alias = root.parent / "scratch-alias"
    alias.symlink_to(scratch, target_is_directory=True)
    monkeypatch.setenv("TMPDIR", str(alias))
    inside_logs = root / "audit"
    monkeypatch.setenv(audit.ENV_DIR, str(inside_logs))
    assert audit.install()
    with temporary.open():
        pass
    with (alias / "input").open():
        pass
    (inside_logs / "scratch").write_text("scratch", encoding="utf-8")
    assert reads(inside_logs, "f") == []


def test_wrapper_error_has_message_and_bounded_traceback(environment, monkeypatch):
    root, logs = environment
    assert audit.install()

    def broken(*args):
        raise ValueError("forced wrapper failure")

    assert not list(logs.glob("*.error"))
    monkeypatch.setattr(audit, "_path", broken)
    with pytest.raises(FileNotFoundError):
        open("input")
    marker = logs / f"{os.getpid()}.error"
    content = marker.read_bytes()
    lines = content.decode("utf-8").splitlines()
    assert lines[0] == "ValueError: forced wrapper failure"
    assert any("broken" in line for line in lines[1:])
    assert len(lines[1:]) <= 20
    assert len(content) <= 4096
    audit._local.busy = True
    try:
        audit._error(str(logs), RuntimeError("must not replace the first error"))
    finally:
        audit._local.busy = False
    assert marker.read_bytes() == content


def test_error_marker_caps_large_messages(environment):
    root, logs = environment
    try:
        raise RuntimeError("x" * 8000)
    except RuntimeError as exc:
        audit._error(str(logs), exc)
    content = (logs / f"{os.getpid()}.error").read_bytes()
    assert content.startswith(b"RuntimeError: ")
    assert len(content) <= 4096


def test_trace_nodeids_and_main_log_are_independent(environment):
    root, logs = environment
    target = root / "input"
    target.write_text("payload", encoding="utf-8")
    code = (
        "import importlib.util, os, sys\n"
        f"spec = importlib.util.spec_from_file_location('audit', {str(MODULE)!r})\n"
        "audit = importlib.util.module_from_spec(spec)\n"
        "sys.modules[spec.name] = audit\n"
        "spec.loader.exec_module(audit)\n"
        "assert audit.install()\n"
        "for nodeid in ('test_input.py::test_first', 'test_input.py::test_second'):\n"
        "    os.environ['PYTEST_CURRENT_TEST'] = nodeid + ' (setup)'\n"
        f"    os.stat({str(target)!r})\n"
        "    os.environ['PYTEST_CURRENT_TEST'] = nodeid + ' (call)'\n"
        f"    os.stat({str(target)!r})\n"
        "    os.environ['PYTEST_CURRENT_TEST'] = nodeid + ' (teardown)'\n"
        f"    open({str(target)!r}).close()\n"
        "del os.environ['PYTEST_CURRENT_TEST']\n"
    )
    main_logs = []
    for tracing in (False, True):
        directory = logs / ("trace" if tracing else "plain")
        env = {**os.environ, audit.ENV_DIR: str(directory),
               "LEAF_READ_AUDIT_TRACE": "1" if tracing else "0"}
        result = subprocess.run([sys.executable, "-c", code], env=env,
                                capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        path, = directory.glob("*.jsonl")
        main_logs.append(path.read_bytes().splitlines()[1:])
        if tracing:
            sidecar, = directory.glob("*.trace")
            records = [json.loads(line) for line in sidecar.read_text().splitlines()]
            assert records == [
                {"t": "test_input.py::test_first", "k": "f", "p": "input"},
                {"t": "test_input.py::test_second", "k": "f", "p": "input"},
            ]
        else:
            assert not list(directory.glob("*.trace"))
    assert main_logs[0] == main_logs[1] == [b'{"k": "f", "p": "input"}']


def test_trace_overflow_and_failure_do_not_affect_main_log(environment, monkeypatch):
    root, logs = environment
    monkeypatch.setenv("LEAF_READ_AUDIT_TRACE", "1")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(audit, "TRACE_MAX", 2)
    assert audit.install()
    for name in ("first", "second", "third", "fourth"):
        assert not os.path.exists(name)
    sidecar, = logs.glob("*.trace")
    records = [json.loads(line) for line in sidecar.read_text().splitlines()]
    assert records == [{"t": None, "k": "e", "p": "first"},
                       {"t": None, "k": "e", "p": "second"}, {"t": None, "k": "o"}]
    assert reads(logs, "e") == ["first", "second", "third", "fourth"]
    assert not list(logs.glob("*.error"))


def test_trace_write_failure_never_writes_error_marker(environment, monkeypatch):
    root, logs = environment
    monkeypatch.setenv("LEAF_READ_AUDIT_TRACE", "1")
    assert audit.install()
    audit._state["trace_path"] = str(logs / "absent-directory" / "output.trace")
    assert not os.path.exists("input")
    assert reads(logs, "e") == ["input"]
    assert audit._state["trace"] is False
    assert not list(logs.glob("*.error"))


def test_trace_failure_does_not_disable_fork_child(environment, monkeypatch):
    root, logs = environment
    monkeypatch.setenv("LEAF_READ_AUDIT_TRACE", "1")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert audit.install()
    audit._state["trace_path"] = str(logs / "absent-directory" / "output.trace")
    assert not os.path.exists("before")
    assert audit._state["trace"] is False
    audit._after_fork()
    assert audit._state["trace"] is True
    assert not os.path.exists("after")
    sidecar, = logs.glob("*.trace")
    assert [json.loads(line) for line in sidecar.read_text().splitlines()] == [
        {"t": None, "k": "e", "p": "after"},
    ]
    assert sorted(reads(logs, "e")) == ["after", "before"]
    assert not list(logs.glob("*.error"))


def test_trace_opens_once_for_fifty_distinct_reads(environment, monkeypatch):
    root, logs = environment
    for index in range(50):
        (root / f"input-{index}").write_text("payload", encoding="utf-8")
    monkeypatch.setenv("LEAF_READ_AUDIT_TRACE", "1")
    assert audit.install()
    assert audit._state["trace_fd"] is None
    original = os.open
    opened = []

    def counting(path, *args, **kwargs):
        if os.fspath(path) == audit._state["trace_path"]:
            opened.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(os, "open", counting)
    for index in range(50):
        with open(root / f"input-{index}") as stream:
            assert stream.read() == "payload"
    assert opened == [audit._state["trace_path"]]
    sidecar, = logs.glob("*.trace")
    records = [json.loads(line) for line in sidecar.read_text().splitlines()]
    assert len(records) == 50
    assert len(reads(logs, "f")) == 50
    fd = audit._state["trace_fd"]
    audit._close_trace()
    assert audit._state["trace_fd"] is None
    with pytest.raises(OSError):
        os.fstat(fd)
    assert not list(logs.glob("*.error"))


def test_fork_closes_inherited_trace_and_opens_new_sidecar(environment, monkeypatch):
    root, logs = environment
    monkeypatch.setenv("LEAF_READ_AUDIT_TRACE", "1")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    assert audit.install()
    assert not os.path.exists("before")
    previous_path = audit._state["trace_path"]
    previous_fd = audit._state["trace_fd"]
    close = os.close
    closed = []

    def closing(fd):
        closed.append(fd)
        return close(fd)

    monkeypatch.setattr(os, "close", closing)
    audit._after_fork()
    assert audit._state["trace_fd"] is None
    assert previous_fd in closed
    assert audit._state["trace_path"] != previous_path
    assert not os.path.exists("after")
    records = [[json.loads(line) for line in path.read_text().splitlines()]
               for path in logs.glob("*.trace")]
    assert sorted(records, key=lambda rows: rows[0]["p"]) == [
        [{"t": None, "k": "e", "p": "after"}],
        [{"t": None, "k": "e", "p": "before"}],
    ]
    assert not list(logs.glob("*.error"))


def test_dir_fd_never_uses_absolute_fast_cache(environment, monkeypatch):
    root, logs = environment
    target = str(root / "input")
    (root / "input").write_text("payload", encoding="utf-8")
    fd = os.open(root, os.O_RDONLY)
    try:
        assert audit.install()
        os.stat(target)
        assert ("f", target) in audit._fast
        original = audit._guard
        calls = []

        def counting(*args):
            calls.append(args)
            return original(*args)

        monkeypatch.setattr(audit, "_guard", counting)
        os.stat(target, dir_fd=fd)
        assert calls
    finally:
        os.close(fd)
    assert reads(logs, "f") == ["input"]


@pytest.mark.parametrize("name", [
    "mod.cpython-313.pyc",
    "mod.cpython-313.opt-2.pyc",
    "mod.cpython-313-pytest-9.0.2.pyc",
])
def test_dotted_bytecode_tags_record_source(environment, name):
    root, logs = environment
    assert audit.install()
    with pytest.raises(FileNotFoundError):
        open(root / "pkg" / "__pycache__" / name)
    assert reads(logs, "f") == ["pkg/mod.py"]
    assert not list(logs.glob("*.error"))


def test_real_pytest_traces_nodeid_and_maps_rewrite_cache(environment):
    root, logs = environment
    (root / "input").write_text("payload", encoding="utf-8")
    (root / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    test_file = root / "test_probe.py"
    test_file.write_text(
        "def test_reads():\n"
        "    with open('input', encoding='utf-8') as stream:\n"
        "        assert stream.read() == 'payload'\n", encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": str(MODULE.parent),
           audit.ENV_DIR: str(logs), audit.ENV_ROOT: str(root),
           "LEAF_READ_AUDIT_TRACE": "1", "PYTHONDONTWRITEBYTECODE": "1",
           "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "PYTEST_ADDOPTS": ""}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "read_audit", "-q",
         "-p", "no:cacheprovider", str(test_file)],
        cwd=root, env=env, capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    sidecar, = logs.glob("*.trace")
    records = [json.loads(line) for line in sidecar.read_text(encoding="utf-8").splitlines()]
    assert {"k": "f", "p": "input", "t": "test_probe.py::test_reads"} in records
    assert not any("-pytest-" in entry.get("p", "") for entry in entries(logs))
    assert not list(logs.glob("*.error"))


def test_retargeted_symlink_records_both_targets(environment):
    root, logs = environment
    first = root / "first-input"
    second = root / "second-input"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    link = root / "input-link"
    link.symlink_to(first)
    assert audit.install()
    with link.open() as stream:
        assert stream.read() == "first"
    assert ("f", str(link)) not in audit._fast
    link.unlink()
    link.symlink_to(second)
    with link.open() as stream:
        assert stream.read() == "second"
    assert set(reads(logs, "f")) == {"input-link", "first-input", "second-input"}
    assert ("f", str(link)) not in audit._fast
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("owner,name,expected", [
    (os.path, "exists", True), (os.path, "lexists", True),
    (os.path, "isfile", True), (os.path, "isdir", False), (os.path, "islink", False),
    (Path, "exists", True), (Path, "is_file", True),
    (Path, "is_dir", False), (Path, "is_symlink", False),
])
def test_existence_and_type_probes_are_distinct_from_stat(environment, owner, name, expected):
    root, logs = environment
    target = root / "input"
    target.write_text("payload", encoding="utf-8")
    signature = inspect.signature(getattr(owner, name))
    assert audit.install()
    probe = getattr(owner, name)
    assert inspect.signature(probe) == signature
    # Repeating an absolute string exercises the e fast path, including its guard.
    for _ in range(2):
        assert probe(target if owner is Path else str(target)) is expected
    if "follow_symlinks" in signature.parameters:
        assert probe(target, follow_symlinks=False) is expected
    assert reads(logs, "e") == ["input"]
    assert reads(logs, "f") == []
    os.stat(target)
    assert reads(logs, "f") == ["input"]
    assert reads(logs, "e") == ["input"]
    assert not list(logs.glob("*.error"))


def test_missing_path_exists_is_only_an_existence_read(environment):
    root, logs = environment
    assert audit.install()
    assert not (root / "missing").exists()
    assert reads(logs, "e") == ["missing"]
    assert reads(logs, "f") == []


def test_existence_reads_normalize_filter_and_follow_symlinks(environment, monkeypatch):
    root, logs = environment
    target = root / "target"
    target.write_text("payload", encoding="utf-8")
    link = root / "link"
    link.symlink_to(target)
    scratch = root / "scratch"
    scratch.mkdir()
    (scratch / "generated").write_text("generated", encoding="utf-8")
    monkeypatch.setenv("TMP", str(scratch))
    assert audit.install()
    assert link.is_file()
    assert os.path.islink(str(link))
    assert not Path("package/__pycache__/module.cpython-312.pyc").exists()
    assert not Path(".git/missing").exists()
    assert not Path(".pytest_cache/missing").exists()
    assert not (root.parent / "outside").exists()
    assert (scratch / "generated").exists()
    link.unlink()
    link.symlink_to("missing-target")
    assert os.path.lexists(link)
    assert not link.exists()
    assert set(reads(logs, "e")) == {"link", "target", "package/module.py", "missing-target"}
    assert reads(logs, "f") == []
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("form", ["relative", "absolute", "pathlib", "dir-fd"])
def test_nonlink_paths_resolve_only_once(environment, monkeypatch, form):
    root, logs = environment
    target = root / "input"
    target.write_text("payload", encoding="utf-8")
    fd = os.open(root, os.O_RDONLY)
    try:
        assert audit.install()
        original = os.path.realpath
        resolved = []

        def counting(path, *args, **kwargs):
            if path == str(target):
                resolved.append(path)
            return original(path, *args, **kwargs)

        monkeypatch.setattr(os.path, "realpath", counting)
        for _ in range(3):
            if form == "dir-fd":
                os.stat("input", dir_fd=fd)
            else:
                os.stat({"relative": "input", "absolute": str(target), "pathlib": target}[form])
        assert resolved == [str(target)]
        assert reads(logs, "f") == ["input"]
        assert not list(logs.glob("*.error"))
    finally:
        os.close(fd)


@pytest.mark.parametrize("name,expected", [
    ("python", "python"), ("python3.13", "python"), ("PyThOn3.12.EXE", "python"),
    ("python.exe", "python"), ("python-helper", "python-helper"), ("pypy3", "pypy3"),
])
def test_opaque_python_executable_names_are_normalized(environment, monkeypatch, name, expected):
    root, logs = environment

    def execute(self, args, executable=None, cwd=None, env=None, shell=False):
        return 42

    monkeypatch.setattr(subprocess.Popen, "_execute_child", execute)
    assert audit.install()
    assert subprocess.Popen._execute_child(None, [name, "-I", "-c", "pass"], name, str(root), {}) == 42
    children = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert children == [{"k": "x", "exe": expected, "cwd": ".", "paths": [], "argv": [expected, "-I", "-c", "pass"]}]


@pytest.mark.parametrize("case", [
    "hash-object", "clone", "inside-temp", "git-dir", "unknown", "config",
    "config-env", "config-parameters", "config-count",
])
def test_r1_git_exclusion_requires_all_locations_outside(environment, monkeypatch, case):
    root, logs = environment
    target = root / "x"
    target.write_text("payload", encoding="utf-8")
    (root / ".git").mkdir()
    temporary = root / "scratch"
    temporary.mkdir()
    monkeypatch.setenv("TMPDIR", str(temporary))
    outside = str(root.parent)
    command, cwd, env = ["git", "-C", outside, "hash-object", str(target)], root, {}
    if case == "clone":
        command, cwd = ["git", "clone", str(root), "dest"], outside
    elif case == "inside-temp":
        command = ["git", "-C", str(temporary), "init"]
    elif case == "git-dir":
        command, cwd = ["git", "--git-dir", str(root / ".git"), "log"], outside
    elif case == "unknown":
        command, cwd = ["git", "--future-option", "status"], outside
    elif case in {"config", "config-env"}:
        option = "-c" if case == "config" else "--config-env"
        value = outside if case == "config" else "WORKTREE"
        command, cwd, env = ["git", option, "core.worktree=" + value, "status"], outside, {"WORKTREE": outside}
    elif case in {"config-parameters", "config-count"}:
        key = "GIT_CONFIG_PARAMETERS" if case == "config-parameters" else "GIT_CONFIG_COUNT"
        command, cwd, env = ["git", "status"], outside, {key: "0"}
    assert audit.install()
    audit._guard(audit._child, None, command, cwd, env)
    children = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert len(children) == (0 if case == "inside-temp" else 1)
    if case in {"hash-object", "clone", "git-dir"}:
        assert children[0]["paths"] == (["x"] if case == "hash-object" else ["."])
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("option", ["--namespace", "--config-env", "--attr-source",
                                   "--super-prefix", "--shallow-file"])
def test_r2_git_global_option_values_are_not_subcommands(environment, option):
    root, logs = environment
    assert audit.install()
    audit._guard(audit._child, None, ["git", option, "ns", "-C", str(root), "status"], root.parent, {})
    child, = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert child["cwd"] == "."
    assert child["sub"] == "status"
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("option", ["-C", "--git-dir", "--work-tree"])
def test_r3_git_locations_resolve_symlinks_before_parent(environment, option):
    root, logs = environment
    (root / "physical" / "deep").mkdir(parents=True)
    (root / "alias").symlink_to(root / "physical" / "deep", target_is_directory=True)
    assert audit.install()
    audit._guard(audit._child, None, ["git", option, "alias/..", "status"], root, {})
    child, = [entry for entry in entries(logs) if entry.get("k") == "x"]
    if option == "-C":
        assert child["cwd"] == "physical"
    else:
        assert child["paths"] == ["physical"]
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("case", ["git-dir", "options", "assignment-path"])
def test_r4_env_dispatch_and_assignment_paths(environment, case):
    root, logs = environment
    (root / ".git").mkdir()
    target = root / "f"
    target.write_text("payload", encoding="utf-8")
    command = ["env", "GIT_DIR=" + str(root / ".git"), "git", "log"]
    if case == "options":
        command = ["env", "-i", "-u", "OLD", "--unset=OTHER", "-C", str(root),
                   "--chdir=.", "-", "git", "status"]
    elif case == "assignment-path":
        command = ["env", "cat", "INPUT=" + str(target)]
    assert audit.install()
    audit._guard(audit._child, None, command, root.parent, {})
    child, = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert child["exe"] == ("cat" if case == "assignment-path" else "git")
    if case == "git-dir":
        assert child["paths"] == ["."]
    elif case == "options":
        assert child["cwd"] == "."
    else:
        assert child["paths"] == ["f"]
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("launcher", ["subprocess", "bytes", "explicit", "system", "popen"])
def test_r5_shell_identity_and_command_paths(environment, launcher):
    root, logs = environment
    target = root / "file with spaces"
    target.write_text("payload", encoding="utf-8")
    command = "cat " + shlex.quote(str(target))
    assert audit.install()
    if launcher in {"subprocess", "bytes"}:
        subprocess.run(os.fsencode(command) if launcher == "bytes" else command,
                       shell=True, cwd=root.parent, check=True, capture_output=True, timeout=20)
    elif launcher == "explicit":
        subprocess.run(["/bin/sh", "-c", command], cwd=root.parent,
                       check=True, capture_output=True, timeout=20)
    elif launcher == "system":
        assert os.system(command + " >/dev/null") == 0
    else:
        with os.popen(command) as stream:
            assert stream.read() == "payload"
    child, = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert child["exe"] == "sh"
    assert child["paths"] == ["file", "file with spaces"]
    assert "sub" not in child
    assert not list(logs.glob("*.error"))


def test_r6_git_options_are_normalized_and_part_of_deduplication(environment):
    root, logs = environment
    (root / "scripts").mkdir()
    assert audit.install()
    for options in (["-z", "--exclude=a", "--exclude=b"], ["-s"], ["-s"]):
        audit._guard(audit._child, None, ["git", "ls-files", *options, "scripts"], root, {})
    children = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert [entry["opts"] for entry in children] == [["--exclude=", "-z"], ["-s"]]
    assert all(entry["paths"] == ["scripts"] for entry in children)


def test_r7_rglob_records_directory_listings_and_captured_staticmethods(environment, monkeypatch):
    root, logs = environment
    (root / "nested").mkdir()
    (root / "nested" / "input.py").write_text("pass\n", encoding="utf-8")
    # Exercise the captured Python 3.13 class attributes on older Linux seats too.
    class CapturedGlobber(getattr(audit.glob, "_StringGlobber", object)):
        scandir = staticmethod(os.scandir)
        lstat = staticmethod(os.lstat)
        lexists = staticmethod(os.path.lexists)

    monkeypatch.setattr(audit.glob, "_StringGlobber", CapturedGlobber, raising=False)
    assert audit.install()
    assert list(root.rglob("*.py")) == [root / "nested" / "input.py"]
    captured = object.__new__(CapturedGlobber)
    with captured.scandir(root) as iterator:
        assert list(iterator)
    assert captured.lexists(root / "nested")
    assert captured.lstat(root / "nested").st_mode
    assert {".", "nested"} <= set(reads(logs, "d"))
    assert "nested" in reads(logs, "e")
    assert "nested" in reads(logs, "f")
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("race", ["during-resolution", "during-chdir"])
def test_r8_relative_fast_keys_cannot_cross_chdir(environment, monkeypatch, race):
    root, logs = environment
    for name in ("a", "b"):
        (root / name).mkdir()
        (root / name / "x").write_text(name, encoding="utf-8")
    assert audit.install()
    os.chdir(root / "a")
    if race == "during-resolution":
        original = audit._relative

        def change_after_resolving(path, *args, **kwargs):
            result = original(path, *args, **kwargs)
            os.chdir(root / "b")
            return result

        with monkeypatch.context() as patch:
            patch.setattr(audit, "_relative", change_after_resolving)
            os.stat("x")
        assert audit._fast_key("f", "x") not in audit._fast
        os.stat("x")
    else:
        os.stat("x")
        original = os.chdir.__wrapped__

        def change_and_probe(path):
            assert audit._chdir_pending == 1
            assert audit._fast_key("f", "x") is None
            original(path)
            os.stat("x")
            assert audit._fast_key("f", "x") is None

        epoch = audit._cwd_epoch
        audit._chdir(change_and_probe)(root / "b")
        assert audit._cwd_epoch == epoch + 2
        assert audit._chdir_pending == 0
        assert audit._fast == set()
    assert {"a/x", "b/x"} <= set(reads(logs, "f"))
    assert not list(logs.glob("*.error"))


def test_r9_fast_strings_do_not_check_pathlike(monkeypatch):
    class RejectCheck(type):
        def __instancecheck__(cls, value):
            raise AssertionError("hot strings must bypass PathLike")

    class PathLike(metaclass=RejectCheck):
        pass

    monkeypatch.setattr(audit.os, "PathLike", PathLike)
    monkeypatch.setattr(audit, "_chdir_pending", 0)
    for value in ("/absolute", b"/absolute", "relative", b"relative"):
        expected = ("f", value) if os.path.isabs(value) else ("f", value, audit._cwd_epoch)
        assert audit._fast_key("f", value) == expected


def test_c1_opaque_child_in_temporary_cwd_is_recorded(environment):
    root, logs = environment
    assert audit.install()
    subprocess.run(["uname"], cwd=audit.tempfile.gettempdir(),
                   check=True, capture_output=True, timeout=20)
    child, = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert child == {"k": "x", "exe": "uname", "cwd": None, "paths": [], "argv": ["uname"]}
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("case,recorded", [
    ("init-temp", False), ("init-db-temp", False), ("outside", False),
    ("hooks-path", True), ("other-git-env", True), ("punctuation", True),
    ("nonexistent-root-path", True), ("git-exe", True), ("uncertain", True),
])
def test_c2_only_proven_outside_git_children_are_skipped(environment, case, recorded):
    root, logs = environment
    (root / "hooks").mkdir()
    temporary = audit.tempfile.gettempdir()
    command, cwd, env = ["git", "status"], root.parent, {}
    if case in {"init-temp", "init-db-temp"}:
        command, cwd = ["git", "init" if case == "init-temp" else "init-db", temporary], root
    elif case == "hooks-path":
        command = ["git", "-c", "core.hooksPath=" + str(root / "hooks"), "commit"]
    elif case == "other-git-env":
        env = {"GIT_OBJECT_DIRECTORY": str(root / "objects")}
    elif case == "punctuation":
        command = ["git", "status", "cat " + str(root / "hooks") + ">out"]
    elif case == "nonexistent-root-path":
        command += [str(root / "not-created")]
    elif case == "git-exe":
        command[0] = "git.exe"
    elif case == "uncertain":
        command = ["git", "--future-option", "status"]
    assert audit.install()
    audit._guard(audit._child, None, command, cwd, env)
    children = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert len(children) == int(recorded)
    if case == "hooks-path":
        assert children[0]["paths"] == ["hooks"]
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("program,options,command", [
    ("bash", "-lc", "cat {path}"), ("sh", "-c", "cat {path}>out"),
    ("dash", "-ec", "cat {path};true"), ("zsh", "-xc", "cat {path}|cat"),
    ("ksh", "-euc", "cat {path}"), ("mksh", "-c", "cat {path}"),
    ("ash", "-c", "cat {path}"), ("busybox", "-c", "cat {path}"),
    ("fish", "-c", "cat {path}"),
])
def test_c3_shell_option_clusters_and_punctuation_find_paths(environment, program, options, command):
    root, logs = environment
    (root / "f").write_text("payload", encoding="utf-8")
    assert audit.install()
    audit._guard(audit._child, None, [program, options, command.format(path=root / "f")], root.parent, {})
    child, = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert child["exe"] == program
    assert child["paths"] == ["f"]
    assert "sub" not in child
    # shell=True is authoritative even with an executable whose basename is git.
    audit._guard(audit._child, "git", ["git", "-c", "cat " + str(root / "f")], root.parent, {}, (), True)
    shell_child = [entry for entry in entries(logs) if entry.get("k") == "x"][-1]
    assert shell_child["exe"] == "git" and "sub" not in shell_child
    assert shell_child["paths"] == ["f"]
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("form", ["-S", "-S=", "-Sattached", "--split-string", "--split-string=", "--split-stringattached",
                                  "attached-options", "unknown", "wrapper"])
def test_c4_env_split_strings_and_opaque_wrappers(environment, form):
    root, logs = environment
    (root / ".git").mkdir()
    value = shlex.join(["git", "-C", str(root), "log"])
    command = ["env", form, value]
    if form in {"-S=", "--split-string="}:
        command = ["env", form + value]
    elif form == "-Sattached":
        command = ["env", "-S" + value]
    elif form == "--split-stringattached":
        command = ["env", "--split-string" + value]
    elif form == "attached-options":
        command = ["env", "-uOLD", "-C" + str(root), "git", "log"]
    elif form == "unknown":
        command = ["env", "--future-option", "GIT_DIR=" + str(root / ".git"), "git", "log"]
    elif form == "wrapper":
        command = ["timeout", "5", "env", "GIT_DIR=" + str(root / ".git"), "git", "log"]
    assert audit.install()
    audit._guard(audit._child, None, command, root.parent, {})
    child, = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert child["exe"] == ("env" if form == "unknown" else "timeout" if form == "wrapper" else "git")
    if form in {"unknown", "wrapper"}:
        assert child["paths"] == ["."]
        assert "sub" not in child
    else:
        assert child["cwd"] == "."
        assert child["sub"] == "log"
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("source", ["argument", "assignment", "git-dir", "work-tree", "global-C", "missing-argument"])
def test_c5_every_child_maps_skipped_checkout_paths_to_dot(environment, source):
    root, logs = environment
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    target = str(root / ".git" / "HEAD")
    arguments, env = ["opaque"], {}
    if source == "missing-argument":
        arguments.append(str(root / ".git" / "not-created"))
    elif source == "argument":
        arguments.append(target)
    elif source == "assignment":
        arguments.append("INPUT=" + target)
    elif source == "global-C":
        arguments = ["git", "-C", str(root / ".git"), "log"]
    else:
        env["GIT_DIR" if source == "git-dir" else "GIT_WORK_TREE"] = target
    assert audit.install()
    audit._guard(audit._child, None, arguments, root.parent, env)
    child, = [entry for entry in entries(logs) if entry.get("k") == "x"]
    if source == "missing-argument":
        assert child["paths"] == ["."]
    else:
        assert child["paths"] == ["."]
    assert not list(logs.glob("*.error"))


def test_c6_globber_python_emulations_keep_descriptors_and_still_glob(environment, monkeypatch):
    root, logs = environment
    (root / "input.py").write_text("pass\n", encoding="utf-8")

    class EmulatedGlobber:
        def scandir(self, path):
            return os.scandir(path)

        def lstat(self, path):
            return os.lstat(path)

        def lexists(self, path):
            return os.path.lexists(path)

    originals = {name: inspect.getattr_static(EmulatedGlobber, name)
                 for name in ("scandir", "lstat", "lexists")}
    monkeypatch.setattr(audit.glob, "_Globber", EmulatedGlobber, raising=False)
    assert audit.install()
    for name, original in originals.items():
        assert inspect.getattr_static(EmulatedGlobber, name) is original
    instance = EmulatedGlobber()
    with instance.scandir(root) as iterator:
        assert [entry.name for entry in iterator] == ["input.py"]
    assert instance.lexists(root / "input.py")
    assert instance.lstat(root / "input.py").st_mode
    assert list(root.rglob("*.py")) == [root / "input.py"]
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("program", ["opaque", "git"])
def test_c7_candidate_realpaths_are_lazy_and_invalid_tokens_are_harmless(environment, monkeypatch, program):
    root, logs = environment
    assert audit.install()
    original = os.path.realpath
    resolved = []

    def counting(path, *args, **kwargs):
        resolved.append(os.fspath(path))
        return original(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(os.path, "realpath", counting)
        audit._guard(audit._child, None, [program, "status", "not-a-path", "bad\0token"], root.parent, {})
    if program == "opaque":
        assert not any("not-a-path" in path or "bad\0token" in path for path in resolved)
        assert len([entry for entry in entries(logs) if entry.get("k") == "x"]) == 1
    else:
        assert any("not-a-path" in path for path in resolved)
    assert not list(logs.glob("*.error"))


def test_c8_explicit_child_cwd_is_physical_before_resolving_paths(environment):
    root, logs = environment
    (root / "physical" / "deep").mkdir(parents=True)
    (root / "alias").symlink_to(root / "physical" / "deep", target_is_directory=True)
    (root / "physical" / "f").write_text("payload", encoding="utf-8")
    assert audit.install()
    audit._guard(audit._child, None, ["opaque", "f"], "alias/..", {})
    child, = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert child["cwd"] == "physical"
    assert child["paths"] == ["physical/f"]
    assert not list(logs.glob("*.error"))


def test_c9_dir_fd_stat_does_not_need_a_live_cwd(environment):
    root, logs = environment
    (root / "f").write_text("payload", encoding="utf-8")
    vanished = root.parent / "vanished"
    vanished.mkdir()
    fd = os.open(root, os.O_RDONLY)
    assert audit.install()
    try:
        os.chdir(vanished)
        vanished.rmdir()
        assert os.stat("f", dir_fd=fd).st_size == len("payload")
    finally:
        os.chdir(root)
        os.close(fd)
    assert "f" in reads(logs, "f")
    assert not list(logs.glob("*.error"))


def test_c10_child_argv_is_normalized_bounded_and_keeps_first_seen(environment):
    root, logs = environment
    target = root / "f"
    target.write_text("payload", encoding="utf-8")
    temporary = Path(audit.tempfile.gettempdir()) / "scratch-file"
    temporary.write_text("scratch", encoding="utf-8")
    assert audit.install()
    for suffix in ("first", "second"):
        command = ["opaque", str(target), str(temporary), "x" * 200, suffix,
                   *["arg" + str(index) for index in range(20)]]
        audit._guard(audit._child, None, command, root.parent, {})
    child, second = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert child["argv"] == ["opaque", "root:/f", "<tmp>/scratch-file", "x" * 200, "first",
                             *["arg" + str(index) for index in range(20)]]
    assert second["argv"] == [*child["argv"][:4], "second", *child["argv"][5:]]
    assert child["paths"] == ["f"]
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("case", [
    "template", "template-pair", "template-after-operand", "separate-git-dir", "template-env",
    "file-url", "localhost-url", "https-url", "remote-path", "attached-option",
    "home", "editor", "other-env", "config-env",
])
def test_d1_git_skip_considers_all_path_sources(environment, case):
    root, logs = environment
    outside = root.parent / "fixture"
    outside.mkdir()
    for directory in ("tpl", "home"):
        (root / directory).mkdir()
    (root / "msg").write_text("message", encoding="utf-8")
    command, cwd, env = ["git", "status"], outside, {}
    if case.startswith("template") or case == "separate-git-dir":
        cwd = root
        command = ["git", "init", "--template=tpl", str(outside / "new")]
        if case == "template-pair":
            command = ["git", "init", "--template", "tpl", str(outside / "new")]
        elif case == "template-after-operand":
            command = ["git", "init", str(outside / "new"), "--template=tpl"]
        elif case == "separate-git-dir":
            command = ["git", "init", "--separate-git-dir=tpl", str(outside / "new")]
        elif case == "template-env":
            command, env = ["git", "init", str(outside / "new")], {"GIT_TEMPLATE_DIR": "tpl"}
    elif case.endswith("url") or case == "remote-path":
        source = {"file-url": "file://", "localhost-url": "file://localhost",
                  "https-url": "https://example.invalid", "remote-path": "host:"}[case] + str(root)
        command = ["git", "clone", source, str(outside / "clone")]
    elif case == "attached-option":
        command = ["git", "commit", "-F" + str(root / "msg")]
    elif case == "config-env":
        command = ["git", "--config-env", "core.hooksPath=HOOKS", "commit"]
        env = {"HOOKS": "../root/tpl"}
    else:
        key = {"home": "HOME", "editor": "EDITOR", "other-env": "ARBITRARY"}[case]
        env[key] = str(root / ("home" if case == "home" else "msg"))
    assert audit.install()
    audit._guard(audit._child, None, command, cwd, env)
    children = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert [child["exe"] for child in children] == ([] if case == "other-env" else ["git"])
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("options,recorded", [
    (["-q", "-b", "main"], False), (["-bmain"], False),
    (["--initial-branch", "main"], False), (["--initial-branch=main"], False),
    (["--object-format", "sha1"], False), (["--ref-format", "files"], False),
    (["--shared", "group"], False), (["--template", ""], False),
    (["--future-option", "value"], True),
])
def test_d2_git_init_options_do_not_become_directory_operands(environment, options, recorded):
    root, logs = environment
    assert audit.install()
    command = ["git", "init", *options, str(root.parent / "new-fixture")]
    audit._guard(audit._child, None, command, root, {})
    assert len([entry for entry in entries(logs) if entry.get("k") == "x"]) == int(recorded)
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("cwd", ["absolute", "relative", "inherited"])
def test_d3_child_recording_survives_deleted_cwd(environment, cwd):
    root, logs = environment
    vanished = root.parent / "deleted-child-cwd"
    vanished.mkdir()
    assert audit.install()
    try:
        os.chdir(vanished)
        vanished.rmdir()
        if cwd == "absolute":
            subprocess.run(["true"], cwd=str(root), env={}, check=True, timeout=20)
        else:
            audit._guard(audit._child, None, ["true"], "." if cwd == "relative" else None, {})
    finally:
        os.chdir(root)
    child, = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert child["exe"] == "true"
    assert child["cwd"] == ("." if cwd == "absolute" else None)
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("command", ["cat {path}", "echo `cat {path}`", "cat {path}>out"])
def test_d4_every_child_tokenizes_distinct_compound_candidates(environment, monkeypatch, command):
    root, logs = environment
    (root / "f").write_text("payload", encoding="utf-8")
    command = command.format(path=root / "f")
    tokenized = []
    original = audit._child_tokens

    def counting(value):
        tokenized.append(value)
        return original(value)

    monkeypatch.setattr(audit, "_child_tokens", counting)
    assert audit.install()
    audit._guard(audit._child, None, ["timeout", "5", "sh", "-c", command, command], root.parent, {})
    child, = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert child["exe"] == "timeout"
    assert child["paths"] == ["f"]
    assert tokenized == [command]
    assert not list(logs.glob("*.error"))


def test_d6_empty_path_children_keep_distinct_argv_before_observation(environment):
    root, logs = environment
    assert audit.install()
    for code in ("pass", "print(1)", "pass"):
        audit._guard(audit._child, None, ["python", "-I", "-c", code], root.parent, {})
    children = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert [child["argv"] for child in children] == [
        ["python", "-I", "-c", "pass"], ["python", "-I", "-c", "print(1)"],
    ]
    assert all(child["paths"] == [] for child in children)
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("source", ["audit-env", "arbitrary", "editor", "path", "git-env"])
def test_e1_git_env_candidates_are_only_dereferenced_variables(environment, monkeypatch, source):
    root, logs = environment
    monkeypatch.setenv("PWD", str(root))
    monkeypatch.setenv("OLDPWD", str(root))
    assert audit.install()
    env = None if source == "audit-env" else {}
    if source == "arbitrary":
        env["ARBITRARY"] = str(root / "unread")
    elif source == "editor":
        env["EDITOR"] = "sh " + str(root / "editor.sh")
    elif source == "path":
        env["PATH"] = os.pathsep.join(["/usr/bin", str(root / "bin"), "/bin"])
    elif source == "git-env":
        env["GIT_OBJECT_DIRECTORY"] = str(root / "objects")
    audit._guard(audit._child, None, ["git", "status"], root.parent, env)
    children = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert bool(children) is (source in {"editor", "path", "git-env"})
    assert not list(logs.glob("*.error"))


def test_e2_embedded_checkout_paths_include_missing_python_sources(environment):
    root, logs = environment
    assert audit.install()
    code = ("import importlib.util as u; "
            f"u.spec_from_file_location('g','{root}/scripts/x.py')")
    audit._guard(audit._child, None, [sys.executable, "-I", "-c", code], root.parent, {})
    child, = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert child["paths"] == ["scripts/x.py"]
    assert child["argv"] == ["python", "-I", "-c", code.replace(str(root), "root:")]
    assert not list(logs.glob("*.error"))


def test_e3_portable_argv_keeps_full_command_identity_and_hashes_overflow(environment, monkeypatch):
    root, logs = environment
    monkeypatch.setattr(audit.tempfile, "tempdir", str(root.parent))
    monkeypatch.setenv("TMPDIR", str(root.parent))
    temporary = audit.tempfile.gettempdir()
    assert audit.install()
    assert temporary not in audit._state["temp_roots"]
    assert temporary in audit._state["argv_temp_roots"]
    prefix = ["/usr/local/bin/python3.13", "-I", "-c",
              f"load('{root}/missing.py')", temporary + "/a.sh --flag", "/opt/leaf-input.dat",
              "'/usr/bin/tool' --input=" + str(root / "missing.py")]
    for suffix in ("first", "second", "first"):
        command = [*prefix, "x" * 450 + suffix, *["arg" + str(i) for i in range(70)]]
        audit._guard(audit._child, None, command, root.parent, {})
    children = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert len(children) == 2
    first, second = [child["argv"] for child in children]
    assert first[:7] == ["python", "-I", "-c", "load('root:/missing.py')",
                         "<tmp>/a.sh --flag", "<ext>/leaf-input.dat",
                         "'<ext>/tool' --input=root:/missing.py"]
    assert len(first) == 65
    assert all(len(token) <= 400 for token in first[:64])
    assert first[-1].startswith("sha256:") and len(first[-1]) == 71
    assert first[:-1] == second[:-1] and first[-1] != second[-1]
    assert all(child["paths"] == ["missing.py"] for child in children)
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("suffix", ["", " --flag", "'", '"', ",", "(", ")", "[", "]",
                                   "\t", "`", ";", "&", "|", "<", ">"])
def test_i1_temp_inside_checkout_keeps_basename_and_token_suffix(environment, monkeypatch, suffix):
    root, logs = environment
    temporary = root / "tmp"
    monkeypatch.setenv("TMPDIR", str(temporary))
    assert audit.install()
    path = temporary / "pytest-0" / "popen-gw3" / "test_x0" / "extracted"
    audit._guard(audit._child, None, ["opaque", str(path) + suffix], root, {})
    child, = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert child["argv"] == ["opaque", "<tmp>/extracted" + suffix]


def test_i2_checkout_inside_temp_still_uses_root_and_temp_matches_boundaries(environment, monkeypatch):
    root, logs = environment
    temporary = root.parent
    monkeypatch.setenv("TMPDIR", str(temporary))
    monkeypatch.setattr(audit.tempfile, "tempdir", str(temporary))
    assert audit.install()
    # Keep this boundary check independent of a seat's additional temp roots.
    monkeypatch.setitem(audit._state, "argv_temp_roots", {str(temporary)})
    arguments = ["opaque", str(root / "f"), str(temporary),
                 str(temporary / "pytest-9" / "a.sh") + " --flag",
                 str(temporary) + "2/x", str(root) + "2/f"]
    audit._guard(audit._child, None, arguments, root, {})
    child, = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert child["argv"] == ["opaque", "root:/f", "<tmp>", "<tmp>/a.sh --flag",
                             "<ext>/x", "<tmp>/f"]


def test_e4_location_misses_are_cheap_and_skipped_git_never_normalizes_argv(environment, monkeypatch):
    root, logs = environment
    assert audit.install()

    def unexpected(*args):
        raise AssertionError("outside location and skipped git must stay cheap")

    with monkeypatch.context() as patch:
        patch.setattr(audit, "_under", unexpected)
        assert audit._child_location(str(root.parent / "outside")) is False
    with monkeypatch.context() as patch:
        patch.setattr(audit, "_child_argv", unexpected)
        audit._guard(audit._child, None, ["git", "status"], root.parent, {})
    assert not [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("exe", ["opaque", "git"])
def test_e5_empty_assignment_candidates_never_add_the_current_directory(environment, exe):
    root, logs = environment
    assert audit.install()
    audit._guard(audit._child, None, [exe, "status", "EMPTY=", ""], root, {})
    child, = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert child["paths"] == []
    assert child["argv"][-2:] == ["EMPTY=", ""]
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("command", [["timeout", "5", "git", "status"],
                                     ["sh", "-c", "git status"]])
def test_e6_wrapped_git_adds_checkout_wide_path_hint(environment, command):
    root, logs = environment
    assert audit.install()
    audit._guard(audit._child, None, command, root.parent, {})
    child, = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert child["exe"] == command[0]
    assert child["paths"] == ["."]
    assert not list(logs.glob("*.error"))


def test_e7_git_clustered_option_suffixes_are_path_candidates(environment):
    root, logs = environment
    fixture = root.parent / "fixture-repo"
    fixture.mkdir()
    (root / "msg").write_text("message", encoding="utf-8")
    assert audit.install()
    audit._guard(audit._child, None, ["git", "commit", "-qF../root/msg"], fixture, {})
    child, = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert child["exe"] == "git"
    assert child["paths"] == []
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("source", ["template", "template-env", "intermediate-C"])
def test_e9_init_base_follows_globals_and_keeps_every_C_target(environment, source):
    root, logs = environment
    outside = root.parent / "fixture-repo"
    outside.mkdir()
    (root / "templates").mkdir()
    env = {}
    if source == "intermediate-C":
        command = ["git", "-C", str(root), "-C", str(outside), "init", "new"]
    else:
        command = ["git", "-C", str(root), "init"]
        if source == "template":
            command += ["--template=templates"]
        else:
            env["GIT_TEMPLATE_DIR"] = "templates"
        command += [str(outside / "new")]
    assert audit.install()
    audit._guard(audit._child, None, command, root.parent, env)
    child, = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert child["exe"] == "git" and child["sub"] == "init"
    assert "." in child["paths"]
    if source != "intermediate-C":
        assert "templates" in child["paths"]
    assert not list(logs.glob("*.error"))


@pytest.mark.parametrize("wrapper", [False, True])
def test_e10_executable_override_is_a_path_candidate(environment, wrapper):
    root, logs = environment
    script = root / ("env" if wrapper else "launcher.sh")
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    assert audit.install()
    if wrapper:
        audit._guard(audit._child, str(script), ["display-name", "git", "status"], root.parent, {})
    else:
        subprocess.run(["display-name"], executable=str(script), cwd=root.parent,
                       env={}, check=True, timeout=20)
    child, = [entry for entry in entries(logs) if entry.get("k") == "x"]
    assert child["exe"] == ("git" if wrapper else "launcher.sh")
    assert child["paths"] == [script.name]
    assert child["argv"] == (["git", "git", "status"] if wrapper else ["launcher.sh"])
    assert not list(logs.glob("*.error"))
