"""TEL-1: a firing OperatorEgressDenied must emit log + event + metric.

Before this card: OperatorEgressDenied (operator_egress_guard.py `_deny`,
formerly four separate `raise` sites) produced NO log line, NO metric, NO
event -- a firing security control was invisible. This test triggers a real
denial and asserts ALL THREE emissions land, each independently mutation-red:
deleting any one of the three `_emit_egress_denied_*` calls inside `_deny()`
breaks exactly one of the three assertion blocks below and nothing else.
"""
from __future__ import annotations

import json
import logging
import socket
import subprocess

import pytest

import operator_egress_guard as guard
import telemetry_sink
from operator_egress_guard import OperatorEgressDenied, operator_execution


@pytest.fixture(autouse=True)
def _reset_sink(monkeypatch):
    # Same pattern as tests/test_telemetry.py: make the sink "enabled" without
    # the BigQuery SDK, and keep the flusher off so rows stay in the queue for
    # inspection instead of racing a background thread.
    monkeypatch.setattr(telemetry_sink, "disabled_reason", lambda: None)
    monkeypatch.setattr(telemetry_sink, "_ensure_flusher", lambda: None)
    with telemetry_sink._wake:
        telemetry_sink._queue.clear()
    yield


def _find_json_lines(text: str) -> list[dict]:
    docs = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            docs.append(json.loads(line))
        except ValueError:
            continue
    return docs


def test_denial_emits_structured_log_product_event_and_emf_metric(caplog, capsys):
    """Layer 1 (unconditional): resolving a known production deploy route
    fires a denial with no operator context armed. Asserts all three
    emissions and that NONE of them carry the raw host (only the bounded
    kind/host_class/caller_surface classification)."""
    assert not guard.is_armed()

    with caplog.at_level(logging.WARNING):
        with pytest.raises(OperatorEgressDenied):
            socket.getaddrinfo("api.vercel.com", 443)

    # (a) structured log at deny.
    log_docs = []
    for rec in caplog.records:
        try:
            log_docs.append(json.loads(rec.getMessage()))
        except (ValueError, TypeError):
            continue
    log_matches = [d for d in log_docs if d.get("event") == "security.egress_denied"]
    assert log_matches, "no structured security.egress_denied log line emitted"
    log_doc = log_matches[0]
    assert log_doc["kind"] == "deploy-route/name-resolution"
    assert log_doc["host_class"] == "deploy_control_plane"
    assert log_doc["caller_surface"] == "process_wide"
    assert "api.vercel.com" not in json.dumps(log_doc)

    # (b) product event security.egress_denied {kind, host_class, caller_surface}.
    with telemetry_sink._wake:
        rows = list(telemetry_sink._queue)
    events = [r for r in rows if r["event_name"] == "security.egress_denied"]
    assert events, "no security.egress_denied product event enqueued"
    labels = json.loads(events[0]["labels"])
    assert labels["kind"] == "deploy-route/name-resolution"
    assert labels["host_class"] == "deploy_control_plane"
    assert labels["caller_surface"] == "process_wide"
    assert "api.vercel.com" not in json.dumps(labels)

    # (c) EMF metric EgressDenied (Count) dimension {kind} in Leaf/Platform/APS.
    metric_docs = [
        d for d in _find_json_lines(capsys.readouterr().err) if "EgressDenied" in d
    ]
    assert metric_docs, "no EgressDenied EMF metric line emitted"
    metric_doc = metric_docs[0]
    directive = metric_doc["_aws"]["CloudWatchMetrics"][0]
    assert directive["Namespace"] == "Leaf/Platform/APS"
    assert directive["Dimensions"] == [["kind"]]
    assert directive["Metrics"] == [{"Name": "EgressDenied", "Unit": "Count"}]
    assert metric_doc["EgressDenied"] == 1
    assert metric_doc["kind"] == "deploy-route/name-resolution"
    assert "api.vercel.com" not in json.dumps(metric_doc)


def test_deploy_cli_spawn_denial_classifies_as_process_process_wide(caplog):
    """Layer 1 spawn path: a deploy-CLI spawn has no host at all, so
    host_class is "process", and it fires without arming."""
    assert not guard.is_armed()
    with caplog.at_level(logging.WARNING):
        with pytest.raises(OperatorEgressDenied):
            subprocess.Popen(["vercel", "promote"])
    with telemetry_sink._wake:
        rows = list(telemetry_sink._queue)
    events = [r for r in rows if r["event_name"] == "security.egress_denied"]
    assert events
    labels = json.loads(events[-1]["labels"])
    assert labels["kind"] == "deploy-cli-spawn"
    assert labels["host_class"] == "process"
    assert labels["caller_surface"] == "process_wide"


def test_operator_context_host_denial_classifies_as_unallowlisted_operator_context():
    """Layer 2 (armed): a non-deploy, non-allowlisted host denied under an
    armed operator handler classifies as unallowlisted / operator_context,
    never the raw host example.com."""
    with operator_execution():
        with pytest.raises(OperatorEgressDenied):
            socket.getaddrinfo("example.com", 443)
    with telemetry_sink._wake:
        rows = list(telemetry_sink._queue)
    events = [r for r in rows if r["event_name"] == "security.egress_denied"]
    assert events
    labels = json.loads(events[-1]["labels"])
    assert labels["kind"] == "name-resolution"
    assert labels["host_class"] == "unallowlisted"
    assert labels["caller_surface"] == "operator_context"
    assert "example.com" not in json.dumps(labels)
