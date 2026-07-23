from __future__ import annotations

import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).with_name("probe-public-hosts.py")
SPEC = importlib.util.spec_from_file_location("probe_public_hosts", SCRIPT)
probe = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = probe
SPEC.loader.exec_module(probe)


def _contract(location="https://fixture.test/", status=308):
    return {
        "schema": "leaf.public-host-contract.v1",
        "max_hops_to_https": 1,
        "timeout_seconds": 0.5,
        "max_body_bytes": 128,
        "hosts": [{
            "name": "fixture",
            "host": "fixture.test",
            "http_url": "http://fixture.test/",
            "status": status,
            "location": location,
        }],
    }


@pytest.fixture
def redirect_server():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.observed_headers = dict(self.headers)
            self.send_response(308)
            self.send_header("Location", "https://fixture.test/")
            self.end_headers()
            self.wfile.write(b"redirect")

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join(timeout=2)
    server.server_close()


def test_hermetic_local_fixture_passes_exact_no_follow_contract(redirect_server):
    local = f"http://127.0.0.1:{redirect_server.server_port}/"

    def requester(_url, timeout, body_limit):
        return probe.fetch_once(local, timeout, body_limit)

    report = probe.probe_contract(_contract(), requester)
    assert report["ok"] is True
    assert report["observations"] == [{
        "host": "fixture", "status": 308,
        "location": "https://fixture.test/",
    }]
    observed = redirect_server.observed_headers
    assert "Authorization" not in observed
    assert "Cookie" not in observed


def test_http_200_is_a_precise_failure():
    report = probe.probe_contract(
        _contract(),
        lambda *_args: probe.ProbeResponse(200, (), 2))
    checks = {failure["check"] for failure in report["failures"]}
    assert {"http_not_200", "status", "location_count"} <= checks


@pytest.mark.parametrize(
    "location,check",
    [
        ("http://fixture.test/", "https_in_one_hop"),
        ("https://elsewhere.test/", "same_host"),
        ("http://fixture.test/", "no_loop"),
    ],
)
def test_insecure_off_host_and_loop_redirects_fail(location, check):
    report = probe.probe_contract(
        _contract(),
        lambda *_args: probe.ProbeResponse(308, (location,), 0))
    assert check in {failure["check"] for failure in report["failures"]}


@pytest.mark.parametrize(
    "location",
    [
        "https://fixture.test/?unexpected=true",
        "https://fixture.test/#unexpected",
        "https://user:password@fixture.test/",
    ],
)
def test_location_must_match_exactly_without_hidden_components(location):
    report = probe.probe_contract(
        _contract(),
        lambda *_args: probe.ProbeResponse(308, (location,), 0))
    assert "location" in {failure["check"] for failure in report["failures"]}
    assert "password" not in json.dumps(report)


def test_body_and_request_failures_are_bounded_and_sanitized():
    too_large = probe.probe_contract(
        _contract(),
        lambda *_args: probe.ProbeResponse(308, ("https://fixture.test/",), 129))
    assert too_large["failures"] == [{
        "host": "fixture", "check": "body_limit", "expected": 128, "actual": 129,
    }]

    secret = "credential-that-must-not-appear"

    def unavailable(*_args):
        raise RuntimeError(secret)

    failed = probe.probe_contract(_contract(), unavailable)
    assert failed["failures"][0]["actual"] == "RuntimeError"
    assert secret not in json.dumps(failed)


def test_default_mode_is_offline_read_only_and_live_violation_is_nonzero(
        tmp_path, monkeypatch, capsys):
    path = tmp_path / "contract.json"
    path.write_text(json.dumps(_contract()), encoding="utf-8")
    monkeypatch.setattr(
        probe, "fetch_once",
        lambda *_args: (_ for _ in ()).throw(AssertionError("network used")))
    assert probe.main(["--contract", str(path)]) == 0
    default_report = json.loads(capsys.readouterr().out)
    assert default_report["network_requests"] == 0
    assert default_report["mutations"] == 0

    monkeypatch.setattr(
        probe, "fetch_once",
        lambda *_args: probe.ProbeResponse(200, (), 0))
    assert probe.main(["--contract", str(path), "--live"]) == 1
    live_report = json.loads(capsys.readouterr().out)
    assert live_report["ok"] is False


def test_frozen_contract_has_exact_apex_www_platform_locations():
    contract = probe.load_contract()
    assert {
        item["name"]: (item["status"], item["location"])
        for item in contract["hosts"]
    } == {
        "apex": (308, "https://leafautomation.ai/"),
        "www": (308, "https://www.leafautomation.ai/"),
        "platform": (308, "https://platform.leafautomation.ai/"),
    }
