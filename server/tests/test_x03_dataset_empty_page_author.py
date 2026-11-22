"""Empty pages may carry a continuation; only the terminal cursor ends discovery."""
import httpx
import pytest

from test_integration_tool_service import rig, service
from test_x03_project_dataset_discovery import record


CASES = [
    ("List all project datasets across the inventory.", ["frames", "cables"]),
    ("Show the ready datasets in this project.", ["frames"]),
    ("Which project datasets are stale?", ["cables"]),
]


@pytest.mark.parametrize("ask,expected", CASES)
def test_empty_pages_between_results(rig, ask, expected):
    pages = [[], [record("frames", "ready")], [], [], [record("cables", "stale")], []]
    visited = []
    def catalog(request):
        index = int(request.url.params.get("after", "0"))
        visited.append(index)
        cursor = str(index + 1) if index + 1 < len(pages) else None
        return httpx.Response(200, json={"records": pages[index], "nextCursor": cursor})
    rig.svc.transport = httpx.MockTransport(catalog)
    scope = rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id
    result = rig.svc.run_authored_dataset_report(ask, *scope, limit=1)
    assert visited == list(range(len(pages)))
    assert result["pages_read"] == len(pages)
    assert result["datasets_scanned"] == 2
    assert [row["dataset_ref"]["id"] for row in result["datasets"]] == expected


def test_endless_empty_pages_fail_at_catalog_limit(rig):
    count = 0
    def catalog(request):
        nonlocal count
        count += 1
        assert count <= 1001
        return httpx.Response(200, json={"records": [], "nextCursor": str(count)})
    rig.svc.transport = httpx.MockTransport(catalog)
    scope = rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id
    with pytest.raises(service.ReadFailure, match="response_too_large"):
        rig.svc.run_authored_dataset_report(CASES[0][0], *scope, limit=1)
    assert count == 1001
