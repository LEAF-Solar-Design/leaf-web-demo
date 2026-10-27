"""Offline durability-probe contract tests; no AWS or network access."""
from copy import deepcopy
import builtins
import json
from pathlib import Path
import socket
from types import ModuleType

import pytest

import probe_conv_durable_restart as p

PROJECT = "11111111-1111-4111-8111-111111111111"
CONVERSATION = "22222222-2222-4222-8222-222222222222"
MESSAGE = "33333333-3333-4333-8333-333333333333"
TOKEN = "SENTINEL_PRIVATE_BEARER_TOKEN"
DEFINITION = "arn:aws:ecs:us-east-1:807034087062:task-definition/leaf-platform:42"
CREATED = "2026-10-27T01:02:03+00:00"
RECEIPT_KEYS = {
    "schema", "env", "origin", "run_id", "project_sha256", "conversation_id",
    "message_id", "content_sha256", "source_sha_before", "source_sha_after",
    "tasks_before", "tasks_after", "task_definition", "checks", "outcome",
    "reason", "started_at", "finished_at",
}


class Fake(p.LiveAdapters):
    def __init__(self):
        super().__init__("staging")
        self.clock = 0
        self.account = p.ACCOUNT
        self.service_data = {"deployments": [{"rolloutState": "COMPLETED"}],
                             "runningCount": 1, "desiredCount": 1,
                             "taskDefinition": DEFINITION}
        self.builds = 0
        self.build_reads = 0
        self.restarts = 0
        self.force_calls = []
        self.unchanged_tasks = False
        self.unhealthy = False
        self.tail_defect = None
        self.replay_new = False
        self.posts = []
        self.saved = []
        self.created = None
        self.raise_write = False

    def client(self, name):
        raise AssertionError("AWS forbidden")

    def now(self):
        return 1700000000 + self.clock

    def monotonic(self):
        return self.clock

    def sleep(self, seconds):
        assert 0 < seconds <= p.POLL_S
        self.clock += seconds

    def nonce(self):
        return "0123456789abcdef"

    def caller_account(self):
        return self.account

    def service(self, environment):
        return deepcopy(self.service_data)

    def delivery_builds_in_progress(self):
        self.build_reads += 1
        return self.builds

    def running_tasks(self, environment):
        return ["b" * 32 if self.restarts and not self.unchanged_tasks else "a" * 32]

    def force_new_deployment(self, environment, task_definition):
        assert len(self.posts) == 2
        self.force_calls.append((environment, task_definition))
        self.restarts += 1

    def save(self, receipt):
        super().save(receipt)
        self.saved.append(deepcopy(receipt))

    def http(self, method, path, token, body=None, key=None):
        assert token == TOKEN
        if path == "/api/health":
            return (503 if self.restarts and self.unhealthy else 200), {
                "source_sha": "c" * 40, "ignored_secret": TOKEN}
        assert path.startswith("/api/projects/" + PROJECT + "/conversations")
        if method == "POST":
            self.posts.append((path, deepcopy(body), key))
            if self.raise_write:
                raise OSError(TOKEN)
            if path.endswith("/conversations"):
                assert body == {} and key is None
                return 201, {"conversation_id": CONVERSATION}
            assert path.endswith("/" + CONVERSATION + "/messages")
            assert body == {"content": "p058 offline-1 0123456789abcdef",
                            "metadata": {"probe": "p058", "run_id": "offline-1"}}
            assert key == "p058-offline-1"
            if self.created is None:
                self.created = {"message_id": MESSAGE, "created_at": CREATED,
                                "content": body["content"]}
                return 201, deepcopy(self.created)
            assert self.posts[1] == self.posts[2]
            return 200, {**self.created, "message_id": PROJECT if self.replay_new else MESSAGE}
        assert path.endswith("/recovery/tail?limit=50")
        items = [deepcopy(self.created)]
        gaps = []
        if self.tail_defect == "missing_before" and not self.restarts:
            items = []
        if self.tail_defect == "missing_after" and self.restarts:
            items = []
        if self.tail_defect == "duplicate":
            items *= 2
        if self.tail_defect == "gaps":
            gaps = [{"ignored_secret": TOKEN}]
        if self.restarts and self.tail_defect in {"content", "created_at"}:
            items[0][self.tail_defect] = "changed"
        return 200, {"items": items, "gaps": gaps}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network forbidden")
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def invoke(tmp_path, fake=None, environment="staging", token=TOKEN, **changes):
    fake = fake if fake is not None else Fake()
    options = {"env": environment, "project-id": PROJECT, "out-dir": str(tmp_path),
               "run-id": "offline-1", "ack": p.ENVIRONMENTS[environment]["ack"]}
    options.update(changes)
    argv = ["run"]
    for key, value in options.items():
        argv.extend(["--" + key, value])
    code = p.main(argv, {} if token is None else {"LEAF_P058_TOKEN": token}, fake)
    return code, fake


def receipt(tmp_path):
    return json.loads((tmp_path / "offline-1" / "receipt.json").read_text())


def refused(tmp_path, reason, **kwargs):
    code, fake = invoke(tmp_path, **kwargs)
    assert code == 2
    assert receipt(tmp_path)["outcome"] == "REFUSED"
    assert receipt(tmp_path)["reason"] == reason
    assert not fake.posts and not fake.restarts


def test_import_performs_no_io(monkeypatch):
    source = Path(p.__file__).read_text()
    compiled = compile(source, p.__file__, "exec")
    def forbidden(*args, **kwargs):
        raise AssertionError("import performed I/O")
    monkeypatch.setattr(builtins, "open", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(p.os, "fsync", forbidden)
    monkeypatch.setattr(p.secrets, "token_hex", forbidden)
    monkeypatch.setattr(p.time, "time", forbidden)
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        assert name not in {"boto3", "botocore", "conversations", "main"}
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    module = ModuleType("offline_import_probe")
    exec(compiled, module.__dict__)
    assert module.SCHEMA == p.SCHEMA


def test_ack_refused(tmp_path):
    refused(tmp_path, "ack_refused", ack="wrong")


@pytest.mark.parametrize("project", ["bad", "00000000-0000-0000-0000-000000000000"])
def test_project_refused(tmp_path, project):
    refused(tmp_path, "project_refused", **{"project-id": project})


def test_token_required(tmp_path):
    refused(tmp_path, "token_required", token=None)


def test_run_exists(tmp_path, capsys):
    directory = tmp_path / "offline-1"
    directory.mkdir()
    original = directory / "receipt.json"
    original.write_text("original evidence")
    code, fake = invoke(tmp_path)
    assert code == 2 and not fake.posts
    assert original.read_text() == "original evidence"
    assert json.loads(capsys.readouterr().out)["reason"] == "run_exists"


def test_account_refused(tmp_path):
    fake = Fake()
    fake.account = "000000000000"
    refused(tmp_path, "account_refused", fake=fake)


@pytest.mark.parametrize("defect", ["deployment", "count", "rollout"])
def test_unsteady_service(tmp_path, defect):
    fake = Fake()
    if defect == "deployment":
        fake.service_data["deployments"] *= 2
    elif defect == "count":
        fake.service_data["runningCount"] = 0
    else:
        fake.service_data["deployments"][0]["rolloutState"] = "IN_PROGRESS"
    refused(tmp_path, "service_unsteady", fake=fake)


def test_staging_delivery_in_flight(tmp_path):
    fake = Fake()
    fake.builds = 1
    refused(tmp_path, "delivery_in_flight", fake=fake)


def test_production_ignores_delivery_builds(tmp_path):
    fake = Fake()
    fake.builds = 1
    code, fake = invoke(tmp_path, fake, environment="production")
    assert code == 0 and fake.build_reads == 0
    assert receipt(tmp_path)["origin"] == p.ENVIRONMENTS["production"]["origin"]


def test_happy_path_exact_pass_receipt(tmp_path):
    code, fake = invoke(tmp_path)
    r = receipt(tmp_path)
    assert code == 0 and r["outcome"] == "PASS" and r["reason"] is None
    assert set(r) == RECEIPT_KEYS
    assert set(r["checks"]) == set(p.CHECKS) and all(r["checks"].values())
    assert r["project_sha256"] == p.sha(PROJECT)
    assert r["conversation_id"] == CONVERSATION and r["message_id"] == MESSAGE
    assert r["content_sha256"] == p.sha(fake.created["content"])
    assert r["tasks_before"] == ["a" * 32] and r["tasks_after"] == ["b" * 32]
    assert r["task_definition"] == DEFINITION
    assert r["source_sha_before"] == r["source_sha_after"] == "c" * 40
    assert r["finished_at"] >= r["started_at"]
    assert len(fake.posts) == 3 and fake.restarts == 1
    assert not (tmp_path / "offline-1" / "receipt.json.tmp").exists()


def test_tail_missing_before(tmp_path):
    fake = Fake()
    fake.tail_defect = "missing_before"
    code, fake = invoke(tmp_path, fake)
    assert code == 1 and receipt(tmp_path)["reason"] == "tail_missing_before"
    assert fake.restarts == 0 and len(fake.posts) == 2


@pytest.mark.parametrize("field", ["content", "created_at"])
def test_tail_changed_after(tmp_path, field):
    fake = Fake()
    fake.tail_defect = field
    code, fake = invoke(tmp_path, fake)
    assert code == 1 and receipt(tmp_path)["reason"] == "tail_changed"
    assert len(fake.posts) == 2


@pytest.mark.parametrize("defect", ["task_definition", "unsteady"])
def test_service_drifted_before_restart_refused(tmp_path, monkeypatch, defect):
    fake = Fake()
    original_service = fake.service

    def service(environment):
        result = original_service(environment)
        if len(fake.posts) == 2:
            if defect == "task_definition":
                result["taskDefinition"] = DEFINITION.rsplit(":", 1)[0] + ":43"
            else:
                result["deployments"][0]["rolloutState"] = "IN_PROGRESS"
        return result

    monkeypatch.setattr(fake, "service", service)
    code, fake = invoke(tmp_path, fake)
    r = receipt(tmp_path)
    assert code == 2 and r["outcome"] == "REFUSED"
    assert r["reason"] == "service_drifted" and r["task_definition"] == DEFINITION
    assert not fake.force_calls and fake.restarts == 0
    assert len(fake.posts) == 2 and not r["checks"]["restarted"]


def test_restart_receives_recorded_task_definition(tmp_path):
    code, fake = invoke(tmp_path)
    assert code == 0
    assert fake.force_calls == [("staging", receipt(tmp_path)["task_definition"])]
    assert fake.force_calls[0][1] == DEFINITION


def test_restart_tasks_unchanged_times_out(tmp_path):
    fake = Fake()
    fake.unchanged_tasks = True
    code, fake = invoke(tmp_path, fake)
    assert code == 3 and receipt(tmp_path)["reason"] == "restart_unobserved"
    assert fake.clock == p.RESTART_TIMEOUT_S
    assert fake.restarts == 1 and len(fake.posts) == 2


def test_health_never_200_times_out(tmp_path):
    fake = Fake()
    fake.unhealthy = True
    code, fake = invoke(tmp_path, fake)
    assert code == 3 and receipt(tmp_path)["reason"] == "health_unobserved"
    assert fake.clock == p.HEALTH_TIMEOUT_S and len(fake.posts) == 2


def test_replay_new_id_fails(tmp_path):
    fake = Fake()
    fake.replay_new = True
    code, _ = invoke(tmp_path, fake)
    assert code == 1 and receipt(tmp_path)["reason"] == "replay_duplicated"


def test_token_never_exposed(tmp_path, capsys):
    code, _ = invoke(tmp_path)
    assert code == 0
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err
    assert TOKEN not in (tmp_path / "offline-1" / "receipt.json").read_text()
    assert p.main(["run", "--token", TOKEN], {}, Fake()) == 2
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err


def test_content_never_in_receipt(tmp_path):
    code, fake = invoke(tmp_path)
    assert code == 0
    text = (tmp_path / "offline-1" / "receipt.json").read_text()
    assert fake.created["content"] not in text and "0123456789abcdef" not in text


def test_write_exception_is_unknown_without_retry_or_secret(tmp_path, capsys):
    fake = Fake()
    fake.raise_write = True
    code, fake = invoke(tmp_path, fake)
    assert code == 3 and len(fake.posts) == 1 and not fake.restarts
    assert receipt(tmp_path)["reason"] == "write_unobserved"
    output = capsys.readouterr()
    assert TOKEN not in output.out + output.err + json.dumps(receipt(tmp_path))


@pytest.mark.parametrize("defect", ["duplicate", "gaps", "missing_after"])
def test_tail_duplicates_gaps_or_missing_after_fail(tmp_path, defect):
    fake = Fake()
    fake.tail_defect = defect
    code, _ = invoke(tmp_path, fake)
    assert code == 1
    assert receipt(tmp_path)["reason"] == (
        "tail_missing_after" if defect == "missing_after" else "tail_missing_before")


@pytest.mark.parametrize("run_id", ["../escape", "", "x" * 65])
def test_invalid_run_id_never_reserves(tmp_path, run_id, capsys):
    code, fake = invoke(tmp_path, **{"run-id": run_id})
    assert code == 2 and not fake.posts and not fake.saved
    assert json.loads(capsys.readouterr().out)["reason"] == "run_id_refused"


def test_gate_registration():
    root = Path(__file__).resolve().parents[2]
    catalog = (root / "scripts/run-all-gates.py").read_text()
    assert 'Suite("server-probe-conv-durable-restart"' in catalog
    assert '_py_pytest("tests/test_probe_conv_durable_restart.py"), 16)' in catalog
    selection = json.loads((root / "scripts/ci/test-selection-map.json").read_text())
    assert "server-probe-conv-durable-restart" in selection["mandatory_suite_ids"]


def test_live_http_disables_redirects_and_sends_bearer_and_key(monkeypatch):
    import urllib.request

    calls = []

    class Response:
        status = 201

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self):
            return b'{"message_id":"33333333-3333-4333-8333-333333333333"}'

    class Opener:
        def open(self, request, timeout):
            calls.append((request, timeout))
            return Response()

    def build(handler):
        assert isinstance(handler, urllib.request.HTTPRedirectHandler)
        assert handler.redirect_request(None, None, 302, "redirect", {},
                                        "https://foreign.example") is None
        return Opener()

    monkeypatch.setattr(urllib.request, "build_opener", build)
    adapter = p.LiveAdapters("staging")
    status, body = adapter.http("POST", "/test", TOKEN, {"content": "probe"}, "p058-key")
    assert status == 201 and body["message_id"] == MESSAGE
    assert len(calls) == 1
    request, timeout = calls[0]
    assert timeout == 15 and request.method == "POST"
    assert request.full_url == p.ENVIRONMENTS["staging"]["origin"] + "/test"
    headers = {name.lower(): value for name, value in request.header_items()}
    assert headers["authorization"] == "Bearer " + TOKEN
    assert headers["idempotency-key"] == "p058-key"
    assert json.loads(request.data) == {"content": "probe"}


def test_live_restart_retains_task_definition(monkeypatch):
    calls = []

    class ECS:
        def update_service(self, **kwargs):
            calls.append(kwargs)

    adapter = p.LiveAdapters("production")
    monkeypatch.setattr(adapter, "client", lambda name: ECS() if name == "ecs" else None)
    adapter.force_new_deployment("production", DEFINITION)
    assert calls == [{"cluster": "leaf-automation-production", "service": "leaf-platform",
                      "taskDefinition": DEFINITION, "forceNewDeployment": True}]
