"""Managed, loopback-only Home Assistant Core.

Pin evidence (checked 2026-11-03):
https://pypi.org/pypi/homeassistant/2026.10.4/json reports stable, non-yanked
2026.10.4, requires_python >=3.14.2; wheel uploaded
2026-10-24T20:32:19.662412Z (sdist 2026-10-24T20:32:27.334934Z).
Measured Homebrew Python 3.14.8 satisfies this pin; 3.13 does not.

Source checked at https://github.com/home-assistant/core/tree/2026.10.4/:
homeassistant/components/onboarding/{__init__,views}.py, config/config_entries.py,
auth/__init__.py, auth/login_flow.py, auth/indieauth.py, websocket_api/http.py,
websocket_api/auth.py, websocket_api/const.py, api/__init__.py,
hue/config_flow.py, wiz/config_flow.py, group/config_flow.py (all components/),
homeassistant/helpers/data_entry_flow.py, helpers/schema_config_entry_flow.py,
and homeassistant/data_entry_flow.py.
Source-driven deviations: /auth/token requires form encoding, not JSON;
users requires client_id; integration requires client_id and redirect_uri and
a credential-backed temporary token. GET config_entries/flow returns 405;
listing uses WS config_entries/flow/progress plus locally tracked user flows.
Completed onboarding has no status view after restart (404).
The group menu accepts next_step_id=light; its form also defaults all=false.
Keychain writes use security -i commands on stdin to avoid getpass truncation;
each write is verified by reading the full value back.
No HA credentials or upstream exception text are logged or returned.
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import plistlib
import re
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import sys
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request

from .. import __version__, launchd
from ..client import secure_opener
from ..config import Config, atomic_write, write_json

PINNED_VERSION = "2026.10.4"
REQUIRES_PYTHON = ">=3.14.2"
INSTALL_BUDGET_S = 1440  # Leave 60 seconds for error persistence before the worker deadline.
LABEL = "ai.leaf.homeassistant"
APP_IDENTIFIER = "ai.leaf.homeassistant.app"
CLIENT_ID = "http://127.0.0.1:8123/"
FLOW_PATH = "/api/config/config_entries/flow"
MAX_BODY = 1048576
FLOW_KEYS = {"flow_id", "type", "step_id", "errors", "title", "description_placeholders"}


class InstallError(RuntimeError):
    """Local diagnostic; the dispatcher maps failures to install_failed."""


class HAError(RuntimeError):
    """Fixed, credential-free diagnostic for an upstream failure."""
    def __init__(self, message, *, status=None):
        super().__init__(message)
        self.status = status


def _json(raw):
    def invalid(value):
        raise ValueError("Non-JSON constant")
    return json.loads(raw, parse_constant=invalid)


class _WebSocket:
    """Small RFC6455 client for the local JSON command API; no dependencies."""
    def __init__(self, port):
        self.deadline = time.monotonic() + 15
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=15)
        self.stream = self.sock.makefile("rb")
        try:
            key = base64.b64encode(secrets.token_bytes(16)).decode()
            self.sock.sendall((
                f"GET /api/websocket HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode("ascii"))
            status = self._line()
            if not status.startswith(b"HTTP/1.1 101 "):
                raise HAError("WebSocket upgrade failed")
            headers, total = {}, len(status)
            while True:
                line = self._line()
                total += len(line)
                if not line or total > 16384:
                    raise HAError("Invalid WebSocket upgrade")
                if line == b"\r\n":
                    break
                name, value = line.decode("ascii").split(":", 1)
                headers[name.lower()] = value.strip()
            expected = base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            if (headers.get("sec-websocket-accept") != expected or
                    headers.get("upgrade", "").lower() != "websocket" or
                    "upgrade" not in headers.get("connection", "").lower()):
                raise HAError("Invalid WebSocket upgrade")
        except Exception:
            self.close()
            raise

    def close(self):
        self.stream.close()
        self.sock.close()

    def _read(self, count):
        data = bytearray()
        while len(data) < count:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("WebSocket deadline exceeded")
            self.sock.settimeout(remaining)
            part = self.stream.read1(count - len(data))
            if not part:
                raise HAError("WebSocket closed")
            data.extend(part)
        return bytes(data)

    def _line(self):
        line = bytearray()
        while len(line) < 4096:
            line.extend(self._read(1))
            if line.endswith(b"\r\n"):
                return bytes(line)
        raise HAError("Invalid WebSocket header")

    def _send(self, payload, opcode=1):
        mask = secrets.token_bytes(4)
        size = len(payload)
        length = bytes([size | 128]) if size < 126 else b"\xfe" + struct.pack("!H", size) if size < 65536 else b"\xff" + struct.pack("!Q", size)
        self.sock.sendall(bytes([128 | opcode]) + length + mask + bytes(value ^ mask[i % 4] for i, value in enumerate(payload)))

    def send(self, value):
        payload = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
        if len(payload) > MAX_BODY:
            raise HAError("WebSocket request too large")
        self._send(payload)

    def receive(self):
        payload = bytearray()
        started = False
        while True:
            first, second = self._read(2)
            opcode, final = first & 15, bool(first & 128)
            if first & 112 or second & 128:
                raise HAError("Invalid WebSocket frame")
            size = second & 127
            if size == 126:
                size = struct.unpack("!H", self._read(2))[0]
            elif size == 127:
                size = struct.unpack("!Q", self._read(8))[0]
            if size > MAX_BODY or len(payload) + size > MAX_BODY:
                raise HAError("WebSocket response too large")
            data = self._read(size)
            if opcode >= 8:
                if not final or size > 125 or opcode not in {8, 9, 10}:
                    raise HAError("Invalid WebSocket control frame")
                if opcode == 8:
                    raise HAError("WebSocket closed")
                if opcode == 9:
                    self._send(data, 10)
                continue
            if (opcode == 1 and started) or (opcode == 0 and not started) or opcode not in {0, 1}:
                raise HAError("Invalid WebSocket message")
            started = True
            payload.extend(data)
            if final:
                return _json(payload.decode("utf-8"))


class HomeAssistant:
    def __init__(self, home=None, *, platform=None, user_home=None, uid=None,
                 runner=subprocess.run, clock=time.monotonic, sleep=time.sleep,
                 port=8123):
        self.home = Path(home.home if isinstance(home, Config) else home) if home is not None else Config().home
        self.root = self.home / "ha"
        self.platform = platform or sys.platform
        self.user_home = Path(user_home) if user_home is not None else None
        self.uid = uid
        self.runner, self.clock, self.sleep = runner, clock, sleep
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError("Invalid local port")
        self.port = port
        self._cached_token, self._token_until = None, 0

    def __getstate__(self):
        state = dict(self.__dict__)
        state.update(_cached_token=None, _token_until=0)
        return state

    def _load(self, name, default):
        try:
            return _json((self.root / name).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default

    def _credential(self, name):
        if self.platform == "darwin":
            result = self.runner(["/usr/bin/security", "find-generic-password", "-s", LABEL, "-a", name, "-w"],
                                 check=False, capture_output=True, text=True, timeout=15)
            if result.returncode == 44:
                return None
            if result.returncode:
                raise HAError("Credential read failed")
            return result.stdout.rstrip("\r\n") or None
        return self._load("credentials.json", {}).get(name)

    def _save_credential(self, name, value):
        if self.platform == "darwin":
            if not value or any(char in value for char in '"\\\n\r\x00'):
                raise HAError("Invalid credential value")
            try:
                self.runner(["/usr/bin/security", "-i"],
                            input=f'add-generic-password -U -s "{LABEL}" -a "{name}" -w "{value}"\nquit\n',
                            start_new_session=True, check=False, capture_output=True, text=True, timeout=15)
                # security -i may exit nonzero after quit even when the write landed.
                saved = self._credential(name)
            except Exception:
                raise HAError("Credential write failed") from None
            if saved != value:
                raise HAError("Credential write failed")
        else:
            values = self._load("credentials.json", {})
            values[name] = value
            write_json(self.root / "credentials.json", values)
        self._cached_token, self._token_until = None, 0

    def auth_headers(self):
        if self._cached_token is None or self.clock() >= self._token_until:
            self._cached_token = self._credential("token")
            self._token_until = self.clock() + 300
        return {"Authorization": "Bearer " + self._cached_token} if self._cached_token else {}

    def _request(self, method, path, data=None, *, token=None, authenticated=True, form=False):
        headers = {"Accept": "application/json"}
        if authenticated:
            headers.update({"Authorization": "Bearer " + token} if token else self.auth_headers())
            if "Authorization" not in headers:
                raise HAError("Onboarding required")
        body = None
        if data is not None:
            body = (urlencode(data) if form else json.dumps(data, allow_nan=False, separators=(",", ":"))).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded" if form else "application/json"
        if body is not None and len(body) > MAX_BODY:
            raise HAError("Request too large")
        request = Request(f"http://127.0.0.1:{self.port}" + path, data=body, method=method, headers=headers)
        try:
            with secure_opener().open(request, timeout=15) as response:
                content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                if content_type != "application/json":
                    raise HAError("Expected JSON response")
                raw = response.read(MAX_BODY + 1)
                if len(raw) > MAX_BODY:
                    raise HAError("Response too large")
                return _json(raw.decode("utf-8"))
        except HTTPError as exc:
            status = exc.code
            exc.close()
            raise HAError("Home Assistant request failed", status=status) from None
        except Exception:
            raise HAError("Home Assistant request failed") from None

    def _ws(self, command, *, token=None):
        token = token or self.auth_headers().get("Authorization", "").removeprefix("Bearer ")
        if not token:
            raise HAError("Onboarding required")
        ws = None
        try:
            ws = _WebSocket(self.port)
            if ws.receive().get("type") != "auth_required":
                raise HAError("Invalid WebSocket authentication")
            ws.send({"type": "auth", "access_token": token})
            if ws.receive().get("type") != "auth_ok":
                raise HAError("WebSocket authentication failed")
            ws.send({"id": 1, **command})
            result = ws.receive()
            if result.get("id") != 1 or result.get("type") != "result" or result.get("success") is not True:
                raise HAError("WebSocket command failed")
            return result["result"]
        except Exception:
            raise HAError("Home Assistant WebSocket failed") from None
        finally:
            if ws is not None:
                ws.close()

    def _healthy(self, timeout=5):
        try:
            request = Request(f"http://127.0.0.1:{self.port}/api/", headers={"Accept": "application/json"})
            with secure_opener().open(request, timeout=timeout) as response:
                return response.status == 200
        except HTTPError as exc:
            exc.close()
            return exc.code == 401
        except (OSError, HTTPException, ValueError):
            return False

    def state(self):
        state = self._load("state.json", None)
        if state is None:
            return None
        if self._healthy():
            state["status"] = "running"
            state["last_health_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        elif state["status"] != "error":
            state["status"] = "stopped"
        write_json(self.root / "state.json", state)
        return {key: state[key] for key in ("package", "version", "status", "last_health_at")}

    def _service_path(self):
        return launchd.service_path(self.platform, self.user_home).with_name(LABEL + (".plist" if self.platform == "darwin" else ".service"))

    def _app_path(self):
        home = self.user_home if self.user_home is not None else Path.home()
        return home / "Applications/Leaf Home Assistant.app"

    def _build_app(self):
        app = self._app_path()
        values = [str(self.root / "lan.status"), str(self.root / "hass.log"),
                  str(self.root / "venv/bin/hass"), "-c", str(self.root / "config")]
        if any(any(char in value for char in "\n\r\x00") for value in values):
            raise HAError("Invalid Home Assistant launcher argument")
        contents = app / "Contents"
        (contents / "MacOS").mkdir(parents=True, exist_ok=True)
        (contents / "Resources").mkdir(exist_ok=True)
        atomic_write(contents / "Info.plist", plistlib.dumps({
            "CFBundleIdentifier": APP_IDENTIFIER,
            "CFBundleName": "Leaf Home Assistant",
            "CFBundleDisplayName": "Leaf Home Assistant",
            "CFBundleExecutable": "leaf-ha-launcher",
            "CFBundlePackageType": "APPL",
            "CFBundleVersion": __version__,
            "CFBundleShortVersionString": __version__,
            "LSUIElement": True,
            "NSLocalNetworkUsageDescription": "Leaf Home Assistant controls the lights on your home network.",
            "NSBluetoothAlwaysUsageDescription": "Home Assistant can find Bluetooth devices near this computer.",
            "NSBonjourServices": ["_hue._tcp"],
        }, sort_keys=True))
        launcher = contents / "MacOS/leaf-ha-launcher"
        shutil.copyfile(Path(__file__).parent / "assets/leaf-ha-launcher", launcher)
        launcher.chmod(0o755)
        atomic_write(contents / "Resources/run.args", ("\n".join(values) + "\n").encode("utf-8"))
        self.runner(["codesign", "--force", "--deep", "-s", "-", str(app)],
                    check=True, capture_output=True, timeout=30)
        return app

    def _stop_app(self):
        launcher = self._app_path() / "Contents/MacOS/leaf-ha-launcher"
        # pgrep patterns are regular expressions: anchor and escape the entire
        # executable path so another app or a command mentioning it cannot match.
        result = self.runner(["pgrep", "-f", "^" + re.escape(str(launcher)) + "$"],
                             check=False, capture_output=True, text=True, timeout=10)
        if result.returncode == 1:
            return
        if result.returncode != 0:
            raise HAError("Home Assistant launcher lookup failed")
        try:
            pids = {int(value) for value in result.stdout.splitlines()}
            if any(pid <= 0 for pid in pids):
                raise ValueError
        except (ValueError, AttributeError):
            raise HAError("Invalid Home Assistant launcher PID") from None
        for pid in pids:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = self.clock() + 10
        while pids:
            for pid in tuple(pids):
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    pids.remove(pid)
            if not pids:
                return
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise HAError("Home Assistant launcher stop failed")
            self.sleep(min(0.1, remaining))

    def _stop(self, *, remove=False):
        path = self._service_path()
        if self.platform == "darwin":
            if not path.exists() and not self._app_path().exists():
                return
            uid = os.getuid() if self.uid is None else self.uid
            result = self.runner(["launchctl", "bootout", f"gui/{uid}", str(path)], check=False, capture_output=True, timeout=15)
            absent_codes = {3, 113}
        else:
            result = self.runner(["systemctl", "--user", "disable" if remove else "stop", *(["--now"] if remove else []), LABEL + ".service"],
                                 check=False, capture_output=True, timeout=15)
            absent_codes = {5}
        if getattr(result, "returncode", 0) not in {0, *absent_codes}:
            raise HAError("Home Assistant service stop failed")
        if self.platform == "darwin":
            self._stop_app()
        if remove:
            path.unlink(missing_ok=True)
            if self.platform == "darwin":
                if self._app_path().exists():
                    shutil.rmtree(self._app_path())
            else:
                self.runner(["systemctl", "--user", "daemon-reload"], check=True, capture_output=True, timeout=15)

    def _start(self):
        path = self._service_path()
        hass, config, log = self.root / "venv/bin/hass", self.root / "config", self.root / "hass.log"
        if self.platform == "darwin":
            app = self._build_app()
            rendered = plistlib.loads(launchd.render_plist(self.home, str(hass)))
            rendered.update(Label=LABEL, ProgramArguments=["/usr/bin/open", "-W", str(app)],
                            AssociatedBundleIdentifiers=[APP_IDENTIFIER],
                            StandardOutPath=str(log), StandardErrorPath=str(log))
            atomic_write(path, plistlib.dumps(rendered, sort_keys=True))
            (self.root / "lan.status").unlink(missing_ok=True)
            uid = os.getuid() if self.uid is None else self.uid
            self.runner(["launchctl", "bootstrap", f"gui/{uid}", str(path)], check=True, capture_output=True, timeout=15)
        else:
            quote = launchd._unit_quote
            unit = ("[Unit]\nDescription=Leaf Home Assistant\nAfter=network-online.target\n\n"
                    "[Service]\nType=simple\n"
                    f"ExecStart={quote(hass, executable=True)} -c {quote(config, executable=True)}\n"
                    "Restart=always\nRestartSec=30\n"
                    f"StandardOutput=append:{launchd._log_path(log)}\n"
                    f"StandardError=append:{launchd._log_path(log)}\n\n[Install]\nWantedBy=default.target\n")
            atomic_write(path, unit.encode())
            self.runner(["systemctl", "--user", "daemon-reload"], check=True, capture_output=True, timeout=15)
            self.runner(["systemctl", "--user", "enable", "--now", LABEL + ".service"], check=True, capture_output=True, timeout=15)

    def _python(self, deadline):
        # The pin has a single inclusive lower bound. Refuse an unsupported
        # constraint rather than silently selecting an incompatible Python.
        requirement = re.fullmatch(r">=(\d+)\.(\d+)\.(\d+)", REQUIRES_PYTHON)
        if requirement is None:
            raise InstallError("Unsupported Python requirement")
        minimum = tuple(int(value) for value in requirement.groups())
        series = ".".join(str(value) for value in minimum[:2])
        if self.platform == "darwin":
            homebrew = Path("/opt/homebrew/bin")
            installed = sorted(path.as_posix() for path in homebrew.glob("python3.*")
                               if re.fullmatch(r"python3\.\d+", path.name))
            candidates = [(homebrew / ("python" + series)).as_posix(), *installed, (homebrew / "python3").as_posix()]
        else:
            candidates = [shutil.which("python" + series), shutil.which("python3"), sys.executable]
        candidates.insert(0, str(self.root / "venv/bin/python"))
        def find():
            for candidate in dict.fromkeys(candidates):
                if not candidate or not Path(candidate).is_file():
                    continue
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise TimeoutError("Installation deadline exceeded")
                try:
                    result = self.runner([candidate, "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
                                         check=True, capture_output=True, text=True, timeout=min(15, remaining))
                    version = tuple(int(value) for value in result.stdout.strip().split("."))
                    if len(version) == 3 and version >= minimum:
                        return candidate
                except (OSError, subprocess.SubprocessError, ValueError):
                    continue
            return None
        python = find()
        if python is None and self.platform == "darwin":
            self._install_command(["/opt/homebrew/bin/brew", "install", "python@" + series], deadline)
            python = find()
        if python is None:
            raise InstallError("No Python satisfies " + REQUIRES_PYTHON)
        return python

    def _install_command(self, argv, deadline):
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise TimeoutError("Installation deadline exceeded")
        with (self.root / "install.log").open("ab") as stream:
            self.runner(argv, check=True, stdout=stream, stderr=subprocess.STDOUT, timeout=remaining)

    def _wait_healthy(self, deadline):
        while True:
            remaining = deadline - self.clock()
            if remaining <= 0:
                break
            if self._healthy(timeout=min(5, remaining)):
                if self.clock() < deadline:
                    return
                break
            remaining = deadline - self.clock()
            if remaining > 0:
                self.sleep(min(5, remaining))
        raise InstallError("Home Assistant did not become healthy before installation deadline")

    def _ensure_loopback_bind(self):
        path = self.root / "config/.storage/http"
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            storage = _json(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            storage = {"version": 2, "minor_version": 2, "key": "http", "data": {
                "stable": {"server_port": 8123, "server_host": ["127.0.0.1"],
                           "cors_allowed_origins": ["https://cast.home-assistant.io"],
                           "login_attempts_threshold": -1, "ip_ban_enabled": True,
                           "ssl_profile": "modern", "use_x_frame_options": True,
                           "created_at": datetime.now(timezone.utc).isoformat(),
                           "error": None, "error_message": None},
                "pending": None, "yaml_migration_done": True}}
        except (ValueError, UnicodeError):
            raise InstallError("Malformed Home Assistant HTTP storage") from None
        if (not isinstance(storage, dict) or not isinstance(storage.get("data"), dict) or
                not isinstance(storage["data"].get("stable"), dict)):
            raise InstallError("Malformed Home Assistant HTTP storage")
        storage["data"]["stable"]["server_host"] = ["127.0.0.1"]
        storage["data"]["pending"] = None
        storage["data"]["yaml_migration_done"] = True
        atomic_write(path, json.dumps(storage, allow_nan=False).encode())

    def _lan_reachable(self) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
                probe.connect(("10.255.255.255", 1))
                host = probe.getsockname()[0]
            if host.startswith("127.") or host == "0.0.0.0":
                return False
            with socket.create_connection((host, self.port), timeout=1):
                return True
        except OSError:
            return False

    def _install(self, *, upgrade=False):
        deadline = self.clock() + INSTALL_BUDGET_S
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            python = self._python(deadline)
            if upgrade or self._service_path().exists():
                self._stop()
            venv = self.root / "venv"
            if venv.exists() and python != str(venv / "bin/python"):
                # A stale venv may belong to an unsupported older interpreter.
                shutil.rmtree(venv)
            if not (venv / "bin/python").exists():
                self._install_command([python, "-m", "venv", str(venv)], deadline)
            pip = str(venv / "bin/python")
            self._install_command([pip, "-m", "pip", "install", "--upgrade", "pip", "wheel"], deadline)
            self._install_command([pip, "-m", "pip", "install", "homeassistant==" + PINNED_VERSION], deadline)
            if self.clock() >= deadline:
                raise TimeoutError("Installation deadline exceeded")
            config = self.root / "config/configuration.yaml"
            if not config.exists():
                atomic_write(config, b"default_config:\nlogger:\n  default: warning\nrecorder:\n  purge_keep_days: 3\n")
            write_json(self.root / "state.json", {"package": "homeassistant", "version": PINNED_VERSION,
                       "status": "installed", "last_health_at": None})
            # HA must never listen beyond localhost; fails closed
            self._ensure_loopback_bind()
            self._start()
            self._wait_healthy(deadline)
            if self._lan_reachable():
                try:
                    self._stop(remove=True)
                finally:
                    raise InstallError("Home Assistant is reachable beyond localhost; stopped")
            state = self.state()
            return {**state, "python": python, "requires_python": REQUIRES_PYTHON}
        except Exception as exc:
            state = self._load("state.json", None) or {"package": "homeassistant", "version": PINNED_VERSION, "last_health_at": None}
            state["status"] = "error"
            try:
                with (self.root / "install.log").open("rb") as stream:
                    stream.seek(0, 2)
                    stream.seek(max(0, stream.tell() - 16384))
                    tail = "\n".join(stream.read().decode("utf-8", errors="replace").splitlines()[-40:])
            except OSError:
                tail = "No installer output"
            state["error"] = "Home Assistant installation failed\n" + tail
            if isinstance(exc, InstallError) and str(exc) == "Home Assistant is reachable beyond localhost; stopped":
                state["error"] = str(exc)
            write_json(self.root / "state.json", state)
            raise InstallError(state["error"]) from None

    def _uninstall(self, keep_data):
        self._stop(remove=True)
        if (self.root / "venv").exists():
            shutil.rmtree(self.root / "venv")
        if not keep_data:
            if (self.root / "config").exists():
                shutil.rmtree(self.root / "config")
            if self.platform == "darwin":
                for name in ("password", "token", "refresh_token"):
                    self.runner(["/usr/bin/security", "delete-generic-password", "-s", LABEL, "-a", name],
                                check=False, capture_output=True, timeout=15)
            else:
                (self.root / "credentials.json").unlink(missing_ok=True)
        for name in ("state.json", "flows.json"):
            (self.root / name).unlink(missing_ok=True)
        self._cached_token, self._token_until = None, 0
        return {"uninstalled": True}

    def _login(self):
        password = self._credential("password")
        if not password:
            raise HAError("Onboarded instance has no managed credentials")
        flow = self._request("POST", "/auth/login_flow", {"client_id": CLIENT_ID,
                             "handler": ["homeassistant", None], "redirect_uri": CLIENT_ID}, authenticated=False)
        result = self._request("POST", "/auth/login_flow/" + self._flow_id(flow["flow_id"]),
                               {"client_id": CLIENT_ID, "username": "leaf-owner", "password": password}, authenticated=False)
        if result.get("type") != "create_entry":
            raise HAError("Managed owner login failed")
        return self._request("POST", "/auth/token", {"client_id": CLIENT_ID, "grant_type": "authorization_code",
                             "code": result["result"]}, authenticated=False, form=True)

    def _onboard(self):
        token = self._credential("token")
        try:
            steps = self._request("GET", "/api/onboarding", authenticated=False)
        except HAError as exc:
            # HA does not register onboarding views on an already-onboarded
            # restart. Other errors must not masquerade as completed setup.
            if exc.status != 404:
                raise
            steps = []
        pending = {step["step"] for step in steps if not step["done"]}
        if not pending and token:
            return {"onboarded": True}
        if "user" in pending:
            token = None  # A reset config must never reuse an old owner's token.
            password = secrets.token_urlsafe(32)
            self._save_credential("password", password)
            owner = self._request("POST", "/api/onboarding/users", {"name": "Leaf Automation",
                                  "username": "leaf-owner", "password": password, "language": "en", "client_id": CLIENT_ID}, authenticated=False)
            credentials = self._request("POST", "/auth/token", {"client_id": CLIENT_ID,
                                        "grant_type": "authorization_code", "code": owner["auth_code"]}, authenticated=False, form=True)
        else:
            refresh = self._credential("refresh_token")
            if refresh:
                credentials = self._request("POST", "/auth/token", {"client_id": CLIENT_ID,
                                            "grant_type": "refresh_token", "refresh_token": refresh}, authenticated=False, form=True)
            else:
                credentials = self._login()
        if credentials.get("refresh_token"):
            self._save_credential("refresh_token", credentials["refresh_token"])
        access = credentials["access_token"]
        for step in ("core_config", "analytics", "integration"):
            if step in pending:
                data = {"client_id": CLIENT_ID, "redirect_uri": CLIENT_ID} if step == "integration" else {}
                self._request("POST", "/api/onboarding/" + step, data, token=access)
        if not token:
            token = self._ws({"type": "auth/long_lived_access_token", "lifespan": 3650,
                              "client_name": "Leaf Automation device agent"}, token=access)
            if not isinstance(token, str) or not token:
                raise HAError("Invalid access token")
            self._save_credential("token", token)
        return {"onboarded": True}

    @staticmethod
    def _flow_id(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            raise HAError("Invalid flow identifier")
        return value

    def _remember(self, result, handler):
        flows = self._load("flows.json", {})
        flow_id = self._flow_id(result["flow_id"])
        if result["type"] in {"create_entry", "abort"}:
            flows.pop(flow_id, None)
        else:
            flows[flow_id] = handler
        write_json(self.root / "flows.json", flows)
        # Never expose HA entry data/result objects (Hue stores bridge keys).
        return {key: value for key, value in result.items() if key in FLOW_KEYS}

    @staticmethod
    def _hue_init_choice(result):
        schema = result.get("data_schema")
        if not isinstance(schema, list):
            return "manual"
        for field in schema:
            if not isinstance(field, dict) or field.get("name") != "id":
                continue
            options = field.get("options", field.get("in", field.get("type")))
            for _ in range(3):
                if not isinstance(options, dict) or not ("options" in options or "in" in options):
                    break
                options = options.get("options", options.get("in"))
            if isinstance(options, (list, dict)):
                for option in options:
                    if isinstance(option, dict):
                        option = option.get("value")
                    elif isinstance(option, (list, tuple)) and len(option) == 2:
                        option = option[0]
                    if isinstance(option, str) and option and option != "manual":
                        return option
        return "manual"

    def _flow_start(self, handler, data=None):
        if handler not in {"hue", "wiz", "group"}:
            raise HAError("Unsupported flow handler")
        if handler == "group":
            group_data = dict(data) if data is not None else {}
            group_type = group_data.pop("group_type", "light")
            if group_type != "light":
                raise HAError("Unsupported group type")
            if data is not None:
                data = {"hide_members": False, "all": False,
                        **{key: value for key, value in group_data.items()
                           if key in {"name", "entities", "hide_members", "all"}}}
        result = self._request("POST", FLOW_PATH, {"handler": handler, "show_advanced_options": False})
        hue_init = False
        if handler == "hue":
            for _ in range(2):
                if result.get("type") != "form" or result.get("step_id") != "init":
                    break
                hue_init = True
                result = self._request("POST", FLOW_PATH + "/" + self._flow_id(result["flow_id"]),
                                       {"id": self._hue_init_choice(result)})
        self._remember(result, handler)
        if handler == "group" and result["type"] == "menu":
            result = self._request("POST", FLOW_PATH + "/" + self._flow_id(result["flow_id"]), {"next_step_id": group_type})
            self._remember(result, handler)
        if (result["type"] == "form" and data is not None and
                (not hue_init or result.get("step_id") == "manual")):
            result = self._request("POST", FLOW_PATH + "/" + self._flow_id(result["flow_id"]), data)
        return self._remember(result, handler)

    def _flow_step(self, flow_id, data):
        flow_id = self._flow_id(flow_id)
        current = self._request("GET", FLOW_PATH + "/" + flow_id)
        handler = current.get("handler")
        if handler not in {"hue", "wiz", "group"}:
            raise HAError("Unsupported flow handler")
        if handler == "hue" and current.get("step_id") == "init" and "id" not in data:
            data = {"id": self._hue_init_choice(current)}
        if handler == "group":
            if current["type"] == "menu":
                data = {"next_step_id": "light"}
            elif current.get("step_id") == "light":
                data = {"hide_members": False, "all": False, **data}
            else:
                raise HAError("Unsupported group type")
        return self._remember(self._request("POST", FLOW_PATH + "/" + flow_id, data), handler)

    def _flow_list(self):
        flows = self._ws({"type": "config_entries/flow/progress"})
        tracked = self._load("flows.json", {})
        known = {flow["flow_id"] for flow in flows}
        for flow_id, handler in list(tracked.items()):
            if handler not in {"hue", "wiz"} or flow_id in known:
                continue
            # The progress command excludes user-source flows. Query those we
            # started, retaining them across operation workers and restarts.
            try:
                flows.append(self._request("GET", FLOW_PATH + "/" + self._flow_id(flow_id)))
            except HAError as exc:
                if exc.status != 404:
                    raise
                tracked.pop(flow_id, None)
        write_json(self.root / "flows.json", tracked)
        result = []
        for flow in flows:
            if flow.get("handler") not in {"hue", "wiz"} or flow.get("type") in {"create_entry", "abort"}:
                continue
            item = {key: flow[key] for key in ("flow_id", "handler", "step_id")}
            context = flow.get("context", {})
            if isinstance(context.get("title_placeholders"), dict):
                title = context["title_placeholders"]
                if isinstance(title.get("name"), str):
                    item["context_title"] = title["name"]
                if isinstance(title.get("host"), str):
                    item["host"] = title["host"]
            if isinstance(context.get("title"), str):
                item["context_title"] = context["title"]
            result.append(item)
        return result

    def handle(self, op, args):
        if op in {"pkg.install", "pkg.upgrade"}:
            if args.get("version") != PINNED_VERSION:
                raise InstallError("Unsupported Home Assistant version")
            return self._install(upgrade=op == "pkg.upgrade")
        if op == "pkg.uninstall":
            if type(args.get("keep_data")) is not bool:
                raise HAError("Invalid keep_data")
            return self._uninstall(args["keep_data"])
        if op == "pkg.health":
            result = self.state() or {"package": "homeassistant", "version": PINNED_VERSION,
                                      "status": "stopped", "last_health_at": None}
            if self.platform == "darwin":
                try:
                    access = (self.root / "lan.status").read_text(encoding="utf-8").strip()
                except FileNotFoundError:
                    access = "unknown"
                result["lan_access"] = access if access in {"ok", "denied"} else "unknown"
            return result
        if op == "ha.onboard":
            return self._onboard()
        if op == "ha.flow.start":
            return self._flow_start(args["handler"], args.get("data"))
        if op == "ha.flow.step":
            return self._flow_step(args["flow_id"], args["data"])
        if op == "ha.flow.list":
            return self._flow_list()
        raise HAError("Unsupported operation")
