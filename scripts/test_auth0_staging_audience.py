"""Offline staging audience plans, apply boundaries, and CLI transport checks."""

from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


SPEC = importlib.util.spec_from_file_location(
    "auth0_staging_audience", Path(__file__).with_name("auth0_staging_audience.py"))
audience = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audience)
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=timezone.utc)


class FakeTransport:
    def __init__(self, *, present=False):
        self.declared = audience.load_declared()
        self.server = deepcopy(self.declared["resource_server"]) if present else None
        self.grants = {}
        self.clients = {cid: {"client_id": cid, "client_metadata": {
            "leaf_tenant_audience": self.declared["production_identifier"],
            "leaf_tenant_id": "tenant-" + cid, "unrelated": {"keep": True}}}
            for cid in self.declared["tenant_audience_clients"]}
        if present:
            for grant in self.declared["grants"]:
                self.grants[grant["client_id"]] = {
                    "client_id": grant["client_id"], "audience": self.server["identifier"],
                    "scope": deepcopy(grant["scope"])}
        self.calls = []

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != "GET"]

    def request(self, method, path, *, body=None):
        self.calls.append((method, path, deepcopy(body)))
        if method == "GET":
            if path == audience.resource_path(self.declared):
                return [deepcopy(self.server)] if self.server is not None else []
            for grant in self.declared["grants"]:
                cid = grant["client_id"]
                if path == audience.grant_path(self.declared, cid):
                    return [deepcopy(self.grants[cid])] if cid in self.grants else []
                if path == audience.client_path(cid):
                    return deepcopy(self.clients[cid])
        if (method, path) == ("POST", "resource-servers"):
            self.server = deepcopy(body)
        elif (method, path) == ("POST", "client-grants"):
            self.grants[body["client_id"]] = deepcopy(body)
        elif method == "PATCH" and path.startswith("clients/"):
            self.clients[path.split("/")[1]]["client_metadata"] = deepcopy(body["client_metadata"])
        else:
            raise AssertionError("Unexpected fake operation")
        return {}


class FakeRunner:
    def __init__(self):
        self.calls = []
        self.response = SimpleNamespace(returncode=0, stdout=b"{}", stderr=b"private diagnostic")

    def __call__(self, argv, **kwargs):
        self.calls.append((argv, kwargs))
        return self.response


@pytest.fixture
def run(tmp_path, capsys):
    def invoke(fake, phase="provision", *, confirm=None, receipt_dir=None, extra=None):
        directory = receipt_dir if receipt_dir is not None else tmp_path / "receipts"
        argv = ["--phase", phase, "--receipt-dir", str(directory)]
        if confirm is not None:
            argv += ["--apply", "--confirm", confirm]
        argv += extra or []
        code = audience.main(argv, transport=fake, now=NOW)
        captured = capsys.readouterr()
        report = json.loads(captured.out)
        if code != 3:
            path = directory / ("auth0-staging-audience-" + phase + "-20261029T120000Z.json")
            assert json.loads(path.read_text(encoding="utf-8")) == report
        assert all(set(op) == {"op", "client_id"} for op in report["operations"])
        assert "private diagnostic" not in captured.err
        return code, report, captured.err
    return invoke


def test_repo_declared_file_is_valid():
    declared = audience.load_declared()
    assert declared["schema"] == "leaf.auth0-staging-audience.v1"
    assert declared["production_identifier"] == "https://api.leafdesign.ai"
    assert declared["resource_server"]["identifier"] == "https://api-staging.leafdesign.ai"
    assert [grant["client_id"] for grant in declared["grants"]] == [
        "pIijKCw7Q9KuEWqUNMkGfE119VHRr2kI",
        "Noa3pkZarByn3dLrKYEK3JCVzyCPnIgf",
    ]
    assert len(declared["tenant_audience_clients"]) == 2
    ids = {grant["client_id"] for grant in declared["grants"]} | set(declared["tenant_audience_clients"])
    assert "80lytsxkpPHsaxulj90Lrz0Nk3D5hZb2" not in ids
    assert not ids & {"DPw2LbX95wTOVHoXs7W0BPuTL3xD90Mt", "Uqvr8iVtpyvhdfV0ACVavUMKaThrQZKV"}
    for key in ("schema", "production_identifier", "tenant_audience_clients"):
        invalid = deepcopy(declared)
        invalid[key] = None
        with pytest.raises(audience.SyncError, match="^declared_invalid$"):
            audience.validate_declared(invalid)


def test_provision_plan_on_empty_tenant(run):
    fake = FakeTransport()
    code, report, _ = run(fake)
    assert code == 1 and report["result"] == "drift"
    assert [op["op"] for op in report["operations"]] == ["create_resource_server"] + ["create_client_grant"] * 2
    assert len(report["plan_digest"]) == 64
    assert not fake.writes


def test_provision_match_when_present(run):
    fake = FakeTransport(present=True)
    code, report, _ = run(fake)
    assert code == 0 and report["result"] == "match"
    assert report["operations"] == [] and report["plan_digest"] is None
    assert not fake.writes


def test_provision_resource_server_conflict_refuses(run):
    fake = FakeTransport(present=True)
    fake.server["token_lifetime"] = 900
    code, report, stderr = run(fake)
    assert code == 4 and report["error"] == "resource_server_conflict"
    assert "LEAF_AUTH0_STAGING_AUDIENCE_ERROR=resource_server_conflict" in stderr
    assert not fake.writes


def test_provision_apply_requires_matching_confirm(run):
    fake = FakeTransport()
    code, report, _ = run(fake, confirm="0" * 64)
    assert code == 4 and report["error"] == "confirm_mismatch"
    assert not fake.writes
    code, report, stderr = run(fake, extra=["--apply", "--confirm", "sensitive-input"])
    assert code == 2 and report["error"] == "arguments_invalid"
    assert "sensitive-input" not in stderr
    assert not fake.writes


def test_provision_apply_then_readback_applied(run):
    fake = FakeTransport()
    _, planned, _ = run(fake)
    code, report, _ = run(fake, confirm=planned["plan_digest"])
    assert code == 0 and report["result"] == "applied"
    assert report["plan_digest"] == planned["plan_digest"]
    assert [call[1] for call in fake.writes] == ["resource-servers"] + ["client-grants"] * len(fake.declared["grants"])
    assert [call[2]["client_id"] for call in fake.writes[1:]] == [g["client_id"] for g in fake.declared["grants"]]


def test_cutover_refuses_without_staging_api(run):
    fake = FakeTransport()
    code, report, _ = run(fake, "cutover")
    assert code == 4 and report["error"] == "staging_api_missing"
    fake = FakeTransport(present=True)
    fake.grants.pop(next(iter(fake.grants)))
    code, report, _ = run(fake, "cutover")
    assert code == 4 and report["error"] == "staging_api_missing"
    assert not fake.writes


def test_cutover_plan_sets_staging_audience(run):
    fake = FakeTransport(present=True)
    code, report, _ = run(fake, "cutover")
    assert code == 1
    assert report["operations"] == [{"op": "set_tenant_audience", "client_id": cid}
                                     for cid in fake.declared["tenant_audience_clients"]]
    assert not fake.writes


def test_cutover_unexpected_metadata_refuses(run):
    fake = FakeTransport(present=True)
    next(iter(fake.clients.values()))["client_metadata"]["leaf_tenant_audience"] = "https://other.example"
    code, report, _ = run(fake, "cutover")
    assert code == 4 and report["error"] == "metadata_unexpected"
    assert not fake.writes


def test_cutover_patch_keeps_other_metadata_keys(run):
    fake = FakeTransport(present=True)
    before = deepcopy(fake.clients)
    _, plan, _ = run(fake, "cutover")
    code, report, _ = run(fake, "cutover", confirm=plan["plan_digest"])
    assert code == 0 and report["result"] == "applied"
    for method, path, body in fake.writes:
        assert method == "PATCH" and set(body) == {"client_metadata"}
        expected = before[path.split("/")[1]]["client_metadata"]
        expected["leaf_tenant_audience"] = fake.declared["resource_server"]["identifier"]
        assert body["client_metadata"] == expected
    # A provider that drops another key must fail even when the audience matches.
    fake = FakeTransport(present=True)
    original = fake.request
    def dropping_request(method, path, *, body=None):
        result = original(method, path, body=body)
        if method == "PATCH":
            fake.clients[path.split("/")[1]]["client_metadata"].pop("unrelated")
        return result
    fake.request = dropping_request
    _, plan, _ = run(fake, "cutover")
    code, report, _ = run(fake, "cutover", confirm=plan["plan_digest"])
    assert code == 1 and report["error"] == "readback_mismatch"


def test_rollback_targets_production_and_never_deletes(run):
    fake = FakeTransport()
    for client in fake.clients.values():
        client["client_metadata"]["leaf_tenant_audience"] = fake.declared["resource_server"]["identifier"]
    _, plan, _ = run(fake, "rollback")
    code, report, _ = run(fake, "rollback", confirm=plan["plan_digest"])
    assert code == 0 and report["result"] == "applied"
    assert len(fake.writes) == 2
    assert all(call[0] == "PATCH" for call in fake.writes)
    assert all(call[2]["client_metadata"]["leaf_tenant_audience"] == fake.declared["production_identifier"]
               for call in fake.writes)


def test_transport_refuses_unlisted_operations():
    declared = audience.load_declared()
    grant_only_client_id = "X" * 32
    declared["grants"].append({"client_id": grant_only_client_id, "scope": []})
    runner = FakeRunner()
    transport = audience.CliTransport(declared, phase="cutover", allow_writes=True, runner=runner)
    for method, path, body in [
        ("PATCH", "clients/" + grant_only_client_id,
         {"client_metadata": {"leaf_tenant_audience": declared["resource_server"]["identifier"]}}),
        ("DELETE", "resource-servers", None),
        ("GET", audience.client_path("Y" * 32), None),
    ]:
        with pytest.raises(audience.SyncError, match="^operation_not_allowed$"):
            transport.request(method, path, body=body)
    assert runner.calls == []


def test_cli_transport_argv_shape():
    declared = audience.load_declared()
    runner = FakeRunner()
    transport = audience.CliTransport(declared, phase="provision", allow_writes=True, runner=runner)
    transport.request("GET", audience.resource_path(declared))
    transport.request("POST", "resource-servers", body=declared["resource_server"])
    argv, options = runner.calls[1]
    assert isinstance(argv, list) and argv[:4] == ["auth0", "api", "post", "resource-servers"]
    assert argv[4:] == ["--data", json.dumps(declared["resource_server"], separators=(",", ":"))]
    assert options == {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "timeout": 60, "shell": False}
    assert not any("token" in arg for arg in argv[:5])
    for response in (SimpleNamespace(returncode=1, stdout=b"{}"),
                     SimpleNamespace(returncode=0, stdout=b"not JSON"),
                     SimpleNamespace(returncode=0, stdout=b" " * (audience.MAX_RESPONSE_BYTES + 1))):
        runner.response = response
        with pytest.raises(audience.SyncError, match="^request_failed$"):
            transport.request("GET", audience.resource_path(declared))
    def timeout_runner(*args, **kwargs):
        raise subprocess.TimeoutExpired("private", 60)
    transport.runner = timeout_runner
    with pytest.raises(audience.SyncError, match="^request_failed$"):
        transport.request("GET", audience.resource_path(declared))


def test_receipt_write_failure_exits_three(run, tmp_path):
    blocked = tmp_path / "file"
    blocked.write_text("not a directory", encoding="utf-8")
    code, report, stderr = run(FakeTransport(present=True), receipt_dir=blocked)
    assert code == 3 and report["error"] == "receipt_write_failed"
    assert "LEAF_AUTH0_STAGING_AUDIENCE_ERROR=receipt_write_failed" in stderr
