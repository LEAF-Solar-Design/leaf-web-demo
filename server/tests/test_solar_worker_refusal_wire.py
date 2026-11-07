"""Keep the client no-worker fixture identical to the real admission producer."""
import json
from pathlib import Path

import solar_project_admission as policy


def test_g17_worker_refusal_matches_client_fixture():
    fixture = Path(__file__).resolve().parents[2] / "web/src/solar/__fixtures__/workerUnavailable.json"
    expected = json.loads(fixture.read_text(encoding="utf-8"))
    reason = "SIP_R4_WORKER_UNAVAILABLE"
    status, retryable = policy._FAILURES[reason]
    refusal = policy._refusal(reason, status=status, retryable=retryable)
    assert status == refusal["status_code"] == 503
    assert retryable is True
    assert refusal["content"] == expected
