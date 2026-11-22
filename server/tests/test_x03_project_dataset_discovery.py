"""Exercise the turn-scoped discovery service with a local paged Ops catalog."""
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from test_integration_tool_service import FIELD, REF, read, rig, service


CASES = [
    ("Show which connected project datasets my drawing checker can read, including stale ones.",
     ["panels", "issues"], ["ready", "stale"]),
    ("List all inspection datasets across pages and flag outdated results.",
     ["loads", "frames", "history"], ["empty", "ready", "stale"]),
    ("Which connected data is stale or unavailable in this drawing project?",
     ["survey", "beams", "unused"], ["stale", "unavailable", "empty"]),
]


def record(name, status):
    fresh = read()["freshness"] if status != "unavailable" else None
    return {"reference": {**REF, "id": name}, "title": name, "fields": [FIELD],
            "lastAttempt": {"outcome": "failure" if status in {"stale", "unavailable"} else "success",
                            "row_count": int(status == "ready")}, "freshness": fresh}


@pytest.mark.parametrize("ask,names,expected", CASES)
def test_project_dataset_traversal(rig, ask, names, expected):
    visited = []
    def fetch(request):
        assert request.method == "GET"
        assert request.url.path == f"/api/v1/projects/{rig.auth.project_id}/integrations/datasets"
        cursor = request.url.params.get("after")
        index = int(cursor[1:]) if cursor else 0
        visited.append(cursor)
        return httpx.Response(200, json={"records": [record(names[index], expected[index])],
                           "nextCursor": f"p{index+1}" if index+1 < len(names) else None})

    rig.svc.transport = httpx.MockTransport(fetch)
    pages = list(rig.svc.iter_dataset_pages(rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id, limit=1))
    rows = [row for page in pages for row in page["datasets"]]
    assert [(r["dataset_ref"]["id"], r["status"]) for r in rows] == list(zip(names, expected)), ask
    assert visited == [None, *(f"p{i}" for i in range(1, len(names)))]


def test_discovery_stops_on_two_cursor_cycle(rig):
    seen = []
    def fetch(request):
        after = request.url.params.get("after")
        seen.append(after)
        return httpx.Response(200, json={"records": [], "nextCursor": {None:"a", "a":"b", "b":"a"}[after]})
    rig.svc.transport = httpx.MockTransport(fetch)
    with pytest.raises(service.ReadFailure, match="invalid_response"):
        list(rig.svc.iter_dataset_pages(rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id, limit=1))
    assert seen == [None, "a", "b"]


def test_discovery_rechecks_project_before_next_page(rig):
    rig.response = {"records": [], "nextCursor":"opaque"}
    pages = rig.svc.iter_dataset_pages(rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id)
    assert next(pages)["next_cursor"] == "opaque"
    rig.current = replace(rig.current, authority=replace(rig.auth, project_id=str(uuid4())))
    with pytest.raises(service.ReadFailure, match="denied|unavailable"):
        next(pages)
    assert len(rig.requests) == 1
