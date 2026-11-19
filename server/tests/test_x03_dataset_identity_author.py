"""Reconcile project dataset identity before filtering or publishing a report."""
from copy import deepcopy

import httpx
import pytest

from test_integration_tool_service import rig, service
from test_x03_project_dataset_discovery import record


CASES = [
    ("List every project dataset once across catalog pages.", ["frames", "panels"]),
    ("Which ready datasets can this checker read?", ["frames"]),
    ("Show the stale datasets in the project inventory.", ["panels"]),
]


@pytest.mark.parametrize("ask,expected", CASES)
def test_repeated_catalog_identity(rig, ask, expected):
    original = record("frames", "ready")
    repeat = deepcopy(original)
    pages = [[original, record("panels", "stale")], [repeat]]
    visited = []

    def catalog(request):
        cursor = request.url.params.get("after")
        visited.append(cursor)
        return httpx.Response(200, json={"records": pages[bool(cursor)],
                                         "nextCursor": None if cursor else "next"})

    rig.svc.transport = httpx.MockTransport(catalog)
    scope = rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id
    result = rig.svc.run_authored_dataset_report(ask, *scope, limit=2)
    assert [row["dataset_ref"]["id"] for row in result["datasets"]] == expected
    assert result["datasets_scanned"] == 2
    assert visited == [None, "next"]

    for changed_pin in ({"digest": "b" * 64}, {"version": 2}):
        repeat["reference"] = {**original["reference"], **changed_pin}
        with pytest.raises(service.ReadFailure, match="identity_conflict"):
            rig.svc.run_authored_dataset_report(ask, *scope, limit=2)
