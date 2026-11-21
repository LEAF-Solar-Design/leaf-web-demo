"""A failed continuation cannot produce a complete authored inventory."""
import httpx
import pytest

from test_integration_tool_service import rig, service
from test_x03_project_dataset_discovery import record


@pytest.mark.parametrize("ask,pages_before_failure,status,expected", [
    ("List all project datasets across the inventory.", 1, 503, ["frame", "wire"]),
    ("Show the ready datasets in this project.", 2, 429, ["frame"]),
    ("Which project datasets are stale?", 3, 504, ["wire"]),
])
def test_late_failure_then_explicit_restart(rig, ask, pages_before_failure, status, expected):
    visited = []
    recovering = False

    def catalog(request):
        index = int(request.url.params.get("after", "0"))
        visited.append(index)
        if index == pages_before_failure:
            if not recovering:
                return httpx.Response(status)
            rows, cursor = [record("wire", "stale")], None
        else:
            rows = [record("frame", "ready")] if index == 0 else []
            cursor = str(index + 1)
        return httpx.Response(200, json={"records": rows, "nextCursor": cursor})

    rig.svc.transport = httpx.MockTransport(catalog)
    scope = rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id
    with pytest.raises(service.ReadFailure, match="^incomplete_inventory$") as failure:
        rig.svc.run_authored_dataset_report(ask, *scope, limit=1)
    assert failure.value.pages_read == pages_before_failure
    assert visited == list(range(pages_before_failure + 1))
    recovering = True
    visited.clear()
    result = rig.svc.run_authored_dataset_report(ask, *scope, limit=1)
    assert visited == list(range(pages_before_failure + 1))
    assert result["datasets_scanned"] == 2
    assert [row["dataset_ref"]["id"] for row in result["datasets"]] == expected
