"""Synthetic source-intake phase diagnostics do not change producer behavior."""
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event
from uuid import UUID

import pytest

import solar_project_solaredge as se
from solar_design_graph import GraphValidationError
from test_project_solaredge import lane, upload  # noqa: F401


PHASES = ("authorization", "inspection", "artifact_prepare", "authority_recheck",
          "slot_reservation", "artifact_finish", "artifact_commit")


def records(capfd):
    return [json.loads(line) for line in capfd.readouterr().err.splitlines()
            if '"event":"solar_pdf_intake_phase"' in line]


def test_source_phase_sequence_is_correlated_and_contains_no_request_data(lane, capfd):
    before = lane.head, len(lane.versions), len(lane.calls)
    result = upload(lane)
    events = records(capfd)
    assert [(e["phase"], e["state"]) for e in events] == [
        (phase, state) for phase in PHASES for state in ("started", "completed")]
    attempts = {e["attempt_id"] for e in events}
    assert len(attempts) == 1
    UUID(attempts.pop())
    for event in events:
        assert set(event) == {"event", "schema", "attempt_id", "phase", "state", "elapsed_ms"}
        assert event["schema"] == "leaf.solar-pdf-intake-phase.v1"
        assert type(event["elapsed_ms"]) is int and event["elapsed_ms"] >= 0
    text = json.dumps(events)
    for value in (str(lane.org), str(lane.project), str(lane.drawing),
                  str(lane.ctx.parent_version_id), result["source"]["content_sha256"]):
        assert value not in text
    assert (lane.head, len(lane.versions), len(lane.calls)) == before
    assert result["schema"] == se.SOURCE_SCHEMA


def test_inspection_failure_keeps_refusal_and_does_not_log_exception_text(lane, monkeypatch, capfd):
    before = dict(lane.blobs)
    def fail(_data):
        raise se.sources.ImportSourceError("IMPORT_PDF_MALFORMED")
    monkeypatch.setattr(se.sources, "inspect_solaredge_pdf", fail)
    with pytest.raises(GraphValidationError) as exc:
        upload(lane)
    assert exc.value.code == "IMPORT_PDF_MALFORMED"
    events = records(capfd)
    assert [(e["phase"], e["state"]) for e in events] == [
        ("authorization", "started"), ("authorization", "completed"),
        ("inspection", "started"), ("inspection", "failed")]
    assert "IMPORT_PDF_MALFORMED" not in json.dumps(events)
    assert lane.blobs == before


def test_pending_inspection_is_observable_before_it_finishes(lane, monkeypatch, capfd):
    entered, release = Event(), Event()
    original = se.sources.inspect_solaredge_pdf
    def pending(data):
        entered.set()
        assert release.wait(5), "synthetic supervisor deadline"
        return original(data)
    monkeypatch.setattr(se.sources, "inspect_solaredge_pdf", pending)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(upload, lane)
        try:
            assert entered.wait(5)
            early = records(capfd)
            assert early[-1]["phase"] == "inspection" and early[-1]["state"] == "started"
            assert not future.done()
        finally:
            release.set()
        future.result(timeout=5)
    late = records(capfd)
    assert late[0]["phase"] == "inspection" and late[0]["state"] == "completed"
    assert early[-1]["attempt_id"] == late[0]["attempt_id"]


def test_observer_failure_cannot_break_success_or_denial(lane, monkeypatch):
    class BrokenStream:
        def write(self, _value):
            raise OSError("synthetic private diagnostic canary")
        def flush(self):
            raise OSError("synthetic private diagnostic canary")
    monkeypatch.setattr(se, "_IMPORT_TRACE_STREAM", BrokenStream())
    assert upload(lane)["schema"] == se.SOURCE_SCHEMA
    denial = {"denied": True}
    monkeypatch.setattr(se, "_authorized", lambda *args: (None, denial))
    assert upload(lane) is denial


def test_separate_attempts_get_distinct_correlation_ids(lane, capfd):
    upload(lane)
    first = records(capfd)
    upload(lane)
    second = records(capfd)
    assert first[0]["attempt_id"] != second[0]["attempt_id"]
