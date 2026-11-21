"""Project declared catalog columns without fabricating absent metadata."""
import httpx
import pytest

from test_integration_tool_service import rig, service
from test_x03_project_dataset_discovery import record


CASES = [
    ("List project datasets with columns title and synced_at.", "id",
     [{"title": "alpha", "synced_at": None}, {"title": "bravo"}]),
    ("Show my datasets with fields status, source_updated_at.", "version",
     [{"status": "ready", "source_updated_at": None}, {"status": "unavailable"}]),
    ("Which datasets are available? Return columns fetched_at and freshness.", "digest",
     [{"fetched_at": "2026-10-20T12:00:00Z",
       "freshness": {"synced_at": None, "source_updated_at": None, "fetched_at": "2026-10-20T12:00:00Z"}},
      {"freshness": None}]),
]


@pytest.mark.parametrize("ask,identity_field,expected", CASES)
def test_declared_catalog_projection(rig, ask, identity_field, expected):
    scope = rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id
    rows = [record("alpha", "ready"), record("bravo", "unavailable")]
    visits = []

    def catalog(request):
        index = int(request.url.params.get("after", 0))
        visits.append(index)
        return httpx.Response(200, json={"records": [rows[index]], "nextCursor": "1" if index == 0 else None})

    rig.svc.transport = httpx.MockTransport(catalog)
    second_ref = rows[1]["reference"]
    value = second_ref.pop(identity_field)
    with pytest.raises(service.ReadFailure, match="^invalid_response$"):
        rig.svc.run_authored_dataset_report(ask, *scope, limit=1)
    assert visits == [0, 1]
    second_ref[identity_field] = value
    report = rig.svc.run_authored_dataset_report(ask, *scope, limit=1)
    assert report["datasets"] == [
        {"dataset_ref": row["reference"], **wanted} for row, wanted in zip(rows, expected)]
    assert report["datasets_scanned"] == report["pages_read"] == 2
    # Sorting still uses the validated title when the projection leaves it out.
    first = rig.svc.run_authored_dataset_report(ask, *scope, limit=1, page_size=1, sort="-title")
    assert first["datasets"] == report["datasets"][1:] and first["next_cursor"]
    second = rig.svc.run_authored_dataset_report(
        ask, *scope, limit=1, page_size=1, sort="-title", cursor=first["next_cursor"])
    assert second["datasets"] == report["datasets"][:1] and second["next_cursor"] is None
