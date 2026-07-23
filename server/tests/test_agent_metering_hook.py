"""The live turn relay emits exactly one idempotent agent usage record."""
from __future__ import annotations

import json
import os
import platform as _stdlib_platform
import sys
import tempfile
import threading
from pathlib import Path

_stdlib_platform.python_implementation()

SERVER_DIR = Path(__file__).resolve().parent.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))
os.environ.setdefault(
    "SESSIONS_DB",
    str(Path(tempfile.mkdtemp(prefix="agent-metering-sessions-")) / "sessions.db"),
)

import turn_runner  # noqa: E402


class _Response:
    def __init__(self, events):
        self.events = events

    def iter_lines(self, decode_unicode=True):
        assert decode_unicode is True
        for event in self.events:
            yield json.dumps(event)

    def close(self):
        return None


def test_tool_free_live_turn_is_metered_once_and_replay_has_stable_key(monkeypatch):
    stored = {}
    appended = []
    complete = threading.Event()

    def append_usage(record):
        key = (record["tenant_id"], record["session_id"], record["turn_id"])
        stored.setdefault(key, dict(record))
        appended.append(key)
        complete.set()

    monkeypatch.setattr(turn_runner.agent_ledger, "append", append_usage)
    monkeypatch.setattr(turn_runner.session_store, "append_event", lambda *_args: 1)
    monkeypatch.setattr(turn_runner.session_store, "end_turn", lambda *_args: True)
    events = [
        {"type": "turn_usage",
         "data": {"cost_tokens": 42, "total_cost_usd": 0.0004}},
        {"type": "turn_complete", "data": {"stop_reason": "end_turn"}},
        {"type": "turn_complete", "data": {"stop_reason": "end_turn"}},
    ]

    for _replay in range(2):
        complete.clear()
        turn_runner._spawn_relay(
            "tenant-a", "session-a", "turn-a", _Response(events), 1.0)
        assert complete.wait(timeout=1.0)

    key = ("tenant-a", "session-a", "turn-a")
    assert list(stored) == [key]
    assert stored[key] == {
        "tenant_id": "tenant-a",
        "session_id": "session-a",
        "turn_id": "turn-a",
        "cost_tokens": 42,
        "usd_est": 0.0004,
        "tools_called": [],
        "stop_reason": "end_turn",
    }
    # The PostgreSQL writer collapses these two exact stable-key deliveries.
    assert appended == [key, key]


def test_metering_failure_never_strands_active_turn(monkeypatch, capsys):
    ended = threading.Event()
    monkeypatch.setattr(
        turn_runner.agent_ledger, "append",
        lambda _record: (_ for _ in ()).throw(RuntimeError("database down")),
    )
    monkeypatch.setattr(turn_runner.session_store, "append_event", lambda *_args: 1)
    monkeypatch.setattr(
        turn_runner.session_store, "end_turn",
        lambda *_args: ended.set(),
    )
    turn_runner._spawn_relay(
        "tenant-a", "session-a", "turn-a",
        _Response([{
            "type": "turn_complete", "data": {"stop_reason": "end_turn"},
        }]),
        1.0,
    )
    assert ended.wait(timeout=1.0)
    assert "terminal metering failed: RuntimeError" in capsys.readouterr().err
