"""Hermetic production acceptance probe checks; no test opens a socket."""

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import urllib.error
import uuid

import pytest


SPEC = importlib.util.spec_from_file_location(
    "production_tenant_header_probe", Path(__file__).with_name("production_tenant_header_probe.py"))
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)

ORG = "baf176a7-3b1b-4ab2-91dc-c08e87da70db"
TENANT = "8bdfdcd7-a615-4cb3-9939-049213125f71"
TOKEN = "secret-bearer-must-never-be-persisted"
DIGEST = "sha256:" + "a" * 64
COMMIT = "b" * 40
PATHS = ["/api/session", "/api/session", "/api/deployment-identity",
         "/api/capabilities", "/api/tools"]


@pytest.fixture
def harness(monkeypatch, tmp_path):
    monkeypatch.setenv("LEAF_PROBE_BEARER", TOKEN)
    state = {
        "out": tmp_path / "snapshot.json", "calls": [],
        "responses": [
            (200, {"tenant_id": TENANT, "org_id": ORG, "intake": {"private": TOKEN}}),
            (200, {"tenant_id": TENANT, "org_id": ORG.upper()}),
            (200, {"status": "verified", "environment": "production", "private": TOKEN,
                   "services": {"platform": {"image_digest": DIGEST,
                                             "source_revision": COMMIT,
                                             "private": TOKEN}}}),
            (200, {"families": [
                {"family_id": "second", "capabilities": [{"name": "zeta"}]},
                {"family_id": "first", "capabilities": [{"name": "alpha"}]}]}),
            (200, {"tools": [{"name": "tool_z"}, {"name": "tool_a"}]}),
        ],
    }

    def fake_get(base_url, path, headers, timeout_s):
        index = len(state["calls"])
        state["calls"].append((base_url, path, copy.deepcopy(headers), timeout_s))
        response = state["responses"][index]
        if isinstance(response, Exception):
            raise response
        return copy.deepcopy(response)

    monkeypatch.setattr(probe, "http_get", fake_get)
    state["argv"] = ["--base-url", "https://platform.leafdesign.ai",
                     "--acceptance-org-id", ORG.upper(), "--out", str(state["out"]),
                     "--timeout-s", "7"]
    return state


def snapshot(harness):
    return json.loads(harness["out"].read_text(encoding="utf-8"))


def test_happy_path(harness):
    assert probe.main(harness["argv"]) == 0
    result = snapshot(harness)
    assert result["schema"] == "leaf.studio-environment-truth.v1"
    assert result["ok"] is True and result["failed_check"] is None
    assert result["env"] == "production"
    assert result["captured_at"].endswith("Z")
    assert result["session"] == {"tenant_id": TENANT, "org_id": ORG,
                                 "acceptance_org_match": True}
    assert result["spoof"] == {"status": 200, "honored": False}
    assert result["deployment_identity"] == {
        "status": "verified", "environment": "production",
        "services": {"platform": {"digest": DIGEST, "commit": COMMIT}}}
    # Fixed independent canonical byte strings pin sorting and JSON encoding.
    assert result["capabilities"] == {
        "count": 2, "ids": ["alpha", "zeta"],
        "sha256": hashlib.sha256(b'["alpha","zeta"]').hexdigest()}
    assert result["tools"] == {
        "count": 2, "names": ["tool_a", "tool_z"],
        "sha256": hashlib.sha256(b'["tool_a","tool_z"]').hexdigest()}
    calls = harness["calls"]
    assert [call[1] for call in calls] == PATHS
    assert all(call[3] == 7 for call in calls)
    assert all(call[2]["Authorization"] == "Bearer " + TOKEN for call in calls)
    assert all("X-Tenant-Id" not in call[2] for i, call in enumerate(calls) if i != 1)
    foreign = calls[1][2]["X-Tenant-Id"]
    assert uuid.UUID(foreign).version == 4 and foreign not in (TENANT, ORG)
    assert list(harness["out"].parent.iterdir()) == [harness["out"]]


def test_wrong_org_stops_before_spoof(harness):
    harness["responses"][0][1]["org_id"] = str(uuid.uuid4())
    assert probe.main(harness["argv"]) == 1
    assert len(harness["calls"]) == 1
    assert snapshot(harness)["failed_check"] == "acceptance_org_mismatch"
    assert snapshot(harness)["session"]["acceptance_org_match"] is False


def test_spoof_echoing_foreign_tenant_fails(harness):
    harness["responses"][1][1]["tenant_id"] = str(uuid.uuid4())
    assert probe.main(harness["argv"]) == 1
    result = snapshot(harness)
    assert result["ok"] is False
    assert result["failed_check"] == "spoofed_tenant_honored"
    assert result["spoof"]["honored"] is True
    assert len(harness["calls"]) == 2


def test_spoof_403_is_refusal(harness):
    harness["responses"][1] = (403, None)
    assert probe.main(harness["argv"]) == 0
    assert snapshot(harness)["spoof"] == {"status": 403, "honored": False}
    assert len(harness["calls"]) == 5


def test_mismatched_deployment_is_not_verified(harness):
    identity = copy.deepcopy(harness["responses"][2][1])
    for status, environment_fields, requested_env, failed_check in [
        ("mismatch", {"environment": "production"}, "production",
         "deployment_identity_not_verified"),
        ("verified", {"environment": "staging"}, "production",
         "deployment_identity_environment_mismatch"),
        ("verified", {}, "production", "deployment_identity_environment_mismatch"),
        ("verified", {"environment": None}, "production",
         "deployment_identity_environment_mismatch"),
        ("verified", {"environment": 123}, "production",
         "deployment_identity_environment_mismatch"),
        ("verified", {"environment": "production"}, "staging",
         "deployment_identity_environment_mismatch"),
    ]:
        body = copy.deepcopy(identity)
        del body["environment"]
        body.update(status=status, **environment_fields)
        harness["responses"][2] = (200, body)
        harness["calls"].clear()
        assert probe.main(harness["argv"] + ["--env", requested_env]) == 1
        result = snapshot(harness)
        assert result["ok"] is False
        assert result["failed_check"] == failed_check
        assert result["deployment_identity"]["status"] == status
        environment = environment_fields.get("environment")
        assert result["deployment_identity"]["environment"] == (
            environment if isinstance(environment, str) else None)
        assert len(harness["calls"]) == 3


@pytest.mark.parametrize("index,check", enumerate(
    ["session", "spoof", "deployment_identity", "capabilities", "tools"]))
def test_transport_errors_are_unreadable(harness, index, check):
    harness["responses"][index] = (
        TimeoutError(TOKEN) if index % 2 == 0 else urllib.error.URLError(TOKEN))
    assert probe.main(harness["argv"]) == 3
    result = snapshot(harness)
    assert result["ok"] is False
    assert result["failed_check"] == "unreadable_" + check
    assert len(harness["calls"]) == index + 1
    assert TOKEN not in harness["out"].read_text(encoding="utf-8")


def test_missing_bearer_makes_no_requests(harness, monkeypatch):
    monkeypatch.delenv("LEAF_PROBE_BEARER")
    assert probe.main(harness["argv"]) == 2
    assert harness["calls"] == []
    assert not harness["out"].exists()


def test_snapshot_and_output_never_contain_bearer(harness, capsys):
    # Also exercise a credential echoed into an otherwise allowed scalar.
    harness["responses"][2][1]["services"]["platform"]["source_revision"] = TOKEN
    assert probe.main(harness["argv"]) == 0
    output = capsys.readouterr()
    rendered = harness["out"].read_text(encoding="utf-8")
    assert TOKEN not in rendered + output.out + output.err
    assert "private" not in rendered and "intake" not in rendered
    assert snapshot(harness)["deployment_identity"]["services"]["platform"]["commit"] == "[redacted]"


@pytest.mark.parametrize("option,value", [
    ("--base-url", "http://platform.leafdesign.ai"),
    ("--acceptance-org-id", "not-a-uuid"),
    ("--timeout-s", "nan"),
    ("--base-url", "https://localhost@evil.example/path?secret"),
])
def test_invalid_configuration_never_requests(harness, option, value):
    argv = harness["argv"][:]
    argv[argv.index(option) + 1] = value
    assert probe.main(argv) == 2
    assert harness["calls"] == []
    assert not harness["out"].exists()


@pytest.mark.parametrize("base_url", ["http://localhost:8765", "http://127.0.0.1:8765"])
def test_local_http_is_allowed(harness, base_url):
    harness["argv"][1] = base_url
    assert probe.main(harness["argv"]) == 0
    assert snapshot(harness)["base_url"] == base_url
