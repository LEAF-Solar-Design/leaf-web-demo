"""A local provider loses its row stream partway through a catalog page."""
import json

import httpx
import pytest

from test_integration_tool_service import rig, service
from test_x03_project_dataset_discovery import record


CASES = [
    ("List all project datasets across the inventory.", 0, ["row0", "row1"]),
    ("Show ready datasets in this project.", 1, ["row0", "row2"]),
    ("Which project datasets are stale?", 2, ["row1", "row3"]),
]


@pytest.mark.parametrize("ask,complete_pages,expected", CASES)
def test_partial_provider_rows(rig, ask, complete_pages, expected):
    closed = []
    read_rows = []
    interrupt = True

    def catalog(request):
        page = int(request.url.params.get("after", 0))
        last = page == complete_pages
        cursor = None if last else str(page + 1)

        class Rows(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'{"records":['
                for index in range(page, page + (2 if last else 1)):
                    row = record(f"row{index}", "ready" if index % 2 == 0 else "stale")
                    read_rows.append(index)
                    yield (b"," if index != page else b"") + json.dumps(row).encode()
                    if interrupt and last:
                        raise httpx.ReadError("provider row stream interrupted")
                yield b'],"nextCursor":' + json.dumps(cursor).encode() + b"}"

            async def aclose(self):
                closed.append(page)

        return httpx.Response(200, headers={"Content-Type": "application/json"}, stream=Rows())

    rig.svc.transport = httpx.MockTransport(catalog)
    scope = rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id
    with pytest.raises(service.IncompleteInventory) as err:
        rig.svc.run_authored_dataset_report(ask, *scope, limit=2)
    assert vars(err.value) == {"pages_read": complete_pages}
    assert closed == read_rows == list(range(complete_pages + 1))

    interrupt = False
    closed.clear()
    report = rig.svc.run_authored_dataset_report(ask, *scope, limit=2)
    assert [row["dataset_ref"]["id"] for row in report["datasets"]] == expected
    assert closed == list(range(complete_pages + 1))
