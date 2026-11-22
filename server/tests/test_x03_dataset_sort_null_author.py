"""Require valid provider titles before any sorted inventory is returned."""
import httpx
import pytest

from test_integration_tool_service import rig, service
from test_x03_project_dataset_discovery import record


CASES = [
    ("List all project datasets from title A to Z.", "title", {"title": None}, 0),
    ("Show my datasets from last title to first.", "-title", {}, 1),
    ("Which ready datasets are in title order?", "title", {"title": 7}, 2),
]


@pytest.mark.parametrize("ask,sort,invalid,position", CASES)
def test_catalog_sort_key_contract(rig, ask, sort, invalid, position):
    scope = rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id
    labels = [("west", "Roof"), ("east", "Beam"), ("arch", "Roof")]
    rows = [{**record(key, "ready"), "title": name} for key, name in labels]
    requests = []

    def catalog(request):
        index = int(request.url.params.get("after", 0))
        requests.append(index)
        return httpx.Response(200, json={"records": [rows[index]],
                                         "nextCursor": str(index + 1) if index < 2 else None})

    rig.svc.transport = httpx.MockTransport(catalog)
    original = rows[position]
    rows[position] = {**{k: v for k, v in original.items() if k != "title"}, **invalid}
    with pytest.raises(service.ReadFailure, match="^invalid_response$"):
        rig.svc.run_authored_dataset_report(ask, *scope, sort=sort, limit=1, page_size=2)
    assert requests == list(range(position + 1))

    rows[position] = original
    result = rig.svc.run_authored_dataset_report(ask, *scope, sort=sort, limit=1, page_size=2)
    assert result["next_cursor"]
    next_page = rig.svc.run_authored_dataset_report(
        ask, *scope, sort=sort, limit=1, page_size=2, cursor=result["next_cursor"])
    # Hand-ranked by title, then stable ID, both in the requested direction.
    expected = ["east", "arch", "west"] if sort == "title" else ["west", "arch", "east"]
    assert [row["dataset_ref"]["id"] for row in result["datasets"] + next_page["datasets"]] == expected
    assert next_page["next_cursor"] is None
