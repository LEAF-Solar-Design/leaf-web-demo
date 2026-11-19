"""The local transport declares who must release its catalog iterator."""
import asyncio
import json
from threading import Event

import httpx
import pytest

from test_integration_tool_service import rig, service
from test_x03_project_dataset_discovery import record


CASES = [
    ("List all connected datasets for this drawing.", "complete"),
    ("Show the ready datasets in my project.", "failure"),
    ("Which datasets can my drawing checker read?", "cancel"),
]


@pytest.mark.parametrize("ask,outcome", CASES)
@pytest.mark.parametrize("ownership", ["owned", "borrowed"])
def test_catalog_iterator_ownership(rig, ask, outcome, ownership):
    cancel = Event()
    payload = json.dumps({"records": [record("north", "ready")], "nextCursor": None}).encode()

    class Pages(httpx.AsyncByteStream):
        def __init__(self):
            self.closes = 0
            self.pages = iter([payload, b"next consumer"])

        async def __aiter__(self):
            if self.closes:
                raise RuntimeError("closed iterator")
            yield next(self.pages)
            if outcome == "failure":
                raise httpx.ReadError("page interrupted")
            if outcome == "cancel":
                cancel.set()

        async def aclose(self):
            self.closes += 1

    pages = Pages()
    rig.svc.transport = httpx.MockTransport(lambda _: httpx.Response(
        200, headers={"Content-Type": "application/json"}, stream=pages,
        extensions={"leaf.catalog_stream_ownership": ownership}))
    scope = rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id
    if outcome == "complete":
        report = rig.svc.run_authored_dataset_report(ask, *scope, cancel_event=cancel)
        assert [r["dataset_ref"]["id"] for r in report["datasets"]] == ["north"]
    else:
        with pytest.raises(service.IncompleteInventory) as stopped:
            rig.svc.run_authored_dataset_report(ask, *scope, cancel_event=cancel)
        assert stopped.value.pages_read == int(outcome == "cancel")

    if ownership == "borrowed":
        assert pages.closes == 0
        assert asyncio.run(anext(pages.__aiter__())) == b"next consumer"
        asyncio.run(pages.aclose())
    assert pages.closes == 1
