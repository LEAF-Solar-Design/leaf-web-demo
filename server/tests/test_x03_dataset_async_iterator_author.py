"""Exercise suspended provider iteration and asynchronous release before report return."""
import asyncio
import json

import httpx
import pytest

from test_integration_tool_service import rig, service
from test_x03_project_dataset_discovery import record


CASES = [
    ("List the datasets connected to this drawing.", "complete"),
    ("Show ready datasets across the project.", "failure"),
    ("Which project datasets can the checker read?", "cancel"),
]


@pytest.mark.parametrize("ask,outcome", CASES)
def test_async_page_lifetime(rig, ask, outcome):
    mode = outcome
    events = []
    class Page(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"records":['
            await asyncio.sleep(0)
            events.append("resumed")
            if mode == "cancel":
                raise asyncio.CancelledError("local producer cancelled")
            if mode == "failure":
                raise RuntimeError("local producer failed")
            yield json.dumps(record("alpha", "ready")).encode() + b'],"nextCursor":null}'

        async def aclose(self):
            events.append("closing")
            await asyncio.sleep(0)
            events.append("closed")

    rig.svc.transport = httpx.MockTransport(lambda _: httpx.Response(
        200, headers={"Content-Type": "application/json"}, stream=Page()))
    scope = rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id
    if mode != "complete":
        with pytest.raises(service.IncompleteInventory) as stopped:
            rig.svc.run_authored_dataset_report(ask, *scope)
        assert vars(stopped.value) == {"pages_read": 0}
        assert events == ["resumed", "closing", "closed"]
        events.clear()
    mode = "complete"
    report = rig.svc.run_authored_dataset_report(ask, *scope)
    assert [row["dataset_ref"]["id"] for row in report["datasets"]] == ["alpha"]
    assert events == ["resumed", "closing", "closed"]
