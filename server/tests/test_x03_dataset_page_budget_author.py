"""Bound provider rows and check terminal counts before issuing an authored inventory."""
import httpx
import pytest

from test_integration_tool_service import rig, service
from test_x03_project_dataset_discovery import record


CASES = [
    ("List every project dataset in this inventory.", "oversized", ["a", "b", "c"]),
    ("Show the ready project datasets in the catalog.", "short", ["a", "c"]),
    ("Which project datasets are stale?", "overrun", ["b"]),
]


@pytest.mark.parametrize("ask,case,expected", CASES)
def test_page_budget_and_count(rig, monkeypatch, ask, case, expected):
    a, b, c, d = [record(key, state) for key, state in
                  [("a", "ready"), ("b", "stale"), ("c", "ready"), ("d", "stale")]]
    pages = [([a, b], 3), ([c], 3)]
    requests = []

    def catalog(request):
        index = int(request.url.params.get("after", 0))
        requests.append(index)
        rows, declared = pages[index]
        data = {"records": rows, "nextCursor": str(index + 1) if index + 1 < len(pages) else None}
        if declared is not None:
            data["totalCount"] = declared
        return httpx.Response(200, json=data)

    monkeypatch.setattr(service, "DATASET_ROW_BUDGET", 3)
    rig.svc.transport = httpx.MockTransport(catalog)
    scope = rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id

    def report():
        return rig.svc.run_authored_dataset_report(ask, *scope, limit=2)

    if case == "oversized":
        pages[:] = [([a, b, c], None), ([d], None)]
        with pytest.raises(service.ReadFailure, match="^invalid_response$"):
            report()
        assert requests == [0]
    else:
        pages[:] = [([a, b], 3), ([], 3)] if case == "short" else [([a], 1), ([b], 1), ([c], 1)]
        with pytest.raises(service.IncompleteInventory):
            report()
        assert requests == [0, 1]

    requests.clear()
    pages[:] = [([a, b], None), ([c, d], None), ([], None)]
    with pytest.raises(service.ReadFailure, match="^response_too_large$"):
        report()
    assert requests == [0, 1]

    requests.clear()
    pages[:] = [([a, b], 3), ([c], 3)]
    result = report()
    assert requests == [0, 1]
    assert result["datasets_scanned"] == 3
    assert [row["dataset_ref"]["id"] for row in result["datasets"]] == expected
