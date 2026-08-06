from __future__ import annotations

import client


def test_workitem_timing_names_every_aps_phase_and_accounts_for_total():
    status = {
        "stats": {
            "timeQueued": "2026-08-06T09:00:00Z",
            "timeDownloadStarted": "2026-08-06T09:00:10Z",
            "timeInstructionsStarted": "2026-08-06T09:00:20Z",
            "timeInstructionsEnded": "2026-08-06T09:00:25.810Z",
            "timeUploadEnded": "2026-08-06T09:00:30Z",
        }
    }

    timing = client._workitem_timing(status, submitted_at=1786006799.5)

    assert timing == {
        "contract": "leaf.cad-timing.aps.v1",
        "spans_ms": {
            "submit": 500,
            "queue": 10000,
            "task_start": 10000,
            "engine": 5810,
            "output_upload": 4190,
        },
        "accounted_ms": 30500,
        "unavailable_spans": ["image_pull", "drawing_fetch"],
    }


def test_workitem_timing_is_fail_closed_for_missing_or_invalid_stats():
    assert client._workitem_timing({}) == {
        "contract": "leaf.cad-timing.aps.v1",
        "spans_ms": {
            "submit": None,
            "queue": None,
            "task_start": None,
            "engine": None,
            "output_upload": None,
        },
        "accounted_ms": None,
        "unavailable_spans": [
            "submit", "queue", "task_start", "image_pull", "drawing_fetch",
            "engine", "output_upload",
        ],
    }

    invalid = {"stats": {
        "timeQueued": "later",
        "timeDownloadStarted": "earlier",
    }}
    assert client._workitem_timing(invalid)["spans_ms"]["queue"] is None
