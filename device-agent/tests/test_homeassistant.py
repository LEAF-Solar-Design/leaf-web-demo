"""Local HTTP/RFC6455 fixtures; no HA, Homebrew, launchd or systemd needed."""
import base64
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import pickle
import plistlib
import socket
import struct
import subprocess
import threading
from types import SimpleNamespace
from urllib.parse import parse_qs

import pytest

from leaf_agent.config import write_json
from leaf_agent.core import OP_TIMEOUTS
from leaf_agent.packages import Registry
from leaf_agent.packages.homeassistant import (
    CLIENT_ID, FLOW_KEYS, FLOW_PATH, HAError, HomeAssistant, InstallError,
    INSTALL_BUDGET_S, LABEL, MAX_BODY, PINNED_VERSION, REQUIRES_PYTHON,
)


FIXTURES = Path(__file__).parent / "fixtures/ha"


class Clock:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@pytest.fixture
def ha_server():
    onboarding = json.loads((FIXTURES / "onboarding.json").read_text())
    flows = json.loads((FIXTURES / "flows.json").read_text())
    state = SimpleNamespace(onboarding=onboarding, flows=flows, steps=copy.deepcopy(onboarding["pending"]),
                            requests=[], ws=[], errors=[], current={}, positions={}, health=401,
                            bad_json=False, redirect=False, bad_ws=False, fragment_ws=False,
                            reject_ws=False, oversized_ws=False, fail_step=None, onboarding_gone=False)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def reply(self, status, data):
            body = b"NaN" if state.bad_json else json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def send_frame(self, payload, *, opcode=1, final=True):
            size = len(payload)
            length = bytes([size]) if size < 126 else b"\x7e" + struct.pack("!H", size) if size < 65536 else b"\x7f" + struct.pack("!Q", size)
            self.wfile.write(bytes([(128 if final else 0) | opcode]) + length + payload)
            self.wfile.flush()

        def send_ws(self, value):
            self.send_frame(json.dumps(value).encode())

        def read_ws(self):
            first, second = self.rfile.read(2)
            assert first == 129 and second & 128
            size = second & 127
            if size == 126:
                size = struct.unpack("!H", self.rfile.read(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self.rfile.read(8))[0]
            mask = self.rfile.read(4)
            data = self.rfile.read(size)
            return json.loads(bytes(value ^ mask[i % 4] for i, value in enumerate(data)))

        def websocket(self):
            self.close_connection = True
            key = self.headers["Sec-WebSocket-Key"]
            accept = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", "wrong" if state.bad_ws else accept)
            self.end_headers()
            if state.bad_ws:
                return
            self.send_ws(onboarding["auth_required"])
            auth = self.read_ws()
            assert auth["type"] == "auth"
            assert auth["access_token"] in {onboarding["tokens"]["access_token"], onboarding["long_lived"]["result"]}
            state.ws.append(auth)
            if state.reject_ws:
                self.send_ws({"type": "auth_invalid", "message": "fixture rejection"})
                return
            self.send_ws(onboarding["auth_ok"])
            command = self.read_ws()
            state.ws.append(command)
            if command["type"] == "auth/long_lived_access_token":
                assert command == {"id": 1, "type": "auth/long_lived_access_token", "lifespan": 3650,
                                   "client_name": "Leaf Automation device agent"}
                response = onboarding["long_lived"]
            else:
                assert command == {"id": 1, "type": "config_entries/flow/progress"}
                response = {"id": 1, "type": "result", "success": True, "result": flows["progress"]}
            if state.oversized_ws:
                self.wfile.write(b"\x81\x7f" + struct.pack("!Q", MAX_BODY + 1))
                self.wfile.flush()
            elif state.fragment_ws:
                body = json.dumps(response).encode()
                self.send_frame(body[:20], final=False)
                self.send_frame(b"ping", opcode=9)
                first, second = self.rfile.read(2)
                assert first == 138 and second == 132
                mask, data = self.rfile.read(4), self.rfile.read(4)
                assert bytes(value ^ mask[i % 4] for i, value in enumerate(data)) == b"ping"
                self.send_frame(body[20:], opcode=0)
            else:
                self.send_ws(response)

        def do_GET(self):
            self.route()

        def do_POST(self):
            self.route()

        def route(self):
            try:
                self.connection.settimeout(3)
                if self.path == "/api/websocket":
                    return self.websocket()
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                content_type = self.headers.get("Content-Type", "")
                body = ({key: value[0] for key, value in parse_qs(raw.decode()).items()} if content_type == "application/x-www-form-urlencoded"
                        else json.loads(raw) if raw else None)
                state.requests.append((self.command, self.path, body, dict(self.headers)))
                if state.redirect:
                    self.send_response(302)
                    self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/stolen")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/api/":
                    return self.reply(state.health, {"message": "API running."})
                if self.path == "/api/onboarding":
                    if state.onboarding_gone:
                        return self.reply(404, {"message": "404: Not Found"})
                    return self.reply(200, state.steps)
                if self.path == "/auth/login_flow":
                    assert body == {"client_id": CLIENT_ID, "handler": ["homeassistant", None], "redirect_uri": CLIENT_ID}
                    return self.reply(200, onboarding["login_form"])
                if self.path == "/auth/login_flow/fixture-login":
                    assert body["username"] == "leaf-owner" and body["password"]
                    assert body["client_id"] == CLIENT_ID
                    return self.reply(200, onboarding["login_result"])
                if self.path == "/auth/token":
                    assert content_type == "application/x-www-form-urlencoded"
                    assert body["client_id"] == CLIENT_ID
                    assert body["grant_type"] in {"authorization_code", "refresh_token"}
                    if body["grant_type"] == "authorization_code":
                        assert body["code"] == onboarding["owner"]["auth_code"]
                    else:
                        assert body["refresh_token"] == onboarding["tokens"]["refresh_token"]
                    return self.reply(200, onboarding["tokens"])
                if self.path == "/api/onboarding/users":
                    assert set(body) == {"name", "username", "password", "language", "client_id"}
                    assert body["name"] == "Leaf Automation" and body["username"] == "leaf-owner"
                    assert len(base64.urlsafe_b64decode(body["password"] + "=")) == 32
                    assert body["client_id"] == CLIENT_ID and body["language"] == "en"
                    state.steps[0]["done"] = True
                    return self.reply(200, onboarding["owner"])
                if self.path.startswith("/api/onboarding/"):
                    step = self.path.rsplit("/", 1)[-1]
                    assert self.headers["Authorization"] == "Bearer " + onboarding["tokens"]["access_token"]
                    assert body == ({"client_id": CLIENT_ID, "redirect_uri": CLIENT_ID} if step == "integration" else {})
                    if state.fail_step == step:
                        state.fail_step = None
                        return self.reply(503, {"message": "fixture interruption"})
                    next(item for item in state.steps if item["step"] == step)["done"] = True
                    return self.reply(200, {"auth_code": "unused-integration-code"} if step == "integration" else {})
                assert self.headers["Authorization"] == "Bearer " + onboarding["long_lived"]["result"]
                if self.path == FLOW_PATH:
                    if self.command == "GET":
                        return self.reply(405, {"message": "Method Not Allowed"})
                    assert body == {"handler": body["handler"], "show_advanced_options": False}
                    handler = body["handler"]
                    result = copy.deepcopy(flows[handler][0])
                    state.positions[result["flow_id"]] = 0
                    state.current[result["flow_id"]] = result
                    return self.reply(200, result)
                if self.path.startswith(FLOW_PATH + "/"):
                    flow_id = self.path.rsplit("/", 1)[-1]
                    if flow_id not in state.current:
                        return self.reply(404, {"message": "Unknown flow specified"})
                    current = state.current[flow_id]
                    if self.command == "GET":
                        return self.reply(200, current)
                    handler = current["handler"]
                    if handler == "group":
                        if current["type"] == "menu":
                            assert body == {"next_step_id": "light"}
                        else:
                            assert body == {"name": "Living room", "entities": ["light.one", "light.two"], "hide_members": False, "all": False}
                    elif current["step_id"] in {"manual", "user"}:
                        assert body == {"host": "10.0.0.20"}
                    else:
                        assert body == {}
                    state.positions[flow_id] += 1
                    result = copy.deepcopy(flows[handler][state.positions[flow_id]])
                    state.current[flow_id] = result
                    return self.reply(200, result)
                raise AssertionError("Unexpected fake HA request")
            except (ConnectionResetError, BrokenPipeError):
                pass
            except Exception as exc:
                state.errors.append(exc)
                self.close_connection = True

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.port = server.server_port
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not state.errors


def local_handler(tmp_path, ha_server, **kwargs):
    handler = HomeAssistant(tmp_path / "agent", platform="linux", user_home=tmp_path / "user", port=ha_server.port, **kwargs)
    handler._save_credential("token", ha_server.onboarding["long_lived"]["result"])
    return handler


def install_stub(monkeypatch, *, available=None, fail_pip=False, clock=None, brew_version="3.14.8", pip_seconds=10):
    monkeypatch.setattr(HomeAssistant, "_lan_reachable", lambda self: False)
    available = available if available is not None else {"/opt/homebrew/bin/python3.14": "3.14.8"}
    calls = []
    real_is_file = Path.is_file
    real_glob = Path.glob

    def is_file(path):
        if str(path).replace("\\", "/").startswith("/opt/homebrew/"):
            return str(path).replace("\\", "/") in available
        return real_is_file(path)

    monkeypatch.setattr(Path, "is_file", is_file)

    def glob(path, pattern):
        if path.as_posix() == "/opt/homebrew/bin":
            return (Path(name) for name in available if name.startswith("/opt/homebrew/bin/python3."))
        return real_glob(path, pattern)

    monkeypatch.setattr(Path, "glob", glob)

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[0] == "/opt/homebrew/bin/brew":
            assert argv == ["/opt/homebrew/bin/brew", "install", "python@3.14"]
            available["/opt/homebrew/bin/python3.14"] = brew_version
        if "-c" in argv and argv[0] in available:
            return SimpleNamespace(returncode=0, stdout=available[argv[0]] + "\n")
        if argv[1:3] == ["-m", "venv"]:
            venv = Path(argv[3])
            (venv / "bin").mkdir(parents=True)
            (venv / "bin/python").touch()
            (venv / "bin/hass").touch()
            available[str(venv / "bin/python")] = available[argv[0]]
        if argv[1:3] == ["-m", "pip"]:
            assert kwargs["stderr"] == subprocess.STDOUT
            assert 0 < kwargs["timeout"] <= INSTALL_BUDGET_S
            if clock:
                clock.now += pip_seconds
            if fail_pip:
                kwargs["stdout"].write("\n".join(f"pip diagnostic {number:02d}" for number in range(55)).encode())
                raise subprocess.CalledProcessError(1, argv)
        return SimpleNamespace(returncode=0, stdout="")

    return run, calls


def test_pin_registry_and_spawn_safety(tmp_path):
    assert PINNED_VERSION == "2026.10.4" and REQUIRES_PYTHON == ">=3.14.2"
    registry = Registry(tmp_path)
    handler = registry.get("homeassistant")
    assert isinstance(handler, HomeAssistant)
    assert handler.home == tmp_path and handler.state() is None
    assert registry.get("arbitrary") is None and registry.states() == []
    handler._cached_token = "must-not-cross-worker-boundary"
    assert b"must-not-cross-worker-boundary" not in pickle.dumps(registry)
    assert pickle.loads(pickle.dumps(handler)).root == handler.root
    replacement = HomeAssistant(tmp_path / "replacement")
    registry.register("homeassistant", replacement)
    assert registry.get("homeassistant") is replacement


def test_install_existing_python_plist_and_health_wait(tmp_path, monkeypatch, ha_server):
    clock = Clock()
    runner, calls = install_stub(monkeypatch, clock=clock)
    handler = HomeAssistant(tmp_path, platform="darwin", user_home=tmp_path / "user", uid=501,
                            runner=runner, clock=clock, sleep=clock.sleep, port=ha_server.port)
    probes = []

    def healthy(timeout=5):
        probes.append((clock(), timeout))
        return len(probes) >= 3

    monkeypatch.setattr(handler, "_healthy", healthy)
    result = handler.handle("pkg.install", {"version": PINNED_VERSION})
    assert result["status"] == "running" and result["python"] == "/opt/homebrew/bin/python3.14"
    assert result["requires_python"] == ">=3.14.2"
    assert clock.sleeps == [5, 5]
    argv = [item[0] for item in calls]
    assert not any(item[0].endswith("brew") for item in argv)
    assert argv[1] == ["/opt/homebrew/bin/python3.14", "-m", "venv", str(handler.root / "venv")]
    pip = str(handler.root / "venv/bin/python")
    assert argv[2:4] == [[pip, "-m", "pip", "install", "--upgrade", "pip", "wheel"],
                         [pip, "-m", "pip", "install", "homeassistant==" + PINNED_VERSION]]
    assert calls[3][1]["timeout"] < calls[2][1]["timeout"]
    assert argv[-1] == ["launchctl", "bootstrap", "gui/501", str(handler._service_path())]
    plist = plistlib.loads(handler._service_path().read_bytes())
    assert plist["Label"] == LABEL
    assert plist["ProgramArguments"] == [str(handler.root / "venv/bin/hass"), "-c", str(handler.root / "config")]
    assert plist["RunAtLoad"] and plist["KeepAlive"] and plist["ThrottleInterval"] == 30
    assert plist["StandardOutPath"] == plist["StandardErrorPath"] == str(handler.root / "hass.log")
    assert (handler.root / "config/configuration.yaml").read_text() == "default_config:\nlogger:\n  default: warning\nrecorder:\n  purge_keep_days: 3\n"
    assert set(handler.state()) == {"package", "version", "status", "last_health_at"}


def test_install_seeds_loopback_http_storage(tmp_path, monkeypatch, ha_server):
    runner, calls = install_stub(monkeypatch)
    handler = HomeAssistant(tmp_path, platform="darwin", user_home=tmp_path / "user", uid=501,
                            port=ha_server.port)
    snapshots = []

    def run(argv, **kwargs):
        if argv[:2] == ["launchctl", "bootstrap"]:
            snapshots.append(json.loads((handler.root / "config/.storage/http").read_text()))
        return runner(argv, **kwargs)

    handler.runner = run
    handler.handle("pkg.install", {"version": PINNED_VERSION})
    storage = json.loads((handler.root / "config/.storage/http").read_text())
    assert snapshots == [storage]
    assert storage["version"] == 2 and storage["minor_version"] == 2 and storage["key"] == "http"
    assert storage["data"]["stable"]["server_host"] == ["127.0.0.1"]
    assert storage["data"]["pending"] is None and storage["data"]["yaml_migration_done"] is True
    assert storage["data"]["stable"]["created_at"].endswith("+00:00")
    if os.name != "nt":
        assert (handler.root / "config/.storage").stat().st_mode & 0o777 == 0o700


def test_upgrade_repairs_existing_http_storage(tmp_path, monkeypatch, ha_server):
    runner, calls = install_stub(monkeypatch)
    handler = HomeAssistant(tmp_path, platform="darwin", user_home=tmp_path / "user", uid=501,
                            runner=runner, port=ha_server.port)
    handler._service_path().parent.mkdir(parents=True)
    handler._service_path().touch()
    storage = {"version": 2, "minor_version": 2, "key": "http", "extra": "preserved", "data": {
        "stable": {"server_port": 8123, "cors_allowed_origins": ["https://example.org"]},
        "pending": {"server_host": ["0.0.0.0"]}, "yaml_migration_done": False, "other": 42}}
    path = handler.root / "config/.storage/http"
    write_json(path, storage)
    ensure = handler._ensure_loopback_bind

    def repair():
        assert any(argv[:2] == ["launchctl", "bootout"] for argv, _ in calls)
        assert not any(argv[:2] == ["launchctl", "bootstrap"] for argv, _ in calls)
        ensure()

    monkeypatch.setattr(handler, "_ensure_loopback_bind", repair)
    handler.handle("pkg.upgrade", {"version": PINNED_VERSION})
    storage["data"]["stable"]["server_host"] = ["127.0.0.1"]
    storage["data"]["pending"] = None
    storage["data"]["yaml_migration_done"] = True
    assert json.loads(path.read_text()) == storage


@pytest.mark.parametrize("content", ["{", "null", '{"data": {"stable": []}}', '{"data": {}}'])
def test_malformed_http_storage_refuses_start(tmp_path, monkeypatch, content):
    runner, calls = install_stub(monkeypatch)
    handler = HomeAssistant(tmp_path, platform="darwin", user_home=tmp_path / "user", uid=501,
                            runner=runner)
    path = handler.root / "config/.storage/http"
    path.parent.mkdir(parents=True)
    path.write_text(content)
    with pytest.raises(InstallError):
        handler.handle("pkg.install", {"version": PINNED_VERSION})
    assert path.read_text() == content
    assert not any(argv[:2] == ["launchctl", "bootstrap"] for argv, _ in calls)


@pytest.mark.parametrize("platform", ["darwin", "linux"])
@pytest.mark.parametrize("remove_fails", [False, True])
def test_lan_reachable_stops_and_fails(tmp_path, monkeypatch, ha_server, platform, remove_fails):
    interpreter = tmp_path / "python3.14"
    interpreter.touch()
    stub, calls = install_stub(monkeypatch, available={str(interpreter): "3.14.8"})

    def runner(argv, **kwargs):
        result = stub(argv, **kwargs)
        if remove_fails and (argv[:2] == ["launchctl", "bootout"] or argv[:3] == ["systemctl", "--user", "disable"]):
            raise HAError("Home Assistant service stop failed")
        return result

    handler = HomeAssistant(tmp_path, platform=platform, user_home=tmp_path / "user", uid=501,
                            runner=runner, port=ha_server.port)
    monkeypatch.setattr(handler, "_python", lambda deadline: str(interpreter))
    monkeypatch.setattr(handler, "_lan_reachable", lambda: True)
    with pytest.raises(InstallError, match="^Home Assistant is reachable beyond localhost; stopped$"):
        handler.handle("pkg.install", {"version": PINNED_VERSION})
    argv = [item[0] for item in calls]
    if platform == "darwin":
        assert argv[-2][:2] == ["launchctl", "bootstrap"]
        assert argv[-1] == ["launchctl", "bootout", "gui/501", str(handler._service_path())]
    else:
        assert ["systemctl", "--user", "disable", "--now", LABEL + ".service"] in argv
        if not remove_fails:
            assert argv[-1] == ["systemctl", "--user", "daemon-reload"]
    assert handler._service_path().exists() is remove_fails
    state = json.loads((handler.root / "state.json").read_text())
    assert state["package"] == "homeassistant" and state["version"] == PINNED_VERSION
    assert state["status"] == "error"
    assert state["error"] == "Home Assistant is reachable beyond localhost; stopped"


def test_lan_reachable_false_when_refused(tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as closed:
        closed.bind(("0.0.0.0", 0))
        handler = HomeAssistant(tmp_path, port=closed.getsockname()[1])
        assert handler._lan_reachable() is False


def test_brew_fallback_and_upgrade_order(tmp_path, monkeypatch, ha_server):
    runner, calls = install_stub(monkeypatch, available={"/opt/homebrew/bin/python3.13": "3.13.9"})
    handler = HomeAssistant(tmp_path, platform="darwin", user_home=tmp_path / "user", uid=501, runner=runner, port=ha_server.port)
    result = handler.handle("pkg.install", {"version": PINNED_VERSION})
    assert result["python"] == "/opt/homebrew/bin/python3.14"
    assert ["/opt/homebrew/bin/brew", "install", "python@3.14"] in [item[0] for item in calls]
    (handler.root / "config/configuration.yaml").write_text("user configuration\n")
    calls.clear()
    handler.handle("pkg.upgrade", {"version": PINNED_VERSION})
    argv = [item[0] for item in calls]
    stop = next(i for i, item in enumerate(argv) if item[:2] == ["launchctl", "bootout"])
    pip = next(i for i, item in enumerate(argv) if item[1:3] == ["-m", "pip"])
    assert stop < pip < len(argv) - 1 and argv[-1][1] == "bootstrap"
    assert not any(item[1:3] == ["-m", "venv"] for item in argv)
    assert (handler.root / "config/configuration.yaml").read_text() == "user configuration\n"


def test_linux_python_selection_and_systemd(tmp_path, monkeypatch, ha_server):
    interpreter = tmp_path / "python3.14"
    interpreter.touch()
    runner, calls = install_stub(monkeypatch, available={str(interpreter): "3.14.2"})
    monkeypatch.setattr("leaf_agent.packages.homeassistant.shutil.which", lambda name: str(interpreter) if name == "python3.14" else None)
    handler = HomeAssistant(tmp_path, platform="linux", user_home=tmp_path / "user % and $", runner=runner, port=ha_server.port)
    assert handler.handle("pkg.install", {"version": PINNED_VERSION})["python"] == str(interpreter)
    unit = handler._service_path().read_text()
    quoted_hass = '"' + str(handler.root / "venv/bin/hass").replace("\\", "\\\\") + '"'
    assert "RestartSec=30" in unit and f"ExecStart={quoted_hass} -c " in unit
    assert ["systemctl", "--user", "enable", "--now", LABEL + ".service"] == calls[-1][0]


@pytest.mark.parametrize("interpreter,version", [("python3.14", "3.14.2"), ("python3.15", "3.15.0"), ("python3", "3.14.8")])
def test_compatible_homebrew_python_needs_no_install(tmp_path, monkeypatch, interpreter, version):
    expected = "/opt/homebrew/bin/" + interpreter
    runner, calls = install_stub(monkeypatch, available={expected: version})
    handler = HomeAssistant(tmp_path, platform="darwin", runner=runner)
    assert handler._python(handler.clock() + 900) == expected
    assert not any(item[0][0].endswith("brew") for item in calls)


@pytest.mark.parametrize("version", ["3.13.9", "3.14.1", "invalid"])
def test_brew_result_must_satisfy_requirement(tmp_path, monkeypatch, version):
    runner, calls = install_stub(monkeypatch, available={"/opt/homebrew/bin/python3": version}, brew_version=version)
    handler = HomeAssistant(tmp_path, platform="darwin", runner=runner)
    handler.root.mkdir()
    with pytest.raises(InstallError, match="No Python satisfies >=3.14.2"):
        handler._python(handler.clock() + 900)
    assert ["/opt/homebrew/bin/brew", "install", "python@3.14"] in [item[0] for item in calls]
    assert not any(item[0][1:3] == ["-m", "venv"] for item in calls)


def test_install_failure_has_last_40_log_lines(tmp_path, monkeypatch):
    runner, calls = install_stub(monkeypatch, fail_pip=True)
    handler = HomeAssistant(tmp_path, platform="darwin", user_home=tmp_path / "user", runner=runner, uid=501)
    with pytest.raises(InstallError) as raised:
        handler.handle("pkg.install", {"version": PINNED_VERSION})
    message = str(raised.value)
    assert "pip diagnostic 14" not in message and "pip diagnostic 15" in message and "pip diagnostic 54" in message
    assert len(message.splitlines()) == 41
    state = json.loads((handler.root / "state.json").read_text())
    assert state["status"] == "error" and state["error"] == message
    assert not any(item[0][0] == "launchctl" for item in calls)


def test_health_wait_uses_remaining_deadline(tmp_path, monkeypatch):
    clock = Clock()
    clock.now = 100
    handler = HomeAssistant(tmp_path, clock=clock, sleep=clock.sleep)
    probes = []
    monkeypatch.setattr(handler, "_healthy", lambda timeout=5: probes.append(timeout) or False)
    with pytest.raises(InstallError, match="installation deadline"):
        handler._wait_healthy(112)
    assert clock() == 112 and clock.sleeps == [5, 5, 2] and probes == [5, 5, 2]
    with pytest.raises(InstallError, match="installation deadline"):
        handler._wait_healthy(112)
    assert probes == [5, 5, 2]


@pytest.mark.parametrize("op", ["pkg.install", "pkg.upgrade"])
def test_install_budget_has_worker_margin(op):
    assert INSTALL_BUDGET_S == 1440
    assert INSTALL_BUDGET_S + 30 < OP_TIMEOUTS[op]


@pytest.mark.parametrize("op", ["pkg.install", "pkg.upgrade"])
@pytest.mark.parametrize("becomes_healthy", [True, False])
def test_slow_pip_and_health_share_install_budget(tmp_path, monkeypatch, op, becomes_healthy):
    clock = Clock()
    clock.now = 50
    runner, calls = install_stub(monkeypatch, clock=clock, pip_seconds=(INSTALL_BUDGET_S - 12) / 2)
    handler = HomeAssistant(tmp_path, platform="darwin", user_home=tmp_path / "user", uid=501,
                            runner=runner, clock=clock, sleep=clock.sleep)
    if op == "pkg.upgrade":
        write_json(handler.root / "state.json", {"package": "homeassistant", "version": PINNED_VERSION,
                   "status": "installed", "last_health_at": None})
        handler._service_path().parent.mkdir(parents=True)
        handler._service_path().touch()
    probes = []

    def healthy(timeout=5):
        probes.append((clock(), timeout))
        return becomes_healthy and len(probes) >= 3

    monkeypatch.setattr(handler, "_healthy", healthy)
    if becomes_healthy:
        assert handler.handle(op, {"version": PINNED_VERSION})["status"] == "running"
        assert clock() == 50 + INSTALL_BUDGET_S - 2
    else:
        (handler.root / "install.log").parent.mkdir(parents=True, exist_ok=True)
        (handler.root / "install.log").write_text("slow pip diagnostic\n")
        with pytest.raises(InstallError, match="slow pip diagnostic") as raised:
            handler.handle(op, {"version": PINNED_VERSION})
        assert clock() == 50 + INSTALL_BUDGET_S
        state = json.loads((handler.root / "state.json").read_text())
        assert state["status"] == "error" and state["error"] == str(raised.value)
    assert [timeout for _, timeout in probes[:3]] == [5, 5, 2]
    assert clock.sleeps == ([5, 5] if becomes_healthy else [5, 5, 2])
    pip_calls = [kwargs for argv, kwargs in calls if argv[1:3] == ["-m", "pip"]]
    assert [kwargs["timeout"] for kwargs in pip_calls] == [INSTALL_BUDGET_S, (INSTALL_BUDGET_S + 12) / 2]


@pytest.mark.parametrize("op", ["pkg.install", "pkg.upgrade"])
def test_pip_budget_exhaustion_records_error_without_starting_service(tmp_path, monkeypatch, op):
    clock = Clock()
    runner, calls = install_stub(monkeypatch)

    def timed_out(argv, **kwargs):
        if argv[1:3] == ["-m", "pip"]:
            kwargs["stdout"].write(b"pip exhausted budget\n")
            clock.now += kwargs["timeout"]
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return runner(argv, **kwargs)

    handler = HomeAssistant(tmp_path, platform="darwin", user_home=tmp_path / "user", uid=501,
                            runner=timed_out, clock=clock, sleep=clock.sleep)
    write_json(handler.root / "state.json", {"package": "homeassistant", "version": PINNED_VERSION,
               "status": "installed", "last_health_at": None})
    with pytest.raises(InstallError, match="pip exhausted budget") as raised:
        handler.handle(op, {"version": PINNED_VERSION})
    assert clock() == INSTALL_BUDGET_S
    state = json.loads((handler.root / "state.json").read_text())
    assert state["status"] == "error" and state["error"] == str(raised.value)
    assert not any(argv[:2] == ["launchctl", "bootstrap"] for argv, _ in calls)


def test_reject_wrong_pin_and_missing_interpreter(tmp_path, monkeypatch):
    handler = HomeAssistant(tmp_path)
    with pytest.raises(InstallError, match="Unsupported"):
        handler.handle("pkg.install", {"version": "https://evil.invalid/code"})
    assert not handler.root.exists()
    monkeypatch.setattr(Path, "is_file", lambda path: False)
    with pytest.raises(InstallError, match="No Python"):
        handler._python(handler.clock() + 20)


def test_onboarding_and_idempotent_rerun(tmp_path, ha_server, caplog):
    handler = HomeAssistant(tmp_path, platform="linux", port=ha_server.port)
    assert handler.handle("ha.onboard", {}) == {"onboarded": True}
    saved = json.loads((handler.root / "credentials.json").read_text())
    assert saved["token"] == ha_server.onboarding["long_lived"]["result"]
    assert all(item["done"] for item in ha_server.steps)
    if os.name != "nt":
        assert (handler.root / "credentials.json").stat().st_mode & 0o777 == 0o600
    first_requests, first_ws = len(ha_server.requests), len(ha_server.ws)
    fresh = HomeAssistant(tmp_path, platform="linux", port=ha_server.port)
    result = fresh.handle("ha.onboard", {})
    assert result == {"onboarded": True}
    assert len(ha_server.requests) == first_requests + 1 and len(ha_server.ws) == first_ws
    assert fresh.auth_headers() == {"Authorization": "Bearer " + saved["token"]}
    assert not any(secret in caplog.text or secret in json.dumps(result) for secret in saved.values())


def test_onboarding_recovers_interrupted_steps_and_missing_token(tmp_path, ha_server):
    handler = HomeAssistant(tmp_path, platform="linux", port=ha_server.port)
    ha_server.fail_step = "analytics"
    with pytest.raises(HAError):
        handler.handle("ha.onboard", {})
    assert ha_server.steps[0]["done"] and ha_server.steps[1]["done"]
    fresh = HomeAssistant(tmp_path, platform="linux", port=ha_server.port)
    assert fresh.handle("ha.onboard", {}) == {"onboarded": True}
    users = [item for item in ha_server.requests if item[1] == "/api/onboarding/users"]
    assert len(users) == 1
    values = fresh._load("credentials.json", {})
    values.pop("token")
    values.pop("refresh_token")
    write_json(fresh.root / "credentials.json", values)
    assert HomeAssistant(tmp_path, platform="linux", port=ha_server.port).handle("ha.onboard", {}) == {"onboarded": True}
    assert any(item[1] == "/auth/login_flow" for item in ha_server.requests)


def test_onboarding_after_ha_restart_has_no_status_view(tmp_path, ha_server):
    handler = HomeAssistant(tmp_path, platform="linux", port=ha_server.port)
    assert handler.handle("ha.onboard", {}) == {"onboarded": True}
    ha_server.onboarding_gone = True
    before = len(ha_server.requests)
    fresh = HomeAssistant(tmp_path, platform="linux", port=ha_server.port)
    assert fresh.handle("ha.onboard", {}) == {"onboarded": True}
    assert [item[1] for item in ha_server.requests[before:]] == ["/api/onboarding"]
    values = fresh._load("credentials.json", {})
    values.pop("token")
    values.pop("refresh_token")
    write_json(fresh.root / "credentials.json", values)
    assert fresh.handle("ha.onboard", {}) == {"onboarded": True}
    assert any(item[1] == "/auth/login_flow" for item in ha_server.requests[before:])


def test_existing_unmanaged_owner_fails_without_overwrite(tmp_path, ha_server):
    for step in ha_server.steps:
        step["done"] = True
    handler = HomeAssistant(tmp_path, platform="linux", port=ha_server.port)
    with pytest.raises(HAError, match="no managed credentials"):
        handler.handle("ha.onboard", {})
    assert [item[1] for item in ha_server.requests] == ["/api/onboarding"]


def test_keychain_secret_uses_stdin_and_cache_expires(tmp_path, caplog):
    calls, values = [], {}
    clock = Clock()

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        assert argv[0] == "/usr/bin/security" and argv[argv.index("-s") + 1] == LABEL
        name = argv[argv.index("-a") + 1]
        if argv[1] == "add-generic-password":
            assert argv[-1] == "-w" and kwargs["start_new_session"] is True
            secret, confirm, empty = kwargs["input"].split("\n")
            assert secret == confirm and empty == ""
            assert secret not in argv and "-A" not in argv
            values[name] = secret
            return SimpleNamespace(returncode=0, stdout="")
        return SimpleNamespace(returncode=0 if name in values else 44, stdout=values.get(name, "") + "\n")

    handler = HomeAssistant(tmp_path, platform="darwin", runner=runner, clock=clock)
    assert handler.auth_headers() == {}
    handler._save_credential("token", "private-keychain-secret")
    assert handler.auth_headers() == {"Authorization": "Bearer private-keychain-secret"}
    reads = len(calls)
    values["token"] = "rotated-private-secret"
    clock.now = 299
    assert handler.auth_headers() == {"Authorization": "Bearer private-keychain-secret"}
    assert len(calls) == reads
    clock.now = 300
    assert handler.auth_headers() == {"Authorization": "Bearer rotated-private-secret"}
    assert len(calls) == reads + 1 and not (handler.root / "credentials.json").exists()
    assert "private" not in caplog.text


def test_hue_link_retry_and_success(tmp_path, ha_server):
    handler = local_handler(tmp_path, ha_server)
    result = handler.handle("ha.flow.start", {"handler": "hue", "data": {"host": "10.0.0.20"}})
    assert result["type"] == "form" and result["step_id"] == "link"
    assert set(result) <= FLOW_KEYS
    # A fresh handler simulates the next isolated operation worker.
    fresh = HomeAssistant(handler.home, platform="linux", port=ha_server.port)
    retry = fresh.handle("ha.flow.step", {"flow_id": result["flow_id"], "data": {}})
    assert retry["errors"] == {"base": "register_failed"} and retry["type"] == "form"
    done = fresh.handle("ha.flow.step", {"flow_id": result["flow_id"], "data": {}})
    assert done == {"flow_id": "hue-user", "type": "create_entry", "title": "Hue Bridge fixture"}
    assert fresh._load("flows.json", {}) == {}


def test_wiz_by_host_and_group_light_menu(tmp_path, ha_server):
    handler = local_handler(tmp_path, ha_server)
    wiz = handler.handle("ha.flow.start", {"handler": "wiz", "data": {"host": "10.0.0.20"}})
    assert wiz["type"] == "create_entry" and "result" not in wiz
    group = handler.handle("ha.flow.start", {"handler": "group", "data": {"name": "Living room", "entities": ["light.one", "light.two"]}})
    assert group == {"flow_id": "group-user", "type": "create_entry", "title": "Living room"}
    posts = [(path, data) for method, path, data, headers in ha_server.requests if method == "POST"]
    assert (FLOW_PATH + "/group-user", {"next_step_id": "light"}) in posts


def test_group_flow_accepts_studio_group_type(tmp_path, ha_server):
    handler = local_handler(tmp_path, ha_server)
    data = {"group_type": "light", "name": "Living room", "entities": ["light.one", "light.two"]}
    result = handler.handle("ha.flow.start", {"handler": "group", "data": data})
    assert result == {"flow_id": "group-user", "type": "create_entry", "title": "Living room"}
    assert data == {"group_type": "light", "name": "Living room", "entities": ["light.one", "light.two"]}


def test_group_flow_rejects_other_group_type(tmp_path, ha_server):
    handler = local_handler(tmp_path, ha_server)
    with pytest.raises(HAError, match="^Unsupported group type$"):
        handler.handle("ha.flow.start", {"handler": "group", "data": {"group_type": "switch"}})
    assert ha_server.requests == []


def test_group_flow_drops_unknown_keys(tmp_path, ha_server):
    handler = local_handler(tmp_path, ha_server)
    result = handler.handle("ha.flow.start", {"handler": "group", "data": {
        "name": "Living room", "entities": ["light.one", "light.two"], "color": "blue",
    }})
    assert result == {"flow_id": "group-user", "type": "create_entry", "title": "Living room"}


def test_group_start_without_data_and_step(tmp_path, ha_server):
    handler = local_handler(tmp_path, ha_server)
    result = handler.handle("ha.flow.start", {"handler": "group"})
    assert result["type"] == "form" and result["step_id"] == "light"
    result = handler.handle("ha.flow.step", {"flow_id": result["flow_id"], "data": {"name": "Living room", "entities": ["light.one", "light.two"]}})
    assert result["type"] == "create_entry"


def test_flow_list_discovered_and_tracked_user_flows(tmp_path, ha_server):
    handler = local_handler(tmp_path, ha_server)
    handler.handle("ha.flow.start", {"handler": "hue"})
    result = HomeAssistant(handler.home, platform="linux", port=ha_server.port).handle("ha.flow.list", {})
    assert result == [
        {"flow_id": "hue-discovered", "handler": "hue", "step_id": "link", "context_title": "Hue Bridge", "host": "10.0.0.20"},
        {"flow_id": "wiz-discovered", "handler": "wiz", "step_id": "discovery_confirm", "context_title": "WiZ bulb", "host": "10.0.0.21"},
        {"flow_id": "hue-user", "handler": "hue", "step_id": "manual"},
    ]
    assert not any(method == "GET" and path == FLOW_PATH for method, path, data, headers in ha_server.requests)


def test_flow_list_prunes_completed_user_flow_and_keeps_unavailable(tmp_path, ha_server):
    handler = local_handler(tmp_path, ha_server)
    write_json(handler.root / "flows.json", {"expired-flow": "hue"})
    assert len(handler.handle("ha.flow.list", {})) == 2
    assert handler._load("flows.json", {}) == {}
    handler.handle("ha.flow.start", {"handler": "hue"})
    ha_server.bad_json = True
    with pytest.raises(HAError):
        handler.handle("ha.flow.list", {})
    assert handler._load("flows.json", {}) == {"hue-user": "hue"}


@pytest.mark.parametrize("keep_data", [True, False])
def test_uninstall_data_and_credentials(tmp_path, ha_server, keep_data):
    calls = []
    handler = local_handler(tmp_path, ha_server, runner=lambda argv, **kwargs: calls.append(argv))
    for directory in ("venv", "config"):
        (handler.root / directory).mkdir()
        (handler.root / directory / "preserved").touch()
    write_json(handler.root / "state.json", {"package": "homeassistant", "version": PINNED_VERSION, "status": "running", "last_health_at": None})
    handler._service_path().parent.mkdir(parents=True)
    handler._service_path().touch()
    handler.auth_headers()
    assert handler.handle("pkg.uninstall", {"keep_data": keep_data}) == {"uninstalled": True}
    assert not (handler.root / "venv").exists() and not handler._service_path().exists()
    assert (handler.root / "config").exists() is keep_data
    assert (handler.root / "credentials.json").exists() is keep_data
    assert handler.state() is None and handler._cached_token is None
    assert calls[0] == ["systemctl", "--user", "disable", "--now", LABEL + ".service"]
    assert calls[-1] == ["systemctl", "--user", "daemon-reload"]


@pytest.mark.parametrize("http_status,expected", [(401, "running"), (200, "running"), (503, "stopped")])
def test_health_package_state_exact(tmp_path, ha_server, http_status, expected):
    handler = HomeAssistant(tmp_path, platform="linux", port=ha_server.port)
    write_json(handler.root / "state.json", {"package": "homeassistant", "version": PINNED_VERSION, "status": "installed", "last_health_at": None})
    ha_server.health = http_status
    result = handler.handle("pkg.health", {})
    assert set(result) == {"package", "version", "status", "last_health_at"}
    assert result["status"] == expected
    assert bool(result["last_health_at"]) is (expected == "running")


@pytest.mark.parametrize("failure", ["bad_json", "redirect", "bad_ws", "reject_ws", "oversized_ws"])
def test_upstream_errors_do_not_leak_credentials(tmp_path, ha_server, caplog, failure):
    handler = local_handler(tmp_path, ha_server)
    setattr(ha_server, failure, True)
    with pytest.raises(HAError) as raised:
        handler.handle("ha.flow.start", {"handler": "hue"}) if failure in {"bad_json", "redirect"} else handler.handle("ha.flow.list", {})
    assert "fixture-long-lived-access" not in str(raised.value) + caplog.text
    assert not any(path == "/stolen" for method, path, data, headers in ha_server.requests)


def test_websocket_fragmentation_and_ping(tmp_path, ha_server):
    handler = local_handler(tmp_path, ha_server)
    ha_server.fragment_ws = True
    assert len(handler.handle("ha.flow.list", {})) == 2


def test_flow_rejects_other_handlers_and_path_injection(tmp_path, ha_server):
    handler = local_handler(tmp_path, ha_server)
    with pytest.raises(HAError, match="Unsupported"):
        handler.handle("ha.flow.start", {"handler": "auth"})
    for flow_id in ("../auth/token", "x?secret", "x%2fy", "", "x" * 129):
        with pytest.raises(HAError, match="Invalid flow"):
            handler.handle("ha.flow.step", {"flow_id": flow_id, "data": {}})
    assert not ha_server.requests
    ha_server.current["foreign"] = {"flow_id": "foreign", "handler": "auth", "type": "form", "step_id": "user"}
    with pytest.raises(HAError, match="Unsupported"):
        handler.handle("ha.flow.step", {"flow_id": "foreign", "data": {"secret": "do-not-submit"}})
    assert all(method == "GET" for method, path, data, headers in ha_server.requests)


@pytest.mark.parametrize("returncode", [0, 3, 113])
def test_mac_uninstall_bootout_and_keychain_deletion(tmp_path, returncode):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=returncode)

    handler = HomeAssistant(tmp_path, platform="darwin", user_home=tmp_path / "user", uid=501,
                            runner=runner)
    handler._service_path().parent.mkdir(parents=True)
    handler._service_path().touch()
    handler.handle("pkg.uninstall", {"keep_data": False})
    assert calls[0] == ["launchctl", "bootout", "gui/501", str(handler._service_path())]
    assert [item[-1] for item in calls[1:]] == ["password", "token", "refresh_token"]
    assert all(item[:2] == ["/usr/bin/security", "delete-generic-password"] for item in calls[1:])
    assert not handler._service_path().exists()


@pytest.mark.parametrize("keep_data", [True, False])
def test_mac_uninstall_without_plist_skips_bootout(tmp_path, keep_data):
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "launchctl":
            raise AssertionError("No plist was written by the failed install")
        return SimpleNamespace(returncode=0)

    handler = HomeAssistant(tmp_path, platform="darwin", user_home=tmp_path / "user", uid=501, runner=runner)
    (handler.root / "venv").mkdir(parents=True)
    (handler.root / "config").mkdir()
    write_json(handler.root / "state.json", {"package": "homeassistant", "version": PINNED_VERSION,
               "status": "error", "last_health_at": None})
    assert handler.handle("pkg.uninstall", {"keep_data": keep_data}) == {"uninstalled": True}
    assert not (handler.root / "venv").exists() and not (handler.root / "state.json").exists()
    assert (handler.root / "config").exists() is keep_data
    assert len(calls) == (0 if keep_data else 3)


def test_service_stop_failure_preserves_data(tmp_path):
    handler = HomeAssistant(tmp_path, platform="darwin", user_home=tmp_path / "user", uid=501,
                            runner=lambda argv, **kwargs: SimpleNamespace(returncode=1))
    (handler.root / "venv").mkdir(parents=True)
    (handler.root / "config").mkdir()
    handler._service_path().parent.mkdir(parents=True)
    handler._service_path().touch()
    with pytest.raises(HAError, match="stop failed"):
        handler.handle("pkg.uninstall", {"keep_data": False})
    assert (handler.root / "venv").exists() and (handler.root / "config").exists()
    assert handler._service_path().exists()


@pytest.mark.parametrize("op,args", [("pkg.health", {}), ("pkg.uninstall", {"keep_data": True})])
def test_mac_package_operations_with_locked_keychain(config, tmp_path, monkeypatch, op, args):
    from leaf_agent.ops import Dispatcher

    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=1)

    handler = HomeAssistant(tmp_path / "managed", platform="darwin", user_home=tmp_path / "user", uid=501, runner=runner)
    monkeypatch.setattr(handler, "_healthy", lambda timeout=5: False)
    write_json(handler.root / "state.json", {"package": "homeassistant", "version": PINNED_VERSION,
               "status": "installed", "last_health_at": None})
    with pytest.raises(HAError, match="Credential read failed"):
        handler.auth_headers()
    calls.clear()
    registry = Registry(config.home)
    registry.register("homeassistant", handler)
    result = Dispatcher(config, registry).dispatch(op, {"package": "homeassistant", **args})
    assert result["status"] == "ok" and not calls


def test_health_through_dispatcher_is_credential_free(config, tmp_path, ha_server, caplog):
    from leaf_agent.ops import Dispatcher

    handler = local_handler(tmp_path, ha_server)
    registry = Registry(config.home)
    registry.register("homeassistant", handler)
    result = Dispatcher(config, registry).dispatch("pkg.health", {"package": "homeassistant"})
    assert result == {"status": "ok", "data": {"package": "homeassistant", "version": PINNED_VERSION,
                                              "status": "stopped", "last_health_at": None}}
    assert "fixture-long-lived-access" not in json.dumps(result) + caplog.text
