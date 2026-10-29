"""Offline coverage for the scheduled SPA client drift check."""

from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import urllib.error

import pytest

SCRIPT = Path(__file__).with_name("check_auth0_spa_client_drift.py")
SPEC = importlib.util.spec_from_file_location("check_auth0_spa_client_drift", SCRIPT)
drift = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(drift)

DOMAIN = "fixture.us.auth0.com"
CLIENT_ID = "zkJjr0ZFtcyQjyJ8e4zdkdgzoMaVWt5O"
SECRET = "sentinel-client-secret-do-not-print"
TOKEN = "sentinel-management-token-do-not-print"
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("LEAF_AUTH0_ACTIONS_CLIENT_ID", "fixture-machine-client")
    monkeypatch.setenv("LEAF_AUTH0_ACTIONS_CLIENT_SECRET", SECRET)


class FakeTransport:
    def __init__(self):
        self.calls = []
        self.declared = json.loads(drift.DECLARED_PATH.read_text(encoding="utf-8"))["declared"]
        self.live = deepcopy(self.declared)

    def request(self, method, path, *, body=None, query=None, token=None):
        self.calls.append((method, path))
        if (method, path) == ("POST", "/oauth/token"):
            assert body == {"grant_type": "client_credentials", "client_id": "fixture-machine-client",
                            "client_secret": SECRET, "audience": f"https://{DOMAIN}/api/v2/",
                            "scope": "read:clients"}
            assert token is None and query is None
            return {"access_token": TOKEN}
        assert (method, path) == ("GET", "/api/v2/clients/" + CLIENT_ID)
        assert token == TOKEN and body is None
        assert query == {"fields": ",".join(self.declared), "include_fields": "true"}
        return self.live


class Runner:
    def __init__(self, tmp_path, capsys):
        self.receipts = tmp_path / "receipts"
        self.capsys = capsys
        self.second = 0

    def __call__(self, transport, argv=None):
        self.second += 1
        now = NOW.replace(second=self.second)
        code = drift.main(argv if argv is not None else [
            "--domain", DOMAIN, "--receipt-dir", str(self.receipts)], transport=transport, now=now)
        captured = self.capsys.readouterr()
        path = self.receipts / ("auth0-spa-client-drift-" + now.strftime("%Y%m%dT%H%M%SZ") + ".json")
        receipt = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        return code, receipt, captured


@pytest.fixture
def run(tmp_path, capsys):
    return Runner(tmp_path, capsys)


def test_match_exits_zero(run):
    fake = FakeTransport()
    # Undeclared server fields, including nested fields, do not cause drift.
    fake.live["client_secret"] = SECRET
    fake.live["refresh_token"]["undeclared"] = True
    code, receipt, captured = run(fake)
    assert code == 0 and receipt["state"] == "match"
    assert receipt["schema"] == "leaf.auth0-spa-client-drift-check.v1"
    assert receipt["domain"] == DOMAIN and receipt["client_id"] == CLIENT_ID
    assert receipt["error"] is None and set(receipt["fields"]) == set(fake.declared)
    assert all(field["state"] == "match" for field in receipt["fields"].values())
    assert captured.out == "LEAF_AUTH0_SPA_DRIFT=match\n" and captured.err == ""
    assert fake.calls == [("POST", "/oauth/token"), ("GET", "/api/v2/clients/" + CLIENT_ID)]


def test_added_callback_is_drift(run):
    fake = FakeTransport()
    fake.live["callbacks"].append("https://unexpected.example/callback")
    code, receipt, captured = run(fake)
    assert code == 1 and receipt["state"] == "drift"
    assert receipt["fields"]["callbacks"] == {
        "state": "drift", "added": ["https://unexpected.example/callback"], "removed": []}
    assert captured.out == "LEAF_AUTH0_SPA_DRIFT=drift\n"


def test_removed_web_origin_is_drift(run):
    fake = FakeTransport()
    removed = fake.live["web_origins"].pop()
    code, receipt, _ = run(fake)
    assert code == 1
    assert receipt["fields"]["web_origins"] == {"state": "drift", "added": [], "removed": [removed]}
    del fake.live["web_origins"]
    code, receipt, _ = run(fake)
    assert code == 1 and receipt["fields"]["web_origins"]["state"] == "drift"
    assert set(receipt["fields"]["web_origins"]["removed"]) == set(fake.declared["web_origins"])


def test_list_order_only_is_match(run):
    fake = FakeTransport()
    for value in fake.live.values():
        if isinstance(value, list):
            value.reverse()
            value.append(value[0])
    code, receipt, _ = run(fake)
    assert code == 0 and receipt["state"] == "match"
    assert receipt["fields"]["callbacks"] == {"state": "match", "added": [], "removed": []}


def test_scalar_change_is_drift(run):
    fake = FakeTransport()
    fake.live["token_endpoint_auth_method"] = "client_secret_post"
    code, receipt, _ = run(fake)
    assert code == 1 and receipt["fields"]["token_endpoint_auth_method"] == {"state": "drift"}
    del fake.live["token_endpoint_auth_method"]
    assert run(fake)[0] == 1
    fake.live = deepcopy(fake.declared)
    fake.live["oidc_conformant"] = 1
    assert run(fake)[0] == 1  # Booleans and integers are distinct JSON scalars.


def test_nested_refresh_token_change_is_drift(run):
    fake = FakeTransport()
    fake.live["refresh_token"]["rotation_type"] = "rotating"
    code, receipt, _ = run(fake)
    assert code == 1
    field = receipt["fields"]["refresh_token"]
    assert field["state"] == "drift" and field["fields"]["rotation_type"] == {"state": "drift"}
    assert field["fields"]["leeway"] == {"state": "match"}
    fake.live = deepcopy(fake.declared)
    del fake.live["refresh_token"]["leeway"]
    assert run(fake)[0] == 1


def test_forbidden_is_unavailable(run):
    # Exercise the actual HTTP status mapping with a fake opener, no network.
    class ForbiddenOpener:
        def open(self, request, timeout):
            assert timeout == 30
            if request.get_method() == "POST":
                return TokenResponse()
            assert request.get_method() == "GET"
            raise urllib.error.HTTPError(request.full_url, 403, SECRET, {}, None)

    class TokenResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def geturl(self):
            return f"https://{DOMAIN}/oauth/token"

        def read(self, limit):
            assert limit == 2 * 1024 * 1024
            return json.dumps({"access_token": TOKEN}).encode("utf-8")

    transport = drift.HttpsTransport(DOMAIN, CLIENT_ID)
    transport._opener = ForbiddenOpener()
    code, receipt, captured = run(transport)
    assert code == 2 and receipt["state"] == "unavailable"
    assert receipt["error"] == "management_forbidden" and receipt["fields"] == {}
    assert captured.err == "the machine client needs read:clients\n"
    assert captured.out == "LEAF_AUTH0_SPA_DRIFT=unavailable\n"


def test_missing_credentials_makes_no_request(run, monkeypatch):
    for variable in ("LEAF_AUTH0_ACTIONS_CLIENT_ID", "LEAF_AUTH0_ACTIONS_CLIENT_SECRET"):
        with monkeypatch.context() as patch:
            patch.delenv(variable)
            fake = FakeTransport()
            code, receipt, _ = run(fake)
            assert code == 2 and receipt["state"] == "unavailable"
            assert receipt["error"] == "credentials_missing" and fake.calls == []


def test_transport_refuses_other_paths(run):
    transport = drift.HttpsTransport(DOMAIN, CLIENT_ID)
    with pytest.raises(drift.CheckError, match="^operation_not_allowed$"):
        transport.request("GET", "/api/v2/clients/another-client")
    with pytest.raises(drift.CheckError, match="^operation_not_allowed$"):
        transport.request("PATCH", "/api/v2/clients/" + CLIENT_ID, body={})
    with pytest.raises(drift.CheckError, match="^operation_not_allowed$"):
        transport.request("GET", "/api/v2/clients/" + CLIENT_ID, body={})
    with pytest.raises(drift.CheckError, match="^domain_invalid$"):
        drift.HttpsTransport("evil.example/" + SECRET, CLIENT_ID)
    fake = FakeTransport()
    code, receipt, captured = run(fake, ["--domain", DOMAIN, "--receipt-dir", str(run.receipts),
                                         "--deploy", SECRET])
    assert code == 2 and receipt is None and fake.calls == []
    assert captured.err == drift.ERROR_PREFIX + "arguments_invalid\n"


def test_no_secret_or_token_in_outputs(run):
    fake = FakeTransport()
    fake.live["token_endpoint_auth_method"] = SECRET
    fake.live["client_secret"] = SECRET
    fake.live["access_token"] = TOKEN
    fake.live["callbacks"].extend([SECRET, TOKEN])
    code, receipt, captured = run(fake)
    assert code == 1
    text = json.dumps(receipt) + captured.out + captured.err
    assert SECRET not in text and TOKEN not in text
    # Unexpected exceptions may contain response bodies or credentials.
    class BrokenTransport:
        def request(self, *args, **kwargs):
            raise RuntimeError(SECRET + TOKEN)
    code, receipt, captured = run(BrokenTransport())
    assert code == 2 and receipt["error"] == "request_failed"
    text = json.dumps(receipt) + captured.out + captured.err
    assert SECRET not in text and TOKEN not in text
    for path in run.receipts.iterdir():
        text = path.read_text(encoding="utf-8")
        assert SECRET not in text and TOKEN not in text


def test_repo_declared_file_is_valid():
    record = json.loads(drift.DECLARED_PATH.read_text(encoding="utf-8"))
    assert record["schema"] == "leaf.auth0-spa-client.v1"
    assert record["client_id"] == CLIENT_ID
    declared = record["declared"]
    assert declared["token_endpoint_auth_method"] == "none"
    for key in ("callbacks", "allowed_logout_urls", "web_origins", "allowed_origins"):
        assert isinstance(declared[key], list) and declared[key]
        assert "https://studio.leafautomation.ai" in declared[key]

    def check_keys(value, path=()):
        if isinstance(value, dict):
            for key, child in value.items():
                child_path = path + (key,)
                assert "secret" not in key.lower() or child_path == (
                    "declared", "jwt_configuration", "secret_encoded")
                check_keys(child, child_path)
        elif isinstance(value, list):
            for child in value:
                check_keys(child, path)
    check_keys(record)


def test_receipt_write_failure_exits_three(run):
    run.receipts.write_text("a file blocks the receipt directory", encoding="utf-8")
    code, receipt, captured = run(FakeTransport())
    assert code == 3 and receipt is None
    assert captured.out == "" and captured.err == drift.ERROR_PREFIX + "receipt_write_failed\n"
    assert run.receipts.read_text(encoding="utf-8") == "a file blocks the receipt directory"
