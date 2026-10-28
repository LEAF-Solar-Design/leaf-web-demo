"""Hermetic PP-2 contract tests: local binary children and loopback only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from urllib.parse import quote

import pytest

import proof_project_pg as pg

RUN = "pp2-test-1234"
ECHO = "import os\nwhile True:\n d=os.read(0,65536)\n if not d: break\n os.write(1,d)\n"


def wait_for(predicate):
    deadline = time.monotonic() + 10
    while not predicate():
        assert time.monotonic() < deadline, "condition did not become true"
        time.sleep(0.01)


class FakeDocker:
    def __init__(self):
        self.calls = []
        self.rows = {"containers": [], "networks": []}
        self.envpaths = []
        self.passwords = []

    def __call__(self, args):
        self.calls.append(list(args))
        if "--env-file" in args:
            path = Path(args[args.index("--env-file") + 1])
            self.envpaths.append(path)
            self.passwords.append(path.read_text().strip().split("=", 1)[1])
            if os.name != "nt":
                assert path.stat().st_mode & 0o077 == 0
        if args[:2] == ["ps", "-a"]:
            return "\n".join(map(json.dumps, self.rows["containers"]))
        if args[:2] == ["network", "ls"]:
            return "\n".join(map(json.dumps, self.rows["networks"]))
        return ""


@contextmanager
def tcp_echo():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(64)
    listener.settimeout(0.1)
    closing = threading.Event()
    clients, workers = [], []
    def echo(client):
        with client:
            try:
                while data := client.recv(65536):
                    client.sendall(data)
            except OSError:
                pass
    def accept():
        while not closing.is_set():
            try:
                client, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            clients.append(client)
            worker = threading.Thread(target=echo, args=(client,), daemon=True)
            workers.append(worker)
            worker.start()
    thread = threading.Thread(target=accept, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()
    finally:
        closing.set()
        listener.close()
        thread.join(timeout=2)
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            client.close()
        for worker in workers:
            worker.join(timeout=2)


@pytest.fixture
def relay():
    created = []
    servers = []
    def make(code=None):
        children, calls = [], []
        server = tcp_echo()
        address = server.__enter__()
        servers.append(server)
        def spawn(argv, **kwargs):
            calls.append((argv, kwargs["env"]))
            # Keep the production pipes, buffering, stderr and environment shape.
            # The local Windows interpreter needs SYSTEMROOT for Winsock; the
            # production interpreter runs inside the Linux relay container.
            if os.name == "nt":
                kwargs["env"] = {**kwargs["env"], "SYSTEMROOT": os.environ["SYSTEMROOT"]}
            relay_code = argv[argv.index("-c") + 1] if code is None else code
            child = subprocess.Popen([sys.executable, "-B", "-u", "-c", relay_code,
                                      address[0], str(address[1])], **kwargs)
            children.append(child)
            return child
        authority = pg.PgAuthority(RUN, docker=FakeDocker(), spawn=spawn)
        authority._listen()
        created.append(authority)
        return authority, children, calls
    yield make
    for authority in created:
        authority.stop()
        wait_for(lambda: not authority._connections)
    for server in servers:
        server.__exit__(None, None, None)


def exchange(address, payload):
    with socket.create_connection(address, timeout=10) as client:
        # Send concurrently so a MiB never depends on OS pipe/socket capacity.
        failures = []
        def send():
            try:
                client.sendall(payload)
            except Exception as exc:
                failures.append(exc)
        writer = threading.Thread(target=send)
        writer.start()
        data = bytearray()
        while len(data) < len(payload):
            chunk = client.recv(min(65536, len(payload) - len(data)))
            assert chunk, "premature EOF"
            data.extend(chunk)
        writer.join(timeout=10)
        assert not writer.is_alive() and not failures
        assert bytes(data) == payload


def test_pp2_byte_transparency(relay):
    authority, _, _ = relay()
    exchange(authority.address, bytes(range(256)))
    exchange(authority.address, secrets.token_bytes(1024 * 1024))


def test_pp2_eight_concurrent_clients(relay):
    authority, _, _ = relay()
    barrier = threading.Barrier(8)
    def client(index):
        barrier.wait(timeout=10)
        exchange(authority.address, bytes([index]) * 131072 + bytes(range(256)))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(client, range(8)))


def test_pp2_client_disconnect(relay):
    authority, children, _ = relay()
    client = socket.create_connection(authority.address, timeout=5)
    wait_for(lambda: len(children) == 1)
    assert children[0].poll() is None
    client.close()
    wait_for(lambda: children[0].poll() is not None)
    wait_for(lambda: not authority._connections)


def test_pp2_child_exit(relay):
    authority, children, _ = relay("import sys; sys.stdin.buffer.read(1)")
    with socket.create_connection(authority.address, timeout=5) as client:
        client.sendall(b"x")
        assert client.recv(1) == b""
    wait_for(lambda: len(children) == 1 and children[0].poll() is not None)


def test_pp2_connection_cap(relay):
    authority, children, _ = relay()
    clients = []
    try:
        for _ in range(32):
            clients.append(socket.create_connection(authority.address, timeout=5))
        wait_for(lambda: len(children) == 32)
        with socket.create_connection(authority.address, timeout=2) as extra:
            assert extra.recv(1) == b""
        assert len(children) == 32
        assert all(child.poll() is None for child in children)
        clients.pop().close()
        wait_for(lambda: len(authority._connections) == 31)
        exchange(authority.address, b"slot reused")
        assert len(children) == 33
    finally:
        for client in clients:
            client.close()


def test_pp2_loopback_bind(relay):
    authority, _, _ = relay()
    assert authority._listener.getsockname()[0] == authority.address[0] == "127.0.0.1"
    addresses = {row[4][0] for row in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    addresses = {address for address in addresses if not address.startswith("127.") and address != "0.0.0.0"}
    if not addresses:
        pytest.skip("PP2 host has no non-loopback IPv4 address")
    for address in addresses:
        with pytest.raises(OSError):
            socket.create_connection((address, authority.address[1]), timeout=0.5)


def row(name, resource_id, run=RUN, owner=pg.OWNER):
    return {"ID": resource_id, "Names": name, "Name": name,
            "Labels": f"{pg.RUN_LABEL}={run},{pg.OWNER_LABEL}={owner}"}


def test_pp2_docker_argv(relay):
    docker = FakeDocker()
    authority = pg.PgAuthority(RUN, docker=docker)
    authority._provision()
    name = "leafproof-" + RUN
    labels = ["--label", f"{pg.RUN_LABEL}={RUN}", "--label", f"{pg.OWNER_LABEL}={pg.OWNER}"]
    assert docker.calls == [
        ["network", "create", "--internal", *labels, name],
        ["run", "-d", "--name", name + "-pg", *labels, "--network", name,
         "--tmpfs", "/var/lib/postgresql/data:rw", "--env-file", str(docker.envpaths[0]),
         "--env", "POSTGRES_DB=leaf", "postgres:16"],
        ["run", "-d", "--name", name + "-relay", *labels, "--network", name,
         "python:3.12", "sleep", "infinity"],
    ]
    assert docker.passwords == [authority._password]
    assert not docker.envpaths[0].exists()
    provision_calls = list(docker.calls)
    local, _, calls = relay()
    exchange(local.address, b"exec argv")
    assert calls[0][0] == ["docker", "exec", "-i", name + "-relay", "python3", "-u", "-c",
                           pg.RELAY_CODE, name + "-pg", "5432"]
    docker.calls.clear()
    docker.rows["containers"] = [row(name + "-pg", "pg-id"), row(name + "-relay", "relay-id")]
    docker.rows["networks"] = [row(name, "net-id")]
    assert pg.cleanup(RUN, docker=docker) == {"containers": ["pg-id", "relay-id"], "networks": ["net-id"]}
    expected = []
    for command in (["ps", "-a"], ["network", "ls"]):
        expected += [[*command, "--filter", f"label={pg.RUN_LABEL}={RUN}", "--filter",
                      f"label={pg.OWNER_LABEL}={pg.OWNER}", "--format", "{{json .}}"],
                     [*command, "--filter", "name=" + name, "--format", "{{json .}}"]]
    expected += [["rm", "-f", "pg-id"], ["rm", "-f", "relay-id"], ["network", "rm", "net-id"]]
    assert docker.calls == expected
    for argv in provision_calls + docker.calls + [calls[0][0]]:
        assert not {"-p", "--publish", "-t"}.intersection(argv)
        assert authority._password not in " ".join(argv)


def test_pp2_run_id_validation(monkeypatch):
    docker = FakeDocker()
    monkeypatch.setattr(pg, "_docker", docker)
    for valid in (RUN, "a" * 8, "0" + "-" * 63):
        assert pg.PgAuthority(valid).run_id == valid
    for invalid in ("short", "UPPERCASE", "-leading-hyphen", "a" * 65, "path/separator", "space here"):
        for action in (pg.PgAuthority, pg.PgAuthority.start, pg.cleanup):
            with pytest.raises(ValueError, match="invalid proof run id"):
                action(invalid)
    assert docker.calls == []


def test_pp2_cleanup_selection():
    docker = FakeDocker()
    prefix = "leafproof-" + RUN
    own = row(prefix + "-pg", "own")
    docker.rows["containers"] = [own, row("leafproof-another-run-pg", "other", run="another-run"),
                                 row("leafpg-oracle", "oracle", owner="other"),
                                 row("unrelated", "wrong-owner", owner="other")]
    docker.rows["networks"] = [row(prefix, "net"), row("leafproof-another-run", "other-net", run="another-run")]
    assert pg.cleanup(RUN, docker=docker) == {"containers": ["own"], "networks": ["net"]}
    assert [call for call in docker.calls if "rm" in call] == [["rm", "-f", "own"], ["network", "rm", "net"]]
    # Both individual labels are required even if the filtered listing lies.
    for bad in (row(prefix + "-pg", "bad", owner="foreign"), row(prefix + "-pg", "bad", run="another-run")):
        docker.rows["containers"] = [bad]
        docker.calls.clear()
        with pytest.raises(RuntimeError):
            pg.cleanup(RUN, docker=docker)
        assert not any("rm" in call for call in docker.calls)


def test_pp2_unlabelled_name_refusal():
    for kind in ("containers", "networks"):
        docker = FakeDocker()
        docker.rows["containers"] = [row("leafproof-" + RUN + "-relay", "owned")]
        bad = row("leafproof-" + RUN + ("-pg" if kind == "containers" else ""), "unlabelled")
        bad["Labels"] = ""
        docker.rows[kind].append(bad)
        with pytest.raises(RuntimeError):
            pg.cleanup(RUN, docker=docker)
        assert not any("rm" in call for call in docker.calls)


def test_pp2_redaction(relay, monkeypatch):
    authority, _, _ = relay()
    secret = authority._password
    assert secret in authority.dsn()
    assert authority.redacted_dsn() == authority.dsn().replace(secret, "***")
    import psycopg
    def fail(*args, **kwargs):
        raise RuntimeError("leaked " + secret)
    monkeypatch.setattr(psycopg, "connect", fail)
    monkeypatch.setattr(pg.subprocess, "run", fail)
    monkeypatch.setattr(pg.secrets, "token_urlsafe", lambda _: secret)
    for action in (authority.readiness, lambda: pg._docker(["ps"]),
                   lambda: pg.cleanup(RUN, docker=fail),
                   lambda: pg.PgAuthority.start(RUN, docker=fail)):
        with pytest.raises(RuntimeError) as error:
            action()
        import traceback
        assert secret not in "".join(traceback.format_exception(error.value))


def test_pp2_no_ambient_dsn(relay, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://ambient-secret@outside/foreign")
    monkeypatch.setenv("PGPASSWORD", "ambient-password")
    monkeypatch.setenv("PGHOST", "outside")
    authority, _, calls = relay()
    exchange(authority.address, b"env isolation")
    recorded = []
    def run(argv, **kwargs):
        recorded.append((argv, kwargs["env"]))
        return SimpleNamespace(returncode=0, stdout="")
    monkeypatch.setattr(pg.subprocess, "run", run)
    pg._docker(["ps", "-a"])
    for argv, env in recorded + calls:
        assert not {"DATABASE_URL", "PGPASSWORD", "PGHOST"}.intersection(env)
        assert set(env) <= {"PATH", "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG",
                            "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH", "DOCKER_API_VERSION"}
        assert "ambient" not in str(argv) + str(env)
    assert "ambient" not in authority.dsn() and "outside" not in authority.dsn()


def test_pp2_stop_closes_children(relay):
    authority, children, _ = relay()
    with socket.create_connection(authority.address, timeout=5) as client:
        wait_for(lambda: len(children) == 1)
        authority.stop()
        assert client.recv(1) == b""
        assert children[0].poll() is not None
    with pytest.raises(OSError):
        socket.create_connection(authority.address, timeout=0.5)
    assert authority.stop() == {"containers": [], "networks": []}


def test_pp2_readiness_transaction(relay, monkeypatch):
    import psycopg
    authority, _, _ = relay()
    events = []
    class Transaction:
        def __enter__(self): events.append("begin")
        def __exit__(self, *args): events.append("commit")
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): events.append("close")
        def transaction(self): return Transaction()
        def execute(self, sql, params):
            assert events[-1] == "begin"
            assert params == (bytes(range(256)),) and "::bytea" in sql
            return SimpleNamespace(fetchone=lambda: (params[0], "16.fake"))
    def connect(**kwargs):
        assert kwargs == authority._connect_args()
        events.append("connect")
        return Connection()
    monkeypatch.setattr(psycopg, "connect", connect)
    for _ in range(2):
        assert authority.readiness() == {"ok": True, "server_version": "16.fake", "round_trip_bytes": 256}
    assert events == ["connect", "begin", "commit", "close"] * 2


def test_pp2_start_failure_cleanup(monkeypatch):
    docker = FakeDocker()
    secret = "fixed-test-secret"
    monkeypatch.setattr(pg.secrets, "token_urlsafe", lambda _: secret)
    original = docker.__call__
    def broken(args):
        result = original(args)
        if args[:2] == ["network", "create"]:
            return "net\n"
        if "--env-file" in args:
            docker.rows["networks"] = [row("leafproof-" + RUN, "net")]
            raise RuntimeError(secret)
        return result
    with pytest.raises(RuntimeError) as error:
        pg.PgAuthority.start(RUN, docker=broken)
    assert secret not in str(error.value)
    assert not docker.envpaths[0].exists()
    assert ["network", "rm", "net"] in docker.calls
    # If the host process cannot spawn, startup still removes the resources.
    docker = FakeDocker()
    def provisioned(args):
        result = docker(args)
        if args[:2] == ["network", "create"]:
            docker.rows["networks"] = [row("leafproof-" + RUN, "spawn-net")]
            return "spawn-net\n"
        return result
    def fail_start(self):
        raise OSError(secret)
    monkeypatch.setattr(pg.multiprocessing.process.BaseProcess, "start", fail_start)
    with pytest.raises(RuntimeError) as error:
        pg.PgAuthority.start(RUN, docker=provisioned)
    assert secret not in str(error.value)
    assert ["network", "rm", "spawn-net"] in docker.calls


def test_pp2_collision_preserved():
    docker = FakeDocker()
    docker.rows["containers"] = [row("leafproof-" + RUN + "-pg", "existing")]
    with pytest.raises(RuntimeError):
        pg.PgAuthority.start(RUN, docker=docker)
    assert not any("rm" in call or "run" in call or "create" in call for call in docker.calls)


def echo_spawn(argv, **kwargs):
    assert argv[:3] == ["docker", "exec", "-i"]
    return subprocess.Popen([sys.executable, "-B", "-u", "-c", ECHO], **kwargs)


def test_pp2_managed_transport_process():
    authority = pg.PgAuthority(RUN, docker=FakeDocker(), spawn=echo_spawn)
    try:
        authority._start_transport()
        process = authority._transport_process
        assert process.pid != os.getpid() and process.is_alive()
        assert authority.address[0] == "127.0.0.1"
        exchange(authority.address, bytes(range(256)) * 4096)
        authority.stop()
        assert not process.is_alive()
        with pytest.raises(OSError):
            socket.create_connection(authority.address, timeout=0.5)
    finally:
        authority.stop()


@pytest.mark.parametrize("variable,value", [
    ("PGHOSTADDR", "127.0.0.2"), ("PGHOST", "outside"), ("PGPORT", "1"),
    ("PGSERVICE", "redirect"), ("PGSSLMODE", "require"), ("PGPASSFILE", "foreign.pass"),
    ("PGPASSWORD", "foreign-password"), ("PGGSSENCMODE", "require"),
], ids=lambda value: value)
def test_pp2_explicit_connection_parameters(variable, value, monkeypatch, tmp_path):
    import psycopg
    service = tmp_path / "pg_service.conf"
    service.write_text("[redirect]\nhost=outside\nhostaddr=127.0.0.2\nport=1\npassword=foreign\n",
                       encoding="utf-8", newline="\n")
    monkeypatch.setenv("PGSERVICEFILE", str(service))
    monkeypatch.setenv(variable, value)
    authority = pg.PgAuthority(RUN, docker=FakeDocker())
    authority.address = ("127.0.0.1", 12345)
    captured = []
    def connect(*args, **kwargs):
        captured.append((args, kwargs))
        raise RuntimeError("stop after capture")
    monkeypatch.setattr(psycopg, "connect", connect)
    with pytest.raises(RuntimeError, match="readiness failed"):
        authority.readiness()
    assert captured == [((), {"host": "127.0.0.1", "hostaddr": "127.0.0.1", "port": 12345,
                              "dbname": "leaf", "user": "postgres", "password": authority._password,
                              "sslmode": "disable", "gssencmode": "disable", "connect_timeout": 5})]


def test_pp2_real_psycopg_ignores_pghostaddr(monkeypatch):
    import psycopg
    assert callable(psycopg.connect)
    with socket.socket() as foreign, socket.socket() as intended:
        try:
            foreign.bind(("127.0.0.2", 0))
        except OSError as exc:
            pytest.skip(f"127.0.0.2 cannot be bound: {exc}")
        foreign.listen(1)
        foreign.settimeout(0.3)
        # Both endpoints close immediately so a redirected startup never waits
        # on authentication; the accepted endpoint alone proves routing.
        intended.bind(("127.0.0.1", foreign.getsockname()[1]))
        intended.listen(1)
        intended.settimeout(0.1)
        received, closing = [], threading.Event()
        def serve(listener, label):
            while not closing.is_set():
                try:
                    client, _ = listener.accept()
                except socket.timeout:
                    continue
                with client:
                    received.append(label)
                return
        threads = [threading.Thread(target=serve, args=(listener, label))
                   for listener, label in ((foreign, "foreign"), (intended, "intended"))]
        for thread in threads:
            thread.start()
        try:
            monkeypatch.setenv("PGHOSTADDR", "127.0.0.2")
            for key in ("PGSERVICE", "PGSERVICEFILE"):
                monkeypatch.delenv(key, raising=False)
            authority = pg.PgAuthority(RUN, docker=FakeDocker())
            authority.address = intended.getsockname()
            with pytest.raises(RuntimeError, match="readiness failed"):
                authority.readiness()
        finally:
            closing.set()
            for thread in threads:
                thread.join(timeout=2)
        assert received == ["intended"], "ambient PGHOSTADDR reached the foreign listener"


class StatefulDocker(FakeDocker):
    def __call__(self, args):
        result = super().__call__(args)
        if args[:2] == ["network", "create"] or args[:2] == ["run", "-d"]:
            kind = "networks" if args[0] == "network" else "containers"
            name = args[-1] if kind == "networks" else args[args.index("--name") + 1]
            if any(resource["Name"] == name for resource in self.rows[kind]):
                raise RuntimeError("resource exists")
            resource_id = name + "-id"
            self.rows[kind].append(row(name, resource_id))
            return resource_id + "\n"
        if "rm" in args:
            kind = "networks" if args[0] == "network" else "containers"
            self.rows[kind] = [resource for resource in self.rows[kind]
                               if args[-1] not in (resource["ID"], resource["Name"])]
        return result


def test_pp2_concurrent_start_preserves_winner(monkeypatch):
    docker = StatefulDocker()
    a_checked, release_a, b_attempted, a_ready = [threading.Event() for _ in range(4)]
    b_calls = []
    name = "leafproof-" + RUN
    def first(args):
        result = docker(args)
        if args[:2] == ["network", "ls"] and "name=" + name in args:
            a_checked.set()
            assert release_a.wait(10)
        return result
    def second(args):
        b_calls.append(args)
        if args[:2] == ["network", "create"]:
            b_attempted.set()
            assert a_ready.wait(10)
            raise RuntimeError("B network creation loses")
        return docker(args)
    monkeypatch.setattr(pg.PgAuthority, "_start_transport", lambda self: None)
    monkeypatch.setattr(pg.PgAuthority, "readiness", lambda self: {"ok": True})
    def start_b():
        try:
            with pytest.raises(RuntimeError) as error:
                pg.PgAuthority.start(RUN, docker=second)
            return str(error.value)
        finally:
            b_attempted.set()
    with ThreadPoolExecutor(max_workers=2) as pool:
        winner = pool.submit(pg.PgAuthority.start, RUN, docker=first)
        try:
            assert a_checked.wait(10)
            loser = pool.submit(start_b)
            assert b_attempted.wait(10)
        finally:
            release_a.set()
        try:
            authority = winner.result(timeout=10)
        finally:
            a_ready.set()
        error = loser.result(timeout=10)
    assert {resource["Name"] for resource in docker.rows["containers"]} == {name + "-pg", name + "-relay"}
    assert [resource["Name"] for resource in docker.rows["networks"]] == [name]
    assert error == "run already exists" and b_calls == []
    authority.stop()


def test_pp2_start_lock_is_held_by_os():
    code = ("import proof_project_pg as pg\n"
            "try:\n"
            " with pg._start_lock('" + RUN + "'): print('acquired')\n"
            "except RuntimeError as exc: print(str(exc))\n")
    def contender():
        return subprocess.run([sys.executable, "-B", "-c", code], cwd=Path(pg.__file__).parent,
                              capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    with pg._start_lock(RUN):
        assert contender() == "run already exists"
    assert contender() == "acquired"


@pytest.mark.parametrize("stage", ["network", "relay", "transport"])
def test_pp2_failed_start_removes_only_created(stage, monkeypatch):
    docker = StatefulDocker()
    name = "leafproof-" + RUN
    foreign = row(name + "-pg", "other-start-pg")
    def fail(args):
        if stage == "network" and args[:2] == ["network", "create"]:
            docker.rows["containers"].append(foreign)
            raise RuntimeError("network refused")
        if stage == "relay" and "--name" in args and name + "-relay" in args:
            docker.rows["containers"].append(row(name + "-relay", "other-start-relay"))
            raise RuntimeError("relay refused")
        return docker(args)
    def fail_transport(self):
        raise RuntimeError("transport refused")
    monkeypatch.setattr(pg.PgAuthority, "_start_transport", fail_transport)
    with pytest.raises(RuntimeError, match="start failed"):
        pg.PgAuthority.start(RUN, docker=fail)
    remaining = [resource["ID"] for resource in docker.rows["containers"]]
    assert remaining == {"network": ["other-start-pg"], "relay": ["other-start-relay"], "transport": []}[stage]
    assert docker.rows["networks"] == []
    removed = [args[-1] for args in docker.calls if "rm" in args]
    assert all(not resource.startswith("other-start") for resource in removed)
    assert len(removed) == {"network": 0, "relay": 2, "transport": 3}[stage]


def populate_runs(docker):
    for run in (RUN, RUN + "-child"):
        name = "leafproof-" + run
        docker.rows["containers"].extend([row(name + suffix, name + suffix + "-id", run=run)
                                          for suffix in ("-pg", "-relay")])
        docker.rows["networks"].append(row(name, name + "-id", run=run))


def assert_only_child_remains(docker):
    assert len(docker.rows["containers"]) == 2 and len(docker.rows["networks"]) == 1
    assert all(resource["Name"].startswith("leafproof-" + RUN + "-child")
               for resources in docker.rows.values() for resource in resources)
    assert {args[-1] for args in docker.calls if "rm" in args} == {
        "leafproof-" + RUN + suffix + "-id" for suffix in ("", "-pg", "-relay")}


def test_pp2_cleanup_preserves_child_run():
    docker = StatefulDocker()
    populate_runs(docker)
    removed = pg.cleanup(RUN, docker=docker)
    assert len(removed["containers"]) == 2 and len(removed["networks"]) == 1
    assert_only_child_remains(docker)


def test_pp2_mid_smoke_failure_cleans_short_run(monkeypatch):
    docker = StatefulDocker()
    populate_runs(docker)
    authority = pg.PgAuthority(RUN, docker=docker)
    monkeypatch.setattr(pg.PgAuthority, "start", lambda run: authority)
    def fail():
        raise RuntimeError("mid-smoke readiness failed")
    monkeypatch.setattr(authority, "readiness", fail)
    with pytest.raises(RuntimeError, match="smoke failed"):
        pg.smoke(RUN)
    assert_only_child_remains(docker)


def test_pp2_readiness_rejects_byte_mismatch(monkeypatch):
    import psycopg
    authority = pg.PgAuthority(RUN, docker=FakeDocker())
    authority.address = ("127.0.0.1", 12345)
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def transaction(self): return self
        def execute(self, sql, params):
            return SimpleNamespace(fetchone=lambda: (params[0].replace(b"\xff", b"?"), "16.fake"))
    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: Connection())
    with pytest.raises(RuntimeError, match="readiness failed"):
        authority.readiness()


# Independent pin of PostgreSQL 18 section 32.15 (2026-10-13), not derived from
# the implementation tuple: omitting a variable must fail these rows too.
LIBPQ_ENV_NAMES = tuple("""
PGHOST PGSSLNEGOTIATION PGHOSTADDR PGPORT PGDATABASE PGUSER PGPASSWORD PGPASSFILE
PGREQUIREAUTH PGCHANNELBINDING PGSERVICE PGSERVICEFILE PGOPTIONS PGAPPNAME PGSSLMODE
PGREQUIRESSL PGSSLCOMPRESSION PGSSLCERT PGSSLKEY PGSSLCERTMODE PGSSLROOTCERT PGSSLCRL
PGSSLCRLDIR PGSSLSNI PGREQUIREPEER PGSSLMINPROTOCOLVERSION PGSSLMAXPROTOCOLVERSION
PGGSSENCMODE PGKRBSRVNAME PGGSSLIB PGGSSDELEGATION PGCONNECT_TIMEOUT PGCLIENTENCODING
PGTARGETSESSIONATTRS PGLOADBALANCEHOSTS PGMINPROTOCOLVERSION PGMAXPROTOCOLVERSION
PGDATESTYLE PGTZ PGGEQO PGSYSCONFDIR PGLOCALEDIR
""".split())
# Measured with psycopg 3.3.4 / libpq 180003 and every above variable absent.
CLEAN_STARTUP = {"user": "postgres", "database": "leaf"}


@pytest.fixture
def clean_pg_env(monkeypatch):
    for name in LIBPQ_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


@contextmanager
def startup_listener(host="127.0.0.1", response="close"):
    """Capture real protocol-3 startup frames, never credentials or SQL.

    Closing (or sending an ErrorResponse) deliberately makes connect fail.
    Packet evidence, not connection success, is the oracle.
    """
    packets, snapshots, errors = [], [], []
    closing = threading.Event()
    with socket.socket() as listener:
        listener.bind((host, 0))
        listener.listen(8)
        listener.settimeout(0.05)
        def serve():
            try:
                while not closing.is_set():
                    try:
                        client, _ = listener.accept()
                    except socket.timeout:
                        continue
                    with client:
                        client.settimeout(2)
                        def read_exact(count):
                            data = b""
                            while len(data) < count:
                                part = client.recv(count - len(data))
                                if not part:
                                    raise AssertionError("truncated startup packet")
                                data += part
                            return data
                        size, = struct.unpack("!I", read_exact(4))
                        assert 8 <= size <= 65536
                        body = read_exact(size - 4)
                        assert struct.unpack("!I", body[:4]) == (196608,)
                        assert body.endswith(b"\0\0")
                        fields = body[4:-2].split(b"\0")
                        assert len(fields) % 2 == 0
                        keys = [value.decode("utf-8") for value in fields[::2]]
                        assert len(keys) == len(set(keys))
                        packets.append(dict(zip(keys, (v.decode("utf-8") for v in fields[1::2]))))
                        snapshots.append(tuple(name for name in LIBPQ_ENV_NAMES if name in os.environ))
                        if response == "reject":
                            error = b"SFATAL\0C08006\0Mpp2d listener refused\0\0"
                            client.sendall(b"E" + struct.pack("!I", len(error) + 4) + error)
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=serve)
        thread.start()
        try:
            yield SimpleNamespace(address=listener.getsockname(), packets=packets, snapshots=snapshots)
        finally:
            closing.set()
            thread.join(timeout=3)
            assert not thread.is_alive(), "startup listener did not stop"
            assert not errors, "startup listener failed to decode a packet"


def packet_authority(address):
    authority = pg.PgAuthority(RUN, docker=FakeDocker())
    authority.address = address
    return authority


def test_pp2d_r1_missing_service(clean_pg_env, monkeypatch, tmp_path):
    missing = tmp_path / "absent.conf"
    assert not missing.exists()
    monkeypatch.setenv("PGSERVICE", "missing")
    monkeypatch.setenv("PGSERVICEFILE", str(missing))
    with startup_listener() as genuine:
        with pytest.raises(RuntimeError, match="readiness failed"):
            packet_authority(genuine.address).readiness()
    assert genuine.packets == [CLEAN_STARTUP]


def test_pp2d_r2_real_service(clean_pg_env, monkeypatch, tmp_path):
    # 127.0.0.2 was bindable on the measurement host; no skip or fallback needed.
    with startup_listener("127.0.0.2") as foreign, startup_listener() as genuine:
        service = tmp_path / "pg_service.conf"
        service.write_text("[redirect]\nhost=127.0.0.2\nhostaddr=127.0.0.2\n"
                           f"port={foreign.address[1]}\napplication_name=service-injected\n",
                           encoding="utf-8", newline="\n")
        monkeypatch.setenv("PGSERVICE", "redirect")
        monkeypatch.setenv("PGSERVICEFILE", str(service))
        with pytest.raises(RuntimeError, match="readiness failed"):
            packet_authority(genuine.address).readiness()
    assert foreign.packets == []
    # Explicit host parameters already defeat service redirection on the baseline;
    # the service's unpinned application_name is the measured regression oracle.
    assert genuine.packets == [CLEAN_STARTUP]


@pytest.mark.parametrize("form", ["readiness", "dsn"])
def test_pp2d_r3_options(clean_pg_env, monkeypatch, form):
    import psycopg
    monkeypatch.setenv("PGOPTIONS", "-c default_transaction_read_only=on -c statement_timeout=1")
    with startup_listener() as genuine:
        authority = packet_authority(genuine.address)
        if form == "readiness":
            with pytest.raises(RuntimeError, match="readiness failed"):
                authority.readiness()
        else:
            with pytest.raises(psycopg.OperationalError):
                psycopg.connect(authority.dsn())
    assert len(genuine.packets) == 1
    assert "options" not in genuine.packets[0]
    assert genuine.packets == [CLEAN_STARTUP]


def test_pp2d_r4_exact_startup(clean_pg_env, monkeypatch):
    with startup_listener() as clean:
        with pytest.raises(RuntimeError, match="readiness failed"):
            packet_authority(clean.address).readiness()
    assert clean.packets == [CLEAN_STARTUP]
    hostile = {
        "PGAPPNAME": "ambient-app", "PGCLIENTENCODING": "LATIN1",
        "PGDATESTYLE": "SQL, DMY", "PGTZ": "Pacific/Honolulu", "PGUSER": "foreign",
        "PGDATABASE": "foreign", "PGPORT": "1", "PGHOST": "outside.invalid",
        "PGHOSTADDR": "127.0.0.2", "PGPASSWORD": secrets.token_urlsafe(32),
        "PGSSLMODE": "require", "PGCONNECT_TIMEOUT": "1", "PGTARGETSESSIONATTRS": "read-only",
    }
    for name, value in hostile.items():
        monkeypatch.setenv(name, value)
    with startup_listener() as genuine:
        with pytest.raises(RuntimeError, match="readiness failed"):
            packet_authority(genuine.address).readiness()
    assert genuine.packets == [CLEAN_STARTUP]


@pytest.mark.parametrize("response", ["close", "reject"])
def test_pp2d_r5_environment_restored(clean_pg_env, monkeypatch, response):
    monkeypatch.setenv("PGOPTIONS", "-c statement_timeout=1")
    monkeypatch.setenv("PGAPPNAME", "restore-exact-\u00e9")
    monkeypatch.delenv("PGSERVICEFILE", raising=False)
    before = dict(os.environ)
    with startup_listener(response=response) as genuine:
        with pytest.raises(RuntimeError, match="readiness failed"):
            packet_authority(genuine.address).readiness()
    restored = dict(os.environ) == before
    assert restored, "environment mapping changed (values withheld)"
    assert genuine.packets == [CLEAN_STARTUP]
    assert genuine.snapshots == [()]


def test_pp2d_r6_pre_network_exception(clean_pg_env, monkeypatch):
    import psycopg
    original_connect = psycopg.connect
    snapshots, raised = [], []
    def connect(*args, **kwargs):
        snapshots.append(tuple(name for name in LIBPQ_ENV_NAMES if name in os.environ))
        try:
            return original_connect(*args, **kwargs)
        except psycopg.OperationalError as exc:
            raised.append(exc)
            raise
    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setenv("PGAPPNAME", "restore-before-network")
    before = dict(os.environ)
    authority = packet_authority(("127.0.0.1", "invalid-port"))
    with pytest.raises(psycopg.OperationalError, match="invalid integer value") as error:
        authority._connect()
    assert len(raised) == 1 and error.value is raised[0]
    restored = dict(os.environ) == before
    assert restored, "environment mapping changed (values withheld)"
    assert snapshots == [()]


def test_pp2d_r7_clean_environment():
    from types import MappingProxyType
    assert isinstance(pg._LIBPQ_ENV, tuple)
    assert len(pg._LIBPQ_ENV) == len(set(pg._LIBPQ_ENV))
    assert set(pg._LIBPQ_ENV) == set(LIBPQ_ENV_NAMES)
    kept = {"PATH": "unchanged", "PG_NOT_A_LIBPQ_SETTING": "keep", "OTHER": "\u00e9\n"}
    source = dict(kept, **{name: "ambient" for name in LIBPQ_ENV_NAMES})
    before = dict(source)
    assert pg.clean_libpq_environment(source) == kept
    assert source == before
    assert pg.clean_libpq_environment(MappingProxyType(source)) == kept
    empty_of_pg = pg.clean_libpq_environment(kept)
    assert empty_of_pg == kept and empty_of_pg is not kept
    assert pg.clean_libpq_environment({}) == {}


def test_pp2d_r8_single_connect_site():
    source = Path(pg.__file__).read_text(encoding="utf-8")
    assert source.count("psycopg.connect(") == 1


@pytest.mark.parametrize("entry", ["readiness", "smoke"])
def test_pp2d_r9_all_callers_isolated(monkeypatch, entry):
    import psycopg
    snapshots = []
    sleeping, cancelled = threading.Event(), threading.Event()
    class QueryCanceled(Exception):
        pass
    class Connection:
        info = SimpleNamespace(backend_pid=42)
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def transaction(self): return self
        def commit(self): pass
        def rollback(self): pass
        def cancel(self): cancelled.set()
        def execute(self, sql, params=None):
            value = None
            if sql.startswith("SELECT %s::bytea"):
                value = (params[0], "18.fake")
            elif sql == "SELECT %s::int":
                value = (params[0],)
            elif sql.startswith("INSERT"):
                self.payload = params[0]
            elif sql == "SELECT payload FROM proof_binary":
                value = (self.payload,)
            elif sql == "SELECT pg_sleep(30)":
                sleeping.set()
                assert cancelled.wait(3), "fake cancellation did not arrive"
                raise QueryCanceled()
            elif sql.startswith("SELECT wait_event"):
                assert sleeping.wait(3), "fake sleeper did not start"
                value = ("PgSleep",)
            return SimpleNamespace(fetchone=lambda: value)
    def connect(*args, **kwargs):
        snapshots.append(tuple(name for name in LIBPQ_ENV_NAMES if name in os.environ))
        return Connection()
    for name in LIBPQ_ENV_NAMES:
        monkeypatch.setenv(name, "ambient")
    before = dict(os.environ)
    monkeypatch.setattr(psycopg, "connect", connect)
    authority = packet_authority(("127.0.0.1", 12345))
    monkeypatch.setattr(pg.PgAuthority, "start", lambda run: authority)
    monkeypatch.setattr(authority, "stop", lambda: {})
    if entry == "readiness":
        assert authority.readiness()["ok"]
        expected = 1
    else:
        assert pg.smoke(RUN)["ok"]
        expected = 8  # readiness, four transactions, binary, sleeper, observer
    restored = dict(os.environ) == before
    assert restored, "environment mapping changed (values withheld)"
    assert snapshots == [()] * expected


def test_pp2d_dsn_strings():
    authority = packet_authority(("127.0.0.1", 12345))
    suffix = "/leaf?hostaddr=127.0.0.1&sslmode=disable&gssencmode=disable&connect_timeout=5&options="
    expected = f"postgresql://postgres:{quote(authority._password, safe='')}@127.0.0.1:12345{suffix}"
    matches = authority.dsn() == expected
    assert matches, "DSN contract changed (credential withheld)"
    assert authority.redacted_dsn() == f"postgresql://postgres:***@127.0.0.1:12345{suffix}"


def test_pp2d_r10_clean_environment_any_case():
    kept = {"PATH": "unchanged", "OTHER": "keep", "PG_NOT_A_LIBPQ_SETTING": "keep", 7: "non-text key"}
    source = dict(kept)
    for name in LIBPQ_ENV_NAMES:
        source[name.lower()] = "lower"
        source[name.title()] = "title"
        source[name[:2].lower() + name[2:]] = "mixed"
    source["pGsErViCe"] = "alternating"
    before = dict(source)
    cleaned = pg.clean_libpq_environment(source)
    assert cleaned == kept and cleaned is not kept
    assert source == before


def test_pp2d_r11_child_environment_has_no_libpq_name():
    names = set(LIBPQ_ENV_NAMES)
    base = {k: v for k, v in os.environ.items() if k.upper() not in names}
    base.update({"pgservice": "review-missing", "PgServiceFile": "C:/review-does-not-exist/service.conf",
                 "pghost": "ambient-host", "pgOptions": "-c search_path=ambient"})
    code = ("import json, os, sys; names = set(json.loads(sys.argv[1]));"
            " print(json.dumps(sorted(k for k in os.environ if k.upper() in names)))")
    child = subprocess.run([sys.executable, "-c", code, json.dumps(sorted(names))],
                           env=pg.clean_libpq_environment(base), capture_output=True, text=True,
                           timeout=60, check=True)
    assert json.loads(child.stdout) == []


def test_d_b_start_cleanup_after_transport_failure(monkeypatch):
    authority = pg.PgAuthority(RUN, docker=FakeDocker())
    events = []
    def provision():
        authority._created = {"containers": ["a" * 12, "b" * 12], "networks": ["c" * 12]}
        events.append("provision")
    def start_transport():
        events.append("start-transport")
        raise OSError("fake")
    def stop_transport():
        events.append("stop-transport")
        raise OSError("fake")
    def cleanup_created():
        events.append("cleanup-created")
        authority._created = {"containers": [], "networks": []}
    monkeypatch.setattr(authority, "_provision", provision)
    monkeypatch.setattr(authority, "_start_transport", start_transport)
    monkeypatch.setattr(authority, "_stop_transport", stop_transport)
    monkeypatch.setattr(authority, "_cleanup_created", cleanup_created)
    with pytest.raises(RuntimeError) as error:
        authority._start()
    assert str(error.value) == "proof database start and cleanup failed"
    assert error.value.__cause__ is None and error.value.__suppress_context__
    assert events == ["provision", "start-transport", "stop-transport", "cleanup-created"]
    assert authority._created == {"containers": [], "networks": []}
