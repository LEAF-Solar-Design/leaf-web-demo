"""Stop discovery at a provider page boundary and discard the partial inventory."""
from threading import Event
import json

import httpx
import pytest

from test_integration_tool_service import rig, service
from test_x03_project_dataset_discovery import record


@pytest.mark.parametrize("ask,stop_after,options,expected", [
    ("List all project datasets in the catalog.", 1, {}, ["d0", "d1", "d2", "d3"]),
    ("Show ready datasets in this project.", 2, {"page_size": 2}, ["d0", "d2"]),
    ("Which project datasets are stale?", 3, {}, ["d1", "d3"]),
])
def test_cancel_between_dataset_pages(rig, ask, stop_after, options, expected):
    event = Event()
    requests, closed, streams_closed = [], [], []

    def catalog(request):
        index = int(request.url.params.get("after", 0))
        requests.append(index)
        row = record(f"d{index}", "stale" if index % 2 else "ready")
        class PageStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield json.dumps({"records": [row],
                                  "nextCursor": str(index + 1) if index < 3 else None}).encode()

            async def aclose(self):
                streams_closed.append(index)

        return httpx.Response(200, headers={"Content-Type": "application/json"}, stream=PageStream())

    class Transport(httpx.MockTransport):
        async def aclose(self):
            await super().aclose()
            closed.append(requests[-1])
            if len(closed) == stop_after:
                event.set()

    rig.svc.transport = Transport(catalog)
    scope = rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id
    with pytest.raises(service.IncompleteInventory, match="^incomplete_inventory$") as failure:
        rig.svc.run_authored_dataset_report(ask, *scope, limit=1, cancel_event=event, **options)
    assert failure.value.pages_read == stop_after
    assert streams_closed == closed == requests == list(range(stop_after))
    assert all(not any(buf) for buf in rig.keys.buffers)

    result = rig.svc.run_authored_dataset_report(ask, *scope, limit=1, **options)
    assert requests[stop_after:] == list(range(4))
    assert [row["dataset_ref"]["id"] for row in result["datasets"]] == expected
