"""A strict discovery report must come from one inventory revision."""
import httpx
import pytest

from test_integration_tool_service import rig, service
from test_x03_project_dataset_discovery import record


CASES = [
    ("List all datasets from one catalog snapshot.", ["panels", "frames"]),
    ("Show the stale datasets from a consistent inventory.", ["frames"]),
    ("Which ready datasets belong to the same catalog revision?", ["panels"]),
]


@pytest.mark.parametrize("ask,expected", CASES)
def test_inventory_requires_one_snapshot(rig, ask, expected):
    inventory = {"rev1": ["panels", "frames"], "rev2": ["issues", "risers"]}
    state = {"snapshot": "rev1", "change": False}
    pages = []
    def catalog(request):
        index = int(request.url.params.get("after", "0"))
        if state["change"] and index:
            state["snapshot"] = "rev2"
        rev = state["snapshot"]
        pages.append((rev, index))
        row = record(inventory[rev][index], ["ready", "stale"][index])
        return httpx.Response(200, json={"records": [row], "nextCursor": "1" if index == 0 else None,
                                        "snapshotId": rev})

    rig.svc.transport = httpx.MockTransport(catalog)
    scope = rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id
    report = rig.svc.run_authored_dataset_report(ask, *scope, limit=1)
    assert [r["dataset_ref"]["id"] for r in report["datasets"]] == expected
    assert pages == [("rev1", 0), ("rev1", 1)]
    pages.clear()
    state["change"] = True
    with pytest.raises(service.ReadFailure, match="snapshot_changed"):
        rig.svc.run_authored_dataset_report(ask, *scope, limit=1)
    assert pages == [("rev1", 0), ("rev2", 1)]


def test_strict_report_requires_provider_snapshot_evidence(rig):
    rig.response = {"records": [], "nextCursor": None}
    with pytest.raises(service.ReadFailure, match="snapshot_unavailable"):
        rig.svc.run_authored_dataset_report(CASES[0][0], rig.auth.tenant_id,
                                            rig.auth.session_id, rig.auth.turn_id)
