"""Continue an inventory while provider arrival order changes between full scans."""
import httpx
import pytest

from test_integration_tool_service import rig
from test_x03_project_dataset_discovery import record


ROWS = [
    ("e", "Roof", "ready"), ("a", "Roof", "stale"), ("f", "Beam", "ready"),
    ("d", "Beam", "stale"), ("b", "Roof", "ready"), ("c", "Beam", "ready"),
]
CASES = [
    ("List all project datasets from first title to last.", "title", 2, ["c", "d", "f", "a", "b", "e"]),
    ("Show the project datasets from last title to first.", "-title", 2, ["e", "b", "a", "f", "d", "c"]),
    ("Which ready project datasets are available in title order?", "title", 1, ["c", "f", "b", "e"]),
]


@pytest.mark.parametrize("ask,sort,size,expected", CASES)
def test_title_ties_across_boundaries(rig, ask, sort, size, expected):
    requests = []
    order = list(ROWS)

    def catalog(request):
        offset = int(request.url.params.get("after", 0))
        requests.append(offset)
        rows = [{**record(key, status), "title": title} for key, title, status in order[offset:offset + 2]]
        return httpx.Response(200, json={"records": rows,
                                         "nextCursor": str(offset + 2) if offset + 2 < len(order) else None})

    rig.svc.transport = httpx.MockTransport(catalog)
    scope = rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id
    cursor = None
    seen = []
    for offset in range(0, len(expected), size):
        page = rig.svc.run_authored_dataset_report(ask, *scope, sort=sort, page_size=size, limit=2, cursor=cursor)
        ids = [row["dataset_ref"]["id"] for row in page["datasets"]]
        assert ids == expected[offset:offset + size]
        seen.extend(ids)
        cursor = page["next_cursor"]
        assert bool(cursor) == (offset + size < len(expected))
        order.append(order.pop(0))
    assert seen == expected
    assert requests == [0, 2, 4] * (len(expected) // size)
