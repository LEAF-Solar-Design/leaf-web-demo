from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys

import pytest

SCRIPT = Path(__file__).with_name("deploy_auth0_actions.py")
SPEC = importlib.util.spec_from_file_location("deploy_auth0_actions", SCRIPT)
sync = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = sync
SPEC.loader.exec_module(sync)

DOMAIN = "fixture.us.auth0.com"
SECRET = "sentinel-client-secret-do-not-print"
TOKEN = "sentinel-management-token-do-not-print"


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("LEAF_AUTH0_ACTIONS_CLIENT_ID", "fixture-client")
    monkeypatch.setenv("LEAF_AUTH0_ACTIONS_CLIENT_SECRET", SECRET)


class FakeTransport:
    def __init__(self, *, drift=False, missing=False, mismatch=False, delay=0,
                 draft_statuses=("built",)):
        self.calls = []
        self.draft_status_reads = []
        self.draft_statuses = draft_statuses
        self.mismatch = mismatch
        self.delay = delay
        self.actions = {}
        self.bindings = {}
        for index, target in enumerate(sync.TARGETS):
            action_id = f"action-{index}"
            code = (sync.REPO_ROOT / target["file"]).read_text(encoding="utf-8")
            self.actions[action_id] = {
                "name": target["action_name"], "code": "old code" if drift else code,
                "version": "old-version", "pending": False, "polls": 0,
            }
            self.bindings[target["trigger"]] = [] if missing and index == 0 else [{
                "id": f"binding-{index}",
                "action": {"id": action_id, "name": target["action_name"]},
            }]

    @property
    def writes(self):
        return [call for call in self.calls if call[0] == "PATCH" or call[1].endswith("/deploy")]

    def request(self, method, path, *, body=None, query=None, token=None):
        self.calls.append((method, path, body, query, token))
        if path == "/oauth/token":
            assert method == "POST" and token is None
            assert body["client_secret"] == SECRET
            assert body["audience"] == f"https://{DOMAIN}/api/v2/"
            assert body["grant_type"] == "client_credentials"
            return {"access_token": TOKEN, "token_type": "Bearer"}
        assert token == TOKEN
        if path.endswith("/bindings"):
            assert method == "GET" and query["per_page"] == "50"
            bindings = self.bindings[path.split("/")[-2]]
            page = int(query["page"])
            return {"bindings": bindings[page * 50:(page + 1) * 50],
                    "total": len(bindings), "page": page, "per_page": 50}
        parts = path.split("/")
        action_id = parts[5]
        action = self.actions[action_id]
        if method == "PATCH":
            assert len(parts) == 6 and set(body) == {"code"}
            action["draft"] = body["code"]
            action["draft_statuses"] = list(self.draft_statuses)
            return {}
        if method == "POST":
            assert parts[6:] == ["deploy"]
            action["pending"] = True
            return {}
        assert method == "GET"
        if len(parts) == 6:
            status = "built"
            if "draft_statuses" in action:
                status = action["draft_statuses"][0]
                if len(action["draft_statuses"]) > 1:
                    action["draft_statuses"].pop(0)
                self.draft_status_reads.append((len(self.calls) - 1, action_id, status))
            if action["pending"]:
                action["polls"] += 1
                if action["polls"] > self.delay:
                    action["version"] = "new-version"
                    action["code"] = "different readback" if self.mismatch else action["draft"]
                    action["pending"] = False
            return {"id": action_id, "name": action["name"],
                    "status": status,
                    "deployed_version": {"id": action["version"]},
                    "code": "draft code must never be compared"}
        assert parts[6:] == ["versions", action["version"]]
        return {"id": action["version"], "action_id": action_id,
                "code": action["code"], "deployed": True, "status": "built"}


def _check(fake, capsys):
    status = sync.main(["--check", "--domain", DOMAIN], transport=fake)
    captured = capsys.readouterr()
    assert captured.err == ""
    assert len(captured.out.splitlines()) == 1
    return status, json.loads(captured.out)


def _deploy(fake, digest, *, sleep=lambda seconds: None):
    return sync.main(["--deploy", "--domain", DOMAIN, "--confirm", digest],
                     transport=fake, sleep=sleep)


def test_check_exits_zero_when_deployed_code_matches(capsys):
    fake = FakeTransport()
    status, report = _check(fake, capsys)
    assert status == 0 and report["result"] == "match"
    assert report["schema"] == "leaf.auth0-actions-sync.v1"
    assert report["mode"] == "check" and report["domain"] == DOMAIN
    assert report["plan_digest"] is None
    assert all(target["state"] == "match" for target in report["targets"])
    assert fake.writes == []
    assert fake.calls[0][2]["scope"] == "read:actions read:triggers"
    assert sync.main(["--domain", DOMAIN], transport=FakeTransport()) == 0
    assert json.loads(capsys.readouterr().out)["mode"] == "check"


def test_check_exits_one_on_drift_and_prints_plan_digest(capsys):
    fake = FakeTransport(drift=True)
    # Put the matching binding on page two to exercise actual pagination.
    trigger = sync.TARGETS[0]["trigger"]
    fake.bindings[trigger] = [{"id": f"other-{i}", "action": {
        "id": f"other-action-{i}", "name": "unmanaged"}} for i in range(50)] + fake.bindings[trigger]
    status, report = _check(fake, capsys)
    assert status == 1 and report["result"] == "drift"
    projection = [{key: target[key] for key in (
        "trigger", "action_name", "repo_sha256", "deployed_sha256")}
        for target in report["targets"]]
    expected = hashlib.sha256(json.dumps(sorted(projection, key=lambda t: t["trigger"]),
                                         sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert report["plan_digest"] == expected
    assert sync.plan_digest(list(reversed(report["targets"]))) == expected
    assert fake.writes == []
    assert any(call[3] == {"page": "1", "per_page": "50"} for call in fake.calls)
    fake = FakeTransport()
    duplicate = dict(fake.bindings[trigger][0], id="duplicate-binding")
    fake.bindings[trigger].append(duplicate)
    assert sync.main(["--domain", DOMAIN], transport=fake) == 2
    assert capsys.readouterr().err == "LEAF_AUTH0_ACTIONS_ERROR=action_ambiguous\n"


def test_check_reports_missing_action_as_drift(capsys):
    fake = FakeTransport(missing=True)
    status, report = _check(fake, capsys)
    assert status == 1 and report["result"] == "drift"
    assert report["targets"][0]["state"] == "missing"
    assert report["targets"][0]["deployed_sha256"] is None
    assert report["plan_digest"] is not None
    assert _deploy(fake, report["plan_digest"]) == 2
    assert capsys.readouterr().err == "LEAF_AUTH0_ACTIONS_ERROR=action_missing\n"
    assert fake.writes == []


def test_check_normalizes_line_endings_before_hashing(capsys):
    fake = FakeTransport()
    fake.actions["action-0"]["code"] = fake.actions["action-0"]["code"].replace("\n", "\r\n") + "\r\n \t"
    fake.actions["action-1"]["code"] = fake.actions["action-1"]["code"].replace("\n", "\r")
    assert _check(fake, capsys)[0] == 0
    assert sync.normalized_sha256("a\r\nb\r  \t") == hashlib.sha256(b"a\nb\n").hexdigest()


def test_deploy_refuses_without_matching_confirm_digest(capsys):
    fake = FakeTransport(drift=True)
    assert sync.main(["--deploy", "--domain", DOMAIN], transport=fake) == 2
    assert fake.calls == []
    assert capsys.readouterr().err == "LEAF_AUTH0_ACTIONS_ERROR=confirm_required\n"
    assert _deploy(fake, "0" * 64) == 2
    assert capsys.readouterr().err == "LEAF_AUTH0_ACTIONS_ERROR=confirm_mismatch\n"
    assert fake.writes == []
    for args in (["--check", "--confirm", "0" * 64], ["--check", "--deploy"],
                 ["--deploy", "--confirm", "A" * 64]):
        fresh = FakeTransport()
        assert sync.main(["--domain", DOMAIN] + args, transport=fresh) == 2
        assert fresh.calls == []
        assert capsys.readouterr().out == ""


def test_deploy_updates_deploys_and_reads_back_match(capsys):
    fake = FakeTransport(drift=True, delay=2)
    _, report = _check(fake, capsys)
    fake.calls.clear()
    sleeps = []
    assert _deploy(fake, report["plan_digest"], sleep=sleeps.append) == 0
    deployed = json.loads(capsys.readouterr().out)
    assert deployed["result"] == "deployed" and deployed["mode"] == "deploy"
    assert all(target["state"] == "match" for target in deployed["targets"])
    assert sleeps == [3, 3, 3, 3]
    assert fake.calls[0][2]["scope"] == "read:actions read:triggers update:actions create:actions"
    assert [(call[0], call[1]) for call in fake.writes] == [
        ("PATCH", "/api/v2/actions/actions/action-0"),
        ("POST", "/api/v2/actions/actions/action-0/deploy"),
        ("PATCH", "/api/v2/actions/actions/action-1"),
        ("POST", "/api/v2/actions/actions/action-1/deploy")]
    for index, target in enumerate(sync.TARGETS):
        expected = (sync.REPO_ROOT / target["file"]).read_bytes().decode().replace("\r\n", "\n")
        assert fake.actions[f"action-{index}"]["draft"] == expected
    # A deploy response alone is not evidence that the version changed.
    stalled = FakeTransport(drift=True, delay=100)
    _, report = _check(stalled, capsys)
    sleeps = []
    assert _deploy(stalled, report["plan_digest"], sleep=sleeps.append) == 2
    assert capsys.readouterr().err == "LEAF_AUTH0_ACTIONS_ERROR=deploy_not_observed\n"
    assert stalled.actions["action-0"]["polls"] == 10 and sleeps == [3] * 9


def test_deploy_exits_one_when_readback_hash_differs(capsys):
    fake = FakeTransport(drift=True, mismatch=True)
    _, report = _check(fake, capsys)
    assert _deploy(fake, report["plan_digest"]) == 1
    readback = json.loads(capsys.readouterr().out)
    assert readback["result"] == "readback_mismatch"
    assert all(target["state"] == "drift" for target in readback["targets"])


def test_deploy_with_no_drift_makes_no_writes(capsys):
    fake = FakeTransport()
    assert _deploy(fake, "0" * 64) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["result"] == "match" and report["plan_digest"] is None
    assert fake.writes == []


def test_deploy_touches_only_the_drifting_target(capsys):
    fake = FakeTransport()
    fake.actions["action-1"]["code"] = "old code"
    _, report = _check(fake, capsys)
    assert [target["state"] for target in report["targets"]] == ["match", "drift"]
    fake.calls.clear()
    assert _deploy(fake, report["plan_digest"]) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["result"] == "deployed"
    assert [(call[0], call[1]) for call in fake.writes] == [
        ("PATCH", "/api/v2/actions/actions/action-1"),
        ("POST", "/api/v2/actions/actions/action-1/deploy")]
    assert "draft" not in fake.actions["action-0"]
    assert fake.actions["action-0"]["version"] == "old-version"


def test_deploy_waits_for_the_draft_to_build_before_deploying(capsys):
    fake = FakeTransport(drift=True, draft_statuses=("pending", "pending", "built"))
    _, report = _check(fake, capsys)
    fake.calls.clear()
    sleeps = []
    assert _deploy(fake, report["plan_digest"], sleep=sleeps.append) == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert json.loads(captured.out)["result"] == "deployed"
    assert sleeps == [1, 1, 1, 1]
    for action_id in fake.actions:
        path = f"/api/v2/actions/actions/{action_id}"
        patch_index = next(i for i, call in enumerate(fake.calls)
                           if call[:2] == ("PATCH", path))
        deploy_index = next(i for i, call in enumerate(fake.calls)
                            if call[:2] == ("POST", path + "/deploy"))
        reads = [(i, status) for i, read_id, status in fake.draft_status_reads
                 if read_id == action_id and patch_index < i < deploy_index]
        assert [status for _, status in reads] == ["pending", "pending", "built"]
        assert fake.calls[reads[-1][0]][:2] == ("GET", path)


def test_deploy_fails_closed_when_the_draft_does_not_build(monkeypatch, capsys):
    monkeypatch.setattr(sync, "DRAFT_BUILD_TIMEOUT_SECONDS", 2)
    for status, error in (("failed", "draft_build_failed"), ("pending", "draft_build_timeout")):
        now = [0.0]
        sleeps = []
        monkeypatch.setattr(sync.time, "monotonic", lambda: now[0])

        def sleep(seconds):
            sleeps.append(seconds)
            now[0] += seconds

        fake = FakeTransport(drift=True, draft_statuses=(status,))
        _, report = _check(fake, capsys)
        fake.calls.clear()
        assert _deploy(fake, report["plan_digest"], sleep=sleep) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == (
            f"LEAF_AUTH0_ACTIONS_ERROR={error}\n"
            f"Action {sync.TARGETS[0]['action_name']} did not build; last status: {status}.\n")
        assert [(call[0], call[1]) for call in fake.writes] == [
            ("PATCH", "/api/v2/actions/actions/action-0")]
        assert sleeps == ([] if status == "failed" else [1, 1])
        assert all(action["version"] == "old-version" for action in fake.actions.values())


def test_missing_credentials_fail_closed_without_network(monkeypatch, capsys):
    for name in ("LEAF_AUTH0_ACTIONS_CLIENT_ID", "LEAF_AUTH0_ACTIONS_CLIENT_SECRET"):
        for value in (None, ""):
            with monkeypatch.context() as patch:
                if value is None:
                    patch.delenv(name)
                else:
                    patch.setenv(name, value)
                fake = FakeTransport()
                assert sync.main(["--domain", DOMAIN], transport=fake) == 2
                captured = capsys.readouterr()
                assert captured.out == ""
                assert captured.err == "LEAF_AUTH0_ACTIONS_ERROR=management_credentials_missing\n"
                assert fake.calls == []


def test_secret_never_reaches_output_or_argv(capsys):
    argv = ["--domain", DOMAIN]
    original = list(sys.argv)
    fake = FakeTransport()
    assert sync.main(argv, transport=fake) == 0
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert TOKEN not in captured.out + captured.err
    assert fake.actions["action-0"]["code"] not in captured.out + captured.err
    assert argv == ["--domain", DOMAIN] and sys.argv == original
    for action in sync.build_parser()._actions:
        assert all("secret" not in option and "token" not in option for option in action.option_strings)

    class FailingTransport:
        def request(self, *args, **kwargs):
            raise RuntimeError(SECRET + TOKEN)

    assert sync.main(argv, transport=FailingTransport()) == 2
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == "LEAF_AUTH0_ACTIONS_ERROR=unexpected\n"
    assert sync.main(argv + ["--unknown", SECRET], transport=fake) == 2
    assert SECRET not in capsys.readouterr().err


class FakeOpener:
    def __init__(self):
        self.calls = []
        self.payload = b"{}"

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        response = io.BytesIO(self.payload)
        response.geturl = lambda: request.full_url
        response.status = 200
        return response


def test_transport_refuses_paths_outside_actions_api(monkeypatch):
    opener = FakeOpener()
    handlers = []

    def build_opener(*args):
        handlers.extend(args)
        return opener

    monkeypatch.setattr(sync.urllib.request, "build_opener", build_opener)
    readonly = sync.HttpsTransport(DOMAIN, allow_writes=False)
    writable = sync.HttpsTransport(DOMAIN, allow_writes=True)
    for transport, method, path in (
        (readonly, "GET", "/api/v2/users"),
        (writable, "DELETE", "/api/v2/actions/actions/action-0"),
        (readonly, "PATCH", "/api/v2/actions/actions/action-0"),
        (readonly, "POST", "/api/v2/actions/actions/action-0/deploy"),
        (writable, "PATCH", "/api/v2/actions/triggers/post-login/bindings"),
        (writable, "GET", "/api/v2/actions/../users"),
        (writable, "GET", "https://other.auth0.com/api/v2/actions/actions/action-0"),
        (writable, "PATCH", "/api/v2/actions/actions/action-0?unexpected=true"),
    ):
        with pytest.raises(sync.SyncError):
            transport.request(method, path)
    assert opener.calls == []
    assert handlers and all(isinstance(handler, sync._NoRedirect) for handler in handlers)
    assert handlers[0].redirect_request(None, None, 302, "", {}, "https://other.test") is None
    for domain in ("https://fixture.auth0.com", "fixture.auth0.com.evil", "FIXTURE.auth0.com", "fixture.auth0.com\n"):
        with pytest.raises(sync.SyncError):
            sync.HttpsTransport(domain, allow_writes=True)


def test_pre_user_registration_action_is_never_a_target():
    assert isinstance(sync.TARGETS, tuple) and len(sync.TARGETS) == 2
    assert sync.TARGETS == (
        {"file": "server/auth0-actions/post-login-add-tenant-claim.js",
         "trigger": "post-login", "action_name": "post-login-add-tenant-claim"},
        {"file": "server/auth0-actions/credentials-exchange-add-tenant-claim.js",
         "trigger": "credentials-exchange", "action_name": "credentials-exchange-add-tenant-claim"},
    )
    assert "pre-user-registration" not in json.dumps(sync.TARGETS)


def test_every_request_has_a_timeout(monkeypatch):
    opener = FakeOpener()
    monkeypatch.setattr(sync.urllib.request, "build_opener", lambda *args: opener)
    transport = sync.HttpsTransport(DOMAIN, allow_writes=True)
    for method, path, kwargs in (
        ("POST", "/oauth/token", {"body": {"client_secret": SECRET}}),
        ("GET", "/api/v2/actions/triggers/post-login/bindings", {"query": {"page": "0", "per_page": "50"}}),
        ("GET", "/api/v2/actions/actions/action-0", {}),
        ("GET", "/api/v2/actions/actions/action-0/versions/version-0", {}),
        ("PATCH", "/api/v2/actions/actions/action-0", {"body": {"code": "fixture code"}}),
        ("POST", "/api/v2/actions/actions/action-0/deploy", {}),
    ):
        assert transport.request(method, path, **kwargs) == {}
    assert len(opener.calls) == 6
    assert all(isinstance(timeout, (int, float)) and 0 < timeout <= 30
               for _, timeout in opener.calls)
    opener.payload = b"x" * (sync.MAX_RESPONSE_BYTES + 1)
    with pytest.raises(sync.SyncError, match="response_too_large"):
        transport.request("GET", "/api/v2/actions/actions/action-0")
    opener.payload = SECRET.encode()
    with pytest.raises(sync.SyncError) as caught:
        transport.request("GET", "/api/v2/actions/actions/action-0")
    assert SECRET not in str(caught.value)
