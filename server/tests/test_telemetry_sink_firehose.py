"""TEL-6: telemetry_sink Firehose backend (GCP deprecation, platform half).

Tier 1 (acceptance_oracle): live-verifying the staging bucket write needs AWS
access this builder does not have at build time, so this file proves the
UNIT layer by mutation -- backend selection, disabled-reason gating, row
shape parity with the BigQuery path, PutRecordBatch batching/partial-failure
handling, and the never-raise/kill-switch contract -- and hands the live
probe (staging bucket receives objects under events/; the ops-dashboard
/aws-fleet buckets panel flips from Never written) back as a named
operator/train step. No test talks to real AWS.
"""
from __future__ import annotations

import json
import sys
import types

import pytest

import telemetry_sink


@pytest.fixture(autouse=True)
def _reset_sink(monkeypatch):
    with telemetry_sink._wake:
        telemetry_sink._queue.clear()
        for k in telemetry_sink._stats:
            telemetry_sink._stats[k] = 0
    telemetry_sink._created_tables.clear()
    telemetry_sink._noted.clear()
    telemetry_sink._sdk_checked = None
    telemetry_sink._firehose_sdk_checked = None
    telemetry_sink._client = None
    telemetry_sink._firehose_client = None
    yield


def _enable_fake_sink(monkeypatch):
    monkeypatch.setattr(telemetry_sink, "disabled_reason", lambda: None)
    monkeypatch.setattr(telemetry_sink, "_ensure_flusher", lambda: None)


# --------------------------------------------------------------------------- #
# backend() selection: fail-closed default, env override
# --------------------------------------------------------------------------- #

def test_backend_defaults_to_bigquery_when_unset(monkeypatch):
    monkeypatch.delenv("LEAF_TELEMETRY_BACKEND", raising=False)
    assert telemetry_sink.backend() == "bigquery"


@pytest.mark.parametrize("value,expected", [
    ("firehose", "firehose"),
    ("FIREHOSE", "firehose"),
    ("  dual  ", "dual"),
    ("bigquery", "bigquery"),
])
def test_backend_env_selects_case_and_whitespace_insensitively(monkeypatch, value, expected):
    monkeypatch.setenv("LEAF_TELEMETRY_BACKEND", value)
    assert telemetry_sink.backend() == expected


def test_backend_invalid_value_fails_closed_to_bigquery(monkeypatch):
    monkeypatch.setenv("LEAF_TELEMETRY_BACKEND", "s3-direct-nonsense")
    assert telemetry_sink.backend() == "bigquery"


# --------------------------------------------------------------------------- #
# firehose_stream() naming
# --------------------------------------------------------------------------- #

def test_firehose_stream_name_defaults_to_env_suffixed(monkeypatch):
    monkeypatch.delenv("LEAF_TELEMETRY_FIREHOSE_STREAM", raising=False)
    monkeypatch.setenv("LEAF_METRICS_ENVIRONMENT", "staging")
    assert telemetry_sink.firehose_stream() == "leaf-telemetry-events-staging"


def test_firehose_stream_name_defaults_to_unknown_without_environment(monkeypatch):
    monkeypatch.delenv("LEAF_TELEMETRY_FIREHOSE_STREAM", raising=False)
    monkeypatch.delenv("LEAF_METRICS_ENVIRONMENT", raising=False)
    assert telemetry_sink.firehose_stream() == "leaf-telemetry-events-unknown"


def test_firehose_stream_name_override_env_wins(monkeypatch):
    monkeypatch.setenv("LEAF_METRICS_ENVIRONMENT", "staging")
    monkeypatch.setenv("LEAF_TELEMETRY_FIREHOSE_STREAM", "custom-stream-name")
    assert telemetry_sink.firehose_stream() == "custom-stream-name"


# --------------------------------------------------------------------------- #
# disabled_reason(): kill switch, per-backend gating, dual fail-closed
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("backend_value", ["bigquery", "firehose", "dual"])
def test_kill_switch_wins_for_every_backend(monkeypatch, backend_value):
    monkeypatch.setenv("LEAF_TELEMETRY_BACKEND", backend_value)
    monkeypatch.setenv("LEAF_TELEMETRY_DISABLED", "1")
    assert "kill switch" in (telemetry_sink.disabled_reason() or "")


def test_disabled_reason_bigquery_default_env_is_honest(monkeypatch):
    # Default test env: no GCP creds -> honest disabled reason (unchanged
    # pre-TEL-6 behaviour for the default backend).
    monkeypatch.delenv("LEAF_TELEMETRY_BACKEND", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    reason = telemetry_sink.disabled_reason()
    assert reason is not None
    assert "GOOGLE_APPLICATION_CREDENTIALS" in reason


def test_disabled_reason_firehose_enabled_when_boto3_importable(monkeypatch):
    # boto3 is a hard server/ dependency, so with backend=firehose and no
    # kill switch the sink is honestly enabled without any GCP creds at all.
    monkeypatch.setenv("LEAF_TELEMETRY_BACKEND", "firehose")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    assert telemetry_sink.disabled_reason() is None


def test_disabled_reason_firehose_without_boto3(monkeypatch):
    monkeypatch.setenv("LEAF_TELEMETRY_BACKEND", "firehose")
    real_import = __import__

    def _no_boto3(name, *a, **kw):
        if name == "boto3":
            raise ImportError("no boto3 in this image")
        return real_import(name, *a, **kw)

    monkeypatch.setattr("builtins.__import__", _no_boto3)
    reason = telemetry_sink.disabled_reason()
    assert reason is not None
    assert "boto3" in reason


def test_disabled_reason_dual_fails_closed_when_bigquery_side_not_ready(monkeypatch):
    monkeypatch.setenv("LEAF_TELEMETRY_BACKEND", "dual")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    reason = telemetry_sink.disabled_reason()
    # Firehose alone would be "ok" (boto3 is importable); dual must still
    # report disabled because bigquery has no credentials.
    assert reason is not None
    assert "dual backend not fully configured" in reason
    assert "firehose: ok" in reason


def test_disabled_reason_dual_enabled_when_both_sides_ready(monkeypatch):
    monkeypatch.setenv("LEAF_TELEMETRY_BACKEND", "dual")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", json.dumps({"project_id": "p"}))
    monkeypatch.setattr(telemetry_sink, "_sdk_checked", True)
    monkeypatch.setattr(telemetry_sink, "_firehose_sdk_checked", True)
    assert telemetry_sink.disabled_reason() is None


# --------------------------------------------------------------------------- #
# _flush_batch(): routes to the right backend(s), never both when not asked
# --------------------------------------------------------------------------- #

def test_flush_batch_bigquery_only_routes_to_bigquery_not_firehose(monkeypatch):
    monkeypatch.setenv("LEAF_TELEMETRY_BACKEND", "bigquery")
    calls = []
    monkeypatch.setattr(telemetry_sink, "_flush_bigquery", lambda rows: calls.append(("bq", rows)))
    monkeypatch.setattr(telemetry_sink, "_flush_firehose", lambda rows: calls.append(("fh", rows)))
    telemetry_sink._flush_batch([{"event_name": "a.b"}])
    assert [c[0] for c in calls] == ["bq"]


def test_flush_batch_firehose_only_routes_to_firehose_not_bigquery(monkeypatch):
    monkeypatch.setenv("LEAF_TELEMETRY_BACKEND", "firehose")
    calls = []
    monkeypatch.setattr(telemetry_sink, "_flush_bigquery", lambda rows: calls.append(("bq", rows)))
    monkeypatch.setattr(telemetry_sink, "_flush_firehose", lambda rows: calls.append(("fh", rows)))
    telemetry_sink._flush_batch([{"event_name": "a.b"}])
    assert [c[0] for c in calls] == ["fh"]


def test_flush_batch_dual_routes_to_both_with_the_identical_rows(monkeypatch):
    monkeypatch.setenv("LEAF_TELEMETRY_BACKEND", "dual")
    calls = []
    monkeypatch.setattr(telemetry_sink, "_flush_bigquery", lambda rows: calls.append(("bq", rows)))
    monkeypatch.setattr(telemetry_sink, "_flush_firehose", lambda rows: calls.append(("fh", rows)))
    batch = [{"event_name": "a.b"}, {"event_name": "c.d"}]
    telemetry_sink._flush_batch(batch)
    assert [c[0] for c in calls] == ["bq", "fh"]
    assert calls[0][1] is batch and calls[1][1] is batch


# --------------------------------------------------------------------------- #
# _flush_firehose(): row shape, batching, partial + whole-batch failure
# --------------------------------------------------------------------------- #

class _FakeFirehoseClient:
    def __init__(self, failed_put_count=0, raises=None):
        self.failed_put_count = failed_put_count
        self.raises = raises
        self.calls = []

    def put_record_batch(self, DeliveryStreamName, Records):
        self.calls.append((DeliveryStreamName, Records))
        if self.raises:
            raise self.raises
        responses = []
        for i, _r in enumerate(Records):
            if i < self.failed_put_count:
                responses.append({"ErrorCode": "ServiceUnavailableException",
                                   "ErrorMessage": "throttled"})
            else:
                responses.append({"RecordId": f"r{i}"})
        return {"FailedPutCount": self.failed_put_count, "RequestResponses": responses}


def test_flush_firehose_row_shape_matches_bigquery_promoted_columns(monkeypatch):
    monkeypatch.setenv("LEAF_TELEMETRY_FIREHOSE_STREAM", "leaf-telemetry-events-staging")
    fake = _FakeFirehoseClient()
    monkeypatch.setattr(telemetry_sink, "_get_firehose_client", lambda: fake)
    row = {
        "timestamp": "2026-08-19T00:00:00.000000Z",
        "event_type": "custom_event",
        "event_name": "job.terminal",
        "tenant_id": "acme",
        "tenant_kind": "account",
        "user_email": None,
        "session_id": "s1",
        "environment": "staging",
        "app_version": None,
        "labels": json.dumps({"schema_version": "1"}),
    }
    telemetry_sink._flush_firehose([row])
    assert len(fake.calls) == 1
    stream, records = fake.calls[0]
    assert stream == "leaf-telemetry-events-staging"
    assert len(records) == 1
    data = records[0]["Data"]
    assert isinstance(data, (bytes, bytearray))
    assert data.endswith(b"\n")
    decoded = json.loads(data.decode("utf-8"))
    # Exactly the BigQuery promoted columns (docs/PLATFORM_TELEMETRY.md),
    # including `labels` traveling as the SAME already-serialized string.
    assert set(decoded) == {
        "timestamp", "event_type", "event_name", "tenant_id", "tenant_kind",
        "user_email", "session_id", "environment", "app_version", "labels",
    }
    assert decoded["event_name"] == "job.terminal"
    assert decoded["tenant_id"] == "acme"
    assert isinstance(decoded["labels"], str)
    assert json.loads(decoded["labels"])["schema_version"] == "1"
    assert telemetry_sink.stats()["flushed"] == 1
    assert telemetry_sink.stats()["dropped_flush"] == 0


def test_flush_firehose_sends_one_record_per_row_as_newline_delimited_json(monkeypatch):
    fake = _FakeFirehoseClient()
    monkeypatch.setattr(telemetry_sink, "_get_firehose_client", lambda: fake)
    rows = [{"event_name": f"a.b{i}"} for i in range(5)]
    telemetry_sink._flush_firehose(rows)
    _stream, records = fake.calls[0]
    assert len(records) == 5
    for i, rec in enumerate(records):
        decoded = json.loads(rec["Data"].decode("utf-8"))
        assert decoded["event_name"] == f"a.b{i}"


def test_flush_firehose_partial_failure_drops_only_failed_records(monkeypatch):
    fake = _FakeFirehoseClient(failed_put_count=2)
    monkeypatch.setattr(telemetry_sink, "_get_firehose_client", lambda: fake)
    rows = [{"event_name": "a.b"} for _ in range(5)]
    telemetry_sink._flush_firehose(rows)
    assert telemetry_sink.stats()["flushed"] == 3
    assert telemetry_sink.stats()["dropped_flush"] == 2


def test_flush_firehose_whole_batch_exception_never_raises_and_drops_all(monkeypatch):
    fake = _FakeFirehoseClient(raises=RuntimeError("access denied"))
    monkeypatch.setattr(telemetry_sink, "_get_firehose_client", lambda: fake)
    rows = [{"event_name": "a.b"} for _ in range(4)]
    telemetry_sink._flush_firehose(rows)  # must not raise
    assert telemetry_sink.stats()["dropped_flush"] == 4
    assert telemetry_sink.stats()["flushed"] == 0


def test_flush_firehose_client_construction_failure_never_raises(monkeypatch):
    def _boom():
        raise RuntimeError("no AWS credentials in this environment")

    monkeypatch.setattr(telemetry_sink, "_get_firehose_client", _boom)
    telemetry_sink._flush_firehose([{"event_name": "a.b"}])  # must not raise
    assert telemetry_sink.stats()["dropped_flush"] == 1


# --------------------------------------------------------------------------- #
# end-to-end emit(): same bounded-queue/drop-oldest semantics regardless of
# backend, and the row lands in the queue identically under firehose
# --------------------------------------------------------------------------- #

def test_emit_enqueues_the_same_row_shape_under_firehose_backend(monkeypatch):
    monkeypatch.setenv("LEAF_TELEMETRY_BACKEND", "firehose")
    _enable_fake_sink(monkeypatch)
    ok = telemetry_sink.emit(
        "job.terminal", tenant_id="acme", tenant_kind="account", session_id="s1",
        labels={"attempts": 2},
    )
    assert ok is True
    row = telemetry_sink._queue[-1]
    assert row["event_name"] == "job.terminal"
    assert isinstance(row["labels"], str)
    assert json.loads(row["labels"])["attempts"] == "2"


def test_emit_drops_oldest_on_overflow_under_dual_backend(monkeypatch):
    monkeypatch.setenv("LEAF_TELEMETRY_BACKEND", "dual")
    _enable_fake_sink(monkeypatch)
    from collections import deque

    monkeypatch.setattr(telemetry_sink, "_queue", deque(maxlen=3))
    for i in range(5):
        telemetry_sink.emit(
            "a.b", tenant_id="t", tenant_kind="account", session_id="s",
            labels={"i": i},
        )
    q = list(telemetry_sink._queue)
    assert len(q) == 3
    kept = [json.loads(r["labels"])["i"] for r in q]
    assert kept == ["2", "3", "4"]


def test_emit_disabled_under_dual_when_bigquery_side_not_configured(monkeypatch):
    """The end-to-end proof that dual's fail-closed gate actually blocks
    emit(), not just disabled_reason() in isolation."""
    monkeypatch.setenv("LEAF_TELEMETRY_BACKEND", "dual")
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS_JSON", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
    ok = telemetry_sink.emit(
        "job.terminal", tenant_id="acme", tenant_kind="account", session_id="s1")
    assert ok is False
    assert telemetry_sink.stats()["enqueued"] == 0


# --------------------------------------------------------------------------- #
# region resolution (mirrors mcp_authority.py's AWS_REGION pattern)
# --------------------------------------------------------------------------- #

def test_aws_region_defaults_to_us_east_1(monkeypatch):
    monkeypatch.delenv("AWS_REGION", raising=False)
    assert telemetry_sink._aws_region() == "us-east-1"


def test_aws_region_env_override_wins(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    assert telemetry_sink._aws_region() == "us-west-2"


def test_make_firehose_client_uses_boto3_firehose_client_with_resolved_region(monkeypatch):
    calls = []

    class _FakeBoto3:
        @staticmethod
        def client(name, region_name=None):
            calls.append((name, region_name))
            return "the-client"

    monkeypatch.setitem(sys.modules, "boto3", _FakeBoto3())
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    result = telemetry_sink._make_firehose_client()
    assert result == "the-client"
    assert calls == [("firehose", "us-west-2")]
