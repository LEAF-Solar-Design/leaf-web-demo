from datetime import datetime, timedelta, timezone
import json
import logging
import multiprocessing
import os
from pathlib import Path
import ssl
import time
from uuid import uuid4
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from conftest import Echo, command
from leaf_agent import sign
from leaf_agent.__main__ import main
from leaf_agent.client import Backoff, Client, ClientError, Revoked, secure_opener
from leaf_agent.config import Config, agent_home, atomic_write, validate_origin, write_json
from leaf_agent.core import Agent, isolated
from leaf_agent.ops import Dispatcher


class Clock:
    def __init__(self):
        self.now = 0.0
        self.delays = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        self.delays.append(seconds)


def inline(dispatcher, op, args, timeout, tick):
    return dispatcher.dispatch(op, args)


def test_enroll_poll_result_roundtrip(config, studio, enrolled, registry):
    studio.commands = [command()]
    agent = Agent(config, enrolled, registry, sleep=lambda _: None)
    assert agent.run(max_cycles=1) == 0
    assert studio.results[0]["status"] == "ok"
    assert studio.results[0]["data"]["count"] == 1
    assert studio.heartbeats[0]["packages"] == [registry.get("echo").state()]
    assert config.load() == {"server": studio.origin, "device_id": studio.device_id}
    assert len((config.home / "device.key").read_bytes()) == 32
    if os.name == "posix":
        assert (config.home / "device.key").stat().st_mode & 0o777 == 0o600
    else:
        # Windows has no POSIX 0600 mode; mkstemp creates the identity privately
        # in the owner's application data directory, with inherited Windows ACLs.
        assert not (config.home / "device.key").is_symlink()
    assert len(studio.nonces) == len(studio.seen)


def test_fresh_nonce_on_retry_and_replay_is_rejected(config, studio, enrolled):
    assert enrolled.poll() == []
    _, target, body, headers = studio.seen[-1]
    request = Request(studio.origin + target, headers=headers, method="GET")
    with pytest.raises(HTTPError) as caught:
        studio.opener.open(request, timeout=15)
    with caught.value as response:
        assert json.loads(response.read())["error"]["code"] == "signature_replay"
    assert enrolled.poll() == []
    assert len(studio.nonces) == 2
    assert body == b""


def test_disabled_refuses_every_command_but_keeps_heartbeats(config, studio, enrolled, registry):
    config.pause()
    studio.commands = [command(op, args) for op, args in [("pkg.health", {"package": "echo"}), ("agent.pause", {}), ("agent.update", {}), ("http.proxy", {}), ("unknown", {})]]
    studio.commands.append(command(expires_at="invalid"))
    clock = Clock()
    agent = Agent(config, enrolled, registry, clock=clock, sleep=clock.sleep)
    assert agent.run(max_cycles=65) == 0
    assert len(studio.results) == 6
    assert all(result["status"] == "refused" and result["error"]["code"] == "paused" for result in studio.results)
    assert len(studio.heartbeats) == 3
    assert not (config.home / "echo-count").exists()


@pytest.mark.parametrize("endpoint", ["poll", "results", "heartbeat"])
def test_revocation_on_any_signed_endpoint_wipes_identity(config, studio, enrolled, registry, endpoint):
    agent = Agent(config, enrolled, registry, sleep=lambda _: None, executor=inline)
    if endpoint != "heartbeat":
        agent.next_heartbeat = agent.clock() + 100
    if endpoint == "results":
        agent.execute(command())
    studio.revoked = True
    assert agent.run(max_cycles=1) == 3
    assert not (config.home / "device.key").exists()
    assert not (config.home / "config.json").exists()


def test_completed_command_deduplicates_after_restart(config, studio, enrolled, registry):
    item = command()
    studio.commands = [item]
    agent = Agent(config, enrolled, registry, sleep=lambda _: None, executor=inline)
    assert agent.run(max_cycles=1) == 0
    original = studio.results[-1]
    studio.commands = [item]
    restarted = Agent(config, enrolled, registry, sleep=lambda _: None, executor=inline)
    assert restarted.run(max_cycles=1) == 0
    assert studio.results[-1] == original
    assert (config.home / "echo-count").read_text() == "1"


def test_new_identity_discards_old_pending_results(config, studio, enrolled, registry):
    agent = Agent(config, enrolled, registry, executor=inline)
    agent.execute(command())
    agent.db.close()
    config.save(studio.origin, str(uuid4()))
    restarted = Agent(config, Client(config), registry)
    try:
        assert restarted.db.execute("SELECT COUNT(*) FROM results").fetchone() == (0,)
    finally:
        restarted.db.close()


def test_pause_transition_applies_before_next_command(config, studio, enrolled, registry):
    studio.commands = [command("agent.pause", {}), command("pkg.health")]
    agent = Agent(config, enrolled, registry, sleep=lambda _: None)
    assert agent.run(max_cycles=1) == 0
    assert studio.results[0]["status"] == "ok"
    assert studio.results[1]["error"]["code"] == "paused"
    assert not (config.home / "echo-count").exists()


def test_failed_result_post_is_durable_and_retried(config, studio, enrolled, registry):
    studio.commands = [command()]
    studio.fail_status, studio.fail_path = 503, "/api/devices/agent/results"
    agent = Agent(config, enrolled, registry, sleep=lambda _: None, executor=inline)
    assert agent.run(max_cycles=1) == 0
    assert not studio.results
    studio.fail_status = None
    restarted = Agent(config, enrolled, registry, sleep=lambda _: None, executor=inline)
    assert restarted.run(max_cycles=1) == 0
    assert len(studio.results) == 1
    assert (config.home / "echo-count").read_text() == "1"


def test_expiry_unknown_op_and_invalid_args(config, studio, enrolled, registry):
    old = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    older = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
    studio.commands = [command(expires_at=old, issued_at=older), command("shell.exec", {}),
                       command("pkg.health", {"package": "echo", "shell": "bad"}),
                       command(expires_at="2026-01-01T00:00:00")]
    agent = Agent(config, enrolled, registry, sleep=lambda _: None, executor=inline)
    assert agent.run(max_cycles=1) == 0
    assert [row["error"]["code"] for row in studio.results] == ["command_expired", "unsupported_op", "invalid_args", "invalid_args"]
    assert not (config.home / "echo-count").exists()


def test_operation_timeouts_are_selected_and_workers_are_stopped(config, enrolled, registry, local_package):
    timeouts = []
    def record(dispatcher, op, args, timeout, tick):
        timeouts.append(timeout)
        return {"status": "ok"}
    agent = Agent(config, enrolled, registry, executor=record)
    try:
        agent.execute(command("pkg.install", {"package": "echo", "version": "1"}))
        agent.execute(command("pkg.upgrade", {"package": "echo", "version": "1"}))
        agent.execute(command("pkg.uninstall", {"package": "echo", "keep_data": True}))
        agent.execute(command("pkg.health"))
        agent.execute(command("http.proxy", {"package": "echo", "method": "GET", "path": "/api/states"}))
        assert timeouts == [1500, 1500, 120, 30, 30]
    finally:
        agent.db.close()
    registry.handlers["echo"] = Echo(config.home, delay=5)
    children_before = {child.pid for child in multiprocessing.active_children()}
    result = isolated(Dispatcher(config, registry), "pkg.health", {"package": "echo"}, .25, lambda: None)
    assert result["error"]["code"] == "timeout"
    assert {child.pid for child in multiprocessing.active_children()} == children_before
    assert not (config.home / "echo-count").exists()


def test_worker_wait_keeps_heartbeat_scheduler_active(config, enrolled, registry, local_package):
    registry.handlers["echo"] = Echo(config.home, delay=1.2)
    ticks = []
    result = isolated(Dispatcher(config, registry), "pkg.health", {"package": "echo"}, 15, lambda: ticks.append(True))
    assert result["status"] == "ok"
    assert ticks


def test_heartbeat_failure_does_not_interrupt_operation(config, enrolled, registry):
    def fake_executor(dispatcher, op, args, timeout, tick):
        tick()
        return {"status": "ok"}
    agent = Agent(config, enrolled, registry, executor=fake_executor)
    def broken_heartbeat():
        raise ClientError("temporary failure")
    agent.heartbeat = broken_heartbeat
    try:
        assert agent.execute(command())["status"] == "ok"
    finally:
        agent.db.close()


@pytest.mark.parametrize("status", [401, 429, 500, 503])
def test_transport_failure_backoff_has_bounded_calls(config, studio, enrolled, status):
    clock = Clock()
    enrolled.backoff = Backoff(enrolled.device_id, clock.sleep)
    studio.fail_status = status
    calls = 0
    while clock.now < 120:
        with pytest.raises(ClientError):
            enrolled.poll()
        calls += 1
    assert calls <= 9
    assert all(1 <= delay <= 60 for delay in clock.delays)
    assert len({row[3]["X-Leaf-Nonce"] for row in studio.seen}) == calls


def test_network_backoff_and_explicit_timeouts(config, studio, enrolled):
    class Broken:
        def __init__(self):
            self.timeouts = []
        def open(self, request, timeout):
            self.timeouts.append(timeout)
            raise URLError("sensitive upstream text")
    broken = Broken()
    clock = Clock()
    enrolled.opener, enrolled.backoff = broken, Backoff(enrolled.device_id, clock.sleep)
    for action in (lambda: enrolled.poll(25), lambda: enrolled.poll(0),
                   lambda: enrolled.post_results([{}]), lambda: enrolled.heartbeat([], 0, 0)):
        with pytest.raises(ClientError, match="Studio connection"):
            action()
    assert broken.timeouts == [35, 10, 15, 15]
    assert all(1 <= delay <= 60 for delay in clock.delays)


def test_logs_redact_enrollment_key_and_package_credentials(config, studio, registry, caplog):
    caplog.set_level(logging.INFO, logger="leaf_agent")
    client = Client(config, studio.origin, sleep=lambda _: None)
    client.register(studio.token)
    seed = (config.home / "device.key").read_bytes()
    studio.fail_status = 401
    with pytest.raises(ClientError):
        client.poll()
    text = caplog.text
    for secret in (studio.token, seed.hex(), sign.b64url(seed), repr(seed), "local-only-secret"):
        assert secret not in text
    assert "Device enrolled" in text


def test_enrollment_rerun_preserves_identity(config, studio, enrolled):
    seed = (config.home / "device.key").read_bytes()
    assert Client(config, studio.origin).register("unused-token") == studio.device_id
    assert (config.home / "device.key").read_bytes() == seed
    with pytest.raises(ClientError):
        Client(config, "https://different.invalid").register("unused-token")


@pytest.mark.parametrize("origin", ["http://example.com", "http://127.0.0.1:8000", "https://user:pass@example.com", "https://example.com/path", "https://example.com?x=1", "file:///tmp", "https://example.com:70000"])
def test_invalid_origins_rejected(origin):
    with pytest.raises(ValueError):
        validate_origin(origin)


def test_tls_context_always_verifies(config):
    opener = secure_opener()
    contexts = [handler._context for handler in opener.handlers if hasattr(handler, "_context")]
    assert contexts and all(ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname for ctx in contexts)


def test_cli_env_enrollment_pause_resume_status(config, studio, monkeypatch, capsys):
    monkeypatch.setenv("LEAF_ENROLL_TOKEN", studio.token)
    assert main(["enroll", "--server", studio.origin]) == 0
    assert capsys.readouterr().out.strip() == studio.device_id
    assert "LEAF_ENROLL_TOKEN" not in os.environ
    assert main(["pause"]) == 0 and config.disabled
    assert main(["status"]) == 0
    assert json.loads(capsys.readouterr().out)["paused"] is True
    assert main(["resume"]) == 0 and not config.disabled
    log = (config.home / "agent.log").read_text(encoding="utf-8")
    assert studio.token not in log


def test_home_resolution_and_atomic_key(config, monkeypatch, tmp_path):
    assert agent_home() == config.home
    monkeypatch.delenv("LEAF_AGENT_HOME")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("leaf_agent.config.sys.platform", "darwin")
    assert agent_home() == tmp_path / "Library/Application Support/LeafAgent"
    monkeypatch.setattr("leaf_agent.config.sys.platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert agent_home() == tmp_path / "xdg/leaf-agent"
    monkeypatch.delenv("XDG_DATA_HOME")
    assert agent_home() == tmp_path / ".local/share/leaf-agent"
    target = tmp_path / "private/device.key"
    atomic_write(target, b"a" * 32)
    atomic_write(target, b"b" * 32)
    assert target.read_bytes() == b"b" * 32
    assert list(target.parent.iterdir()) == [target]
