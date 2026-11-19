"""Provider cursors at the byte boundary continue; oversized cursors stop the scan."""
import httpx
import pytest

from test_integration_tool_service import rig, service
from test_x03_project_dataset_discovery import record


CASES = [
    ("List every dataset in this project.", 0, ["d0", "d1"]),
    ("Show the ready datasets in this inventory.", 1, ["d0", "d2"]),
    ("Which project datasets are stale?", 2, ["d1", "d3"]),
]


@pytest.mark.parametrize("ask,boundary_page,expected", CASES)
def test_provider_cursor_byte_limit(rig, ask, boundary_page, expected):
    scope = rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id
    boundary = "tag_" * 256
    assert len(boundary.encode("utf-8")) == 1024
    # One example crosses the wire limit even though its character count is smaller.
    token = "é" * 513 if boundary_page == 1 else boundary + "x"
    assert len(token.encode("utf-8")) > 1024
    visited = []

    def catalog(request):
        after = request.url.params.get("after")
        index = len(visited)
        visited.append(after)
        assert index <= boundary_page + 1
        cursor = str(index + 1) if index < boundary_page else token if index == boundary_page else None
        row = record(f"d{index}", "stale" if index % 2 else "ready")
        return httpx.Response(200, json={"records": [row], "nextCursor": cursor})

    rig.svc.transport = httpx.MockTransport(catalog)
    with pytest.raises(service.IncompleteInventory, match="^incomplete_inventory$") as failure:
        rig.svc.run_authored_dataset_report(ask, *scope, limit=1)
    assert failure.value.pages_read == boundary_page
    prefix = [None, *(str(i) for i in range(1, boundary_page + 1))]
    assert visited == prefix

    visited.clear()
    token = boundary
    result = rig.svc.run_authored_dataset_report(ask, *scope, limit=1)
    assert visited == [*prefix, boundary]
    assert result["pages_read"] == result["datasets_scanned"] == len(visited)
    assert [row["dataset_ref"]["id"] for row in result["datasets"]] == expected
