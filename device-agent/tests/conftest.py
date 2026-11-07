"""Independent wire verifier and loopback package fake (no external services)."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import sys
import threading
import time
from types import SimpleNamespace
from urllib.request import Request
from uuid import uuid4

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from leaf_agent.config import Config, write_json
from leaf_agent.client import Client, secure_opener
from leaf_agent.packages import Registry


def command(op="pkg.health", args=None, **overrides):
    now = datetime.now(timezone.utc)
    return {"command_id": str(uuid4()), "op": op, "args": {"package": "echo"} if args is None else args,
            "issued_at": (now - timedelta(seconds=1)).isoformat(),
            "expires_at": (now + timedelta(minutes=10)).isoformat(), **overrides}


class Echo:
    """Test-only handler. Deliberately absent from the production registry."""
    def __init__(self, home, delay=0):
        self.home, self.delay = Path(home), delay

    def auth_headers(self):
        return {"Authorization": "Bearer local-only-secret"}

    def state(self):
        return {"package": "echo", "version": "1", "status": "running", "last_health_at": None}

    def handle(self, op, args):
        time.sleep(self.delay)
        counter = self.home / "echo-count"
        count = int(counter.read_text()) + 1 if counter.exists() else 1
        counter.write_text(str(count))
        return {"op": op, "args": args, "count": count}


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("LEAF_AGENT_HOME", str(tmp_path / "agent home"))
    cfg = Config()
    cfg.home.mkdir()
    cfg.pin_catalog()
    try:
        yield cfg
    finally:
        logger = logging.getLogger("leaf_agent")
        for handler in list(logger.handlers):
            if isinstance(handler, RotatingFileHandler):
                logger.removeHandler(handler)
                handler.close()


@pytest.fixture
def registry(config):
    value = Registry()
    value.register("echo", Echo(config.home))
    return value


@pytest.fixture
def enrolled(config, studio, local_package):
    catalog = list(config.catalog().values())
    client = Client(config, studio.origin, sleep=lambda seconds: None)
    client.register(studio.token)
    write_json(config.home / "catalog.json", catalog)
    return client


def start_server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


@pytest.fixture
def studio(monkeypatch):
    state = SimpleNamespace(token="A" * 43, device_id=str(uuid4()), public_key=None,
                            commands=[], results=[], heartbeats=[], nonces=set(), seen=[],
                            revoked=False, fail_status=None, fail_path=None, used=False,
                            lock=threading.Lock())

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, body=None):
            raw = b"" if body is None else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def error(self, status, code):
            self.reply(status, {"error": {"code": code, "message": "request rejected"}})

        def do_POST(self):
            self.route()

        def do_GET(self):
            self.route()

        def route(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = json.loads(raw) if raw else None
            if self.path == "/api/devices/agent/register":
                if state.used or body["token"] != state.token:
                    return self.error(401, "enrollment_invalid")
                assert set(body) == {"token", "public_key", "agent_version", "platform", "hostname"}
                state.public_key = Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(body["public_key"] + "="))
                state.used = True
                return self.reply(201, {"device_id": state.device_id, "server_time": "2026-11-02T10:00:00Z"})
            if state.revoked:
                return self.error(401, "device_revoked")
            try:
                assert self.headers["X-Leaf-Device"] == state.device_id
                timestamp, nonce = self.headers["X-Leaf-Timestamp"], self.headers["X-Leaf-Nonce"]
                assert re.fullmatch(r"[0-9]+", timestamp)
                assert abs(time.time() - int(timestamp)) <= 60
                assert re.fullmatch(r"[A-Za-z0-9_-]{22}", nonce)
                signed = "\n".join(["LEAF-DEVICE-V1", self.command, self.path, timestamp, nonce,
                                    hashlib.sha256(raw).hexdigest()]).encode()
                state.public_key.verify(base64.urlsafe_b64decode(self.headers["X-Leaf-Signature"] + "=="), signed)
            except Exception:
                return self.error(401, "signature_invalid")
            with state.lock:
                if nonce in state.nonces:
                    return self.error(401, "signature_replay")
                state.nonces.add(nonce)
            state.seen.append((self.command, self.path, raw, dict(self.headers)))
            if state.fail_status and (state.fail_path is None or self.path.startswith(state.fail_path)):
                return self.error(state.fail_status, "signature_invalid" if state.fail_status == 401 else "service_unavailable")
            if self.path.startswith("/api/devices/agent/poll?wait="):
                if not state.commands:
                    return self.reply(204)
                commands, state.commands = state.commands[:8], state.commands[8:]
                return self.reply(200, {"commands": commands})
            if self.path == "/api/devices/agent/results":
                assert set(body) == {"results"}
                state.results.extend(body["results"])
                return self.reply(200, {"accepted": len(body["results"])})
            if self.path == "/api/devices/agent/heartbeat":
                assert set(body) == {"agent_version", "packages", "free_mem_mb", "uptime_s"}
                state.heartbeats.append(body)
                return self.reply(200, {"server_time": "2026-11-02T10:00:00Z"})
            self.error(404, "not_found")

    server, thread = start_server(Handler)
    state.origin = f"https://127.0.0.1:{server.server_port}"

    class LoopbackTransport:
        """Only the injected test transport maps HTTPS to the plain fake server.

        Production origin validation remains HTTPS-only and its default opener
        always verifies certificates. Request targets/body/signatures stay exact.
        """
        def __init__(self):
            self.opener = secure_opener()

        def open(self, request, timeout):
            assert request.full_url.startswith(state.origin + "/")
            local_url = "http://127.0.0.1:" + str(server.server_port) + request.full_url[len(state.origin):]
            local = Request(local_url, data=request.data, headers=dict(request.header_items()), method=request.get_method())
            return self.opener.open(local, timeout=timeout)

    state.opener = LoopbackTransport()
    monkeypatch.setattr("leaf_agent.client.secure_opener", lambda: state.opener)
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def local_package(config):
    state = SimpleNamespace(requests=[], status=200, content_type="application/json", raw=None, redirect=None)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.route()

        def do_POST(self):
            self.route()

        def route(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            state.requests.append((self.path, dict(self.headers), raw))
            data = state.raw if state.raw is not None else json.dumps({"path": self.path, "authorization": self.headers.get("Authorization"),
                                                                     "body": json.loads(raw) if raw else None}).encode()
            self.send_response(state.status)
            self.send_header("Content-Type", state.content_type)
            if state.redirect:
                self.send_header("Location", state.redirect)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server, thread = start_server(Handler)
    state.port = server.server_port
    write_json(config.home / "catalog.json", [{"package": "echo", "display_name": "Echo", "version": "1",
        "port": state.port, "health_path": "/api/", "relay_allow": [
            {"method": "GET", "path_prefix": "/api/states"},
            {"method": "POST", "path_prefix": "/api/services/light/"}]}])
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
