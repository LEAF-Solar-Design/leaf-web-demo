"""Regression guards for display-only structured question relay events."""
from __future__ import annotations

import json
import sys
import time
import uuid
from pathlib import Path


SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

import session_store  # noqa: E402
import turn_runner  # noqa: E402


class _ScriptedResponse:
    def __init__(self, events):
        self._lines = [json.dumps(event).encode("utf-8") for event in events]

    def iter_lines(self, decode_unicode=False):
        yield from self._lines

    def close(self):
        return None


def _wait_until(predicate, timeout_s=3.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_question_required_relay_is_verbatim_and_does_not_enter_prior_messages():
    tenant_id = f"tenant-question-{uuid.uuid4()}"
    session = session_store.get_or_create_session(tenant_id, f"dwg-{uuid.uuid4()}")
    session_id = session["session_id"]
    turn_id = f"turn-{uuid.uuid4()}"
    assert session_store.try_begin_turn(session_id, turn_id, 30.0)

    question = {
        "question_id": "question-1",
        "question": "Which plan?",
        "options": [{"label": "Standard"}, {"label": "Premium", "description": "More seats"}],
    }
    turn_runner._spawn_relay(tenant_id, session_id, turn_id, _ScriptedResponse([
        {"type": "question_required", "data": question},
        {"type": "turn_complete", "data": {"stop_reason": "end_turn"}},
    ]), 30.0)

    assert _wait_until(lambda: session_store.get_session(session_id)["active_turn_id"] is None)
    events = session_store.recent_events(session_id, 20)
    relayed = next(event for event in events if event["type"] == "question_required")
    assert relayed["turn_id"] == turn_id
    assert relayed["data"] == question
    assert session_store.get_approval("question-1") is None
    assert turn_runner._prior_messages(session_id, exclude_turn_id="none") == []
