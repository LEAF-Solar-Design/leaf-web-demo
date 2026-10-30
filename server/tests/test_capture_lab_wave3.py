"""Offline producer/consumer contract: no application, network or database."""
import base64
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

import pytest

import capture_lab_wave3 as c


SHA, KEY, DIGEST = "a" * 40, "b" * 32, "c" * 64
STAMP = "2026-10-28T12:00:00Z"
DOC = "12345678-1234-1234-1234-123456789abc"
PRIVATE = "tenant-private reader@example.invalid eyJhbGciOiJIUzI1NiJ9.eyJ0ZXN0Ijp0cnVlfQ.abcdefghijk"
ROOT = Path(__file__).resolve().parents[2]


def meta(name):
    return c.common(name, SHA, KEY, STAMP)


def source_replay():
    return dict(status="complete", watermark={DOC: 2}, published={DOC: 2},
                last_commit="d" * 40, tenant=PRIVATE, schema="leaf.lab-replay.v1")


def head():
    return dict(revision=2, published_revision=2, sha256=DIGEST,
                tenant_id=PRIVATE, author=PRIVATE, canonical_text=PRIVATE)


def run_row(run_id="run-1", state="finished"):
    return dict(run_id=run_id, graph_revision=2, graph_sha256=DIGEST, state=state,
                tenant_id=PRIVATE)


def jobs(run_id="run-1"):
    return [dict(status="complete", idempotency_key=f"lab:{run_id}:tool-a:1", tenant_id=PRIVATE)]


def activation():
    return dict(activation_id="activation-1", doc_id=DOC, revision=2, graph_sha256=DIGEST,
                state="active", created_at="2026-10-28T10:00:00+00:00",
                next_due_at="2026-10-29T12:00:00+00:00", tenant_id=PRIVATE,
                transitions=[dict(to_state="active", reason="created", at="2026-10-28T10:00:00Z")])


class Clock:
    def __init__(self):
        self.elapsed = 0

    def sleep(self, seconds):
        self.elapsed += seconds

    def monotonic(self):
        return self.elapsed

    def now(self):
        return datetime(2026, 10, 28, 12, tzinfo=timezone.utc)


class FakeDB:
    def __init__(self):
        self.reads = []

    def replay(self, scope):
        self.reads.append(("replay", scope))
        return source_replay()

    def publication_count(self, scope):
        self.reads.append(("count", scope))
        return 1

    def jobs(self, scope, run_id):
        self.reads.append(("jobs", scope, run_id))
        return jobs(run_id)


class FakeHTTP:
    def __init__(self):
        self.calls = []
        self.paused = False
        self.fail_resume = False
        self.rows = {}
        self.activation = activation()
        self.event_pages = 0

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        if path.endswith("/head") or "/revisions/" in path:
            return head()
        if path.endswith("/publish"):
            assert body == {"expected_head": 2}
            return head()
        if path == "/api/lab/runs/worker/pause":
            assert body == {"reason": "wave3 capture"}
            self.paused = True
            return {}
        if path == "/api/lab/runs/worker/resume":
            if self.fail_resume:
                raise RuntimeError(PRIVATE)
            self.paused = False
            return {}
        if path == "/api/lab/runs":
            assert body == {"doc_id": DOC, "graph_revision": 2}
            rid = f"run-{len(self.rows) + 1}"
            self.rows[rid] = run_row(rid)
            return {"run_id": rid, "state": "queued"}
        if "/events?" in path:
            self.event_pages += 1
            return {"events": [{"seq": 1, "payload": PRIVATE}]} if "after_seq=0&" in path else {"events": []}
        if path.startswith("/api/lab/runs/"):
            rid = path.split("/")[4]
            if path.endswith("/cancel"):
                self.rows[rid]["state"] = "cancelled"
                return {}
            result = dict(self.rows[rid])
            if self.paused:
                result["state"] = "queued"
            return result
        if path == "/api/lab/activations":
            if method == "POST":
                assert body == {"doc_id": DOC, "interval_s": 60}
                return copy.deepcopy(self.activation)
            return {"activations": [copy.deepcopy(self.activation)]}
        if path.endswith("/pause"):
            self.activation["state"] = "paused"
            return copy.deepcopy(self.activation)
        if path.endswith("/resume"):
            self.activation["state"] = "active"
            return copy.deepcopy(self.activation)
        raise AssertionError(path)


def fake_capture():
    clock, http, db = Clock(), FakeHTTP(), FakeDB()
    capture = c.Capture(http, db, PRIVATE, now=clock.now,
                        monotonic=clock.monotonic, sleep=clock.sleep)
    return capture, http, db, clock


def argv(name, directory, *extra):
    return [name, "--sha", SHA, "--proof-key", KEY, "--doc", DOC,
            "--out-dir", str(directory), *extra]


def activation_flags():
    return ["--editor-closed-at", "2026-10-28T10:01:00Z",
            "--observed-at", "2026-10-28T10:02:00Z", "--widget-rendered"]


def reviewed_flags():
    return [flag for key in c.IDS if key not in c.LIVE for flag in ("--reviewed", key)]


def relay_flags():
    return ["--ops-sha", "e" * 40, "--list-ok", "--detail-ok", "--intent-by-id-ok",
            "--opaque-handles-only", "--caps-held", "--fallback-policy-ok"]


def test_projection_successes():
    replay = c.replay_receipt(meta("replay"), source_replay(), DOC)
    assert replay["revision"] == replay["publishedRevision"] == 2
    assert replay["headCommit"] == "d" * 40 and replay["divergent"] is False
    ledger = c.ledger_receipt(meta("ledger"), head(), head(), 1)
    assert ledger["digest"] == DIGEST and ledger["publicationCount"] == 1
    run = c.run_receipt(meta("run"), run_row(), head(), jobs() + jobs())
    assert (run["runState"], run["brokerState"], run["toolNodeCount"], run["brokerDigest"]) == (
        "succeeded", "succeeded", 1, DIGEST)
    active = c.activation_receipt(meta("activation"), activation(), 1,
                                  "2026-10-28T10:01:00Z", "2026-10-28T10:02:00Z", True)
    assert active["activatedAt"] == "2026-10-28T10:00:00Z" and active["widgetSurface"] == "/mini"
    relay = c.relay_receipt(meta("relay"), "e" * 40, dict.fromkeys(c.RELAY, True))
    assert all(relay[k] is True for k in c.RELAY)
    # SEL-316e3 requires explicit review of every documented control.
    rollback = c.rollback_receipt(meta("rollback"), dict.fromkeys(c.LIVE, (True, True)), set(c.IDS) - c.LIVE)
    assert [x["id"] for x in rollback["controls"]] == list(c.IDS)
    assert len(rollback["controls"]) == 10
    assert sum(x["method"] == "live" for x in rollback["controls"]) == 3


@pytest.mark.parametrize("source,code", [
    (None, "replay_absent"), ({"status": "in_progress"}, "replay_not_complete"),
    ({"status": "divergent"}, "replay_not_complete"),
    ({"status": "complete", "watermark": {}}, "replay_doc_absent"),
])
def test_replay_refusals(source, code):
    with pytest.raises(c.Refused, match=code):
        c.replay_receipt(meta("replay"), source, DOC)


@pytest.mark.parametrize("state,source_jobs,code", [
    ("failed", jobs(), "run_not_finished"), ("cancelled", jobs(), "run_not_finished"),
    ("finished", [], "no_broker_jobs"),
    ("finished", [dict(jobs()[0], status="failed")], "broker_incomplete"),
])
def test_run_mapping_refusals(state, source_jobs, code):
    with pytest.raises(c.Refused, match=code):
        c.run_receipt(meta("run"), run_row(state=state), head(), source_jobs)


@pytest.mark.parametrize("digest", [None, "A" * 64, "c" * 63])
def test_digest_invalid(digest):
    with pytest.raises(c.Refused, match="digest_invalid"):
        c.ledger_receipt(meta("ledger"), head(), dict(head(), sha256=digest), 1)


@pytest.mark.parametrize("mutation,closed,observed,widget,code", [
    ("absent", "2026-10-28T10:01:00Z", "2026-10-28T10:02:00Z", True, "activation_absent"),
    ("transition", "2026-10-28T10:01:00Z", "2026-10-28T10:02:00Z", True, "activation_time_unknown"),
    ("ok", "2026-10-28T09:59:00Z", "2026-10-28T10:02:00Z", True, "time_order"),
    ("ok", "2026-10-28T10:01:00Z", "2026-10-28T10:01:00Z", True, "time_order"),
    ("ok", None, "2026-10-28T10:01:00Z", True, "time_order"),
    ("ok", "2026-10-28T10:01:00Z", "2026-10-28T10:02:00Z", False, "widget_unobserved"),
])
def test_activation_refusals(mutation, closed, observed, widget, code):
    row = activation()
    if mutation == "transition":
        row["transitions"] = []
    with pytest.raises(c.Refused, match=code):
        c.activation_receipt(meta("activation"), None if mutation == "absent" else row,
                             1, closed, observed, widget)


@pytest.mark.parametrize("missing", list(c.RELAY))
def test_relay_requires_each_observation(missing):
    observations = dict.fromkeys(c.RELAY, True)
    del observations[missing]
    with pytest.raises(c.Refused, match="relay_unobserved"):
        c.relay_receipt(meta("relay"), "e" * 40, observations)


@pytest.mark.parametrize("control", [key for key in c.IDS if key in c.LIVE])
def test_rollback_projection_requires_recovery(control):
    observations = dict.fromkeys(c.LIVE, (True, True))
    observations[control] = (True, False)
    with pytest.raises(c.Refused, match=control):
        # SEL-316e3 attestation allows this test to reach live recovery validation.
        c.rollback_receipt(meta("rollback"), observations, set(c.IDS) - c.LIVE)


@pytest.mark.parametrize("missing", [key for key in c.IDS if key not in c.LIVE])
def test_rollback_requires_each_documented_review(tmp_path, capsys, missing):
    reviewed = [key for key in c.IDS if key not in c.LIVE and key != missing]
    flags = [flag for key in reviewed for flag in ("--reviewed", key)]
    capture, http, db, _ = fake_capture()
    assert c.main(argv("rollback", tmp_path, *flags), capture=capture) == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {"outcome": "refused", "reason": "documented_unreviewed"}
    assert output.err == ""
    assert not http.calls and not db.reads and not list(tmp_path.iterdir())
    with pytest.raises(c.Refused, match="^documented_unreviewed$"):
        c.rollback_receipt(meta("rollback"), dict.fromkeys(c.LIVE, (True, True)), reviewed)


@pytest.mark.parametrize("unknown", ["worker-pause", "run-cancel", "activation-pause", "unknown"])
def test_rollback_refuses_unknown_review(tmp_path, capsys, unknown):
    capture, http, db, _ = fake_capture()
    assert c.main(argv("rollback", tmp_path, *reviewed_flags(), "--reviewed", unknown), capture=capture) == 1
    assert json.loads(capsys.readouterr().out) == {"outcome": "refused", "reason": "reviewed_unknown"}
    assert not http.calls and not db.reads and not list(tmp_path.iterdir())
    with pytest.raises(c.Refused, match="^reviewed_unknown$"):
        c.rollback_receipt(meta("rollback"), dict.fromkeys(c.LIVE, (True, True)), (set(c.IDS) - c.LIVE) | {unknown})


def test_live_rollback_and_resume_failure(tmp_path, capsys):
    capture, http, db, clock = fake_capture()
    # SEL-316e3 requires all seven reviews before any live exercise.
    result = capture.capture(c.parser().parse_args(argv("rollback", tmp_path, *reviewed_flags())))
    assert [x["id"] for x in result["controls"]] == list(c.IDS)
    assert result["controls"] == [dict(id=key, method="live" if key in c.LIVE else "documented",
                                       exercised=key in c.LIVE, restored=key in c.LIVE) for key in c.IDS]
    assert clock.elapsed == 30
    assert not http.paused and http.activation["state"] == "active"
    assert len(http.rows) == 3
    capture, http, _, _ = fake_capture()
    http.fail_resume = True
    assert c.main(argv("rollback", tmp_path, *reviewed_flags()), capture=capture) == 1
    assert json.loads(capsys.readouterr().out) == {"outcome": "refused", "reason": "worker-pause"}
    assert any(path.endswith("/worker/resume") for _, path, _ in http.calls)
    assert not (tmp_path / "rollback.json").exists()


def test_timeout_and_cancel_without_receipt(tmp_path, capsys):
    capture, http, _, clock = fake_capture()
    http.paused = True
    with pytest.raises(c.Refused, match="run_timeout"):
        capture.capture(c.parser().parse_args(argv("run", tmp_path, "--timeout", "10")))
    assert clock.elapsed == 10
    http.paused = False
    assert c.main(argv("run", tmp_path, "--cancel"), capture=capture) == 0
    assert json.loads(capsys.readouterr().out) == {"outcome": "cancelled"}
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("state", [None, "unknown", "failed", "cancelled"])
def test_worker_recovery_requires_observed_success_state(state):
    capture, http, _, _ = fake_capture()
    original = http.request

    def recovery(method, path, body=None):
        result = original(method, path, body)
        if method == "GET" and path.startswith("/api/lab/runs/") and not http.paused:
            return dict(result, state=state)
        return result

    http.request = recovery
    with pytest.raises(c.Refused, match="worker-pause"):
        capture.rollback(DOC, 10)
    assert any(path.endswith("/worker/resume") for _, path, _ in http.calls)
    assert not http.paused


def test_poll_refuses_another_runs_observation():
    capture, http, _, _ = fake_capture()
    http.request = lambda *args: run_row("different-run")
    with pytest.raises(c.Refused, match="run_identity_invalid"):
        capture.poll("run-1", 10)


@pytest.mark.parametrize("flags,code", [
    ([], "widget_unobserved"),
    (["--widget-rendered"], "time_order"),
    (["--widget-rendered", "--editor-closed-at", "2026-10-28T10:02:00Z",
      "--observed-at", "2026-10-28T10:01:00Z"], "time_order"),
])
def test_activation_observations_required_before_creation(tmp_path, capsys, flags, code):
    capture, http, db, _ = fake_capture()
    # SEL-316e3 requires observations only on the separate capture path.
    assert c.main(argv("activation", tmp_path, *flags), capture=capture) == 1
    assert json.loads(capsys.readouterr().out) == {"outcome": "refused", "reason": code}
    assert not http.calls and not db.reads
    assert not list(tmp_path.iterdir())


def test_activation_create_then_capture(tmp_path, capsys):
    capture, http, db, clock = fake_capture()
    created = clock.now().isoformat().replace("+00:00", "Z")
    http.activation["created_at"] = created
    http.activation["transitions"][0]["at"] = created
    assert c.main(argv("activation", tmp_path, "--create"), capture=capture) == 0
    assert json.loads(capsys.readouterr().out) == {"outcome": "activation_created"}
    assert http.calls == [("POST", "/api/lab/activations", {"doc_id": DOC, "interval_s": 60})]
    assert not db.reads and not list(tmp_path.iterdir())
    http.calls.clear()
    capture.now = lambda: datetime(2026, 10, 28, 12, 3, tzinfo=timezone.utc)
    flags = ["--editor-closed-at", "2026-10-28T12:01:00Z",
             "--observed-at", "2026-10-28T12:02:00Z", "--widget-rendered"]
    assert c.main(argv("activation", tmp_path, *flags), capture=capture) == 0
    assert json.loads(capsys.readouterr().out) == {"outcome": "captured", "receipt": "activation"}
    assert http.calls == [("GET", "/api/lab/activations", None)]
    receipt = json.loads((tmp_path / "activation.json").read_text())
    assert receipt["activatedAt"] == created
    assert receipt["editorClosedAt"] == "2026-10-28T12:01:00Z"
    assert receipt["observedAt"] == "2026-10-28T12:02:00Z"


@pytest.mark.parametrize("flags", [
    ["--editor-closed-at", "2026-10-28T12:01:00Z"],
    ["--observed-at", "2026-10-28T12:02:00Z"],
    ["--widget-rendered"], activation_flags(),
])
def test_activation_create_refuses_observation_options(tmp_path, capsys, flags):
    capture, http, db, _ = fake_capture()
    assert c.main(argv("activation", tmp_path, "--create", *flags), capture=capture) == 1
    assert json.loads(capsys.readouterr().out) == {"outcome": "refused", "reason": "usage"}
    assert not http.calls and not db.reads and not list(tmp_path.iterdir())


@pytest.mark.parametrize("sample", [DOC, "tenant-private", "subject", "nonce", "approval_id",
                                      "client_request_id", "reader@example.invalid",
                                      "eyJhbGciOiJIUzI1NiJ9.eyJ0ZXN0Ijp0cnVlfQ.abcdefghijk"])
def test_privacy_before_any_write(tmp_path, sample):
    receipt = dict(meta("replay"), extra=sample)
    with pytest.raises(c.Refused, match="privacy"):
        c.write_receipt(tmp_path, "replay", receipt)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("method,path", [("GET", "/api/lab/docs/a/../head"),
    ("POST", "/api/lab/runs/x/events"), ("GET", "https://evil.invalid/api/lab/activations"),
    ("GET", "/api/lab/runs/x/events?after_seq=0&limit=501")])
def test_path_refused(method, path):
    with pytest.raises(c.Refused, match="path_refused"):
        c.allowed_path(method, path)


def test_origin_refused():
    with pytest.raises(c.Refused, match="origin_refused"):
        c.HTTP("private", "https://evil.invalid")
    with pytest.raises(c.Refused, match="origin_refused"):
        c.NoRedirect().redirect_request(None)


def test_http_bounds(monkeypatch):
    seen = []

    class Response:
        status = 200
        raw = b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            seen.append(size)
            return self.raw

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == c.ORIGIN + "/api/lab/activations"
            assert timeout == 30
            return Response()

    http = c.HTTP("private")
    http.opener = Opener()
    assert http.request("GET", "/api/lab/activations") == {}
    assert seen == [1024 * 1024 + 1]
    Response.raw = b" " * (1024 * 1024 + 1)
    with pytest.raises(c.Refused, match="response_too_large"):
        http.request("GET", "/api/lab/activations")


def test_database_queries_are_scoped_selects(monkeypatch):
    db = c.Database("fake")
    seen = []

    def read(sql, params):
        seen.append((sql, params))
        if "receipt FROM" in sql:
            return [{"receipt": json.dumps(source_replay())}]
        if "COUNT" in sql:
            return [{"count": 1}]
        return jobs()

    monkeypatch.setattr(db, "_read", read)
    assert db.replay(PRIVATE) == source_replay()
    assert db.publication_count(PRIVATE) == 1
    assert db.jobs(PRIVATE, "run-1") == jobs()
    assert len(seen) == 3
    for sql, params in seen:
        assert sql.startswith("SELECT ") and "tenant_id = %s" in sql
        assert PRIVATE not in sql and params[0] == PRIVATE
    assert seen[-1][1][2] == "lab:run-1:"


def test_database_jobs_reads_async_jobs(monkeypatch):
    seen = []

    def read(self, statement, parameters):
        seen.append((statement, parameters))
        return []

    monkeypatch.setattr(c.Database, "_read", read)
    c.Database("postgresql://unused").jobs("tenant-x", "run-1")

    assert len(seen) == 1
    statement, parameters = seen[0]
    assert "FROM async_jobs WHERE tenant_id = %s" in statement
    assert " FROM jobs " not in statement
    assert parameters == ("tenant-x", len("lab:run-1:"), "lab:run-1:")


def test_credentials_environment_routing_only():
    payload = base64.urlsafe_b64encode(json.dumps({"https://leafdesign.ai/tenant_id": DOC}).encode()).decode().rstrip("=")
    token = "e30." + payload + ".c2ln"
    assert c.credentials({"LEAF_E2E_STAGING_JWT": token}, "unused") == (token, DOC)
    with pytest.raises(c.Refused, match="credentials_invalid"):
        c.credentials({"LEAF_E2E_STAGING_JWT": PRIVATE}, "unused")


def test_arguments_and_errors_are_sanitized(tmp_path, capsys):
    assert c.main([PRIVATE]) == 1
    assert json.loads(capsys.readouterr().out) == {"outcome": "refused", "reason": "arguments_invalid"}
    capture, http, db, _ = fake_capture()
    for flag, value, code in [("--sha", "A" * 40, "sha_invalid"),
                               ("--proof-key", "x", "proof_key_invalid"),
                               ("--timeout", "1801", "timeout_invalid")]:
        arguments = argv("replay", tmp_path)
        if flag in arguments:
            arguments[arguments.index(flag) + 1] = value
        else:
            arguments += [flag, value]
        assert c.main(arguments, capture=capture) == 1
        assert json.loads(capsys.readouterr().out)["reason"] == code
    assert not http.calls and not db.reads
    assert not list(tmp_path.iterdir())


def test_events_are_bounded_and_activation_newest(tmp_path):
    capture, http, _, _ = fake_capture()
    original = http.request

    def endless(method, path, body=None):
        if "/events?" in path:
            after = int(path.split("after_seq=")[1].split("&")[0])
            return {"events": [{"seq": after + 1}]}
        return original(method, path, body)

    http.request = endless
    with pytest.raises(c.Refused, match="events_incomplete"):
        capture.events("run-1")
    old = dict(activation(), activation_id="old", created_at="2026-10-27T10:00:00Z")
    http.request = lambda *args: {"activations": [old, activation()]}
    assert capture.active(DOC)["activation_id"] == "activation-1"
    http.request = lambda *args: {"activations": []}
    with pytest.raises(c.Refused, match="activation_absent"):
        capture.active(DOC)


@pytest.mark.parametrize("control", ["worker-pause", "run-cancel", "activation-pause"])
def test_failed_live_controls_refuse_and_attempt_recovery(tmp_path, capsys, control):
    capture, http, _, _ = fake_capture()
    original = http.request

    def failing(method, path, body=None):
        if control == "worker-pause" and path.endswith("/worker/pause"):
            raise RuntimeError(PRIVATE)
        if control == "run-cancel" and path.endswith("/cancel"):
            raise RuntimeError(PRIVATE)
        if control == "activation-pause" and path.endswith("/activation-1/pause"):
            raise RuntimeError(PRIVATE)
        return original(method, path, body)

    http.request = failing
    # SEL-316e3 reviews must precede the live failure under test.
    assert c.main(argv("rollback", tmp_path, *reviewed_flags()), capture=capture) == 1
    output = capsys.readouterr()
    assert json.loads(output.out) == {"outcome": "refused", "reason": control}
    assert output.err == "" and PRIVATE not in output.out
    if control == "worker-pause":
        assert any(path.endswith("/worker/resume") for _, path, _ in http.calls)
    if control == "activation-pause":
        assert any(path.endswith("/activation-1/resume") for _, path, _ in http.calls)
    assert not list(tmp_path.iterdir())


def test_exact_fractional_activation_times_and_null_publication():
    row = activation()
    row["transitions"][0]["at"] = "2026-10-28T10:00:00.00001Z"
    receipt = c.activation_receipt(meta("activation"), row, 0,
                                    "2026-10-28T10:00:00.00001Z", "2026-10-28T10:00:00.00002Z", True)
    assert receipt["activatedAt"].endswith(".00001Z")
    replay = source_replay()
    del replay["published"]
    assert c.replay_receipt(meta("replay"), replay, DOC)["publishedRevision"] is None
    assert c.ledger_receipt(meta("ledger"), dict(head(), published_revision=None), head(), 0)["publishedRevision"] is None


def test_end_to_end_checker_and_privacy(tmp_path, capsys):
    capture, http, db, clock = fake_capture()
    # SEL-316e3 captures an existing activation and attests to documented controls.
    extra = {"ledger": ["--publish"], "activation": activation_flags(),
             "relay": relay_flags(), "rollback": reviewed_flags()}
    for name in ("replay", "ledger", "run", "activation", "relay", "rollback"):
        assert c.main(argv(name, tmp_path, *extra.get(name, [])), capture=capture) == 0
        printed = capsys.readouterr()
        assert printed.err == ""
        assert json.loads(printed.out) == {"outcome": "captured", "receipt": name}
        raw = (tmp_path / f"{name}.json").read_text(encoding="utf-8")
        for private in (PRIVATE, DOC, "reader@example.invalid", "tenant", "eyJhbGci"):
            assert private not in raw and private not in printed.out
        receipt = json.loads(raw)
        assert receipt["sha"] == SHA and receipt["proofKey"] == KEY
    assert http.event_pages == 2
    assert sum(kind == "count" for kind, *_ in db.reads) == 2
    assert len(list(tmp_path.iterdir())) == 6
    command = ["node", "scripts/verify-lab-wave3.mjs"]
    # Missing node is a failure, never a skip. These real consumer checks are
    # deliberately in the same test as fake-backed production CLI captures.
    result = subprocess.run(command + ["--receipts", str(tmp_path), "--require-receipts"],
                            cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["verdict"] == "pass"
    path = tmp_path / "run.json"
    bad = json.loads(path.read_text())
    bad["digest"] = "f" * 64
    path.write_text(json.dumps(bad), encoding="utf-8")
    result = subprocess.run(command + ["--receipts", str(tmp_path), "--require-receipts"],
                            cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 1
    result = subprocess.run(command + ["--self-test"], cwd=ROOT,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["verdict"] == "pass"
