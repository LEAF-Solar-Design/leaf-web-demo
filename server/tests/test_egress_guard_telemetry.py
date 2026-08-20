"""TEL-1: a firing OperatorEgressDenied must be OBSERVABLE, not just enforced.

Today (pre-TEL-1) a denial produced NO log line, NO metric, NO event -- a
firing security control was invisible. This test proves the three required
emissions independently, so deleting any ONE of them (a mutation) fails only
its own assertion below, never the other two:

  (a) a structured log line (JSON to stderr, no `_aws` envelope)
  (b) product event security.egress_denied via telemetry_sink, labels
      {kind, host_class, caller_surface} -- host_class is a CLASS, never the
      raw host
  (c) EMF metric EgressDenied (Count) in Leaf/Platform/APS, dimension {kind},
      via emf_metrics's own EMF writer (docs/PLATFORM_TELEMETRY.md,
      CONTRACT-ADDENDUM.md conventions: domain.action event name, bounded
      string labels, never a raw/high-cardinality value).
"""
from __future__ import annotations

import json
import socket

import pytest

import emf_metrics
import telemetry_sink
from operator_egress_guard import OperatorEgressDenied, operator_execution


@pytest.fixture(autouse=True)
def _reset_sink(monkeypatch):
    monkeypatch.setattr(telemetry_sink, "disabled_reason", lambda: None)
    monkeypatch.setattr(telemetry_sink, "_ensure_flusher", lambda: None)
    monkeypatch.setattr(emf_metrics, "_DISABLED", False)
    with telemetry_sink._wake:
        telemetry_sink._queue.clear()
    yield


def _stderr_lines(capsys) -> list[dict]:
    err = capsys.readouterr().err
    return [json.loads(ln) for ln in err.splitlines() if ln.strip()]


# --------------------------------------------------------------------------- #
# (a) structured log line
# --------------------------------------------------------------------------- #

def test_deny_emits_a_structured_log_line(capsys):
    with pytest.raises(OperatorEgressDenied):
        socket.getaddrinfo("api.vercel.com", 443)  # Layer 1, unarmed

    logs = [d for d in _stderr_lines(capsys) if "_aws" not in d]
    assert logs, "no structured log line was written on a firing denial"
    doc = logs[-1]
    assert doc["event"] == "operator_egress_denied"
    assert doc["kind"] == "deploy-route/name-resolution"
    assert doc["host_class"] == "production_control_plane"
    assert doc["caller_surface"] == "process"
    # NEVER the raw host in the log document.
    assert "api.vercel.com" not in json.dumps(doc)


def test_deny_log_line_reflects_operator_handler_caller_surface(capsys):
    with operator_execution():
        with pytest.raises(OperatorEgressDenied):
            socket.getaddrinfo("example.com", 443)  # Layer 2, armed

    logs = [d for d in _stderr_lines(capsys) if "_aws" not in d]
    assert logs
    doc = logs[-1]
    assert doc["caller_surface"] == "operator_handler"
    assert doc["host_class"] == "operator_disallowed_host"
    assert "example.com" not in json.dumps(doc)


# --------------------------------------------------------------------------- #
# (b) product event security.egress_denied
# --------------------------------------------------------------------------- #

def test_deny_emits_the_security_egress_denied_product_event():
    with pytest.raises(OperatorEgressDenied):
        socket.getaddrinfo("api.vercel.com", 443)

    assert telemetry_sink._queue, "no product event was enqueued on a firing denial"
    row = telemetry_sink._queue[-1]
    assert row["event_name"] == "security.egress_denied"
    assert row["event_type"] == "custom_event"
    labels = json.loads(row["labels"])
    assert labels["kind"] == "deploy-route/name-resolution"
    assert labels["host_class"] == "production_control_plane"
    assert labels["caller_surface"] == "process"
    # host_class is an ALLOWLISTED CLASS, never the raw host (it can carry
    # secrets): the raw target must not appear anywhere in the emitted row.
    assert "api.vercel.com" not in json.dumps(row)


def test_deploy_cli_spawn_denial_classifies_host_as_deploy_cli():
    import subprocess

    with pytest.raises(OperatorEgressDenied):
        subprocess.Popen(["vercel", "promote"])

    row = telemetry_sink._queue[-1]
    assert row["event_name"] == "security.egress_denied"
    labels = json.loads(row["labels"])
    assert labels["kind"] == "deploy-cli-spawn"
    assert labels["host_class"] == "deploy_cli"
    assert labels["caller_surface"] == "process"
    assert "vercel" not in json.dumps(row)


# --------------------------------------------------------------------------- #
# (c) EMF metric EgressDenied
# --------------------------------------------------------------------------- #

def test_deny_emits_the_egress_denied_emf_metric(capsys):
    with pytest.raises(OperatorEgressDenied):
        socket.getaddrinfo("api.vercel.com", 443)

    docs = [d for d in _stderr_lines(capsys) if "_aws" in d]
    assert docs, "no EMF metric document was written on a firing denial"
    doc = docs[-1]
    directive = doc["_aws"]["CloudWatchMetrics"][0]
    assert directive["Namespace"] == emf_metrics.NAMESPACE
    assert directive["Dimensions"] == [["kind"]]
    assert directive["Metrics"] == [{"Name": "EgressDenied", "Unit": "Count"}]
    assert doc["EgressDenied"] == 1
    assert doc["kind"] == "deploy-route/name-resolution"


def test_egress_denied_metric_honors_the_emf_kill_switch(capsys, monkeypatch):
    monkeypatch.setattr(emf_metrics, "_DISABLED", True)
    with pytest.raises(OperatorEgressDenied):
        socket.getaddrinfo("api.vercel.com", 443)

    docs = [d for d in _stderr_lines(capsys) if "_aws" in d]
    assert not docs, "APS_EMF_DISABLED must suppress EgressDenied like every other metric"


# --------------------------------------------------------------------------- #
# all three fire together on ONE denial (mutation-red: deleting any single
# emit call in operator_egress_guard._deny leaves the other two assertions
# above green and only its own red)
# --------------------------------------------------------------------------- #

def test_a_single_denial_produces_all_three_emissions_together(capsys):
    with pytest.raises(OperatorEgressDenied):
        socket.getaddrinfo("169.254.169.254", 80)  # cloud metadata, Layer 1

    lines = _stderr_lines(capsys)
    logs = [d for d in lines if "_aws" not in d]
    metrics = [d for d in lines if "_aws" in d]
    assert logs and logs[-1]["host_class"] == "cloud_metadata"
    assert metrics and metrics[-1]["kind"] == "deploy-route/name-resolution"
    assert telemetry_sink._queue
    labels = json.loads(telemetry_sink._queue[-1]["labels"])
    assert labels["host_class"] == "cloud_metadata"


# --------------------------------------------------------------------------- #
# telemetry never blocks or softens the deny itself
# --------------------------------------------------------------------------- #

def test_deny_still_raises_when_telemetry_sink_emit_is_broken(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("sink exploded")

    monkeypatch.setattr(telemetry_sink, "emit", _boom)
    with pytest.raises(OperatorEgressDenied):
        socket.getaddrinfo("api.vercel.com", 443)


def test_deny_still_raises_when_emf_emit_is_broken(monkeypatch):
    def _boom(*a, **kw):
        raise RuntimeError("emf exploded")

    monkeypatch.setattr(emf_metrics, "_emit", _boom)
    with pytest.raises(OperatorEgressDenied):
        socket.getaddrinfo("api.vercel.com", 443)
