from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import sys

import pytest

SCRIPT = Path(__file__).with_name("check_auth0_action_drift.py")
SPEC = importlib.util.spec_from_file_location("check_auth0_action_drift", SCRIPT)
drift = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = drift
SPEC.loader.exec_module(drift)
sync = drift.sync

BUILDSPEC = Path(__file__).resolve().parents[1] / ".codebuild" / "auth0-action-drift.yml"
DOMAIN = "fixture.us.auth0.com"
SECRET = "sentinel-client-secret-do-not-print"
TOKEN = "sentinel-management-token-do-not-print"


@pytest.fixture(autouse=True)
def credentials(monkeypatch):
    monkeypatch.setenv("LEAF_AUTH0_ACTIONS_CLIENT_ID", "fixture-client")
    monkeypatch.setenv("LEAF_AUTH0_ACTIONS_CLIENT_SECRET", SECRET)


class FakeTransport:
    """Read-only Auth0 double: any write raises, every call is recorded."""

    def __init__(self, *, fail=None, missing=False):
        self.calls = []
        self.fail = fail
        self.missing = missing
        self.code = {target["trigger"]: (sync.REPO_ROOT / target["file"]).read_text(encoding="utf-8")
                     for target in sync.TARGETS}

    @property
    def writes(self):
        return [call for call in self.calls if call[0] != "GET" and call[1] != "/oauth/token"]

    def request(self, method, path, *, body=None, query=None, token=None):
        self.calls.append((method, path))
        if path == "/oauth/token":
            assert method == "POST" and body["client_secret"] == SECRET
            assert body["scope"] == sync.CHECK_SCOPES
            return {"access_token": TOKEN}
        if method != "GET":
            raise AssertionError("drift check attempted an Action write")
        assert token == TOKEN
        if self.fail:
            raise sync.SyncError(self.fail)
        parts = path.split("/")
        if path.endswith("/bindings"):
            index = [target["trigger"] for target in sync.TARGETS].index(parts[-2])
            target = sync.TARGETS[index]
            bindings = [] if self.missing and index == 0 else [{
                "id": f"binding-{index}",
                "action": {"id": f"action-{index}", "name": target["action_name"]}}]
            return {"bindings": bindings, "total": len(bindings), "page": 0, "per_page": 50}
        index = int(parts[5].split("-")[1])
        target = sync.TARGETS[index]
        if len(parts) == 6:
            return {"id": parts[5], "name": target["action_name"],
                    "deployed_version": {"id": "version-1"}}
        return {"id": "version-1", "action_id": parts[5], "deployed": True, "status": "built",
                "code": self.code[target["trigger"]]}

    def drift(self, index=0, code="old code"):
        self.code[sync.TARGETS[index]["trigger"]] = code


class Runner:
    def __init__(self, tmp_path, capsys):
        self.tmp_path = tmp_path
        self.capsys = capsys
        self.receipts = tmp_path / "receipts"
        self.state = tmp_path / "state" / "state.json"
        self.minute = 0

    def __call__(self, transport, argv=None):
        self.minute += 1
        now = datetime(2026, 10, 1, 0, self.minute, tzinfo=timezone.utc)
        status = drift.main(argv or ["--domain", DOMAIN, "--receipt-dir", str(self.receipts),
                                     "--state-file", str(self.state)],
                            transport=transport, now=now)
        captured = self.capsys.readouterr()
        lines = captured.out.splitlines()
        notices = [json.loads(line[len(drift.NOTICE_PREFIX):])
                   for line in lines if line.startswith(drift.NOTICE_PREFIX)]
        receipt = None
        if status in (0, 1, 2) and lines:
            summary = json.loads(lines[-1])
            receipt = json.loads((self.receipts / summary["receipt"]).read_text(encoding="utf-8"))
            assert summary["state"] == receipt["state"]
        return status, receipt, notices, captured


@pytest.fixture
def run(tmp_path, capsys):
    return Runner(tmp_path, capsys)


def test_match_writes_a_receipt_and_no_notice(run):
    fake = FakeTransport()
    status, receipt, notices, captured = run(fake)
    assert status == 0 and captured.err == ""
    assert receipt["schema"] == drift.RECEIPT_SCHEMA and receipt["state"] == "match"
    assert receipt["domain"] == DOMAIN and receipt["plan_digest"] is None
    assert [target["state"] for target in receipt["targets"]] == ["match", "match"]
    assert receipt["error_code"] is None and receipt["action_writes"] == 0
    assert receipt["notice_emitted"] is False and receipt["notice_suppressed"] is False
    assert notices == [] and fake.writes == []
    state = json.loads(run.state.read_text(encoding="utf-8"))
    assert state["drift_digest"] is None and state["unavailable_code"] is None


def test_unchanged_drift_emits_exactly_one_notice(run):
    fake = FakeTransport()
    fake.drift()
    results = [run(fake) for _ in range(3)]
    assert [status for status, *_ in results] == [1, 1, 1]
    receipts = [receipt for _, receipt, _, _ in results]
    assert all(receipt["state"] == "drift" for receipt in receipts)
    digest = receipts[0]["plan_digest"]
    assert drift.HEX64.fullmatch(digest)
    assert all(receipt["plan_digest"] == digest for receipt in receipts)
    notices = [notice for _, _, batch, _ in results for notice in batch]
    assert len(notices) == 1
    assert notices[0]["kind"] == "drift" and notices[0]["plan_digest"] == digest
    assert notices[0]["dedupe_key"] == "drift:" + digest
    assert notices[0]["drifted"] == [{"trigger": sync.TARGETS[0]["trigger"],
                                      "action_name": sync.TARGETS[0]["action_name"],
                                      "state": "drift"}]
    assert [receipt["notice_suppressed"] for receipt in receipts] == [False, True, True]
    assert len(list(run.receipts.iterdir())) == 3
    # A changed drift is a new fact and notifies once more.
    fake.drift(1, "other old code")
    status, receipt, notices, _ = run(fake)
    assert status == 1 and receipt["plan_digest"] != digest and len(notices) == 1
    assert run(fake)[2] == []
    assert fake.writes == []


def test_drift_that_clears_and_returns_notifies_again(run):
    fake = FakeTransport()
    fake.drift()
    assert len(run(fake)[2]) == 1
    original = (sync.REPO_ROOT / sync.TARGETS[0]["file"]).read_text(encoding="utf-8")
    fake.drift(0, original)
    status, receipt, notices, _ = run(fake)
    assert status == 0 and receipt["state"] == "match" and notices == []
    fake.drift()
    status, _, notices, _ = run(fake)
    assert status == 1 and len(notices) == 1
    assert fake.writes == []


def test_missing_action_is_drift_not_match(run):
    fake = FakeTransport(missing=True)
    status, receipt, notices, _ = run(fake)
    assert status == 1 and receipt["state"] == "drift"
    assert receipt["targets"][0]["state"] == "missing"
    assert receipt["targets"][0]["deployed_sha256"] is None
    assert notices[0]["drifted"][0]["state"] == "missing"
    assert fake.writes == []


def test_unavailable_is_distinct_deduped_and_keeps_the_drift_key(run):
    fake = FakeTransport(fail="request_failed")
    status, receipt, notices, captured = run(fake)
    assert status == 2 and receipt["state"] == "unavailable"
    assert receipt["error_code"] == "request_failed" and receipt["targets"] == []
    assert receipt["plan_digest"] is None
    assert [notice["kind"] for notice in notices] == ["unavailable"]
    assert notices[0]["error_code"] == "request_failed"
    assert notices[0]["dedupe_key"] == "unavailable:request_failed"
    assert run(fake)[2] == []
    # drift, an unavailable blip, then the same drift: one drift notice only.
    fake.fail = None
    fake.drift()
    assert len(run(fake)[2]) == 1
    fake.fail = "request_failed"
    assert [notice["kind"] for notice in run(fake)[2]] == ["unavailable"]
    fake.fail = None
    status, receipt, notices, _ = run(fake)
    assert status == 1 and notices == [] and receipt["notice_suppressed"] is True
    assert fake.writes == []


def test_missing_credentials_are_unavailable_before_any_call(run, monkeypatch):
    monkeypatch.delenv("LEAF_AUTH0_ACTIONS_CLIENT_SECRET")
    fake = FakeTransport()
    status, receipt, notices, _ = run(fake)
    assert status == 2 and receipt["error_code"] == "management_credentials_missing"
    assert notices[0]["kind"] == "unavailable" and fake.calls == []


def test_malformed_check_output_is_unavailable_never_match(run, monkeypatch):
    def fake_main(stdout, code, stderr=""):
        def runner(argv, *, transport=None):
            print(stdout, end="")
            print(stderr, end="", file=sys.stderr)
            return code
        return runner

    good = {"schema": drift.SYNC_SCHEMA, "mode": "check",
            "domain": DOMAIN, "result": "match", "plan_digest": None,
            "targets": [{"trigger": target["trigger"], "action_name": target["action_name"],
                         "repo_sha256": "a" * 64, "deployed_sha256": "a" * 64, "state": "match"}
                        for target in sync.TARGETS]}
    cases = [
        (fake_main("not json\n", 0), "report_invalid"),
        (fake_main(json.dumps(dict(good, plan_digest="b" * 64)) + "\n", 0), "report_invalid"),
        (fake_main(json.dumps(dict(good, mode="deploy")) + "\n", 0), "report_invalid"),
        (fake_main(json.dumps(good) + "\n", 1), "report_invalid"),
        (fake_main(json.dumps(dict(good, targets=good["targets"][:1])) + "\n", 0), "report_invalid"),
        (fake_main("", 2, "Traceback: something\n"), "check_error_unrecognized"),
    ]
    for runner, code in cases:
        monkeypatch.setattr(sync, "main", runner)
        status, receipt, _, _ = run(None)
        assert status == 2 and receipt["state"] == "unavailable" and receipt["error_code"] == code

    def crash(argv, *, transport=None):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(sync, "main", crash)
    status, receipt, _, captured = run(None)
    assert status == 2 and receipt["error_code"] == "check_crashed"
    assert SECRET not in captured.out + captured.err
    monkeypatch.setattr(sync, "main", fake_main(json.dumps(good) + "\n", 0))
    assert run(None)[0] == 0


def test_only_check_mode_runs_and_the_real_transport_has_writes_off(run, monkeypatch):
    seen_argv, seen_transports = [], []
    real_main = sync.main

    def spy(argv, *, transport=None):
        seen_argv.append(list(argv))
        return real_main(argv, transport=transport)

    def transport_factory(domain, *, allow_writes):
        seen_transports.append((domain, allow_writes))
        return FakeTransport()

    monkeypatch.setattr(sync, "main", spy)
    monkeypatch.setattr(sync, "HttpsTransport", transport_factory)
    status, receipt, _, _ = run(None)
    assert status == 0 and receipt["state"] == "match"
    assert seen_argv == [["--check", "--domain", DOMAIN]]
    assert seen_transports == [(DOMAIN, False)]


def test_deploy_arguments_are_refused_without_any_call(run):
    fake = FakeTransport()
    base = ["--domain", DOMAIN, "--receipt-dir", str(run.receipts), "--state-file", str(run.state)]
    for extra in (["--deploy"], ["--confirm", "0" * 64], ["--check"]):
        status, receipt, notices, captured = run(fake, base + extra)
        assert status == 2 and receipt is None and notices == []
        assert captured.out == "" and captured.err == drift.ERROR_PREFIX + "arguments_invalid\n"
    assert fake.calls == [] and not run.receipts.exists()


def test_invalid_domain_is_unavailable_and_never_recorded(run):
    fake = FakeTransport()
    bad = "evil.example.com/" + SECRET
    status, receipt, _, captured = run(fake, ["--domain", bad, "--receipt-dir", str(run.receipts),
                                              "--state-file", str(run.state)])
    assert status == 2 and receipt["error_code"] == "domain_invalid" and receipt["domain"] is None
    assert fake.calls == []
    assert SECRET not in captured.out + run.state.read_text(encoding="utf-8")


def test_corrupt_or_foreign_state_renotifies_rather_than_missing(run):
    fake = FakeTransport()
    fake.drift()
    _, receipt, notices, _ = run(fake)
    digest = receipt["plan_digest"]
    assert len(notices) == 1
    for broken in ("{not json", json.dumps({"schema": drift.STATE_SCHEMA, "domain": "other.auth0.com",
                                            "drift_digest": digest, "unavailable_code": None}),
                   json.dumps({"schema": "wrong", "domain": DOMAIN, "drift_digest": digest}),
                   "x" * (drift.MAX_STATE_BYTES + 1)):
        run.state.write_text(broken, encoding="utf-8")
        assert len(run(fake)[2]) == 1
        state = json.loads(run.state.read_text(encoding="utf-8"))
        assert state["schema"] == drift.STATE_SCHEMA and state["drift_digest"] == digest


def test_no_credential_reaches_any_output_or_file(run):
    fake = FakeTransport()
    fake.drift()
    outputs = [run(fake)[3] for _ in range(2)]
    fake.fail = "management_forbidden"
    outputs.append(run(fake)[3])
    text = "".join(captured.out + captured.err for captured in outputs)
    text += "".join(path.read_text(encoding="utf-8") for path in run.tmp_path.rglob("*") if path.is_file())
    assert SECRET not in text and TOKEN not in text


def test_a_receipt_is_never_overwritten_and_a_failed_write_exits_three(run):
    run.receipts.mkdir(parents=True)
    taken = run.receipts / "auth0-action-drift-20261001T000100000000Z.json"
    taken.write_text("earlier receipt", encoding="utf-8")
    fake = FakeTransport()
    status, receipt, notices, captured = run(fake)
    assert status == drift.EXIT_WRITE_FAILED and receipt is None and notices == []
    assert captured.err == drift.ERROR_PREFIX + "receipt_or_state_write_failed\n"
    assert taken.read_text(encoding="utf-8") == "earlier receipt"
    assert not run.state.exists()


def test_buildspec_runs_only_the_read_only_wrapper():
    text = BUILDSPEC.read_text(encoding="utf-8")
    assert text.startswith("version: 0.2\n")
    assert "python3 scripts/check_auth0_action_drift.py" in text
    assert "set -euo pipefail" in text and "set -x" not in text
    commands = [line for line in text.splitlines() if not line.lstrip().startswith("#")]
    body = "\n".join(commands)
    assert "--deploy" not in body and "--confirm" not in body
    assert "deploy_auth0_actions.py" not in body
    # Credentials are external project inputs: never assigned or fetched here.
    assert "CLIENT_SECRET" not in body and "secrets-manager" not in body
    assert '--domain "$LEAF_AUTH0_DOMAIN"' in body
    assert "0|1|2)" in body and "base-directory: auth0-drift-artifacts" in body
