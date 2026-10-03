#!/usr/bin/env python3
"""SYNTHETIC, TEST ONLY: recorded String Sizer replay, not live sizing."""
from __future__ import annotations

import _thread
import argparse
import hashlib
import hmac
import importlib
import importlib.util
import io
import json
import os
from pathlib import Path, PureWindowsPath
import platform  # Preload stdlib before a server directory can shadow it.
import queue
import re
import runpy
import secrets
import socket
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LABEL = "SYNTHETIC, TEST ONLY: recorded String Sizer replay, not live sizing."
REPO = Path(__file__).resolve().parent.parent
MANIFEST = REPO / "scripts/fixtures/string_sizer_replay_manifest.json"
MANIFEST_SCHEMA = "leaf.synthetic-string-sizing-fixtures.v1"
RECEIPT_SCHEMA = "leaf.synthetic-string-sizing-receipt.v1"
GRANT_REF = "synthetic-sizing-g2"
TENANT = "demo-tenant"
LOCAL_POSTURES = {"", "local", "test", "development", "dev"}
ENTRY_KEYS = {"id", "path", "fixture_sha256", "request_wire_sha256",
              "response_wire_sha256", "response_encoding"}


class ReplayConfigError(ValueError):
    pass


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def response_span(raw):
    """Walk top-level JSON offsets; preserve the original response value bytes."""
    text = raw.decode("utf-8")
    decoder = json.JSONDecoder()
    pos = 0

    def whitespace(offset):
        while offset < len(text) and text[offset] in " \t\r\n":
            offset += 1
        return offset

    pos = whitespace(pos)
    if text[pos:pos + 1] != "{":
        raise ReplayConfigError("response_wire_sha256: fixture must be an object")
    pos += 1
    span = None
    seen = set()
    while True:
        pos = whitespace(pos)
        if text[pos:pos + 1] == "}":
            break
        key, pos = decoder.raw_decode(text, pos)
        if not isinstance(key, str) or key in seen:
            raise ReplayConfigError("response_wire_sha256: duplicate or invalid key")
        seen.add(key)
        pos = whitespace(pos)
        if text[pos:pos + 1] != ":":
            raise ReplayConfigError("response_wire_sha256: missing colon")
        start = whitespace(pos + 1)
        _, pos = decoder.raw_decode(text, start)
        if key == "response":
            span = text[start:pos].encode("utf-8")
        pos = whitespace(pos)
        if text[pos:pos + 1] == "}":
            break
        if text[pos:pos + 1] != ",":
            raise ReplayConfigError("response_wire_sha256: missing separator")
        pos += 1
    if span is None:
        raise ReplayConfigError("response_wire_sha256: missing response")
    return span


def sizing_module(server_dir=None):
    directory = str(Path(server_dir or REPO / "server").resolve())
    sys.path.insert(0, directory)
    return importlib.import_module("solar_sizing_client")


def load_manifest(path, repo_root):
    root = Path(repo_root).resolve()
    try:
        manifest = json.loads(Path(path).read_bytes())
    except (OSError, ValueError) as exc:
        raise ReplayConfigError("manifest: unreadable JSON") from exc
    if not isinstance(manifest, dict):
        raise ReplayConfigError("schema: expected object")
    root_keys = {"schema", "label", "fixtures"}
    if set(manifest) != root_keys:
        raise ReplayConfigError("manifest keys: missing " + repr(sorted(root_keys - set(manifest)))
                                + "; extra " + repr(sorted(set(manifest) - root_keys)))
    for key, expected in (("schema", MANIFEST_SCHEMA), ("label", LABEL)):
        if manifest.get(key) != expected:
            raise ReplayConfigError(key + ": unsupported value")
    entries = manifest.get("fixtures")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 16:
        raise ReplayConfigError("fixtures: expected 1..16 entries")
    result, ids = {}, set()
    client = sizing_module()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ReplayConfigError("fixtures keys: expected object")
        if set(entry) != ENTRY_KEYS:
            raise ReplayConfigError("fixtures keys: missing " + repr(sorted(ENTRY_KEYS - set(entry)))
                                    + "; extra " + repr(sorted(set(entry) - ENTRY_KEYS)))
        for key, value in entry.items():
            if not isinstance(value, str):
                raise ReplayConfigError(key + ": expected string")
        fixture_id = entry["id"]
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", fixture_id) or fixture_id in ids:
            raise ReplayConfigError("id: invalid or repeated")
        ids.add(fixture_id)
        relative = Path(entry["path"])
        windows = PureWindowsPath(entry["path"])
        if (relative.is_absolute() or windows.is_absolute() or windows.drive
                or ".." in relative.parts or ".." in windows.parts):
            raise ReplayConfigError("path: repository-relative path required")
        fixture_path = (root / relative).resolve()
        if not fixture_path.is_relative_to(root):
            raise ReplayConfigError("path: outside repository")
        try:
            raw = fixture_path.read_bytes()
        except OSError as exc:
            raise ReplayConfigError("path: unreadable fixture") from exc
        if sha256(raw) != entry["fixture_sha256"]:
            raise ReplayConfigError("fixture_sha256: pin mismatch")
        try:
            fixture = json.loads(raw)
            request = client.wire_bytes(client.SizingRequest.model_validate(fixture["request"]))
        except (ValueError, TypeError, KeyError) as exc:
            raise ReplayConfigError("request_wire_sha256: invalid request") from exc
        digest = sha256(request)
        if digest != entry["request_wire_sha256"]:
            raise ReplayConfigError("request_wire_sha256: pin mismatch")
        if digest in result:
            raise ReplayConfigError("request_wire_sha256: duplicate request digest")
        if entry["response_encoding"] != "original-utf8-json-value-span":
            raise ReplayConfigError("response_encoding: unsupported value")
        try:
            response = response_span(raw)
            if json.loads(response) != fixture["response"]:
                raise ValueError()
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            raise ReplayConfigError("response_wire_sha256: invalid response span") from exc
        if sha256(response) != entry["response_wire_sha256"]:
            raise ReplayConfigError("response_wire_sha256: pin mismatch")
        result[digest] = (fixture_id, response, sha256(response))
    return result


class ReplayServer:
    def __init__(self, fixture_map, bearer_token, *, read_timeout_s=5.0,
                 max_body_bytes=65536, max_events=1024):
        self.fixture_map = dict(fixture_map)
        self.events = []
        self.failed = False
        self.on_event = None
        self.lock = threading.RLock()
        self.closed = False
        owner = self

        class DeadlineServer(ThreadingHTTPServer):
            daemon_threads = False

            def get_request(self):
                connection, address = super().get_request()
                with owner.lock:
                    accepted[connection] = time.monotonic() + read_timeout_s
                return connection, address

        accepted = {}

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                with owner.lock:
                    self.deadline = accepted.pop(self.connection)
                self.received = False
                self.recorded = False
                self.deadline_expired = False
                handler = self

                class Incoming(io.RawIOBase):
                    def readable(self):
                        return True

                    def readinto(self, buffer):
                        count = handler.connection.recv_into(buffer)
                        if count:
                            handler.received = True
                        return count

                self.rfile.close()
                self.rfile = io.BufferedReader(Incoming())
                self.timer = threading.Timer(max(0, self.deadline - time.monotonic()),
                                             self.expire)
                self.timer.daemon = True
                self.timer.start()

            def expire(self):
                self.deadline_expired = True
                try:
                    self.connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            def identity(self):
                words = getattr(self, "raw_requestline", b"").split()
                if len(words) < 2:
                    return None, None
                method = words[0].decode("ascii", errors="replace")
                target = words[1]
                return method, "/string-length" if target == b"/string-length" else "other"

            def record_once(self, status, digest=None, fixture_id=None, response_digest=None):
                if not self.recorded:
                    self.recorded = True
                    method, path = self.identity()
                    owner.record(method, path, status, digest, fixture_id,
                                 response_digest, max_events)

            def handle(self):
                try:
                    super().handle()
                except OSError:
                    pass

            def finish(self):
                self.timer.cancel()
                self.timer.join()
                if self.received and not self.recorded:
                    self.record_once("timeout")
                try:
                    super().finish()
                except OSError:
                    pass

            def send_error(self, code, message=None, explain=None):
                # Parser messages can contain client text; send only a fixed JSON refusal.
                self.close_connection = True
                if self.deadline_expired or time.monotonic() >= self.deadline:
                    if self.received:
                        self.record_once("timeout")
                    return
                self.record_once(code)
                body = json.dumps({"status": code, "label": LABEL}).encode("utf-8")
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

            def log_message(self, *args):
                pass  # Never log headers, tokens or incoming bodies.

            def handle_request(self):
                self.close_connection = True
                if self.deadline_expired or time.monotonic() >= self.deadline:
                    self.record_once("timeout")
                    return
                digest = fixture_id = response_digest = None
                status, body = 200, None
                if self.identity()[1] != "/string-length":
                    status = 404
                elif self.command != "POST":
                    status = 405
                else:
                    auth = self.headers.get_all("Authorization", [])
                    if (len(auth) != 1 or not hmac.compare_digest(
                            auth[0].encode("utf-8"), ("Bearer " + bearer_token).encode("utf-8"))):
                        status = 401
                    elif self.headers.get_all("Content-Type", []) != ["application/json"]:
                        status = 415
                    else:
                        lengths = self.headers.get_all("Content-Length", [])
                        if (self.headers.get_all("Transfer-Encoding") is not None
                                or len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0])):
                            status = 411
                        elif len(lengths[0]) > 20 or int(lengths[0]) > max_body_bytes:
                            status = 413
                        else:
                            length = int(lengths[0])
                            try:
                                chunks, remaining = [], length
                                while remaining:
                                    timeout = self.deadline - time.monotonic()
                                    if timeout <= 0:
                                        raise TimeoutError()
                                    self.connection.settimeout(timeout)
                                    chunk = self.rfile.read1(remaining)
                                    if not chunk:
                                        raise TimeoutError()
                                    chunks.append(chunk)
                                    remaining -= len(chunk)
                                raw = b"".join(chunks)
                            except (OSError, TimeoutError):
                                self.record_once("timeout")
                                return
                            digest = sha256(raw)
                            match = owner.fixture_map.get(digest)
                            if match is None:
                                status = 409
                                body = {"classification": "SYNTHETIC_REPLAY_REQUEST_MISMATCH",
                                        "expected_sha256": sorted(owner.fixture_map),
                                        "actual_sha256": digest, "label": LABEL}
                            else:
                                fixture_id, body, response_digest = match
                self.record_once(status, digest, fixture_id, response_digest)
                if not isinstance(body, bytes):
                    body = json.dumps(body or {"status": status, "label": LABEL}).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

            # Catch all methods, including unknown verbs, with the same ordered checks.
            def __getattr__(self, name):
                if name.startswith("do_"):
                    return self.handle_request
                raise AttributeError(name)

        self.server = DeadlineServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}/string-length"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .05}, daemon=True)
        self.thread.start()

    def record(self, method, path, status, digest, fixture_id, response_digest, max_events):
        if method is not None and method not in {"POST", "GET", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
            method = "other"
        if path is not None and path != "/string-length":
            path = "other"
        with self.lock:
            if status != 200 or len(self.events) >= max_events:
                self.failed = True
            self.events.append({"method": method, "path": path, "status": status,
                                "request_sha256": digest, "fixture_id": fixture_id,
                                "response_sha256": response_digest})
            if self.on_event is not None:
                self.on_event()

    def close(self):
        if not self.closed:
            self.closed = True
            self.server.shutdown()
            self.server.server_close()
            self.thread.join()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, prefix="." + path.name,
                                         delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_grant_file(run_root, *, ttl_s):
    token = secrets.token_urlsafe(32)
    path = Path(run_root) / "synthetic-cloud-grants.json"
    atomic_json(path, {GRANT_REF: {"tenant_id": TENANT, "audience": "https://api.leafdesign.ai",
                                "expires_at": int(time.time()) + ttl_s + 300,
                                "access_token": token}})
    return path, token


def prepare_child(role, sizing_url, server_dir=None):
    if role not in {"broker", "app"}:
        raise ReplayConfigError("role: broker or app required")
    if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}/string-length", sizing_url):
        raise ReplayConfigError("sizing-url: loopback replay URL required")
    directory = Path(server_dir or REPO / "server").resolve()
    client = sizing_module(directory)
    requests = importlib.import_module("requests")

    class IsolatedRequests:
        RequestException = requests.RequestException

        @staticmethod
        @contextmanager
        def post(*args, **kwargs):
            with requests.Session() as session:
                session.trust_env = False
                with session.post(*args, **kwargs) as response:
                    yield response

    client.SIZING_URL = sizing_url
    client.requests = IsolatedRequests
    return directory / (role + ".py")


def verify_receipt(path, run_id):
    try:
        receipt = json.loads(Path(path).read_bytes())
    except (OSError, ValueError):
        print("Synthetic sizing receipt: unreadable JSON")
        return 1
    for key, expected in (("schema", RECEIPT_SCHEMA), ("label", LABEL),
                          ("run_id", run_id), ("ok", True)):
        if not isinstance(receipt, dict) or receipt.get(key) != expected or (
                key == "ok" and receipt.get(key) is not True):
            print("Synthetic sizing receipt: invalid " + key)
            return 1
    return 0


def supervise(args):
    if os.environ.get("LEAF_AUTH_LIVE") != "0":
        print("Synthetic string sizing runs in account mode only", file=sys.stderr)
        return 2
    if any(os.environ.get(key, "").strip().lower() not in LOCAL_POSTURES
           for key in ("LEAF_RUNTIME_ENV", "LEAF_ENV")):
        print("Synthetic string sizing refuses a deployed runtime posture", file=sys.stderr)
        return 2
    root = Path(args.run_root).resolve()
    receipt_path = root / "synthetic-sizing-receipt.json"
    receipt_path.unlink(missing_ok=True)
    manifest_path = Path(args.manifest or MANIFEST).resolve()
    fixtures = load_manifest(manifest_path, REPO)
    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw)
    grant_path, token = write_grant_file(root, ttl_s=args.max_seconds)
    listener = ReplayServer(fixtures, token)
    receipt = {"schema": RECEIPT_SCHEMA, "label": LABEL, "run_id": args.run_id,
               "record": "sf-w3-sizing-replay", "pid": os.getpid(),
               "manifest_sha256": sha256(manifest_raw),
               "fixtures": [{key: entry[key] for key in
                             ("id", "fixture_sha256", "request_wire_sha256", "response_wire_sha256")}
                            for entry in manifest["fixtures"]],
               "endpoint": listener.url, "grant_ref": GRANT_REF, "tenant_id": TENANT,
               "bootstrap_roles": [], "events": [], "ttl_expired": False, "ok": True,
               "limitations": [LABEL, "synthetic fixture, not a live-service capture",
                               "no browser walk (G2b)", "no staging sizing (G3)",
                               "a drawing sized under one replay port does not re-verify under another"]}
    main_failed = False

    def publish():
        with listener.lock:
            receipt["events"] = list(listener.events)
            receipt["ok"] = not (listener.failed or receipt["ttl_expired"] or main_failed)
            atomic_json(receipt_path, receipt)

    listener.on_event = publish
    finished = threading.Event()

    def watchdog():
        if not finished.wait(args.max_seconds):
            with listener.lock:
                receipt["ttl_expired"] = True
                publish()
            _thread.interrupt_main()

    launcher = None
    previous_argv = sys.argv
    code = 1
    watcher = threading.Thread(target=watchdog, daemon=True)
    try:
        publish()
        launcher_path = Path(args.launcher or REPO / "scripts/start-leaf.py").resolve()
        server_dir = Path(args.server_dir or REPO / "server").resolve()
        spec = importlib.util.spec_from_file_location("_synthetic_sizing_launcher", launcher_path)
        launcher = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(launcher)
        original_spawn = launcher.spawn

        def spawn(name, cmd, cwd, env):
            child_env = dict(env)
            child_env.pop("DATABASE_URL", None)
            child_env.pop("LEAF_CLOUD_GRANTS_FILE", None)
            role = None
            if Path(cwd).resolve() == server_dir:
                for candidate in ("broker", "app"):
                    if cmd == [sys.executable, candidate + ".py"]:
                        role = candidate
                        break
            if role is not None:
                cmd = [sys.executable, "-B", str(Path(__file__).resolve()), "bootstrap",
                       "--role", role, "--sizing-url", listener.url,
                       "--server-dir", str(server_dir)]
                if role == "broker":
                    child_env["LEAF_CLOUD_GRANTS_FILE"] = str(grant_path)
                with listener.lock:
                    receipt["bootstrap_roles"].append(role)
                    publish()
            return original_spawn(name, cmd, cwd, child_env)

        launcher.spawn = spawn
        launcher_args = list(args.launcher_args)
        if launcher_args[:1] == ["--"]:
            launcher_args.pop(0)
        sys.argv = [str(launcher_path), *launcher_args]
        watcher.start()
        code = launcher.main()
    except KeyboardInterrupt:
        code = 1
    except SystemExit:
        code = 1
    except Exception:
        # A stable message avoids leaking launcher exception payloads into logs.
        print("Synthetic sizing supervisor: launcher failed", file=sys.stderr)
        code = 1
    finally:
        finished.set()
        try:
            if launcher is not None and hasattr(launcher, "cleanup"):
                launcher.cleanup()
        except Exception:
            code = 1
            print("Synthetic sizing supervisor: cleanup failed", file=sys.stderr)
        finally:
            listener.close()
            main_failed = code != 0
            publish()
            sys.argv = previous_argv
            if watcher.ident is not None:
                watcher.join()
    return 0 if code == 0 and receipt["ok"] else 1


def main():
    parser = argparse.ArgumentParser(description=LABEL)
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap")
    bootstrap.add_argument("--role", choices=("broker", "app"), required=True)
    bootstrap.add_argument("--sizing-url", required=True)
    bootstrap.add_argument("--server-dir", type=Path)
    supervisor = commands.add_parser("supervise")
    supervisor.add_argument("--run-root", type=Path, required=True)
    supervisor.add_argument("--run-id", required=True)
    supervisor.add_argument("--manifest", type=Path)
    supervisor.add_argument("--max-seconds", type=int, default=1800)
    supervisor.add_argument("--launcher", type=Path)
    supervisor.add_argument("--server-dir", type=Path)
    supervisor.add_argument("launcher_args", nargs=argparse.REMAINDER)
    verify = commands.add_parser("verify-receipt")
    verify.add_argument("--receipt", type=Path, required=True)
    verify.add_argument("--run-id", required=True)
    args = parser.parse_args()
    try:
        if args.command == "bootstrap":
            entry = prepare_child(args.role, args.sizing_url, args.server_dir)
            sys.argv = [str(entry)]
            runpy.run_path(str(entry), run_name="__main__")
            return 0
        if args.command == "verify-receipt":
            return verify_receipt(args.receipt, args.run_id)
        if not 1 <= args.max_seconds <= 7200:
            parser.error("--max-seconds must be in 1..7200")
        return supervise(args)
    except ReplayConfigError as exc:
        print("Synthetic sizing configuration: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
