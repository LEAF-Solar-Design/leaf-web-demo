"""Nine offline contracts for the paid APS ramp, using only an injected client."""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import socket
import sys
import threading
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import aps_concurrency_ramp as ramp


class FakeClient:
    ENGINE = "Autodesk.AutoCAD+26_0"

    def __init__(self, queue_s=2, engine_s=36, statuses=("success",), reject=False):
        self.queue_s = queue_s
        self.engine_s = engine_s
        self.statuses = statuses
        self.reject = reject
        self.calls = []
        self.arguments = []
        self.submit_threads = {}
        self.lock = threading.Lock()
        self.expected_env = None

    def activity_qualified(self, name):
        if self.expected_env:
            assert (os.environ["APS_MAX_CONCURRENCY"],
                    os.environ["APS_POLL_BUDGET_PER_MIN"]) == self.expected_env
        return "fake-owner." + name + "+prod"

    def run_nonce(self):
        self.calls.append("nonce")
        return "unique-nonce"

    def ephemeral_input_key(self, name, tenant_id, nonce):
        assert tenant_id == "aps-ramp" and nonce == "unique-nonce"
        return "t/aps-ramp/in/" + nonce + "_" + name

    def upload_object(self, path, key):
        assert Path(path).is_file() and key.startswith("t/aps-ramp/in/")
        self.calls.append("upload")

    def signed_download_url(self, key, minutes):
        assert minutes == 60
        self.calls.append("download-url")
        return "https://fake.invalid/input?X-Amz-Signature=download-secret"

    def signed_upload_url(self, key):
        self.calls.append(key)
        return "upload-key", "https://fake.invalid/" + key + "?X-Amz-Signature=output-secret"

    def submit_workitem(self, activity, arguments, poll, tenant_id):
        assert activity == self.activity_qualified(ramp.ACTIVITY)
        assert poll is False and tenant_id == "aps-ramp"
        assert arguments["Params"] == {"url": "data:application/json,{}"}
        assert arguments["HostDwg"]["verb"] == "get"
        assert arguments["Result"]["verb"] == "put"
        with self.lock:
            self.arguments.append(arguments)
            level = arguments["Result"]["url"].split("unique-nonce_")[1].split("_")[0]
            self.submit_threads.setdefault(level, set()).add(threading.get_ident())
            index = len(self.arguments)
        if self.reject:
            error = RuntimeError("must never persist this message or its signed URL")
            error.response = SimpleNamespace(status_code=429)
            raise error
        return {"id": "wi-" + str(index)}

    def _poll_workitem(self, identifier):
        index = int(identifier.split("-")[1]) - 1
        status = self.statuses[index % len(self.statuses)]
        base = datetime(2026, 10, 18, tzinfo=timezone.utc)
        offsets = (0, 1, self.queue_s, self.queue_s + self.engine_s,
                   self.queue_s + self.engine_s + 1)
        return {"id": identifier, "status": "pending" if status == "timeout" else status,
                "_timeout": status == "timeout",
                "reportUrl": "https://fake.invalid/report?X-Amz-Signature=report-secret",
                "stats": {k: (base + timedelta(seconds=s)).isoformat()
                          for k, s in zip(ramp.TIMES, offsets)}}


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline ramp tests must not use a network or real client")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(client=forbidden))
    monkeypatch.setattr(ramp, "importlib", SimpleNamespace(import_module=forbidden))
    monkeypatch.setenv("APS_CREDENTIALS_JSON", json.dumps(
        {"client_id": "offline-client-value", "client_secret": "offline-secret-value"}))
    monkeypatch.setenv("APS_CRED", str(tmp_path / "missing-creds"))
    monkeypatch.setenv("APS_USD_PER_HR", "10")
    monkeypatch.setenv("APS_MAX_CONCURRENCY", "1")
    monkeypatch.setenv("APS_POLL_BUDGET_PER_MIN", "1")


def live(tmp_path, client, *flags):
    dwg = tmp_path / "input.dwg"
    dwg.write_bytes(b"offline fixture")
    out = tmp_path / "receipt.json"
    code = ramp.main(["--live", "--dwg", str(dwg), "--out", str(out),
                      "--levels", "1,2,4", *flags], client_module=client, now=lambda: 42.)
    return code, json.loads(out.read_text()) if out.exists() else None


def test_dry_run_makes_no_client_calls_and_reports_the_plan(capsys, tmp_path):
    client = FakeClient()
    assert ramp.main([], client_module=client) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan == {"levels": [1, 2, 4, 8, 16, 32], "total_workitems": 63,
                    "estimated_cost_usd": 63 * .008,
                    "activity_id": "fake-owner.LeafTool_count_by_layer+prod"}
    assert client.calls == [] and client.arguments == []
    assert ramp.main(["--out", str(tmp_path / "plan.json")]) == 0
    assert json.loads(capsys.readouterr().out)["activity_id"].startswith("<nickname-placeholder>")
    for flags in (["--levels", "2,1"], ["--levels", "1,1"], ["--levels", "0"],
                  ["--levels", "101"], ["--levels", "1,"], ["--spend-cap-usd", "nan"],
                  ["--spend-cap-usd", "51"], ["--spend-cap-usd", "0"],
                  ["--stop-error-rate", "0"], ["--stop-error-rate", "1.1"],
                  ["--stop-queue-p95-s", "inf"], ["--poll-budget-per-min", "121"],
                  ["--poll-budget-per-min", "0"], ["--live"]):
        assert ramp.main(flags, client_module=client) == 2
    assert client.arguments == []


def test_live_refuses_without_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv("APS_CREDENTIALS_JSON")
    client = FakeClient()
    code, receipt = live(tmp_path, client)
    assert code == 3 and receipt is None
    assert client.calls == []
    monkeypatch.setenv("APS_CREDENTIALS_JSON", "malformed")
    assert live(tmp_path, client) == (2, None)


def test_sets_concurrency_and_poll_budget_env_before_client_use(tmp_path):
    client = FakeClient()
    client.expected_env = ("4", "37")
    assert live(tmp_path, client, "--poll-budget-per-min", "37")[0] == 0
    assert os.environ["APS_MAX_CONCURRENCY"] == "4"
    assert os.environ["APS_POLL_BUDGET_PER_MIN"] == "37"


def test_completed_ramp_summarises_every_level(tmp_path):
    client = FakeClient()
    code, receipt = live(tmp_path, client)
    assert code == 0 and receipt["stop_reason"] == "completed"
    assert receipt["schema"] == "leaf.aps-concurrency-ramp.v1"
    assert receipt["engine"] == client.ENGINE
    assert receipt["levels_requested"] == [1, 2, 4]
    assert receipt["cumulative_usd_est"] == pytest.approx(.7)
    assert receipt["started_at"] == receipt["ended_at"]
    assert receipt["spend_cap_usd"] == 10 and receipt["poll_budget_per_min"] == 60
    for n, level in zip((1, 2, 4), receipt["levels"]):
        assert level["n"] == level["submitted"] == level["succeeded"] == n
        assert level["failed"] == level["poll_timeouts"] == 0
        assert level["submit_errors"] == []
        assert level["queue_s"] == {"p50": 2, "p95": 2, "max": 2}
        assert level["total_s"] == {"p50": 39, "p95": 39}
        assert level["engine_s"] == 36 * n and level["usd_est"] == pytest.approx(.1 * n)
        for row in level["workitems"]:
            assert (row["queue_s"], row["engine_s"], row["total_s"]) == (2, 36, 39)
            assert row["usd_est"] == pytest.approx(.1)
            assert row["submit_wall_s"] == 0
    assert client.calls.count("upload") == client.calls.count("download-url") == 1
    assert len({a["HostDwg"]["url"] for a in client.arguments}) == 1
    assert len({a["Result"]["url"] for a in client.arguments}) == 7
    assert {k: len(v) for k, v in client.submit_threads.items()} == {"1": 1, "2": 2, "4": 4}
    missing = ramp._timing({"stats": {}}, 10)
    assert all(v is None for v in missing.values())
    assert ramp._percentile([0, 10], .95) == pytest.approx(9.5)


def test_stops_on_submit_429(tmp_path):
    client = FakeClient(reject=True)
    code, receipt = live(tmp_path, client)
    assert code == 0 and receipt["stop_reason"] == "submit_error"
    assert len(client.arguments) == len(receipt["levels"]) == 1
    assert receipt["levels"][0]["submit_errors"] == [429]
    assert receipt["levels"][0]["submitted"] == 0


def test_stops_on_error_rate(tmp_path):
    client = FakeClient(statuses=("failedInstructions", "timeout"))
    code, receipt = live(tmp_path, client, "--levels", "2,4")
    assert code == 0 and receipt["stop_reason"] == "error_rate"
    assert len(client.arguments) == 2 and len(receipt["levels"]) == 1
    assert receipt["levels"][0]["failed"] == receipt["levels"][0]["poll_timeouts"] == 1


def test_stops_on_queue_p95(tmp_path):
    client = FakeClient(queue_s=301)
    code, receipt = live(tmp_path, client)
    assert code == 0 and receipt["stop_reason"] == "queue_p95"
    assert receipt["levels"][0]["queue_s"]["p95"] == 301
    assert len(client.arguments) == 1


def test_stops_on_projected_spend_cap(tmp_path):
    client = FakeClient()
    code, receipt = live(tmp_path, client, "--spend-cap-usd", ".15")
    assert code == 0 and receipt["stop_reason"] == "spend_cap"
    assert receipt["cumulative_usd_est"] == pytest.approx(.1)
    assert len(client.arguments) == 1


def test_receipt_never_contains_urls_or_secrets(tmp_path, capsys):
    client = FakeClient()
    code, receipt = live(tmp_path, client)
    assert code == 0 and receipt is not None
    raw = (tmp_path / "receipt.json").read_text()
    for marker in ("X-Amz-", "Signature=", "AWSAccessKeyId", "https://", "client_secret",
                   "offline-client-value", "offline-secret-value", "upload-key", "reportUrl"):
        assert marker not in raw
        assert marker not in capsys.readouterr().out
    for marker in ("client_secret", "X-Amz-", "Signature=", "AWSAccessKeyId"):
        target = tmp_path / ("guard-" + str(len(marker)))
        assert ramp._write(target, {"status": marker}) is False
        assert not target.exists()
    def broken_poll(identifier):
        raise RuntimeError("offline-secret-value https://fake.invalid/?X-Amz-Secret")
    client._poll_workitem = broken_poll
    code, partial = live(tmp_path, client)
    assert code == 1 and partial["stop_reason"] == "unexpected_error"
    assert partial["levels"][0]["submitted"] == 1
    assert "offline-secret-value" not in (tmp_path / "receipt.json").read_text()
