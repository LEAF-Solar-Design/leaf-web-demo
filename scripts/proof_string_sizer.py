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
import signal
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
ROOFTOP_SIZING_MANIFEST = REPO / "scripts/fixtures/rooftop_string_sizer_replay_manifest.json"
ROOFTOP_SOLVE_MANIFEST = REPO / "scripts/fixtures/rooftop_solve_replay_manifest.json"
ROOFTOP_LABEL = "SYNTHETIC, TEST ONLY: Rooftop solve replay with fixture-derived panel IDs, not live solving."
ROOFTOP_SCHEMA = "leaf.synthetic-rooftop-solve-fixtures.v1"
ROOFTOP_PACKAGE_SHA = "53ed4b7b1f8d5c22c9a5e991d0eafd100e6fb598fe702114b091d3f7d66c4618"
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


def load_manifest(path, repo_root, *, label=LABEL):
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
    for key, expected in (("schema", MANIFEST_SCHEMA), ("label", label)):
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
        try:
            client.validate_response(json.loads(response))
        except (ValueError, client.CloudError) as exc:
            raise ReplayConfigError("response: invalid sizing model") from exc
        result[digest] = (fixture_id, response, sha256(response))
    return result


def solve_module():
    sizing_module()
    return importlib.import_module("leaf_cloud_client")


def contained_path(root, value, within=None):
    if not isinstance(value, str):
        raise ReplayConfigError("path: expected string")
    relative, windows = Path(value), PureWindowsPath(value)
    if (relative.is_absolute() or windows.is_absolute() or windows.drive
            or ".." in relative.parts or ".." in windows.parts):
        raise ReplayConfigError("path: repository-relative path required")
    target = (Path(root).resolve() / relative).resolve()
    if not target.is_relative_to(Path(root).resolve()):
        raise ReplayConfigError("path: outside repository")
    if within is not None and not target.is_relative_to((Path(root).resolve() / within).resolve()):
        raise ReplayConfigError("path: outside " + within)
    return target


def load_solve_manifest(path, repo_root):
    """Validate the fixed derived package before creating any listener or grant."""
    try:
        manifest = json.loads(Path(path).read_bytes())
        if (set(manifest) != {"schema", "label", "package", "fixtures"}
                or manifest["schema"] != ROOFTOP_SCHEMA or manifest["label"] != ROOFTOP_LABEL):
            raise ReplayConfigError("solve manifest: unsupported keys or profile")
        pin = manifest["package"]
        if set(pin) != {"path", "bytes", "sha256"}:
            raise ReplayConfigError("package: invalid keys")
        raw = contained_path(repo_root, pin["path"], within="scripts/fixtures").read_bytes()
        if (pin["bytes"] != 366852 or pin["sha256"] != ROOFTOP_PACKAGE_SHA
                or len(raw) != pin["bytes"] or sha256(raw) != pin["sha256"]):
            raise ReplayConfigError("package: pin mismatch")
        package = json.loads(raw)
        if (set(package) != {"schema", "label", "source_intake_sha256", "upload_intake_sha256",
                             "source_solve_fixture_sha256", "fixtures"}
                or package["schema"] != ROOFTOP_SCHEMA or package["label"] != ROOFTOP_LABEL):
            raise ReplayConfigError("package: invalid keys or profile")
        entries = manifest["fixtures"]
        pairs = package["fixtures"]
        if not isinstance(entries, list) or len(entries) != 7 or len(pairs) != 7:
            raise ReplayConfigError("fixtures: expected seven solves")
        client = solve_module()
        result, ids = {}, set()
        keys = {"id", "request_bytes", "request_wire_sha256", "response_bytes",
                "response_wire_sha256", "response_encoding"}
        digests = [entry["request_wire_sha256"] for entry in entries]
        if len(set(digests)) != len(digests):
            raise ReplayConfigError("request_wire_sha256: duplicate request digest")
        for entry, pair in zip(entries, pairs):
            if set(entry) != keys or set(pair) != {"id", "request", "response"}:
                raise ReplayConfigError("fixtures: invalid keys")
            identity = entry["id"]
            if (not isinstance(identity, str) or not re.fullmatch(r"rooftop-piece-[0-9]+", identity)
                    or identity in ids or pair["id"] != identity):
                raise ReplayConfigError("id: invalid or repeated")
            ids.add(identity)
            if entry["response_encoding"] != "canonical-json":
                raise ReplayConfigError("response_encoding: unsupported value")
            request = client.StringerRequest.model_validate(pair["request"])
            request_raw = client.canonical_bytes(request.wire_payload())
            response_raw = client.canonical_bytes(pair["response"])
            response = client.StringerResponse.model_validate_json(response_raw)
            response.original_visited_path(request)
            digest = sha256(request_raw)
            if digest in result:
                raise ReplayConfigError("request_wire_sha256: duplicate request digest")
            if (len(request_raw) != entry["request_bytes"] or digest != entry["request_wire_sha256"]
                    or len(response_raw) != entry["response_bytes"]
                    or sha256(response_raw) != entry["response_wire_sha256"]):
                raise ReplayConfigError("wire: pin mismatch")
            result[digest] = (identity, response_raw, sha256(response_raw))
        return result
    except ReplayConfigError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ReplayConfigError("solve manifest: invalid package or model") from exc


class ReplayServer:
    def __init__(self, fixture_map, bearer_token, *, read_timeout_s=5.0,
                 max_body_bytes=65536, max_events=1024, endpoint="/string-length", label=LABEL):
        if endpoint not in {"/string-length", "/api/ml/"}:
            raise ReplayConfigError("endpoint: unsupported replay path")
        self.endpoint = endpoint
        self.label = label
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
                return method, endpoint if target == endpoint.encode("ascii") else "other"

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
                body = json.dumps({"status": code, "label": label}).encode("utf-8")
                try:
                    self.send_response(code)
                    self.send_header("X-Leaf-Synthetic-Replay", label)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

            def send_response(self, code, message=None):
                # The stdlib suppresses headers for malformed/HTTP-0.9 request
                # lines. Every emitted refusal still needs its synthetic label.
                if self.request_version == "HTTP/0.9":
                    self.request_version = "HTTP/1.0"
                super().send_response(code, message)

            def log_message(self, *args):
                pass  # Never log headers, tokens or incoming bodies.

            def handle_request(self):
                self.close_connection = True
                if self.deadline_expired or time.monotonic() >= self.deadline:
                    self.record_once("timeout")
                    return
                digest = fixture_id = response_digest = None
                status, body = 200, None
                if self.identity()[1] != endpoint:
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
                                        "actual_sha256": digest, "label": label}
                            else:
                                fixture_id, body, response_digest = match
                self.record_once(status, digest, fixture_id, response_digest)
                if not isinstance(body, bytes):
                    body = json.dumps(body or {"status": status, "label": label}).encode("utf-8")
                try:
                    self.send_response(status)
                    self.send_header("X-Leaf-Synthetic-Replay", label)
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
        self.url = f"http://127.0.0.1:{self.server.server_port}{endpoint}"
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": .05}, daemon=True)
        self.thread.start()

    def record(self, method, path, status, digest, fixture_id, response_digest, max_events):
        if method is not None and method not in {"POST", "GET", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
            method = "other"
        if path is not None and path != self.endpoint:
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


def prepare_child(role, sizing_url, server_dir=None, *, solver_url=None):
    if role not in {"broker", "app"}:
        raise ReplayConfigError("role: broker or app required")
    if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}/string-length", sizing_url):
        raise ReplayConfigError("sizing-url: loopback replay URL required")
    if solver_url is not None and not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}/api/ml/", solver_url):
        raise ReplayConfigError("solver-url: loopback replay URL required")
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
    if solver_url is not None:
        solver = importlib.import_module("leaf_cloud_client")
        solver.SOLVER_URL = solver_url
        solver.requests = IsolatedRequests
    return directory / (role + ".py")


def verify_receipt(path, run_id, profile="string-sizing"):
    if profile not in {"string-sizing", "rooftop"}:
        print("Synthetic sizing receipt: invalid profile")
        return 1
    try:
        receipt = json.loads(Path(path).read_bytes())
    except (OSError, ValueError):
        print("Synthetic sizing receipt: unreadable JSON")
        return 1
    rooftop = profile == "rooftop"
    if isinstance(receipt, dict) and (
            (rooftop and receipt.get("profile") != "rooftop")
            or (not rooftop and "profile" in receipt)):
        print("Synthetic sizing receipt: invalid profile")
        return 1
    for key, expected in (("schema", RECEIPT_SCHEMA), ("label", ROOFTOP_LABEL if rooftop else LABEL),
                          ("run_id", run_id), ("ok", True)):
        if not isinstance(receipt, dict) or receipt.get(key) != expected or (
                key == "ok" and receipt.get(key) is not True):
            print("Synthetic sizing receipt: invalid " + key)
            return 1
    if rooftop:
        try:
            expected = rooftop_pins()
            if (receipt.get("pins") != expected or receipt.get("cleanup_complete") is not True
                    or receipt.get("record") != "sf-sp21a-rooftop-replay"
                    or receipt.get("ttl_expired") is not False
                    or receipt.get("bootstrap_roles") != ["broker", "app"]):
                raise ValueError()
            sizing_manifest = json.loads(ROOFTOP_SIZING_MANIFEST.read_bytes())
            solve_manifest = json.loads(ROOFTOP_SOLVE_MANIFEST.read_bytes())
            sizing_pins = [{key: entry[key] for key in
                            ("id", "fixture_sha256", "request_wire_sha256", "response_wire_sha256")}
                           for entry in sizing_manifest["fixtures"]]
            if (receipt.get("manifest_sha256") != expected["sizing_manifest_sha256"]
                    or receipt.get("fixtures") != sizing_pins
                    or receipt.get("solve_fixtures") != solve_manifest["fixtures"]):
                raise ValueError()
            endpoints = receipt["endpoints"]
            if set(endpoints) != {"/string-length", "/api/ml/"}:
                raise ValueError()
            for endpoint, url in endpoints.items():
                if not re.fullmatch(r"http://127\.0\.0\.1:[0-9]{1,5}" + re.escape(endpoint), url):
                    raise ValueError()
            if receipt["endpoint"] != endpoints["/string-length"]:
                raise ValueError()
            maps = {"/string-length": load_manifest(ROOFTOP_SIZING_MANIFEST, REPO, label=ROOFTOP_LABEL),
                    "/api/ml/": load_solve_manifest(ROOFTOP_SOLVE_MANIFEST, REPO)}
            for event in receipt["events"]:
                endpoint = event["path"]
                match = maps[endpoint][event["request_sha256"]]
                if (event["method"] != "POST" or event["status"] != 200
                        or event["endpoint"] != endpoints[endpoint]
                        or event["fixture_id"] != match[0] or event["response_sha256"] != match[2]):
                    raise ValueError()
        except (OSError, ValueError, TypeError, KeyError, ReplayConfigError):
            print("Synthetic Rooftop receipt: invalid profile proof")
            return 1
    return 0


def rooftop_pins():
    return {"sizing_manifest_sha256": sha256(ROOFTOP_SIZING_MANIFEST.read_bytes()),
            "solve_manifest_sha256": sha256(ROOFTOP_SOLVE_MANIFEST.read_bytes()),
            "package_sha256": ROOFTOP_PACKAGE_SHA, "package_bytes": 366852}


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
    stop_path = root / "stop-request"
    stop_path.unlink(missing_ok=True)
    profile = getattr(args, "profile", "string-sizing")
    if profile not in {"string-sizing", "rooftop"}:
        raise ReplayConfigError("profile: unsupported value")
    rooftop = profile == "rooftop"
    label = ROOFTOP_LABEL if rooftop else LABEL
    if rooftop and args.manifest is not None:
        raise ReplayConfigError("manifest: Rooftop uses the owned pinned manifests")
    manifest_path = Path(args.manifest or (ROOFTOP_SIZING_MANIFEST if rooftop else MANIFEST)).resolve()
    fixtures = load_manifest(manifest_path, REPO, label=label) if rooftop else load_manifest(manifest_path, REPO)
    solves = load_solve_manifest(ROOFTOP_SOLVE_MANIFEST, REPO) if rooftop else None
    manifest_raw = manifest_path.read_bytes()
    manifest = json.loads(manifest_raw)
    grant_path, token = write_grant_file(root, ttl_s=args.max_seconds)
    listener = ReplayServer(fixtures, token, label=label) if rooftop else ReplayServer(fixtures, token)
    solver_listener = None
    if rooftop:
        try:
            solver_listener = ReplayServer(solves, token, endpoint="/api/ml/", label=label,
                                           max_body_bytes=solve_module().MAX_RESPONSE_BYTES)
            solver_listener.lock = listener.lock
        except BaseException:
            listener.close()
            raise
    listeners = [listener] + ([solver_listener] if solver_listener is not None else [])
    receipt = {"schema": RECEIPT_SCHEMA, "label": label, "run_id": args.run_id,
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
    if rooftop:
        receipt.update(profile=profile, record="sf-sp21a-rooftop-replay", pins=rooftop_pins(),
                       endpoints={item.endpoint: item.url for item in listeners}, cleanup_complete=False,
                       solve_fixtures=json.loads(ROOFTOP_SOLVE_MANIFEST.read_bytes())["fixtures"])
        receipt["limitations"] = [label, "fixed fixture-derived identities, not live solving",
                                  "source/build-context packaging evidence only; no image inspection",
                                  "zero events do not prove a walk; browser proof belongs to SP-21C"]
    main_failed = False

    def publish():
        with listener.lock:
            receipt["events"] = [dict(event, endpoint=item.url) if rooftop else dict(event)
                                 for item in listeners for event in item.events]
            receipt["ok"] = not (any(item.failed for item in listeners) or receipt["ttl_expired"] or main_failed)
            atomic_json(receipt_path, receipt)

    listener.on_event = publish
    if solver_listener is not None:
        solver_listener.on_event = publish
    finished = threading.Event()

    def watchdog():
        deadline = time.monotonic() + args.max_seconds
        stop_signalled = False
        while not finished.wait(0.25):
            if stop_path.exists() and not stop_signalled:
                stop_signalled = True
                _thread.interrupt_main()
            if time.monotonic() >= deadline:
                with listener.lock:
                    receipt["ttl_expired"] = True
                    publish()
                _thread.interrupt_main()
                return

    launcher = None
    previous_argv = sys.argv
    code = 1
    # The TTL watchdog stops the launcher through _thread.interrupt_main(), which does
    # nothing while SIGINT is ignored or default. A CI job or a background shell inherits
    # SIGINT as ignored, so the supervisor owns the disposition for the run and restores it.
    sigint_owned = threading.current_thread() is threading.main_thread()
    previous_sigint = signal.getsignal(signal.SIGINT) if sigint_owned else None
    if sigint_owned:
        signal.signal(signal.SIGINT, signal.default_int_handler)
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
                if solver_listener is not None:
                    cmd += ["--solver-url", solver_listener.url]
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
            for item in listeners:
                item.close()
            if rooftop:
                receipt["cleanup_complete"] = True
            main_failed = code != 0
            publish()
            sys.argv = previous_argv
            if watcher.ident is not None:
                watcher.join()
            if sigint_owned and previous_sigint is not None:
                signal.signal(signal.SIGINT, previous_sigint)
    return 0 if code == 0 and receipt["ok"] else 1


def main():
    parser = argparse.ArgumentParser(description=LABEL)
    commands = parser.add_subparsers(dest="command", required=True)
    bootstrap = commands.add_parser("bootstrap")
    bootstrap.add_argument("--role", choices=("broker", "app"), required=True)
    bootstrap.add_argument("--sizing-url", required=True)
    bootstrap.add_argument("--solver-url")
    bootstrap.add_argument("--server-dir", type=Path)
    supervisor = commands.add_parser("supervise")
    supervisor.add_argument("--run-root", type=Path, required=True)
    supervisor.add_argument("--run-id", required=True)
    supervisor.add_argument("--manifest", type=Path)
    supervisor.add_argument("--profile", choices=("string-sizing", "rooftop"), default="string-sizing")
    supervisor.add_argument("--max-seconds", type=int, default=1800)
    supervisor.add_argument("--launcher", type=Path)
    supervisor.add_argument("--server-dir", type=Path)
    supervisor.add_argument("launcher_args", nargs=argparse.REMAINDER)
    verify = commands.add_parser("verify-receipt")
    verify.add_argument("--receipt", type=Path, required=True)
    verify.add_argument("--run-id", required=True)
    verify.add_argument("--profile", choices=("string-sizing", "rooftop"), default="string-sizing")
    args = parser.parse_args()
    try:
        if args.command == "bootstrap":
            entry = prepare_child(args.role, args.sizing_url, args.server_dir, solver_url=args.solver_url)
            sys.argv = [str(entry)]
            runpy.run_path(str(entry), run_name="__main__")
            return 0
        if args.command == "verify-receipt":
            return verify_receipt(args.receipt, args.run_id, profile=args.profile)
        if not 1 <= args.max_seconds <= 7200:
            parser.error("--max-seconds must be in 1..7200")
        return supervise(args)
    except ReplayConfigError as exc:
        print("Synthetic sizing configuration: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
