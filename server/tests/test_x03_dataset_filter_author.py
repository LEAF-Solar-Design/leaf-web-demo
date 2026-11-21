"""Check every page against the status predicate explicitly requested upstream."""
import httpx
import pytest

from test_integration_tool_service import rig, service
from test_x03_project_dataset_discovery import record


CASES = [
    ("List ready datasets with provider-side filtering.", {"ready"}, "empty", 0),
    ("Show stale or unavailable project datasets filtered at the source.",
     {"stale", "unavailable"}, "ready", 1),
    ("Which datasets are not stale? Apply the filter at the provider.",
     {"ready", "empty", "unavailable"}, "stale", 2),
]


@pytest.mark.parametrize("ask,allowed,outside,bad_page", CASES)
def test_pinned_provider_filter(rig, ask, allowed, outside, bad_page):
    scope = rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id
    statuses = sorted(allowed)
    pages = [statuses[i % len(statuses)] for i in range(3)]
    seen = []

    def catalog(request):
        after = request.url.params.get("after")
        index = int(after) if after is not None else 0
        seen.append((index, request.url.params.get("statuses")))
        return httpx.Response(200, json={"records": [record(f"d{index}", pages[index])],
                                         "nextCursor": str(index + 1) if index < 2 else None})

    rig.svc.transport = httpx.MockTransport(catalog)
    expected = list(pages)
    pages[bad_page] = outside
    assert not all(status in allowed for status in pages)
    with pytest.raises(service.ReadFailure, match="^provider_filter_mismatch$"):
        rig.svc.run_authored_dataset_report(ask, *scope, limit=1)
    query = ",".join(statuses)
    assert seen == [(i, query) for i in range(bad_page + 1)]
    pages[:] = expected
    seen.clear()
    report = rig.svc.run_authored_dataset_report(ask, *scope, limit=1)
    assert seen == [(i, query) for i in range(3)]
    assert [(row["title"], row["status"]) for row in report["datasets"]] == [
        (f"d{i}", status) for i, status in enumerate(pages) if status in allowed]
