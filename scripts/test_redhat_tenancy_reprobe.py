from __future__ import annotations

import importlib.util
import json
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


SCRIPT = Path(__file__).with_name("redhat_tenancy_reprobe.py")
SPEC = importlib.util.spec_from_file_location("redhat_tenancy_reprobe", SCRIPT)
reprobe = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = reprobe
SPEC.loader.exec_module(reprobe)

MARKER = "PRIVATE_RESPONSE_BODY_MUST_NOT_APPEAR"
VALID_SHA = "0123456789abcdef0123456789abcdef01234567"
EXPECTED_CHECKS = [
    ("GET", "/api/jobs"),
    ("GET", "/api/jobs?tenant_id=leaf-redhat-reprobe-foreign"),
    ("GET", "/api/jobs/00000000-0000-4000-8000-000000000000"),
    ("GET", "/api/jobs/00000000-0000-4000-8000-000000000000/stream"),
    ("GET", "/api/projects"),
    ("GET", "/api/ops/tenants"),
    ("OPTIONS", "/api/health"),
    ("GET", "/api/health"),
]
# The reachability check goes first, then every finding's checks.
EXPECTED_REQUESTS = [("GET", "/api/health"), *EXPECTED_CHECKS]
EDGE_BODY = b"error code: 1010"


@pytest.fixture
def stub():
    state = {
        "F1": 401, "F6": 403, "F7": 404, "F17": None,
        "health_status": 200, "preflight_status": 400,
        "source_sha": VALID_SHA, "delay": 0, "seen": [],
        "location": "/redirect-target", "status_by_path": {}, "health_body": None,
        "acao_by_method": {}, "malformed_health_chunks": False, "malformed_preflight_chunks": False,
        "agents": [], "edge_agent": None, "body_by_path": {}, "headers_by_path": {},
    }

    class Handler(BaseHTTPRequestHandler):
        def respond(self):
            state["seen"].append((self.command, self.path, dict(self.headers)))
            state["agents"].append(self.headers.get_all("User-Agent") or [])
            body = MARKER.encode()
            acao = None
            if self.path.startswith("/api/jobs"):
                status = state["F1"]
                if self.path == "/api/jobs" and state["delay"]:
                    time.sleep(state["delay"])
            elif self.path == "/api/projects":
                status = state["F6"]
            elif self.path == "/api/ops/tenants":
                status = state["F7"]
            elif self.path == "/api/health":
                status = state["preflight_status"] if self.command == "OPTIONS" else state["health_status"]
                acao = state["acao_by_method"].get(self.command, state["F17"])
                if acao == "reflect":
                    acao = self.headers.get("Origin", "").upper()
                body = json.dumps({"source_sha": state["source_sha"], "private": MARKER}).encode()
                if state["health_body"] is not None:
                    body = state["health_body"]
            else:
                status = 200
            status = state["status_by_path"].get(self.path, status)
            body = state["body_by_path"].get(self.path, body)
            extra_headers = state["headers_by_path"].get(self.path, {})
            if state["edge_agent"] is not None and self.headers.get("User-Agent") != state["edge_agent"]:
                # Cloudflare's browser integrity check answers before the app sees the request.
                status, body, acao, extra_headers = 403, EDGE_BODY, None, {}
            self.send_response(status)
            if status == 302:
                self.send_header("Location", state["location"])
            if acao is not None:
                self.send_header("Access-Control-Allow-Origin", acao)
            for name, value in extra_headers.items():
                self.send_header(name, value)
            self.send_header("X-Private-Header", MARKER)
            self.send_header("Set-Cookie", "private_cookie=" + MARKER)
            self.send_header("Content-Type", "application/json")
            malformed_chunks = self.path == "/api/health" and (
                (self.command == "GET" and state["malformed_health_chunks"])
                or (self.command == "OPTIONS" and state["malformed_preflight_chunks"])
            )
            if malformed_chunks:
                self.send_header("Transfer-Encoding", "chunked")
                body = b"NOT_HEX\r\ninvalid\r\n0\r\n\r\n"
            else:
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                # Expected when the probe closes without consuming a body.
                pass

        do_GET = respond
        do_OPTIONS = respond
        do_POST = respond
        do_PUT = respond
        do_PATCH = respond
        do_DELETE = respond

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["origin"] = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def _probe(stub):
    return reprobe.probe(stub["origin"], timeout=0.5)


def _cli(stub, capsys, *extra):
    code = reprobe.main(["--origin", stub["origin"], "--timeout", "0.5", *extra])
    output = capsys.readouterr()
    assert output.err == ""
    receipt = json.loads(output.out)
    assert output.out == json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    return code, receipt


def test_all_findings_pass_against_secure_stub(stub, capsys, tmp_path):
    out = tmp_path / "receipt.json"
    code, receipt = _cli(stub, capsys, "--out", str(out))
    assert code == 0
    assert receipt["overall"] == "PASS"
    assert receipt["schema"] == "leaf.redhat-reprobe.v1"
    assert receipt["origin"] == stub["origin"]
    assert receipt["probed_at"].endswith("Z")
    assert datetime.fromisoformat(receipt["probed_at"].replace("Z", "+00:00")).utcoffset().total_seconds() == 0
    assert set(receipt["findings"]) == {"F1", "F6", "F7", "F17"}
    for finding in receipt["findings"].values():
        assert finding["verdict"] == "PASS"
        assert all(check["verdict"] == "PASS" for check in finding["checks"])
    assert json.loads(out.read_text(encoding="utf-8")) == receipt


def test_f1_fails_when_jobs_are_readable_without_credentials(stub, capsys):
    stub["F1"] = 200
    code, receipt = _cli(stub, capsys)
    assert code == 1
    assert receipt["overall"] == "FAIL"
    assert receipt["findings"]["F1"]["verdict"] == "FAIL"
    assert all(check["verdict"] == "FAIL" for check in receipt["findings"]["F1"]["checks"])


def test_f6_fails_when_org_header_is_trusted(stub):
    stub["F6"] = 200
    receipt = _probe(stub)
    assert receipt["findings"]["F6"]["verdict"] == receipt["overall"] == "FAIL"
    headers = next(headers for _, path, headers in stub["seen"] if path == "/api/projects")
    assert {key.lower(): value for key, value in headers.items()}["x-org-id"] == "00000000-0000-4000-8000-000000000001"


def test_f7_fails_when_qa_header_opens_ops(stub):
    stub["F7"] = 200
    receipt = _probe(stub)
    assert receipt["findings"]["F7"]["verdict"] == receipt["overall"] == "FAIL"
    headers = next(headers for _, path, headers in stub["seen"] if path == "/api/ops/tenants")
    assert {key.lower(): value for key, value in headers.items()}["x-internal-role"] == "qa"


def test_f17_fails_on_wildcard_acao(stub):
    stub["F17"] = "*"
    stub["health_status"] = 500
    receipt = _probe(stub)
    assert receipt["findings"]["F17"]["verdict"] == receipt["overall"] == "FAIL"
    assert all(check["verdict"] == "FAIL" for check in receipt["findings"]["F17"]["checks"])
    assert all(check["acao"] == "*" for check in receipt["findings"]["F17"]["checks"])


def test_f17_fails_when_foreign_origin_is_reflected(stub):
    stub["F17"] = "reflect"
    for foreign in (reprobe.FOREIGN_ORIGIN, "https://other-attacker.invalid"):
        receipt = reprobe.probe(stub["origin"], timeout=0.5, foreign_origin=foreign)
        assert receipt["findings"]["F17"]["verdict"] == receipt["overall"] == "FAIL"
        assert all(check["verdict"] == "FAIL" for check in receipt["findings"]["F17"]["checks"])


def test_malformed_health_body_after_wildcard_acao_keeps_the_cors_failure(stub, capsys):
    stub["acao_by_method"] = {"OPTIONS": None, "GET": "*"}
    stub["preflight_status"] = 400
    stub["health_status"] = 200
    stub["malformed_health_chunks"] = True
    code, receipt = _cli(stub, capsys)
    assert code == 1
    assert receipt["findings"]["F17"]["verdict"] == receipt["overall"] == "FAIL"
    preflight, health = receipt["findings"]["F17"]["checks"]
    assert preflight == {
        "method": "OPTIONS", "path": "/api/health", "status": 400,
        "verdict": "PASS", "acao": None,
    }
    assert health == {
        "method": "GET", "path": "/api/health", "status": 200,
        "verdict": "FAIL", "acao": "*",
    }
    assert receipt["source_sha"] is None


def test_health_body_of_exactly_64_kib_keeps_its_source_sha(stub, capsys):
    body = json.dumps({"source_sha": VALID_SHA}).encode()
    # An overflowing health body is not a readable JSON object, so the reachability precondition fails.
    for size, expected_sha, expected_code, expected_overall in (
        (65536, VALID_SHA, 0, "PASS"), (65537, None, 3, "UNREADABLE"),
    ):
        stub["health_body"] = body.ljust(size, b" ")
        assert len(stub["health_body"]) == size
        code, receipt = _cli(stub, capsys)
        assert code == expected_code
        assert receipt["overall"] == expected_overall
        assert receipt["source_sha"] == expected_sha


def test_wildcard_acao_on_successful_responses_fails_the_probe(stub, capsys):
    stub["F17"] = "*"
    stub["preflight_status"] = stub["health_status"] = 200
    code, receipt = _cli(stub, capsys)
    assert code == 1
    assert receipt["findings"]["F17"]["verdict"] == receipt["overall"] == "FAIL"
    for check in receipt["findings"]["F17"]["checks"]:
        assert check["status"] == 200
        assert check["acao"] == "*"
        assert check["verdict"] == "FAIL"


def test_f17_passes_when_a_different_origin_is_allowed(stub):
    stub["F17"] = "https://trusted.invalid"
    stub["health_status"] = 503
    receipt = _probe(stub)
    # A 503 health answer fails the reachability precondition, so no finding can pass.
    assert receipt["findings"]["F17"]["verdict"] == receipt["overall"] == "UNREADABLE"
    assert all(check["acao"] == stub["F17"] for check in receipt["findings"]["F17"]["checks"])


def test_server_error_and_timeout_are_unreadable_not_pass(stub, capsys):
    stub["F1"] = 500
    code, receipt = _cli(stub, capsys)
    assert code == 3
    assert receipt["findings"]["F1"]["verdict"] == receipt["overall"] == "UNREADABLE"
    assert all(check["status"] == 500 for check in receipt["findings"]["F1"]["checks"])
    stub["F1"] = 401
    stub["delay"] = 0.8
    code, receipt = _cli(stub, capsys)
    assert code == 3
    assert receipt["findings"]["F1"]["verdict"] == receipt["overall"] == "UNREADABLE"
    assert receipt["findings"]["F1"]["checks"][0]["status"] is None
    assert receipt["findings"]["F1"]["checks"][0]["verdict"] == "UNREADABLE"
    stub["delay"] = 0
    stub["F1"] = 500
    stub["F6"] = 200
    assert _probe(stub)["overall"] == "FAIL"


def test_redirect_is_not_followed(stub):
    stub["F1"] = 302
    receipt = _probe(stub)
    assert receipt["findings"]["F1"]["verdict"] == receipt["overall"] == "UNREADABLE"
    assert all(check["status"] == 302 for check in receipt["findings"]["F1"]["checks"])
    assert not any(path == "/redirect-target" for _, path, _ in stub["seen"])
    assert [(method, path) for method, path, _ in stub["seen"]] == EXPECTED_REQUESTS


def test_malformed_redirect_location_is_unreadable_and_the_probe_completes(stub, capsys, tmp_path):
    stub["status_by_path"]["/api/jobs"] = 302
    stub["location"] = "http://["
    out = tmp_path / "malformed-redirect.json"
    code, receipt = _cli(stub, capsys, "--out", str(out))
    assert code == 3
    assert receipt["overall"] == "UNREADABLE"
    assert receipt["findings"]["F1"]["checks"][0] == {
        "method": "GET", "path": "/api/jobs", "status": 302, "verdict": "UNREADABLE",
    }
    checks = [check for finding in receipt["findings"].values() for check in finding["checks"]]
    assert all(check["verdict"] == "PASS" for check in checks[1:])
    assert [(method, path) for method, path, _ in stub["seen"]] == EXPECTED_REQUESTS
    assert json.loads(out.read_text(encoding="utf-8")) == receipt


def test_deeply_nested_health_body_yields_a_null_source_sha(stub, capsys, tmp_path):
    stub["health_body"] = b"[" * 10000 + b"0" + b"]" * 10000
    out = tmp_path / "nested-health.json"
    code, receipt = _cli(stub, capsys, "--out", str(out))
    # An unparseable health body fails the reachability precondition.
    assert code == 3
    assert receipt["overall"] == "UNREADABLE"
    assert receipt["reachability"]["verdict"] == "UNREADABLE"
    assert receipt["source_sha"] is None
    assert [(method, path) for method, path, _ in stub["seen"]] == EXPECTED_REQUESTS
    assert json.loads(out.read_text(encoding="utf-8")) == receipt


def test_health_read_is_bounded_and_protected_bodies_are_never_read(stub, monkeypatch):
    reads = []
    original_open = reprobe.urllib.request.OpenerDirector.open

    def recording_open(opener, request, *args, **kwargs):
        error = None
        try:
            response = original_open(opener, request, *args, **kwargs)
        except reprobe.urllib.error.HTTPError as exc:
            response = error = exc
        original_read = response.read

        def recording_read(*read_args, **read_kwargs):
            record = {
                "method": request.get_method(), "url": request.full_url, "status": response.code,
                "args": read_args, "kwargs": read_kwargs, "bytes": 0,
            }
            reads.append(record)
            body = original_read(*read_args, **read_kwargs)
            record["bytes"] = len(body)
            return body

        response.read = recording_read
        if error is not None:
            raise error
        return response

    monkeypatch.setattr(reprobe.urllib.request.OpenerDirector, "open", recording_open)
    for oversized in (False, True):
        reads.clear()
        stub["seen"].clear()
        if oversized:
            stub.update(F1=200, F6=200, F7=200)
            stub["health_body"] = json.dumps({"source_sha": VALID_SHA}).encode().ljust(
                reprobe.MAX_BODY_BYTES + 100, b" "
            )
        receipt = _probe(stub)
        assert receipt["source_sha"] == (None if oversized else VALID_SHA)
        assert [(method, path) for method, path, _ in stub["seen"]] == EXPECTED_REQUESTS
        assert reads
        health_reads = []
        for record in reads:
            assert len(record["args"]) == 1 and not record["kwargs"]
            assert isinstance(record["args"][0], int)
            if record["url"] == stub["origin"] + "/api/health":
                assert record["method"] == "GET"
                assert 0 < record["args"][0] <= reprobe.MAX_BODY_BYTES + 1
                health_reads.append(record)
            else:
                # Only a bounded denial body is ever read, never a 2xx protected body.
                assert record["status"] in reprobe.EDGE_STATUSES
                assert 0 < record["args"][0] <= reprobe.EDGE_BODY_BYTES
        assert sum(record["bytes"] for record in health_reads) <= reprobe.MAX_BODY_BYTES + 1
        assert sum(record["bytes"] for record in health_reads) > 0


def test_probe_never_sends_a_mutating_method(stub):
    _probe(stub)
    assert [(method, path) for method, path, _ in stub["seen"]] == EXPECTED_REQUESTS
    assert {method for method, _, _ in stub["seen"]} == {"GET", "OPTIONS"}
    # The reachability check carries no Origin; every F17 check carries the foreign one.
    assert "origin" not in {key.lower() for key in stub["seen"][0][2]}
    for method, path, headers in stub["seen"][1:]:
        headers = {key.lower(): value for key, value in headers.items()}
        assert "authorization" not in headers
        assert "proxy-authorization" not in headers
        assert "cookie" not in headers
        if path == "/api/health":
            assert headers["origin"] == reprobe.FOREIGN_ORIGIN
            if method == "OPTIONS":
                assert headers["access-control-request-method"] == "GET"


def test_receipt_carries_no_bodies_and_a_validated_source_sha(stub):
    receipt = _probe(stub)
    assert receipt["source_sha"] == VALID_SHA
    assert set(receipt) == {
        "schema", "origin", "probed_at", "source_sha", "reachability", "findings", "overall",
    }
    assert receipt["reachability"] == {
        "method": "GET", "path": "/api/health", "status": 200, "verdict": "PASS",
    }
    assert MARKER not in json.dumps(receipt)
    for name, finding in receipt["findings"].items():
        assert set(finding) == {"verdict", "checks"}
        for check in finding["checks"]:
            assert set(check) == (
                {"method", "path", "status", "verdict"}
                | ({"acao"} if name == "F17" else set())
                | ({"edge_blocked"} if check["status"] in reprobe.EDGE_STATUSES else set())
            )
    for invalid in ("unknown", "ABCDEF0", "abcdef", "f" * 41, "abcdef0\n", None, 1234567):
        stub["source_sha"] = invalid
        assert _probe(stub)["source_sha"] is None
    stub["source_sha"] = VALID_SHA
    stub["health_status"] = 201
    assert _probe(stub)["source_sha"] is None


def test_origin_argument_is_validated(stub, capsys):
    origins = (
        stub["origin"].replace("http://", "http://user:password@"),
        stub["origin"] + "?query=1", stub["origin"] + "?",
        stub["origin"] + "/path", stub["origin"] + "#fragment",
        "http://example.invalid", "ftp://127.0.0.1", "https://",
    )
    for origin in origins:
        assert reprobe.main(["--origin", origin]) == 5
        output = capsys.readouterr()
        assert output.out == ""
        assert len(output.err.splitlines()) == 1
    for args in ([], ["--offline", "--origin", stub["origin"]]):
        assert reprobe.main(args) == 5
        assert len(capsys.readouterr().err.splitlines()) == 1
    for timeout in ("0.09", "30.1", "nan", "inf", "not-a-number"):
        assert reprobe.main(["--origin", stub["origin"], "--timeout", timeout]) == 5
        assert len(capsys.readouterr().err.splitlines()) == 1
    assert stub["seen"] == []


def test_offline_self_test_passes(capsys):
    assert reprobe.main(["--offline"]) == 0
    output = capsys.readouterr()
    assert output.err == ""
    receipt = json.loads(output.out)
    assert receipt["overall"] == "PASS"
    assert receipt["schema"] == reprobe.SCHEMA
    assert receipt["origin"].startswith("http://127.0.0.1:")


def test_probe_routes_exist_in_server_source(stub):
    repo = SCRIPT.parent.parent
    required = {
        "server/routers/jobs.py": (
            '@router.get("/api/jobs")',
            '@router.get("/api/jobs/{job_id}")',
            '@router.get("/api/jobs/{job_id}/stream")',
        ),
        "server/routers/ops.py": ('@router.get("/api/ops/tenants")',),
        "platform/api.py": ('@router.get("/projects")', 'APIRouter(prefix="/api"'),
        "server/app.py": ('@app.get("/api/health")',),
    }
    for relative, decorators in required.items():
        source = (repo / relative).read_text(encoding="utf-8")
        for decorator in decorators:
            assert decorator in source, (relative, decorator)
    assert [(check["method"], check["path"]) for finding in _probe(stub)["findings"].values()
            for check in finding["checks"]] == EXPECTED_CHECKS


def _record_reads(monkeypatch):
    reads = []
    original_open = reprobe.urllib.request.OpenerDirector.open

    def recording_open(opener, request, *args, **kwargs):
        error = None
        try:
            response = original_open(opener, request, *args, **kwargs)
        except reprobe.urllib.error.HTTPError as exc:
            response = error = exc
        original_read = response.read

        def recording_read(*read_args, **read_kwargs):
            record = {"url": request.full_url, "bytes": 0}
            reads.append(record)
            body = original_read(*read_args, **read_kwargs)
            record["bytes"] = len(body)
            return body

        response.read = recording_read
        if error is not None:
            raise error
        return response

    monkeypatch.setattr(reprobe.urllib.request.OpenerDirector, "open", recording_open)
    return reads


def test_every_request_carries_the_fixed_user_agent(stub):
    assert reprobe.USER_AGENT == "leaf-redhat-reprobe/1.0"
    _probe(stub)
    assert [(method, path) for method, path, _ in stub["seen"]] == EXPECTED_REQUESTS
    # Reachability plus the eight finding checks, each with exactly one agent header.
    assert len(stub["agents"]) == len(stub["seen"]) == len(EXPECTED_REQUESTS)
    assert stub["agents"] == [["leaf-redhat-reprobe/1.0"]] * len(EXPECTED_REQUESTS)


def test_case_headers_cannot_override_the_user_agent(stub, monkeypatch):
    original_cases = reprobe._cases

    def spoofed_cases(foreign_origin):
        return {
            finding: [
                (method, path, {**headers, "User-Agent": "spoofed/0", "user-agent": "spoofed/1"})
                for method, path, headers in cases
            ]
            for finding, cases in original_cases(foreign_origin).items()
        }

    monkeypatch.setattr(reprobe, "_cases", spoofed_cases)
    receipt = _probe(stub)
    assert receipt["overall"] == "PASS"
    assert stub["agents"] == [["leaf-redhat-reprobe/1.0"]] * len(EXPECTED_REQUESTS)


def test_edge_block_on_every_route_is_unreadable_not_pass(stub, capsys, monkeypatch):
    stub["edge_agent"] = "leaf-redhat-reprobe/1.0"
    monkeypatch.setattr(reprobe, "USER_AGENT", "Python-urllib/3.13")
    code, receipt = _cli(stub, capsys)
    assert code == 3
    assert receipt["overall"] == "UNREADABLE"
    assert receipt["reachability"]["verdict"] == "UNREADABLE"
    assert receipt["reachability"]["status"] == 403
    assert receipt["source_sha"] is None
    assert stub["agents"] == [["Python-urllib/3.13"]] * len(EXPECTED_REQUESTS)
    for name, finding in receipt["findings"].items():
        assert finding["verdict"] == "UNREADABLE"
        for check in finding["checks"]:
            assert check["status"] == 403
            assert check["edge_blocked"] is True
            assert check["verdict"] == "UNREADABLE"
    for name in ("F1", "F6", "F7"):
        assert all(check["edge_blocked"] is True for check in receipt["findings"][name]["checks"])


def test_fixed_agent_passes_the_same_edge(stub, capsys):
    stub["edge_agent"] = "leaf-redhat-reprobe/1.0"
    code, receipt = _cli(stub, capsys)
    assert code == 0
    assert receipt["overall"] == "PASS"
    assert receipt["reachability"] == {
        "method": "GET", "path": "/api/health", "status": 200, "verdict": "PASS",
    }
    assert receipt["source_sha"] == VALID_SHA
    checks = [check for finding in receipt["findings"].values() for check in finding["checks"]]
    assert not any(check.get("edge_blocked") for check in checks)


def test_cf_mitigated_header_marks_edge_block(stub):
    stub["F6"] = 403
    stub["headers_by_path"]["/api/projects"] = {"cf-mitigated": "challenge"}
    stub["body_by_path"]["/api/projects"] = b"<!DOCTYPE html><html><body>Just a moment...</body></html>"
    receipt = _probe(stub)
    check = receipt["findings"]["F6"]["checks"][0]
    assert check["status"] == 403
    assert check["edge_blocked"] is True
    assert check["verdict"] == "UNREADABLE"
    assert receipt["findings"]["F6"]["verdict"] == receipt["overall"] == "UNREADABLE"


def test_app_403_json_body_still_passes(stub):
    stub["F6"] = 403
    stub["body_by_path"]["/api/projects"] = b'{"error":{"code":"FORBIDDEN"}}'
    receipt = _probe(stub)
    check = receipt["findings"]["F6"]["checks"][0]
    assert check["status"] == 403
    assert check["edge_blocked"] is False
    assert check["verdict"] == "PASS"
    assert receipt["findings"]["F6"]["verdict"] == receipt["overall"] == "PASS"


def test_unreachable_health_forces_unreadable_findings(stub, capsys):
    stub.update(F1=403, F6=403, F7=403, health_status=503)
    secure = b'{"error":{"code":"FORBIDDEN"}}'
    for _, path in EXPECTED_CHECKS[:6]:
        stub["body_by_path"][path] = secure
    code, receipt = _cli(stub, capsys)
    assert code == 3
    assert receipt["reachability"] == {
        "method": "GET", "path": "/api/health", "status": 503, "verdict": "UNREADABLE",
    }
    assert receipt["source_sha"] is None
    for name in ("F1", "F6", "F7"):
        assert all(check["verdict"] == "PASS" and check["edge_blocked"] is False
                   for check in receipt["findings"][name]["checks"])
    assert all(finding["verdict"] == "UNREADABLE" for finding in receipt["findings"].values())
    assert receipt["overall"] == "UNREADABLE"


def test_health_non_json_forces_unreadable(stub, capsys):
    stub["health_body"] = b"ok"
    code, receipt = _cli(stub, capsys)
    assert code == 3
    assert receipt["reachability"]["status"] == 200
    assert receipt["reachability"]["verdict"] == "UNREADABLE"
    assert receipt["source_sha"] is None
    assert all(finding["verdict"] == "UNREADABLE" for finding in receipt["findings"].values())
    assert receipt["overall"] == "UNREADABLE"


def test_leak_still_fails_when_health_is_unreachable(stub, capsys):
    stub["health_status"] = 503
    stub["F1"] = 200
    code, receipt = _cli(stub, capsys)
    assert code == 1
    assert receipt["reachability"]["verdict"] == "UNREADABLE"
    assert receipt["findings"]["F1"]["verdict"] == "FAIL"
    assert all(check["verdict"] == "FAIL" for check in receipt["findings"]["F1"]["checks"])
    assert receipt["overall"] == "FAIL"


def test_edge_block_body_read_is_bounded(stub, monkeypatch):
    reads = _record_reads(monkeypatch)
    stub["F6"] = 403
    stub["body_by_path"]["/api/projects"] = b"x" * (4 * 1024 * 1024)
    receipt = _probe(stub)
    project_reads = [record for record in reads if record["url"] == stub["origin"] + "/api/projects"]
    assert project_reads
    assert sum(record["bytes"] for record in project_reads) <= reprobe.EDGE_BODY_BYTES == 512
    check = receipt["findings"]["F6"]["checks"][0]
    assert check["status"] == 403
    assert check["edge_blocked"] is False
    assert check["verdict"] == "PASS"
    assert receipt["overall"] == "PASS"


def test_unreadable_body_keeps_observed_cors_leak_fail(stub, capsys):
    stub.update(F1=401, F6=401, F7=401, preflight_status=403, malformed_preflight_chunks=True)
    stub["acao_by_method"] = {"OPTIONS": "*", "GET": None}
    for _, path in EXPECTED_CHECKS[:6]:
        stub["body_by_path"][path] = b'{"error":{"code":"UNAUTHORIZED"}}'
    code, receipt = _cli(stub, capsys)
    assert receipt["reachability"]["verdict"] == "PASS"
    preflight = receipt["findings"]["F17"]["checks"][0]
    assert preflight["method"] == "OPTIONS" and preflight["status"] == 403
    assert preflight["acao"] == "*"
    assert preflight["edge_blocked"] is not True
    assert preflight["verdict"] == "FAIL"
    assert receipt["findings"]["F17"]["verdict"] == receipt["overall"] == "FAIL"
    assert code == 1
