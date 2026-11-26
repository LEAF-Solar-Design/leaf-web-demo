"""Keep the report or its primary failure when catalog stream cleanup needs a retry."""
import json
from threading import Event

import httpx
import pytest

from test_integration_tool_service import rig, service
from test_x03_project_dataset_discovery import record


CASES = [
    ("List all datasets in this project.", "complete"),
    ("Show the ready datasets for this drawing.", "cancel"),
    ("Which project datasets are stale?", "interrupted"),
]


@pytest.mark.parametrize("ask,outcome", CASES)
def test_close_failure_preserves_outcome(rig, caplog, ask, outcome):
    cancelled = Event()
    close_attempts = []
    released = set()

    class Page(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"records":['
            if outcome == "interrupted":
                raise httpx.ReadError("local row read interrupted")
            yield json.dumps(record("beam", "ready")).encode() + b'],"nextCursor":null}'
            if outcome == "cancel":
                cancelled.set()

        async def aclose(self):
            close_attempts.append("page")
            if len(close_attempts) == 1:
                raise OSError("local cleanup detail")
            released.add("page")

    class Catalog(httpx.MockTransport):
        async def aclose(self):
            released.add("transport")
            await super().aclose()

    rig.svc.transport = Catalog(lambda _: httpx.Response(
        200, headers={"Content-Type": "application/json"}, stream=Page()))
    scope = rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id
    if outcome == "complete":
        report = rig.svc.run_authored_dataset_report(ask, *scope, cancel_event=cancelled)
        assert [row["dataset_ref"]["id"] for row in report["datasets"]] == ["beam"]
    else:
        with pytest.raises(service.IncompleteInventory) as primary:
            rig.svc.run_authored_dataset_report(ask, *scope, cancel_event=cancelled)
        assert primary.value.pages_read == (1 if outcome == "cancel" else 0)
    assert close_attempts == ["page", "page"]
    assert released == {"page", "transport"}
    assert caplog.messages.count("Retrying dataset response stream close") == 1
    assert "local cleanup detail" not in caplog.text
