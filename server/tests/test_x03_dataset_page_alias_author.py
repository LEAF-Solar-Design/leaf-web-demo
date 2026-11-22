"""A local HTTP catalog deliberately builds every page from the same objects."""
from copy import deepcopy

import httpx
import pytest

from test_integration_tool_service import rig
from test_x03_project_dataset_discovery import record


CASES = [
    ("List my project datasets with columns title and fields.", ("title", "fields")),
    ("Show connected datasets with columns fields, freshness.", ("fields", "freshness")),
    ("Which ready datasets can I read? Return columns fields and fetched_at.", ("fields", "fetched_at")),
]


@pytest.mark.parametrize("ask,columns", CASES)
def test_reused_catalog_page_is_an_immutable_observation(rig, ask, columns):
    scope = rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id
    # Both fields also share the same nested type object inside the local provider.
    row = deepcopy(record("placeholder", "ready"))
    shared_type = row["fields"][0]["type"]
    row["fields"] = [{**row["fields"][0], "id": field, "type": shared_type} for field in ("a", "b")]
    page = {"records": [row], "nextCursor": None}
    identities = []
    items = [("beam", "m", "2026-09-01"), ("panel", "W", "2026-09-02"), ("load", "A", "2026-09-03")]

    def catalog(request):
        index = int(request.url.params.get("after", 0))
        name, unit, date = items[index]
        row["title"] = name.upper()
        row["reference"]["id"] = name
        row["freshness"]["fetched_at"] = date + "T12:00:00Z"
        shared_type["unit"] = unit
        row["fields"].sort(key=lambda field: field["id"], reverse=index == 1)
        page["nextCursor"] = str(index + 1) if index < 2 else None
        identities.append((id(page), id(row), id(row["fields"]), id(row["reference"]), id(shared_type)))
        return httpx.Response(200, json=page)

    rig.svc.transport = httpx.MockTransport(catalog)
    result = rig.svc.run_authored_dataset_report(ask, *scope, limit=1)
    assert len(identities) == 3 and len(set(identities)) == 1
    assert result["datasets_scanned"] == result["pages_read"] == 3
    assert [entry["dataset_ref"]["id"] for entry in result["datasets"]] == [name for name, _, _ in items]
    for index, entry in enumerate(result["datasets"]):
        name, unit, date = items[index]
        assert set(entry) == {"dataset_ref", *columns}
        field_ids = ["b", "a"] if index == 1 else ["a", "b"]
        assert [(f["id"], f["type"]["unit"]) for f in entry["fields"]] == [(f, unit) for f in field_ids]
        if "title" in columns:
            assert entry["title"] == name.upper()
        if "freshness" in columns:
            assert entry["freshness"]["fetched_at"] == date + "T12:00:00Z"
        if "fetched_at" in columns:
            assert entry["fetched_at"] == date + "T12:00:00Z"
