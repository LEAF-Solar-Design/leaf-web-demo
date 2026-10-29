"""Run-owned PostgreSQL authority. Use PgAuthority.start(run_id), then stop().

Host consumers receive dsn() explicitly. No published ports or ambient DB config.
The optional docker/spawn constructor seams support hermetic transport tests.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import multiprocessing
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import stat
import socket
import subprocess
import tempfile
import threading
import time
from urllib.parse import quote

OWNER = "proof_project_pg"
RUN_LABEL = "leaf.proof-project.run"
OWNER_LABEL = "leaf.proof-project.owner"
MAX_CONNECTIONS = 32
# PostgreSQL 18, section 32.15: https://www.postgresql.org/docs/18/libpq-envars.html
# Include session and internal settings as well as connection defaults.
_LIBPQ_ENV = (
    "PGHOST", "PGSSLNEGOTIATION", "PGHOSTADDR", "PGPORT", "PGDATABASE", "PGUSER",
    "PGPASSWORD", "PGPASSFILE", "PGREQUIREAUTH", "PGCHANNELBINDING", "PGSERVICE",
    "PGSERVICEFILE", "PGOPTIONS", "PGAPPNAME", "PGSSLMODE", "PGREQUIRESSL",
    "PGSSLCOMPRESSION", "PGSSLCERT", "PGSSLKEY", "PGSSLCERTMODE", "PGSSLROOTCERT",
    "PGSSLCRL", "PGSSLCRLDIR", "PGSSLSNI", "PGREQUIREPEER", "PGSSLMINPROTOCOLVERSION",
    "PGSSLMAXPROTOCOLVERSION", "PGGSSENCMODE", "PGKRBSRVNAME", "PGGSSLIB",
    "PGGSSDELEGATION", "PGCONNECT_TIMEOUT", "PGCLIENTENCODING", "PGTARGETSESSIONATTRS",
    "PGLOADBALANCEHOSTS", "PGMINPROTOCOLVERSION", "PGMAXPROTOCOLVERSION",
    "PGDATESTYLE", "PGTZ", "PGGEQO", "PGSYSCONFDIR", "PGLOCALEDIR",
)
_LIBPQ_ENV_SET = frozenset(_LIBPQ_ENV)
_CONNECT_LOCK = threading.Lock()
RELAY_CODE = r'''
import os, socket, sys, threading
s = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=10)
s.settimeout(None)
def upstream():
    try:
        while True:
            data = os.read(0, 65536)
            if not data: break
            s.sendall(data)
    finally:
        os._exit(0)
threading.Thread(target=upstream, daemon=True).start()
try:
    while True:
        data = s.recv(65536)
        if not data: break
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
finally:
    os._exit(0)
'''


def clean_libpq_environment(environ):
    """Copy a consumer's environment without libpq's documented ambient inputs.

    Names compare without regard to case: a Windows child reads ``pgservice``
    as ``PGSERVICE``, and POSIX libpq never reads a lowercase name, so dropping
    one changes nothing libpq reads. A key that is not text is kept.
    """
    return {
        name: value for name, value in environ.items()
        if not (isinstance(name, str) and name.upper() in _LIBPQ_ENV_SET)
    }


def _libpq_connect(args, *, native=False):
    import psycopg
    # Preserve Docker's documented-name isolation; native calls exclude every
    # PG* name as well. One connect site serves readiness and database creation.
    with _CONNECT_LOCK:
        names = tuple(key for key in os.environ if key.upper().startswith("PG")) if native else _LIBPQ_ENV
        previous = {name: os.environ[name] for name in names if name in os.environ}
        try:
            for name in names:
                os.environ.pop(name, None)
            return psycopg.connect(**args)
        finally:
            for name in names:
                os.environ.pop(name, None)
            os.environ.update(previous)


def _validate(run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{7,63}", run_id):
        raise ValueError("invalid proof run id")


def _docker_env():
    return {k: os.environ[k] for k in (
        "PATH", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
        "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_API_VERSION",
    ) if k in os.environ}


def _docker(args):
    # Never propagate docker output/exceptions: daemon diagnostics may echo secrets.
    try:
        result = subprocess.run(["docker", *args], env=_docker_env(),
                                capture_output=True, text=True, timeout=60)
        if result.returncode:
            raise RuntimeError("docker command failed")
        return result.stdout
    except Exception:
        raise RuntimeError("proof database docker operation failed") from None


@contextmanager
def _password_file(password):
    """Create private at birth, including a protected owner-only DACL on Windows."""
    path = Path(tempfile.gettempdir()) / ("leafproof-" + secrets.token_hex(16) + ".env")
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        import msvcrt
        class SecurityAttributes(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("descriptor", ctypes.c_void_p),
                        ("inherit", wintypes.BOOL)]
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        descriptor = ctypes.c_void_p()
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                "D:P(A;;FA;;;OW)", 1, ctypes.byref(descriptor), None):
            raise RuntimeError("cannot create private credential file")
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                      wintypes.HANDLE]
        try:
            attrs = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
            handle = kernel.CreateFileW(str(path), 0x40000000, 0, ctypes.byref(attrs), 1, 0x80, None)
            if handle == wintypes.HANDLE(-1).value:
                raise RuntimeError("cannot create private credential file")
            fd = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
        finally:
            kernel.LocalFree(descriptor)
    else:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(("POSTGRES_PASSWORD=" + password + "\n").encode())
        yield str(path)
    finally:
        path.unlink(missing_ok=True)


@contextmanager
def _start_lock(run_id):
    # Keep the inode: deleting a lock path could give contenders different locks.
    path = Path(tempfile.gettempdir()) / ("leafproof-" + run_id + ".lock")
    with open(path, "a+b") as stream:
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError("run already exists") from None
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _inventory(run_id, docker):
    selected = {"containers": [], "networks": []}
    prefix = "leafproof-" + run_id
    names = {"containers": {prefix + "-pg", prefix + "-relay"}, "networks": {prefix}}
    for kind, command in (("containers", ["ps", "-a"]), ("networks", ["network", "ls"])):
        common = ["--format", "{{json .}}"]
        owned = docker([*command, "--filter", f"label={RUN_LABEL}={run_id}",
                        "--filter", f"label={OWNER_LABEL}={OWNER}", *common])
        named = docker([*command, "--filter", "name=" + prefix, *common])
        rows = [json.loads(line) for line in (owned + "\n" + named).splitlines() if line.strip()]
        for row in rows:
            name = row.get("Names", row.get("Name", ""))
            labels = dict(item.split("=", 1) for item in row.get("Labels", "").split(",") if "=" in item)
            matches = labels.get(RUN_LABEL) == run_id and labels.get(OWNER_LABEL) == OWNER
            if name in names[kind] and not matches:
                raise RuntimeError("refusing cleanup: run name has foreign ownership labels")
            if matches and name in names[kind] and row["ID"] not in selected[kind]:
                selected[kind].append(row["ID"])
    return selected


def cleanup(run_id, *, docker=None):
    """Preflight both inventories before deleting anything; remove by listed ID."""
    _validate(run_id)
    docker = docker or _docker
    try:
        selected = _inventory(run_id, docker)
        for resource in selected["containers"]:
            docker(["rm", "-f", resource])
        for resource in selected["networks"]:
            docker(["network", "rm", resource])
        return selected
    except Exception:
        raise RuntimeError("proof database cleanup refused or failed") from None


def _transport_main(run_id, channel, spawn):
    # No credential crosses this boundary. A separate interpreter keeps legacy
    # libpq PQcancel (which can hold its caller's GIL) from starving the relay.
    authority = PgAuthority(run_id, spawn=spawn)
    try:
        authority._listen()
        channel.send(authority.address)
        channel.recv()
    except EOFError:
        pass
    finally:
        authority._stop_transport()
        channel.close()


class PgAuthority:
    def __init__(self, run_id, *, docker=None, spawn=None):
        _validate(run_id)
        self.run_id = run_id
        self.name = "leafproof-" + run_id
        self._password = secrets.token_urlsafe(32)
        self._docker = docker or _docker
        self._spawn = spawn or subprocess.Popen
        self._listener = None
        self._accept_thread = None
        self._connections = set()
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self._transport_process = None
        self._transport_channel = None
        self._created = {"containers": [], "networks": []}

    @classmethod
    def start(cls, run_id, **seams):
        """Create fresh resources and wait for a host transaction.

        Windows callers must use the usual ``if __name__ == '__main__'`` guard
        because the managed host relay uses multiprocessing's spawn context.
        """
        authority = cls(run_id, **seams)
        with _start_lock(run_id):
            return authority._start()

    def _start(self):
        # A collision must not lead to adopting or deleting a pre-existing run.
        try:
            existing = _inventory(self.run_id, self._docker)
        except Exception:
            raise RuntimeError("proof database run unavailable") from None
        if any(existing.values()):
            raise RuntimeError("run already exists")
        try:
            self._provision()
            self._start_transport()
            deadline = time.monotonic() + 60
            while True:
                try:
                    self.readiness()
                    return self
                except RuntimeError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("proof database readiness timed out") from None
                    time.sleep(0.2)
        except BaseException:
            failed = False
            for cleanup_step in (self._stop_transport, self._cleanup_created):
                try:
                    cleanup_step()
                except BaseException:
                    failed = True
            if failed:
                raise RuntimeError("proof database start and cleanup failed") from None
            raise RuntimeError("proof database start failed") from None

    def _provision(self):
        labels = ["--label", f"{RUN_LABEL}={self.run_id}", "--label", f"{OWNER_LABEL}={OWNER}"]
        resource = self._docker(["network", "create", "--internal", *labels, self.name]).strip()
        self._created["networks"].append(resource or self.name)
        with _password_file(self._password) as envfile:
            resource = self._docker(["run", "-d", "--name", self.name + "-pg", *labels,
                          "--network", self.name, "--tmpfs", "/var/lib/postgresql/data:rw",
                          "--env-file", envfile, "--env", "POSTGRES_DB=leaf", "postgres:16"]).strip()
            self._created["containers"].append(resource or self.name + "-pg")
        resource = self._docker(["run", "-d", "--name", self.name + "-relay", *labels,
                      "--network", self.name, "python:3.12", "sleep", "infinity"]).strip()
        self._created["containers"].append(resource or self.name + "-relay")

    def _cleanup_created(self):
        for kind, command in (("containers", ["rm", "-f"]), ("networks", ["network", "rm"])):
            for resource in list(self._created[kind]):
                self._docker([*command, resource])
                self._created[kind].remove(resource)

    def _listen(self):
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(64)
        self._listener.settimeout(0.2)
        self.address = self._listener.getsockname()
        self._accept_thread = threading.Thread(target=self._accept, daemon=True)
        self._accept_thread.start()

    def _start_transport(self):
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        self._transport_channel = parent
        self._transport_process = context.Process(
            target=_transport_main, args=(self.run_id, child, self._spawn), daemon=True)
        try:
            self._transport_process.start()
        except BaseException:
            child.close()
            parent.close()
            self._transport_process = None
            self._transport_channel = None
            raise
        child.close()
        if not parent.poll(15):
            raise RuntimeError("proof relay startup timed out")
        self.address = parent.recv()

    def _accept(self):
        while not self._closing.is_set():
            try:
                client, _ = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lock:
                if self._closing.is_set() or len(self._connections) >= MAX_CONNECTIONS:
                    client.close()
                    continue
                try:
                    child = self._spawn(["docker", "exec", "-i", self.name + "-relay",
                                         "python3", "-u", "-c", RELAY_CODE, self.name + "-pg", "5432"],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, bufsize=0, env=_docker_env())
                except Exception:
                    client.close()
                    continue
                pair = (client, child)
                self._connections.add(pair)
            state = {"remaining": 2}
            for direction in (True, False):
                threading.Thread(target=self._pump, args=(pair, direction, state), daemon=True).start()

    @staticmethod
    def _close_pair(pair):
        client, child = pair
        try:
            client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        client.close()
        try:
            child.kill()
        except OSError:
            pass

    def _pump(self, pair, upstream, state):
        client, child = pair
        try:
            while True:
                data = client.recv(65536) if upstream else child.stdout.read(65536)
                if not data:
                    break
                if upstream:
                    # Raw pipe writes may be short; never discard a binary suffix.
                    remaining = memoryview(data)
                    while remaining:
                        count = child.stdin.write(remaining)
                        if not count:
                            raise OSError("closed pipe")
                        remaining = remaining[count:]
                else:
                    client.sendall(data)
        except (OSError, ValueError):
            pass
        finally:
            self._close_pair(pair)
            child.wait()
            with self._lock:
                state["remaining"] -= 1
                if not state["remaining"]:
                    child.stdin.close()
                    child.stdout.close()
                    self._connections.discard(pair)

    def dsn(self):
        """Explicit authority for consumers in other processes.

        Supervisors must pass clean_libpq_environment(environ) as the child
        environment: a DSN alone cannot suppress all ambient libpq settings.
        """
        return (f"postgresql://postgres:{quote(self._password, safe='')}@{self.address[0]}:{self.address[1]}"
                f"/leaf?hostaddr={self.address[0]}&sslmode=disable&gssencmode=disable&connect_timeout=5&options=")

    def _connect_args(self):
        return {"host": self.address[0], "hostaddr": self.address[0], "port": self.address[1],
                "dbname": "leaf", "user": "postgres", "password": self._password,
                "sslmode": "disable", "gssencmode": "disable", "connect_timeout": 5}

    def _connect(self):
        return _libpq_connect(self._connect_args())

    def redacted_dsn(self):
        return self.dsn().replace(quote(self._password, safe=""), "***")

    def readiness(self):
        try:
            payload = bytes(range(256))
            with self._connect() as conn:
                with conn.transaction():
                    actual, version = conn.execute(
                        "SELECT %s::bytea, current_setting('server_version')", (payload,)).fetchone()
                    if bytes(actual) != payload:
                        raise RuntimeError("binary round trip mismatch")
            return {"ok": True, "server_version": version, "round_trip_bytes": len(actual)}
        except Exception:
            raise RuntimeError("proof database readiness failed") from None

    def _stop_transport(self):
        if self._transport_process:
            try:
                self._transport_channel.send("stop")
            except (BrokenPipeError, EOFError, OSError):
                pass
            self._transport_process.join(timeout=15)
            if self._transport_process.is_alive():
                # Do not remove resources while a relay may still be using them.
                raise RuntimeError("proof relay shutdown timed out")
            self._transport_channel.close()
            self._transport_process = None
        self._closing.set()
        if self._listener:
            self._listener.close()
        if self._accept_thread:
            self._accept_thread.join()
        with self._lock:
            pairs = list(self._connections)
        for pair in pairs:
            self._close_pair(pair)
        for _, child in pairs:
            child.wait()

    def stop(self):
        self._stop_transport()
        return cleanup(self.run_id, docker=self._docker)


NATIVE_DATA = "postgres/data"
NATIVE_TIMEOUT = 60


def _native_environment(environ=None):
    source = clean_libpq_environment(os.environ if environ is None else environ)
    # Deliberately also exclude unknown/future PG* settings.
    result = {key: value for key, value in source.items()
              if key.upper() in {"PATH", "HOME", "TMPDIR", "SYSTEMROOT", "WINDIR"}
              and not key.upper().startswith("PG")}
    result.update(LC_ALL="C", LANG="C", TZ="UTC")
    return result


def _native_command(argv, *, runner=None, environ=None, timeout=NATIVE_TIMEOUT + 5):
    try:
        result = (runner or subprocess.run)(list(argv), env=_native_environment(environ),
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise RuntimeError("native command failed")
        return result.stdout
    except Exception:
        # Neither child output nor an exception containing argv may escape.
        raise RuntimeError("proof native database operation failed") from None


def resolve_native_binaries(*, environ=None, executable=None, runner=None):
    source = os.environ if environ is None else environ
    executable = executable or (lambda path: Path(path).is_file() and os.access(path, os.X_OK))
    directories = list(dict.fromkeys(filter(None, [source.get("LEAF_PG_BIN"),
        *source.get("PATH", "").split(os.pathsep), "/opt/homebrew/opt/postgresql@16/bin",
        "/usr/local/opt/postgresql@16/bin", "/usr/lib/postgresql/16/bin"])))
    binaries = {}
    for name in ("initdb", "pg_ctl", "postgres"):
        for directory in directories:
            candidate = str(Path(directory) / name)
            if executable(candidate):
                binaries[name] = candidate
                break
        if name not in binaries:
            raise RuntimeError("proof native database binaries unavailable")
    if len({Path(path).resolve().parent for path in binaries.values()}) != 1:
        raise RuntimeError("proof native database binaries must share a directory")
    for name, binary in binaries.items():
        version = _native_command([binary, "--version"], runner=runner, environ=source)
        if re.fullmatch(re.escape(name) + r" \(PostgreSQL\) 16(?:\.[0-9]+)?(?: [^\r\n]*)?\s*", version) is None:
            raise RuntimeError("proof native database requires PostgreSQL 16")
    return binaries


def native_process_identity(pid, *, runner=None, kill_probe=None):
    """Read the process table, not a PID's existence alone (UTC, fixed locale)."""
    import calendar
    try:
        result = (runner or subprocess.run)(["/bin/ps", "-p", str(pid), "-o", "lstart=", "-o", "comm="],
            env=_native_environment(), stdin=subprocess.DEVNULL, capture_output=True,
            text=True, timeout=5)
        if result.returncode:
            # ps failure is not evidence of absence, even with empty output.
            if result.stderr.strip():
                raise ValueError("process table failed")
            try:
                (kill_probe or os.kill)(pid, 0)
            except ProcessLookupError:
                return None
            raise ValueError("process table failed")
        fields = result.stdout.strip().split(None, 5)
        epoch = calendar.timegm(time.strptime(" ".join(fields[:5]), "%a %b %d %H:%M:%S %Y"))
        if len(fields) != 6 or Path(fields[5]).name != "postgres":
            raise ValueError("not a postmaster")
        return {"start_id": "ps:" + str(epoch), "started_at": epoch}
    except Exception:
        raise RuntimeError("proof native database process identity unavailable") from None


def native_process_gone(ownership, *, process_table=None):
    """Only proven absence or a successfully read different start ID is gone."""
    if not ownership or not ownership.get("pid"):
        return True
    identity = (process_table or native_process_identity)(ownership["pid"])
    if identity is None:
        return True
    if (type(identity) is not dict or type(identity.get("started_at")) is not int
            or identity["started_at"] <= 0
            or not re.fullmatch(r"ps:[0-9]{1,12}", identity.get("start_id", ""))):
        raise RuntimeError("proof native database process identity unavailable")
    return identity["start_id"] != ownership["start_id"]


def _stop_native_process(ownership, *, process_table=None):
    # The data path may have vanished. Never signal a reused PID or infer death
    # from an unreadable process table. SIGINT is PostgreSQL's fast shutdown.
    if native_process_gone(ownership, process_table=process_table):
        return True
    if not native_process_gone(ownership, process_table=process_table):
        try:
            os.kill(ownership["pid"], signal.SIGINT)
        except ProcessLookupError:
            return True
    deadline = time.monotonic() + NATIVE_TIMEOUT
    while not native_process_gone(ownership, process_table=process_table):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def _native_paths(run_root):
    root = Path(run_root)
    # Refuse links at every ancestor, including a vanished root after cleanup.
    if not root.is_absolute() or any(p.is_symlink() for p in (root, *root.parents)):
        raise RuntimeError("proof native database path refused")
    owned = root / "postgres"
    if owned.is_symlink() or (owned / "data").is_symlink() or (owned / "socket").is_symlink():
        raise RuntimeError("proof native database path refused")
    return owned, owned / "data", owned / "owner.json"


def _native_marker(run_id, run_root, port, *, owned=None):
    if owned is None:
        owned, data, marker = _native_paths(run_root)
    marker = owned / "owner.json"
    if any(path.is_symlink() for path in (owned, marker, owned / "data", owned / "socket")):
        raise RuntimeError("proof native database ownership refused")
    try:
        with marker.open("rb") as stream:
            raw = stream.read(4097)
        if len(raw) > 4096:
            raise ValueError("marker size")
        value = json.loads(raw)
        if (type(value) is not dict or set(value) != {"run_id", "kind", "data_dir", "port"}
                or value != {"run_id": run_id, "kind": "native", "data_dir": NATIVE_DATA, "port": port}):
            raise ValueError("marker mismatch")
        return value
    except Exception:
        raise RuntimeError("proof native database ownership refused") from None


def native_inventory(run_id, run_root, port, *, process_table=None):
    _validate(run_id)
    if type(port) is not int or not 1 <= port <= 65535:
        raise RuntimeError("proof native database port refused")
    owned, data, marker = _native_paths(run_root)
    if not owned.exists():
        return None
    value = _native_marker(run_id, run_root, port)
    pidfile = data / "postmaster.pid"
    if pidfile.is_symlink():
        raise RuntimeError("proof native database identity refused")
    value.update(pid=0, start_id="", started_at=0)
    try:
        with pidfile.open("r", encoding="utf-8") as stream:
            raw = stream.read(4097)
    except FileNotFoundError:
        return value  # Owned directory remains; absence is not removal.
    try:
        lines = raw.splitlines()
        pid, epoch = int(lines[0]), int(lines[2])
        if (len(raw) > 4096 or pid <= 0 or epoch <= 0 or Path(lines[1]) != data
                or int(lines[3]) != port or lines[4] != str(owned / "socket")
                or lines[5] != "127.0.0.1"):
            raise ValueError("postmaster mismatch")
        identity = (process_table or native_process_identity)(pid)
        if (identity is None or type(identity.get("started_at")) is not int
                or abs(identity["started_at"] - epoch) > 2
                or not re.fullmatch(r"ps:[0-9]{1,12}", identity.get("start_id", ""))):
            raise ValueError("postmaster identity mismatch")
        value.update(pid=pid, start_id=identity["start_id"], started_at=epoch)
        return value
    except Exception:
        raise RuntimeError("proof native database identity refused") from None


def _remove_native_directory(run_id, run_root, port):
    owned, data, marker = _native_paths(run_root)
    validated = owned.lstat()
    if not stat.S_ISDIR(validated.st_mode):
        return False
    _native_marker(run_id, run_root, port)
    tombstone = owned.with_name("postgres.removing-" + secrets.token_hex(16))
    # Move the selected directory before inspecting it again. A replacement
    # cannot inherit the validated inode merely by occupying the same path.
    owned.rename(tombstone)
    moved = tombstone.lstat()
    if (not stat.S_ISDIR(moved.st_mode)
            or (moved.st_dev, moved.st_ino) != (validated.st_dev, validated.st_ino)):
        return False
    _native_marker(run_id, run_root, port, owned=tombstone)
    shutil.rmtree(tombstone)
    return not tombstone.exists() and not tombstone.is_symlink()


def cleanup_native(run_id, run_root, port, ownership, *, binaries=None, runner=None,
                   process_table=None, environ=None):
    """A captured private identity authorizes stop; a marker authorizes removal."""
    try:
        table = process_table or native_process_identity
        observed = native_inventory(run_id, run_root, port, process_table=table)
        if observed is None:
            # Recheck a captured PID even if the directory was removed externally.
            if any(Path(run_root).glob("postgres.removing-*")):
                return False
            return _stop_native_process(ownership, process_table=table)
        if not ownership or observed != ownership:
            return False
        owned, data, marker = _native_paths(run_root)
        pid = ownership["pid"]
        if pid:
            binaries = binaries or resolve_native_binaries(environ=environ, runner=runner)
            # Re-read immediately before pg_ctl consumes postmaster.pid.
            if native_inventory(run_id, run_root, port, process_table=table) != ownership:
                return False
            _native_command([binaries["pg_ctl"], "stop", "-D", str(data), "-m", "fast",
                             "-w", "-t", str(NATIVE_TIMEOUT)], runner=runner, environ=environ)
            if not native_process_gone(ownership, process_table=table):
                return False
        after = native_inventory(run_id, run_root, port, process_table=table)
        if after is None or after["pid"]:
            return False
        if not _remove_native_directory(run_id, run_root, port):
            return False
        return native_inventory(run_id, run_root, port, process_table=table) is None
    except Exception:
        return False


class NativePgAuthority(PgAuthority):
    """Native PG16, using the same binary transaction readiness as the relay."""
    def __init__(self, run_id, *, run_root, port, resolver=None, runner=None, process_table=None,
                 environ=None):
        _validate(run_id)
        if type(port) is not int or not 1 <= port <= 65535:
            raise RuntimeError("proof native database port refused")
        self.run_id, self.run_root, self.port = run_id, Path(run_root), port
        self.address = ("127.0.0.1", port)
        self._password = secrets.token_urlsafe(32)
        self._resolver, self._runner = resolver or resolve_native_binaries, runner
        self._process_table, self._environ = process_table or native_process_identity, environ
        self._binaries, self._ownership = None, None
        self._created, self._start_attempted = False, False

    def __repr__(self):
        return "<native project database authority>"

    @classmethod
    def start(cls, run_id, **seams):
        # Exclusive mkdir is the native collision arbiter; no global temp lock.
        authority = cls(run_id, **seams)
        return authority._start()

    def _command(self, argv):
        return _native_command(argv, runner=self._runner, environ=self._environ)

    def _inventory(self):
        return native_inventory(self.run_id, self.run_root, self.port, process_table=self._process_table)

    def _start(self):
        owned, data, marker = _native_paths(self.run_root)
        self._binaries = self._resolver(environ=self._environ, runner=self._runner)
        try:
            owned.mkdir(mode=0o700)
        except FileExistsError:
            raise RuntimeError("run already exists") from None
        self._created = True
        try:
            with os.fdopen(os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w", newline="\n") as stream:
                json.dump({"run_id": self.run_id, "kind": "native", "data_dir": NATIVE_DATA,
                           "port": self.port}, stream, sort_keys=True)
            data.mkdir(mode=0o700)
            sockets = owned / "socket"
            sockets.mkdir(mode=0o700)
            password_file = owned / "password"
            try:
                with os.fdopen(os.open(password_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as stream:
                    stream.write((self._password + "\n").encode())
                self._command([self._binaries["initdb"], "-D", str(data), "-U", "postgres",
                    "--auth-local=scram-sha-256", "--auth-host=scram-sha-256", "--pwfile", str(password_file),
                    "--encoding=UTF8", "--locale=C"])
            finally:
                password_file.unlink(missing_ok=True)
            # Config avoids pg_ctl -o's shell-like option parsing entirely.
            socket_value = str(sockets).replace("\\", "\\\\").replace("'", "''")
            with (data / "postgresql.conf").open("a", encoding="utf-8", newline="\n") as stream:
                stream.write("\nlisten_addresses = '127.0.0.1'\nport = " + str(self.port)
                    + "\nunix_socket_directories = '" + socket_value + "'\nunix_socket_permissions = 0700\n")
            self._start_attempted = True
            self._command([self._binaries["pg_ctl"], "start", "-D", str(data), "-p", self._binaries["postgres"],
                "-l", str(owned / "postgres.log"), "-w", "-t", str(NATIVE_TIMEOUT)])
            self._ownership = self._inventory()
            if not self._ownership or not self._ownership["pid"]:
                raise RuntimeError("postmaster identity missing")
            # CREATE DATABASE cannot run in a transaction. Password stays in libpq kwargs.
            with self._connect_database("postgres") as conn:
                if type(conn.info.server_version) is not int or conn.info.server_version // 10000 != 16:
                    raise RuntimeError("proof native database requires PostgreSQL 16")
                conn.autocommit = True
                conn.execute("CREATE DATABASE leaf")
            deadline = time.monotonic() + NATIVE_TIMEOUT
            while True:
                try:
                    self.readiness()
                    return self
                except RuntimeError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("proof native database readiness timed out") from None
                    time.sleep(0.2)
        except BaseException:
            if not self._cleanup_created():
                raise RuntimeError("proof native database start and cleanup failed") from None
            raise RuntimeError("proof native database start failed") from None

    def _connect_database(self, database):
        args = self._connect_args()
        args["dbname"] = database
        return _libpq_connect(args, native=True)

    def _connect(self):
        return self._connect_database("leaf")

    def dsn(self):
        return f"postgresql://postgres:{quote(self._password, safe='')}@127.0.0.1:{self.port}/leaf"

    def _stop_transport(self):
        # Native mode has no relay; deletion still requires a captured identity.
        return None

    def _cleanup_created(self):
        try:
            if not self._created:
                return self._inventory() is None
            ownership = self._ownership or self._inventory()
            # A failed start without a PID cannot prove it did not spawn a child.
            if self._start_attempted and (not ownership or not ownership["pid"]):
                return False
            return cleanup_native(self.run_id, self.run_root, self.port, ownership,
                binaries=self._binaries, runner=self._runner, process_table=self._process_table,
                environ=self._environ)
        except Exception:
            return False

    def stop(self):
        return self._cleanup_created()


def smoke(run_id):
    """Exercise real libpq cancellation and binary transactions; timings in seconds."""
    result = {"run_id": run_id, "timings_s": {}}
    started = time.perf_counter()
    authority = PgAuthority.start(run_id)
    result["timings_s"]["start"] = time.perf_counter() - started
    try:
        begin = time.perf_counter()
        result["readiness"] = authority.readiness()
        result["timings_s"]["readiness"] = time.perf_counter() - begin
        barrier = threading.Barrier(4)
        def transaction(index):
            begin = time.perf_counter()
            with authority._connect() as conn:
                with conn.transaction():
                    barrier.wait(timeout=20)
                    value = conn.execute("SELECT %s::int", (index,)).fetchone()[0]
                    assert value == index
            return time.perf_counter() - begin
        begin = time.perf_counter()
        with ThreadPoolExecutor(max_workers=4) as pool:
            result["concurrent_transaction_s"] = list(pool.map(transaction, range(4)))
        result["timings_s"]["four_connections"] = time.perf_counter() - begin
        begin = time.perf_counter()
        payload = secrets.token_bytes(1024 * 1024)
        with authority._connect() as conn:
            with conn.transaction():
                conn.execute("CREATE TABLE proof_binary (payload bytea NOT NULL)")
                conn.execute("INSERT INTO proof_binary VALUES (%s)", (payload,))
                assert bytes(conn.execute("SELECT payload FROM proof_binary").fetchone()[0]) == payload
        result["round_trip_bytes"] = len(payload)
        result["timings_s"]["one_mib"] = time.perf_counter() - begin
        with authority._connect() as conn:
            outcome = {}
            def sleeper():
                begin = time.perf_counter()
                try:
                    conn.execute("SELECT pg_sleep(30)")
                except Exception as exc:
                    outcome["error_class"] = type(exc).__name__
                outcome["sleep_elapsed_s"] = time.perf_counter() - begin
            thread = threading.Thread(target=sleeper)
            thread.start()
            # Observe the sleep on the server before cancelling it from this thread.
            with authority._connect() as observer:
                deadline = time.monotonic() + 15
                while True:
                    observer.commit()
                    active = observer.execute(
                        "SELECT wait_event FROM pg_stat_activity WHERE pid = %s",
                        (conn.info.backend_pid,)).fetchone()
                    if active and active[0] == "PgSleep":
                        break
                    if time.monotonic() > deadline:
                        raise RuntimeError("sleep not observed")
                    time.sleep(0.02)
            begin = time.perf_counter()
            conn.cancel()
            outcome["cancel_call_s"] = time.perf_counter() - begin
            thread.join(timeout=10)
            outcome["cancel_to_error_s"] = time.perf_counter() - begin
            assert not thread.is_alive() and outcome["error_class"] == "QueryCanceled"
            conn.rollback()
            result["cancellation"] = outcome
        result["ok"] = True
    except Exception:
        raise RuntimeError("proof database smoke failed") from None
    finally:
        begin = time.perf_counter()
        result["removed"] = authority.stop()
        result["timings_s"]["stop"] = time.perf_counter() - begin
        result["timings_s"]["total"] = time.perf_counter() - started
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("smoke", "cleanup"))
    parser.add_argument("--run-id", required=True)
    args = parser.parse_args()
    try:
        result = smoke(args.run_id) if args.command == "smoke" else cleanup(args.run_id)
    except Exception:
        print(json.dumps({"ok": False, "error": "proof database operation failed"}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
