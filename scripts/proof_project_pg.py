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
            try:
                self._stop_transport()
                self._cleanup_created()
            except Exception:
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
        import psycopg
        # This CLI helper has a single-threaded readiness path. Smoke also
        # connects from worker threads, so serialize the process-wide environment
        # change for just this call; transactions remain concurrent afterwards.
        # Embedding alongside unrelated environment readers/writers is unsupported.
        with _CONNECT_LOCK:
            previous = {name: os.environ[name] for name in _LIBPQ_ENV if name in os.environ}
            try:
                for name in _LIBPQ_ENV:
                    os.environ.pop(name, None)
                return psycopg.connect(**self._connect_args())
            finally:
                for name in _LIBPQ_ENV:
                    os.environ.pop(name, None)
                os.environ.update(previous)

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
