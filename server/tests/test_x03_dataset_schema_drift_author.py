"""Keep each dataset schema and the provider's declared schema revision coherent."""
from copy import deepcopy

import httpx
import pytest

from test_integration_tool_service import rig, service
from test_x03_project_dataset_discovery import record


CASES = [
    ("List every project dataset under one schema revision.", "type"),
    ("Show ready datasets across pages with a consistent schema version.", "revision"),
    ("Which datasets share the same catalog schema revision?", "missing"),
]


@pytest.mark.parametrize("ask,drift", CASES)
def test_provider_schema_coherence(rig, ask, drift):
    scope = rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id
    original = deepcopy(record("panels", "ready"))
    other = deepcopy(record("risers", "ready"))
    other["fields"][0]["type"] = {"kind": "boolean"}
    repeat = deepcopy(original)
    pages = [
        {"records": [original, other], "nextCursor": "next", "schemaRevision": "forms-v3"},
        {"records": [repeat], "nextCursor": None, "schemaRevision": "forms-v3"},
    ]
    for page in pages:
        page["snapshotId"] = "inventory-173"
    visited = []

    def catalog(request):
        index = int("after" in request.url.params)
        visited.append(index)
        return httpx.Response(200, json=pages[index])

    rig.svc.transport = httpx.MockTransport(catalog)
    second = pages[1]
    if drift == "type":
        repeat["fields"][0]["type"] = {"kind": "boolean"}
    elif drift == "revision":
        second["schemaRevision"] = "forms-v4"
    else:
        del second["schemaRevision"]
    with pytest.raises(service.IncompleteInventory, match="^incomplete_inventory$"):
        rig.svc.run_authored_dataset_report(ask, *scope, limit=2)
    assert visited == [0, 1]

    repeat["fields"] = deepcopy(original["fields"])
    second["schemaRevision"] = pages[0]["schemaRevision"]
    visited.clear()
    result = rig.svc.run_authored_dataset_report(ask, *scope, limit=2)
    assert [row["dataset_ref"]["id"] for row in result["datasets"]] == ["panels", "risers"]
    assert result["datasets_scanned"] == result["pages_read"] == 2
    assert visited == [0, 1]


def test_schema_revision_is_independent_of_inventory_snapshot(rig):
    scope = rig.auth.tenant_id, rig.auth.session_id, rig.auth.turn_id
    pages = [{"records": [record("east", "ready")], "nextCursor": "p2", "snapshotId": "i1"},
             {"records": [record("west", "ready")], "nextCursor": None, "snapshotId": "i1"}]
    rig.svc.transport = httpx.MockTransport(lambda request: httpx.Response(
        200, json=pages[int("after" in request.url.params)]))
    ask = "List the project datasets."
    assert rig.svc.run_authored_dataset_report(ask, *scope)["datasets_scanned"] == 2
    with pytest.raises(service.IncompleteInventory, match="^incomplete_inventory$"):
        rig.svc.run_authored_dataset_report(CASES[0][0], *scope)
    pages[1]["schemaRevision"] = 2
    with pytest.raises(service.IncompleteInventory, match="^incomplete_inventory$"):
        rig.svc.run_authored_dataset_report(ask, *scope)
    pages[0]["schemaRevision"] = 2
    pages[1]["snapshotId"] = "i2"
    with pytest.raises(service.ReadFailure, match="^snapshot_changed$"):
        rig.svc.run_authored_dataset_report(CASES[0][0], *scope)
