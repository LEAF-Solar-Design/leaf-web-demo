"""Verify report scope and the provider's page-local query projections and filters."""
from copy import deepcopy
import json

import httpx
import pytest

from test_integration_tool_service import FIELD, read, rig
from test_x03_project_dataset_discovery import record


CASES = [
    ("List ready datasets filtered at the source. Return columns title and fields.", "ready", "title", "gt", 3),
    ("Show stale project datasets with provider-side filtering. Include columns status and fields.", "stale", "status", "eq", 7),
    ("Which empty datasets are available? Filter at the provider. Show columns fields and freshness.", "empty", "freshness", "lt", 12),
]


@pytest.mark.parametrize("ask,state,column,operator,bound", CASES)
def test_requests_have_independent_scope(rig, ask, state, column, operator, bound):
    scope = rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id
    catalog_requests, query_requests = [], []
    def provider(request):
        if request.method == "GET":
            cursor = request.url.params.get("after")
            catalog_requests.append(dict(request.url.params))
            request.url = request.url.copy_merge_params({"statuses": "unavailable"})
            name = "east" if cursor is None else "west"
            return httpx.Response(200, json={"records": [record(name, state)],
                "nextCursor": "west" if cursor is None else None})
        page = json.loads(request.content)
        assert request.url.path == (
            f"/api/v1/projects/{rig.auth.project_id}/integrations/datasets/east/versions/1/query")
        query_requests.append(deepcopy(page))
        # Local providers may adapt the request for their own query language.
        page["filters"][0]["value"] = -100
        page["fields"].append("private")
        response = read()
        response["dataset_ref"]["id"] = "east"
        response["rows"] = ([] if state == "empty" else
            [{"key": "p2" if "cursor" in page else "p1",
              "values": {"amount": bound + 1 if operator == "gt" else bound}}])
        response["next_cursor"] = None if "cursor" in page else "second"
        return httpx.Response(200, json=response)

    rig.svc.transport = httpx.MockTransport(provider)
    report = rig.svc.run_authored_dataset_report(ask, *scope, limit=1)
    assert catalog_requests == [{"limit": "1", "statuses": state},
                                {"limit": "1", "statuses": state, "after": "west"}]
    assert [r["dataset_ref"]["id"] for r in report["datasets"]] == ["east", "west"]
    assert all(set(row) == {"dataset_ref", column, "fields"} for row in report["datasets"])

    # Discovery GETs carry status filters. The nested filter and field projection
    # travel on the subsequent dataset query POST, using the discovered version.
    row = report["datasets"][0]
    assert row["fields"] == [FIELD]
    wanted = {"fields": ["amount"], "filters": [{"field": "amount", "op": operator, "value": bound}], "limit": 1}
    caller = {"dataset_ref": row["dataset_ref"], "query": deepcopy(wanted)}
    first = rig.call(caller)["result"]
    assert caller["query"] == wanted
    assert first["next_cursor"] == "second"
    next_page = {**caller, "query": {**caller["query"], "cursor": first["next_cursor"]}}
    second = rig.call(next_page)["result"]
    assert second["next_cursor"] is None
    assert query_requests == [wanted, {**wanted, "cursor": "second"}]
    assert caller["query"] == wanted and next_page["query"] == query_requests[1]
