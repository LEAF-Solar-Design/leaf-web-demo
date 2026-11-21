"""Continuation tokens for authored inventories belong to one authority and query."""
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from test_integration_tool_service import rig, service
from test_x03_project_dataset_discovery import record


CASES = (
    ("List the ready project datasets.", "project"),
    ("Show the ready datasets I can read.", "filter"),
    ("Which ready datasets are available?", "sort"),
)


@pytest.mark.parametrize("ask,change", CASES)
def test_dataset_report_continuation(rig, ask, change):
    requests = []
    def catalog(request):
        after = request.url.params.get("after")
        requests.append(after)
        name = "east" if after else "west"
        return httpx.Response(200, json={"records": [record(name, "ready")],
                                         "nextCursor": None if after else "p2"})

    rig.svc.transport = httpx.MockTransport(catalog)
    scope = rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id
    def report(prompt, sort="title", cursor=None):
        return rig.svc.run_authored_dataset_report(prompt, *scope, limit=1, page_size=1,
                                                   sort=sort, cursor=cursor)

    first = report(ask)
    assert [row["title"] for row in first["datasets"]] == ["east"]
    cursor = first["next_cursor"]
    assert cursor and cursor != "p2"
    second = report(ask, cursor=cursor)
    assert [row["title"] for row in second["datasets"]] == ["west"]
    assert second["next_cursor"] is None
    assert set(requests) == {None, "p2"}
    requests.clear()
    if change == "project":
        rig.current = replace(rig.current, authority=replace(rig.auth, project_id=str(uuid4())))
    with pytest.raises(service.ReadFailure, match="cursor_mismatch"):
        report(ask.replace("ready", "stale") if change == "filter" else ask,
               "-title" if change == "sort" else "title", cursor)
    assert requests == []
